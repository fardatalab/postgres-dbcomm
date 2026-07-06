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

**Design principle (DECIDED — wrap is geometry-only, never content).** Wrap-gap
handling is a pure byte-ring-*geometry* concern on BOTH sides; it must never depend
on a record's *semantic identity* (ordinal, generation, record-kind). Reading the
transport-header **length** field is byte-ring framing, not "content" — that is
allowed and is exactly the fit test; the semantic identity is what must never be
consulted to decide gap-vs-record. Concretely:

- **Producer:** open the gap by the fit test alone (`contiguousBytes < recordBytes`
  ⇒ skip the tail, restart at offset 0), and leave a **readable transport-length
  header at the record cursor even in the gap** so the consumer's fit test always
  has a length to read. The selected-DPU basebackup producer does this by copying
  the real next-record `CitusTupleSinkTransportHeader` into the gap
  (`homer_client.c:3814-3838`) — which doubly serves the DPU egress's wrap proof.
- **Consumer:** detect the gap by geometry only — (1) `contiguousBytes <
  minRecordBytes` ⇒ no record can start here; (2) transport length `recordBytes >
  contiguousBytes` ⇒ the record will not fit before the wrap ⇒ gap. Skip the dead
  trailer to offset 0; never inspect ordinal/generation/kind to decide a gap.
- **Anti-pattern (deprecated, do not carry forward):** the host-service tuple
  producer leaves *stale* bytes in the gap (no readable length header), which forced
  its consumer (`HomerClientDrainResultSinkUntil`) to guess the gap via an
  ordinal/protocol *content-mismatch*. That is the wrong pattern and exists only in
  the soon-to-be-deleted host-service path. New producers/consumers — basebackup
  now, the Part 3.5 DPU tuple sink later — are geometry-only.

Consequences that MUST be preserved:

- **The tail counts through the gap.** `publishedTail` advances by the skipped
  gap bytes as well as the real bytes (the gap is "published" as dead bytes), so
  credit accounting stays a simple monotonic byte count. The consumer must skip
  the same gap using the same `contiguousBytes < recordBytes` test.
- **Transport fragmentation is position-preserving byte chunking, NOT semantic
  re-framing.** The RDMA/DMA substrate may move the same bytes in arbitrary-sized
  chunks (scheduler pacing / HOL-blocking), but each chunk lands at its true absolute
  offset, adds nothing (no per-chunk header, no FRAGMENT flags), and the destination is
  byte-identical — so whole records reassemble for free. (History: the egress once did
  *semantic* re-framing with `outgoingFragment*` + generated FRAGMENT headers + a
  service-side reassembler; that was removed on July 6 — see the cross-node checkpoint
  "P1.5". The `FRAGMENT_FIRST/LAST` ABI bits are now unused/reserved.)
- **The wrap gap is relayed VERBATIM and skipped by geometry.** Because mirror-1:1
  makes source ring == peer ring, the egress writes `[absoluteStart, absoluteEnd)` (gap
  padding included) to matching offsets; the consumer skips the gap by geometry
  (`recordBytes > contiguousBytes`). The old separate source-skip (`outgoingSourceSkip*`)
  is gone — one wrap split (`ringBytes - absolute % ringBytes`) now covers it, in the
  shared helper `HomerByteRingRelayNextChunk`.

## Directions and their consumers (now SYMMETRIC — both pure byte relays)

As of July 6 (P1.5, see cross-node checkpoint) both directions are the SAME
position-preserving, object-agnostic byte relay — no record parse, no re-framing, split
only at the ring wrap and scheduler/credit limits, sharing `HomerByteRingRelayNextChunk`:

- **host→DPU pull → RDMA egress (sender side, basebackup)** — the DPU pulls the host
  producer ring into a DPU-local **mirror**, then RDMA-relays the ready mirror range
  `[absoluteStart, absoluteEnd)` verbatim to the peer ring at matching offsets
  (`HomerServicePumpOutgoingDpuMirrorByteRingPayload`). Byte-only; a chunk may end
  mid-record (the no-torn frontend defers until the record is whole; RC write ordering
  puts payload before the tail doorbell).
- **DPU→host write (receiver side, basebackup relay)** — the same shape over DMA: the far
  sender RDMA-writes byte ranges into the DPU landing ring and the DPU DMA-relays
  `[writtenTail, publishedTail)` to the host consumer ring, split only at the wrap. No
  header parse. See `HomerServicePumpIncomingByteRingDpuRelay`.

(Historical note: the sender egress used to be the asymmetric one — it parsed framing
headers and re-fragmented per record. That reframing was removed; the two directions are
now genuine mirror images. Routing the receive relay through `HomerByteRingRelayNextChunk`
and the two-ring tuple path are tracked follow-ups.)

## The frontend byte-ring sink core (host consumer) — no-torn-object + geometry-only gaps

The FINAL host consumer (basebackup `--homer-receive` now; the Part 3.5 DPU tuple
sink later) drains WHOLE objects out of the host consumer ring through ONE shared
inline core: `src/include/distributed/homer/homer_byte_ring_sink.h`
(`HomerByteRingSinkDrain`). It is header-only so it compiles into the external
client TU (`homer_client.c`, no `postgres.h`) and, later, a backend TU, with no
link wiring. It enforces, once, what every byte-ring sink shares:

- **No torn object:** deliver an object only when its whole `[transport header +
  payload]` span is contiguous (always true for a real record — records never
  straddle the wrap) AND fully published; otherwise defer (VISIBILITY_PENDING). The
  Homer user is never handed a partial object.
- **Geometry-only wrap-gap skip** (per the principle above): `contiguousBytes <
  minRecordBytes`, or transport length `recordBytes > contiguousBytes`, ⇒ advance
  `consumedHead` through the dead trailer to offset 0. Gap dead bytes are NOT counted
  as delivered (so `delivered_bytes` reflects real object bytes, ~1.24% header
  overhead, not the raw published tail with its ~13.79% gap+header inflation).
- **consumedHead is Loop-2 back-pressure:** advance it only past fully-delivered
  objects; the DPU relay must not wrap onto an object still being read.

The per-object type injects only `minRecordBytes`/`maxRecordBytes` and a
`validateAndDeliver` (envelope correctness + the actual hand-up); gap detection lives
entirely in the core.

**Bug the core was born with (FAIL #2 — fixed).** The core was first extracted from
the tuple host-service consumer, so it inherited that consumer's *content-mismatch*
gap detector and MISSED the general geometric fit test. On basebackup — whose gap (by
the geometry-only production rule above) holds a copied header with the MATCHING
ordinal — the copied header classified "expected", sailed past the mismatch skip, and
the no-torn gate read `recordBytes > contiguousBytes` as "not published yet, wait"
instead of "won't fit ⇒ gap", so the drain stalled forever at the first wrap and
back-pressured the far sender into a 99% CPU producer-reserve spin. Fix: split the
gate so `recordBytes > contiguousBytes` (at the ring end, fully published) is a
wrap-gap skip, not a visibility defer. **Lesson: this is precisely why wrap detection
must be geometry-only — a content/identity detector is structurally blind to a gap
that, by design, carries a valid header with the matching identity.**

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
