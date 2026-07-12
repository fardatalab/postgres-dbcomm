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
>
> **✅ P0 VALIDATED (July 10, 2026, three runs, full four-role deployment, v4 on both DPUs).**
> Run 1: TCP transport smoke ok both ends; v4 arena import accepted (48+1 rebased descriptors); S4.1
> client 2-ring import accepted (`descriptor_count=2`, farnet0 DPU); 4-role basebackup (23.2 GB) proved
> the setup listener survives the new 16-enrolled-rings state. Run 2: proved there is NO silent
> host-service fallback, and found S4.2's second client dependency (thread-level local control open).
> Run 3 (corrected topology: peer=farnet1 DPU 10.10.1.201, farnet0 host service up for control hosting
> only, farnet1 host service DOWN): farnet0-DPU→farnet1-DPU peer transport confirmed
> (`established persistent outgoing RDMA peer transport host=10.10.1.201`), `DPU backend spawn begin` →
> `spawn COMPLETED … launched_pid=884091`, live arena backend pid-cross-checked with zero FATALs — the
> S4.0b arena result-ring wiring arm survives real startup — and the sink-gate diagnostic never fired.
> The client died exactly at the predicted S4.2 boundary (`client_sql_session_warmup_begin: invalid
> arguments`, the selected-DPU `control==NULL` gap), AFTER opening the session.
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

> **⏺ P1 RE-GROUNDED (July 10, 2026, codex trace of the real call paths) — three corrections that
> SIMPLIFY the plan below; the P1.x items are restated to match. Key evidence anchors:**
> - **The landing ring already exists — do NOT invent one.** The `OPEN_COMMAND_SESSION` response already
>   advertises a command mailbox: node B registers a `CitusRemoteExecLocalCommandMailbox`
>   (`tuple_sink_service_process.c:38410`) and returns it as `peerCommandMailboxDescriptor` (`:38431`);
>   node A saves descriptor + doorbell token (`:36026`/`:36041`). In the DPU service that mailbox lives in
>   DPU RAM — it IS the command landing ring. The immediate encoding already has `CLIENT_COMMAND` (2-bit
>   kind space is FULL — a fifth kind is impossible; reuse `CLIENT_COMMAND`,
>   builder `remote_execution_peer_transport_rdma.c:10116`), and the old host-path pump already posts
>   compact `CitusRemoteExecLocalCommandRecord` bytes + `readySeq` into exactly this mailbox
>   (`:20719`/`:20737`, fixed-slot variant `:20767`). **No handshake change, no peer-protocol bump for
>   P1** (v19 stays; the request/response structs have no room anyway — appending fields + a bump is only
>   needed if a future phase truly needs new descriptors).
> - **The wire unit is the materialized record, not the raw START.** No compact command struct exists;
>   "compact" = a valid prefix of `CitusRemoteExecLocalCommandRecord`
>   (`remote_execution_backend_protocol.h:299`; finalize `:17856`; RDMA bytes `:17928`). Node A already
>   owns the materializer (`HomerServiceDpuMaterializeBackendCommandRecord` `:39962`). The record carries
>   NO serviceSessionId — the remote mailbox is per-session, so the node-B session id is implied by the
>   TARGET mailbox (node A must use `peerCommandServiceSessionId`-scoped state, saved at `:36013`).
> - **Routing lookup + wedge.** The staged START carries the node-A `serviceSessionId`; the egress lookup
>   is `TupleSinkServiceFindSessionById` (`:22648`) → a `clientSqlRemoteSender` session holding the peer
>   connection, remote descriptors, doorbell token, and the registered command scratch region. (The §3.4
>   `sessionUID` plan is unnecessary for this direction — there is no sessionUID session index, and none
>   is needed.) TODAY a remote-session START **wedges** consumer A: `StageOneBackendCommandForPublish`
>   fails the local role-2 lookup (`:40148`) and never releases the engine-staged command — P1's fork
>   replaces this failure path. Also: the local stager manufactures a frontend START ack tied to the
>   role-1 slot (`:40163`) — for remote sessions node A must NOT manufacture it; STARTED flows back
>   end-to-end via P2.
> - **One FIFO, one drainer.** Engine staged commands are a strict FIFO; two collectors cannot drain it
>   safely. The FORK therefore lives INSIDE the single stage drainer (peek head → resolve session → local
>   publish OR hand to egress); the egress SEND remains its own scheduler-granted collector
>   (`DPU_PEER_COMMAND_EGRESS`, armed off `peerCommandEgressDepth`), fed by a small egress queue the
>   stage fills. §3.1's shape survives with the fork one level deeper.
> - **Node B has no selected-session at peer OPEN** (`FindOrCreateSelectedSession`'s only caller is the
>   local pulled-START path `:40117`; peer OPEN creates only the regular receiver session `:38361`; spawn
>   completion marks it backend-active `:42885`). The landing consumer must create/sync the node-B
>   selected-session and fill a publish slot DIRECTLY (record + role-2 ref + sequence — the publish-slot
>   shape at `:614`, fill `:40206`), NOT reuse the role-1-coupled stager verbatim.

- P1.1 **Node-B landing consumer** (was "add the landing ring" — the ring exists): a new collector that
  drains peer-receiver sessions' local command mailboxes (readySeq protocol, as the backend consumes at
  `remote_execution_backend_bridge.c:2856`), demand-armed by sessions with `clientSqlPeerReceiver` + a
  spawned arena backend + an unconsumed `readySeq`. Per record: find-or-create the node-B selected
  session, resolve role-2 via the bound arena refs, fill a `HomerServiceDpuBackendCommandPublishSlot`,
  install in-flight sequence/kind/flags (mirror `:40199`), advance `consumedEpoch`. The existing
  `DPU_BACKEND_COMMAND_PUBLISH` then DMA-publishes role 2 unchanged. The recv-side `CLIENT_COMMAND` WIMM
  currently only validates the token (`remote_execution_peer_transport_rdma.c:6725`, mailbox state is
  scheduler-discovered per `:1058`) — readiness may stay poll-based first; doorbell-driven arming is an
  optimization.
- P1.2 **Node-A fork in the stage drainer**: in `HomerServiceDpuStageOneBackendCommandForPublish`, after
  `FindSessionById`, route a `clientSqlRemoteSender` session's START to the egress queue (materialize via
  the existing materializer, enqueue, release the engine stage, bump `peerCommandEgressDepth`) instead of
  the local role-2 lookup; keep the local path byte-identical for local sessions. This kills the wedge.
- P1.3 **`DPU_PEER_COMMAND_EGRESS`** (node A): scheduler collector (4+1 edits) armed off
  `peerCommandEgressDepth`; action drains the egress queue up to `maxItems`: respect remote mailbox slot
  accounting, reserve/post via the send core REFACTORED OUT of `TupleSinkServicePumpRemoteClientSqlCommands`
  (record+readySeq posts `:20719`/`:20737` + `CLIENT_COMMAND` doorbell), shared by both callers.
- **Checkpoint P1:** a cross-node START reaches node B's landing consumer and DMA-publishes role 2; the
  arena backend consumes and RUNS it (observable: backend's consumedEpoch advances / command executes).
  Completion/result not yet returned (P2/P3).

  > **⏺ P1 IMPLEMENTED (citus `df2f81979`, July 10, 2026)** — codex-worker build, reviewed with four
  > findings fixed: (A) the CLIENT_COMMAND doorbell immediate is opt-in and OFF for both send-core
  > callers (unconditional WIMM would have changed the validated legacy pump's recv-WQE accounting for
  > zero benefit — both receivers are poll-armed); (B) dead-session egress entries are dropped, not
  > retried forever; (C) verified with evidence that DPU-opened sessions DO allocate the command
  > mailbox + scratch registration the egress stages through (session create ensures mailboxes
  > `:23156`, assignment `:19370`, scratch registration `:36384`, all before remote-sender marking);
  > (D) the START request carries no client sequence — authority is node-A-service-assigned,
  > documented at the fork.
  >
  > **⏱ P1 CHECKPOINT RUN (July 10, 2026, citus `1ec4a2db1` + slice): the bridge WORKS end-to-end —
  > p1 PASS (farnet0 DPU: "routed selected-DPU command to peer egress session=1 sequence=1" +
  > "posted peer command ... remote_epoch=1", targeting 10.10.1.201), p2 PASS (farnet1 DPU:
  > "landed peer command session=1 sequence=1" with code-confirmed publish-slot staging), p4 PASS
  > (client past the old warmup boundary; now fails at completion PEEK — the expected P2/P3 edge).
  > ONE REGRESSION (p3 FAIL): the first-ever arena role-3 completion pull hit "backend completion
  > control snapshot protocol mismatch" → sticky engine fatalError. Diagnosis: the arena slot's
  > completionMailbox CONTROL HEADER is never initialized (agent slot init sets only
  > resultRingControl; Tier-2 lifecycle mailboxes got headers from their creator; the arena
  > completion mailbox was never pull-read before P1) — an S3.2c-era latent gap. Fix in flight:
  > initialize arena mailbox headers at slot init + recycle, replicating the legacy creator
  > field-for-field. NOTE ALSO: p3's error firing does NOT prove the backend executed BEGIN (the
  > control-header read fails regardless); backend execution proof moves to the re-run.**
  >
  > **⚠ RE-RUN (citus `ce31e63a1`): the header-init fix did NOT resolve it** — same mismatch with a
  > VERIFIED-fresh arena (removed from /dev/shm, recreation logged) and VERIFIED-fresh citus.so
  > (maps inode + md5). r2/r4/r5 unchanged-good. Layouts confirmed identical
  > (`CitusRemoteExecLocalCompletionMailbox` header == `HomerDpuDmaBackendCompletionControlSnapshot`,
  > both {u32 proto, u32 reserved, u64 published, u64 consumed}); control-read source =
  > `hostRingAddress + hostControlOffset` = the mailbox header; validator is a plain proto==15 check.
  > (The validator's `slot=16` lead is a red herring — AGENTS.md's stale-service example
  > `handle=2 slot=17 session=2` implies slot=16 is the normal FIRST-spawn value.) Next: diagnostic
  > `f5ad8a39a` dumps ring identity + raw snapshot words at the failure; a focused farnet1-DPU-only
  > redeploy run is collecting the evidence. Candidate space now: wrong-ring read via the D6 scan,
  > a torn/short DMA read, a ref/generation mismatch steering the read, or the snapshot buffer not
  > being the task's actual destination.
  >
  > **🔴 DIAGNOSTIC RESULT (run 4, citus `f5ad8a39a`) — DECISIVE, and STOPPED here for discussion:**
  > `diag ring=1 role=3 gen=4128789413882223(=current) bound_session=1 host_ring=0x7f86d83d6700
  > ctrl_off=0 ctrl_bytes=24 snapshot{proto=0 reserved=0 published=0 consumed=0}` — the DPU read the
  > RIGHT ring (ring 1 = slot 0's completion mailbox), right generation, right bound session, right
  > offset — and got ALL ZEROS from memory the host verifiably stamped proto=15 (fresh arena, fresh
  > fixed citus.so, both verified). **The DMA read returns different bytes than the postmaster's
  > mapping holds.**
  >
  > **Leading theory — arena EXPORT-1 DMA has NEVER been verified end-to-end.** Inventory of all DMA
  > traffic through the arena mmap (export id 1): every READ before this one expected zeros (role-5
  > publish lines pre-publication, grouped-control of unenrolled rings = none), and the one WRITE
  > (P1's role-2 command publish) had no confirmation the backend ever saw it (the backend idles;
  > execution was never proven — the p2 'landed' line proves publish STAGING, not arrival). The spawn
  > region (export id 2) round-trips BOTH directions verifiably (DPU writes the spawn request, backend
  > reads it; backend writes state, DPU state-reads see COMPLETED). So a broken export-1 window
  > (reads return zeros / writes vanish) is consistent with EVERY observation across all four runs —
  > and with the AGENTS.md lesson "an import being accepted proves nothing." This is the same class as
  > the S0/S0b spikes: the export mechanics (posix-shm MAP_SHARED, fork-reader) were proven for a
  > DEDICATED single-export region; the arena's TWO-EXPORT setup message (arena 57 MB + spawn region)
  > has never had an arena-side DMA round-trip proof.
  >
  > **Next probes (cheapest-decisive first):**
  > 1. HOST-side: hexdump /dev/shm/citus_homer_frontend_arena_v2 at offsetof(slots[0].completionMailbox)
  >    during a live run → proves proto=15 is really in tmpfs (final elimination of host-side stamping).
  > 2. DPU-side: one-shot diagnostic DMA read of the arena ADVISORY HEADER (offset 0 of export 1:
  >    protocolVersion + slotCount=16, known nonzero) at import-accept time, and of a spawn-region word
  >    (known-good export 2) → if the header reads zero, the whole export-1 window is broken
  >    (import/translation bug in the two-export path: mmapExportId→doca_mmap selection, base-VA
  >    translation, or export-descriptor handling); if it reads correctly, the fault is
  >    offset/descriptor-specific to component-addressed slot rings.
  > 3. Check the TCP transport smoke's two-export coverage: --export-posix-shm exercises ONE export;
  >    whether any smoke imports TWO mmap exports in one setup message and DMA-round-trips the SECOND
  >    is exactly the regression-net gap this would have slipped through.
  >
  > **Checkpoint sequencing correction:** the checkpoint cannot run as phased — NO driver can submit a
  > cross-node START until S4.2's submission half exists (the client dies at warmup before writing
  > role 1; the SQL-UDF driver is single-node → local fork arm only). Resolution: pull the MINIMAL
  > S4.2 slice forward into P1 validation — client submits START through the same DPU control slot it
  > already uses for OPEN; the completion WAIT stays unimplemented (client timeout after submission is
  > the expected P1 boundary). Design nuance to settle while building the slice: what the LOCAL path's
  > control-slot START response actually asserts (`:40163` region) — if it is merely "accepted/queued,
  > sequence=N" (not STARTED), the remote fork SHOULD manufacture the same accepted-response so the
  > client unblocks and learns the node-A-assigned sequence (resolving finding D's client-side half);
  > the role-6 STARTED/terminal completion remains strictly P2.
  >
  > **✅ P1 CLOSED — VALIDATED (July 11, 2026, run 10, citus `039f56b08`).** The completion-control
  > zero-read blocker is fixed. Root cause was NOT on the DPU/DMA side (every layer there was verified
  > across runs 4–8): it was a host-side race — `HomerFrontendAgentBindArenaSlot` re-ran
  > `HomerFrontendAgentInitArenaSlot` (memset→restamp) on the completion mailbox that the DPU was
  > already polling at ~100% read duty, so one DMA sampled the transient zero window and fatally failed
  > proto validation. Fix: bind now VERIFIES the FREE-implies-initialized invariant instead of
  > rewriting it (`b29a90330`), relaxed to accept the DPU-pre-published role-2 command
  > (`cmd.publishedEpoch <= slotCount`, `039f56b08`). Run 10 evidence: 22 bounded control reads (not
  > 300k+ spinning), zero mismatch/fatal strings, `remote exec backend` alive post-workload, engine
  > advanced to the benign `missing frontend completion event descriptor` (session=1) — i.e. the
  > backend consumed and RAN the command, and the chain reached the P2 seam (completion event has
  > nowhere to go until P2 builds role-6 delivery). Full arc + the three latent bugs uncovered
  > (unstamped headers → bind memset zero-window → bind memset destroying pre-published commands) are
  > in §7. Deferred to S3.4/S4 teardown: (a) a "backend died pre-publish" give-up path (run 9 showed
  > the DPU spins forever otherwise); (b) `ReleaseArenaSlot`/reaper memset vs. a live DPU poller needs
  > DPU-unbind-before-release ordering; (c) crossprobe + `[ctl-read-diag]` scaffolding removal.

**Phase P2 — completion bridge (node-B role-3 → node-A role-6).**
- P2.1 ~~Add~~ **The completion landing ring likewise already exists** (same July-10 re-grounding as P1):
  the OPEN request already carries node A's completion-mailbox descriptor + doorbell token
  (`localClientCompletionMailboxDescriptor`, built `:36237`, saved on node B `:38387`), and the existing
  sealed-completion publish (`TupleSinkServicePublishPeerClientCommandCompletion` `:18168`) RDMA-writes
  into it with the `CLIENT_COMPLETION` immediate. In the DPU service that mailbox is node-A DPU RAM — the
  landing ring. P2's work is the node-A DRAIN of that mailbox into the role-6 push (plus the node-B
  egress fed from consumer B's events), not a new ring. Ground the details (who allocates the node-A DPU
  session's completion mailbox; the drain's readiness) with a P2-opening trace before coding.
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

  > **⏺ P2 GROUNDED + DESIGN RESOLVED (July 11, 2026, two codex-explore traces).** All the ring/mailbox
  > machinery already exists; P2 is wiring + one recv-handler branch + a completion-drain fork. Resolved
  > decisions (each replaces a plan sketch with the confirmed reality):
  >
  > - **Correlation = the LOCAL `serviceSessionId`, NOT a new `sessionUID` and NOT the §3.4 sessionUID
  >   INDEX.** `sessionUID` already EXISTS (`tuple_sink_service_process.c:1399`) and is the cross-node
  >   result-ring (role-7) identity threaded through result-SEND peer-opens — reserved for P3, unused by
  >   P2. Both selected-session creators copy the local `serviceSessionId` (node-A START `:40443`, node-B
  >   landed command `:40649`); `HomerServiceDpuFindSelectedSession(serviceSessionId)` (`:39874`) and
  >   `TupleSinkServiceFindSessionById(serviceSessionId)` (`:22841`) both match on it. So the selected
  >   session ↔ service session link is already there on each node; P2 adds NO identity plumbing.
  > - **P2.1 allocation: NO new work.** Node A's completion landing mailbox is allocated automatically at
  >   session create (`TupleSinkServiceEnsureClientCompletionMailbox` `:19204`, called from
  >   `TupleSinkServiceCreateSession` `:23149`, reached by the DPU async OPEN path
  >   `:38975`→`:37687`→`:36324`) and RDMA-registered during OPEN (`:36419`), with node B saving the
  >   descriptor + doorbell token (`:38618`). It is a service-owned POSIX-shm mailbox — distinct from the
  >   arena role-6 ring; P2.3 still must DMA from it into role 6.
  > - **EOS guard is DORMANT in P2.** The arena backend leaves `resultServiceSinkId==0` until P3
  >   (`remote_execution_backend_bridge.c:2846`), so no terminal completion carries `TUPLE_SINK_EOS`
  >   (`:1815`), so `HomerServicePayloadStreamResultEosPosted` (`:18353`) returns ready immediately. P2.2
  >   sources the EOS check from the QUEUED completion's own `resultFlags`/`resultServiceSinkId` (the slot
  >   retains the full body) rather than the service session's `currentCommand*` fields (which node B's
  >   role-3 accept path never populates) — correct-by-construction, becomes live only when P3 binds a
  >   sink.
  > - **Node-A ingress (P2.3): doorbell kind space is FULL** (2-bit, all 4 used —
  >   `remote_execution_peer_transport_rdma.c:79`). Keep `CLIENT_COMPLETION`; BRANCH the recv handler
  >   `TupleSinkServiceHandlePeerClientCompletionDoorbell` (`:28287`) on
  >   `HomerServiceDpuFindSelectedSession(serviceSessionId)`: FOUND ⇒ enqueue a
  >   `HomerServiceDpuCompletionEventSlot` for the existing `DPU_COMPLETION_PUSH` collector (`:41677`,
  >   action `:44199`), NOT-FOUND ⇒ the legacy CPU-publish into `clientCompletionMailbox` (`:28398`). Per
  >   the July-11 owner steer (host-service legacy is being retired right after this plan), the legacy arm
  >   stays a thin, clearly-marked short-lived branch — delete it with the host-service teardown, do not
  >   extend it.
  > - **Node-B egress (P2.2): FORK the completion drain, don't replace it.** Today `DPU_COMPLETION_PUSH`
  >   unconditionally drains queued completions via `HomerServiceDpuPublishOneSelectedCompletionEvent`
  >   (`:40405`), which on node B fails "missing frontend completion event descriptor" because the remote
  >   client's role-6 ring is not local. Fork on the selected session's service session (looked up by
  >   `serviceSessionId`): if `clientSqlPeerReceiver`/`dpuPeerCommandLandingActive` (peer-landed, remote
  >   client) ⇒ arm the NEW `DPU_PEER_COMPLETION_EGRESS`; else (local client) ⇒ the existing role-6 push,
  >   unchanged. Implement as TWO ready-counts (local-destined vs peer-destined) so each collector is armed
  >   by an EXACT count (AGENTS.md collector-arming rule), not one shared count both actions race. The
  >   egress action reuses `TupleSinkServicePublishPeerClientCommandCompletion`'s send body (`:18485`)
  >   retargeted to node B's saved `clientSqlPeerCompletionMailboxDescriptor` + token, fed the queued
  >   completion body instead of reconstructing from `currentCommand*`.
  > - **`DPU_PEER_COMPLETION_EGRESS` wiring — the 5 core + 4 operational sites** (mirror
  >   `DPU_PEER_COMMAND_EGRESS`): collector enum `:2504`, action enum `:2553`, collector→source
  >   `:10209`, collector→action+maxItems `:10436`, phase-body naming `:11366`; PLUS registry init
  >   `:15720`, feedback map `:39255` (→`peerSendCqFeedback`), candidate arming `:41593`, and the DPU
  >   action switch executor `:43654`. (AGENTS.md: a collector armed-but-never-named-in-phase-body is armed
  >   every pass and granted never — the S3.3 silent-wedge bug. Enumerate by hand.)
  >
  > **CLIENT-SIDE scope (the second, legacy-retiring half).** `HomerClientPeekNextCompletionEvent`
  > (`homer_client.c:6273`) reads the legacy host-service mailboxes (`backendCompletionMailbox` /
  > `completionMailbox` `readyEpochSlots`) and hard-requires `session->control != NULL` (`:6285`) — so a
  > selected-DPU session (which sets `control == NULL`) fails the peek immediately (that IS run-10's
  > "invalid arguments while peeking Homer completion").
  >
  > **CORRECTION (July 11, 2026, re-verified):** an earlier draft here said role 6 is exported "only on
  > the basebackup stream, never on the pgbench SQL session." That was WRONG. `HomerClientOpenSqlSessionSelectedDpu`
  > (`homer_client.c:2908`) ALREADY exports the role-6 completion-event line on the SQL session's
  > `commandDpuStream` (`dpuFrontendCompletionEvent` `:3049`, `HOMER_CLIENT_DPU_COMPLETION_EVENT_RING_INDEX`),
  > and node A's DPU `HomerServiceDpuPublishOneSelectedCompletionEvent` DMA-writes a
  > `HomerDpuBridgeFrontendCompletionEvent` into it. So the client slice does NOT need a new export — it is
  > only a PEEK REDIRECT: mirror the START path's already-shipped selected-DPU branch (`:6013-6054`, which
  > relaxes the guard to `(session->control == NULL && !session->commandDpuStreamOpen)` and drives
  > `session->commandDpuStream`) inside `HomerClientPeekNextCompletionEvent`/`WaitCommandCompletion`: when
  > `session->commandDpuStreamOpen`, read/parse `commandDpuStream.dpuFrontendCompletionEvent` (role 6)
  > instead of requiring `control` and reading the legacy mailbox. This is the completion half of S4.2 and
  > the first concrete host-service retirement — MODERATE, pattern already established, no design fork.
  >
  > **SEQUENCING:** backend bridge FIRST (P2.2+P2.3 — legacy-free), then the client peek-redirect
  > (BLOCKED on the backend-bridge validation — no point consuming role 6 until the DPU proves it writes
  > it). Symmetric to P1 pulling the minimal S4.2 submission slice forward. The backend-bridge checkpoint
  > has TWO possible shapes, and the validation distinguishes them (both PROVE egress+ingress):
  > - If run-10's client opened via `HomerClientOpenSqlSessionSelectedDpu` (its `control==NULL` peek
  >   failure strongly implies so), it ALREADY exported role 6 to node A's local DPU, so after egress node
  >   A's `HomerServiceDpuPublishOneSelectedCompletionEvent` FINDS the descriptor and the role-6 DMA
  >   SUCCEEDS — no "missing … descriptor" on EITHER node, the completion sits in the client's role-6 ring,
  >   and ONLY the client peek-redirect remains. (Most likely.)
  > - If node A has no role-6 descriptor, the "missing … descriptor" error MOVES from node B to node A —
  >   still proving the completion traversed the bridge, but the client export is also needed.
  > Node B ceasing to spin on "missing … descriptor" (it egresses instead) is the invariant signal in both.
  >
  > **⏱ BACKEND-BRIDGE VALIDATION (run 11, July 11, 2026, citus `2b7e0052d`) — FAIL, but it RE-SEQUENCES
  > the work: the client slice is a PREREQUISITE, not a follow-on.** Node B: `missing … descriptor` = 0
  > (good — the fork took effect, no more local push), `fatal error state` = 0, AND zero egress
  > log lines (success OR failure) — node B went SILENT right after `landed peer command session=1
  > sequence=1`. Node A: only the CLIENT's own `CLOSE_SESSION` + full farnet0 import teardown, on the
  > client's OWN bridge generation — within ~1 s of session open. ROOT CAUSE (agent-flagged, confirmed by
  > code): the pgbench client's warmup does START then immediately PEEKs the completion; the peek fails
  > synchronously at the `control==NULL` guard (`homer_client.c:6285`) → "invalid arguments while peeking
  > Homer completion" → the client tears its whole session down in ~1 s — BEFORE the backend produces a
  > completion / before egress can fire, and the teardown resets node B's peer session
  > (`dpuPeerCommandLandingActive`→false via ResetSession) so the completion path never runs. So the
  > backend bridge CANNOT be observed in isolation; the client peek-redirect must land FIRST so the client
  > stays alive (polling role 6) long enough for backend→pull→egress→ingress→role-6 to complete. (The zero
  > egress-failure lines + zero "missing descriptor" are consistent with "never exercised," not "wiring
  > gap" — the 9 wiring sites + phase-body naming were review-verified present. A stats/log build can
  > confirm arming later if the redirect run still stalls.) NOTE: deploy-proof literal is
  > `HOMER_PROGRESS_COLLECTOR_DPU_PEER_COMPLETION_EGRESS` (count 1/DPU); `dpu-peer-completion-egress` is a
  > runtime name string, absent from `strings`.
  >
  > **CLIENT PEEK-REDIRECT (in progress).** `HomerDpuBridgeFrontendCompletionEvent` (role 6) =
  > `{publishedEpoch (DPU-owned, off 0), serviceSessionId, commandSequence, backendCompletionEpoch,
  > commandCompletion}`. In `HomerClientPeekNextCompletionEvent` (+ Ack + the wait loop), when
  > `session->commandDpuStreamOpen`: relax the `control==NULL` and `both-mailboxes-NULL` guards (mirror
  > START `:6024`), load `dpuFrontendCompletionEvent->publishedEpoch` (acquire), lease `commandCompletion`
  > when `publishedEpoch >= lastConsumed+1`, ACK by advancing the local epoch (no mailbox writeback — role
  > 6 has no consumed word; synchronous `-M simple` serialization avoids single-slot overwrite, so no
  > flow-control line needed for the checkpoint).
  >
  > **⏱ FULL-P2 VALIDATION (run 12, July 11, 2026, citus `4216b1cb8`) — FAIL, STOPPED for discussion.**
  > The client fix WORKED partially: `invalid arguments while peeking Homer completion` is GONE (the client
  > now polls role 6). But the completion still never flows, and this rules out my run-11 teardown theory:
  > with the client staying ALIVE (busy-spinning in the wait loop, 98% CPU, 47 s+), node B's log STILL ends
  > at `landed peer command session=1 sequence=1` — no egress, no "missing descriptor" (0 on BOTH nodes),
  > no fatal (0 on both). role 6 is never written, so the client polls forever.
  >
  > **TWO bugs, both need attention:**
  > 1. **P2 completion never egresses (primary).** Static inspection is EXHAUSTED and contradictory: the
  >    pull-arming is unchanged (`HomerServiceDpuSelectedBackendCompletionWaitCount` counts `commandInFlight
  >    && completionEventCount==0`, armed at `:41846`); landing sets `commandInFlight=true` (`:40831`);
  >    accept→enqueue is unchanged from run 10 (where a completion WAS enqueued — it spun the local push on
  >    "missing descriptor" 8.75M×); and the fork's peer path (`IsPeerDestined = clientSqlPeerReceiver &&
  >    dpuPeerCommandLandingActive`, both sticky-true on node B) + the EOS-prefix count return ready for a
  >    warmup completion (terminal, no TUPLE_SINK_EOS ⇒ `EosPosted`=true). Yet a queued completion is in
  >    NEITHER the peer ready-count (→ egress) NOR the local ready-count (→ "missing descriptor"). Since
  >    those two are exhaustive over `IsPeerDestined`, a queued completion MUST land in one — so either it
  >    is not enqueued (pull/accept silently not happening — the production build logs neither), or a count
  >    has a runtime bug static reading misses, or the collector arms but never gets granted (source/
  >    scheduling). REQUIRES a diagnostic build: log pull/accept/enqueue, `completionEventCount`,
  >    `IsPeerDestined`, and both ready-counts per pass on node B. Candidate: a small compile-gated block in
  >    `HomerServiceAppendDpuDmaCollectorCandidates` + the accept path, or `-DHOMER_SERVICE_PROGRESS_STATS=1`.
  > 2. **Client selected-DPU wait timeout never fires (secondary).** `HomerClientWaitCommandCompletion`'s
  >    `HomerClientMonotonicMillis() >= selectedDpuDeadline` (10 s) did not trigger at 47 s — a real bug in
  >    that comparison/reset (so a hung client spins forever instead of erroring cleanly). Fix regardless.
  >
  > Cheapest thing to check FIRST: whether the backend even PUBLISHES a terminal completion to role 3 for
  > `client_sql_session_warmup_begin` (the `remote exec backend` was 100% CPU in run 11 — it could be stuck
  > pre-publish, so the pull waits on a completion that is never produced; that would explain "in neither
  > count" trivially — nothing is enqueued). If it does publish, instrument the enqueue→classify→count chain.
  >
  > **⏺ CODEX ANALYSIS (July 11, 2026) — REFRAMES the bug: it is UPSTREAM of the fork, in the
  > publish→pull chain, and my "in neither count" was the wrong framing.** Findings, each file:line-backed:
  > - **`warmup_begin` is a normal command, NOT special.** It is `CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_TX_BEGIN`
  >   with `SQL_RESULT_NONE` (`pgbench.c:9662`); the backend runs it, creates NO result sink
  >   (`remote_execution_backend_bridge.c:2163`), and publishes a normal terminal COMPLETED with
  >   `resultFlags=NONE, resultServiceSinkId=0` (`:3018`, `:1804`). So the client correctly waits on role 6,
  >   and P2 alone (no P3) should deliver it. Hypothesis #4 RULED OUT.
  > - **EOS blocking RULED OUT for warmup.** `HomerServicePayloadStreamResultEosPosted` returns true for a
  >   terminal record lacking TUPLE_SINK_EOS (`:18375`); warmup has no EOS bit. So IF enqueued+peer-classified,
  >   the peer count MUST be nonzero. Hypothesis #2 RULED OUT for this command.
  > - **Fork/egress collector wiring is COMPLETE and NOT starved.** Source/action/phase-body/executor all
  >   present; feedback backoff is disabled by `knownExpectedWork=true` (`:10067`, armed true at `:41912`).
  >   The only residual scheduler risk is the 6-grant/phase collector budget (`:170`) dropping a late
  >   collector on a pass, but that cannot PERMANENTLY starve (47 s), and completion-egress is early in the
  >   order. Hypothesis #3 WEAK.
  > - **Pull/accept/enqueue NOT miswired by P2** (confirmed vs `039f56b08`): `selectedBackendCompletionWaitCount`
  >   (`:40110`), the 3-way pull arming (`:41837`), `HomerServiceDpuAcceptOneBackendCompletion` (`:40950`), and
  >   the pull action's submit+accept loop (`:44045`) are unchanged.
  > - **KEY INSIGHT: `"landed peer command"` marks STAGING, not role-2 publication.** At that log point
  >   `HomerServiceDpuLandOnePeerCommand` has only filled `backendCommandPublishSlots[]` + set `commandInFlight`
  >   (`:40901`); the role-2 DMA that actually delivers the command to the backend's mailbox is a SEPARATE
  >   later step, `DPU_BACKEND_COMMAND_PUBLISH` (armed `:41803`). So run 12's silence-after-"landed" is
  >   consistent with the chain stalling BEFORE the backend ever sees the command. The 4 candidate break
  >   points, in order: (1) `DPU_BACKEND_COMMAND_PUBLISH` never granted; (2) role-2 DMA submitted but not
  >   completed; (3) backend never published role 3; (4) `DPU_BACKEND_COMPLETION_PULL` never granted/accepted.
  >   Static diff cannot distinguish — needs a diagnostic build logging, per landed in-flight session:
  >   `backendCommandPublishCount`, publish queue depth/in-flight, `selectedBackendCompletionWaitCount`,
  >   backend completion ready/staged/in-flight, plus one-shot grant counters at the publish action (`:44121`),
  >   the pull action (`:44045`), and accept (`:40950`). First missing transition = the break.
  > - **SECONDARY bug ROOT-CAUSED: the client timeout is in the WRONG function.** pgbench warmup uses
  >   `HomerRunCommandAndWait`'s unbounded `while (homer_command_pending)` loop over `receiveHomerCommand` →
  >   `HomerClientPeekNextCompletionEvent` (`pgbench.c:4303`, `:4200`) — it NEVER calls
  >   `HomerClientWaitCommandCompletion`, so the worker's 10 s selected-DPU deadline there is correct but
  >   never reached. The bounded deadline belongs in `HomerRunCommandAndWait`/`receiveHomerCommand`.
  >
  > **OPEN PUZZLE for the diagnostic to resolve:** at `039f56b08` (run 10) this SAME warmup made node B
  > enqueue a completion (local push spun 8.75 M×) — so back then the command WAS published, the backend ran
  > it, and a completion was enqueued. At `4216b1cb8` node B apparently stalls before that. The publish/pull
  > code is unchanged, so the difference is either a subtle side effect of the P2 candidate-builder change or
  > the client now STAYING ALIVE (run 10 tore down in ~1 s; could a CLOSE_SESSION teardown have been what
  > flushed the pending publish?). The diagnostic's first-missing-transition will tell.

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
  **Plus a second dependency found by P0 validation run 2 (July 10, 2026):** `pgbench --homer`
  unconditionally opens the LOCAL host service's control shm region per thread
  (`pgbench.c:9142` → `HomerClientOpenControl` → `shm_open(CITUS_REMOTE_EXEC_CONTROL_SHM_NAME)`,
  `homer_client.c:402`) BEFORE any DPU logic — so a pure-DPU client currently still requires a local
  host `citus_tuple_sink_service` just to boot. S4.2 must make that open conditional (skip for the
  pure selected-DPU path) or the "no host services" cross-node topology can never run. Until then,
  cross-node validations run with the CLIENT-side host service up (control-region hosting only) and
  the BACKEND-side host service down (loud-failure guard).
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

**⚠ `--homer-peer-host` SELECTS THE RELAY TOPOLOGY, and the wrong value silently validates the wrong
path (cost one P0 validation cycle, July 10, 2026).** With `10.10.1.101` (farnet1 HOST service — the
value in the standing AGENTS.md pgbench recipe), the farnet0 DPU relays the OPEN to the farnet1 HOST
service, which spawns a backend via the deprecated host-service arm — everything "passes" while the
farnet1 DPU, the arena spawn, and the arena backend arm are never touched, and the only tell is the
ABSENCE of `DPU backend spawn begin/COMPLETED` on the farnet1 DPU. The DPU-relay command chain needs
`--homer-peer-host 10.10.1.201` (the farnet1 **DPU**) and the DPU services started WITH peer-bind env
(`HOMER_SERVICE_PEER_BIND_HOST=10.10.1.201/10.10.1.200`, `HOMER_SERVICE_PEER_PORT=9717`) on top of the
DMA env. For cross-node validations of THIS design, also leave the two HOST services DOWN so a silent
host-relay fallback fails loudly instead of masquerading as a pass.

> **⚠ "Both host services down" SUPERSEDED IN PART by the P3.2 client-dependency note (§4) — cost
> run 17 a cycle (July 11, 2026).** It is only achievable after S4.2 makes the client's
> control-region open conditional; until then `pgbench --homer` cannot BOOT without a local control
> shm (`HomerClientOpenControl` per thread, before any DPU logic). Standing arrangement: CLIENT-side
> host service UP (control-region hosting only — verify its log shows no session/command activity),
> BACKEND-side host service DOWN (the loud-failure guard that matters), positive DPU-relay proof from
> the lifecycle/p2diag lines on BOTH DPU logs. Runs 13–16 ran this way partly by ACCIDENT: a stale
> `/citus_remote_execution_control_v27` shm satisfied the client even with the farnet0 host service
> down, so a hard-clean baseline (which removes it) made the topology error visible where a dirty
> baseline had masked it.

See also: [Homer tuple DEFORM and the two-ring result relay](tuple_deform_two_ring_relay_overview.md).

## 7. P1 blocker investigation — Codex second opinion + arena poke probe (July 10, 2026)

**Codex verdicts (file:line-proven):** Theory A (interior-address translation bug) REFUTED — Homer passes
full exporter VAs straight to `doca_buf_inventory_buf_get_by_addr` (`homer_service_dpu_dma.c:11067`,
role-2 write `:10898`, spawn `:10136`, grouped `:10277`); the TCP smoke ALREADY exercises
interior-addressed role-2/3 DMA (`homer_dpu_tcp_transport_smoke.c:735/:758/:1537`). Corollary: rebasing
roles 2/3 to arena-base = numerically identical address = NOT a fix. Theory B's wrong-mmap-selection
REFUTED — export→mmap wiring validated at three layers (`homer_dpu_comch_abi.h:664/:694`,
`homer_service_dpu_setup_tcp.c:885`, `homer_service_dpu_dma.c:6911/:6997/:7036`, lookup by ids `:8327`).
Local completion-buffer aliasing also checked (by hand): pool sizing deliberately shares
`commandPullBufferCount`; submit and acceptance use the same slot-carried index; DMA completed → a
successful 24-B copy delivering zeros means the SOURCE reads zero in the DPU's view. Remaining suspect:
the 57-MiB arena mmap itself (size and/or two-export combination — no smoke covers real two-export DMA
or a large region; the only two-export check is parse-only `homer_dpu_comch_abi_check.c:111/:220`).

**Probe (zero Homer code changes):** a standalone host tool mmaps the LIVE arena shm, reads
`hostPublishOffset` + `bridgeGeneration` from the in-region advisory header, and hand-crafts a VALID
publish line into `hostPublishLines[2]` (ring 2 = slot 0's role-5 ring, grouped-control-ENROLLED since
S4.0b): body fields matching every acceptance check (identity vs descriptor, ACTIVE, flags subset,
monotonic), epoch release-stored LAST. Detector already exists in the DPU service: the S4.0b sink gate
prints "role-5 publication discovered before sink bind" on exactly this event.
- Line appears → arena interior reads WORK through this mmap → fault is specific to the
  completion-control task path → hunt there.
- Nothing appears (or identity/frontier errors) → arena reads broadly broken → registration/size issue →
  adopt the separate-small-exports workaround (Tier-2-proven shape) to unblock P1; file the large-mmap
  + smoke-gap investigation separately.

**PROBE RESULT (run 5): arena DMA reads WORK near base.** arena_poke stamped ring 2's publish line
(arena+256) from a third process; the DPU's grouped-control sweep discovered it byte-exact
("role-5 publication discovered before sink bind (session=0 ring=2 tail=64, occurrence=1)", zero
validation errors). So: two-export import, cross-process tmpfs coherence, and the arena DMA window are
all FINE — the broad-breakage theory is dead. Remaining confound: the working read differs from the
failing one on BOTH task type (grouped-control vs backend-completion-control) AND depth (+256 vs
~+800 KB, past slot 0's command mailbox). Note also the Tier-2 completion pull has not been re-run
since the pre-P0 green baseline, so a task-path regression is not formally excluded. FINAL SPLIT in
flight: a compile-gated engine cross-probe reading 24 B at ring 1's interior hostRingAddress once at
arena import — zeros = depth-dependent (DOCA/kernel; go separate-small-exports workaround), proto=15 =
task-path bug (small haystack).

**CROSS-PROBE VERDICT (run 6, citus `d1cfa413b`): the address is INNOCENT.** Raw synchronous DMA at the
exact failing address returned `proto=15` (`crossprobe completion-ring host=0x7f7df0e49700 bytes:
0000000f 0000...`), with the arena-base sanity read showing the advisory header correctly. Both interior
address computations agree. The bug is INSIDE the async backend-completion-control task machinery.
**New lead (from the cross-probe implementation itself):** the engine's 3,072 task slots alias its
1,024-buffer pools under MODULO mapping (the probe needed a special scratch-finder to avoid it). If any
async step derives a buffer from the task-slot index by modulo while another step uses the owner-recorded
index — or a concurrent submit memsets an aliased buffer between DMA completion and acceptance — the
acceptance reads a freshly-memset (all-zero) buffer while the real data sits elsewhere. Timing fits:
Tier-2 ran green with few in-flight tasks (indices in lockstep); S4.0b's enrolment of 16 role-5 rings
adds 16 grouped tasks per sweep, desynchronizing slot/buffer counters exactly when the symptom appeared
(the round-1 zeros were REAL — headers were genuinely unstamped pre-`ce31e63a1`; rounds 2/4 zeros are the
aliasing). Audit in flight: the async completion-control path's buffer lifecycle end-to-end.

**AUDIT (Codex, round 2): completion-path Homer bookkeeping CLEAN end-to-end** — index consistency
(submit :2332/:2350 → builder :11241/:11261, owner stores same index :11344, dst acquired against
localBackendCompletionMmap :11369), acceptance-BEFORE-release ordering (retire path :9890-:9904, buf
refs freed only at :10105), pools separately allocated (:12391-:12448). TWO takeaways:
(1) 🐛 REAL SEPARATE BUG filed: grouped-control derives its buffer as taskSlotIndex %
groupedControlBufferCount (:10547, init :12550) while 3,072 task slots exist (:12311) vs 1,024 buffers
(:1002) — live slots i / i+1024 / i+2048 collide. Fix with a groupedControlBufferInUse[] free list +
owner-carried index (the backend-completion model). Concurrency-sensitive corruption; NOT the cause of
the completion zeros (distinct allocations).
(2) LAST UNTESTED VARIABLE found by cross-referencing: every WORKING read (grouped sweeps + the
cross-probe, which borrows a grouped buffer) uses the GROUPED local mmap as DMA destination; the failing
path is the ONLY user of localBackendCompletionMmap. Probe extension in flight: same source, dst = a
completion-pool buffer via the exact same mmap/inventory/address math. Zeros → completion pool's local
mmap/registration is the culprit; proto=15 → DOCA inventory recycling confirmed, adopt the
persistent-per-buffer doca_buf fix (create once from localBackendCompletionMmap per staging buffer,
retain for engine lifetime, keep the inUse free list).

**FIXES LANDED — citus `a1f0d016c` (July 10, 2026).** Fix 1: persistent dst doca_bufs + explicit
zero-length `set_data` cursor reset before every completion CONTROL_READ submission (mechanism — DOCA
inventory recycling returning bufs with unreset append cursors — identified by the project owner;
SLOT_READ/CONSUMED_EPOCH follow after hardware validation, then the other five builders). Fix 2:
grouped-control free-list ownership replaces the modulo (which also fixed the SECOND latent grouped bug:
the late snapshot re-read via `lastControlBufferIndex` was itself alias-corruptible;
`CopyGroupedControlSnapshot` now returns the ring-owned `acceptedHostPublishLine`;
`lastControlBufferIndex` removed; exhaustion = loud deferred tripwire, unreachable by ~2 orders under
the per-ring in-flight rule + async windows). P1 checkpoint re-run in flight; pass = the benign
`missing frontend completion event descriptor` for session 1 (backend-executed proof, P2 seam). After
P1 closes: remove the crossprobe scaffolding, extend cursor hygiene to remaining builders (queued with
fix B before any S6 number).

**RE-RUN AFTER `a1f0d016c` (run 7): STILL FAILS, bit-for-bit** — same all-zero snapshot, unaffected by
the persistent-dst cursor fix. f2/f4/f5 unchanged-good (grouped free-list tripwire silent under real
churn — Fix 2 behaves). Subsequent line-by-line read of `HomerDpuDmaSubmitOneBackendCompletionTask`
verified CORRECT: src/dst addresses, per-kind mmap selection, (src,dst) task-init order, persistent-dst
wiring, geometry checks. **Every software layer is now verified; the DMA completes successfully yet
delivers zeros from memory a raw synchronous read shows as proto=15.** Determinism (identical across 4
runs, immune to 3 fixes) argues a systematic property of THIS path, not state decay. REMAINING
UNTESTED DELTAS vs. every working read: (1) the DMA CONTEXT — backend-completion tasks are plausibly the
only user of dmaContexts[COMPLETION class] in the cross-node flow (spawn/grouped/pulls ride other
classes); (2) the 24-byte copy size (working reads are 64 B); (3) the gated 4th crossprobe read
(completion-pool dst) was NEVER RUN — run 7 used the normal build. NEXT INSTRUMENTATION OPTIONS:
(A) run the gated crossprobe round (collects the pool-dst datapoint; extend it to also submit one read
on the COMPLETION-class context and one 24-B read — three discriminators in one run); (B) instrument
the failing task itself: print doca_buf_get_data()/data_len of src+dst immediately before submit and
after completion, plus a host-side hexdump of the mailbox header AFTER the failure. STOPPED for
discussion per the multi-shot rule.

**RUN-8 PREP (citus `4cf2505a2`, July 10, 2026): DMA-CONTEXT THEORY KILLED at the desk — user approved
the probe round, and re-reading run 6's probe source (`git show d1cfa413b`) eliminated remaining delta
(1) without a run: `HomerDpuDmaArenaCrossprobeRead64` selected its context via
`ClassIndexForWorkload(descriptors[ringIndex].workloadClass)`, and ring 1's arena descriptor class is
COMPLETION (enforced at homer_service_dpu_dma.c:2300), so run 6's proto=15 read at the failing address
ALREADY rode `dmaContexts[COMPLETION]`. Submit flavor is likewise non-discriminating (the working
grouped sweep :10717 uses the same `doca_task_submit_ex(FLUSH)` as the failing :2378). Also nailed:
arena role-3 `hostControlOffset = 0` (homer_frontend_agent.c:771), so the failing address IS
`hostRingAddress` and the old 4th probe read was already aimed at it. SURVIVING per-read deltas: (a)
copy size 24 vs 64, (b) dst pool/mmap — the failing path is the only user of
`localBackendCompletionMmap`. Committed ladder (one flip per rung, all at the failing address, all
gated by `HOMER_DPU_ARENA_CROSSPROBE`): `ctl-64-grouped` (run-6 re-baseline) / `ctl-24-grouped` (size
flip) / `ctl-64-pool` (dst flip) / `ctl-24-pool` (both) / `ctl-24-pool-persist` (bit-exact live
replica: real Fix-1 persistent dst buf + set_data(addr,0) cursor pin + submit_ex FLUSH; caveat: rung 5
vs 4 flips buf-object AND submit flavor together — a follow-up single run splits that cell if needed).
Option-B instrumentation ships in the SAME build: `[ctl-read-diag] pre-submit` prints live src/dst
doca_buf data ptr+len in `HomerDpuDmaSubmitOneBackendCompletionTask` (expect src{data=addr len=24},
dst{data=&control len=0}; any drift = cursor bug caught red-handed) and a post-completion dst-buf
state line in the acceptance fail dump (len=24 at &control while the snapshot reads zero = DOCA claims
it wrote exactly where we read nothing). Host-side leg: after the failure, `od` the arena mailbox
header at offset = (ctl-* host VA − arena-base host VA), both printed by the ladder; proto=15 there
completes the contradiction. DECISION TABLE: zeros first at `ctl-24-*` → 24-byte-copy quirk
(workaround: widen control reads to 64); zeros first at `ctl-*-pool` → completion-pool
mmap/registration defect; all rungs proto=15 while the live task still fails → per-read properties
fully innocent, engine state at live-submission time is the axis (pre/post ctl-read-diag lines become
the primary evidence). Ungated builds verified bit-identical (strings count 0). Deploy: gated build on
farnet1 DPU only; farnet0 DPU + hosts unchanged from run 7.

**RUN 8 RESULT → ROOT CAUSE FOUND (July 10, 2026).** All six ladder rungs read proto=15 — including
`ctl-24-pool-persist`, the bit-exact one-shot replica — so every per-read property is exonerated. The
decisive NEW datum was option B's pre-submit lines: **~100 successful live CONTROL_READs preceded the
failing one** (acceptance is fatal-on-first-mismatch, so they all delivered proto=15). The bug is an
EVENT, not a property: something transiently zeroes the mailbox header. Post-completion diag shows
DOCA appended 24 bytes exactly at `&control` (dst data==addr, len==24) while the snapshot reads zero,
and the host `od` at the failing offset showed `0000000f` immediately after the failure — the DMA
faithfully copied memory that WAS zero at sampling time and was re-stamped nanoseconds later. The only
writer in the codebase that zeroes that header is
[HomerFrontendAgentInitArenaSlot](homer_frontend_agent.c) (`memset(&slot->completionMailbox, …)` then
proto re-stamp), and `HomerFrontendAgentBindArenaSlot` called it as "belt-and-braces" right after the
FREE→BOUND CAS — executed by the SPAWNED BACKEND during startup, while the DPU (which bound its ring
at spawn time) polls the mailbox with ~100% read-in-flight duty cycle. The bind-site comment even
warned "the DPU … may DMA-read this slot's completion mailbox at any time" but reasoned only about
stale-VALUE regression, not the reset's own memset→restamp ZERO WINDOW. **This is the second bug
behind one symptom**: pre-`ce31e63a1` the header was never stamped (round-1 zeros were truthful);
`ce31e63a1` stamped at creation AND bind — fixing bug one while arming this race. The completion
PUBLISH path only writes `publishedEpoch` (never proto), so no alternative writer exists; the backend
was alive at failure (rules out release/reap).

**FIX — citus `b29a90330`.** Bind now VERIFIES the invariant the code already documents ("a slot is
always empty and ABI-initialized while it is FREE" — maintained by CreateArena + ReleaseArenaSlot +
the S3.2b reaper) and fails the claim loudly on violation (`arena slot N violated the
FREE-implies-initialized invariant …`, backend FATALs) instead of writing tenant-visible bytes over a
live DPU poller. Host-only change (citus.so on the PostgreSQL node); no ABI bump, no DPU redeploy.
Built + installed on farnet1 (citus install → postgres ninja relink → meson install; HomerDpuFrontend
symbols verified). RESIDUAL (S3.4/S4 teardown scope, documented in the commit):
`ReleaseArenaSlot`/reaper still memset while a DPU ring could poll — correct fix is teardown ordering
(DPU unbinds ring BEFORE slot release), not weakening the FREE invariant. Not hit by current
validation flows (kill -9 skips on_proc_exit; agent dies with postmaster before reaping). Run 9 in
flight: same topology as run 8, farnet1 DPU keeps the gated build (instrumentation now serves as
confirmation — expect pre-submit lines with NO zero read, completion pulled, and the benign
`missing frontend completion event descriptor` P2-seam end state).

**RUN 9 (July 10, 2026): completion-zero race CONFIRMED DEAD — 318,660+ live control reads, zero
mismatches, engine never fataled.** But the new bind verification FATALed the backend with
`arena slot 0 violated the FREE-implies-initialized invariant (cmd proto=15 slots=64 pub=1 con=0,
cpl proto=15 pub=0 con=0, res proto=12 bytes=262144)`. Reading it against the constants: `res
proto=12` is CORRECT (12 IS `CITUS_TUPLE_SINK_PROTOCOL_VERSION` — homer_abi_version.h:29; the
validation agent's stale-byte-ring theory was a red herring), `cpl` was pristine — the ONLY violated
condition was **`cmd pub=1`: the DPU had legitimately published command sequence 1 into the role-2
mailbox between spawn COMPLETED and the backend's bind** (`landed peer command session=1 sequence=1`
in the DPU log; command landing overlaps backend fork/exec by design). The b29a90330 check was too
strict on exactly that producer-owned field. **THIRD latent bug thereby exposed**: the OLD bind-time
memset was also silently destroying that pre-published command (body + publishedEpoch) in runs 6–8,
stranding the DPU's in-flight bookkeeping — the next blocker in line had the completion race not
fataled first. Failure-cascade note for S3.4/S4: with the backend FATALed pre-bind, the DPU polled
the completion mailbox FOREVER at 100% CPU (~1.2M diag lines) — "backend died before publishing
anything" needs a give-up path (spawn-state machine can watch the pid) in the teardown stage.

**REFINEMENT — citus `039f56b08` (built + installed on farnet1).** Bind verification now accepts the
DPU-pre-published role-2 state: `cmd.publishedEpoch <= slotCount` (window bound; torn-read-safe since
the DPU writes it as one aligned u64) with `cmd.consumedEpoch == 0` still strict; all backend-owned
counters (cmd consumed, cpl published) and host-init-owned proto/geometry stamps remain strict. Run
10 in flight, same PASS criteria as run 9. Unrelated observation from run 9: a `dbcomm tpch_sf10`
PostgreSQL instance appeared on farnet1 ~27 s after the run's cleanup — foreign to this workstream,
left untouched; check ownership before preflighting future runs.

**RUN 10 — PASS. P1 CLOSED (July 11, 2026, citus `039f56b08`).** Backend bound (no FATAL — the
`FREE-implies-initialized` tripwire did not fire), the completion-control read that fataled in runs
4–8 now succeeds, and the DPU's control-read poll loop TERMINATED after exactly **22** `[ctl-read-diag]`
pre-submit iterations (run 9 spun 300k+ and climbing; the bounded count is the proof the completion
was actually pulled, not merely non-fatal). `grep -c` over the full farnet1 DPU log: `backend
completion control validation failed` = 0, `snapshot protocol mismatch` = 0, `fatal error state` = 0;
`remote exec backend` (pid 1182603) was `Rs` (alive) after the workload. The engine then advanced to
the documented P2 seam — `DPU frontend completion event publication failed: selected-DPU session is
missing frontend completion event descriptor service_session_id=1`, repeating as its own retry loop
(no client-side role-6 descriptor exists until P2). The pgbench client failed identically to runs 8/9
(`invalid arguments while peeking Homer completion`) — the completion never reaches the client, which
is precisely the P2 gap, not a regression. Intended end state: spawn → bind → command landing →
**backend executes** → completion read/pull all work; only completion *delivery to the client* (P2)
and results (P3) remain.

**Closing tally — three latent bugs lived behind one symptom, peeled in order:**
1. `ce31e63a1` (pre-P1): arena completion-mailbox control header was never stamped → round-1 zeros
   were truthful. Fixed by stamping at slot init/recycle. (Symptom persisted → not the whole story.)
2. `b29a90330`: that same stamp, re-run at BIND while the DPU polls, has a memset→restamp zero window
   the DMA can sample → the deterministic blocker. Fixed by making bind verify, not rewrite.
3. `039f56b08`: bind's verification was too strict — the DPU legitimately pre-publishes the role-2
   command before the backend binds, so `cmd.publishedEpoch` is not zero. Relaxed to a window bound.
   (Bonus: the OLD bind memset was silently destroying that pre-published command in runs 6–8 — a
   fourth bug that would have surfaced next.)

**Post-P1 cleanup queued (not blocking P2):** remove the `HOMER_DPU_ARENA_CROSSPROBE` crossprobe
ladder + `[ctl-read-diag]` prints (spent scaffolding); add a backend-died-pre-publish give-up path in
the spawn/teardown machine (run 9's infinite poll); reorder teardown so the DPU unbinds the ring
before `ReleaseArenaSlot`/reaper memsets it; extend Fix-1 persistent-dst cursor hygiene to
SLOT_READ/CONSUMED_EPOCH and the other five task builders.

## 8. P2 execution record — runs 13–16: three stacked root causes, then the bridge closed (July 11, 2026)

Run 12 ended in a STOP: node B silent after "landed peer command", static inspection exhausted.
The way out was an **evidence-quality correction**, not more code reading:

**The absence-of-evidence error (found via owner nudge "was it ever granted?").** `HOMER_SERVICE_LOG`
compiles to `((void) 0)` unless `HOMER_SERVICE_VERBOSE_LOGGING`; every SUCCESS path in the
publish→pull→egress chain logs through it, while failures use raw `fprintf`. So "0 egress lines in the
log" had only ever meant "no egress FAILURE" — Codex and I spent runs 11–12 debugging a node B that was
(mostly) working. Countermeasure: `p2diag` (`HOMER_DPU_P2_DIAG`, citus `349f4d58a`) — counters +
prints at every chain edge on BOTH nodes (publish grants/submitted, pull, accepted, enqueued, egress,
ingress landed/queued, role-6 push) + a throttled `snap#N ARM{...} DID{...}` snapshot. Lesson recorded:
**instrument the success edges before debugging silence; a missing log line in this codebase proves
nothing.**

With eyes on, three latent defects fell in three runs:

1. **Run 13 — node A never creates a selected session** (`INGRESS ... selected=no(legacy path)`).
   P1's egress fork in `HomerServiceDpuStageOneBackendCommandForPublish` returns early ABOVE the local
   path's `HomerServiceDpuFindOrCreateSelectedSession`, so the cross-node client's completions had no
   selected-session to land in. Fix `820b66ba2`: create + stamp the selected session in the fork
   (currentCommandSequence, commandInFlight, bridgeGeneration, inFlight* fields) after the egress slot
   commits.
2. **Run 14 — role-6 descriptor never found (7,087,703 misses).** The client stamped its command-session
   descriptors' `serviceSessionId` with an OWNER TAG (its `dpuBridgeGeneration`); the import seeded
   `boundServiceSessionId` from it; lookups keyed by the REAL session id could never match. Latent since
   S4.1 — no consumer of role 6 existed until the P2 peek-redirect. First fix attempt (`f1ab6c094`)
   keyed the lookup by the tag instead — **dead end**:
3. **Run 15 — the tag leaked into the payload.** `HomerDpuDmaFindDescriptorRef` fills the resolved ref's
   `serviceSessionId` from the BINDING, so keying by tag pushed the tag into the client-visible event
   body → `selected-DPU completion identity mismatch: session=1 event_session=2433107406503011`.
   Proper fix = **option B** (`553774131`): the client declares the rings UNBOUND (`serviceSessionId=0`,
   the D5 arena convention), and the service stamps the REAL id at OPEN hand-out via new
   `HomerDpuDmaBindFrontendSessionRings` (homer_service_dpu_dma.c) — the direct twin of
   `HomerDpuDmaBindRingSession` for client command-session exports. `serviceSessionId` now means what it
   says on every export; basebackup paths (which legitimately REQUEST an id) untouched.

**Run 16 — P2 CLOSED, new blocker exposed.** The client completed **five full command round-trips**
(sequences 1–5: both warmups + three transaction commands), each showing the complete chain on both
DPUs (`PUBLISH submitted → ENQUEUED terminal → EGRESS SENT` on node B; `INGRESS selected=YES → QUEUED →
role-6 push SUBMITTED` on node A), then failed at exactly the predicted P3 gap:
`Homer sql_execute failed: tuple-sink queue descriptor did not include a queue name`
(backend-side ereport at homer_tuple_queue_frontend.c:615 — no result-queue descriptor exists until
P3). Identity mismatch gone; `missing frontend completion event descriptor` = 0 on both nodes.

**But:** on command 6's error exit the ORIGINAL P1-shaped fatal recurred on node B —
`backend completion control ... snapshot{proto=0 reserved=0 published=0 consumed=0}` → engine sticky
fatal → node A spun forever. This is NOT a P1 regression: it is the **documented teardown residual**
(see `b29a90330` commit notes and §7 closing) firing for the first time, because run 16 was the first
run in which a backend ever exited GRACEFULLY (every prior run ended in `kill -9`, which skips
`on_proc_exit`). Chain: backend `ereport(ERROR)` → PG_CATCH publishes FAILED completion
(remote_execution_backend_bridge.c:3033) → `proc_exit(1)` (line 3076) → `on_proc_exit` →
`HomerFrontendAgentReleaseArenaSlot` → `HomerFrontendAgentInitArenaSlot` memset
(homer_frontend_agent.c:576) **while the DPU still DMA-polls role 3** → zero-window read → fatal.
Sharper than noise: on other timings the memset can destroy the terminal completion BEFORE the DPU
pulls it — the client would hang with no error. Fix design in §9 (P2.T).

## 9. P2.T — arena-slot teardown handshake (design of record, July 11, 2026)

**Invariant to establish:** the host never memsets an arena slot while any DPU ring bound to it can
still issue DMA against it. (`ReleaseArenaSlot`'s reset stays load-bearing — FREE-implies-initialized
— the fix is strictly an ORDERING protocol in front of it.)

**Alternatives considered:**
- **D-A (agent-reclaimed):** backend exits without reset; DPU notifies the frontend agent over the
  doorbell TCP connection when it has unbound; agent reclaims. Architecturally the endgame (it also
  covers crash paths), but needs a new DPU→host control message + agent reclaim walk. Deferred to
  S3.4; residuals below point at it.
- **D-B (tombstone in the control word):** backend writes a detach sentinel into the role-3 control
  header; DPU treats it as clean detach and acks by DMA. Rejected: overloads the snapshot-protocol
  validation that is our only corruption canary, and still needs a new ack write path.
- **D-C (service-ordered RELEASE command) — CHOSEN:** the node-B service already learns the exact
  moment a session is over (it pulls and egresses the terminal completion), already has an exact-order
  write channel into the slot (role-2 command publish), and the backend already sits in a command
  loop. Teardown becomes one more command. Zero new DMA paths, zero new collectors, and it doubles as
  the socketless backend's missing clean-shutdown signal (the S3.4 gap).

**Protocol (D-C):** on accepting a completion whose `postCommandState ∈ {DO_NOT_REUSE, FAILED}`
(session-terminal — NOT merely `TupleSinkServiceCommandStateIsTerminal`, which is true of every
completed command) for a selected-DPU session with a bound arena slot, the node-B service runs a
per-session teardown phase machine:

- **T1 QUIESCE** — new engine API sets a new per-ring runtime flag `pollQuiesced` on the slot's role-3
  ring; `HomerDpuDmaSubmitBackendCompletionPulls`'s scan skips quiesced rings (next to the existing D5
  unbound-skip, homer_service_dpu_dma.c:1520ff). Bindings stay intact — a partial UNBIND would both
  break the role-2 publish and trip the "partially bound = allocator bug" refusal in
  `HomerDpuDmaBindRingSession` (:5689).
- **T2 DRAIN** — advance when (a) the session's completion-event queue is empty (terminal completion
  egressed) AND (b) the slot's three rings have zero in-flight DMA tasks. Requires a new per-ring
  `inFlightTaskCount` on `HomerDpuDmaRingRuntime` (submit++ / completion-- for every task addressing
  the ring: control read, slot read, consumed-epoch credit write, command publish). Class-level
  counters are too coarse under multi-session; per-ring is the honest gate. This also guarantees the
  final credit write (`HomerDpuDmaSubmitBackendCompletionCreditPublication`, submitted at accept time,
  asynchronous) lands BEFORE the release — otherwise it could dirty a freshly reset slot.
- **T3 RELEASE_PUBLISH** — publish new command kind `CITUS_REMOTE_EXEC_COMMAND_BACKEND_SLOT_RELEASE`
  (9U, homer_control_abi.h) to role-2 through the EXISTING
  `backendCommandPublishSlots` + `HomerDpuDmaSubmitBackendCommandPublication` machinery, sequence =
  next command sequence, marked fire-and-forget (no completion expected → `commandInFlight` NOT set,
  the accept path never sees it).
- **T4 FINISH** — when role-2 in-flight drains (RELEASE landed), full
  `TupleSinkServiceReleaseDpuArenaBinding` (unbind also clears `pollQuiesced`) + destroy the selected
  session + service session.

**Driver:** NO new collector (the S3.3 armed-but-never-named trap). The phase machine arms and pumps
through the existing `HOMER_PROGRESS_COLLECTOR_DPU_BACKEND_COMMAND_PUBLISH`: its ready-count gains a
"teardown pending" term and its action advances the phases.

**Backend side (arena arm ONLY — legacy arms keep exiting immediately; no DPU polls their mailboxes):**
- Command loop: a RELEASE command → release the slot (existing checked release = memset + FREE), mark
  granted (slot index → INVALID so the exit callback no-ops), `proc_exit(0)`. No completion published,
  no consumedEpoch writeback (nobody is listening; the slot is being wiped anyway).
- Terminal commands (`shouldExitAfterCommand`): do NOT break-and-exit; keep consuming — the next
  command is the RELEASE. Bounded await (~10 s, CLOCK_MONOTONIC).
- PG_CATCH: after publishing FAILED + advancing consumedEpoch, await RELEASE (same bound) BEFORE the
  munmaps, then release + `proc_exit(1)`. On timeout: leave the slot BOUND (loud stderr), exit WITHOUT
  memset — a loud leak beats a racy wipe.
- `RemoteExecBackendReleaseArenaSlotOnExit`: gate the release on the granted flag; un-granted exits
  leave BOUND + log.

**Protocol bump:** `CITUS_REMOTE_EXEC_BACKEND_PROTOCOL_VERSION` 15→16
(remote_execution_backend_protocol.h:28) so any stale binary anywhere fails FAST at the proto check
instead of silently never sending/understanding RELEASE. Consequences: both DPUs must resync+rebuild;
the stale `/dev/shm/citus_homer_frontend_arena_v2` (stamped 15) must be removed at clean baseline
(already SOP).

**Residuals (documented, deliberately NOT fixed here):**
- Backend FATALs before the command loop (bind failure etc.) and hard crashes leave the slot BOUND;
  the agent reaper's later memset can still race a DPU that never quiesced. Real fix is D-A (S3.4).
- A session that dies WITHOUT a terminal completion (client vanishes mid-command) has no teardown
  trigger — existing "backend-died / give-up path" backlog item.
- Node A's spin-forever when the peer goes silent (run 16's `pullg` 2M→22M) is untouched; separate
  client-deadline fix is queued (pgbench `HomerRunCommandAndWait` has no deadline).

**Validation gate (run 17):** two back-to-back cross-node pgbench attempts with NO restarts between.
PASS = attempt 1 reaches the P3 gap error AND node-B DPU log shows quiesce→RELEASE→unbind with zero
completion-control fatals AND the backend exits with the slot cycling to FREE; attempt 2 binds a slot
again (REUSE proof — the first-ever second tenancy of an arena slot) and reaches the same P3 gap.
`snapshot{proto=0` count must be 0 on both DPUs.

### 9b. Run 17 — handshake works, two teardown-boundary defects (July 11, 2026)

**Run 17a (INCONCLUSIVE, runbook bug):** my runbook said "both host services down" (§6's old rule) and
omitted `--homer-dpu-command`; the client cannot boot without a local control shm (§4 P3.2 note — see
the superseded-in-part banner added to §6). Runs 13–16 had passed this precondition partly by ACCIDENT
via a stale `/citus_remote_execution_control_v27`. Corrected topology: client-side host service UP
(control hosting only, log must stay activity-free), backend-side host service DOWN.

**Run 17b (FAIL, both diagnosed same-day):** the handshake core WORKED — T1→T2→T3 in order,
`received BACKEND_SLOT_RELEASE for arena slot 0` in postgres.log, backend exited cleanly, zero
P1-style fatals, zero accounting underflows. Then two defects at the teardown BOUNDARY:

1. **T4 killed the service via a deliberate guard.** T4 called `TupleSinkServiceResetSession` directly;
   that function is the peer-close lifecycle's "single funnel" and `exit(1)`s
   (tuple_sink_service_process.c ~23150) if the peer can still RDMA-write the session's registered
   command mailbox (`TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister`) — which it could, the
   client having just written a TX_ABORT. Three independent confirmations: node-B log silence after the
   "refusing to reset" print, node-A `CM event=DISCONNECTED` at the same sequence, postmaster
   doorbell-down. **Fix:** T4 tears down ONLY the DPU/backend half (arena unbind + selected-session
   reset) and RETAINS the service session, fenced, until the peer-close lifecycle ends it (bounded,
   documented leak — see "open items" below). The service must NEVER exit(1) out of teardown; that
   guard now stays where it belongs, in the peer-close funnel.
2. **No post-terminal fencing, and a sequence collision.** On the FAILED completion the client's error
   recovery sent TX_ABORT as sequence 6 while the teardown machine allocated sequence 6 for its
   RELEASE. The RELEASE won the mailbox slot; the TX_ABORT vanished; the client burned its (new) 30 s
   deadline, then failed CLOSE_SESSION on the busy role-1 slot. **Fixes:**
   - Service: persistent `dpuBackendTeardownStarted` on the service session (set at T1, survives T4);
     the peer-command landing path consumes-and-REJECTS commands for fenced sessions (advances the
     landing frontier exactly like the normal path — otherwise the record re-lands forever — but never
     stages, never touches selected-session state; loud stderr per reject). A post-teardown
     CLIENT_SQL_SESSION_CLOSE is logged specially; synthesized close-acks are deliberately NOT built.
   - Client: `HomerClientSession.sqlSessionTerminal`, latched at the completion-lease convergence point
     when `postCommandState ∈ {DO_NOT_REUSE, FAILED}` (the socketless backend exits on any failed
     command, so FAILED IS session-fatal by contract), and set by pgbench on a wait timeout. Terminal
     sessions skip the session-finish TX_ABORT and the role-1 CLOSE_SESSION submit, going straight to
     DPU setup-close (which still tears down the client's import on its local DPU).

**Open item (needs owner discussion, deliberately not improvised):** node-B service-session
END-OF-LIFE when the client cannot CLOSE through a dead backend. Current state: the fenced session +
its registered MRs linger until service restart (each pgbench attempt uses a fresh session, so
validation is unaffected). Options: (i) accept the leak until the S4 session-lifecycle work (current
choice); (ii) teach node B to handle a landed CLIENT_SQL_SESSION_CLOSE post-teardown service-side —
synthesize the close completion, mark `peerCloseCommandObserved`, and let the peer-close funnel run
`ResetSession` legally. (ii) is the natural endgame but is new plumbing on the close path; it also
interacts with which side allocates close completions once the backend is gone.

Run 18 gate: as §9's run-17 gate, plus the farnet1 DPU service must be ALIVE after each attempt, the
fence line must NOT fire (a firing fence means a client sender was missed), and the client must exit
promptly with the two new "skipping" lines instead of the 30 s stall.

**RUN 18 — PASS. P2 + P2.T CLOSED (July 11, 2026).** Two back-to-back cross-node pgbench attempts,
zero restarts between them. Both attempts: full T1→T2→T3→T4 (`teardown T4 released backend; retained
fenced service session=N`), `received BACKEND_SLOT_RELEASE for arena slot 0` exactly once per attempt
(grep -c = 2), backend exits by itself, client hits the P3 gap then exits promptly with
`skipping session-finish abort` + `skipping CLOSE_SESSION submit` (no 30 s stall, no busy-slot close
failure). **Slot-REUSE gate proven:** session 2 spawn `COMPLETED`, re-bound the SAME arena slot 0 with
no bind FATALs — the first second tenancy of an arena slot. farnet1 DPU service alive after both
teardowns; every fail-trigger grep 0, including the fence (`rejected peer command after DPU backend
teardown` = 0 — the client fix held, the fence remains pure insurance). Anti-fallback guard held: the
farnet0 host service log stayed startup-only for the whole run. Artifacts: scratchpad `run18/`.

Deferred by explicit choice: `HOMER_DPU_P2_DIAG` scaffolding STAYS through P3 — it instruments exactly
the command/completion chain P3's result path rides, and this arc exists because success-edge
visibility was missing; strip it at P3 close instead. The fenced-session bounded leak stands as the
S4-scoped open item (§9b).

## 10. P3 grounding (codex trace, July 11, 2026) — decisions + corrections to §4's P3 plan

**D-P3.1 (resolves §4's deferred decision): result-stream identity is minted EAGERLY at receiver-side
OPEN — precisely inside `TupleSinkServiceBeginDpuBackendSpawn`, after the arena bind records
`dpuArenaSlotIndex`/`dpuArenaBridgeGeneration` (~:19800-19808) and BEFORE
`TupleSinkServiceFillBackendSpawnRequest` freezes startup data (:19810).** At that point
serviceSessionId + slot + generation are all in hand; lazily creating at first START is too late
(`HandlePeerStartCommandRequest` :39016+ finds an already-spawned session and cannot retrofit
immutable startup data). CAVEAT: the tuple CONTRACT is unknowable at OPEN (the executor derives it in
`RemoteExecSqlDestStartup` :1322/:1349) — so OPEN reserves the session-scoped stream IDENTITY
(`HomerServiceCreatePayloadStreamEntry` :24640 allocates serviceStreamId; mirror-pool bind
:17559/:17583 keys on (parentServiceSessionId, serviceStreamId)); contract/key finalization stays
lazy at the first row-producing command.

**Corrections to the plan (things simpler than §4 assumed):**
- NO new spawn/startup ABI fields: `resultServiceSinkId` + `resultQueueDescriptor` already exist on
  BOTH `CitusRemoteExecBackendSpawnRequest` (remote_execution_backend_protocol.h:210/228/229) and
  startup data (:173/205/206), and `ProcessSpawnRequestSlot` already copies them
  (remote_execution_backend_bridge.c:3345). Only `TupleSinkServiceFillBackendSpawnRequest` (:19622)
  leaves them unfilled, and the backend must copy startupData->resultServiceSinkId into its session.
- The arena branch ALREADY fills the role-5 source descriptor's physical geometry and maps
  `resultQueueControlMappingAddress` (remote_execution_backend_bridge.c:2933/2957/2967/2979);
  `queueShmName` stays deliberately empty — the :615 queue-name check is only hit because the plain
  `OpenCitusTupleSink` (homer_tuple_queue_frontend.c:952, `MapTupleSinkQueue` by name :984/:1011)
  cannot adopt an embedded mapping. The adopt variant binds the handle to the already-mapped address
  and skips shm_open/munmap ownership; immediately after every adopted open/rebuild, call
  `CitusTupleSinkAttachDpuPublishMirror` (decl homer_tuple_queue_frontend.h:124, def :164) with
  `&arena->hostPublishLines[slot*3+2]`, `arena->bridgeHeader.bridgeGeneration` (validate nonzero —
  the shared header is advisory, homer_frontend_agent.h:227), and the global role-5 ring index/id
  (constants homer_frontend_agent.h:130; builder :803/:874).
- NO new node-A data plane: incoming payload dispatch already routes flagged tuple streams into the
  two-ring deform relay (tuple_sink_service_process.c:33420), which resolves the client's role-7 ring
  by the stamped `sessionUID` (:32964, resolution :32331). The command session must carry the same
  tuple family + relay flag + contract + peer payload connection + client sessionUID + bound role-5
  sink id. Sender side: outgoing dispatch (:30817) → mirrored-range query by (session,stream) →
  `HomerServicePumpOutgoingDpuMirrorByteRingPayload` (:30292+), with
  `HomerDpuDmaMirroredRangeMatchesServiceSink` matching boundServiceSessionId+SinkId
  (homer_service_dpu_dma.c:4500) and `HomerDpuDmaBindArenaResultSink` (:6021) as the join stamp.
- Client: the role-7 drain/decode ALREADY exists (`HomerDrainPendingDpuResultRelay` pgbench.c:3761 →
  `HomerClientPollSqlResultDpuReceive` homer_client.c:4850+). The gap is PATH SELECTION only: role 7
  is opened (:9704) and drained (:3847/:3940/:3951) only under `homer_dpu_mode`; selected-DPU command
  mode must open/drain role 7 itself (interim: require both flags; final: fold into
  --homer-dpu-command). The old `control == NULL` START blocker is ALREADY GONE (post-P2:
  homer_client.c:6046/:6202 submit via role 1). Known small gap: relayed ERROR records drain without
  surfacing (:4963).

**Hazards (P2.T interplay) — the first one moves into batch 1:**
- Role-5 rings are ALREADY ENROLLED in grouped-control discovery (homer_service_dpu_dma.c:7034) and
  `HomerDpuDmaSubmitGroupedControlReads` (:1293) does NOT consult `pollQuiesced`. Once a sink binds,
  grouped reads perpetually refill role-5 in-flight counts → every T2 drain stalls into the 30 s
  forced path. Batch-1 interim: grouped scan skips `pollQuiesced` rings, and T1 ALSO quiesces role 5
  (accepting that an un-egressed result tail is dropped at teardown — fine while bring-up clients
  abort on gap errors). The FINAL shape (hop 7) replaces T1's role-5 quiesce with a result-drained
  subphase: wait for final role-5 publication discovery + payload EOS completion, THEN quiesce, THEN
  the existing three-ring in-flight gate.
- EOS ordering: terminal egress already blocks on `payloadEosPosted` + completed source frontier
  (:18445/:18563/:40338); P3 must make that frontier mean "visible through node A's role-7 relay,"
  not merely "pulled from role 5."

**Implementation order (smallest observable hops):** 1 identity-only OPEN hop (allocate stream id,
fill spawnRequest.resultServiceSinkId, backend logs nonzero id) + the batch-1 teardown interim;
2 role-5 producer hop (adopt-mapping open + mirror attach; first publish epoch visible); 3 DPU mirror
join (`BindArenaResultSink`; discovery resolves (session,stream)); 4 node-B egress hop (peer payload
stream; mirrored bytes + EOS advance); 5 node-A relay hop (sessionUID resolves; role 7 advances);
6 client gate (selected-DPU command mode opens/drains role 7; decoded values + ERROR propagation);
7 terminal result-bearing completions with EOS ordering + the result-drained teardown subphase.

### 10b. Run 19 (P3 batch 1) — every hop validated; stall = the EOS gate meeting the missing hops (July 11, 2026)

**Attempt 1 (uncontaminated) — batch 1 works.** `reserved pending DPU result stream identity
session=1 sink=1`; NO queue-name ERROR; the SELECT **executed and completed** (terminal epoch=6
state=COMPLETED flags=3 sink=1); the backend **published 112 bytes into role-5**
(`role-5 publication discovered before sink bind; deferring ready enqueue ... tail=112` — proves the
adopted open AND the mirror attach armed the commit-site stamps, and the engine's sink-gate deferral
fired exactly as designed with hop 3 absent). The terminal completion was ENQUEUED but never egressed:
the §5 EOS-ordering gate (payload-before-completion, :18445/:18563/:40338) correctly refuses to egress
a result-BEARING completion whose payload cannot flow (hops 3–5 missing). Client hit its 30 s deadline
→ self-latched terminal → skipped TX_ABORT/CLOSE (deadline-latch path, as designed). **Consequence for
sequencing: hops 3–5 are one atomic validation unit with batch 1 — a result-bearing command cannot
complete client-visibly until payload egress exists.** (Run 16/18 never saw this because their SELECTs
FAILED pre-publication, so those completions carried no payload gate.)

**Attempt 2 (contaminated by the reap; findings still real):**
- **Reaping a live `remote exec backend` with kill -9 mid-run = full postmaster crash-restart**
  ("terminated by signal 9 ... reinitializing"). Runbooks must treat backend reaps as restarts. Bonus:
  the crash-restart's frontend agent correctly reaped the leaked arena slot itself.
- **Crash-restart hazard (new):** the surviving arena (crash-restart does not re-run _PG_init) keeps
  STALE publish-line generation stamps from the old tenancy; after the new agent re-exports under a
  new bridge generation, grouped-control reads fail
  `host publication generation does not match descriptor` → `PE drain failed`. The
  slot-body reset does not clear `hostPublishLines[]` (arena-level array, not slot body). Fix
  candidates: re-zero publish lines at re-export/import-accept, or at slot release. S3.4/D-A
  adjacent; not a batch-1 bug.
- **`TupleSinkServiceResetSession`'s exit(1) guard is reachable from the spawn/OPEN-failure path**
  (session=2's failed spawn → "refusing to reset peer CLIENT_SQL_SESSION before command-mailbox
  writers quiesce" → silent service death — the run-17 crash CLASS via a different door; the T4 fix
  only removed the teardown-machine callers). Every ResetSession caller reachable while the peer can
  still write the session mailbox is a landmine; needs the same treatment (defer to peer-close
  lifecycle) or writer-quiesce before reset on the failure paths.

Named-string FAIL greps again all 0 through both incidents — silent process death remains the blind
spot; future runbooks: add explicit service-liveness checks after every phase (run 18 had them; keep).

**Batch 2 (next): hop 3 `HomerDpuDmaBindArenaResultSink` (bind at reservation time — the deferral
gate already proved it holds publications safely) + hop 4 node-B peer payload egress for the pending
stream (contract finalization at first result-bearing command; mirror-pool bind; peer payload
connection) + hop 5 node-A sessionUID→role-7 relay. Then rerun; expected: attempt-1's stall point
advances to the client's result-open (the original run-19 prediction, one batch late).**

### 10c. Batch 2 partial land + the hop-4 STOP (July 11, 2026) — needs owner discussion

Landed (uncommitted, tree = citus working dir): hop 3 sink bind at reservation (:19913, undoes cleanly);
contract finalization at the first TUPLE_SINK_READY completion (`HomerServiceDpuFinalizePendingResultStream`
:41106, called from acceptance :41447 — earliest honest point, the executor has derived the contract;
key reconstructed as (timestamp=sessionId, txn=commandSequence, rel=InvalidOid); slot count =
byteRingBytes/maxRecordBytes); hop-5 verification (node-A wiring EXISTS: relay/sessionUID captured at
command OPEN :38993, propagated through peer payload OPEN :37237, soft-retry when role 7 absent
:33157); ResetSession exit(1) guard fenced at 8 OPEN/spawn-failure sites via
`TupleSinkServiceResetPeerOpenFailureIfSafe` (:23642) — failure paths never exit(1) again.

**STOPPED (deliberately, per instruction): driving the peer payload OPEN from node B for a
command-session result stream (:41201).** The proven basebackup/async flow requires three things the
command session does not retain and the backend completion does not carry: the original
`peerEndpoint`, a `placementAccessDescriptor`, and an owned local-control ASYNC slot. Options:
(a) retain the peer endpoint on the command session at its OPEN + allocate an async slot for the
payload open + pass an empty/NA placement descriptor (receiver must tolerate); (b) a lighter
command-session "result stream announce" message on the existing peer control channel instead of the
full payload OPEN; (c) extend the payload OPEN with a command-session variant. Node-A side already
routes by relay-flag + sessionUID, so the receiver half is mostly indifferent. DECISION PENDING —
brought to the owner before proceeding.

### 10d. Owner review of batch 1/2 + hop-4 RESOLUTION (July 11, 2026)

**Hop 4 RESOLVED — reuse the EXISTING v17/v18 result peer-open; none of §10c's (a)/(b)/(c) as framed.**
The result peer-open already exists as a protocol message (`clientSqlResultDpuRelay` on the
command-session AND result open requests since v17, `sessionUID` since v18 —
remote_execution_peer_control_protocol.h:33-34), the --homer-dpu path drives it today (:37237 fills
the outgoing result open from session state), and node A has a dedicated receiver branch
(`clientSqlResultOpen && clientSqlResultDpuRelay`, :35334). Placement descriptors are the COPY/insert
flavor's business (:21744+), not this one — the "empty placement" worry was an artifact of comparing
against the BASEBACKUP open, the wrong template. What hop 4 adds is node-B DRIVER STATE only, both
with precedent: (1) record the ORIGIN peer endpoint on the command session at its OPEN (it arrived
over an established peer connection; the legacy path records its endpoint the same way); (2) drive
the result open through a SERVICE-originated local-control async op — same pattern as option B's
`RESPONSE_OWNER_DPU_STAGED_COMMAND` owner kind. No new wire messages, no extra round trips.

**Adopt-mapping variant retained over a descriptor offset field (owner ok'd either).** Beyond the ABI
bump: the descriptor travels CROSS-NODE inside completions, and a (name, offset) pair is host-local
addressing that must never leak to node A (the client consumes via role-7, never by mapping node B's
arena); the backend already holds the arena mapping (a by-name+offset open would double-map with a
conflicting lifetime); and the variant is implemented + validated (run 19). The offset field remains
an acceptable future option if a caller genuinely needs by-name+offset.

**Two-phase stream bookkeeping confirmed as intended semantics:** identity at spawn (rides the
immutable startup data), tuple contract on-demand at the first result-bearing command — nothing else.

**Role-7 soft-retry: transient tolerance now, FAIL-trigger later.** After hop 6 the client exports
role-7 during session open, BEFORE any command can run, so payload can never legitimately arrive
first. The soft-retry mechanism STAYS (shared with --homer-dpu; covers the died-mid-run client whose
ring vanishes with payload in flight — defer-then-teardown beats erroring the shared transport), but
its run-gate meaning flips: once hop 6 lands, the retry/absence diagnostic joins the named
FAIL-trigger greps — any firing with a live client is a bug to investigate, never tolerated.

### 10e. Run 20 — chain advances to contract finalization; TWO batch-2 defects (July 11, 2026)

Progress proven: identity + `finalized pending DPU result stream session=1 sink=1 sequence=5` (new vs
run 19). Then:
**Defect 1 — the service-owned result peer-open fails its own precondition:** `could not start
service-owned P3 result peer-open sink=1: service result peer-open lacks a unique pending stream or
captured origin endpoint`. Chain stops; terminal completion EOS-gated forever; client 30s
timeout (same visible shape as run 19).

> ⚠ **The hypothesis originally recorded here was WRONG and is superseded by §10f.** It read: "Either the
> origin-endpoint capture at command-session OPEN (:39149) never populated, or the 'unique pending stream'
> search runs AFTER finalization cleared `tupleContractPending` (ordering bug)." Both halves are false.
> `TupleSinkServiceStartServiceResultPeerOpen` never *searches* for a stream — the `streamEntry` is an
> argument — so there is no ordering to get wrong, and the capture is fine. See §10f for the real cause.
> Kept verbatim because the *reason* it was believable is the actual lesson (see §10f's postmortem).
**Defect 2 — failed-stream egress retry STARVES all sessions:** session 1's gated completion retried
`egress publish result=2` ~2M/snapshot (egrg climbs, egrs frozen) and session 2's OWN warmup
completion never egressed — cross-session starvation. Fix needs BOTH a terminal-failure path for a
stream whose peer-open definitively failed (fail the completion rather than gate it forever) AND
egress fairness (a stuck head must not monopolize the action).
Positives: slot 16 reuse clean; all P2.T fail-triggers 0; service alive throughout; session-1 backend
answered pg_ctl stop -m fast from the RELEASE await (CHECK_FOR_INTERRUPTS working; exited "leaving
arena slot 0 BOUND"); session-2 backend (stuck pre-await) needed end-of-run reap as expected.
NEXT: fix defect 1 (endpoint capture / pending-stream lookup ordering), add defect-2's
failure+fairness policy, run 21 (same spec as run 20). Artifacts: scratchpad run20/.
*(That "NEXT" is superseded — see §10f.)*

### 10f. Run-20 defect 1 RE-DIAGNOSED — it is not a bug, it is the missing hop-6 client gate (July 11, 2026)

**The real cause.** `TupleSinkServiceStartServiceResultPeerOpen`
(`tuple_sink_service_process.c:38175`) does not search for anything; its `streamEntry` is a parameter.
What it does is AND **nine** subconditions (`:38184-38195`) and, on any of them, emit **one** generic
message. The subcondition that actually failed is **`!sessionState->clientSqlResultDpuRelay`**.

Why that flag was false: pgbench sets `sessionOptions.clientSqlResultDpuRelay = 1` **only under
`homer_dpu_mode`** (`postgres-citus src/bin/pgbench/pgbench.c:9643-9644`), i.e. only for `--homer-dpu`.
Runs 19 and 20 passed `--homer --homer-dpu-command` and **not** `--homer-dpu`. So the client never
*requested* a DPU result relay, and never *exported a role-7 ring* to receive one — the selected-DPU
opener exports exactly two rings, role-1 control + role-6 completion
(`citus-dbcomm src/bin/homer_client.c:87` `HOMER_CLIENT_DPU_COMMAND_SETUP_RING_COUNT 2U`;
descriptors at `:3112` and `:3147`). Node B **refused correctly**. `sessionUID` was never the problem —
S1a already mints it under either flag (`pgbench.c:9646-9665`).

**So run 20 did not regress and did not expose a peer-open bug. It stopped at batch 2's designed
stopping point** — one hop past run 19 — and what it was missing all along is **hop 6, the client gate**,
which was always scheduled as batch 3.

**Decision (owner directive: decide from the plan's direction, record the reasoning, proceed).**
`--homer-dpu-command` now **IMPLIES** `clientSqlResultDpuRelay = 1` and a bound role-7 ring. Rationale,
straight from P3's premise: a **DPU-spawned backend has no host-shm result sink at all** — its only result
egress is the arena role-5 byte ring, which only the DPU can drain. So for a selected-DPU command session
the result relay is **structurally mandatory, not optional**. The two flags remain independent in the
other direction: `--homer-dpu` alone still means "host command plane + DPU result relay" (the pre-existing
v17/v18 shape). Their CLI validation requirements are already identical (both demand a remote peer,
`pgbench.c:8869/8878`), so composing them needs no new checks.

**Hop 6 is therefore nearly free** — the client half already exists and is exercised by `--homer-dpu`:
`HomerClientBindResultRing` (`homer_client.c:4715`, a reframe of
`HomerClientOpenSqlResultReceiveStreamSelectedDpu`), the drain `HomerClientPollSqlResultDpuReceive`, and
four pgbench sites that gate on `homer_dpu_mode`: the relay flag (`:9643`), the role-7 bind (`:9712`), the
drain diversion (`:3852`), and the shm-sink skip + contract capture (`:3921`). Hop 6 = extend those four
gates to `homer_dpu_mode || homer_dpu_command_mode`. The role-7 rendezvous is node-A-local and keys on
`sessionUID`, and under `--homer-dpu-command` the command session ALSO lives on the client's local DPU
(node A), so the two meet on the same service — which is exactly what hop 5 resolves.

**Consequence for run 21:** the diagnosis is testable with **zero code changes** — run 20's spec plus
`--homer-dpu`, on the already-deployed binaries. That configuration IS the intended hop-6 shape, reached
through existing wiring. (Caveat: the `--homer-dpu` role-7 client path has itself never completed end to
end — see `byte_ring_slot_capacity_regression.md` — so run 21 may find a *new*, further wall. That is
still forward progress and a useful outcome.)

**What IS genuinely defective in batch 2, and is being fixed regardless:**
- **Fix A** — the nine-way compound guard must name the subcondition that failed, and must distinguish
  "client did not request a relay" (an invalid *configuration* for an arena session — fail loudly, set
  `dpuResultPeerOpenFailed`) from "client requested one but our origin-endpoint capture is broken" (a real
  bug). One generic message across nine conditions is what cost run 20 a cycle.
- **Fix B** — §10e's defect 2 stands and is independent of hop 6: a stream whose peer-open definitively
  failed must have a **terminal arm** (fail the gated completion; do not gate forever), and egress must be
  **fair** (a stuck head must not monopolize the action). Run 20's ~2M retries/snapshot starved a second
  session's completion entirely.

**What landed (July 11, 2026), reviewed line by line:**

- **Fix A** — `tuple_sink_service_process.c:38182` `TupleSinkServiceStartServiceResultPeerOpen`. The
  compound guard is replaced by per-subcondition rejects that NAME the failure and print its value
  (`sessionUID=0`, `empty …peerHost`, `peerControlPort=0`, `…ConnectionGeneration=0`,
  `clientSqlPeerConnectionHandle=NULL`, `RDMA generation mismatch … captured=… current=…`, already-pending,
  already-ready). `clientSqlResultDpuRelay=0` is split out as a **terminal configuration failure** —
  `invalid selected-DPU result configuration … client must request the DPU result relay` — and sets
  `dpuResultPeerOpenFailed`, which is what arms Fix B's terminal arm. (The original compound `if` is kept
  commented above it, per house rule.)
- **Fix B1** — `:41926` in `HomerServiceDpuEgressOneSelectedCompletionEvent`. On
  `dpuResultPeerOpenFailed && terminal && (resultFlags & TUPLE_SINK_EOS)`, the queued completion is
  converted in place to `COMMAND_STATE_FAILED` / `RESULT_FLAG_TUPLE_SINK_FAILED` with a client-visible
  `detail`. **Why that releases the gate:** the EOS gate at `:18581` fires only on
  *terminal* ∧ *`resultFlags` has `TUPLE_SINK_EOS`* ∧ *EOS not yet posted*; dropping the EOS bit makes the
  second conjunct false. Verified `FAILED` is both terminal (`:17797`) and push-visible (`:18182`), so the
  publish does not degrade to `NOOP` (which the caller would turn into a hard error). The mutation is
  idempotent across a `NOT_READY` retry — the EOS bit is already gone, so the block is skipped and the
  already-`FAILED` event simply re-publishes.
- **Fix B2** — `:40663` `HomerServiceDpuFindSelectedSessionWithCompletionEvent` + the new
  `peerCompletionEgressScanCursor` (`:760`). Round-robin selection whose cursor advances **on selection,
  not on success** (`:40682`) — that is the entire point: a session whose head then proves gated has
  already yielded its turn. (Advancing only on success would reproduce the starvation exactly.) Per-session
  FIFO is untouched; only the inter-session order rotates. Invariant: **one wedged session costs every
  other session at most one wasted selection per rotation.** Wart: the cursor is named for peer egress but
  is shared with the non-peer publish path (`:41766`); fairness still holds (each call scans all slots and
  wraps), the name just undersells its scope.
- **Fix C (hop 6, client)** — `postgres-citus src/bin/pgbench/pgbench.c`. New derived predicate
  `homer_dpu_result_relay` (`:360`, derived once at `:8932`) replaces `homer_dpu_mode` at the four
  result-path gates: relay flag (`:9693`), role-7 bind (`:9769`), drain diversion (`:3884`), shm-sink skip
  (`:3953`). Two incidental log-integrity fixes found on the way: the **`transport:` line** — the very line
  a validation log is grepped for to prove which stack ran — printed a bare `"homer"` for a
  `--homer-dpu-command` run, i.e. indistinguishable from a plain host-service run; it now names all four
  combinations. And a comment claiming the decode proof is logged under `-d/debug` was propagating the
  `-d` (=`--dbname`) vs `--debug` mix-up that CLAUDE.md records as already having cost a cycle.

**Two other indefinite gates exist and were deliberately NOT touched** (found while fixing B, recorded so
they are not rediscovered as novel): completion-publication source-ring credit (`:18619`) and prior
result-stream send-CQ retirement (`:18677`). Both have live recovery machinery and only become permanent
if their CQ/credit progress itself fails — unlike the EOS gate, whose precondition had become *impossible*.

**Postmortem — why the wrong hypothesis was believable, and the rule it yields.** The error message named
a mechanism ("a unique pending stream") that the function does not implement, and it was written to cover
nine unrelated failure modes at once. So the message *invited* a search for an ordering bug between the
"pending stream" lookup and `FinalizePendingResultStream`, and both function names made that story
coherent. **Rule: a guard that ANDs N conditions must report WHICH one failed. A single message shared by
N conditions is not a diagnostic — it is a decoy, and it will send the next reader (human or model) down
the wrong path with full confidence.** This is the same family as the traps in CLAUDE.md (`pkill -f`
self-match, `rsync -a` mtime, `(deleted)` exe): *an identity check that is exactly right for the case you
thought about, and silently wrong for the one you did not.*

**Second lesson — the runbook is part of the system under test.** Run 21's first dispatch aborted because
the brief told the validator to start postgres with only `-o "-c port=5433"`, silently dropping
`-c citus.enable_homer_dpu_frontend_agent=on` **and** the `HOMER_FRONTEND_DPU_SETUP_*` postmaster
environment that runs 17-20 all carried. Without the frontend agent the backend-spawn region is never
exported to the farnet1 DPU, so a DPU-spawned backend cannot exist. The validator caught it and refused to
proceed — correctly. A hand-retyped runbook is a silent re-derivation of preconditions that took many runs
to establish: **diff a new runbook against the last one that passed, do not retype it.** The canonical
sequence lives in `scratchpad/run17_runbook.md` (steps 1-9 + the RUN 18 DELTA).

### 10g. Run 21 — hop 6 CONFIRMED; the byte-ring mirror geometry bug (July 11, 2026)

**Run 21 validated §10f's re-diagnosis.** With Fix C the client requested the relay
(`transport: homer-dpu-command (implies dpu result relay)`) and the chain advanced TWO hops past run 20:

```
reserved pending DPU result stream identity session=1 sink=1
finalized pending DPU result stream ... sequence=5
started service-owned P3 result peer-open op=0 session=1 sink=1 origin=10.10.1.200:9717   <- run 20's blocker, CLEARED
service-owned P3 result peer-open ready session=1 sink=1 detail=peer stream bound          <- further still
```

Then node B aborted on:

```
DPU mirror position-preserving invariant broken session=1 sink=1
    host_ring=262144  remote_ring=8388608  byte_posted=0 source_posted=0
```

#### The bug (not a sizing preference — a violated ABI rule)

`homer_queue_abi.h:200-221` states the rule outright: *"Why a single constant instead of the old
slotCount * slotCapacityBytes sizing: the sender DPU mirrors the host source ring … and the mirror wrap
position (absolute % ringBytes) MUST coincide with the source wrap position … Deriving the storage of BOTH
the source ring and the mirror from ONE constant guarantees the two moduli are identical. **'slots' (the
count) is gone from the queue descriptor**; the surviving per-record knob is the max record size, which is
**orthogonal to storage**."*

The arena then **resurrected the abolished formula** (`homer_frontend_agent.h:156-160`):
`ARENA_RESULT_RING_STORAGE_BYTES = SLOT_COUNT(256) * MAX_RECORD_BYTES(1024) = 262144`. So the source ring
(arena role-5, 256 KiB) and its mirror (node-A receive ring / client role-7, a fixed 8 MiB) have different
moduli, and the position-preserving relay cannot exist. Run 21 is simply the first run that got far enough
for a result byte to move.

`MAX_RECORD_BYTES = 1024` is NOT the bug and stays — the ABI says max-record is an orthogonal knob, and
`262144 >= 2 * 1024` satisfies the 2x wrap-gap headroom rule. (It is, separately, a real cap on decoded
tuple width; tracked apart from this.)

#### Owner decision (July 11, 2026) — the rule is PER-PAIR, and we will prove it

The mirror only ever needed **each source/mirror PAIR to agree**, not all rings globally. The single global
constant was a *sufficient* condition the ABI adopted for safety, not a *necessary* one —
`CitusHomerPayloadByteRingDescriptor` already carries a per-stream `ringBytes`. Decisions:

1. **State the per-pair rule explicitly** in the ABI comment, in the relay's invariant comment
   (`tuple_sink_service_process.c:30610-30630`), and here. A future reader must not re-derive "one global
   constant" as a requirement.
2. **Purge slot-based vocabulary from the byte-ring path entirely.** It is a byte ring: any size record,
   no slots. The lingering `SLOT_COUNT`-style sizing/naming is what misled this implementation into the
   bug, so it is deleted, not merely commented. (`requestedSlotCount` / `requestedSlotCapacityBytes` on the
   OPEN path, arena `RESULT_RING_SLOT_COUNT`, and any "slot" wording on byte-ring descriptors/comments.)
3. **Give the SQL-result chain its own geometry: 10 MiB (10 * 1024 * 1024 = 10,485,760).** All three rings
   of the chain — arena role-5 (node-B source), node-A receive ring, client role-7 — use it; basebackup
   keeps its 8 MiB. **The sizes are deliberately DIFFERENT between the two chains** so the run exercises
   the per-pair rule instead of silently re-relying on a global constant.
   - 10 MiB is deliberately **NOT a power of two** (2^21 * 5). Verified safe: the ring arithmetic is a true
     `%` (`tuple_sink_service_process.c:7542` `absoluteOffset % ringBytes`), never a `& (ringBytes-1)` mask.
     This is the STRONGER test: a power-of-2 test size would silently pass even if some path *did* assume a
     mask. Cost is one 64-bit division per RDMA **chunk** (not per byte) — negligible.
4. **Enforce the invariant at bind time**, not at first byte. A hard check that every ring in a relay chain
   agrees on `ringBytes`, failing loudly at OPEN/bind. This re-establishes the ABI's safety property at the
   granularity that is actually true. Run 21's abort fired only when data moved — far from the cause.
5. **Split the arena across several DOCA mmap exports.** At 10 MiB role-5 the arena is
   ~16 * (3.05 MiB + 264 KiB + 10 MiB) ~= 213 MiB, over the observed ~100 MB single-region DOCA ceiling.
   This is the documented escape hatch and is already supported:
   `HomerDpuComchSetupHeader.mmapExportCount`, the DPU already loops over exports, and the frontend agent
   already ships TWO exports today (48-descriptor arena + 1-descriptor spawn region).
   **Correction for the record:** an earlier note in this session called the >100 MB arena "structurally
   dead". That was wrong — `homer_frontend_agent.h:110-116` documents the split as the intended way to grow.
   A `_Static_assert` was read as a wall when the comment above it described the door.

#### Second run-21 bug: payload-failure livelock (fixed, pending validation)

After the geometry abort, BOTH DPU services spun at 100% CPU emitting
`tuple-result payload failure had non-frontend owner ... reason=payload-failure-action` forever —
3,181,008+ repeats, logs reaching 1.7 GB and 2.4 GB in UNDER A MINUTE. Cause: the tuple-result failure
delivery path (`:28031-28052`) knew exactly TWO owners of a tuple-result stream (locally-initiated peer
binding; `clientSqlRemoteSender` frontend). P3 introduced a **THIRD** — the service-owned DPU result stream
— which is neither, so it fell into the `return HOMER_FAILURE_DELIVERY_FAILED` arm on every pass, and its
caller re-armed it unboundedly.

**This is the THIRD instance of one pattern in two runs** (the EOS gate in §10e was the first): *a state
machine whose default arm is "fail", invoked from a scheduler loop with no terminal arm.* Fixes: recognize
the P3 owner and commit its failure through Fix B1's terminal arm; give unknown owners a loud one-shot
ABANDON so a future FOURTH owner cannot melt the CPU; rate-limit the diagnostics (an unconditional
`fprintf` inside a retried action is a bug in its own right). A full inventory of every action that can
return hard-failure and be re-armed unconditionally is recorded from the worker's audit — see the P4 item.

**NEXT:** batch 4 = decisions 1-5 above, then re-run run 21's spec. Artifacts: scratchpad/run21/.

### 10h. Runs 22-23 — batch 4 VALIDATED; the real defect is node-B payload egress never arming (July 11, 2026)

**Batch 4 passed every goal it had (run 22).**
- Arena **mmap split works**: 5 imports, arena descriptors tiling `0..47` exactly + the spawn export.
  `/citus_homer_frontend_arena_v3`, 223,370,368 bytes, 16 slots.
- **Per-pair geometry works.** SQL chain at 10 MiB and basebackup at 8 MiB *simultaneously*, with ZERO
  `mirror position-preserving invariant broken` and ZERO `byte-ring bind rejected source/mirror geometry
  mismatch`. The deliberately-different sizes are what make this a PROOF rather than an assumption: nothing
  in the stack silently depended on a global constant. (Owner's call; it paid off.)
- **Livelock gone.** DPU logs 5-15 KB, versus 1.7 GB / 2.4 GB in run 21.

**The real defect: node B binds the result peer stream and then does NOTHING.**
On node B the LAST LINE IN THE LOG is `service-owned P3 result peer-open ready session=1 sink=1 detail=peer
stream bound`. After it: no role-5 mirror/pool registration, no byte-ring pull, no payload egress, no EOS, no
error, not even a failed attempt. Everything upstream is proven: the SELECT lands
(`landed peer command session=1 sequence=5`), is published to the backend, `finalized pending DPU result
stream` fires, the DPU-spawned backend runs at 99.5% CPU. The peer-open handshake completes — and the
role-5 -> peer payload egress is never armed.

**The node-A symptom is FOUR STEPS DOWNSTREAM and its error message is actively wrong.** Node A logs
`WARNING relay target unresolved ... no imported role-7 ring matches this sessionUID; ... Check that the
client used the SAME sessionUID`. A one-shot diagnostic added to `HomerDpuDmaFindDpuToHostByteRingRef`
(`homer_service_dpu_dma.c`, `[role7-diag]`, temporary) settled it:

```
[role7-diag] MISS for sessionUID=3269689567453114 -- dumping every import/descriptor
  import[0] active=1 lifecycle=1 stale=0 gen=3269817316460527 ringCount=2 retirable=1
    ring[0] role=1 (want 7) ...      ring[1] role=6 (want 7) ...          <- command session, correctly skipped
  import[1] active=1 lifecycle=3 stale=0 gen=3269689567453114 ringCount=2 retirable=0
    ring[1] role=7 (want 7) sessionUID=3269689567453114 (want 3269689567453114) gen=MATCH  <- MATCHES EVERYTHING
```

The role-7 ring matches role AND sessionUID exactly — and is never examined, because
`HomerDpuDmaImportCanRetireOwnedWork()` rejects its whole import at the TOP of the scan
(`lifecycle=3` = `HOST_DETACHED`, `retirable=0`). It is HOST_DETACHED because the client had already timed
out at 30 s and closed it. **So node A never even ATTEMPTS to resolve the ring while it is alive** — node B
sent no bytes, so node A's relay pump had no work and never ran. The whole causal chain:

1. node B never arms role-5 -> peer egress  ->  2. no bytes cross  ->  3. node A's relay pump never runs
->  4. client times out and closes role-7  ->  5. teardown finally arms the pump, which resolves against a
HOST_DETACHED import, misses, and blames the client's sessionUID.

**Lesson (a repeat of §10f's, in a new costume).** A `continue` at the TOP of a scan and a `continue` in the
MIDDLE produce the identical observable outcome — "not found" — so the miss message attributed the failure to
the last predicate its author had in mind. **A lookup that can reject at several stages must report WHICH
stage rejected.** The message sent the reader to audit the client's sessionUID, which was correct all along.

**Second lesson, and the one to actually design against:** node B's payload egress fails by *doing nothing
and logging nothing*. **An un-armed collector and a healthy idle one are indistinguishable**, so the bug is
invisible until something downstream times out — 4 hops away, with a misleading message. Silence must not be
a valid state for a bound result stream; a bound-but-never-armed stream needs a one-shot warning.

**NEXT:** wire node B's role-5 -> peer payload egress on the SERVICE_RESULT_OPEN completion path, following
the **working basebackup DPU-sender reference** (same shape: DPU-side byte-ring payload egress from a host
source ring to a peer DPU; validated in the 4-role DPU-relay basebackup topology). Add the missing
observability. Fix the node-A miss message. Then re-run run 22's spec. Artifacts: scratchpad/run22/, run23/.

### 10i. Runs 24-26 — both arms land; the result path still moves zero bytes (July 11, 2026)

**Landed and PROVEN working across these runs:**
- **Node B send arm** (`armed service-owned P3 result mirror egress ...`) — run 24.
- **Node A receive arm** (`armed peer-provisioned P3 result receive relay ... landing_ring=10485760
  role7_target=lazy`) — run 25.
- **Terminal condition on consumer departure** — run 25. `terminating DPU relay after host consumer
  detached ...`; the relay now quiesces instead of free-spinning. (Run 24 had filled a log to **323 MB**.)
- **Watchdog no longer false-alarms** on terminally-torn-down streams — run 26.
- Batch 4's geometry + arena split remain green in every run.

**Still FAILING the gate:** no `homer_last_abalance` ever decodes; the SELECT times out at 30 s. Node B
binds the peer stream and **moves zero result bytes**.

#### Root cause found for run 25's zero bytes — and it was a fix of ours that caused it
The persistent scheduler arm was made *unconditional* for BOTH P3 directions. On node B that made the
SENDER permanently "ready", so it kept winning an empty semantic payload action **before** grouped-control
discovery and the role-5 pull had produced a mirrored range — the stream **starved itself**. Fixed by
restricting persistent eligibility to remotely-initiated RECEIVE relays (which need lazy role-7 rendezvous);
the node-B sender is armed but runnable only when `HomerServicePayloadSendQueueHasReadyWork()` finds a real
mirrored range, restoring the validated basebackup order:
`grouped-control discovery -> role-5 pull -> mirrored range ready -> payload egress`.

> **Review lesson (mine).** I reviewed that always-ready predicate when it landed, worried it would starve
> *other* streams, verified the scheduler's source selection is round-robin, and passed it. The real failure
> mode was one I never considered: it starved **its own** stream by pre-empting the pipeline stages that
> would have produced its work. *Checking the hazard you thought of is not the same as checking the hazard.*

> **Red herring, recorded so nobody re-chases it.** `published_tail=0 consumed_head=0` on node B is the
> service-created fallback `sendQueue`, which selected-DPU egress **deliberately ignores** in favour of the
> DPU mirror frontier. The backend WAS publishing correctly the whole time, through the arena partition
> translator. Batch 4's restructure was innocent — I had wrongly flagged it as the prime suspect.

#### Run 26 — OPEN, and the read of it is NOT settled
Chain now: both arms fire, sender reaches `peer stream bound`. Then node A logs
`setup import host-detached bridge_generation=<sessionUID>` and
`terminating DPU relay after host consumer detached ... (matches=1 import=1 lifecycle=3
skipped_imports=1023)`, and node B sees `CM event=DISCONNECTED` and aborts.

The validator read this as the role-7 import detaching **immediately** after arming (hypothesising the
cold-path TCP setup-socket close being misread as a permanent host detach). **That read is unverified and I
doubt it.** `HomerDpuDmaDetachHostMmapForClose` (`homer_service_dpu_dma.c:8937`) is driven only by an
explicit CLOSE handshake on the setup socket (`homer_service_dpu_setup_tcp.c:726`) — i.e. the client asked
for it. The far likelier reading is that this teardown is the **normal 30 s-timeout close**, and the run
looks different from run 25 only because the terminal-failure path now works and surfaces a FAILED
completion. If so, **nothing about the zero-bytes problem changed** and the scheduling fix did not (or did
not fully) fix it.

**These two readings are distinguishable by TIMESTAMPS, which the small DPU logs do not currently carry.**
Do not spend another run guessing:
**NEXT STEP — settle the timeline first.** Either add timestamps to the DPU service log lines, or log the
service-pass counter on the arm / detach / CM-disconnect lines. Then a single run answers: does node A's
role-7 import detach at t~=0 (a real spurious-detach bug) or at t~=30 s (the client's timeout, meaning the
zero-bytes defect is still upstream and untouched)? Everything downstream of that answer is a different
investigation. Artifacts: scratchpad/run24/, run25/, run26/.

## 11. P4 — paying off the band-aids (design of record, July 11, 2026)

Owner raised the concern directly: *"are we applying band-aid solutions one at a time and spaghettifying the
code and hiding the real issue?"* Partly yes. This section separates the real fixes from the band-aids, names
the root, and states a **falsifiable** acceptance test so this cannot be quietly declared done.

### 11.1 What was a real fix vs. what was a band-aid

**Real** (would exist in any correct design; keep): byte-ring per-pair geometry + arena mmap split (§10g);
the terminal arm on consumer departure; hard-fail when a DPU-relay stream is opened with no usable engine;
the two missing arms *as behaviour*.

**Band-aids** (three, all symptoms of one root):
1. **The artificial "always-ready" scheduler predicate.** Invented to substitute for the re-arm a real owner
   would have performed. It then caused its OWN bug — node B's sender became permanently eligible and
   pre-empted the grouped-control discovery / role-5 pull stages that produce its work, starving *itself* —
   and needed a second patch to narrow it. **A band-aid on a band-aid; the load-bearing smell.**
2. **The two hand-wired arm calls** (send side, then receive side). One lifecycle hook, discovered twice,
   bolted on at two call sites instead of existing once.
3. **Order-dependent special cases.** The P3 branch in payload-failure delivery MUST precede the generic
   locally-initiated case or it becomes dead code — which it already did once, silently.

### 11.2 The root (per Codex's independent audit — and it CORRECTED an overreach of mine)

I proposed making the service-owned stream "a first-class payload owner". **That was overstated.** Payload
scheduling is already owned by the `HomerServicePayloadStreamEntry`, and `SERVICE_RESULT_OPEN` already exists
as a typed owner kind with registration, matching and dispatch (`tuple_sink_service_process.c:36358` ff).

The actual gap is narrower and **cold-path only**: the service-result async op retains **raw `sessionState` /
`streamEntry` pointers with no generation stamp** (`tuple_sink_service_process.c:36410`), and its lifecycle
transitions are hand-wired per call site rather than centralized. That is *why* each hook had to be
rediscovered by hand, and why the compensating "always-ready" hack was needed at all.

### 11.3 Plan

**Phase 0 — finish the P3 gate first. DO NOT refactor on a red test.** With a byte still not moving, a
refactor regression would be indistinguishable from the bug under investigation. This sequencing rule is not
negotiable.

**Phase 1 — the root fix.** Give the cold-path control owner a generation-stamped handle:
`{ owner kind, session index + id + generation, stream index + id + generation }`, with **centralized hooks**
for the five transitions currently hand-wired: OPEN completion, sender/receiver arm, terminal failure,
detach/close cancellation, stale-owner validation. **Hot path keeps direct `streamEntry` ownership — no
virtual dispatch, no indirection on the data path** (Codex was explicit; agreed).

> **ACCEPTANCE TEST FOR PHASE 1 IS SUBTRACTIVE.** The refactor succeeds only if the three band-aids in §11.1
> are **DELETED**: the always-ready predicate, both hand-wired arms, and the order-dependent failure branch.
> **If it lands and those survive, it did not fix the root — it added a layer.** Falsifiable on purpose.

**Phase 2 — convert the "no terminal arm" class into a scheduler invariant.** Hit 4 times (payload-failure
livelock at 3.1M repeats; the 323 MB free-spin; the EOS gate; the consumer-departed relay), with ~25 more
sites inventoried. Fixing them one by one IS the band-aid pattern. Structural fix: progress actions return a
tri-state (`progressed` / `blocked-retryable` / `terminal`), and the **scheduler** enforces that anything
re-armed N times without progress escalates to terminal. One invariant instead of 25 patches.

**Phase 3 — liveness as a first-class assertion.** Every bug in this arc was invisible because *doing nothing*
is indistinguishable from *idling healthily* (see §10h). The un-armed watchdog was an accidental first
instance of the right idea. Generalize: **a bound stream that is neither progressing nor terminal is a bug and
must say so itself.**

**Open questions for the owner:** (a) should Phase 2 land BEFORE the remaining P3 hops? It is insurance
against exactly the failure mode that has been costing runs. (b) Fix all ~25 inventoried sites, or only those
a service-owned stream can actually reach?

### 10j. Runs 27-38 — the result DATA PLANE is DONE; the last hop is the role-7 terminal (July 11, 2026)

**METHOD CHANGE THAT MADE THE DIFFERENCE.** After runs 20-26 produced five patches (one of which *caused*
the next bug), we STOPPED PATCHING and INSTRUMENTED. From run 27 on: **zero speculative fixes**; every fix
landed on a cause visible in a log line. Probes added: 6 frontier probes (backend publish -> DPU accept ->
discover -> pull -> mirrored-range -> payload post), a raw send-CQE observer, address probes, 3-latch pump
probes, the EOS chain, `role7-write`, `role7-eos`, and **timestamped DPU logs via an `awk strftime` pipe at
launch (zero code change)**.

#### PROVEN WORKING, hop by hop (each a log line, not an inference)
| hop | evidence |
|---|---|
| backend publishes result bytes + EOS into arena role-5 | `eos-1/4 ... eos_byte_tail=112` |
| node B accepts, discovers, pulls into DPU mirror | `4/5 mirror-pull-completed ... completed=112` |
| node B posts RDMA payload+tail to node A | `6/6 payload-posted remote_tail=112` |
| the NIC confirms the write | `payload-send-cqe status=0` |
| node B SEES the EOS (**fixed: bridge ABI v5**) | `eos-2/4 nodeB-saw-eos`, `eos-3/4 nodeB-posted-eos` |
| the completion gate RELEASES (held >1M checks before) | `eos-4/4 gate_decision=released` |
| terminal completion egresses in **2 s** (was 28 s) | `zero command sequence` count = **0** |
| node A resolves role-7 and DMA-writes the tuples | `role7-write attempted=1 reason="submitted" bytes=104` |
| node A submits the role-7 TERMINAL | `role7-eos check=1 terminal_submit_ok=1 submitted_tasks=1` |

#### THE REMAINING HOP (run 38, OPEN)
The client (`HomerClientPollSqlResultDpuReceive`, `homer_client.c:4962`) needs THREE things: a published
role-7 credit epoch, all produced bytes drained, AND **`entryState == CLOSED|FAILED`**. Node A now SUBMITS
that terminal (`terminal_submit_ok=1`) — but **nothing ever confirms the terminal DMA COMPLETED**, and the
client still hangs in its drain (no longer a 30 s completion timeout — an indefinite tuple-drain hang, which
is itself the proof the completion now arrives). **NEXT: instrument the terminal DMA's completion/CQE on node
A. Is it posted-but-never-retired (cf. the send-owner claim/release pattern), or completed-but-not-observed?**

#### Run-38 re-read (July 11, 2026) — the terminal chain RETIRED; the open question moved to WHERE the client blocks

Re-reading run 38's existing logs (no new run needed) narrowed "posted-but-never-retired vs
completed-but-not-observed" substantially, and killed one hypothesis before it became a patch:

1. **The terminal chain RETIRED.** `role7-write check=5` (same second as the terminal submit) shows
   `write_in_flight=1`; `check=1000000` shows `write_in_flight=0`. The only code that clears
   `byteRingWriteInFlight` is the PRODUCED_TAIL_PUBLISH retire — success or failure — at
   `homer_service_dpu_dma.c:10220`, or a failed-body retire (`:10182`, `:10203`).
2. **It almost certainly retired SUCCESS.** Every failed DMA completion prints
   `homer DPU DMA: memcpy task failed` (`homer_service_dpu_dma.c:13923`); run 38's node A log has zero
   such lines. So the CLOSED credit line + gate word were most likely DMA'd into host memory.
3. **The address-mismatch hypothesis is DEAD (checked statically, not patched).** The client advertises
   `hostRingAddress = dpuExportBuffer` (export BASE) for both SQL-result descriptors
   (`homer_client.c:3904`, `:3931`); the engine writes the credit line to
   `hostRingAddress + dpuCreditOffset + ringIndex*64` (`homer_service_dpu_dma.c:11840-11850`); the client
   polls `dpuExportBuffer + dpuCreditOffset + ringIndex*64` (`homer_client.c:3836` region). Congruent.
4. **"The client hangs in its drain" was an INHERITED, UNPROVEN framing** (my own, from before the
   context compaction — the verify-claims rule applies to my own summaries too). The pgbench log's last
   line is `sending Homer SQL text SELECT ...` with no completed line — but
   `HomerDrainPendingDpuResultRelay` (`pgbench.c:3794`) is reached only FROM
   `HomerApplyCommandCompletion` (`pgbench.c:3942`), and the "completed" debug line prints only after a
   FULLY applied completion. A completion that arrives but loops forever in `NOT_APPLIED_RETRY`
   (bounded 128-spin drain, `pgbench.c:3858`) is log-indistinguishable from one that never arrived.
   The run-38 UPDATE's completion DID arrive and apply, so the role-6 path works for plain completions;
   the SELECT's differs in carrying TUPLE_SINK_READY + the drain requirement.

So the block point is a genuine three-way ambiguity: (a) the SELECT's terminal completion never
delivered node A → client (role-6 frontend completion event), (b) delivered but the role-7 drain never
observes data/EOS/CLOSED, or (c) the drain observes data but neither `sawEos` nor `streamComplete` ever
turns true (gate legs, `homer_client.c:4969-4976`). Run 39 instruments the WHOLE chain, numbered — the
first silent frontier is the bug:

| # | frontier | site |
|---|---|---|
| A1 | body CQE fired, next task armed (terminal vs data named by cell entryState) | `homer_service_dpu_dma.c` arming fn ~:9793 |
| A2 | tail-body / tail-publish retire, success AND failure, with recomputed dst host addr | `HomerDpuDmaRetireTaskSlotInternal` :10189/:10210 |
| A3 | role-6 frontend completion event submit + retire | `FRONTEND_COMPLETION_EVENT_*` sites |
| C1 | pgbench applied a completion (BEFORE the drain call — placement load-bearing) | `HomerApplyCommandCompletion` pgbench.c:3942 |
| C2 | drain entered / spins exhausted (the currently-silent negative case) | `HomerDrainPendingDpuResultRelay` pgbench.c:3794/:3858 |
| C3 | client's role-7 poll observation: epoch/entryState/tails + the 3 gate legs SEPARATELY, change-latched | `HomerClientPollSqlResultDpuReceive` homer_client.c:4892-4977 |

A2's `dst_host_credit_addr_recomputed` vs C3's `credit_line_addr` gives a direct written-vs-polled
address comparison in one run (the provenance rule, applied to both ends). Ride-along for §10k: a
service-pass counter on the node-B frontier lines, plus millisecond timestamps in the launch wrapper.

#### ROOT-CAUSE FAMILY (this is the real finding — NOT the band-aids)
**P3 introduced a stream shape the codebase did not have: persistent, service-owned, multi-command.** Every
mechanism built for the older shape silently did NOTHING rather than failing:
1. **slot vocabulary** on byte rings (purged, batch 4 — owner's call, and it was right);
2. **`sendQueue.queueControl`** read on a byte ring — NULL by construction, so the EOS `if` never fired;
3. **the DPU's control copy** carried `publishedTail` but not the later-added EOS fields (fixed: ABI v5);
4. **command identity** stamped on the *selected* session, not the *owning* one — a LEGACY completion-mailbox
   consumer set it 28 s late (fixed);
5. **the role-7 terminal** published only on PEER CLOSE — but P3's peer stream is PERSISTENT, so a
   per-command EOS never closes the binding (fix submitted; completion unconfirmed).
Every one was invisible because **nothing errored**: a null pointer that made an `if` never fire; a control
copy that read zero forever; a legacy path stamping a field far too late.

#### SEVEN FALSE READINGS — all mine, none of them code bugs
Six were **a number printed without naming the structure it came from** (`published_tail=0` from the wrong
struct; `relay target unresolved` from a check that never ran; `remained unarmed` from a flag cleared by
teardown; the fallback `sendQueue`'s tails; an "off-by-0x10 addressing bug" that was a FIELD address compared
against a STRUCT BASE — I nearly "fixed" a correct address path). One was **a one-shot probe mistaking "not
yet" for "never"** (`role7_resolved=0` was a pre-resolve snapshot; run 32's conclusion was wrong).

**PROBE RULES, now mandatory (P4 Phase 3):**
- **Name what you measured**, not just the value. A number without provenance is a trap.
- **Never one-shot.** Sample repeatedly or latch on the STATE TRANSITION — a one-shot cannot distinguish
  "not yet" from "never".
- **Always bounded.** Two disk incidents (323 MB, 555 MB) came from my own probes; a third (286 MB) from an
  unbounded retry log. A diagnostic that can fill a disk is a bug.

### 10k. BLOCKER — the ~2 s stall between discovered-enqueued and mirror-pull-submitted (July 11, 2026)

**Raised by the owner, and correctly.** I reported the terminal-completion egress improving from **28 s to
2 s** as a win. **It is not.** On a busy-polling DPU pair over a 400G fabric the target for this path is
MICROSECONDS. Comparing against the bug instead of against the target is how a performance defect ships.
**No performance claim about P3 may be made until this is understood.** (Rule now in the global CLAUDE.md:
*judge latency against the target, not against the bug*.)

**First: my instrument cannot see it.** The DPU log timestamps come from an `awk strftime("%H:%M:%S")` pipe —
**whole seconds**. So "2 s" means somewhere in [1.01, 2.99] s and cannot be refined. **Fix the instrument
before diagnosing:** sub-second timestamps (gawk `%H:%M:%.S`, or stamp in-process), plus the service-pass
counter on these lines.

**But the logs already localize the stall.** Node B, run 33 (identical shape in runs 32/35/37):
```
18:50:54 [p3trace] 2/5 dpu-accepted        tail=112
18:50:54 [p3trace] 3/5 discovered-enqueued tail=112 completed=0
18:50:56 [p3trace] 4/5 mirror-pull-submitted            <- ~2 s later
18:50:56 [p3trace] 4/5 mirror-pull-completed
18:50:56 [p3trace] 6/6 payload-posted                   <- everything after is same-second
```
The work is **queued and then sits there**. Once the pull is granted, pull -> mirror -> RDMA post -> CQE all
complete within the same second. **The entire latency is the wait for the mirror-pull collector's grant.**

**It is VARIABLE:** in run 30 all of those lines landed in the SAME second. Sometimes fast, sometimes ~2 s.

**Leading hypothesis (UNPROVEN — measure, do not assume):** the pull collector is armed on a **coarse
periodic cadence** rather than by the **exact demand fact** (work was enqueued). The codebase already does
this elsewhere by design — CLAUDE.md documents the setup listener granted once per 512 passes and the
doorbell once per 4096 when attached. If so, **this is not a separate perf problem: it is the same
arming/scheduling class that produced five correctness bugs in this arc, wearing a stopwatch.**

**NEXT (blocking, before any P3 perf number):**
1. Sub-second timestamps + service-pass counters on the p3trace lines.
2. Identify which grant/arming rule the mirror-pull collector waits on, and whether it is demand-armed
   (exact count) or interval-armed. Compare with `HomerMachineBaselineCompileExecutionPlan`'s hand-enumerated
   phase body — a collector armed on every pass but granted on a coarse interval is exactly this signature.
3. Only then quote any latency for the cross-node result path.

### 10l. RUN 40 — P3 IS GREEN. The root cause was ONE RULE, violated in both directions (July 11, 2026)

**THE GATE PASSED.** `pgbench --homer --homer-dpu-command -t 5` from farnet0, through the farnet0 DPU ->
DPU-DPU RDMA -> farnet1 DPU -> DPU-spawned socketless backend on farnet1:

```
number of transactions actually processed: 5/5
number of failed transactions: 0 (0.000%)
client 0 Homer DPU result relay for sql_execute reached EOS: rows=1 abalance=3524
   ... x5, five distinct decoded values, exit code 0
```

**Intended-path proof (anti-fallback):** the farnet0 HOST service log contains exactly ONE line — its
startup policy banner — and zero session/command/payload activity. The farnet1 host service was DOWN.
So the whole workload rode the DPU relay; nothing silently fell back.

#### THE PERSISTENT-STREAM IDENTITY RULE (the actual root cause)

P3's role-7 stream is **persistent and multi-command**: one physical byte ring carries every command of a
client SQL session, and the peer binding does NOT close between commands. On such a stream, state splits
in two, and **every component must split it the same way**:

| | resets at each command boundary | absolute, monotonic, NEVER rewound |
|---|---|---|
| **what** | record identity: `recordOrdinal` (-> 0), `generationSequence` (-> 1), EOS latch, semantic frontier | the BYTE frontiers (published / consumed / posted tails) |
| **who says so** | `ResetCitusTupleSinkSendHandle` (homer_tuple_queue_frontend.c:1219), called once per row-producing command with `resetSequence=true` (remote_execution_backend_bridge.c:1399) | that same function's comment: *"resetting the shared byte-ring frontiers here would make old and new command records indistinguishable to a frontend that is still catching up"* |

**The producer already stated and obeyed this rule. Nobody else did.** Every run-39/40 bug is one component
mixing the two halves — and they point in OPPOSITE directions, which is why they never looked related:

| # | component | mistook | symptom |
|---|---|---|---|
| 1 | client validator (`HomerClientSqlResultDeliverDecoded`) | per-COMMAND field as per-STREAM: demanded `generationSequence == 0` forever (the BASEBACKUP convention — copied from the wrong sibling) | every well-formed tuple record silently rejected; drain hung with `consumedHead` pinned at 0 forever |
| 2 | service (`HomerServicePrepareTupleResultStreamForCommand`) | per-STREAM field as per-COMMAND: seeded the source byte frontier from the completion descriptor | rewound a LIVE frontier (`posted_source` 112 -> 0), tripping the mirror's `range_start == posted_source` invariant one pass later -> binding aborted |
| 3 | client `receiveExpectedOrdinal` | per-COMMAND field as per-STREAM: init once, `++` forever | **LATENT** — would have hit on the very next run (producer restarts ordinals per command). Found by *asking the code what the producer does* instead of running and waiting. |

The fix is now **one named rule in one place**: `THE PERSISTENT-STREAM IDENTITY RULE` in `homer_client.c`
(with `HomerClientSqlResultBeginCommandIdentity` / `...AdvanceCommandIdentity`), restated on the service
side at the prepare's reset call. `HomerClientBaseBackupStream` backs BOTH families but basebackup is
single-command and must NOT reset — that asymmetry is now documented at the struct field.

#### The DPU-local `sendQueue` mapping: dead storage, live gates (sweep result)

The command-plane bug (#2's sibling) came from a guard reading `sendQueue.byteRingControl->publishedTail`.
On the DPU-mirror path that mapping is `shm_open`+`mmap` of a **DPU-LOCAL** object
(tuple_sink_service_process.c:23242) while the real producer is a postgres backend **on another machine**
publishing into the host arena. Empirically its `publishedTail` is pinned at 0 (the guard rejected every
command after the first, then retried forever).

An independent read-only sweep (Codex) of every `stream.sendQueue.*` access — all of them live in
`tuple_sink_service_process.c`; no other file in either tree touches these fields — returned:

- **The data region is DEAD on this path.** No reachable P3 code reads or writes `sendQueue.byteStorage`.
  The real payload source is `mirroredRange.localAddress` (`HomerDpuDmaFillMirroredByteRange`,
  homer_service_dpu_dma.c:4701), consumed by `HomerServicePumpOutgoingDpuMirrorByteRingPayload`
  (tuple_sink_service_process.c:30959). The truthful frontier is the engine's `acceptedPublishedTail`
  (homer_service_dpu_dma.c:12222).
- **CORRECTION to my framing:** the control block is *not* literally never-written. The service itself
  initializes `protocolVersion`/`flags`/`ringBytes` and later writes local flags, terminal status, and
  sometimes `consumedHead`. What is **producer-stale** is specifically **`publishedTail`** — the one word
  the broken guards read.
- **No other live BROKEN-ON-MIRROR frontier reader remains**; the other ~8 mirror-reachable readers are
  SAFE-BY-GUARD (they consult the mirrored-range API first) or SAFE-BY-SEMANTICS (diagnostics, local
  flags, teardown bookkeeping).
- **Deleting the mapping is NOT a one-liner.** Six live dependencies treat it as required bookkeeping:
  queue-descriptor construction (`queueShmName`, `byteRingBytes`, :36650/:36671), async stream-open
  validation (:38111), open-completion geometry (:38220), peer-open `requestedByteRingBytes` (:39288),
  the prepare's NULL-control reject (:25918), and local failure routing's "has send control" test (:26502).

**Decision (recorded, per the standing directive):** the broken guard is now **skipped on the mirror path**
rather than rewired to consult the engine. Rationale: a guard whose input is structurally zero is a decoy,
and there is no cheap local truth to swap in (the engine's snapshot lags by design, so any local bound
built from it would false-reject a legitimately-ahead start tail). Divergence is still caught loudly one
hop later by the mirror's own `range_start == payloadSourceBytePostedTail` invariant — which compares two
frontiers that are BOTH real, and is exactly the check that surfaced this bug.

**P4 task (scoped by the sweep):** remove the DPU-local sendQueue mapping on the mirror path, after
replacing those six dependencies. Guiding principle: **a zeroed struct lies; a NULL pointer confesses.**
If the mapping cannot be deleted outright, null the control pointer on the mirror path so any future
reader faults loudly instead of silently reading zeros.

#### Stage-5 ABI cleanup — DECIDED: defer, but do it FIRST in P4 (owner-raised)

The owner asked why basebackup and tuple results disagree on `generationSequence` at all. They should not:

- `CitusTupleSinkBatchHeader` **already carries `sinkSequence`** (homer_tuple_abi.h:192), the producer
  already stamps it (homer_tuple_queue_frontend.c:1494), and three validators already check it. The
  transport envelope's `generationSequence` is a **redundant duplicate** for the tuple family.
- `homer_tuple_abi.h:242-248` says so itself: *"Tuple-result DATA/EOS records **still** use
  generationSequence as their command-local sequence **until the Stage 5 EOS cleanup**. Other object
  families must validate their semantic sequence in their own payload header, not in this common transport
  envelope."* Basebackup already finished that migration; tuple-view did not.
- **This duplication is what let two consumers of the SAME object family disagree** (bug #1). The
  "consistency with the shm path" argument for keeping it is worthless — the shm path is one we are
  retiring.

**Why defer anyway:** the rule fix is **invariant** under this choice. Stage 5 changes only *which field*
the rule reads (`decodedBatch->sinkSequence` instead of `header->generationSequence`) — nothing is thrown
away — and it does NOT fix bug #3 (the ordinal), which needs the rule regardless. Doing an ABI change now
would mean debugging it with no green multi-command baseline to regress against. We now HAVE that baseline.

**Stage-5 shape (P4 item 0):** `HomerDecodedTupleBatchHeader` has a free `reserved0`
(homer_decoded_tuple_abi.h:74) — so the sequence moves there with **no struct growth**. Node A stamps it,
the client validates it there, the envelope's `generationSequence` becomes uniformly 0 across families, and
the envelope-level checks in the shm sink and the service are **deleted**. Behavioral wire change ->
version bump + all four machines rebuilt (stale = loud `BAD_PROTOCOL`, which is the failure mode we want).

#### Open, NOT fixed by run 40

1. **§10k's grant stall remains the perf BLOCKER.** Run 40's `latency average = 10.791 ms` is NOT a
   result — it is a diag-instrumented build with `HOMER_DPU_P2_DIAG` on both DPUs and in pgbench. **No P3
   performance claim may be made** until the probes are stripped AND the discovered->pull grant stall is
   understood. The millisecond timestamps and the `pass=` service-pass counter are now in place to measure it.
2. **Teardown routes a NORMAL disconnect through the FAILURE machinery.** At end of run the client's
   `CM event=DISCONNECTED` produces `payload stream marked ABORTING ... reason=1`, `marked send byte-ring
   failed ... reason=peer-reset-abort`, and `committed service-owned DPU result failure`. Functionally
   harmless (it happens after the last completion; the run exits 0) but it means a clean close and a real
   peer failure are indistinguishable in the logs. Fold into the P2.T teardown work.

## 12. PLAN OF RECORD after run 40 — sequencing forced by a leak, not by preference (July 11, 2026)

### 12.0 The run-40 postscript: the SESSION CLOSE is broken, and it leaks

Run 40's DATA path is green (5/5, §10l). Its **lifecycle close is not**, and the owner was right to
distrust the "harmless teardown" reading. Two error lines sit ABOVE pgbench's summary block (I tailed the
summary and missed them — the same class of mistake as trusting an exit code):

```
error: could not unbind Homer DPU SQL result receive ring:
       close selected-DPU basebackup stream failed: close request referenced an unknown compatibility session id
error: could not close selected-DPU Homer SQL session:
       close selected-DPU client SQL session failed: direct client SQL command mailbox is still busy during close
```

**The causal chain, end to end:**
1. all 5 transactions complete correctly (rc=0, so the failure is INVISIBLE to the exit code);
2. the role-7 unbind fails — the DPU does not recognize the close request's session id;
3. the SQL session close fails — the client's command mailbox is still busy;
4. therefore **`CLIENT_SQL_SESSION_CLOSE` never lands on node B** (its last landed command is sequence 37,
   an ordinary one);
5. therefore node B never observes a session-terminal completion, so **the P2.T teardown handshake
   (T1 quiesce -> T2 drain -> T3 RELEASE -> T4 unbind) NEVER FIRES** — zero such lines in the log;
6. therefore the backend never receives `BACKEND_SLOT_RELEASE`: **it survives, busy-polling, forever**
   (confirmed: `pid 2098287, postgres: remote exec backend` alive long after the run exited), and its
   **arena slot stays BOUND** (zero `BACKEND_SLOT_RELEASE` lines in postgres.log);
7. only then does the RDMA connection drop, and node B — with a still-live binding — routes the ordinary
   `CM event=DISCONNECTED` through the FAILURE machinery: `marked ABORTING`, `marked send byte-ring failed
   reason=peer-reset-abort`, `committed service-owned DPU result failure`.

**Why this is a blocker and not a cosmetic wart:** a leaked backend **busy-polls a pinned CPU**. Any
warmed repeat run — which is exactly what a performance measurement requires — would run alongside one
leaked spinner per prior session, on the same CPU set. **The leak contaminates the very measurement the
§10k stall investigation needs.** It also burns one of 16 arena slots per session, so every run today
requires a full hard clean baseline.

### 12.1 SEQUENCING — and why it is forced

The stall (§10k) cannot be measured cleanly until the leak is fixed, and the cleanup rewrites the code the
measurement would be taken on. So the order is **not** a preference:

| # | phase | why it must be here |
|---|---|---|
| **P5** | **session lifecycle close + teardown** | Without it we cannot take a clean measurement at all (leaked busy-poller on the measured CPUs), and each run needs a hard reset. Also a real correctness leak. |
| **P4.0** | Stage-5 ABI cleanup | Deletes a duplicated field. Cheap, and we now have the green baseline to regress it against. |
| **P4.1** | mirror-path truth-source: source-kind + accessors; **stop mapping** the producer shm on the mirror path | The structural fix for the bug family behind runs 39-40. Behavior-preserving. **TRANSITIONAL, not the end state — see P4.2.** |
| **P4.2a** | **delete the already-dead producer-byte-ring code** | No dependency — deletable today (see §12.5). |
| **§10k** | strip diag, measure, fix the grant stall | Measure ONCE, on the code the DPU path actually executes (final after P4.1). |
| **P4.2b** | **COMPLETE REMOVAL of the producer-byte-ring mapping** | Gated on retiring the host-service local-byte-ring source (an existing owner directive), NOT on anything technical. It deletes code the DPU path never executes, so it lands after the measurement, followed by a cheap re-verify that the numbers did not move. |

**OWNER CORRECTION, and it is right: "leave the pointer NULL" is NOT an acceptable END STATE.** Stopping at
P4.1 would swap a lying struct for a half-dead field — precisely the species of vestige this entire arc has
been paying for. The end state is that the mapping, its mapper, its shm object, and the `queueShmName`
geometry plumbing **cease to exist**. P4.1 is only the safe intermediate step that makes the lie unreadable
while the legacy source still legitimately needs the field.

The one thing that would REORDER this: if the stall's cause turns out to BE a mirror-path truth-source lie
(an arming predicate reading a structurally-zero word). Current evidence says no — the stall sits between
grouped-control DISCOVERY and the PULL grant, inside the DMA engine's collector arming, which the sweep
found clean. But that is a hypothesis; if a probe implicates it, P4.1 becomes the stall fix and they merge.

### 12.2 P5 — session lifecycle close (DO THIS FIRST)

Two independent defects, both first reachable only now that commands actually flow to completion:

- **P5.a — role-7 unbind: "close request referenced an unknown compatibility session id."** The SQL result
  ring is unbound through the BASEBACKUP close path (`HomerClientUnbindRing` -> `HomerClientCloseBaseBackupStream`).
  The identity the close sends does not match what the DPU registered at open. Note the smell: the SQL
  family reusing the basebackup close path is the same "copied from the wrong sibling" pattern that caused
  the generationSequence bug. Diagnose by naming BOTH ids at the reject site (which id was sent, which ids
  are live) — an ANDed/lookup guard must say WHICH stage rejected.
- **P5.b — SQL session close: "direct client SQL command mailbox is still busy during close."** A command
  slot is still leased/unacked when the close runs. Suspect the terminal completion's lease is never
  released on the DPU-relay path (the shm path releases it in the drain). Probe the mailbox occupancy and
  the outstanding lease at close time.
- **P5.c — a clean close must NOT route through the failure machinery.** Once P5.a/b land and
  `CLIENT_SQL_SESSION_CLOSE` reaches node B, a subsequent `DISCONNECTED` must find the binding already
  torn down. Add an explicit assertion/branch so that a disconnect on a stream with no live binding is a
  no-op, and a disconnect WITH a live binding stays loud. **A clean close and a real peer failure must
  never again be indistinguishable in the logs.**

**Acceptance (falsifiable — the leak must be gone, not merely quieter):**
1. `teardown T1 quiesced` .. `T4 released and destroyed` present, in order, for the session;
2. `received BACKEND_SLOT_RELEASE for arena slot N` in farnet1 postgres.log;
3. **NO surviving `postgres: remote exec backend`** after the run (checked by `/proc/<pid>/exe`);
4. **TWO consecutive pgbench runs against the SAME postmaster and the SAME services both pass, with no
   clean baseline in between** — this is the real test, and today it is impossible;
5. ZERO `marked ABORTING` / `peer-reset-abort` / `committed ... failure` lines on a clean close;
6. pgbench exits 0 **with an empty error stream** (rc=0 is NOT the verdict — run 40 exited 0 with two
   errors; read the whole log).

### 12.3 P4.1 — mirror-path truth-source cleanup (design, guided by the sweep)

The sweep (§10l) established: on the DPU-mirror path the DPU-local `sendQueue` mapping's **data region is
dead** (the real source is the engine's mirror), its `publishedTail` is **producer-stale by construction**,
and **six live sites** still gate on the mapping existing. So the cleanup is not "delete the mmap" — it is
**make the lie structurally unreadable**, then delete.

**Design: give the payload SOURCE an explicit kind + accessors, and stop reading a mapped struct.**

```c
typedef enum {
    HOMER_PAYLOAD_SOURCE_LOCAL_BYTE_RING,  /* shm ring this service can read directly (host service) */
    HOMER_PAYLOAD_SOURCE_DPU_MIRROR,       /* host ring reachable ONLY through the DMA engine */
    HOMER_PAYLOAD_SOURCE_FIXED_SLOT,       /* legacy slot ring */
} HomerPayloadSourceKind;
```

- **Geometry moves out of the mapping.** The six dependencies (`queueShmName`/`byteRingBytes` at :36650/:36671,
  async-open validation :38111, open-completion `ringBytes` :38220, peer-open `requestedByteRingBytes` :39288,
  the prepare's NULL-control reject :25918, failure routing's "has send control" :26502) all want
  **geometry or existence**, never a producer frontier. Store geometry in the stream entry (it already
  carries peer ring descriptors) and replace the NULL-pointer existence tests with
  `HomerServicePayloadSourceIsBound(streamEntry)`.
- **One accessor for the frontier**, and it is allowed to say "not locally knowable":
  `HomerServicePayloadSourcePublishedTail(streamEntry, uint64_t *out)` returns false on the mirror path.
  A caller that cannot handle false must not ask — which is exactly the guard we deleted in §10l.
- **Then stop mapping the shm at all on the mirror path, and leave `byteRingControl == NULL`.**

**The principle, stated once:** *a zeroed struct lies; a NULL pointer confesses.* Today a mirror-path reader
of `publishedTail` gets a plausible `0` and silently does the wrong thing. After this, it faults. That is
the whole point — this bug family (5+ instances now) exists because **silence was a valid state**.

**Acceptance:** the three workloads stay green, AND `grep` shows zero reads of
`sendQueue.byteRingControl->publishedTail` outside a `SOURCE_LOCAL_BYTE_RING` branch, AND the mirror path
maps no producer shm.

### 12.4 P4.0 — Stage-5 ABI cleanup (shape settled in §10l)

Move the tuple command-local sequence into `HomerDecodedTupleBatchHeader.reserved0` (free — **no struct
growth**), make the transport envelope's `generationSequence` uniformly 0 across object families, and
**delete** the envelope-level checks in the shm result sink and the service. Version bump; all four
machines rebuilt (stale = loud `BAD_PROTOCOL`). The identity RULE (§10l) does not change — only the field
it reads.

### 12.5 P4.2 — COMPLETE REMOVAL of the producer byte-ring mapping (the end state)

P4.1 stops the mirror path from MAPPING the producer shm, so its `byteRingControl` is NULL there and any
future reader faults instead of silently reading a plausible `0`. **That is a waypoint, not the destination.**
The destination is that none of it exists. The removal splits by DEPENDENCY, and the split is sharp:

#### P4.2a — already dead; delete NOW, no dependency

`HomerServiceTupleViewEosAppendReady` (tuple_sink_service_process.c:13320) **returns `false`
unconditionally** — *"Slice 4d makes EOS backend-owned and immutable. The service must no longer synthesize
semantic terminal records because doing so creates a second delivery path that can race with the backend's
explicit EOS record."* So both of its callers are dead code for **every** stream in **every** configuration:

- `HomerServiceTupleViewNextEosSequenceFromProducerBytes` (:29941, :29979)
- `HomerServiceAppendTupleViewEosRecordToProducerByteRing` (:30053-:30122)
- and the gate itself (`HomerServiceAppendTupleViewEosForStream` exits on the predicate at :30141)

These are among the **heaviest consumers of `sendQueue.byteStorage` and its control words** — so deleting
them shrinks the P4.2b surface before we get there. Pure vestige; zero risk; no owner decision needed.

#### P4.2b — the mapping itself; gated on the host-service retirement

The producer byte-ring mapping is still LEGITIMATELY used when a stream is **not** mirror-sourced.
`HomerServicePayloadStreamUsesDpuMirrorSource` (:6832) excludes: tuple-view streams that are not flagged
`dpuRelayResultStream` (**Citus COPY**, non-DPU pgbench), and everything when the DMA scheduler is off (the
**host service**). So full removal of the mapping **IS** the host-service local-byte-ring retirement —
already a standing owner directive ("host-service legacy code paths are being retired soon; don't extend
them"). It is gated on that decision, not on anything technical.

What disappears when that lands:
- `HomerServiceMapProducerByteRingQueue` (:23242) — the `shm_open` + `mmap` + geometry validation
- the `sendQueue.byteRingControl` / `.byteStorage` / `.byteRingBytes` members and every NULL-test on them
- the `queueShmName` plumbing that carries the shm name into queue descriptors (:36650, :36671) and into
  peer-open's `requestedByteRingBytes` (:39288)
- `HomerServicePumpOutgoingByteRingPayload` (:30204+), the local-blackhole pump (:32239+), and the
  fixed-slot pump (:31663+)
- the six existence/geometry gates P4.1 will have already routed through accessors — those accessors then
  collapse to the mirror case and can be inlined away

**Acceptance for the END STATE (falsifiable):** `grep -r 'sendQueue\.byteRingControl\|byteStorage\|queueShmName'`
over the service returns **nothing**, no `/dev/shm/citus_res_*` object is created by any DPU-path run, and
the three workloads stay green.

**Why P4.2b lands AFTER the §10k measurement:** it deletes code the DPU path never executes, so it cannot
be the stall, and blocking a perf number on a legacy-retirement decision would be backwards. It does thin
shared dispatchers, so a cheap re-verify after it confirms the numbers did not move.

---

## 13. P5 runs 41a-41i — the LEAK IS FIXED; a slot-REUSE race is now the blocker (July 11, 2026)

### 13.1 Status against the §12.2 acceptance bar

| # | acceptance criterion | status |
|---|---|---|
| 1 | `teardown T1..T4` present, in order | **PASS** (`T3 staged RELEASE ... sequence=39`) |
| 2 | `received BACKEND_SLOT_RELEASE for arena slot N` in postgres.log | **PASS** (slot 0) |
| 3 | NO surviving `postgres: remote exec backend` | **PASS — THE LEAK IS GONE** |
| 4 | TWO consecutive runs on the SAME postmaster/services | **FAIL** — run 2 dies (§13.3) |
| 5 | ZERO `ABORTING`/`peer-reset-abort`/`committed ... failure` on a clean close | **FAIL** — 3 lines (P5.c, §13.4) |
| 6 | pgbench exits 0 **with an empty error stream** | **PASS** |

Run 41e/41h (the FIRST run on a fresh stack) is fully green: 5/5 transactions, empty error stream, and
five DISTINCT decoded rows (`reached EOS: rows=1 abalance=5009 / -2132 / 10524 / 3648 / -4056`).

### 13.2 What was actually wrong (both fixed, both were the same species)

**P5.a — the client FABRICATED a service session id it did not own.** `HomerClientCloseBaseBackupStream`
(homer_client.c:5638) already guards the control-plane close with
`serviceSessionId != 0 && serviceSinkId != 0` — a guard that exists precisely to skip the close for a
stream the service never registered. But `HomerClientOpenSqlResultReceiveStreamSelectedDpu` seeded those
two fields from its OWN bridge generation, while the service answers this rendezvous open with a benign OK
and **no session state at all** (tuple_sink_service_process.c, the `clientSqlResultOpen &&
clientSqlResultDpuRelay && RECEIVE` branch returns `serviceSessionId/SinkId = 0`). The fabricated ids
defeated the guard; the unbind closed a session that never existed. FIX: the descriptor ids are now
BRIDGE-local (`bridgeDescriptorSessionId/SinkId`, byte-identical values — the DPU keys its mirror cache on
`descriptor->serviceSinkId`), and `stream->serviceSessionId/SinkId` stay 0 unless the SERVICE hands ids
back.

**P5.b(1) — the selected-DPU close NEVER SENT `CLIENT_SQL_SESSION_CLOSE`.** The shm path sends it
(homer_client.c:1693) and the service's close handler explicitly documents that the frontend does
("The frontend sends CLIENT_SQL_SESSION_CLOSE over the direct command mailbox and waits for backend
completion before calling this lifecycle close"). `HomerClientCloseSqlSessionSelectedDpu` just skipped it
and went straight to the control-plane close, so the backend was never told to exit. FIX: the whole close
order is now an INVARIANT OF THE LIBRARY, not of the caller — semantic close → role-7 unbind → lifecycle
close → setup close. pgbench no longer unbinds the ring itself (it used to do so BEFORE the session close,
i.e. in the wrong order, and a caller can no longer express that).

> ⚠ FIRST ATTEMPT FAILED for an instructive reason: I copied the shm path's *precondition*
> (`commandMailbox != NULL && (backendCompletionMailbox || completionMailbox)`) along with its call. A
> selected-DPU session has **no direct mailbox** — it submits every command through the role-1 control slot
> — so the guard was false and the close was silently skipped again (run 41a).
> `HomerClientStartCommandWithCompletionFlags` already dispatches BOTH channels and needs no precondition.

**P5.b(2) — `TupleSinkServiceCommandMailboxBusy` read a STRUCTURALLY-ZERO word.** The probe (added instead
of a third guess) printed it outright:

```
branch=remote_sender published=0 != retired=37 (accepted=37 consumed=37 outstanding_writes=0)
```

`mailbox->publishedEpoch` is written only by a frontend that publishes DIRECTLY into a shared mailbox — the
shm client. The selected-DPU client has no mailbox, so **nobody ever writes that word and it reads 0
forever**, against a live retired frontier of 37. A perfectly idle session was "busy" permanently, so the
lifecycle close was refused and `BACKEND_SLOT_RELEASE` never went out. **This, not a completion lease, was
the leak.** (The KB's earlier P5.b hypothesis — "the terminal completion's lease is never released" — was
WRONG. Codex refuted it by reading; only the probe found the truth.) FIX: the submission frontier is now
`max(publishedEpoch, acceptedEpoch)` — exact, since sequences start at 1 so a never-written 0 can never
mask a real submission, and on the shm path `published >= accepted` always holds so it degenerates to the
old predicate. P4.1 should replace this with an explicit command-SOURCE kind so the unwritable word cannot
be read at all.

### 13.3 THE BLOCKER — arena slot REUSE poisons the DPU's grouped-control validator

**This bug was UNREACHABLE until today.** Before P5 the backend leaked and never released its arena slot,
so every new session got a virgin slot. The first-ever slot reuse hit this immediately.

Run 41i, with the probes that localized it:

```
23:42:34.567  arena slot 0 ring 2 tenancy reset (forgetting accepted_epoch=5 accepted_tail=560 session=1)
23:42:35.185  grouped-control semantic validation failed: host publication epoch regressed
              [ring=2 role=5 bound_session=2 accepted_epoch=5 line_epoch=0 accepted_tail=560 line_tail=0]
   -> DPU DMA engine is in fatal error state  -> run 2 times out at sql_execute sequence=5
```

**The sequence (each step evidenced):**
1. T4 unbinds the slot. A NEW reset (`HomerDpuDmaResetRingTenancyState`, homer_service_dpu_dma.c) now
   correctly forgets the tenancy — the probe shows it forgetting `epoch=5 tail=560`.
2. **But the HOST's publish line still holds the old tenant's epoch 5** — nobody clears it on release. The
   backend memsets its arena SLOT; the `HomerDpuBridgeHostPublishLine`s live in the bridge control block,
   and the backend bridge never touches them.
3. The DPU reads that stale line. Against the freshly-reset `accepted=0`, epoch 5 looks like a legitimate
   ADVANCE, so it is accepted. **This is the poisoning.**
4. The new backend binds the slot and zeroes its publish lines → the line now reads 0.
5. Next read: `line_epoch=0` vs `accepted_epoch=5` → "regressed" → **engine fatal**, and every subsequent
   session on that DPU is dead (run 3 cannot even open a session).

**Root cause, stated generally:** *an arena slot's host content carries no tenancy stamp, so stale content
from the previous tenant is indistinguishable from fresh content of the new one.* The DPU binds the slot at
SPAWN time — before the new backend exists — and reads it during the window in which it still holds the
dead tenant's bytes.

**Why the tenancy reset alone cannot fix it:** the reset is correct and necessary, but it only makes the
DPU forget. It does not stop the DPU from immediately RE-learning the same stale bytes off the wire.

**Three options (OWNER DECISION NEEDED — do not improvise this inside the DMA engine):**

- **(A) Release-ack.** The backend acknowledges RELEASE only *after* it has cleared the slot AND its publish
  lines; the DPU marks the slot reusable only on that ack. Most correct; adds an ack to the P2.T handshake
  and a wait state to the slot allocator.
- **(B) DPU zeroes the slot's publish lines at BIND, before spawning the backend.** The DPU already has DMA
  write capability into the arena. Race-free by construction: the only other writer of zeros is the old
  backend's memset (also zeros), and the new backend cannot publish before it is spawned. Cheapest, and it
  makes "the baseline is 0" a fact the DPU established itself rather than inherited. Cost: one DMA write +
  completion in the spawn phase machine.
- **(C) Tenancy generation in the ABI.** Add a tenancy counter to the publish line; the DPU compares
  `(tenancy, epoch)` and treats a tenancy change as a fresh baseline instead of a regression. Most general,
  and it kills the whole class — but it is an ABI bump on all four machines.

Current lean: **(B)**, with (C) recorded as the eventual clean answer. (B) is local, needs no ABI change,
and its failure mode is loud.

### 13.4 P5.c still open — a clean close still routes through the FAILURE machinery

```
23:37:01.619 teardown T4 released backend; retained fenced service session=1
23:37:01.621 peer transport ... CM event=DISCONNECTED status=0, resetting connection
23:37:01.621 payload stream marked ABORTING for peer reset session=1 sink=1
23:37:01.624 marked send byte-ring failed for sink=1 reason=peer-reset-abort
23:37:01.624 committed service-owned DPU result failure ... reason=payload-failure-action
```

T4 retains a **fenced** service session and its payload binding, so the ordinary client disconnect that
follows is routed as a peer FAILURE. A clean close and a real peer reset remain indistinguishable in the
logs. Fix with the §12.2 P5.c plan (a disconnect on a stream with no live binding is a no-op; a disconnect
WITH a live binding stays loud) — but note it likely needs T4 to drop the payload binding, which interacts
with (A)/(B)/(C) above, so sequence it after that decision.

### 13.5 Probe rules this run re-earned

- **A branching guard must name its branch AND its operands.** `TupleSinkServiceCommandMailboxBusy` has
  three branches over three frontier pairs and reported all of them as one opaque sentence. Making it
  self-describing cost ~30 lines and solved P5.b in ONE run, after static reading had produced a confidently
  wrong hypothesis.
- **A validator that kills the engine must say WHICH ring.** "host publication epoch regressed" named
  neither ring, role, nor values; it was consistent with three different bugs. With `[ring= role=
  bound_session= accepted_epoch= line_epoch= ...]` the race fell out of a single run.
- **A reset that silently does nothing looks exactly like a reset that ran.** The `tenancy reset
  (forgetting ...)` line is what PROVED my own fix executed — and therefore that it was not the answer.
- **`rc=0` is still not the verdict.** Run 41a exited 0 while silently skipping the semantic close.

---

## 14. P5 CLOSED — arena slot REUSE works; three consecutive runs green (July 12, 2026)

### 14.1 Acceptance (§12.2), all six criteria

| # | criterion | evidence |
|---|---|---|
| 1 | `teardown T1..T4`, in order | 3× `teardown T4` (one per session) |
| 2 | `BACKEND_SLOT_RELEASE` in postgres.log | **3** |
| 3 | NO surviving `remote exec backend` | /proc scan clean |
| 4 | **consecutive runs, NO baseline reset** | **THREE ran back to back; all 5/5, all 5 decoded rows** |
| 5 | ZERO failure-machinery lines on a clean close | **9 — STILL OPEN (P5.c, §14.4)** |
| 6 | rc=0 **and empty error stream** | all three runs |

Slot reuse is genuinely exercised, not vacuously passed: the DPU log shows arena slot 0 released and
re-let three times (`publish lines cleared` → `tenancy reset` → `publish lines cleared` → ...), with
**zero** `epoch regressed` and **zero** `fatal error state`.

### 14.2 The fix that finally worked — and the two that did not

Owner chose option (B): the DPU zeroes the arena slot's host publish lines by DMA at BIND, awaited
before the doorbell (which is what forks the backend, so the zeros necessarily precede the new tenant's
first publication). That is `HOMER_DPU_DMA_TASK_KIND_ARENA_PUBLISH_LINE_CLEAR` +
`HOMER_SERVICE_DPU_SPAWN_PHASE_CLEAR_ARENA_LINES` / `..._AWAIT_CLEAR`, plus a host-side companion that
closes the arena's own stated invariant (`HomerFrontendAgentInitArenaSlot` now clears the slot's publish
lines, which live OUTSIDE `HomerFrontendArenaSlot` and so had been silently exempt from
"a slot is always already empty and ABI-initialized while it is FREE").

**But (B) alone did NOT fix it, and neither did a stale-completion guard.** The full sequence took three
attempts, and the two failures are the lesson:

1. **Tenancy reset** (`HomerDpuDmaResetRingTenancyState`): make the DPU FORGET the dead tenant's frontier.
   The old unbind hand-cleared 3 of ~25 fields; this memsets and restores only the in-flight accounting,
   so a field added later is forgotten by DEFAULT. **Necessary, not sufficient** — run 41i showed the ring
   back at the exact values it had just forgotten.
2. **Tenancy-generation guard**: stamp the tenancy into each grouped-control read, drop completions whose
   stamp is stale. **Fired ZERO times and fixed nothing** (run 41m). The poisoning read was submitted
   AFTER the reset, so it carried the CURRENT tenancy and sailed straight through.
3. **THE ACTUAL FIX — `tenancyBaselinePending`, a SUBMIT-side gate.** Between a slot's release and its
   next tenant's clear landing in host memory, the engine issues **no grouped-control reads at all** for
   that ring. Set by the tenancy reset; cleared in the COMPLETION of the clear DMA — because
   *"the write was issued" is not "the bytes are zero"*.

**OWNER'S POINT, and it is the general rule:** *don't submit a task whose completion will be meaningless —
if the task was submitted, somebody needs its completion, and throwing it away breaks that consumer.*
Discarding a completion is defensible ONLY for **discovery** work that nobody awaits (a grouped-control
read is a poll; its only consumer is the ring runtime it updates, and the next poll re-reads the same
line). For any awaited task — a command pull, a payload write, the spawn chain — a dropped completion
would strand the waiter, and the correct move is to not have it in flight across the boundary. The
generation guard is kept as a cheap net for reads issued *before* a reset, but it is explicitly the
secondary defence; the submit-side gate is the primary one.

### 14.3 The root defect, stated once

**An arena ring's host state is TENANT-scoped, but its DPU-side runtime and its discovery polling were
RING-scoped.** Nothing marked the boundary between tenants, so the DPU could not tell "the previous
tenant's last frontier" from "the current tenant's first". Every symptom (epoch regressed, engine fatal,
second-run session-open timeout) is downstream of that one missing concept.

### 14.4 P5.c — still open (the ONLY remaining P5 item)

A clean close still routes through the FAILURE machinery: `T4 released backend; retained fenced service
session=N`, then the client's ordinary `CM event=DISCONNECTED` finds a live payload binding and produces
`marked ABORTING` / `peer-reset-abort` / `committed ... failure` (3 lines per session, 9 in this run).
A clean close and a real peer reset remain indistinguishable in the logs. T4 must drop the payload
binding so a disconnect on an unbound stream is a no-op, while a disconnect WITH a live binding stays
loud. This does not block P4.0/P4.1 — it is cosmetic-but-dangerous (it hides real peer failures), so fix
it before any failure-injection work.

---

## 15. P5.c — clean close vs. real peer failure (July 12, 2026)

### 15.1 What shipped: the logs no longer lie. The machinery is unchanged.

Every peer-reset line now reports `clean_close=`, stamped from the owning session's
`teardownCompletedCleanly` (set at T4), and the sweep reports `bits=` vs `clean=`:

```
payload stream marked ABORTING (EXPECTED: after a clean close) ... clean_close=1
peer reset-begin captured payload streams bits=0x1 clean=0x1 ...
cleared aborted payload binding ... clean_close=1
committed service-owned DPU result failure ... clean_close=1
```

**Read it as: any bit set in `bits` but NOT in `clean` is a REAL peer failure.** On three consecutive clean
runs, `bits == clean` every time. Before this, a clean close and a genuine peer reset emitted
byte-identical lines, so a real failure was indistinguishable from the tail of every successful run.

### 15.2 THE FULL FIX LANDED — acceptance #5 met (ZERO failure publications on a clean close)

The reporting-only version above was a stopgap; the real fix followed immediately. A session that
completes T1..T4 sets `teardownCompletedCleanly`; the peer-reset sweep stamps its streams with
`peerResetAfterCleanClose`; and at **reset-COMPLETE** such a stream takes a dedicated reclaim branch:

```c
if (streamEntry->stream.peerResetAfterCleanClose) {
    HomerServiceClearPayloadStreamPeerBinding(streamEntry);
    PayloadBindingsCleanReclaimed++;
    HomerServiceResetPayloadStreamEntry(streamEntry, true);   /* unbinds the byte-ring POOL slots */
    return;
}
```

Deliberately NOT called: `MarkPayloadStreamSendFailed` / `...ReceivePeerFailed` (there was no failure),
`ArmPayloadFailureReady` (that is what published the phantom failure completion), and
`ReleaseAbortedProducerSourceCreditAfterTerminal` (it stores into `sendQueue.byteRingControl`, dead
storage on the mirror path, to unblock a producer -- the backend -- that has already exited).

**What was actually wrong.** Nothing observable, which is why it mattered. On every SUCCESSFUL run the
abort path marked the stream FAILED, published a `failureState`, set `dpuResultPeerOpenFailed` on the
retained session, and committed a "service-owned DPU result failure through terminal completion" into a
slot whose client had already exited. The failure counters moved on every success, and — the real hazard —
**the happy path depended on the FAILURE machinery to reclaim its own stream.** The day anyone made that
machinery do more (mark the peer unhealthy, refuse session reuse, notify a client), every clean close
would have tripped it.

**Bonus, and it confirms the mechanism:** the payload stream entry is now REUSED — `index=0` on all three
sessions, where it used to climb 0, 1, 2. The failure path's deferred reclaim had been leaving stream
slots allocated.

**Reclamation stays at reset-COMPLETE, never reset-BEGIN.** The first attempt moved it to begin and broke
the next session's RDMA connect (`CM event ... REJECTED status=8`): the abort path's own comment says why
-- *"the QP/CQ reset is the transport proof"* that no send can still reference the stream, and that proof
does not exist yet at begin. The two-phase structure is load-bearing.

**Validated:** three consecutive pgbench runs, same postmaster and services, no baseline reset — all 5/5,
five decoded rows each, empty error streams; **ZERO** failure publications on a clean close (was 9); 3x
clean reclaim; 3x `BACKEND_SLOT_RELEASE`; zero surviving backends; zero engine fatals; zero postgres
FATALs.

### 15.3 P5 IS CLOSED — all six §12.2 criteria

| # | criterion | result |
|---|---|---|
| 1 | `teardown T1..T4`, in order | PASS (3x) |
| 2 | `BACKEND_SLOT_RELEASE` in postgres.log | PASS (3) |
| 3 | NO surviving `remote exec backend` | PASS (0) |
| 4 | consecutive runs, NO baseline reset | PASS (3 back-to-back) |
| 5 | ZERO failure-machinery lines on a clean close | **PASS (0)** |
| 6 | rc=0 **and** empty error stream | PASS (3x) |

### 15.4 Rules earned

- **Do not restructure a working state machine to fix a logging problem** — but do NOT stop at the log
  either when the state machine is itself lying. The log fix bought a correct, low-risk landing; the state
  fix was then done separately, in the phase where it is safe.
- **A happy path must not depend on the failure path to clean up after it.** That coupling is invisible
  while both work, and it converts any future strengthening of failure handling into a regression on every
  successful run.

---

## 16. P4.0 — Stage-5 ABI cleanup: design of record (July 12, 2026)

### 16.1 What is actually wrong

`CitusTupleSinkTransportHeader.generationSequence` is a **family-dependent** field in a **family-agnostic**
transport envelope. Its own comment admits it:

> *"Tuple-result DATA/EOS records still use generationSequence as their command-local sequence until the
> Stage 5 EOS cleanup. Other object families must validate their semantic sequence in their own payload
> header, not in this common transport envelope."*

Basebackup writes 0; tuple results write a 1-based command-local sequence. A consumer that reads the
envelope therefore has to know which family it is looking at — and in run 39 one did not, demanded the
BASEBACKUP convention (`== 0`) on TUPLE records, and silently rejected every row forever (§10l). This
field is the root of that bug family.

### 16.2 The key finding: on the PACKED path the envelope field is already a pure duplicate

`CitusTupleSinkBatchHeader.sinkSequence` is **already written and already validated** alongside it:

- written: `homer_tuple_queue_frontend.c:1494, 1935`; `tuple_sink_service_process.c:22847, 30352`
- validated: `homer_tuple_queue_frontend.c:2188`; `tuple_sink_service_process.c:29776, 30031`

So for the packed wire form P4.0 is a **deletion**, not a migration: drop the envelope duplicate and its
checks; the payload header already carries the truth.

Only the **DECODED** form (the DPU two-ring deform relay -> role 7) lacks a payload-level sequence: its
`HomerDecodedTupleBatchHeader` has none, so the sequence lives ONLY in the envelope. That is the one place
where something must be added.

### 16.3 DEVIATION from §12.4, and why

§12.4 said to move the sequence into `HomerDecodedTupleBatchHeader.reserved0` — *"free, no struct growth"*.
**That is wrong: `reserved0` is `uint32_t` and the sequence is `uint64_t` (`sinkSequence`).** Reusing it
would silently TRUNCATE — in the very field whose mistyping caused this bug family.

**Decision:** replace `uint32_t reserved0` with `uint64_t commandSequence`, growing the decoded batch header
24 -> 32 bytes. Eight bytes on a record that already carries a 56-byte transport envelope is noise, the
tuples after it are MAXALIGN(8) so no padding is wasted either way, and it makes the command sequence the
SAME TYPE everywhere (`uint64_t`, mirroring `CitusTupleSinkBatchHeader.sinkSequence`). Correctness over a
stale "free" claim.

### 16.4 The change

1. **`homer_decoded_tuple_abi.h`** — `reserved0` -> `uint64_t commandSequence`; bump
   `HOMER_DECODED_TUPLE_BATCH_PROTOCOL_VERSION`.
2. **`homer_tuple_abi.h`** — the envelope's `generationSequence` is RETIRED: same 8 bytes, renamed to a
   reserved field that MUST BE ZERO for every family. Bump `CITUS_TUPLE_SINK_PROTOCOL_VERSION`.
3. **Producers** stop writing a semantic sequence into the envelope (write 0).
4. **The deform** (`homer_tuple_deform.c`) stamps `destBatchHeader->commandSequence` from
   `sourceBatchHeader->sinkSequence` — it already holds the source header.
5. **Consumers** drop every envelope-level `generationSequence` check. The packed path keeps its existing
   `sinkSequence` checks; the decoded path (client `HomerClientSqlResultDeliverDecoded`) validates
   `batchHeader->commandSequence` instead of `header->generationSequence`.
6. **THE PERSISTENT-STREAM IDENTITY RULE (§10l) IS UNCHANGED** — per-COMMAND state still restarts
   (`ordinal -> 0`, `sequence -> 1`); per-STREAM byte frontiers are still absolute. Only the FIELD the rule
   reads changes. The client's `receiveExpectedGenerationSequence` is renamed to
   `receiveExpectedCommandSequence` to match.

Version bumps mean a stale binary fails LOUDLY (`BAD_PROTOCOL`), so all four machines must be rebuilt.

### 16.5 P4.0 LANDED and VALIDATED (citus 879d6d1bc)

Versions: `CITUS_TUPLE_SINK_PROTOCOL_VERSION` 12 -> 13, `HOMER_DECODED_TUPLE_BATCH_PROTOCOL_VERSION` 1 -> 2.
All four machines verified to agree at v13 before the run.

**Two places where 16.2's "pure duplicate" claim was FALSE**, found during implementation. In both, deleting
the envelope check alone would have LOST a real validation, so payload validation was ADDED:
- `HomerClientDrainResultSinkUntil` had no `sinkSequence` check -> one added.
- `HomerServiceTupleViewNextEosSequenceFromProducerBytes` derived its sequence solely from the envelope ->
  now reads the packed batch header. (This function is itself dead code -- see P4.2a -- but it was fixed
  rather than left in a broken half-state.)

**The reserved-is-zero check is ENFORCED, not merely documented.** The client's basebackup receive lost its
zero-check in the first cut; it was restored, and the SQL-decoded envelope guard gained one, with the leg
named in the reject probe. A "MUST be zero" that nobody checks is aspirational, and these 8 bytes have
already acquired a meaning once.

**Validated:** full smoke matrix builds; `homer_tuple_deform_smoke` ALL PASS; three consecutive pgbench runs
on one stack -- all 5/5, **five decoded rows each**, empty error streams, zero reject probes, zero failure
publications, zero engine fatals. The decoded-row count is the real proof: reading the wrong field rejects
every row, which is exactly what run 39 did.

**Remaining P4 sequence:** P4.1 (mirror-path truth-source) -> P4.2a (delete the already-dead producer
byte-ring code) -> section 10k (strip diag, measure, fix the grant stall) -> P4.2b (complete removal, gated
on the host-service local-byte-ring retirement).

### 16.6 "Reserved means zero" is now UNIFORM — one definition, every path (citus a27faae20)

Owner's point, and it corrected a real hole: if the rule is family-agnostic, it must be **uniform**, and it
was not. This is the distinction P4.0 turns on, so it is worth stating precisely:

- the OLD field (`generationSequence`) was **family-DEPENDENT** — it *meant different things* per family
  (0 for basebackup, a 1-based command sequence for tuples). Every consumer had to know which family it was
  reading. That was the bug.
- the NEW field (`reservedSequence1`) is **family-AGNOSTIC** — reserved means **zero, for everybody**. Zero
  per-family knowledge required. Semantic sequences moved DOWN into each family's own payload header
  (`sinkSequence` packed, `commandSequence` decoded), which is where per-family meaning belongs.

**What was actually broken.** The writers were uniform; the checkers were not:

- the service's reserved-zero check sat **inside `else if (objectFamily == BASE_BACKUP_STREAM)`** — a rule
  stated as universal, enforced for exactly one family, with the tuple-view arm checking nothing. Same
  shape as the bug P4.0 exists to kill.
- the two PACKED receive paths (`HomerClientDrainResultSinkUntil`, `PollCitusTupleSinkRecord`) validated no
  reserved field at all.

**And because nothing looked, nothing noticed:** `HomerClientSubmitBaseBackupRecord` and the service's EOS
append assign the envelope field-by-field and **forgot `reserved1`**, while the record buffer is a slice of
a byte ring that is never cleared — so that field went out **carrying whatever the previous record left in
it**. A "MUST be zero" that nobody checks is aspirational.

**Fix:** `CitusTupleSinkTransportHeaderReservedIsZero()` in `homer_tuple_abi.h` is THE SINGLE DEFINITION,
called from every envelope validator (service check hoisted OUT of the basebackup arm to before the
per-family switch). Both leaky producers now memset the envelope before filling it — zero by DEFAULT, so a
field added tomorrow is correct without anyone remembering to come back.

**Rule earned:** *a rule that is uniform in the comment but hand-copied at N call sites is not uniform.*
Give it one definition and call it; otherwise the drift is only a matter of time, and it will drift in the
direction of whichever family the last author had in mind.

**Validated:** three consecutive pgbench runs — 5/5, five decoded rows each, zero reject probes, **zero
"nonzero reserved fields" errors** (with the check finally enforced everywhere, no producer is shipping
garbage), zero failure publications, zero engine fatals. `homer_tuple_deform_smoke` ALL PASS.

**CAVEAT:** the basebackup producer's `reserved1` fix is NOT exercised by pgbench — it needs the 4-role
DPU-relay basebackup workload. Correct by inspection (memset before fill), but unvalidated at runtime.

---

## 17. P4.2a + P4.1 — the mirror-path truth-source cleanup (design of record, July 12, 2026)

### 17.0 SEQUENCING CHANGE: P4.2a moves AHEAD of P4.1 (decided, not improvised)

§12.1 ordered these `P4.1 -> P4.2a`. Reversed, for two reasons found while scoping P4.1:

1. **P4.1's real surface is 115 `sendQueue.*` accesses**, not the six the sweep named (§10l counted only the
   *hard* dependencies — the geometry/existence gates that block deletion. The frontier readers, the
   diagnostics and the pumps sit on top).
2. **P4.2a deletes a chunk of that surface, has zero dependencies, and is bigger than §12.5 recorded.**

Doing the free deletion first strictly shrinks the refactor. Nothing in P4.2a depends on P4.1.

### 17.1 P4.2a is not just dead code — it is a scheduler action that can NEVER be granted

`HomerServiceTupleViewEosAppendReady` returns `false` unconditionally (the body is
`(void) streamEntry; (void) requireCapacity; return false;`). Following its callers UPWARD:

- it is the **sole producer** of the ready bit `HOMER_PROGRESS_REASON_PAYLOAD_EOS_READY` (one site), and
- that bit is the **sole arming input** for the entire scheduler action `HOMER_PROGRESS_ACTION_PAYLOAD_EOS`,
  which is wired through ~8 switches, an action-mask helper, a priority chain and the reason->action map.

So the service carries a **complete, permanently un-armable scheduler action**. That is this arc's bug family
in scheduler form, and CLAUDE.md already names the hazard: *an un-armable collector and a healthy idle one
look identical.* It goes.

**Deleting an enum member is a better refactoring tool than grep here.** CLAUDE.md warns that `-Wswitch`
protects nothing in this file (nearly every switch over the progress enums has a `default:` arm, so ADDING a
member compiles clean and falls through silently). Removing one inverts that: every `case ...PAYLOAD_EOS:`
and every hand-enumerated `AppendCollectorAction` naming it becomes a HARD COMPILE ERROR. The compiler
enumerates the removal sites — the completeness check the `default:` arms deny us in the other direction.

Deleted: the predicate + its 5 call sites, `HomerServiceTupleViewNextEosSequenceFromProducerBytes`,
`HomerServiceAppendTupleViewEosRecordToProducerByteRing`, `HomerServiceAppendTupleViewEosForStream` + caller,
the reason bit, and the action kind. The reason bits are explicit `(1U << N)` values — the deleted one leaves
a HOLE and the others are NOT renumbered (renumbering would be a silent change to every mask).

### 17.2 THE FINDING THAT SHAPES P4.1: the source kind is a process-CONSTANT, re-derived ~25x per pass

`HomerServicePayloadStreamUsesDpuMirrorSource` (tuple_sink_service_process.c:6867) ends in:

```c
dpuDmaState = HomerServiceDpuDmaSchedulerStateForProgress();
return HomerServiceDpuDmaSchedulerEnabled(dpuDmaState) && dpuDmaState->engine != NULL;
```

But `dpuDmaState.enabled` is read ONCE from env at startup (:48009), `engine` is created ONCE (:48031) with
`exit(1)` on failure, and destroyed only at :48212 AFTER the main loop. **`enabled && engine != NULL` is
therefore invariant for the entire life of the main loop.** The predicate re-computes a constant, from a
global, at ~25 call sites, every scheduler pass.

That is not merely waste. It is *why* the source kind reads as a derived opinion rather than a stored fact —
and it hides a latent instance of the very bug family: **if `engine` were ever NULL, every mirror stream would
silently reclassify as LOCAL_BYTE_RING and begin reading the structurally-zero `sendQueue.byteRingControl->
publishedTail`.** Freezing the kind at bind deletes the failure mode outright.

### 17.3 P4.1 design

**(a) `HomerPayloadSourceKind`, stored on the stream entry, stamped at bind.**

```c
typedef enum {
    HOMER_PAYLOAD_SOURCE_INVALID = 0,   /* a zeroed struct must NOT read as a valid kind */
    HOMER_PAYLOAD_SOURCE_FIXED_SLOT,    /* legacy slot ring                                */
    HOMER_PAYLOAD_SOURCE_LOCAL_BYTE_RING, /* shm ring this process can read directly       */
    HOMER_PAYLOAD_SOURCE_DPU_MIRROR,    /* host ring reachable ONLY through the DMA engine */
} HomerPayloadSourceKind;
```

`INVALID = 0` is deliberate and is the §12.3 principle applied to the new field itself: a memset-zeroed entry
must not accidentally read as a valid source.

Classified by `HomerServiceClassifyPayloadSource(objectFamily, dpuRelayResultStream)` against the
process-constant `HomerServiceProcessOwnsDpuDmaEngine()`; stamped at BOTH construction sites
(`HomerServiceCreatePayloadStreamEntry` :25188 and the identity shell
`TupleSinkServiceReserveDpuResultStreamIdentity` :20037). The two existing predicates KEEP their names and
signatures and become pure field reads — so their ~40 call sites are untouched. Minimal diff, maximal safety.

**(b) Geometry moves OUT of the mapping.** Four sites read the ring size from INSIDE the mapped control block
(`sendQueue.byteRingControl->ringBytes`, :38456 and :39524) or from the mapper's own copy (:17720, :36907).
Geometry is known at create (`producerRingBytes`) and does not need a mapping to exist. Add
`stream.payloadSourceRingBytes`, set at create, and read it everywhere. Note :39524 feeds
`requestedByteRingBytes` into the peer OPEN — leaving it to read a NULL mapping would silently send `0`.

**(c) Existence tests -> one accessor.** `HomerServicePayloadSourceIsBound(streamEntry)` replaces the
`sendQueue.queueControl != NULL || sendQueue.byteRingControl != NULL` disjunction (:26646). On DPU_MIRROR it is
TRUE — the engine mirror IS the source; there is no local struct to point at. Getting this wrong silently
disables failure marking on every mirror stream.

**(d) ONE frontier accessor, allowed to answer "not locally knowable".**

```c
static bool HomerServicePayloadSourcePublishedTail(const HomerServicePayloadStreamEntry *e, uint64_t *out);
/* returns FALSE on DPU_MIRROR. A caller that cannot handle false MUST NOT ASK. */
```

**(e) Then stop mapping the producer shm on the mirror path** (skip `HomerServiceMapProducerByteRingQueue` when
kind == DPU_MIRROR), leaving `byteRingControl == NULL` there. *A zeroed struct lies; a NULL pointer confesses.*

**Sites that MUST be converted before (e) is safe** — each is a real breakage, not a theoretical one:

| site | today | after |
|---|---|---|
| prepare's NULL-control reject (:26084-26091) | rejects a stream for lacking a struct it **never uses** — its only reader (:26145) is already skipped on the mirror path | move the NULL check INSIDE the non-mirror branch |
| `hasSendControl` (:26646) | would become FALSE on mirror -> **failure marking silently disabled** | `HomerServicePayloadSourceIsBound()` |
| blackhole reclaim log (:39956) | **UNGUARDED deref** of `byteRingControl->publishedTail` | frontier accessor; print `n/a` when not knowable |
| geometry (:17720, :36907, :38456, :39524) | reads the mapping; :39524 would send `requestedByteRingBytes=0` on the wire | `stream.payloadSourceRingBytes` |
| peer-bind frontier seed (:26003-26026) | seeds from the dead struct (reads 0,0) | accessor; mirror falls to the existing `else` -> 0,0 (identical) |
| abort source release (:28617) | stores `consumedHead` into a struct nobody reads | accessor; skip on mirror |

Already safe, verified, needs no change: the mirror branch is taken FIRST at the frontier facts (:7071) and the
completion apply (:14985, whose comment already says a mirror source has no sendQueue control word to credit);
and legacy sendQueue RDMA registration ALREADY rejects the mirror path outright (:17693). `queueShmName` turned
out to be a non-dependency: its only uses are `shm_unlink` (:25163) and one log line (:36886).

**(f) Land in TWO steps, validated separately.** Step 1 = (a)-(d), mapping still created: behavior-identical,
isolates "did the refactor break something". Step 2 = (e): isolates "did removing the mapping break something".
One extra validation cycle, and it means a hang in step 2 has exactly one possible cause.

### 17.4 Acceptance (falsifiable)

1. The three workloads stay green (pgbench --homer-dpu-command is the gate; 3 consecutive runs, one stack).
2. Zero reads of `sendQueue.byteRingControl->publishedTail` outside a LOCAL_BYTE_RING branch.
3. **No `/dev/shm/citus_res_*` object is created by a DPU-path run.** This is P4.2b's end-state proof, and
   step (e) makes it checkable ALREADY — the mirror path stops creating the shm object at all.

---

## 18. P4.2a LANDED (citus a39f233bf) — and it exposed that P5's acceptance #5 was measured on ONE NODE

### 18.1 P4.2a: validated and committed

All 7 smoke targets build; `homer_tuple_deform_smoke` ALL PASS; three consecutive
`pgbench --homer --homer-dpu-command` runs on ONE stack with **no baseline reset** — 5/5 each, five
decoded rows each, empty error streams, **0** surviving backends, `T1..T4` x3, **0** engine fatals,
anti-fallback proven (the farnet0 HOST service log contains only its startup banner).

Landed-proof for a pure DELETION needs a negative AND a positive control, or "absent" is
indistinguishable from "the binary never built". Both DPUs report
`DELETED_EOS_APPEND_LIT=0` **and** `POSITIVE_CONTROL_LIT=1`.

Two bugs in my own deploy script surfaced and are fixed (`r42_dpu_deploy.sh`):
- the peer path is a TWO-STAGE rsync and **stage 2 had no `--exclude build/`**, so `--delete` wiped the
  farnet0 DPU's build directory on every peer deploy;
- the build step was an inlined double-hop `ssh farnet0 ssh dpu "cd ~/..."`, and `~` expands on
  **farnet0** (to `/home/jasonhu`), not on the DPU (user `ubuntu`). CLAUDE.md names this trap. The build
  now lives in a script that is shipped and invoked BY PATH. Together these had left farnet0's DPU with
  **no service binary at all**.

### 18.2 CORRECTION TO §15.3: acceptance #5 (ZERO failure machinery on a clean close) is NOT met

§15.2 recorded "**ZERO** failure publications on a clean close (was 9)". Today, with the P5.c fix in
place and P4.2a green, the run shows:

| | `marked ABORTING` | `peer-reset-abort` | `committed ... failure` |
|---|---|---|---|
| node B (farnet1 DPU) | 0 | 0 | 0 |
| **node A (farnet0 DPU)** | **3** | **3** | **3** |

**9 = 3 lines x 3 runs — exactly the "was 9" that §15.2 claims to have zeroed.** The P5.c fix works, on
node B. Node A has the SAME defect from a DIFFERENT trigger and was never checked: **acceptance #5 was
measured on one node.** P5 is therefore **NOT closed**; the six-criteria table in §15.3 is wrong on row 5.

This is not a P4.2a regression: its diff touches none of the P5.c machinery (`teardownCompletedCleanly`,
`peerResetAfterCleanClose`, the clean-reclaim branch) — filter returns 0 hits.

### 18.3 CORRECTED DIAGNOSIS — the close is not mis-CLASSIFIED, it is NEVER SENT (P5.d)

My first read of this (that node A's `session=2` was "peer-created" and merely needed a clean stamp) was
WRONG. The owner challenged the two-session claim, and the thread unravelled into something larger.

**Fact 1 — node A really does carry TWO service sessions per run, and BOTH are opened by the CLIENT** on
its DPU control slot, with the same `sessionUID`:

- `OPEN_SESSION opKind=2` = `CITUS_REMOTE_EXEC_CONTROL_OP_COMMAND_SESSION` -> service session 1
- `OPEN_SESSION opKind=1` = `CITUS_REMOTE_EXEC_CONTROL_OP_TUPLE_SINK`     -> service session 2

Sessions are keyed by op kind (`sessionKey.opKind`), so this is the existing architecture, not a bug in
itself. Session 2 owns the result payload stream and the role-7 relay. It is the one that gets aborted.

**Fact 2 — node A receives ZERO `CLOSE_SESSION`, for EITHER session.** The DPU control-slot instrument
logs one (`[homer-service] dpu control slot: CLOSE_SESSION opKind=%u session=%llu sink=%llu`,
tuple_sink_service_process.c:43171) and there is not a single such line in three runs.

**Fact 3 — node B receives ZERO `CLIENT_SQL_SESSION_CLOSE`.** So the SEMANTIC close is not sent either.

**Fact 4 — WHY: `sqlSessionTerminal` is latched during NORMAL command traffic.** It is set at
homer_client.c:6876-6879 whenever any completion carries `postCommandState` of `DO_NOT_REUSE` **or**
`FAILED`; and `HomerClientCloseSqlSessionSelectedDpu` gates BOTH closes on it:

- step 1 (semantic `CLIENT_SQL_SESSION_CLOSE`): `if (!session->sqlSessionTerminal && serviceSessionId != 0)`
- step 3 (lifecycle `CLOSE_SESSION`):           `if (session->sqlSessionTerminal) { ...skip... }`

The client log confirms the skip: `homer client: skipping CLOSE_SESSION submit for terminal session 1
(backend exited)` on every run.

**So on every SUCCESSFUL run, the entire designed close protocol is skipped and the session is torn down
by the client's DOCA detach instead** -- which node A can only interpret as a payload-protocol failure:

```
setup import host-detached ...
terminating DPU relay after host consumer detached session=2 sink=1 sessionUID=...
marked byte-ring receive queue failed for sink=1 reason=relay-host-consumer-detached failure=4
payload stream marked ABORTING for peer reset (REAL PEER FAILURE) ... reason=4 clean_close=0
committed service-owned DPU result failure through terminal completion ... clean_close=0
reclaiming sink session=2 sink=1 reason=payload-failure-reclaim
```

(`reason=4` = `HOMER_PEER_RESET_PAYLOAD_PROTOCOL`, remote_execution_peer_transport_rdma.h:367.)

Node B's `T1..T4` still fire -- but they are triggered by the DPU setup close / doorbell EOF, **NOT** by
the semantic close. That is why P5's validation looked green: **I verified the teardown HAPPENED, never
that it happened THROUGH the path I had just built.**

**Fact 5 -- and this is why it hid: step 1's skip is SILENT.** The guard at homer_client.c:3348 has no
`else`. Step 3 at least prints "skipping CLOSE_SESSION submit for terminal session". Step 1 prints
nothing at all. *Silence is a valid state* -- the exact anti-pattern CLAUDE.md names, and the reason a
close that never ran looked identical to a close that ran fine.

**Fact 6 -- the TUPLE_SINK session is never closed even when step 3 DOES run.** homer_client.c:3420 hard-
codes `request->opKind = CITUS_REMOTE_EXEC_CONTROL_OP_COMMAND_SESSION` with the comment *"serviceSinkId
stays 0: a command session owns no payload sink."* So node A's session 2 has NO close path at all, on any
route. Even fixing the `sqlSessionTerminal` gate would leave it orphaned.

### 18.4 STOPPED — this needs an owner decision, not improvisation

The immediate question is what `DO_NOT_REUSE` is supposed to MEAN. The backend
(remote_execution_backend_bridge.c:3130-3148) sets it on `TX_COMMIT`/`TX_ABORT` **only when the session is
not a client SQL session**, and unconditionally on `CLIENT_SQL_SESSION_CLOSE`. So on the pgbench path it
should mean *"the backend is exiting because you asked it to"* -- yet the client is latching it BEFORE it
ever asks. Which completion carries it, and why, is the next probe.

Three coupled decisions, none of which the plan settles:

1. **Should `sqlSessionTerminal` gate the close AT ALL?** Its comment says a terminal postCommandState means
   "no further command can ever execute on this session" -- but 5/5 transactions execute after it is
   latched, so the flag's stated meaning and its actual behavior already disagree. Distinguishing "the
   backend exited because we told it to" from "the backend died" is the crux, and a real crash MUST stay loud.
2. **Who closes the TUPLE_SINK session?** It has no close path on any route today (Fact 6).
3. **Should "host consumer detached" ever be a FAILURE?** If a client can only detach after it is done, the
   classification is always wrong and `relay-host-consumer-detached` should be a clean terminate. If a
   client CRASH also detaches, the two are genuinely different and a flag is required.

**Rules earned:**
- *A criterion checked on one end of a two-ended path is not checked.* The P5 acceptance table must name
  the NODE for every row.
- *Verifying that an outcome happened is not verifying that it happened through the path you built.* P5's
  teardown was real; the mechanism I credited for it was not the one running.
- *Every skip must log.* A guard with no `else` turns "did not run" into "ran fine".

### 18.5 The owner is RIGHT: it SHOULD be one session with two sinks. The second session is an ARTIFACT.

**What `opKind` actually is — there are TWO of them, and only one is identity.**

| | enum | values | is it session identity? |
|---|---|---|---|
| `sessionKey.opKind` (logged as `sessionKeyOpKind=5`) | the SEMANTIC op kind | `CLIENT_SQL_SESSION`, ... | **YES** — it is a field of `CitusRemoteExecSessionKey` (homer_control_abi.h), the struct that IDENTIFIES a session |
| `request.opKind` (logged as `opKind=1/2`) | `CitusRemoteExecControlOpKind` | `TUPLE_SINK=1`, `COMMAND_SESSION=2` | **NO** — it says what THIS REQUEST OPENS: a payload sink, or the command spine |

Both of node A's `OPEN_SESSION`s carry the **same `sessionKey`** (`sessionKeyOpKind=5`, same db/user/node/
policies) and the **same `sessionUID`** (`5657593410303321`). By identity they ARE the same session. And the
session struct already supports one-session-many-sinks: `TupleSinkServiceSessionState` carries
`activeSinkCount`, incremented at four `activeSinkCount++` sites.

**So why did the second OPEN allocate a NEW session?** Because the service does not ask *"which session does
this sink belong to?"*. It asks `TupleSinkServiceFindReusableSession()` -> `TupleSinkServiceSessionAllowsReuse()`,
which is a **connection-POOLING** predicate:

```c
base-compatible?  &&  freshnessPolicy != FORCE_FRESH
                  &&  !PostCommandStateForbidsReuse(session)     /* DO_NOT_REUSE */
                  &&  !TupleSinkServiceCommandMailboxBusy(session)
                  &&  !TupleSinkServiceCommandStillInFlight(session)
                  &&  (freshness != REQUIRE_CLEAN || activeSinkCount == 0 ...)
```

That predicate answers *"is this idle pooled session safe to hand to a NEW client?"* — a completely different
question from *"is this the session this sink belongs to?"*. **A sink's owner is not a matter of policy; it is
a matter of identity, and the identity is right there in the request (`sessionUID`).**

The TUPLE_SINK open is issued as the client starts its FIRST command, i.e. while session 1 has a command in
flight. Any of `CommandMailboxBusy` / `CommandStillInFlight` then rejects reuse, and the service silently
allocates session 2 — a second service session, for the same client, same uid, same key — which then owns the
result stream and has **no close path on any route** (§18.3 Fact 6).

**This is the same species as everything else in this arc: the right answer was available (the `sessionUID` in
the request) and the code consulted a heuristic instead.**

**HYPOTHESIS, NOT YET PROVEN:** that the rejecting predicate is specifically `CommandMailboxBusy` /
`CommandStillInFlight`. `TupleSinkServiceSessionAllowsReuse` is a 6-way ANDed guard that reports NOTHING about
which arm rejected — CLAUDE.md: *"a guard that ANDs N conditions must report WHICH one failed."* **The next step
is a probe on that predicate, not a patch.** Do not fix this until a log line names the arm.

### 18.6 Revised P5.d shape (for discussion — NOT implemented)

1. **PROBE FIRST.** Make `TupleSinkServiceSessionAllowsReuse` name its rejecting arm, and make the
   OPEN_SESSION handler log "reused session N" vs "allocated NEW session N (reuse rejected: <arm>)". One run
   turns the hypothesis above into a fact.
2. **Sink attach should be by IDENTITY, not by reusability.** An OPEN carrying a `sessionUID` that names a live
   session should ATTACH its sink to that session, full stop. Reusability is for picking a *pooled* session for
   a *new* client, and must not be consulted when the client has already named one.
3. **Then the close collapses to one session** and P5.d's other two questions (§18.4) mostly dissolve: one
   session, closed once, cleanly, with `CLOSE_SESSION` actually reaching node A.
4. `sqlSessionTerminal` gating BOTH closes off, and step 1's silent skip, still need fixing regardless.

### 18.7 CORRECTION TO §18.5, and the REAL session-identity defect

§18.5 said "both sessions are opened by the CLIENT". **That is wrong** — I inferred it from the two
`dpu control slot: OPEN_SESSION` log lines without reading what the second one does. Correcting:

- **The client's role-7 `OP_TUPLE_SINK` open creates NO session.** It is a pure RENDEZVOUS: the handler
  returns early at tuple_sink_service_process.c:36198-36204 with `serviceSessionId=0, serviceSinkId=0` and a
  zeroed queue descriptor. Its only job is to record the ring's `sessionUID` binding.
- **Node A's `session=2` is a "peer compatibility session"**, created by node B's RESULT PEER-OPEN arriving at
  node A: `tuple-sink service: created peer compatibility session=2 op=5 dest_node=1 ...`
  (allocation at :27501, `TupleSinkServiceCreateSession(sessionState, &request->sessionKey, 0)`).

**Terminology, corrected (owner's point).** The right verb is **BIND**, not "open" or "export":
- an **export** is a DOCA mmap export of a host memory REGION + its ring descriptors, shipped over the TCP
  setup socket; the DPU **imports** it. The postmaster's **frontend arena** is the same mechanism for
  socketless BACKENDS (one region, 48 arena rings + 1 spawn). A client makes its own export(s).
- a **BIND** is the session<->ring relationship. **A session can bind all the rings it needs — by design.**
- The bug is the conflation: "I need another region imported" got implemented as "I need another OPEN", and
  the service then answered "who owns this sink?" with a POOLING predicate instead of the identity in the
  request.

**The chain, fully verified by reading:**

| # | fact | file:line |
|---|---|---|
| 1 | node B's result peer-open PUTS the client's uid on the wire: `request->sessionUID = sessionState->sessionUID` | `TupleSinkServiceStartServiceResultPeerOpen` :39229 |
| 2 | node A's session 1 (the client's command session) HAS that uid stamped | :36016 |
| 3 | node A's peer-open handler **NEVER consults `request->sessionUID` to FIND a session** — it calls `FindReusableSession(sessionKey)`, fails, and allocates a "peer compatibility session" | :27489-27501 |
| 4 | it *does* pass the uid DOWN into the stream (`dpuRelayResultSessionUID`) — so the uid is received, stored on the STREAM, and never used to find the OWNER | :27554 |

**So the owner is right and the fix is small in principle: the peer-open should FIND the local session whose
`sessionUID` matches and bind the result stream to it. One session, two bound ring-sets.**

**Why the code didn't do that — a comment describing a topology that no longer exists.** :36161-36164 says:

> *"SQL's command spine lives entirely in the HOST service on both ends while the DPU relay is a separate peer
> leg; the two are bound by the UNIQUE sessionUID, not by a shared session table. (Unifying ownership would
> need a net-new DPU<->DPU command-session spine -- a later milestone.)"*

**That premise is now FALSE for this path.** The command-plane migration (S1-S4) moved the command spine ONTO
the DPU: node A's DPU service now HOLDS the client's command session (session 1, opened over the DPU control
slot, uid stamped). The "net-new DPU<->DPU command-session spine" the comment says we'd need... is the thing
we just built. **The compatibility session is a vestige of the pre-migration topology.**

This is the same species as the rest of the arc: *the truthful identity was on the wire and in the struct; the
code consulted a heuristic instead.*

### 18.8 Also do not lose: ONE export vs TWO (owner: "we should not just ignore it")

The client currently makes TWO DPU exports (two setup connections, two bridge generations, two mmap imports):
COMMAND (role 1 control slot + role 6 completion line) and RESULT (role 7 + credit). homer_client.c:69-78
justifies it as *"hostMmapImportCapacity is 1024, so two imports per client is free."* Free in capacity, NOT
free in lifecycle: **each export detaches separately, and it is one of those detaches that node A reports as a
payload-protocol FAILURE.** Folding them into one export (one region, all four rings) removes a whole
teardown edge. Not required for correctness; explicitly RECORDED, not ignored. Revisit after P5.d.

---

## 19. P5.d PROBE RESULTS (July 12, 2026) — three facts, and one of them REFUTES §18.3

The owner said: *before instrumenting, ask a subagent for an analysis.* That was right, and it paid twice —
it CORRECTED my premise (pgbench does not submit only SQL_EXECUTE; it submits
`CLIENT_SQL_TX_BEGIN -> 5x SQL_EXECUTE -> TX_COMMIT`, plus a warm-up `CLIENT_SQL_TX_BEGIN` + `TX_ABORT`),
and it narrowed the probe from three instruments to two. The Codex sweep also verified, with file:line, that
the entire role-6 completion chain copies `postCommandState` faithfully and that a successful `SQL_EXECUTE`
never sets it — so the probe had exactly one question left to answer.

### 19.1 REFUTED: "the close protocol is never sent" (§18.3) — IT IS SENT

```
homer client: [p5d] sqlSessionTerminal LATCHED by session=1 command_kind=8 command_sequence=38
              command_state=3 post_command_state=2 post_flags=0
```

`command_kind=8` is `CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`. **The semantic close IS submitted
and DOES complete** (the `[p5d] SKIPPED the semantic ...` probe fired ZERO times), and `post=2`
(`DO_NOT_REUSE`) is the CORRECT response to a close. `sqlSessionTerminal` latches as a *consequence* of the
close, exactly as designed.

Node A's ingress census confirms the whole command mix, with post-states:

| kind | count | post | meaning |
|---|---|---|---|
| 7 `CLIENT_SQL_TX_BEGIN` | 6 | 0 | 1 warm-up + 5 transactions |
| 4 `TX_ABORT` | 1 | 1 (`REUSABLE_IDLE`) | the warm-up abort — correctly NOT terminal |
| 6 `SQL_EXECUTE` | 25 | 0 | 5 per transaction |
| 3 `TX_COMMIT` | 5 | 1 (`REUSABLE_IDLE`) | correctly NOT terminal |
| **8 `CLIENT_SQL_SESSION_CLOSE`** | **1** | **2 (`DO_NOT_REUSE`)** | **the latch — and it is CORRECT** |

**§18.3 was WRONG, and it was wrong for a reason worth naming.** I concluded "node B never receives
CLIENT_SQL_SESSION_CLOSE" from grepping node B's log for that literal — which node B simply never prints.
**That is the absence-of-log fallacy, one paragraph after I wrote the rule against it.** The probe caught it.

### 19.2 THE REAL P5.d BUG: only STEP 3 is skipped, on a false premise

`HomerClientCloseSqlSessionSelectedDpu` step 3 (homer_client.c:3399-3411) skips the lifecycle `CLOSE_SESSION`
whenever the session is terminal, justifying it:

> *"the backend already exited ... Submitting CLOSE_SESSION would either hang on a DEAD RESPONDER or trip the
> busy-control-slot refusal"*

**The responder for a control-slot `CLOSE_SESSION` is not the backend — it is the DPU SERVICE, which is very
much alive.** The comment conflates the dead BACKEND (farnet1) with the live SERVICE (node A) that actually
answers control-slot requests. (And the "busy-control-slot refusal" it also feared is the bug already fixed in
P5.b(2).)

Consequence: node A's sessions are NEVER closed, are torn down only by the client's DOCA detach, and that
detach can only be reported as a payload-protocol FAILURE. **That is the whole of the node-A failure
machinery, and it is one skipped request.**

### 19.3 NEW BUG: node A's command session never records its own sessionUID

The probe asked whether the "peer compatibility session" is avoidable, and answered
`live_session_with_that_uid=0 -> load-bearing`. **That verdict is an ARTIFACT of a second bug.**

There are exactly THREE sites that stamp `sessionUID` onto a session:

| site | path |
|---|---|
| :36071 | the **SYNC** control-slot open — **but that path REJECTS remote opens** (*"remote command session open must use async local-control path"*, :36040) |
| :36853 | basebackup RECEIVE |
| :40254 | the peer command-session open (node B side) |

Our client's open is REMOTE (`destNode=1`), so it takes the **ASYNC** path — `TupleSinkServiceCreateSession(
sessionState, &asyncOp->request.sessionKey, 0)` at **:37601 — which never stamps the uid.** So node A's
command session carries `sessionUID = 0` while the uid is on the wire, in the log, and threaded to node B.

The comment at :36071 even says *"farnet0 records it here from the client's OpenSession"* — its author
believed the sync path handled it. Remote opens do not go through it.

**So the peer-open's owner lookup cannot find the session, and mints a compatibility session instead.**
The compat session is NOT load-bearing: it is the downstream symptom of a missing one-line stamp.

### 19.4 BONUS — the §10k stall is now LOCALIZED, and it is not what we assumed

Node A's completion timestamps:

```
1st SQL_EXECUTE   03:41:01.737
2nd SQL_EXECUTE   03:41:03.859   <-- 2.12 SECONDS later
3rd,4th,5th       03.859 - 03.863  (4 ms)
close (kind=8)    03.876
```

`latency average = 431 ms` is **ONE 2.12-second stall averaged over five transactions**, not five slow ones.
And the peer-provisioned result relay ARMS at **03:41:03.859 — exactly when the stall ends**
(`armed peer-provisioned P3 result receive relay session=2 sink=1`).

**The stall IS the result peer-open failing to arrive/arm for 2.1 s.** The instant it arms, all five results
flush in 4 ms and the run completes. There is exactly ONE armed relay per run, and it lives ~31 ms.

This reframes §10k entirely: it is not a per-command grant/scheduling stall in the DMA engine. It is a
ONE-TIME rendezvous stall in the result peer-open. **Do not chase the engine's collector arming.**

### 19.5 Rules re-earned (the hard way, again)

- **Absence of a log line is not absence of the event.** I inferred "the close is never sent" from a literal
  that node B never prints. Written one section after I wrote the rule. The probe cost one cycle; believing
  the inference would have cost a wrong fix in the close path — the exact area that has already bitten 3x.
- **Ask before instrumenting.** The Codex sweep eliminated the whole role-6 chain and corrected the pgbench
  command mix, turning a 3-instrument probe into a 2-instrument one that answered everything in one run.
- **A probe's verdict can be an artifact of a second bug.** `live_session_with_that_uid=0` read as
  "the compat session is load-bearing". It was really "the uid was never stored". Always ask what ELSE
  could make the measurement read that way.

---

## 20. P5.d LANDED — the close now actually closes, ONE session, zero failure machinery (citus b19e0d663)

### 20.1 The four coupled defects, and the one shape behind them

| # | defect | fix |
|---|---|---|
| 1 | **`sessionUID` was never STORED on node A's command session.** The SYNC control-slot open stamps it (:36071 — whose comment even claims *"farnet0 records it here from the client's OpenSession"*), but a REMOTE open is REJECTED by that path (*"must use async local-control path"*, :36040) and lands in the ASYNC one, which omitted the stamp. The uid was on the wire, in the log, and threaded to node B — while the session that owned it locally held **0**. | stamp it in the async path |
| 2 | **The result peer-open decided OWNERSHIP with a connection-POOLING predicate.** Unable to find the owner, it called `FindReusableSession` -> `AllowsReuse` (base-compat AND !FORCE_FRESH AND !DO_NOT_REUSE AND !mailbox-busy AND !cmd-in-flight ...) — which answers *"is this idle pooled session safe to hand to a NEW client?"*, a different question. It minted a throwaway **"peer compatibility session"**: a SECOND service session, same client, same uid, same key, owning the result stream, named by no close path. | look the owner up **by uid**; fall back to pooling only when there is no uid |
| 3 | **The lifecycle `CLOSE_SESSION` was SKIPPED whenever the session was terminal** — i.e. on every successful run, because step 1 makes the backend exit and the backend correctly answers `DO_NOT_REUSE`. Both stated reasons were wrong: *"a dead responder"* conflates the **BACKEND** with the **SERVICE** (a control-slot request is answered by the DPU service, which is alive and is the process that must reclaim the session); *"the busy-control-slot refusal"* is the bug **P5.b(2) already fixed**. And a SESSION close must close **the sinks bound to it** — only the sink-level branch ever initiated the peer close, so the session-level close then rejected with *"exact sinks are still active"*. | send it; release bound sinks |
| 4 | **ORDER: the role-7 unbind ran BEFORE the lifecycle close.** The unbind detaches the result ring's export, and node A reports a host-consumer detach as a payload-protocol FAILURE (correct for a client that vanishes mid-stream). It beat `CLOSE_SESSION` to the service by **~5 ms**, every run. The old comment justified unbind-before-close as *"the DPU must still hold this client's import while it tears the ring down"* — real, but it does NOT order these two: the import dies in **STEP 4**. | close the session first; the detach then finds nothing bound |

**And the family's signature move, one level up.** Once the close ran, the abort STILL said
`REAL PEER FAILURE ... clean_close=0`. `peer-reset-begin` DERIVES cleanliness by looking the owner session
up — but by then **the close has destroyed it**, so `ownerSession == NULL` was read as *"NOT a clean close"*
when it means *"the owner already left"*. Cleanliness is now a **LATCH stamped on the STREAM** at the moment
the owner asks to close; the reset-begin derivation may only ever set it, never clear it.

> *A missing thing must never read as a definite negative.* That is the same sentence as
> "a zeroed struct lies", "silence is a valid state", and "an un-armable collector looks idle".

### 20.2 P5 IS CLOSED — all six §12.2 criteria, WITH THE NODE NAMED ON EVERY ROW

Three consecutive `pgbench --homer --homer-dpu-command` runs, one stack, **no baseline reset**:

| # | criterion | node A (farnet0 DPU) | node B (farnet1 DPU) |
|---|---|---|---|
| 1 | `teardown T1..T4`, in order | n/a | **PASS** (3x T4) |
| 2 | `BACKEND_SLOT_RELEASE` | n/a | **PASS** |
| 3 | NO surviving `remote exec backend` | **PASS** (0, by `/proc/<pid>/exe`) | |
| 4 | consecutive runs, NO baseline reset | **PASS** (3 back-to-back) | |
| 5 | ZERO failure machinery on a clean close | **PASS (0/0/0/0)** | **PASS (0)** |
| 6 | rc=0 **and** empty error stream | **PASS** (3x, 5/5 tx, 5 decoded rows each) | |

Plus, new and specific to P5.d: **3x "bound to the OWNING session by sessionUID"**, **0 compatibility
sessions**, **3x control-slot `CLOSE_SESSION` received**, **3x `CLEAN-CLOSE RECLAIM`**. All 7 smoke targets
build; `homer_tuple_deform_smoke` ALL PASS.

**§15.3's table is superseded by this one.** Its row 5 said PASS on evidence from one node.

### 20.3 Rules earned

- **A criterion checked on one end of a two-ended path is not checked.** Every acceptance row now names the
  node. This cost a whole phase of false confidence.
- **Absence of a log line is not absence of the event.** I concluded "the close is never sent" from a literal
  node B never prints — one section after writing the rule against it. Only the probe caught it.
- **Ask an analyst before you instrument.** The Codex sweep corrected my premise (pgbench sends
  `CLIENT_SQL_TX_BEGIN -> 5x SQL_EXECUTE -> TX_COMMIT` + a warm-up `TX_ABORT`, not just `SQL_EXECUTE`) and
  eliminated the entire role-6 chain by reading, turning a 3-instrument probe into a 2-instrument one that
  answered everything in a single run.
- **A probe's verdict can be an artifact of a second bug.** `live_session_with_that_uid=0` read as
  "the compatibility session is load-bearing". It really meant "the uid was never stored."
- **Ownership is IDENTITY, not policy.** A reuse/pooling predicate must never be asked *"who owns this?"*.

### 20.4 Still open (unchanged by P5.d)

- **§10k, REFRAMED (see §19.4): the stall is ONE ~2.1 s wait for the result peer-open to arm**, not a
  per-command engine grant stall. All five results flush in 4 ms once it arms. **Do not chase the DMA
  engine's collector arming.** This is now the top perf item; `p3trace`/`p2diag` must be stripped first.
- **P4.1** — mirror-path truth-source. Fully designed (§17.3), not started.
- **One export instead of two** (§18.8) — deferred, not ignored.

---

## 21. P6 — ONE EXPORT PER SESSION (design of record + THE CONTRACT, July 12, 2026)

### 21.1 THE CONTRACT (owner-stated; this is the rule, not an optimization)

> **Homer's user has DB semantics. It knows, AT OPEN, what rings a session of a given kind needs.
> So a session BINDS EVERY RING IT NEEDS, IN ONE GO, IN ONE EXPORT.**
>
> The SESSION KIND determines the RING SET. There is never a reason to discover a session's rings
> later, and therefore never a reason for a second export, a second setup handshake, or a rendezvous
> protocol to glue them back together.

Vocabulary, because conflating these is what produced the bug:

- an **EXPORT** is a DOCA mmap export of one host memory REGION plus its ring descriptors, shipped over
  the TCP setup socket; the DPU **IMPORTS** it. The postmaster's **frontend arena** is the same mechanism
  for socketless backends: **ONE region, 48 arena rings + 1 spawn ring, ONE import.**
- a **BIND** is the session<->ring relationship. **A session binds all the rings it needs.**

The arena already obeys the contract. The client does not.

### 21.2 What the client does today (and what it costs)

A selected-DPU SQL client makes **TWO** exports:

| | export A (command) | export B (result) |
|---|---|---|
| descriptor[0] | role 1 `FRONTEND_CONTROL_SLOT` | role 1 `FRONTEND_CONTROL_SLOT` **(a SECOND, redundant control slot)** |
| descriptor[1] | role 6 `FRONTEND_COMPLETION_EVENT` | role 7 `PAYLOAD_BYTE_RING_DPU_TO_HOST` (10 MiB) |
| own TCP setup connection to :9727 | yes | yes |
| own bridge generation / mmap import | yes | yes |
| own **detach** at teardown | yes | yes |

homer_client.c:69-78 justifies the split: *"the two are bound by sessionUID, not by sharing an export.
hostMmapImportCapacity is 1024, so two imports per client is free."* **Free in capacity, not in lifecycle.**
It costs:

1. **A redundant `FRONTEND_CONTROL_SLOT`.** Export B ships a whole second control slot the session never uses.
2. **A second teardown edge.** Export B's detach is what P5.d had to defuse — we made the service treat it as
   expected; we did not remove it.
3. **A rendezvous protocol that exists ONLY to glue the two back together.** The role-7
   `OP_TUPLE_SINK` OPEN_SESSION (tuple_sink_service_process.c:36127-36205) **creates no session** -- it
   returns `serviceSessionId=0, serviceSinkId=0` and a zeroed queue descriptor. Its entire job is to tell
   the DPU *"this ring belongs to sessionUID X"*.
4. **`sessionUID` doing a job it should not have.** It is a cross-node SESSION identity, but because the
   ring lives in an export the session does not own, it is ALSO the ring-lookup key
   (`HomerDpuDmaFindDpuToHostByteRingRef` scans for it). That overload is precisely how it became
   load-bearing in the P5.d bug -- where it turned out never to have been STORED.

### 21.3 The change

**ONE export, THREE descriptors: role 1 (control slot) + role 6 (completion events) + role 7 (result byte
ring).** Everything in export B except the role-7 ring is redundant, and the role-7 ring belongs in the
session's own region.

**No bridge ABI bump is needed.** `ringCount` / `descriptorCount` are already RUNTIME fields of
`HomerDpuBridgeControlBlockHeader` -- the arena export ships **49** rings through the same path. Only
`HOMER_CLIENT_DPU_COMMAND_SETUP_RING_COUNT` goes 2 -> 3. (`HOMER_DPU_BRIDGE_PROTOCOL_VERSION` stays 5, and
the DPUs do NOT need a protocol-driven redeploy -- though they do need the rebuild, as always.)

What is DELETED, not fixed:

- export B in its entirety: its setup connection, its bridge generation, its mmap export/import, its
  redundant role-1 control slot, and **its detach edge**;
- the role-7 rendezvous `OPEN_SESSION` -- client side (the submit) and service side (the whole
  `clientSqlResultOpen && clientSqlResultDpuRelay && direction == RECEIVE` branch);
- the role-7 unbind step in the close (there is no separate export left to tear down);
- `HomerClientBindResultRing` as a separate lifecycle step: the ring is bound when
  `HomerClientOpenSqlSessionSelectedDpu` returns, because the SESSION KIND said it would be.

**Basebackup exports are UNTOUCHED** (`HOMER_CLIENT_DPU_SETUP_RING_COUNT` stays 2). This change is scoped to
the selected-DPU SQL session, which is the one whose ring set the session kind fully determines today.
Basebackup should follow the same contract later; that is a separate step.

### 21.4 Acceptance

1. `pgbench --homer --homer-dpu-command`: three consecutive runs, one stack, no baseline reset -- 5/5 tx,
   five decoded rows each, empty error streams, zero surviving backends.
2. **Node A logs exactly ONE `setup import` and ONE `host-detached` per client** (it logs two today).
3. **ZERO `dpu control slot: OPEN_SESSION opKind=1`** (the rendezvous is gone).
4. Failure machinery stays at zero on BOTH nodes; `CLEAN-CLOSE RECLAIM` still fires (P5.d must not regress).
5. All 7 smoke targets build; `homer_tuple_deform_smoke` ALL PASS.
