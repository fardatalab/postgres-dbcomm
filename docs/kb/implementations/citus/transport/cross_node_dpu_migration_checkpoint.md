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

### e2e run #2 (July 5, 2026): PASS — P1 basebackup receiver DPU offload validated END TO END

The farnet0 DPU was bootstrapped (native aarch64 `~/dbcomm/citus-dbcomm` built against system pg16;
see the bootstrap runbook below) and the full DPU↔DPU receive path ran clean. This is the first
cross-node DPU milestone in the plan.

- **Topology exercised:** farnet1 host PG + `pg_basebackup -t 'homer:mode=rdma,host=10.10.1.200,
  port=9717,node=2,slots=4,bytes=1048576'` (sender) → farnet1 DPU pull + RDMA egress → **farnet0 DPU**
  receiver service (RDMA lands in DPU memory, relays to host) → farnet0 host `pg_basebackup
  --homer-receive` consumer. DPU↔DPU peer RDMA on the fast link (`10.10.1.201`→`10.10.1.200`,
  `enp3s0f0s0`) CONFIRMED WORKING — the plan's "confirm/require DPU↔DPU" unknown is resolved.
- **Geometry constraint (important):** the DPU landing region must hold the WHOLE receive ring
  (`slots × (header + bytes)`); default landing region is ~8.4 MB (`HOMER_DPU_DMA_DEFAULT_LANDING_
  REGION_BYTES`). The runbook's `slots=8,bytes=8388608` → ~67 MB ring → too big (and raising the
  landing region to 100 MB crashed DOCA). Use `slots=4,bytes=1048576` (~4 MB ring) for the receiver.
- **Correctness:** 3 clean measured runs (2 back-to-back, no restart), each `homer receive: stream
  complete, delivered_bytes=N` → `homer receive: closed (CLOSE_ACK)` → exit 0; sender `base backup
  completed` rc=0; byte conservation `delivered_bytes` vs `du -sb data` = consistent ~1.244% (tar
  header/padding overhead). Intended path confirmed on the farnet0 DPU log: RDMA landing in DPU +
  relay `published_tail`/terminal + the close reaching `host-detached`/`reclaiming`, NOT a
  host-service blackhole and NOT a spin.

**Bugs fixed en route to this PASS:**
1. RECEIVE OpenSession `peerEndpoint.protocolVersion` unset → rejected (client fix, `1d396e2cf`).
2. Receiver queue-descriptor fill crash for the basebackup relay (`85026213e`).
3. Diagnostic self-clobber erasing the real error (`27cab4ae7`).
4. **Rendezvous order-dependency (`4e4c8bad4`):** the relay armed only when the consumer opened AFTER
   the far-sender stream existed; in the intended consumer-first order it never armed and the relay
   hung with no data moving. Fixed by arming on the role-7 import resolve (order-independent).
5. **Close-hang (`1c6b8e7f4`) — the last blocker.** Full transfer + byte conservation succeeded but
   the consumer hung at close (`DPU close socket exchange failed`). Root cause proven by a
   close-window DOCA trace (`-DHOMER_DPU_DMA_CLOSE_DIAG`, now close-window-gated): it was NOT a DOCA
   completion problem — both grouped-control CQEs reap, `inflight→0`, context RUNNING. The wedge is
   the SEMANTIC close-drain: `HomerClientCloseBaseBackupStream` is SHARED between the SEND and
   RECEIVE-consume paths but hardcoded the `CLOSE_SESSION` request `direction = SEND`. A receive
   consumer's close therefore hit `TupleSinkServiceHandleCloseSession`'s SEND branch, which errors
   `send side was not open` (sendHandleCount==0) and returns WITHOUT clearing `receiveHandleCount`.
   The lingering `receiveHandleCount` kept `HomerServicePayloadStreamsDrainedForClosingSetup`'s
   local-handle gate non-zero, so CLOSE_ACK was never emitted and the TCP setup-close timed out. Fix:
   send `CLOSE_SESSION` with `stream->queueDescriptor.direction` (SEND for the sender, RECEIVE for the
   consumer). Client-only; the service already had the correct RECEIVE arm; sender path unchanged.
   **Lesson for the shared send/receive client helpers: any control message they emit must carry the
   stream's real direction, never a hardcoded one.**

**Follow-ups:**
- **RESOLVED — the "~50% mid-transfer flakiness" was NOT DOCA cold-start; it was our uncapped DMA
  write** (fix citus `78dda4a68`, validated 5/5 clean). The relay's DPU→host byte-ring write submitted
  a single memcpy of up to a full landing-ring-wrap segment (~MBs); the receiver DPU's device max
  memcpy buf size is **2,097,152 bytes (2 MiB)**, and relay writes that raced ahead of the consumer
  reached ~2.13 MB — just over the cap — so DOCA accepted them at submit (buffers pass bounds checks)
  but failed them at COMPLETION with `IO_FAILED`, resetting the context and aborting the transfer
  before close. The pull path never hit this (bounded by its 64 KB mirror buffer); only the zero-copy
  receive write, sourcing a contiguous landing ring, was uncapped. Fix: query
  `doca_dma_cap_task_memcpy_get_max_buf_size` at device selection, store `engine->maxDmaBufBytes`, and
  clamp every byte-ring write to it (the relay's edge-triggered loop submits the remainder). Lesson:
  the substrate advertises a per-task DMA max — query and respect it, do not assume.
- **NEXT (active) — unify the sender pull-mirror pool → a contiguous ring.** The DMA-cap fix makes the
  WRITE side adapt to the 2 MiB cap (it already sources a contiguous landing ring). The sender-side
  pull mirror is still a **pool of fixed 64 KB `byteStreamBuffers`**, so the pull is pinned at 64 KB
  and does NOT use the cap headroom — it respects the cap trivially but can't adapt to it. Unify the
  sender mirror to a contiguous ring like the receiver's landing region so the pull DMAs
  `min(contiguous-to-wrap, maxDmaBufBytes)` per task and the 64 KB slot artifact is gone. This is a
  refactor of the validated sender pull+egress path (staging, RDMA egress source, range-release/wrap
  bookkeeping) — do it as its own change with its own sender-egress byte-conservation validation.
- **Mid-transfer abort is not signalled to clients:** when a data-path DMA/RDMA fault DOES fire, the
  service tears down the transport (peer `DISCONNECTED`, session reclaimed) but sends NO protocol
  notification to the still-waiting `pg_basebackup` sender and `--homer-receive` consumer, so both
  hang until killed. A real robustness gap in the failure path (happy-path close is correct; the
  failure path needs the same "tell the peer" discipline). **Hit operationally (July 6):** a
  back-pressured sender walsender spinning in `HomerClientReserveBaseBackupRecordForObjectPayload`
  **ignores SIGTERM** — the producer-reserve busy-loop has no `CHECK_FOR_INTERRUPTS`, so a graceful
  cancel does nothing and only SIGKILL stops it (which forces a postmaster crash-recovery cycle). The
  reserve loop should poll for query cancel/terminate, not only its own terminal-stream check.
- **Wrap-gap is geometry-only — DECIDED (July 6).** Wrap-gap detection must never consult record
  *semantic identity* (ordinal/generation/kind); it is pure byte-ring geometry on both producer and
  consumer. Producers must leave a readable transport-length header in the gap (basebackup copies the
  real next-record header; the Part 3.5 DPU tuple sink producer MUST do the same, not leave stale
  bytes). The shared frontend sink core (`homer_byte_ring_sink.h`) detects gaps by geometry only; its
  content-mismatch path is legacy support for the deprecated host-service tuple producer's stale-bytes
  gaps and is removable once that path is deleted. This corrected the FAIL #2 sink-core stall (the
  extracted core inherited the tuple content-mismatch detector and was blind to basebackup's
  matching-ordinal copied-header gap). Canonical write-up + the production-side requirement live in
  `dpu_payload_byte_ring.md` ("Design principle (DECIDED — wrap is geometry-only, never content)" and
  "The frontend byte-ring sink core"). Part 3.5's `deliverObject` (host `base+offset` fixup +
  borrow-then-release) plugs into that geometry-only core.

## P1.5 — position-preserving object-agnostic egress (de-reframe): DONE + validated (July 6, 2026)

The P1 receiver validated byte CONSERVATION with a blackhole that never parsed records. The first
**parsing** consumer (the shared frontend sink core) then stalled: gdb on the live stuck receiver
showed it wedged on the first ~1 MiB archive record carrying `FRAGMENT_FIRST` (transport
`payloadBytes=1048408` vs the semantic header's `1048576`) — the sink core validates whole objects and
rejected the fragment forever → back-pressure → walsender 99.8% spin. Root cause: the **sender egress
was re-framing** — for a basebackup object whose payload was not yet fully in the DPU mirror, it emitted
multiple peer transport records, each with a freshly *generated* `CitusTupleSinkTransportHeader` +
FRAGMENT_FIRST/LAST, undone by a service-ingress reassembler. This is always-on for the cross-node
egress (mirror-range driven), NOT the `HOMER_BULK_FRAGMENT_BYTES` diagnostic knob (which was 0).

**Decision (user):** transport-level fragmentation IS intended — RDMA/DMA chunking so the scheduler can
pace and cut head-of-line blocking — but it must be **object/semantic-agnostic and position-preserving**:
move the SAME bytes in arbitrary-sized chunks to the SAME destination offsets, adding nothing (no
per-chunk header, no FRAGMENT flags, no reassembly). The byte ring always holds whole records; the
frontend always reads whole objects; the same principle applies on BOTH production (sender) and
detection (receiver), and to single-ring (basebackup) and future two-ring (tuple) alike. No branch on
object family or ring topology.

**Implemented (citus, uncommitted on `homer-dpu-migration`):**
- `HomerServicePumpOutgoingDpuMirrorByteRingPayload` (`tuple_sink_service_process.c`) is now a SINGLE
  object-agnostic byte-range relay — the RDMA mirror of the receive relay
  `HomerServicePumpIncomingByteRingDpuRelay`. It relays `[readyRange.absoluteStart, absoluteEnd)`
  verbatim to the peer ring at matching offsets (`num_sge==1`, `prepend=false`), sliced only at the ring
  wrap + scheduler/credit/substrate limits via the new shared helper `HomerByteRingRelayNextChunk`. No
  header parse, no re-framing, byte-only (frozen semantic tail; `payloadEosPosted` not set — basebackup
  close is already byte-only). Producer wrap-gap padding is relayed verbatim (source-skip deleted); the
  consumer skips it by geometry. The record-parse loop + the DPU-mirror fragment helper +
  `forcedFragmentBytes` gate were deleted (~430-line loop → ~30-line relay).
- **Detection side made purely geometric**: the shared sink core's residual content-based
  `classifyHeader` (ordinal-mismatch) wrap detector was removed (`homer_byte_ring_sink.h`,
  `homer_client.c`); the ordinal check became envelope VALIDATION inside `validateAndDeliver`. Both
  sides now use one rule set: geometry-detectable gaps + position-preserving chunks; geometry skip +
  whole-object delivery; no content/identity input to fragmentation or wrap handling.
- `HomerByteRingRelayNextChunk` is factored for reuse — routing the receive relay through it + the
  two-ring tuple path are tracked follow-ups.

**Validation (July 6, cross-node DPU-receiver basebackup e2e, both DPUs rebuilt):** PASS, reproducible.
Sender `pg_basebackup -t 'homer:mode=rdma,host=10.10.1.200,port=9717,node=2,slots=4,bytes=1048576'`
rc=0 (cold 15.15s, warm 13.83/13.81s); receiver `--homer-receive` `stream complete,
delivered_bytes≈23.2 GB` → `closed (CLOSE_ACK)` every run; **every traced record `flags=0` (no FRAGMENT
on the wire)**; ~3159 wrap gaps skipped purely by geometry; exactly 1 transient no-torn defer per run
(a chunk ended mid-record, consumer deferred one poll then delivered — the designed safety); intended
relay path (`peer-provisioned receive sink` → `payload close QUIESCED final_tail==final_head` →
`reason=peer-close-drained`), not a host-service fallback. delivered vs `du -sb data` = −0.34% (within
the ~1.24% band). The ~1 MiB records that fragmented+stalled before now arrive WHOLE — proof the
position-preserving sub-record chunking reassembles for free.

**Deferred (Stage 2-4 cleanup):** DONE in **P1.6** below — the dead fragmentation machinery on the
deprecated host-service paths was removed and the receive relay unified onto the shared chunk helper.
The diagnostic byte-ring sink trace (`HOMER_BYTE_RING_SINK_TRACE`, budget-capped) is kept **gated off by
default**.

## P1.6 — dead-fragmentation removal + relay unification: DONE + validated (July 6, 2026, citus `8f9963d33`)

P1.5 made the LIVE DPU egress position-preserving but banked the fix before removing the now-dead
fragmentation machinery on the deprecated/non-DPU paths, and left the receive relay carrying its own
copy of the chunk math. P1.6 removes the dead code and unifies the one remaining chunk computation. Net
**−522 lines** in `tuple_sink_service_process.c` (44 insertions, 575 deletions) + a reserved-macro note
in `homer_tuple_abi.h`. Behavior-neutral: the only *exercised* change is the relay refactor, which is
byte-identical to the code it replaced.

**Removed (all dead-for-the-live-path):**
- **Legacy host-service sender fragmentation** — `HomerServicePumpOutgoingByteRingPayload` no longer
  fragments; deleted `HomerServiceAppendOutgoingBaseBackupFragmentWrite`,
  `HomerServiceForcedBulkFragmentBytes` + the `HOMER_BULK_FRAGMENT_BYTES` env knob, and the
  `outgoingFragment*` stream fields. Its fragmentation was doubly-gated (`basebackup &&
  forcedFragmentBytes>0`) — a diagnostic opt-in, NOT always-on like the P1.5 egress bug — so removal
  changes no default run.
- **Receiver CPU reassembler** — `HomerServiceProcessReceivedBaseBackupTransportRecord` keeps only the
  whole-record branch + a defensive guard that turns any stray FRAGMENT flag into a hard error; the
  FIRST/continuation/LAST reassembly branches and the `reassembly*` accumulator fields of
  `HomerBaseBackupPayloadState` are gone.
- **Header-ready** — `HomerServicePayloadTransportHeaderReady` allows only `EOS`; any FRAGMENT bit is an
  error, and the FIRST-fragment min-size branch is deleted. With the reassembler guard, the receiver now
  REJECTS fragmentation structurally, so a clean full-EOS run is proof no path re-frames.
- **ABI** — `CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_FIRST/LAST` (`homer_tuple_abi.h`) kept RESERVED
  ("no longer produced") so the bit positions are not reassigned to a meaning an old peer could misread.

**Relay unification:** `HomerServicePumpIncomingByteRingDpuRelay`'s inline chunk math now calls the
shared `HomerByteRingRelayNextChunk` (factored in P1.5). The receive relay (DMA) and the RDMA egress now
share ONE position-preserving, wrap-aware, arbitrary-size chunk computation
(min of range / ring-wrap / credit / substrate-cap) — byte-identical to the prior inline math. The
relay's only credit gate at this layer is the engine's Loop-2 host back-pressure (deferred inside
submit), so the source range itself is passed as the credit bound.

**Validation (July 6, cross-node DPU-receiver basebackup e2e, both DPUs rebuilt with the new binary):**
PASS, first attempt, no DOCA cold-start flakiness. Deployment verified current (`grep -c
HomerServiceForcedBulkFragmentBytes` == 0 on both DPU sources + installed host service, fresh binaries).
Intended DPU↔DPU DOCA-DMA relay confirmed on both DPU logs (sender: persistent outgoing RDMA peer
transport + DOCA `doca_enabled=1`; receiver: `peer-provisioned receive sink`, `final receiver head WIMM
... final_tail==consumed_head==26507315464`), not a libpq/host-CPU fallback. All 3 runs reached `stream
complete` + `closed (CLOSE_ACK)` with no stall; delivered vs `du -sb data` (23,295,123,768 B): warm
repeat 23,215,190,170 B (−0.34%), bonus `bytes=65536` run 23,252,392,378 B (−0.18%), both inside the
~1.24% band. Warm repeat rc=0 at 9.40s real (primary geometry `slots=4,bytes=1048576`); the `bytes=65536`
force-chunk run also delivered whole objects to EOS (8.13s rc=0), extra confidence the shared relay
handles small sub-record segments + mid-record tails. No throughput-regression claim (no documented
reference band for this path); correctness + anti-stall + intended-path are the pass criteria.

**Remaining follow-up:** the two-ring tuple DPU path (Part 3.5 below) reusing this relay with a deform
between two byte rings. Its own effort, not part of this cleanup.

## Part 3.5 — two-ring tuple DPU path for pgbench: PLANNED; Stages A/B/C.1 done (July 7, 2026)

Full plan: `/home/jasonhu/.claude/plans/here-s-a-snippet-on-linked-papert.md`. The going-forward
DPU-offloaded tuple **result-sink**, offloading the receive-side per-tuple **decode** onto the DPU.
Shape: server backend produces packed tuple-view → farnet1 sender DPU mirror → RDMA → farnet0 receiver
DPU landing ring → **deform** → DMA source ring → DMA → client → borrow.

**Direction change that reshaped the plan (record for posterity):** the receive/consumer side was first
mis-modeled as the **Citus tuple COPY** worker backend (coordinator → socketless worker, service-passive
receive). That path is OUTDATED and not the focus. The real workload is **pgbench**: `pgbench --homer`
(a `src/bin/homer_client.c` client, NO libpq in the data path — `pgbench.c:8580`) is the CLIENT, talking
to a PostgreSQL **server backend** (`remote_execution_backend_bridge.c`, the producer) over the Homer
stack. The `citus_remote_exec_pgbench_transaction` SQL wrapper (`remote_exec_pgbench_transaction.c:324`)
is a mistaken indirection — a farnet0 backend that consumes in-process and returns a scalar over libpq —
to be RETIRED (Stage G); its TPC-B body already lives in pgbench's own Homer state machine. The two
paths' consumers differ: the Citus worker decodes to Datums via `BorrowNextRemoteTupleView`
(`homer_frontend.c:1510`, backend-only); the standalone `pgbench --homer` client currently only counts
rows (`homer_client.c:5619`) and has no DPU SQL-result consumer (only basebackup does, `:2893`).

**Deform design DECIDED (the crux):** Homer's internal/on-wire formats are Homer's own — need NOT be
PG-compatible; only the frontend `borrow` interface hands the PG user a PG-compatible tuple. TWO
Homer-owned formats: (1) **packed tuple-view** (`CitusTupleSinkBatchHeader` + MAXALIGN'd null-bitmap
rows) — the server→DPU wire + within-backend sink form (packed because varlena pointers can't be DMA'd;
the producer's `TryAppendTupleViewToCitusTupleSinkBatch` packs — minimal send-side work). (2) **decoded
delivery image** — the DPU→client payload. The deform DECODES (1)→(2) on the DPU: per tuple `nulls[]` +
`values[]` (fixed-by-value inline Datums; varlena = byte OFFSETS into a co-located per-batch varlena blob
carrying real PG varlena headers), driven purely by the on-wire `CitusTupleViewContract`
(`homer_tuple_abi.h:94-104`) so it needs no `TupleDesc`/`MemoryContext`. The client `borrow` only
FINALIZES: varlena offset → client-local pointer. Net: per-tuple decode offloaded host→DPU (the goal
itself). The decoded record is a plain variable-sized byte-ring record — fits the shared sink core +
geometry-only wrap-gap unchanged; both ends are LE 64-bit so `Datum` is byte-identical across the hop,
only pointers are non-portable (hence offsets). NOTE: the deprecated `TupleSinkServiceDecodeIncomingTuple
ViewBatch` is a same-layout re-pack (validated rebuild), NOT a decode — a reference for the attribute
walk, not the function to lift. INVARIANT: the sink must carry detoasted varlena (DPU can't detoast).

**Committed (survive the topology correction — all on send/service infra, topology-agnostic):**
- Stage A `86ac9b418` — DMA engine tuple source ring + WRITE_BODY source selector (basebackup byte-identical).
- Stage B `442e1710b` — send-side EOS byte-position mark (eosByteTail/eosRecordOrdinal, aliased in both
  control views; backend publishes at EOS; mirror egress flips payloadEosPosted). Producer = server backend.
- Stage C.1 `e1eeb07f3` — DPU tuple receive backed by the engine landing region (retire the alias model).
  Compile-clean; NOT yet runtime-validated (folds into the Part 3.5 pgbench validation).

**Remaining (net-new, receive/client side):** C.2 client-side role-7 DPU SQL-result consumer in
`homer_client.c` (adapt basebackup `:2893`); D service two-ring relay + deform (branch at TUPLE_VIEW_BATCH
receive `tuple_sink_service_process.c:31027`, parallel to basebackup `:30992`); E close/EOS (deform detects
EOS → terminal); F client borrow finalize + wire `pgbench --homer` to consume `abalance`; G retire Path B.
Validation: `pgbench --homer` remote-RDMA DPU-enabled, `abalance` correctness + intended-path proof. Large,
multi-session; committed A/B/C.1 + the plan + this note make it resumable.

**Stage D decoded-record layout DECIDED (July 7, 2026 — the plan's deferred "mechanical details"):**
The layout + its alignment math live in ONE shared, `postgres.h`-free header
`src/include/distributed/homer/homer_decoded_tuple_abi.h` with inline helpers, so the service deform
(a PG-backend TU — has `MAXALIGN`/`fetch_att`) and the client finalize (an external TU — reads `values[i]`
as `uint64` and casts, no PG macros) CANNOT disagree byte-for-byte. Same discipline as `homer_byte_ring_sink.h`.
One transport record per batch (`CitusTupleSinkTransportHeader` envelope unchanged), body =
`HomerDecodedTupleBatchHeader { protocolVersion, headerBytes, attributeCount, tupleCount, payloadBytes }`
then per tuple, each MAXALIGN(8)-aligned:
`HomerDecodedTupleHeader { decodedTupleBytes, attributeCount }` · `uint8 nulls[attributeCount]` ·
(pad 8) `uint64 values[attributeCount]` · (pad 8) varlena section.
Decisions + reasoning:
- **`values[]` is a flat Datum array** (O(1) attr access) — THIS is the offload: it eliminates the
  client's sequential MAXALIGN-stepping/length-prefix walk (the expensive part of `DecodeTupleViewFromTupleSinkRow`).
- **by-value → inline `Datum`; by-ref/varlena → byte OFFSET from the decoded-tuple start** into the
  co-located varlena section (client finalize: `values[i] = tupleStart + offset`). Offsets are unavoidable
  for by-ref (a by-ref Datum is a host pointer the DPU can't know). Both sides pick Datum-vs-offset from the
  contract's `attributeByVal`.
- **per-tuple `decodedTupleBytes` length prefix** so the client advances tuple→tuple with NO walk (keeps the
  offload total). Variable-stride ready (varlena); for pgbench int4 it's a constant ~24-byte stride.
- **varlena implemented but UNVALIDATED by the pgbench smoke** (`SELECT abalance` is a single int4, by-value).
  Copy varlena bytes VERBATIM (header included — `PG_DETOAST_DATUM_PACKED` may be short- or long-header;
  client reads via the value's own `VARSIZE_ANY`), each varlena field MAXALIGN(8) in the section. The by-ref
  arm carries a loud defensive log/guard so a future varlena workload flags "validate me" rather than silently
  trusting untested math. Rationale to implement-now-with-guard rather than scope-out: the plan designed the
  offset layout, the by-ref arm is small, and a half-path is more confusing than a guarded complete one.
- **source ring == host role-7 ring == 8 MiB** (`HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`), required because
  `HomerDpuDmaSubmitByteRingWrite(absoluteStart, TUPLE_SOURCE)` addresses source `% sourceStorage` AND host
  `% hostStorage` with ONE absolute — so **source space and host space are the same absolute space** (both
  start at 0, both advance by decoded bytes). **Landing space is separate** (advances by PACKED bytes) — the
  three-position-space decoupling. Credit far sender at landing-consumed (deform copied the bytes out), NOT
  at the DMA writtenTail (which is source→host).
- **the deform reuses `HomerByteRingSinkDrain` for its landing-ring READ loop** (proven wrap-gap geometry +
  no-torn gate + consumedHead advance); its `validateAndDeliver` deforms into the source ring and returns
  false to defer on Loop-2 source-ring-full (so consumedHead/credit naturally back-pressure the far sender).
  The deform is the SOURCE-ring PRODUCER, so it applies the producer wrap-gap protocol (pad + copy the real
  transport header into the gap) so the DMA'd bytes are geometry-skippable by the client sink core.

**Stage D+E IMPLEMENTED + committed (July 7, 2026 — citus `eacc6b559`, kb `86520bb525e`):** the shared header,
the deform (per-row + per-batch + upper-bound), and `HomerServicePumpIncomingTupleViewDpuTwoRingRelay`
(branched at the TUPLE_VIEW_BATCH DPU dispatch under `UsesDpuMirrorSource`). Compile+link clean. NOT yet
runtime-validated (folds into the pgbench e2e). One link-time lesson: the standalone service binary does NOT
link PG's error reporting, so `fetch_att` (its `default: elog(ERROR)` arm) cannot be used even on untaken
paths — by-value Datum reconstruction is via an elog-free `HomerServiceFetchByValDatum` (pure cast macros).

**Stage C.2/F integration is a DESIGN FORK, not the plan's mechanical "reuse basebackup role-7 + swap the
callback" (BLOCKED on a decision — July 7, 2026).** Mapping the client SQL-result lifecycle showed the plan's
assumption is wrong in three load-bearing ways:
- The SQL result sink is the **receive-half of the CLIENT_SQL_SESSION**, bound per-command from the completion
  view (`HomerClientOpenResultSinkFromCompletionView` `homer_client.c:5334` -> `...FromDescriptor:5139` ->
  `HomerClientRebindResultSink:5294`), NOT a standalone receive session like basebackup
  (`HomerClientOpenBaseBackupReceiveStreamSelectedDpu:2893`, which has its own OpenSession(RECEIVE)).
- The result ring is a **local host-shm ring on the client's (farnet0) machine, created by the tuple-sink
  SERVICE** (`tuple_sink_service_process.c:18433`); the farnet0 service RDMA-receives result bytes and writes
  them into that shm (service-passive), and the client just `shm_open`/`mmap`s it and polls `publishedTail`
  (`HomerClientDrainResultSinkUntil:5422`, row-count-only at `:5619`). There is no client-side network I/O,
  no DPU credit line, no exported ring on the SQL path.
- The DPU-vs-SHM selection for SQL is **server-side** (`citus.enable_experimental_homer_dpu_frontend`), and
  that path is a **not-yet-implemented bring-up stub** (`HomerFrontendDmaRaiseNotImplemented`
  `homer_frontend_dma.c:191`) on the BACKEND frontend; the standalone `pgbench --homer` client has NO DPU
  result path at all. Nothing in the pgbench/client path selects DPU -- a new gating signal is required.
So C.2/F needs BOTH (A) a new client role-7 DPU result consumer wired into the SQL session's per-command
completion flow, AND (B) making the farnet0 service's SQL-session receive stream a TUPLE_VIEW_BATCH
DPU-mirror-source stream that targets the client's exported role-7 ring (so the Stage-D relay branch fires)
instead of the service-owned shm ring, PLUS a new DPU-select signal. This touches the OpenSession ABI + the
service SQL receive-stream setup + the client drain contract -- a larger feature to align with the user
before writing it. Options on the table: (1) client exports role-7 at SQL-session-open + an OpenSession
"DPU result receive" flag drives the service to set up the landing-backed receive stream; (2) a separate
standalone DPU role-7 receive session keyed to the SQL serviceSessionId; (3) a decoupled smoke to validate
D+E + client finalize before the full pgbench integration. Stage D+E stands regardless of which is chosen.

**Smoke-first de-risking DONE (July 7, 2026 — citus `3941f57bc`; user chose smoke-first then session integration).**
The deform was extracted out of the 42k-line service file into its own linkable unit
`homer_tuple_deform.c/.h` (`HomerTupleDeformBatchToDecoded` + `HomerTupleDeformDecodedRecordUpperBound`) so the
SAME deform links into both the service relay and a standalone smoke (no drifting copy). The client FINALIZE
landed as postgres.h-free inlines in `homer_decoded_tuple_abi.h` (`HomerDecodedTupleCursor{Init,Next}`) — the
REAL Stage F cursor, shared so smoke + pgbench client run identical code. `src/bin/homer_tuple_deform_smoke.c`
(`make tuple-deform-smoke`, in `all`) builds synthetic packed batches the way the producer packs (null bitmap +
MAXALIGN(8) + store_att_byval), runs the real deform then the real finalize, and checks recovered values:
single int4 (negatives + NULL), int4/int8/int2 alignment + middle NULL, 0-row EOS batch, and the upper bound —
**ALL PASS**. This validates the byte-layout agreement (the highest-risk novelty) on CPU before any 3-machine
run. Build wiring: deform added to `HOMER_SERVICE_OBJS` (kept out of citus.so) + the service-bin recipe.
**pgbench --homer-dpu integration IN PROGRESS (July 7, 2026; plan approved, Option 1 session-scoped role-7).**
Design refinements locked with the user during planning: (a) basebackup and SQL results are the SAME
session-attached consumer pattern — no ownership move (the sink/landing/source rings are service-owned and
DPU-resident; the client only provides the host role-7 ring the DMA lands in, attaching at open / detaching at
close); (b) **no per-command rebind** — the result ring is a continuous decoded byte stream, delimited per
command by the EOS flag; (c) **unified EOS protocol** — in-band FLAG_EOS on the last record, race-free
(stamp-before-publish on a fresh record; the producer already piggybacks it for row-producing commands, so no
separate EOS record on pgbench's hot path), detector always checks the flag; the credit-line terminal is a
SEPARATE transport-teardown layer. Basebackup piggyback conformance is a noted follow-up (it currently uses
terminal-only). Implementation steps, each committed + building clean:
- Steps 1-3 `2b2dc2ae1` — gating flag `clientSqlResultDpuRelay` end-to-end (3 request structs + CONTROL v29 /
  PEER v17 bumps; per-stream `dpuRelayResultStream` bool ANDed into `HomerServicePayloadStreamUsesDpuMirrorSource`
  so ONLY a flagged pgbench SQL result stream relays — also closes a global-toggle footgun that would have stalled
  COPY / non-DPU pgbench tuple streams whenever the DPU scheduler was on). Flow: client OpenSqlSession -> farnet1
  session bool -> per-command result SEND peer-open -> farnet0 receive stream.
- Step 4a `f79a908b2` — relay passes recordKind==ERROR through verbatim (its body is a CitusTupleSinkErrorRecord,
  not a batch header; deforming it mismatched the contract and reset the peer).
- Step 4b `25f562ab1` — client role-7 SQL consumer in homer_client.c (adapted from the basebackup selected-DPU
  RECEIVE template): `HomerClientOpenSqlResultReceiveStreamSelectedDpu` (exports role-7 ring + OpenSession(RECEIVE)
  with the SQL command-session base-compat identity + parentServiceSessionId + clientSqlResultDpuRelay=1,
  consumer-first ids=0), `HomerClientSqlResultDeliverDecoded` (finalize via HomerDecodedTupleCursor, capture attr-0
  = abalance, EOS via flag, ERROR captured), `HomerClientPollSqlResultDpuReceive` (credit-line frontier + sink core
  + terminal). Smoke extended (TestByteRingReceiveFinalize) — value round-trips through the real sink core.
Remaining: **step 5** service RECEIVE-bind extension for `clientSqlResultOpen` — DELICATE consumer-first
rendezvous (the pgbench role-7 open ALWAYS precedes the first command's result peer-open, so
`HomerServiceFindPeerBoundReceiveRelayStream` returns NULL; must mirror basebackup's exact consumer-first behavior
to avoid double-arming the role-7 import or hanging — the relay pump self-arms via role-7 lookup, so the OpenSession
bind's job may just be "return OK, do not create a placeholder stream"; being traced before writing). Then step 6
pgbench `--homer-dpu` flag + drain routing + read abalance; step 7 the 3-machine validation; step 8 retire Path B.

**ALL CODE STEPS DONE + committed + building/installing clean on farnet1 (July 7, 2026). Only validation (step 7)
+ Path-B removal (step 8) remain.**
- Step 5 `99a9e04d3` — the planned RECEIVE-bind extension was WRONG; a consumer-first lifecycle trace showed the pgbench
  role-7 open is ALWAYS consumer-first (opens before the first result), so a relay-bind never fires, AND the existing
  clientSqlResultOpen path can't accept the DPU open at all (it validates+installs a per-command tuple contract the
  consumer lacks at open). Correct code: `TupleSinkServiceHandleOpenSession` detects the DPU consumer open
  (clientSqlResultDpuRelay && direction==RECEIVE) after the protocol checks and returns a **benign OK** naming the
  parent session, skipping contract/slot/stream machinery. The REAL relay runs on the peer-bound result stream (flagged
  via the flow, self-arming on the role-7 import in the relay pump). Client accepts serviceSessionId/SinkId=0 + zeroed
  queue descriptor (drives its own ring). LESSON: the plan's "reuse basebackup RECEIVE-bind" was the OPPOSITE of correct.
- Step 6 `75138f75c54` (postgres tree) — pgbench `--homer-dpu`: openHomerSession sets clientSqlResultDpuRelay=1 + opens
  the role-7 stream once; HomerApplyCommandCompletion captures the per-command contract from the completion view and
  drives HomerDrainPendingDpuResultRelay (EOS-delimited poll loop mirroring the shm drain control-flow contract);
  reads + logs the decoded abalance; `transport: homer-dpu` printed for intended-path proof. Requires --homer + remote peer.
- farnet1 install: citus service/client/extension + pgbench all build+install clean; installed pgbench has --homer-dpu.

**Step 7 (validation) is the next phase — a large iterative 3-machine operation:** sync artifacts to farnet0 + BOTH DPU
native-aarch64 trees (`~/dbcomm/citus-dbcomm`, rebuild `service-bin` there), bring up DPU DOCA-DMA services on farnet0 +
farnet1 DPUs (HOMER_SERVICE_ENABLE_DPU_DMA=1 etc.), start pg + Homer services on both hosts, run
`pgbench --homer-dpu` farnet0->farnet1, and confirm abalance correctness + intended-path from DPU logs (deform ran,
source ring used, role-7 targeted). Expect first-run bugs (first ever exercise of the two-ring tuple relay + deform +
client role-7 consumer over real RDMA/DMA) needing debug+fix iteration; the receiver-DPU DOCA cold-start flakiness
(~50%, restart to recover) applies. Step 8 (delete citus_remote_exec_pgbench_transaction + its UDF/extern/build refs)
comes AFTER validation confirms the new path.

## Part 3.5.1 — explicit session↔ring binding via a unique `sessionUID` (July 8, 2026; supersedes step-5 single-active)

Full plan: `/home/jasonhu/.claude/plans/here-s-a-snippet-on-linked-papert.md`. Design review of step 5's
"benign-OK" rendezvous surfaced that the DPU→host relay resolves its target host role-7 ring by ROLE ONLY
(`HomerDpuDmaFindDpuToHostByteRingRef` kept-first + WARN on >1). That is a real bug the moment two sessions each
want a role-7 ring (two concurrent `--homer-dpu` clients would cross-deliver / race a single-active guess). This
milestone replaces the role-only lookup with an EXPLICIT binding keyed on a **unique per-session id**, for BOTH
pgbench and basebackup. Runtime validation (old step 7) + Path-B removal (old step 8) resume AFTER this lands.

**`sessionKey` (NON-unique) vs `sessionUID` (UNIQUE) — the load-bearing distinction (stated in code + here):**
- `CitusRemoteExecSessionKey` (`homer_control_abi.h`) is a **NON-unique** base-compatibility/reuse key: 13
  connection/policy fields, NO instance discriminator, so several concurrently live sessions with the same
  connection identity (e.g. 4 pgbench clients on one db/user/node) deliberately SHARE one sessionKey (used by
  `TupleSinkServiceFindReusableSession`). It must NEVER bind a ring.
- `sessionUID` (NEW, `uint64`) is the **UNIQUE** per-session id a ring binds to. Client-minted, one per live
  session. It — not sessionKey, not role — is what the relay resolves on. In pgbench the uniqueness comes from the
  per-client `CState *` pointer (pid/time add cross-process entropy). The role-7 opener consumes it as its
  `dpuBridgeGeneration`, so `serviceSessionId == sessionUID` (one unique id per session, not two).

**Design principles (locked with the user):** bind on `sessionUID` never `sessionKey`; **NO role-only fallback**
anywhere once complete (an unresolvable role-7 relay is an ERROR/mis-wire, not single-active); general to any
session; **grow the descriptor** (`HomerDpuBridgeRingDescriptor.sessionUID`, bridge ABI v1→v2, assert 120→128) — do
NOT overload `serviceSessionId`.

**Steps 1-2 DONE + committed + building clean (svc + client + citus.so + postgres backend + pgbench + pg_basebackup):**
- Step 1 (ABI, inert): `sessionUID` on `HomerDpuBridgeRingDescriptor` (+comch v1→v2); SQL 5-hop carrier fields
  `sessionUID` on `HomerClientSessionOptions`, `CitusRemoteExecOpenSessionRequest` (CONTROL v29→v30),
  `TupleSinkServiceSessionState`, `CitusRemoteExecPeerOpenRequest` + `...CommandSessionRequest` (PEER v17→v18);
  `dpuRelayResultSessionUID` on `HomerServicePayloadStreamEntry`; the NON-unique comment on `CitusRemoteExecSessionKey`.
- Step 2 (SQL resolves by sessionUID): the 3 receiver DMA fns
  (`HomerDpuDmaFindDpuToHostByteRingRef` / `...HoldImportForReceiverClose` / `...MarkImportReceiverCloseReleased`)
  take a required `uint64_t sessionUID`; SQL bridge threads it client→command-session peer-open→farnet1 session
  state→result SEND peer-open→stamped on stream B (`HomerServiceCreatePayloadStreamEntry` new param); pgbench mints
  it once in `openHomerSession` into the shared `sessionOptions`; the role-7 descriptor + `dpuBridgeGeneration`
  consume it. Both relay pumps resolve via `HomerServiceResolveRelayResultSessionUID(streamEntry)`.
- **DESIGN DECISION (dual-mode migration bridge, plan-faithful):** the resolver is `sessionUID != 0` → STRICT
  unique-binding match (`matchCount>1` = hard ERROR; `matchCount==0` stays transient-retry so the role-7-open /
  relay-arm race still resolves); `sessionUID == 0` → the TEMPORARY legacy role-only scan that ONLY basebackup
  still hits until step 3 threads its sessionUID (via the pending-binding registry). The plan's own step-3 wording
  "*remove the last role-only path*" confirms role-only is meant to survive through step 2 and die in step 3 —
  this keeps every step independently buildable and never breaks basebackup mid-migration. `HomerServiceResolve
  RelayResultSessionUID` currently returns `streamEntry->dpuRelayResultSessionUID`; step 3 augments ONLY that
  helper body with `if (uid==0) uid = registryLookup(sessionKey)` and deletes the resolver's `==0` branch.

**Step 3 DONE + building clean (svc + client + citus.so + backend + pg_basebackup + pgbench):** basebackup now
binds by sessionUID with **no role-only fallback anywhere**.
- Basebackup receiver mints its own UNIQUE sessionUID = its `dpuBridgeGeneration` (already unique, == serviceSessionId)
  in `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`homer_client.c`), stamping `descriptor[1].sessionUID` and
  `request->sessionUID` on the RECEIVE-bind open.
- **Pending-binding registry** `(sessionKey → sessionUID)` on the service: a small file-scope BSS table
  `ActivePendingBindingTable[MAX_LOCAL_SESSIONS]` with `HomerServiceRegisterPendingBindingSessionUID` /
  `...Lookup...` / `...Unregister...`. Matching is **base-compatibility** (extracted into
  `HomerServiceSessionKeysBaseCompatible`, now the single base-compat definition, also used by
  `TupleSinkServiceSessionMatchesBaseCompatibility`), inheriting basebackup's pre-existing one-backup-per-base-compat
  rendezvous assumption (same as `HomerServiceFindPeerBoundReceiveRelayStream`). Registered UNCONDITIONALLY at the
  basebackup RECEIVE-bind (covers consumer-first), unregistered at the basebackup receive-handle close.
- **Resolver** `HomerServiceResolveRelayResultSessionUID(streamEntry, sessionStates)` now: stream's uid if set (SQL,
  or already-cached basebackup) → else registry lookup by the parent-session sessionKey → CACHE onto the stream → else
  0. `sessionStates` may be NULL at the close-time release (`HomerServiceClearPayloadStreamPeerBinding`, which has no
  session table): safe because the relay already resolved+cached before drain/close. Sender-first fast path also
  direct-stamps stream B at the RECEIVE-bind.
- **Removed the last role-only path**: the 3 DMA fns (`HomerDpuDmaFindDpuToHostByteRingRef` + the two receiver-close
  twins) now require a nonzero sessionUID; a 0 matches NOTHING (unresolved → retry), `matchCount>1` is always a hard
  ERROR. No single-active guessing remains.

**Step 4 DONE + building clean:** the bind/unbind API framing + rendezvous record/assert + doc clarity.
- **Bind/Unbind reframe** (thin, naming-clarity only; the full pooled-ring vision stays future work):
  `HomerClientBindResultRing` reframes `HomerClientOpenSqlResultReceiveStreamSelectedDpu` (the ring's
  OpenSession(RECEIVE) IS the session→ring bind, carrying `options->sessionUID`); `HomerClientUnbindRing` reframes
  `HomerClientCloseBaseBackupStream` (serves both SQL + basebackup role-7 rings). pgbench `openHomerSession` /
  `finishHomerSession` now read as bind/unbind.
- **Rendezvous record + consistency assertion** (diagnostic, NOT load-bearing): the farnet0 command session now
  records `sessionUID` from the client OpenSession (`TupleSinkServiceHandleOpenCommandSession`), so it is
  self-describing on BOTH nodes. The SQL role-7 benign-OK rendezvous asserts the ring's `request->sessionUID`
  matches the parent session's and logs loudly on a nonzero mismatch (a client mis-wire). Dormant by construction
  today (pgbench mints one sessionUID for both opens) but guards a future multi-mint bug.
- **Doc clarity (work item E):** the sessionKey(NON-unique)/sessionUID(UNIQUE) distinction is stated on the
  `CitusRemoteExecSessionKey` struct, the descriptor field, `HomerServiceSessionKeysBaseCompatible`, each resolver,
  and this KB subsection.

**Step 5 DONE (July 8, 2026): ALL binding steps 1-5 code-complete + building/installing/smoke-clean on farnet1.**
Full clean build+install of svc + client + citus.so + postgres backend + pgbench + pg_basebackup, all clean; the
`tuple-deform-smoke` prints ALL PASS (deform + finalize byte-layout agreement intact — the sessionUID work does not
touch the decoded-tuple layout); ABI-lockstep sanity (`strings ... | grep citus_remote_execution_control`) agrees
across pgbench and the service. Commits: citus `656005dfc` (step 2), `e8577eea0` (step 3), `e514d3c58` (step 4);
postgres `e15b37aaf34` (step 2), `869f364607b` (step 3), `4cb853e83af` (step 4).

**Net state of the single-active bug fix:** the DPU→host relay now binds strictly on the UNIQUE per-session
sessionUID with NO role-only fallback anywhere — SQL threads it onto stream B via the command-session bridge;
basebackup delivers it via the `(sessionKey → sessionUID)` pending-binding registry (base-compat matched,
stamp-and-cached onto the stream). Two concurrent `--homer-dpu` clients now each resolve to their own role-7 ring.

**Remaining (the ORIGINAL Part 3.5 step 7/8, unblocked by this fix):** resume the 3-machine validation of
`pgbench --homer-dpu` farnet0→farnet1 (abalance correctness + intended-path from DPU logs; and specifically verify
two concurrent clients each bind their own ring with no WARN / cross-delivery); then retire Path B
(`citus_remote_exec_pgbench_transaction`).

## Part 3.5.2 — kill the throwaway session + the pending-binding registry (July 8, 2026; IN PROGRESS)

Part 3.5.1 left basebackup delivering its `sessionUID` via the base-compat `(sessionKey → sessionUID)`
pending-binding registry, and the farnet0 relay stream owned by a *throwaway* base-compat session created by the far
sender's peer-open. A design review + a **feasibility spike** reframed the fix. The full model, spike evidence, and
the decision live in the canonical design note
[`../../../future-directions/citus/transport/session_identity_and_pairing.md`](../../../future-directions/citus/transport/session_identity_and_pairing.md);
summary of what changed and why:

- **Originally-planned fix (dropped):** an automatic cross-node handshake — the receiver drives a stripped
  `OP_COMMAND_SESSION` open to farnet1 so the sender adopts the receiver's `sessionUID`, then delete the registry.
- **Spike finding that killed it:** in the DPU-basebackup deployment the sender's SEND session is owned by the
  farnet1 **DPU** service (not a host service), so the linkage would have to travel **DPU↔DPU** — a net-new
  DPU-control-slot command-session opener + 3 service-side `opKind`-gate relaxations (`COMMAND_CREATE:34872`,
  `REGISTER_MEMORY:34936`), *not* "reuse SQL's host↔host spine." And even the handshake still performs one
  base-compat+tag rendezvous (relocated to farnet1), so its uniformity gain was small.
- **Decision (adopted, user-chosen):** **receiver-session base-compat+tag scan, no handshake.** Add a
  `launchDiscriminatorTag` (externally coordinated: receiver `--homer-tag N`, sender `tag=N`), create/own the relay
  by a real receiver session `R` found by base-compat+tag across both orderings, resolve the relay uid from the
  owning session/stream, and **delete `ActivePendingBindingTable`**. Same end-state (no throwaway, no registry), same
  one-rendezvous correctness guarantee, far less machinery, no new cross-node channel/ABI beyond the tag.
- **Deferred (KB-only):** session-spawned basebackup receiver (like SQL/COPY) removes even the tag + the farnet0
  rendezvous; see the design note.

Sequencing (each independently buildable; registry deleted only after the scan validates): (1) this KB note + code
comments; (2) `launchDiscriminatorTag` ABI + pg_basebackup plumbing (inert); (3) service scan owns the relay by `R`,
registry kept as belt-and-suspenders, 3-machine validate; (4) delete the registry, re-validate.

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

## Runbook — DPU Homer service bootstrap from farnet1 host (rsync + build, system pg)

Decided July 5, 2026: the DPU only needs the Homer service (`citus_tuple_sink_service`), built
against **system pg** (`/usr/bin/pg_config`, PostgreSQL 16.14) — NOT our custom pg. Rationale: the
Homer wire/ABI is defined by the Citus headers, not Postgres; the service's only pg dependency is
generic (`postgres.h`/`palloc`/`pg_attribute.h` in `tuple_sink_service_process.c`), none of which is
where our fork diverges — which is why the validated farnet1 DPU **sender** already builds against
system pg and interoperates with the host's custom-pg backend. (Making the Homer backend truly
pg-independent, or the Part 3.5 DPU-deform, are separate follow-ups.) Source lives at
`~/dbcomm/citus-dbcomm` on the DPU (NOT `/data/dbcomm` — the DPU has no `/data/dbcomm` and we don't
create one; the Makefile is location-independent via `$(citus_abs_srcdir)`).

**Proven recipe (farnet1 DPU, July 5, 2026 — builds aarch64 with the Gap 2/3/EOS receiver code):**
```sh
# 1. rsync the current citus source from farnet1 host into the DPU tree.
#    farnet1 DPU (reachable as `ssh dpu` from farnet1):
ssh dpu 'mkdir -p ~/dbcomm/citus-dbcomm'
rsync -a --exclude 'build/' --exclude '.git/' --exclude '*_bak' --exclude '*.bak' \
      --exclude 'configure~' --exclude '*.o' --exclude '*.log' \
      /data/dbcomm/citus-dbcomm/ dpu:dbcomm/citus-dbcomm/
#    farnet0 DPU (reachable only as `ssh dpu` FROM farnet0 — double hop): stage on farnet0 host,
#    then push locally to its DPU:
rsync -a --exclude 'build/' --exclude '.git/' --exclude '*_bak' --exclude '*.bak' \
      --exclude 'configure~' --exclude '*.o' --exclude '*.log' \
      /data/dbcomm/citus-dbcomm/ farnet0:/tmp/citus-dbcomm-src/
ssh farnet0 "ssh dpu 'mkdir -p ~/dbcomm/citus-dbcomm'; rsync -a /tmp/citus-dbcomm-src/ dpu:dbcomm/citus-dbcomm/"

# 2. configure for THIS tree/location against system pg, then build ONLY the service.
#    --without-libcurl: libcurl (anonymous-stats dep) is absent on the DPU and not needed.
#    PG_CONFIG=/usr/bin/pg_config: use system pg (this rewrites Makefile.global citus_abs_srcdir +
#    PG_CONFIG for the DPU; a stale host Makefile.global otherwise pins /data/dbcomm/... and fails).
ssh dpu 'cd ~/dbcomm/citus-dbcomm && PG_CONFIG=/usr/bin/pg_config ./configure --without-libcurl \
         && make -j4 service-bin CPPFLAGS="-D_GNU_SOURCE"'

# 3. verify aarch64 + the receiver code is present.
ssh dpu 'file ~/dbcomm/citus-dbcomm/build/homer/citus_tuple_sink_service | grep aarch64; \
         grep -c HomerServicePumpIncomingByteRingDpuRelay ~/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c'
```
The DPU runs `~/dbcomm/citus-dbcomm/build/homer/citus_tuple_sink_service` directly (no install
prefix, no `/data/dbcomm/pg-citus` needed at build or runtime; `ldd` is clean). Only `service-bin`
is needed on the DPU — `client-bin`/`libhomer_client.a` is for the HOST frontend (`pg_basebackup`),
not the DPU. Re-sync just the changed homer files + `make service-bin` for later iterations.
