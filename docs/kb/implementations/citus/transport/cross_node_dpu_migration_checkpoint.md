# Cross-Node DPU-Offload Migration — Implementation Checkpoint

## Scope

Tracks the cross-node DPU-offload migration for the two Homer workloads
(`pgbench --homer` and `pg_basebackup`), where **both** the sender and receiver
sides are DPU-offloaded and the host-service / single-machine variants are
deprecated. Design source: the standalone migration plan
("Cross-Node DPU-Offload Migration — pgbench + basebackup").

The one genuinely-new engine primitive is the **DPU→host byte-ring DMA-write
plane** — the reverse of the existing host→DPU DMA-pull. Everything else is a
role-swap + wiring of the already-built RDMA byte-ring substrate (the receive
ring is already RDMA-`REMOTE_WRITE`-registered and advertised in the peer OPEN
response; running the service on the DPU makes that memory DPU-resident for free).

Prior, separate work (committed): the selected-DPU close lifecycle (Stage
10.0/10.1) — see `dpu_dma_backend_homer_service_implementation_checkpoint.md`.
The legacy peer `POLL_COMMAND_COMPLETION` removal — see
`peer_client_completion_v27_checkpoint.md`.

## Organizing invariant (from the user's zero-copy correction)

A Homer byte ring is always a producer/consumer pair where **one end is DMA and
the other is RDMA**, and neither the ring's backing memory nor its tail is ever
copied — the DMA/RDMA engine advances the frontier *in place*. "A producer must
never overtake the consumer" is the back-pressure.

- **Sender ring** (host→DPU, existing): producer = DMA-pull/read (the DMA read
  advances the tail); consumer = RDMA egress.
- **Receiver landing ring** (DPU-resident, new): producer = inbound peer RDMA
  writes; consumer = DOCA DMA-read → host sink. One ring, doubly registered
  (DOCA-mmap source **and** RDMA-`REMOTE_WRITE` target). **No staging copy.**
- **Host consumer ring** (host-resident, new): producer = DPU DMA-write;
  consumer = host frontend consume API.

Two back-pressure loops on the receive path:
- **Loop 1** — inbound RDMA producer vs. DMA-to-host consumer on the landing
  ring: the DPU credits the far sender only up to what it has DMA'd to the host.
- **Loop 2** — DPU DMA-write producer vs. host consumer on the host ring: the
  DPU throttles its DMA-write on the host's published consumedHead.

## P0a — DPU→host byte-ring DMA-write primitive (DONE, committed `5ee6dca3a`)

The three-task ordered publication `HomerDpuDmaSubmitByteRingWrite`
(`homer_service_dpu_dma.c`): payload body → produced-tail body → produced-tail
publish, each armed from the prior's completion on
`doca_dma_set_ordered_completions(ctx,1)`. New ABI role
`HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_PAYLOAD_BYTE_RING_DPU_TO_HOST = 7`, new task
kinds `BYTE_RING_WRITE_BODY/_PRODUCED_TAIL_BODY/_PRODUCED_TAIL_PUBLISH`, new ready
queue `DPU_TO_HOST_PAYLOAD`. Validated by the DPU TCP transport smoke's
`--expect-dpu-to-host-payload` leg (byte-for-byte match against a zero-filled
ring simultaneously proves the ordering invariant). Two bugs the smoke caught and
fixed before commit: a ring-wrap regression (each byte ring must state its own
extent) and a close-teardown leak (the write task kinds were missing from the
`taskKind→owner-index` switch).

## R1 — zero-copy rework of the DPU→host write primitive (in progress)

**Why:** P0a staged the payload through a `memcpy` into a byteStreamBuffer before
DMAing to the host. The user corrected this: the DPU landing ring must be the
DMA source *directly* (see the organizing invariant). R1 makes
`HomerDpuDmaSubmitByteRingWrite` source the payload body straight from a new
engine-owned **landing region**, with no staging copy.

Changes (`homer_service_dpu_dma.{c,h}`, `homer_dpu_tcp_transport_smoke.c`):
- New engine landing region: `engine->landingRegion` (posix_memalign'd) wrapped
  in `engine->landingMmap` (DOCA `LOCAL_READ_WRITE`), mirroring the
  `byteStreamBuffers`/`localByteStreamMmap` lifecycle. Config
  `landingRegionBytes` (default 8 MiB = the CLAUDE.md basebackup ring default).
  Accessor `HomerDpuDmaGetLandingRegionMemory` (so the service can additionally
  RDMA-register the same memory `REMOTE_WRITE` in P0b).
- `HomerDpuDmaSubmitByteRingWrite` dropped its `srcBytes` param (now
  `(engine, descriptorRef, byteCount, absoluteStart, ...)`); the payload sources
  from `landingRegion + (absoluteStart % ringStorageBytes)`. The byteStreamBuffer
  is retained only to stage the small `DpuCreditLine` for tasks #2/#3.
- The byte-ring task builder's WRITE_BODY source is the landing mmap/address
  (3-way source-mmap select: PULL→host, WRITE_BODY→landing, CREDIT→staging); the
  staging-buffer bounds check is exempted for WRITE_BODY (payload no longer
  touches the byteBuffer and may exceed 64 KiB); guards added so the ring cannot
  exceed the landing region.
- Smoke `--expect-dpu-to-host-payload` leg now writes the synthetic pattern
  directly into the landing region (standing in for peer RDMA) then calls the new
  signature — the exact P0b/P1 receive path minus the fabric.

**Design decisions recorded:**
- **Zero-copy landing source, not a staging copy** (user-directed). One extra
  DPU-local copy per relay is *not* acceptable; the landing region is both the
  RDMA target and the DMA source.
- **Single engine-owned landing region for the first milestone.** One contiguous
  landing region per engine (sized to the largest receive ring). Multi-stream
  concurrent receivers = a landing-region pool, deferred. Noted here so a future
  multi-receiver run knows to lift this.
- **DMA-to-host for basebackup blackhole, minimal host consumer** (user-directed,
  resolving the P1 fork). The receiver DMA-delivers landed bytes to a host
  frontend ring; the blackhole consumer just calls the Homer consume API to
  advance + discard. This keeps P1 as the cheapest end-to-end validator of the
  DMA-write plane (rather than blackholing on the DPU, which would leave the
  plane unexercised until cross-node pgbench).

**Validation:** compiles clean on farnet1 (x86, full DOCA path — service +
smoke). DPU TCP transport smoke (July 4, 2026, native aarch64 DPU build, server
on the farnet1 DPU + client on farnet1, fabric-free — DOCA DMA over the TCP setup
socket, no RDMA): **PASS**. The reworked zero-copy leg reported
`client observed DMA DPU-to-host payload produced_tail=48 epoch=1` with a
byte-for-byte content match (against a zero-filled host ring, so the match also
proves the ordering gate). All pre-existing legs passed with no regression from
the dropped-`srcBytes` signature / 3-way source-mmap change (byte-ring pull
`consumed_head=160 completed_tail=160`, backend command/response publish, backend
completion pull). Clean close: `inflight=0 free_task_slots=3072/3072`, import
host-detached, `tasks=16` complete, no stuck-inflight, no fatal, both sides `ok`.
Caveat: this is the DOCA-DMA/TCP-setup tier only; the RDMA peer path that will
actually land bytes into the landing region is validated by P0b/P1.

## P0b — RDMA landing + credit loops + setup (in progress)

Builds on R1. Components: (2) the service receive ring backed by the engine
landing region, RDMA-`REMOTE_WRITE`-registered + DMA-source, advertised in the
peer OPEN response; (Loop 2) activate the host-publish-line read for role-7 rings
(`HomerDpuDmaDescriptorUsesHostPublishLine`) + a back-pressure gate on
`acceptedPublishedTail` in the write submit; the receiver relay (submit a write
when the far sender's RDMA advances the landing tail); (Loop 1) credit the far
sender up to `writtenTail`; and a `receiverCloseReleased` lifecycle gate mirroring
`senderCloseReleased` (default released, held at bind, released at binding-clear).

### Engine Loop-2 back-pressure (DONE + DPU-smoke validated July 5, 2026)

Three edits in `homer_service_dpu_dma.c`, isolating the *write-side* gate from the
existing pull-side machinery so the DPU→host relay reacts to the host consumer's
`consumedHead` without ever being scheduled off it:

1. `HomerDpuDmaDescriptorUsesHostPublishLine` (`:4726`) now returns true for role 7
   (`PAYLOAD_BYTE_RING_DPU_TO_HOST`) — so grouped-control DMA-reads the host publish
   line for these rings. Inverted role vs host→DPU: the HOST is the consumer, so the
   line's frontier field carries the host's `consumedHead`, landed into
   `ringRuntime->acceptedPublishedTail`.
2. `HomerDpuDmaAcceptGroupedControlSnapshot` (`:8815`) advances `acceptedPublishedTail`
   for a role-7 ring but **deliberately skips the discovered-ready enqueue**
   (`!HomerDpuDmaDescriptorIsDpuToHostByteRing(...)` guard). A DPU-produced ring is
   service-driven (`HomerServicePump*` submits the write on landed-tail advance), NOT
   pull-ready-queue-driven — it must never land on a host-produced pull queue.
3. `HomerDpuDmaSubmitByteRingWrite` (`:3070`) gates on the producible window: if
   `newProducedTail > acceptedPublishedTail + ringStorageBytes` it **defers**
   (retryable: `emptyPolls=1, stillReady=true`, returns true) rather than errors.
   Before the host publishes anything `acceptedPublishedTail == 0`, so the DPU may
   fill the ring exactly once (up to `ringStorageBytes`) — correct, since the host
   must consume before the producer wraps onto unconsumed bytes.

**Smoke design decision — mid-stream round-trip, new ring 5, dual-role flag.** The
`homer_dpu_tcp_transport_smoke` gains a Loop-2 leg on a new small DPU→host ring
(index 5, `LOOP2_RING_BYTES`), gated by `--expect-loop2-backpressure` on BOTH
roles. A server-only proof was rejected: pre-publishing `consumedHead` at setup
fails because the earlier pull-leg grouped-control read (which now covers role-7
rings) would raise ring-5's `acceptedPublishedTail` prematurely and the defer would
never fire; the `publishedEpoch == acceptedPublishedEpoch → early-return` gate is
what keeps the zero-initialized ring inert until the host bumps its epoch. So the
leg is a genuine round-trip mirroring the real receiver: DPU fills to boundary
(write A, epoch 1) → attempts a wrap write that **DEFERS** (asserted) → host
consumes `LOOP2_WRAP_BYTES` and publishes `consumedHead` (epoch 1) on ring-5's host
publish line → DPU grouped-control-reads it → retried wrap write **PROCEEDS**
(epoch 2) → host validates post-wrap ring content by absolute offset
(`pattern(a) = base + a`, physical `= offset + a % ringBytes`). The leg is skipped
entirely when the flag is absent on either side (no regression to existing
invocations), because the DPU leg blocks on the host publish.

**Validation (July 5, 2026, DPU aarch64 native TCP smoke, fabric-free): PASS.** The
defer/credit/proceed evidence appeared in the required cross-process order — DPU
`server Loop-2 wrap write deferred as expected` → host `client Loop-2 published
consumedHead=16` → DPU `server Loop-2 wrap write proceeded after host consumedHead
credit` → host `client Loop-2 back-pressure proceeded: produced_tail=80 epoch=2`
(post-wrap bytes validated by absolute offset). No regression: the setup ack now
reports `rings=6` and the ring-4 single-shot leg still reports `produced_tail=48
epoch=1`; all pre-existing legs (backend command/response/completion, byte-ring
pull) still observed; clean close (`free_task_slots=3072/3072`, host-detached,
`tasks=28`, both sides `ok`). The DPU base was verified byte-identical to farnet1
committed `da1629847` for both changed files before syncing the uncommitted delta.

### P0b Design B — landing-region / receive-ring reconciliation (DONE + DPU-smoke validated)

**Decision (user-approved July 5, 2026).** R1 defined the engine landing region as a
*flat, header-less payload buffer* (DMA source = `landingRegion + ringOffset`, offset 0
= payload byte 0). The service receive ring is `control | byteStorage`, and the **far
sender RDMA-writes payload into byteStorage AND `publishedTail` into the control block at
`ringAddress`** — one contiguous RDMA-registered region, control at the front, storage at
`ringAddress + controlBytes` (`HomerServiceValidatePeerByteRingTailTarget` /
`HomerServicePayloadByteRingStorageAddress`, `tuple_sink_service_process.c`). Zero-copy
relay forces the DMA-source and the RDMA-target to be the *same* bytes, so the two layouts
must reconcile. To keep the **sender unchanged** (plan mandate), chose **Design B**: back
the receive ring's *whole* `control | byteStorage` mapping onto the landing region, and add
an engine `landingPayloadOffset` (= `sizeof(control)`) so the WRITE_BODY source is
`landingRegion + landingPayloadOffset + ringOffset`. Rejected **A** (decouple control +
IMM-derived tail — changes the sender/wire) and **C** (per-stream arbitrary DMA source —
larger engine API, replaces R1's single-landing-region model).

**Engine change (citus, DONE):** `HomerDpuDmaEngineConfig.landingPayloadOffset` +
engine field (default 0 keeps the smoke and the host→DPU pull correct); WRITE_BODY source
and both landing-bound guards (`HomerDpuDmaSubmitByteRingWrite` covers-ring guard + the
per-task source bound) add the offset; `HOMER_DPU_DMA_DEFAULT_LANDING_REGION_BYTES` bumped
to `8 MiB + 64 KiB` (Design B needs `sizeof(control) + ringBytes`, not just `ringBytes`).
**Validation:** DPU aarch64 TCP smoke PASS with a nonzero offset (128) configured on the
engine and the synthetic fills shifted to match — a wrong offset would relay the
zero-filled `[0,128)` prefix and fail the client byte-pattern checks, so PASS *proves* the
arithmetic; all pre-existing legs + the Loop-2 round-trip still pass, clean close.

**Service side (NEXT, validated by P1):** back `localReceiveByteRing.mappingAddress` with
`HomerDpuDmaGetLandingRegionMemory` in the DPU case (a `backedByLandingRegion` flag so
`HomerServiceResetPayloadStreamEntry` does not `free()` the engine-owned buffer); set
`landingPayloadOffset = sizeof(CitusHomerPayloadByteRingControl)` at engine create; the
RDMA registration (`:16241`, already `true`) and OPEN advertisement (`:25742`) are
unchanged.

The receiver relay, Loop-1 credit tie, and `receiverCloseReleased` lifecycle are
validated end-to-end by P1.

## P1 — basebackup receiver DPU offload (Gaps 1–3 DONE + compile-clean; e2e blocked on launcher)

Sender unchanged (pg_basebackup → farnet1 DPU pull + RDMA egress). Receiver:
farnet0 DPU service RDMAs into the landing region, DMA-writes to a host consumer
ring drained by a minimal farnet0 host sink (Homer consume API, blackhole).
First cross-node milestone; validates P0b's full receive path.

**Gap 1 — receive-consume client functions: DONE** (citus `55e944cf6`).
`HomerClientOpenBaseBackupReceiveStreamSelectedDpu` + `HomerClientPollBaseBackupReceive`
(`homer_client.c`), role-7 `PAYLOAD_BYTE_RING_DPU_TO_HOST`, `direction=RECEIVE`. No caller yet.

**Gap 2 — receiver DPU byte-range relay + workload-agnostic RECEIVE-bind: DONE** (citus
`2459afa14`). SINGLE-RING regime. Engine helpers `HomerDpuDmaFindDpuToHostByteRingRef` (resolve
the one role-7 ring by role — a command-less receive consumer has no bridgeGeneration to key on)
and `HomerDpuDmaGetByteRingWriteFrontier` (writtenTail + writeInFlight + host storage). Service
`HomerServicePumpIncomingByteRingDpuRelay`, reached by an early branch in
`HomerServicePumpIncomingByteRingPayload` gated on `BASE_BACKUP_STREAM` + `UsesDpuMirrorSource`:
submits one contiguous `[writtenTail, publishedTail)` segment per pass (split only at the shared
ring wrap), credits the far sender at the completed `writtenTail` (overwrite-safe because the
sender publishes only at record boundaries), keeps the stream hot via `progressResult->stillReady`.
No in-pump parse — close is CLOSE_SINK-driven and the anchor is byte conservation; the DPU
object-parse (`objects=N`) is a deferred, additive side-reader. RECEIVE-bind:
`HomerServiceFindPeerBoundReceiveRelayStream` attaches the receive-consume client to the
peer-provisioned stream by the STANDARD session identity (base compat + peer-bound + byte-ring),
replacing the Citus-COPY sinkKey exact-attach for the basebackup family; sets
`relayConsumerAttached` (the relay rendezvous gate). New stream fields relayConsumerAttached /
relayTargetResolved / relayTargetDescriptorRef / relayCreditedTail.

**Gap 3 — receiver close twin: DONE** (citus `50efa0dbf`). Import `receiverCloseReleased`
(default-released, same polarity as `senderCloseReleased`); `ReclaimDetachedImports` skips
`!receiverCloseReleased` and adds a role-7 relay-drain gate (`writtenTail == publishedProducedTail
&& !byteRingWriteInFlight`, inlined role check so the non-DOCA path avoids the DOCA-only
predicate); role-based `HoldImportForReceiverClose` / `MarkImportReceiverCloseReleased`. Service
holds at the RECEIVE-bind and releases at `HomerServiceClearPayloadStreamPeerBinding` (reached only
once `HomerServiceReceivePayloadTransportIsDrained`, i.e. consumedHead == landingFinalTail); relay
forces the final credit while `receivedPeerClosePending` so consumedHead reaches landingFinalTail.
Composition: relay drains → IsDrained → close advances → RECLAIMABLE → binding clear releases the
hold → reclaim still waits for the last produced-tail publish → free.

**BLOCKER — farnet0 receive-consume LAUNCHER (design decision, raised to user).** Gap 1's client
has no caller. On the DPU the relay early branch idles until `relayConsumerAttached` is set by a
RECEIVE OpenSession, so without a running host consumer the far sender's landed bytes are never
relayed/credited and basebackup would hang. Need a minimal farnet0 host process driving the Gap 1
client (open receive session → export consumer ring → poll-blackhole → close). User preference:
reuse existing basebackup binary/code, not a new standalone binary. Options: (a) `--homer-receive`
consume mode on `pg_basebackup`; (b) receive-consume mode on `homer_dpu_tcp_transport_smoke`;
(c) minimal standalone receiver. Also a runbook same-storage geometry constraint (host consumer
ring storage == landing ring storage) for the position-preserving relay.

### Design note — receive EOS is an explicit terminal signal (strands + all-of gate)

The host consumer cannot INFER end-of-stream: only the DPU knows all three of (1) the far sender
sent CLOSE_SINK, (2) landingFinalTail is frozen, (3) every landed byte has been relayed
(writtenTail == landingFinalTail, no write in flight). So the DPU must emit an EXPLICIT terminal
signal — the same rationale that justifies CLOSE_ACK (the host cannot infer when the DPU finished
detaching, so the DPU explicitly ACKs). EOS and CLOSE_ACK are TWO explicit signals, not one:
EOS ("stream done") is what lets the host START its close; CLOSE_ACK ("detach done") ENDS it. Under
the current host-initiated close handshake they cannot merge (ordering: the host needs EOS to
initiate close, and CLOSE_ACK responds to that initiation). The one way to merge them is to REVERSE
the handshake for the receiver — the DPU, which owns EOS knowledge, initiates the close-request and
the host tears down + acks — but that needs a DPU->host push on the setup socket (today it is
request/response, host-initiated). Deferred; the milestone uses a separate EOS signal + the existing
host-initiated close.

The three conditions above are precisely an ALL_OF gate whose satisfaction fires the terminal
continuation. In the planned async-continuation scheduler they become independent STRANDS
(far-sender-close strand, relay-drain strand, no-inflight strand) joined by an all-of gate, with the
EOS/CLOSE_ACK emission as the joined continuation — the receive-close analogue of the sender's
command/completion two-strand join already modeled in the plan.

## Launcher + EOS terminal: DONE (blocker above resolved)

- **EOS terminal (citus `fb7b50601`):** `HomerDpuDmaSubmitByteRingWriteTerminal` publishes the
  payload-less terminal produced-tail (entryState=CLOSED) at the current writtenTail, reusing the
  ordered tail-body→tail-publish task pair; the relay emits it once when `receivedPeerClosePending
  && writtenTail == publishedTail && !writeInFlight` (new `relayTerminalPublished` stream flag).
- **Launcher (postgres `830c3a4d216`):** `pg_basebackup --homer-receive` (option a chosen). Drives
  Gap 1's client to completion, no libpq flow. Built + linked against `libhomer_client.a` + DOCA;
  the installed `libhomer_client.a` + Homer headers were STALE (pre-Gap-1) and were
  rebuilt/reinstalled so the receive symbols resolve.

## Runbook — cross-node DPU-receiver basebackup (NEW topology; validation pending)

Sender is UNCHANGED (`pg_basebackup -t 'homer:mode=rdma,...'` on farnet1 → farnet1 DPU pull + RDMA
egress). NEW: the receiver is the **farnet0 DPU** service (not the farnet0 host service), which lands
the RDMA in DPU memory (Component 2) and relays to a farnet0 host consumer:

```sh
# farnet0 host: the receive-consume sink. Point the frontend setup at the LOCAL farnet0 DPU.
# node/db/user MUST match the sender's session identity (target node=N, backend MyDatabaseId/
# GetUserId == DBOID/USEROID); slots/bytes MUST match the sender geometry (same-storage relay).
HOMER_FRONTEND_DPU_SETUP_HOST=<farnet0 DPU addr> HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 \
  /data/dbcomm/pg-citus/bin/pg_basebackup --homer-receive \
    --homer-node 2 --homer-database-oid "$DBOID" --homer-user-oid "$USEROID" \
    --homer-slots 8 --homer-bytes 8388608
```

**Correctness anchors:** byte conservation (host `delivered_bytes` == landing tail == sender-produced
bytes) + intended-path confirmation (farnet0 DPU service log shows RDMA landing in DPU + the relay
`writtenTail`/terminal advancing, NOT a host-service blackhole; consumer logs `stream complete` +
`closed (CLOSE_ACK)`).

**OPEN QUESTION blocking the e2e run — the DPU↔DPU receive topology has never been exercised.**
The current validated basebackup DPU path is farnet1-DPU → farnet0-**HOST**-service (RDMA lands in
farnet0 host memory, `host=10.10.1.100`). The receiver relay REQUIRES the service on the farnet0
**DPU** (only the DPU has the DOCA DMA engine), so the sender must RDMA-egress to the farnet0 DPU and
both nodes' DPUs must run the peer transport (the plan's "confirm/require DPU↔DPU"). Unknowns to
resolve before a run: (1) the sender target `host=` and the receiver-service `HOMER_SERVICE_PEER_
BIND_HOST` for DPU↔DPU (DPU fast-link `10.10.1.200`/`10.10.1.201` vs host `10.10.1.100`/`.101`, same
`/24`); (2) whether farnet0 DPU-side peer-RDMA accept from the farnet1 DPU works on this fabric
(cross-lane RDMA has historically failed with transport-retry per CLAUDE.md); (3) the native DPU
rebuild of the Gap 2/3/EOS service on both DPU ARM trees. This is exploratory, not a documented
procedure — drive it interactively/diagnostically, not as a blind subagent run.

### e2e run #1 (July 5, 2026): INCONCLUSIVE — farnet0 DPU is UNPROVISIONED (hard blocker)

A run-validation subagent got through prerequisite 1 (farnet1 installed artifacts confirmed to carry
the new code — `pg_basebackup` has `homer-receive`, `libhomer_client.a` exports the receive symbols,
`citus_tuple_sink_service` has the relay) and then hit a hard deployment gap it could not resolve as
a routine recovery:

- **The farnet0 DPU (`farnet0-bf3-a`, aarch64) has NO citus build environment at all** — no
  `/home/ubuntu/citus-dbcomm` tree (`fatal: cannot change to ... No such file or directory`),
  `/home/ubuntu/dbcomm/` is empty, and no `citus_tuple_sink_service` binary anywhere on its
  filesystem. The farnet1 DPU (the SENDER, previously validated) has the full tree + expected
  in-progress diffs. So the receiver-side DPU was never provisioned: this is a first-time
  BOOTSTRAP (clone + vendor submodules + DOCA SDK wiring for that board), NOT a "sync 5 changed
  files into an existing tree" drift recovery. STEP 1 (DPU liveness gate) could not even be
  attempted — there is no receiver DPU binary to start.
- **Secondary (non-fatal):** farnet0 host's `/data/dbcomm/postgres-citus` and `/data/dbcomm/
  citus-dbcomm` are DANGLING symlinks to a removed NFS path (`/data/jason/fdl1data/dbcomm/...`),
  the stale-export trap CLAUDE.md warns about. The farnet0 host role only needs the installed
  prefix (which synced fine as a real 143M dir), so this does not block this test, but it will
  recur for any future source-tree sync to the farnet0 host.

CONCLUSION: the code side is complete + committed + buildable (farnet1 confirmed); the e2e blocker
is pure infrastructure — the farnet0 DPU must be bootstrapped with a native aarch64 build env
matching the farnet1 DPU (source + submodules + DOCA SDK) before prerequisite 3/STEP 1 can run. This
is a human infrastructure decision (how to get source onto that DPU; how to match its DOCA setup),
raised to the user.

## Operational note — DPU native build tree drift (July 4, 2026)

The DPU ARM tree `/home/ubuntu/citus-dbcomm` drifts behind farnet1 and holds the
previous session's farnet1 content as *uncommitted* working-tree diffs (it does
not receive farnet1's commits). During R1 validation the DPU HEAD was
`3fb0e7749` (5 commits behind farnet1 `5ee6dca3a`), but its working copies of the
files under test were verified **byte-identical** to farnet1's committed P0a
(`git diff` = 0 lines for all 3), a clean ancestor with no aarch64-unique work.
The safe reconciliation is: confirm byte-identity vs the farnet1 commit the DPU
should match, back up the target files, then rsync ONLY the changed files
(itemized) — do not blindly overwrite other DPU files (they carry the needed
build-dependency diffs) and do not `git reset`/`stash` the DPU tree (that would
revert its other files to the behind-HEAD state and break the build).
