# DPU Payload Byte-Ring: Wrap Protocol, Mirror, and Invariants

## Scope

- **What this doc explains**: how the Homer payload byte-ring works on the
  DPU-offloaded path — the control-block + payload-storage layout, the ring
  **wrap-gap protocol** (physical wrap gaps, distinct from semantic
  fragmentation), the per-direction frontiers, the DPU-local **mirror** of the
  source ring, and the **reframing** RDMA egress that parses per-record transport
  headers. It also records a mirror-ring bug and the decided fix.
- **What this doc does NOT cover**: the host-service (non-DPU) service↔service
  byte-ring substrate — see
  [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md).
  The end-to-end cross-node DPU basebackup milestone is in
  [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md).
- **Primary directory**: `docs/kb/implementations/citus/transport/`
- **Doc type**: implementation / grounded invariants
- **Source**: `src/backend/distributed/utils/homer/homer_service_dpu_dma.c`,
  `.../tuple_sink_service_process.c`

## Why this exists

A byte ring is a continuous head/tail byte stream, but the code carries several
non-obvious, load-bearing invariants that are only implicit in the source — and
both an automated refactor and a careful human review got them wrong once (the
mirror-ring stall below). This doc makes those invariants explicit so future work
(byte-orienting more paths, removing slots, Part 3.5 tuple two-ring) does not
re-earn the same bugs.

## Layout: control block + payload storage

A payload byte ring is one contiguous region: a small **control block** at the
front, then **payload byte storage**. Two roles read/write it:

- Producer publishes bytes into the storage and advances `publishedTail`.
- Consumer drains bytes and advances `consumedHead` (the producer's credit).

Control fields (`CitusHomerPayloadByteRingControl`): `protocolVersion`, `flags`,
`ringBytes` (payload storage size), `publishedTail`, `consumedHead`. Absolute
byte offsets are monotonic uint64; the physical position in storage is
`offset % ringBytes` — see `HomerServicePayloadByteRingOffset()`
(`tuple_sink_service_process.c:6910`).

## The ring wrap-gap protocol (the key invariant)

**A single object (record OR its fixed-size framing header) is never physically
split across the ring wrap.** When the next record would run off the end of the
storage, the producer leaves a **physical wrap gap**: it skips the remaining tail
bytes and starts the record at storage offset 0. See the producer at
`tuple_sink_service_process.c:28322-28325` (`if (contiguousBytes < recordBytes)
recordStart += contiguousBytes;`).

Consequences that MUST be preserved:

- **The tail counts through the gap.** `publishedTail` advances by the skipped
  gap bytes as well as the real bytes (the gap is "published" as dead bytes), so
  credit accounting stays a simple monotonic byte count. The consumer must skip
  the same gap using the same `contiguousBytes < recordBytes` test.
- **This is separate from semantic fragmentation.** Semantic fragmentation
  splits a large *object* into multiple transport fragments (each with its own
  header). The wrap gap is purely *physical* ring layout. The DPU egress tracks
  the two independently: `outgoingSourceSkipActive` / `outgoingSourceSkipTail`
  (`tuple_sink_service_process.c:1623-1632`) skip physical wrap-gap bytes without
  emitting any peer-visible object, while `outgoingFragment*` fields drive
  semantic fragmentation. A byte counted in a wrap gap must never publish an
  object.
- **Both the destination (peer) ring AND the source ring obey it.** The egress
  applies the gap rule when writing to the peer ring
  (`tuple_sink_service_process.c:16872-16890`, advancing `batchRemoteTail` past a
  remote wrap gap) and when reading source objects (source-skip above).

## Directions and their consumers (the asymmetry that is real, not artifact)

The two directions have genuinely different consumers, which is why they are NOT
symmetric implementations:

- **host→DPU pull (sender side, basebackup egress)** — the DPU pulls the host
  producer ring into a DPU-local **mirror**, then **reframes**: it parses each
  source record's `CitusTupleSinkTransportHeader` + basebackup header out of the
  mirror and re-fragments the payload into *peer-ring-sized* fragments, stamping
  new `FRAGMENT_FIRST/LAST/EOS` transport headers per fragment
  (`tuple_sink_service_process.c:16673-16919`). The host does not know the peer
  ring geometry; the DPU does — so reframing is the DPU's job, and it REQUIRES
  each source record's fixed-size **header to be contiguous** to parse it. The
  *payload* is byte-fragmentable freely (clamped to the wrap-bounded slice); only
  the header needs contiguity.
- **DPU→host write (receiver side, basebackup relay)** — a **pure byte relay**:
  the far sender RDMA-writes byte ranges into the DPU landing ring and the DPU
  DMA-relays `[writtenTail, publishedTail)` to the host consumer ring, split only
  at the wrap. No header parsing. See the receiver relay in
  `HomerServicePumpIncomingByteRingDpuRelay`.

So "make both directions a ring for symmetry" is only half-right: the receiver is
already a ring *because* it does not parse records; the sender parses framing
headers, so its mirror must still honor "a header never straddles the wrap."

## Frontiers

Per-ring runtime frontiers (`HomerDpuDmaRingRuntime`, `homer_service_dpu_dma.c`),
all monotonic absolute byte counts:

- Pull/mirror (host→DPU): `acceptedPublishedTail` (host-published tail seen via
  grouped-control) ≥ `pulledByteTail`/`completedByteTail` (bytes landed in the
  mirror) ≥ `releasedByteTail` (bytes egressed + send-CQ-retired, ring space
  freed). Host credit (`consumedHead`) is published from `releasedByteTail`.
- Write (DPU→host): `writtenTail` (payload DMA'd to host) ≥
  `publishedProducedTail` (produced-tail gate word landed); `writeCreditEpoch`
  the monotonic publication epoch.

Two flow-control loops (see the cross-node checkpoint for the full picture):
Loop-1 = far-sender credit tied to the DPU's relayed/released frontier; Loop-2 =
host-consumer back-pressure read by DPU grouped-control.

## The mirror-must-be-1:1-with-source invariant (and the bug)

The DPU-local mirror is the DPU's copy of the **source** (host producer) ring,
used as the RDMA egress source (the DPU cannot RDMA directly from host memory —
it must DMA-pull to DPU memory first). The load-bearing invariant, discovered the
hard way:

> **The mirror's ring geometry must be derived from the source stream's geometry,
> not from an engine constant.** Its storage size (hence its wrap position) must
> match the source ring, so the source's wrap gaps coincide with the mirror's
> wraps and the wrap-gap protocol carries over for free.

### Decision that did not survive validation: naive pool→ring with an engine-constant size

A first refactor replaced the sender pull-mirror's fixed-slot pool with a single
contiguous ring but sized it `commandPullBufferCount * 65536` (≈4 MiB) — the old
pool's total capacity, an **engine constant unrelated to the source ring**. The
source ring is `slotCount * slotCapacityBytes` (≈4.2 MiB with the header prefix),
so the mirror wrap fell mid-record at a boundary the source protocol left no gap
for. A source record whose framing **header** straddled the mirror wrap could not
be parsed (`HOMER_PAYLOAD_RANGE_STOP_SOURCE_HEADER_SPLIT`,
`tuple_sink_service_process.c:16792-16798`); the egress spun on the parse/append
loop (~99% CPU, no diagnostic), never released, the mirror filled, host credit
stopped, and the walsender blocked. Symptom: a **silent mid-transfer stall**
(~78% through, after thousands of clean wraps — it dies at the first wrap where a
header happens to straddle). The pool masked this because a pool has no single
wrap — each buffer was its own contiguous span, and `TryAppend` extended a buffer
until a header fit. Making it one ring gave the size a *meaning* it didn't have.

An accompanying claim — "the mirror size 2^22 is a power of two that aligns with
power-of-two host-ring wraps, so basebackup is immune" — was **wrong**: basebackup
does obey the physical wrap-gap protocol on the host ring (no straddle there), but
the *misaligned mirror* wrap still straddles, and the sizes weren't actually
multiples. Do not trust power-of-two coincidence for wrap alignment.

### Decided fix (to implement)

- Make the payload byte-ring storage size a single **`#define`d constant** used by
  **both** the host source ring and the DPU mirror (drop slot-based sizing;
  a byte ring has head/tail, not slots). Then the mirror is a true 1:1 of the
  source, the wraps coincide, the source's wrap gaps land on the mirror wraps, and
  the egress's existing source-skip handles them — **no straddle can occur and no
  header-linearization scratch is needed.**
- Rejected alternative (kept for the record): allow the straddle and **linearize**
  a wrap-straddling header into a small scratch. It works but patches a symptom;
  1:1 geometry removes the failure class.
- Rejected alternative: keep the caller's `slots`/`bytes` knob and size the mirror
  per-stream at bind. Also correct, but a constant both sides share is simpler and
  the slot concept is being removed anyway.

## Other caveats

- **Single active stream.** The mirror (and receiver landing ring) is addressed by
  absolute offset `% ringBytes`, so concurrent sender byte-ring streams would
  collide; the current model is one active stream. Partition per-stream before
  supporting concurrency.
- **Control-line staging is a DMA source, not a payload slot.** Publishing a
  produced-tail / consumed-head credit word to a *remote* ring's control block is
  a DMA whose source must be DPU-registered memory; a small cache-line-sized
  registered cell holds that word (do not conflate it with the 64 KB payload
  staging it used to ride on).
- **The reframing egress needs contiguous headers, not contiguous payloads.**
  Payload fragments freely at any wrap; only the fixed-size framing header must be
  contiguous. Any future byte-ring change must keep that guarantee (1:1 mirror
  provides it).

## Related

- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md)
  — service↔service (non-DPU) byte-ring substrate.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md)
  — cross-node DPU basebackup milestone, close lifecycle, DMA cap, and this
  mirror-ring work in context.
