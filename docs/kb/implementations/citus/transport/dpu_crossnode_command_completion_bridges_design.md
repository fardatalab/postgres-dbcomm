# Cross-node selected-DPU command/completion bridges — design + implementation steps

> **Status:** DESIGN AGREED (July 10, 2026), ready to implement. Feeds the merged **S4+S5** portion of
> [dpu_command_plane_migration_plan.md](./dpu_command_plane_migration_plan.md).
>
> **Scope (user-confirmed):** Homer is RDMA-only; the single-node "local fast path" is being deleted, so
> there is no single-node gate. The gate is a **cross-node** real-pgbench SELECT: client on node A,
> PostgreSQL backend on node B, each talking to its **own local DPU** by DMA, the two DPUs relaying over
> RDMA. This merges the old single-node S4 into the cross-node bring-up (formerly S5).

## 1. The one pipeline (everything is an instance of it)

Every object — command, completion, tuple result — moves through the **same** six-step pipeline. Producer
and consumer always talk to their **own local DPU** by DMA; only the DPU↔DPU leg is RDMA:

```
producer host  --DMA(pull)-->  local DPU  --RDMA egress-->  peer DPU landing ring
   --DMA(publish)-->  consumer host  --consumes via its local role--
```

Split into an **egress half** (local pull → RDMA send, on the producer's DPU) and an **ingress half**
(RDMA land → local DMA-publish, on the consumer's DPU), the three objects are:

| Object | Egress half (producer DPU) | Ingress half (consumer DPU) | Status |
|---|---|---|---|
| **Command**    | node A: pull role 1 → RDMA send        | node B: land → publish role 2         | **build** |
| **Completion** | node B: pull role 3 → RDMA send        | node A: land → publish role 6         | **build** |
| **Result**     | node B: pull role 5 → RDMA send        | node A: land → deform → publish role 7 | **BUILT** (template) |

The **result row is the working template.** Command and completion are new *applications* of the identical
mechanism, not new mechanisms. Role map, for reference:

| Role | Direction | Object | Exported by |
|---|---|---|---|
| 1 | client→DPU | control slot (START/OPEN) | client (node A) |
| 2 | DPU→backend | backend command mailbox | backend arena slot (node B) |
| 3 | backend→DPU | backend completion mailbox | backend arena slot (node B) |
| 5 | backend→DPU | result byte ring | backend arena slot (node B) |
| 6 | DPU→client | client completion events | client (node A) |
| 7 | DPU→client | decoded result tuples | client (node A) |

## 2. What is already built vs. what we build

**The RDMA substrate is complete — do NOT reinvent it.** Persistent session-shared critical-control QP
(`tuple_sink_service_process.c:36138`, shared per `:36143`; node A holds `clientSqlPeerConnectionHandle`
`:36212`, node B holds the accepted handle+generation+identity `:38387`); ordered `WRITE_WITH_IMM`
publication (`remote_execution_peer_transport_rdma.c:9605`); reverse consumed-head credit
(`HomerServicePostReceiverHeadAck` → `TupleSinkServicePostPeerUint64WithImmediateResultRdma` `:31620`);
MR registration/exchange; `sessionUID`-based routing (`HomerDpuDmaFindDpuToHostByteRingRef`
`homer_service_dpu_dma.c:6373`); and **fixed-record** send primitives already exist
(`...PostPeerRegisteredClientCompletionBytesWithImmediateRdma` `:18485`, `...PostPeerUint64...` for
scalars). So control-plane records need nothing new on the wire.

**The DMA endpoints already exist** as DPU-DMA scheduler collectors (`tuple_sink_service_process.c:2489`–
`:2499`): role-1 pull `DPU_COMMAND_PULL`; role-2 materialize+publish `DPU_BACKEND_COMMAND_STAGE`
(consumer A, `:40050`) + `DPU_BACKEND_COMMAND_PUBLISH` (`:43407`); role-3 pull `DPU_BACKEND_COMPLETION_PULL`
(consumer B, `:43295`); role-6 push (`HomerServiceDpuPublishOneSelectedCompletionEvent` `:40405`); role-5
pull `DPU_PAYLOAD_PULL`; role-7 the tuple deform relay (`:32475`). **Consumers A/B only ever produce/consume
their LOCAL role** — that is their whole job and it does not change.

> **⚠ CORRECTION (July 10, 2026, codex trace + hand-verified): role-5 DPU discovery has NEVER existed —
> for ANY path.** The "results are the built template" claim above is true only for the *transport* layers:
> the mirror pull, mirrored-range egress pump, RDMA byte-stream, deform relay, and credit machinery are
> real and proven — **for role 4/role 7 (basebackup + DPU→host relay)**. Role 5 (SQL result ring) was
> never wired into discovery:
> - Grouped-control enrolment is a role WHITELIST — exactly {1, 4, 7} (`HomerDpuDmaDescriptorUsesHostPublishLine`,
>   `homer_service_dpu_dma.c:6632`–`:6661`; its comment explicitly names 2/3/5/8 as "UNREGISTERED").
>   Nothing else ever advances a role-5 ring's `acceptedPublishedTail` (`:11359` is the only writer, fed
>   solely by grouped-control snapshot acceptance).
> - The "working" SQL-UDF Tier-2 path never needed it: its result ring is consumed HOST-side (the frontend
>   maps the shm object by name from the completion's `resultQueueDescriptor`) — the DPU never mirror-pulls
>   role 5 there.
> - Consequently the mirror-pool join is also missing for arena rings: `HomerDpuDmaResolveMirrorSlot`
>   looks the pool up **by descriptor `serviceSinkId`** (`:4355`–`:4371`) and rejects sink 0 (`:4333`);
>   the pool slot is bound by `(parentServiceSessionId, serviceStreamId)` (`tuple_sink_service_process.c:17241`);
>   the pool rejects stream 0 (`homer_dpu_byte_ring_pool.c:145`). So `descriptor sink id == service stream id`
>   is the JOIN KEY — which kills the "sink-0 wildcard match" fix idea (recorded below as rejected).
>
> What this changes: S4.0b (P0) grows the *discovery enablement* and the engine's *bound-sink* concept;
> P3.1 stops being "little/no new code" and becomes the real work item that creates the service-side
> stream entry + mirror-pool slot + sink binding. The full expanded specs are in §4 P0/P3 below.

**What we build is only the RDMA relay halves for command and completion**, mirroring how the payload-stream
pump relays results (`HomerServicePumpOutgoingDpuMirrorByteRingPayload` `:29845`,
`HomerServicePumpIncomingTupleViewDpuTwoRingRelay` `:32475`):

- Two **fixed-record landing rings** — one per direction (command A→B on node B; completion B→A on node A),
  registered as RDMA targets, with WIMM doorbell + reverse credit. **No sharing/multiplexing** (decided).
- A **command-egress** driver (node A) and a **completion-egress** driver (node B), each a **new
  scheduler-granted collector/action** — egress is driven by scheduler grant (decided), so it takes its
  place in the progress machine like every other DPU action.
- A thin **ingress** step per direction: the WIMM doorbell marks a landed record; the existing DMA endpoint
  publishes it (consumer A for command role-2; the role-6 push for completion).

### Design decisions, locked (July 10, 2026)
1. **Command bridge:** node-A egress ships the staged role-1 command to a node-B command landing ring;
   **consumer A's materialize+publish core is refactored to take a plain command record** so it can be fed
   from the landing ring (node B) instead of engine staging. This AVOIDS an engine-staging injection API
   (which does not exist and is high-risk, `homer_service_dpu_dma.h:563`). Consumer A is NOT bypassed
   (rejected: direct-mailbox RDMA into arena role 2).
2. **Completion bridge:** node-B egress ships consumer B's queued completion event to a node-A completion
   landing ring; node-A DPU DMAs it to role 6 (reusing the role-6 push guts). Lands via node-A's DPU
   (mirrors the result relay), **not** a direct RDMA-write into node-A host memory (rejected: the old
   host-service Tier-2 shape, asymmetric with command+result).
3. **No egress-route carried in records / no selected→frontend back-pointer.** Egress is orchestrated at
   the level that already owns the peer connection (the peer session) and routed by `sessionUID`; the
   DPU-scheduler consumers A/B stay connection-unaware. This retires the earlier over-engineered
   "egress route in the ingress record" idea.
4. **One fixed-record landing ring per direction**; the tuple COPY / non-client ring path
   (`PublishPeerCommandCompletion`, `:18682`) is out of scope and untouched.

## 3. Scheduler & session integration

### 3.0 Which template the egress collectors follow (and why they are simpler than results)

The result path taught us that a DPU relay half is really **two** scheduler citizens, and they are drawn
from **two different templates**. It matters which one commands/completions copy.

- **DMA endpoint = a fixed-record DPU collector.** `DPU_PAYLOAD_PULL` (role-5 host ring → DPU-local
  mirrored ring) is armed by an *exact fact* — `facts.totalDiscoveredReadyRingCount > 0`, arming line
  `:41071` — and its action submits a bounded batch of DMA tasks, action case `:43xxx`. Its siblings
  `DPU_BACKEND_COMMAND_PUBLISH` (armed by `dpuDmaState->backendCommandPublishCount > 0`, `:41093`; action
  `:43366`) and `DPU_COMPLETION_PUSH` (armed by `completionPushReadyCount > 0`, `:41169`) are the same
  shape: *exact-count armed, bounded fixed-record batch per grant.*
- **RDMA egress of results = the payload-stream source, NOT a DPU collector.** The role-5 bytes are sent to
  the peer by `PAYLOAD_FRONTIER`→`PAYLOAD_OUTGOING`, which only reaches
  `HomerServicePumpOutgoingDpuMirrorByteRingPayload` (`:29845`, dispatched at `:30456`) after querying
  `HomerDpuDmaMirroredByteRangeReadyForServiceSink`. That source exists because a result is a
  **variable-length, credit-metered, EOS-terminated byte stream** — it owns *seven* actions
  (OUTGOING / INCOMING / SEND_CQ / CREDIT_PUBLISH / EOS / CLOSE_RECLAIM / BLACKHOLE, enum `:2534`+).

**Commands and completions are fixed-size records, so their RDMA egress copies the FIRST template, not the
second.** Each new egress collector is exactly one more fixed-record DPU collector — exact-count armed,
one RDMA WR per record — and needs **none** of the byte-stream layer. Concretely:

| aspect | result egress (built) | command / completion egress (this doc) |
| --- | --- | --- |
| scheduler citizen | payload-stream source (`PAYLOAD_OUTGOING`) | one new fixed-record DPU collector each |
| unit | byte range `[writtenTail, frontier)` | one fixed record |
| arming | mirrored-byte-range readiness query | exact queue depth (like PUBLISH / COMPLETION_PUSH) |
| flow control | byte-granular credit frontier + EOS | per-slot reverse credit on an N-slot ring |
| actions | 7 (OUTGOING/INCOMING/SEND_CQ/CREDIT/EOS/…) | 1 (drain up to `maxItems`, send, retire) |

That is the "should be simpler than that" the discussion flagged: we skip the entire payload-stream machine
and land on the same two-line arm / bounded-drain action that PUBLISH already is.

**Settled (July 10, 2026, user-confirmed): build the fixed-size RDMA half; do NOT lift the variable-size
payload-stream half for fixed records.** Two reasons, so nobody re-litigates this:
1. *"Fixed can ride variable" is true at the substrate level and false at the scheduler-source level.* The
   payload-stream source is coupled to being a byte stream in three places that cannot be cheaply
   decoupled: its ingress is the tuple-deform relay (`:32475` — there is no generic byte receiver to
   reuse, so a command receiver would be new code anyway); its egress starts from a *mirrored byte range*
   produced by role-5 DMA (commands live as staged fixed records, so they would first need a synthetic
   byte-ring encoding); and its credit is byte-granular where a fixed ring wants per-slot credit. Reusing
   it is MORE code than the fixed half, and it puts latency-critical control records behind the
   bulk-payload scheduler.
2. *The fixed-size half is ~90% retarget, not new transport.* Both directions already have working
   fixed-record RDMA sends in-tree on the shared substrate: the peer completion publish
   (`TupleSinkServicePublishPeerClientCommandCompletion` `:18181` — fixed-slot ring
   `completionSlots[slotIndex]` masked at `:18273`, WIMM doorbell `:18277`, reverse slot credit `:18288`
   returning BLOCKED_SOURCE_CREDIT, EOS guard `:18252`) and the peer command post
   (`TupleSinkServicePumpRemoteClientSqlCommands`'s body-then-readySeq publication `:20719`/`:20737` into
   `clientSqlPeerCommandMailboxDescriptor`). "Build the fixed half" concretely means retargeting those two
   sends from a host mailbox to a DPU landing ring.

What stays uniform across command/completion/result is the **substrate** (QP, MR exchange, WIMM
publication, reverse credit, `sessionUID` routing) and the **orchestration shape** (pull → egress → land →
publish). What differs is only the record **framing** — fixed slot ring vs. byte ring — and that split is
inherent to the objects (results are variable-length; commands/completions are fixed). Two framings over
one substrate is the minimal design, not a compromise.

### 3.1 The two new egress collectors — no new queues, just new consumers

The decisive simplification: **the egress collectors reuse the queues the existing pull collectors already
fill.** On node A, `DPU_COMMAND_PULL` (role-1 → *staged command requests*, `facts.totalStagedCommandRequestCount`)
already produces exactly what egress must forward; on node B, `DPU_BACKEND_COMPLETION_PULL` /
`HomerServiceDpuAcceptOneBackendCompletion` (`:40344`) already queues the *completion event*
(`HomerServiceDpuSelectedCompletionEventReadyCount`, `:41168`). Egress is a new **consumer/drain** of those
queues, chosen instead of the local publish drain — no second staging structure.

**The routing fork is decided upstream, at enqueue, by session nature — the egress action stays
routing-decision-free.** A selected session on node A is a `clientSqlRemoteSender`; on node B it is a
`clientSqlPeerReceiver` + arena. So:

- node A (no local backend) never runs `DPU_BACKEND_COMMAND_STAGE`/`_PUBLISH`; its staged commands go to
  `DPU_PEER_COMMAND_EGRESS`.
- node B (client is remote) never runs `DPU_COMPLETION_PUSH` (role-6 is node A's arena); its completion
  events go to `DPU_PEER_COMPLETION_EGRESS`.

Because both the local-publish arming and the egress arming would otherwise key on the *same* counter
(`totalStagedCommandRequestCount` / the completion-event count), keep a small **per-role depth counter**
maintained at enqueue — `peerCommandEgressDepth` (node A) mirroring `backendCommandPublishCount`, and reuse
the completion-event count on node B, since in cross-node-only *every* completion is peer-destined. Arm the
egress collector off that counter, exactly as `DPU_BACKEND_COMMAND_PUBLISH` arms off `backendCommandPublishCount`
at `:41093`. No scan, no per-pass filtering.

**`DPU_PEER_COMMAND_EGRESS` — node A.**
- *Arm* (in `HomerServiceAppendDpuDmaCollectorCandidates` `:40913`, beside the PUBLISH arm): append the
  candidate with `readyHint = peerCommandEgressDepth`, `known=true`, `ready = depth > 0`.
- *Execute* (new case in the action switch, modeled on `DPU_BACKEND_COMMAND_PUBLISH` `:43366`): compute
  `maxItems` from the grant; loop up to `maxItems`: peek the next staged command for a remote-sender
  session, resolve `sessionUID → peer session → connection handle + remote command-landing-ring slot`,
  check remote slot credit, RDMA-send the fixed control record (WIMM publication gate), retire the staged
  slot and decrement the counter. Set `itemsProcessed` / `emptyPolls` / `stillReady` / `budgetExhausted`
  exactly like the PUBLISH case (`:43432`+).

**`DPU_PEER_COMPLETION_EGRESS` — node B.**
- *Arm*: append with `readyHint = HomerServiceDpuSelectedCompletionEventReadyCount(dpuDmaState)` gated by
  the **EOS-ordering guard being satisfied** — do not arm a terminal completion whose result bytes have not
  finished sending (`:18252`/`:18162`). A completion still blocked on result EOS reports
  `HOMER_PROGRESS_REASON_DEPENDENT_COMPLETION_WAIT_PAYLOAD_EOS` (`:2411`) rather than arming.
- *Execute* (new case, modeled on the completion drain guts of
  `HomerServiceDpuPublishOneSelectedCompletionEvent` `:40405`, but sending instead of DMA-publishing): drain
  up to `maxItems` completion events, resolve `sessionUID → node-A completion-landing-ring slot`, reuse the
  send structure of `TupleSinkServicePublishPeerClientCommandCompletion` (`:18485`) retargeted from the
  node-A host mailbox to the node-A DPU landing ring, retire the event.

### 3.2 Ingress — reuse the existing publish collectors, fed by the recv doorbell

Ingress needs **no new collector**: it re-lands each record into the very queue the *peer's* pull collector
would have filled, then the existing publish collector drains it to host memory.

- **Landed command on node B** → mark it as a staged command for its session → the existing consumer A
  (`DPU_BACKEND_COMMAND_STAGE` → `_PUBLISH`) DMA-publishes role 2. (This is why P1.2 refactors consumer A's
  core to accept a *plain record* — so a landed record and an engine-staged one enter the same path.)
- **Landed completion on node A** → queue it as a selected completion event → the existing
  `DPU_COMPLETION_PUSH` DMAs it to role 6.

The recv side rides the already-registered WIMM recv doorbell (`:44822`): its handler resolves the landing
ring's owning session by `sessionUID` and enqueues the record; a later scheduler pass runs the publish
collector. So ingress is one recv handler + an enqueue, not a scheduler citizen of its own.

### 3.3 How the scheduler drives a new collector end to end (the five edits)

Adding either egress collector is the AGENTS.md **four enum/map edits plus the fifth naming edit**, and the
`-Wswitch` note that none of the four switches will warn you if you forget one:

1. `HOMER_PROGRESS_COLLECTOR_*` + `HOMER_PROGRESS_ACTION_*` enums (`:2489` / `:2524`).
2. collector→source map — both belong to a **peer-send** source (reuse `HOMER_PROGRESS_SOURCE_PEER_SEND_CQ`
   or a dedicated peer-egress source; they are send-CQ-bearing RDMA work, so they must share the peer
   send-CQ drain, not a DPU-DMA source).
3. collector→action map.
4. **NAME it in `HomerMachineBaselineCompileExecutionPlan`'s phase body** (`FindCollector` +
   `AppendCollectorAction`, `:41076`+ region) — the edit `-Wswitch` cannot catch; an unnamed collector is
   armed every pass and granted on none (the S3.3 silent-wedge failure mode).

Then the runtime loop drives it like every other collector: readiness pass appends the candidate with an
exact `readyHint`; the plan compiler grants a `maxItems` vector; the action drains up to `maxItems` and
reports `itemsProcessed`/`emptyPolls`/`stillReady`/`budgetExhausted`; per-collector feedback backs off an
empty poll. (Watch the known fix-B feedback **aliasing** — the DPU collectors share one feedback struct;
give each egress collector honest empty/productive accounting or an empty egress poll can nudge a sibling's
backoff. Not a blocker for correctness, but note it when wiring feedback.)

### 3.4 Session wiring & the two-struct gap

Both directions key on the peer session created at `:38387` (node B receiver) / the sender session on
node A, which already hold the connection handle, generation, and remote descriptors. The egress *action*
lives on `dpuDmaState` (which has no peer back-pointer — the two-struct gap), so it bridges the gap the same
way the result **ingress** relay already does: resolve `sessionUID → TupleSinkServiceFindSessionById`
(`:22648`) → peer session → connection + remote landing-ring descriptor, exactly as
`HomerServicePumpIncomingTupleViewDpuTwoRingRelay` resolves `sessionUID` at `:32564`. No new cross-struct
pointer is added; `sessionUID` is the sole routing key, already carried on role 1 (`homer_client.c:3081`)
and on role 7.

The **two landing-ring descriptors are exchanged in the existing `OPEN_COMMAND_SESSION` handshake**
(`:36219` request / `:38370` accept): node A advertises its completion landing ring in the OPEN request;
node B advertises its command landing ring in the response. Each side saves the peer's descriptor beside its
existing `clientSqlPeerConnectionHandle`, and the egress action reads it per record via the sessionUID
resolution above.

## 4. Implementation steps (incremental; each hop independently observable)

Order chosen so each step lights up a checkable signal before the next. Build with DOCA; validate on the
cross-node farnet0(client)→farnet1(backend) topology through both DPUs.

**Phase P0 — surrounding endpoints (no RDMA yet).**

> **⏺ P0 IMPLEMENTED (July 10, 2026): citus `68051ef88` (S4.1) + `ab850f892` (S4.0b).** All five S4.0b
> parts landed as specified below, plus two implementation details worth knowing:
> (1) the **sink gate** in grouped-control acceptance skips the ENTIRE discovered-ready block for a
> host→DPU byte ring with bound sink 0 (not just the enqueue — setting `discoveredReady`/the class
> counter without an enqueueable ref would permanently arm an idle pull collector, the S3.1b starvation
> shape), and `HomerDpuDmaBindArenaResultSink` completes the deferred enqueue at stamp time because each
> publication epoch is consumed exactly once; audited that every non-arena byte-ring export declares a
> nonzero sink (basebackup self-mints `generation ^ 0x5a5a…`), so the gate hits exactly arena role-5.
> (2) the **publish mirror** is generic tuple-sink infrastructure (`CitusTupleSinkAttachDpuPublishMirror`
> + a two-store stamp at the three commit sites: batch submit, error record, empty EOS) with its arena
> caller deferred to P3.1 (the sink handle doesn't exist until the first row-producing command).
> Validation: TCP smoke + v4 arena import + selected-DPU spawn + 4-role basebackup listener check — see
> the plan's P0 validation record.
- **S4.1** Add role 6 to `HomerClientOpenSqlSessionSelectedDpu` (`homer_client.c:3062`): bump
  `HOMER_CLIENT_DPU_COMMAND_SETUP_RING_COUNT` 1→2, add a role-6 descriptor as a client-completion structure
  the node-A DPU DMAs into (carry `serviceSessionId` + `sessionUID` like role 1). Checkpoint: the DPU setup
  log shows a 2-ring import for the session.

  *Grounded (July 10, 2026):*
  - The DPU-side publisher is already built and resolves the target by
    `FindDescriptorRef(bridgeGeneration, serviceSessionId, ROLE_FRONTEND_COMPLETION_EVENT)`
    (`tuple_sink_service_process.c:40412`) and DMAs one `HomerDpuBridgeFrontendCompletionEvent`
    (`homer_dpu_bridge_abi.h:252`) via `HomerDpuDmaSubmitFrontendCompletionEventPublication` (`:40428`).
  - The layout template is the deprecated Tier-2 exporter's role-6 descriptor
    (`homer_frontend_dma.c:1608-1627`): DPU_TO_HOST, `COMPLETION_SLOT|FIXED_SLOT_RING`, FIXED_SLOT geometry,
    `slotCount=1`, `slotBytes=controlBytes=ringBytes=sizeof(event)`, `hostRingOffset=hostControlOffset=0`.
  - **No extra bind step is needed**: the client PRE-ASSIGNS its session id
    (`stream->serviceSessionId = stream->dpuBridgeGeneration`, `homer_client.c:3001`) and declares it in the
    descriptor; import auto-binds declared ids (`boundServiceSessionId = descriptor.serviceSessionId`,
    `homer_service_dpu_dma.c:6809`). Role 6 declares the same id → resolvable immediately.
  - Client export layout is `[bridge header][hostPublishLines×N][dpuCreditLines×N][control slot]` with all
    offsets derived from the ring-count macro (`homer_client.c:2916-2920`, `:3003`); append the
    cache-line-aligned event line after the control slot and grow `mappingBytes`. Two-ring client exports
    are precedented (basebackup builds `descriptor[1]`, `:3428/:3453`), so setup-side handling is generic.
- **S4.0b** Turn on the arena role-5 credit lines (rebase role-5 arena descriptors onto the arena base; set
  `bridgeHeader.dpuCreditOffset = offsetof(HomerFrontendArena, dpuCreditLines)` — see plan §S4.0b/D8b), and
  wire the arena backend's result queue (see below). Fix the role-5 **sink-0 miss** (below). Checkpoint:
  backend produces role-5 bytes; the mirror machinery accepts them
  (`HomerDpuDmaMirroredByteRangeReadyForServiceSink` no longer rejects).

  *Grounded (July 10, 2026), EXPANDED after the role-5 discovery correction (§2 banner) — S4.0b is now
  five sub-parts, not two field edits:*

  - **(a) Descriptor rebase + credit lines, with ONE D8b value corrected.** Rebase the role-5 arena
    descriptors (`homer_frontend_agent.c:750-763`, component-addressed today) onto the arena base and set
    `bridgeHeader.dpuCreditOffset = offsetof(HomerFrontendArena, dpuCreditLines)` (`:210` holds it at 0).
    D8b's recorded `hostControlOffset = offsetof(arena, slots[k].resultRingControl)` is **superseded**: for
    an enrolled ring, `hostControlOffset` is the PUBLISH-LINE pointer (that is the roles-1/4/7 convention —
    e.g. the client's role-1 sets `hostControlOffset = hostPublishOffset`, `homer_client.c:3073`), so set
    `hostControlOffset = offsetof(arena, hostPublishLines[k*3+2])`, `controlBytes =
    sizeof(HomerDpuBridgeHostPublishLine)`. Nothing DPU-side ever reads the role-5 byte-ring control (pull
    math uses `hostRingOffset`, `homer_service_dpu_dma.c:11046`; storage = `ringBytes - hostRingOffset`
    `:2955`/`:4492`; credit dst = `hostRingAddress + dpuCreditOffset + ringIndex*64` `:11113`), so no
    address is lost. The rebased form passes every existing byte-ring check — verified: import validation
    imposes only in-range checks, no adjacency/zero-offset assumptions (`:6598`, `:6604`; the only
    adjacency check is spawn-region-specific, `:9828`) — EXCEPT the role-5 `controlBytes >=
    sizeof(CitusHomerPayloadByteRingControl)` branch (`:6578`), which must learn the publish-line shape
    (part c).
  - **(b) Discovery enablement — cash in D8 exactly as the whitelist comment prescribes.**
    `HomerDpuDmaDescriptorUsesHostPublishLine` (`:6632`) documents the promotion recipe itself: "(a) lay
    out a HostPublishLine array in the exporting region (already reserved: HomerFrontendArena.hostPublishLines,
    D8), (b) point their hostControlOffset at it, (c) add their roles here — IN THAT ORDER". Add role 5 to
    the whitelist, **gated by a new descriptor flag** (e.g. `HOMER_DPU_BRIDGE_RING_FLAG_PUBLISH_LINE`) set
    only by the arena builder — this keeps the dying Tier-2 per-session role-5 exporters (whose
    `hostControlOffset` semantics differ) inert without touching them. Bump
    `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` 3→4 so an undeployed DPU fails LOUD (`BAD_PROTOCOL`) instead of
    silently not discovering (both DPUs must be resynced+rebuilt — standing AGENTS.md rule).
    *Noted deviation:* the comment's "only after grouped-control actually groups" gate (a batching
    optimization that doesn't exist) is deliberately NOT honored — correctness bring-up accepts 16 single
    64-B reads per sweep; the batching optimization queues behind the cross-node gate. Watch the
    starve-diag listener cadence at P0 validation to confirm the extra enrolment doesn't shift scheduling.
  - **(c) Engine: `boundServiceSinkId`, replacing the REJECTED sink-0 wildcard.** The sink id is a JOIN
    KEY, not just a match predicate — `HomerDpuDmaResolveMirrorSlot` looks the mirror pool up BY descriptor
    sink (`:4355-:4371`, rejects 0 at `:4333`), and the pool slot is bound by `(session, streamId)`
    (`tuple_sink_service_process.c:17241`), so a wildcard cannot join; the arena ring must carry the REAL
    stream id. Extend D5's rule ("ring→session is always the DPU's binding") to the sink: add
    `boundServiceSinkId` to `HomerDpuDmaRingRuntime` + a `HomerDpuDmaRingBoundSinkId()` accessor
    (bound-if-nonzero, else descriptor-declared), a bind API to stamp it (called in P3 when the service
    creates the result stream), and use it at ALL FIVE sink-consuming sites:
    `HomerDpuDmaMirroredRangeMatchesServiceSink` (`:4446` — the egress data path),
    `ByteRingFrontierForServiceSink` (`:4693-4695`), `MarkImportSenderCloseReleased` (`:8579-8581`),
    `HoldImportForSenderClose` (`:8639`), and `ResolveMirrorSlot` (`:4355`). Role-5 validation branch
    (`:6578`) learns the flag-gated publish-line shape here too.
    **Phase gating:** grouped-control snapshot acceptance advances tails unconditionally, but only
    enqueues a ready reference when `RingBoundSinkId != 0` (one-shot diagnostic line otherwise) — so P0's
    discovery cannot spam mirror-resolve failures before P3 stamps the sink.
  - **(d) Backend result-queue wiring is a re-point, not a flip.** The legacy Tier-2 arm `shm_open`s a
    separate result object (`remote_execution_backend_bridge.c:2789-2843`, skipped for `arenaBackend`); the
    arena backend's ring lives INSIDE the already-mapped arena slot, and the backend consumes via
    `resultQueueControlMappingAddress` = control + contiguous storage — exactly the arena slot's
    `resultRingControl`/`resultRingStorage` layout (`_Static_assert`, `homer_frontend_agent.c:135`). Wire
    it in the arena bind block (`:2670-2723`, `arenaSlot` at `:2712`): point the mapping fields at
    `&arenaSlot->resultRingControl`, run `RemoteExecResetResultQueueControl` (`:630`) to establish full
    producer control state (arena bind at `homer_frontend_agent.c:432` skips the producer-owned flag),
    synthesize send/receive descriptors locally, `resultQueueReady = true`. No shm_open; no munmap of the
    arena mapping at teardown.
  - **(e) Producer publish-line hook.** The backend's result commit path must additionally update
    `arena->hostPublishLines[slot*3+2]` (tail/epoch/entryState) after committing to the ring control —
    that line is what grouped-control discovery reads. REUSE the canonical publish-line store helper the
    role-4 producer uses (locate it; do NOT hand-roll the store ordering). Gated on `arenaBackend`.

  **P0 checkpoint (adjusted — no result bytes can flow in P0):** commands don't execute until P3, so the
  S4.0b checkpoint is: v4 arena import accepted on the DPU (49 rings, role-5 descriptors carrying the
  publish-line flag), client 2-ring import accepted (S4.1), DPU-spawned backend binds the slot + wires the
  arena result ring + survives (FATAL-absence rule per AGENTS.md), TCP transport smoke green (mandatory for
  the ABI bump), starve-diag listener cadence unchanged with 16 newly-enrolled rings. An optional
  compile-gated backend probe (write one record via the normal commit path at bind, à la the S3.2
  TEST_FIRE precedent) can light discovery up early — deferred unless P3 debugging needs it.

**Phase P1 — command bridge (node-A role-1 → node-B role-2).**
- P1.1 Add the **command landing ring** on node B (fixed-record, WIMM, reverse credit), registered as an
  RDMA target; advertise its descriptor in the `OPEN_COMMAND_SESSION` response; node A saves it beside
  `clientSqlPeerConnectionHandle`.
- P1.2 **Refactor consumer A's core** (`HomerServiceDpuStageOneBackendCommandForPublish` `:40050`) to take a
  plain command record source, so it can be fed by (i) engine staging [legacy single-node] or (ii) a landed
  record. No behavior change for existing callers.
- P1.3 Add `DPU_PEER_COMMAND_EGRESS` (node A) per §3.1: it is a new **consumer** of the existing
  staged-command queue (no new queue), armed off a per-role `peerCommandEgressDepth` counter maintained at
  enqueue, executed like the `DPU_BACKEND_COMMAND_PUBLISH` case. It resolves the peer connection by
  `sessionUID` (§3.4) and RDMA-sends to the node-B command landing ring. Add the recv-doorbell handler on
  node B that re-lands the record into consumer A's staged-command path so `_STAGE`/`_PUBLISH` publish role 2.
- **Checkpoint P1:** a cross-node START reaches consumer A and DMA-publishes role 2; the backend runs the
  command (observable in the DPU log + backend behavior). Completion/result not yet returned.

**Phase P2 — completion bridge (node-B role-3 → node-A role-6).**
- P2.1 Add the **completion landing ring** on node A (fixed-record, WIMM, reverse credit); advertise its
  descriptor in the OPEN request; node B saves it on the receiver session.
- P2.2 Add `DPU_PEER_COMPLETION_EGRESS` (node B) per §3.1: a new **consumer** of the existing
  completion-event queue (no new queue), armed off `HomerServiceDpuSelectedCompletionEventReadyCount`
  **gated by the EOS-ordering guard** (`:18252`/`:18162` — a terminal completion whose result bytes have
  not finished sending reports `..._WAIT_PAYLOAD_EOS` instead of arming). Execute like the completion drain
  guts of `HomerServiceDpuPublishOneSelectedCompletionEvent` (`:40405`) but sending: resolve the peer by
  `sessionUID` (§3.4), reuse the send structure of `TupleSinkServicePublishPeerClientCommandCompletion`
  (`:18485`) retargeted to node A's completion landing ring.
- P2.3 Node-A ingress: recv doorbell marks the landed completion; a scheduler pass DMAs it to role 6
  (reuse the role-6 push guts of `HomerServiceDpuPublishOneSelectedCompletionEvent` `:40405`).
- **Checkpoint P2:** the client observes a role-6 completion for its command.

**Phase P3 — results + client + gate.**
- P3.1 Results — **REAL WORK ITEM** (corrected July 10, 2026; was "little/no new code" until the §2
  discovery correction). P0 leaves discovery armed but sink-less; P3.1 supplies the join:
  - Create the node-B service-side result **stream entry** for a spawned arena backend
    (`HomerServiceCreatePayloadStreamEntry`, `tuple_sink_service_process.c:24305` — allocates
    `serviceStreamId`); decide the creation point (peer OPEN handling vs. first START_COMMAND — the
    peer-open path already reaches it at `:26540/:26570` for the receiver side; trace which side needs
    what before coding).
  - Bind the **mirror-pool slot** with `(serviceSessionId, serviceStreamId)` (`:17241`).
  - **Stamp the ring's `boundServiceSinkId`** via P0's `HomerDpuDmaBindArenaResultSink` (idempotent;
    also completes any sink-gate-DEFERRED ready enqueue — a one-shot producer that published before the
    stamp must not stall, since acceptance consumes each publication epoch exactly once).
  - Pass the sink/stream id to the backend (spawn builder sets session identity but not
    `resultServiceSinkId` today, `:19322`) so `RemoteExecPrepareResultQueueGeneration` and the completion's
    `resultQueueDescriptor` carry real ids.
  - **Backend sink-handle open over the arena mapping** (discovered during P0 implementation):
    `OpenCitusTupleSink` maps its ring BY shm name from offset 0
    (`CitusTupleSinkQueueDescriptor` has no offset field), but the arena ring is embedded at an offset
    inside the arena object — so the lazy open at `remote_execution_backend_bridge.c` (the
    `OpenCitusTupleSink` call in the result-receiver setup) needs an **adopt-mapping variant** that
    binds the sink handle to the ALREADY-mapped `resultQueueControlMappingAddress` instead of
    shm_open-by-name. (A descriptor offset field was rejected: it would bump the tuple-sink descriptor
    ABI for one caller.) Immediately after that open, call `CitusTupleSinkAttachDpuPublishMirror`
    (P0's API) with the arena publish line `hostPublishLines[slot*3+2]`, the arena
    `bridgeHeader.bridgeGeneration`, and global ring index/id — that arms the producer-side discovery
    stamps at the three commit sites.
  - Then verify node-B role-5 egress → node-A role-7 rides the existing (role-4-proven) pump + deform
    relay (`HomerServicePumpOutgoingDpuMirrorByteRingPayload` / `...TupleViewDpuTwoRingRelay`).
- P3.2 **S4.2 client side**: remove the `session->control == NULL` START rejection
  (`homer_client.c:5944`); drive START into role 1; consume role-6 completions (replace
  `HomerClientWaitCommandCompletion`'s shm mailbox) and role-7 result tuples.
- **GATE:** cross-node `pgbench` selected-DPU SELECT returns correct decoded values.

**Phase P4 — S4.0 (D6 refactor), LAST.**
- Cash in D6 for consumers A/B (resolve role-2/role-3 refs once at selected-session creation; add
  `HomerDpuDmaSubmitBackendCompletionPullForRef`; iterate sessions for role-3; demote D5's `continue` to a
  one-shot diagnostic). Note P1.2 already refactors consumer A's core, so fold/sequence D6 onto it. Re-validate
  with the same cross-node SELECT gate.

## 5. Risks / watch-items
- **EOS ordering (P2.2):** never publish terminal completion before the last result byte lands on node A.
  The existing guard (`:18252`) couples completion egress to the result send-completion frontier — preserve it.
- **Credit/backpressure:** each landing ring needs reverse consumed-head credit or a slow consumer stalls
  the sender's QP. Reuse `HomerServicePostReceiverHeadAck` shape.
- **Generation/teardown:** landing-ring descriptors and the egress arming must invalidate on peer reset /
  session close (tie into `ResetSession` + the S3.4 doorbell teardown).
- **`-Wswitch` gives no cover** for the new collectors (default arms everywhere) — hand-enumerate the four
  edit sites (AGENTS.md) and the `FindCollector`/`AppendCollectorAction` naming in the plan body.
- **Two-DPU redeploy:** any bridge/ABI touch needs BOTH DPUs rebuilt (bridge protocol version) or setup
  fails `BAD_PROTOCOL`.

## 6. Validation net
Cross-node DOCA pgbench selected-DPU SELECT (farnet0 client → farnet1 backend, both DPUs). The single-node
SQL-UDF baseline (green at HEAD, citus `89820eb36`) remains a cheap engine-level smoke but is NOT the gate.
Per phase, the checkpoints above are the intermediate signals; the final gate is a decoded SELECT value.
