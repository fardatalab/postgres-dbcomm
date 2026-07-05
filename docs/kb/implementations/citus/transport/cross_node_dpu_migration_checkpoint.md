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

The receiver relay, Loop-1 credit tie, and `receiverCloseReleased` lifecycle are
validated end-to-end by P1.

## P1 — basebackup receiver DPU offload (planned)

Sender unchanged (pg_basebackup → farnet1 DPU pull + RDMA egress). Receiver:
farnet0 DPU service RDMAs into the landing region, DMA-writes to a host consumer
ring drained by a minimal farnet0 host sink (Homer consume API, blackhole).
First cross-node milestone; validates P0b's full receive path.

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
