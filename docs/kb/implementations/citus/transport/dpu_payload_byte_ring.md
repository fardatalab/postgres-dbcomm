# DPU Payload Byte-Ring: Wrap Protocol, Mirror, and Invariants

<!-- kb-summary: Current DPU payload byte-ring wrap protocol, per-session pooled staging/landing/source slots, byte-verbatim egress, and invariants. -->

## Scope

- **What this doc explains**: how the Homer payload byte-ring works on the
  DPU-offloaded path — the control-block + payload-storage layout, the ring
  **wrap-gap protocol** (physical wrap gaps, distinct from semantic
  fragmentation), the per-direction frontiers, the DPU-local **mirror** of the
  source ring, and the byte-verbatim, position-preserving RDMA egress. Every
  data-carrying DPU ring is a per-stream pool slot bound at stream open:
  `MIRROR`, `LANDING`, and, for the two-ring SQL-result relay, `SOURCE`. It also
  records superseded mirror-ring designs and bugs.
- **What this doc does NOT cover**: the host-service (non-DPU) service↔service
  byte-ring substrate — see
  [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md).
  The end-to-end cross-node DPU basebackup milestone is in
  [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md).
- **Primary directory**: `docs/kb/implementations/citus/transport/`
- **Doc type**: implementation / grounded invariants
- **Source**: `src/backend/distributed/utils/homer/homer_dpu_byte_ring_pool.c/.h`,
  `.../homer_service_dpu_dma.c`, `.../tuple_sink_service_process.c`

## Why this exists

A byte ring is a continuous head/tail byte stream, but the code carries several
non-obvious, load-bearing invariants that are only implicit in the source — and
both an automated refactor and a careful human review got them wrong once (the
mirror-ring stall below). This doc makes those invariants explicit so future work
(preserving byte-verbatim relaying and pooled per-stream ownership) does not
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
  (`homer_client.c:3814-3838`).
- **Consumer:** detect the gap by geometry only — (1) `contiguousBytes <
  minRecordBytes` ⇒ no record can start here; (2) transport length `recordBytes >
  contiguousBytes` ⇒ the record will not fit before the wrap ⇒ gap. Skip the dead
  trailer to offset 0; never inspect ordinal/generation/kind to decide a gap.
- **Anti-pattern (deprecated, do not carry forward):** the host-service tuple
  producer leaves *stale* bytes in the gap (no readable length header), which forced
  its consumer (`HomerClientDrainResultSinkUntil`) to guess the gap via an
  ordinal/protocol *content-mismatch*. That is the wrong pattern and exists only in
  the soon-to-be-deleted host-service path. The selected-DPU basebackup and SQL-result
  producers/consumers are geometry-only.

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
  service-side reassembler. The live DPU egress was de-reframed on July 6 (checkpoint
  "P1.5"); the dead fragmentation machinery — the `outgoingFragment*`/`reassembly*` fields,
  `HomerServiceForcedBulkFragmentBytes`/`HOMER_BULK_FRAGMENT_BYTES`, the legacy-pump
  fragment path, and the CPU reassembler — was then deleted in "P1.6", which also made
  header-ready + the reassembler *reject* any FRAGMENT bit. The `FRAGMENT_FIRST/LAST` ABI
  bits are now reserved and never produced.)
- **The wrap gap is relayed VERBATIM and skipped by geometry.** The logical sender
  host ring and remote receiver ring are equal per pair; the DPU mirror is only a
  size-decoupled staging slot. Egress writes `[absoluteStart, absoluteEnd)` (gap
  padding included) at matching logical offsets, and the consumer skips the gap by
  geometry (`recordBytes > contiguousBytes`). The old separate source-skip
  (`outgoingSourceSkip*`) is gone — one wrap split (`ringBytes - absolute % ringBytes`)
  now covers it, in the shared helper `HomerByteRingRelayNextChunk`. See the current
  slot-versus-ring contract in [CONTRACTS.md](CONTRACTS.md).

## Directions and their consumers (now SYMMETRIC — both pure byte relays)

As of July 6 (P1.5, see cross-node checkpoint) both directions are the SAME
position-preserving, object-agnostic byte relay — no record parse, no re-framing, split
only at the ring wrap and scheduler/credit limits, sharing `HomerByteRingRelayNextChunk`:

- **host→DPU pull → RDMA egress (sender side)** — the DPU pulls the host producer
  ring into that stream's DPU-local `MIRROR` pool slot, then RDMA-relays the ready range
  `[absoluteStart, absoluteEnd)` verbatim to the peer ring at matching offsets
  (`HomerServicePumpOutgoingDpuMirrorByteRingPayload`). Byte-only; a chunk may end
  mid-record (the no-torn frontend defers until the record is whole; RC write ordering
  puts payload before the tail doorbell).
- **DPU→host write (receiver side)** — the same shape over DMA: the far sender
  RDMA-writes byte ranges into that stream's `LANDING` pool slot and the DPU DMA-relays
  `[writtenTail, publishedTail)` to the host consumer ring, split only at the wrap. No
  header parse. Basebackup uses its landing ring as the DMA source; the two-ring
  SQL-result path binds a separate `SOURCE` slot for post-deform output. See
  `HomerServicePumpIncomingByteRingDpuRelay`.

(Historical note: the sender egress used to be the asymmetric one — it parsed framing
headers and re-fragmented per record. That reframing was removed; the two directions are
now genuine mirror images. The per-session pool migration is complete and validated:
Stage 1 supplies basebackup `MIRROR`/`LANDING` slots, and Stage 2 supplies the
SQL-result `SOURCE` slot; see
[dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md).)

## The frontend byte-ring sink core (host consumer) — no-torn-object + geometry-only gaps

The FINAL host consumer (basebackup `--homer-receive` and the selected-DPU SQL-result
path) drains WHOLE objects out of the host consumer ring through ONE shared
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

## The mirror-must-be-1:1-with-source invariant (and the bug) — ⚠ SUPERSEDED

> **⚠ SUPERSEDED 2026-07-17.** The 1:1 requirement below belonged to the REFRAMING-era egress, which parsed
> record headers out of the mirror and therefore needed a header not to straddle the mirror wrap. That egress was
> later replaced by a BYTE-VERBATIM, position-preserving relay (`tuple_sink_service_process.c:35005-35053`, "THE
> WR-BUDGET LOOP IS GONE ... the protocol now leaves a wrap GAP and this relay is byte-oriented"), which ships
> raw bytes at matching absolute offsets and reassembles at the far end. So the mirror SLOT is now
> **size-decoupled** from the source/host ring — it need only hold the in-flight window (capacity), NOT match the
> source size. The real, still-current invariant is PER PAIR: sender host==remote
> (`tuple_sink_service_process.c:35031`), receiver source==host (`:37968`); slots are homogeneous only for the
> DOCA arena. VALIDATED 2026-07-17 (basebackup: 44,356 ring laps at 10-MiB slot / 8-MiB host, no corruption). The
> reframing-era bug below is kept as instructive history; its "decided fix = one constant / 1:1" was NOT
> implemented and is unnecessary. Canonical current statement: the transport `CONTRACTS.md` "SLOT vs RING size".

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

- **⚠ RESOLVED — single active stream (per-session byte-ring pool).** The former
  engine-owned singleton data rings — `engine->mirrorRing`, singleton use of
  `engine->landingRegion`, and `engine->tupleSourceRing` — are retired. At stream
  open, `HomerDpuByteRingBind()` binds a slot keyed by stream and purpose: `MIRROR`
  for sender egress, `LANDING` for the receive path, and (for `TUPLE_VIEW_BATCH`)
  `SOURCE` for the two-ring SQL-result relay. `HomerDpuByteRingUnbind()` releases all
  purposes for that stream during reset. Thus concurrent streams have independent
  storage and control prefixes rather than aliasing a shared control block. Stage 1
  (basebackup) and Stage 2 (`SOURCE`) are complete and validated; see
  [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md).
- **Homogeneous byte-ring slots (current design).** `HomerDpuByteRingPool` makes every
  slot the same `[control | storage]` layout, so any slot can serve `MIRROR`, `LANDING`,
  or `SOURCE`. `HomerDpuByteRingPoolInit()` sets `slotStorageBytes` to
  `HOMER_SQL_RESULT_BYTE_RING_STORAGE_BYTES` (10 MiB), the larger logical-ring class.
  A slot is a container, not a stream's ring geometry: basebackup's logical
  `HOMER_PAYLOAD` ring is 8 MiB, while SQL results use the 10-MiB
  `HOMER_SQL_RESULT` ring. Every slot carries the `landingPayloadOffset` control
  prefix; `LANDING` uses it and `MIRROR`/`SOURCE` leave it unused. A future purpose
  needing more storage or a different layout must widen the homogeneous slot or add a
  typed pool; it must not equate slot size with host/remote logical-ring size. The
  canonical current invariant is [CONTRACTS.md](CONTRACTS.md)'s “Byte-ring SLOT size
  vs RING size” entry.
- **Control-line staging is a DMA source, not a payload slot.** Publishing a
  produced-tail / consumed-head credit word to a *remote* ring's control block is
  a DMA whose source must be DPU-registered memory; a small cache-line-sized
  registered cell holds that word (do not conflate it with the 64 KB payload
  staging it used to ride on).
- **[SUPERSEDED] The reframing egress needed contiguous headers.** In the reframing era the egress parsed
  headers out of the mirror, so the fixed-size framing header had to be contiguous (which 1:1 provided). The
  CURRENT egress is byte-verbatim (no header parse), so a header may be split across the mirror STAGING wrap and
  rejoined at the far end by absolute offset; records never straddle a RING wrap (wrap-gap protocol on the
  host/remote ring, which are equal per pair). The mirror slot is size-decoupled — see the section banner above.

## Related

- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md)
  — service↔service (non-DPU) byte-ring substrate.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md)
  — cross-node DPU basebackup milestone, close lifecycle, DMA cap, and this
  mirror-ring work in context.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md)
  — completed and validated per-session `MIRROR`/`LANDING`/`SOURCE` pool migration.
- [CONTRACTS.md](CONTRACTS.md) — current byte-ring slot-versus-logical-ring geometry
  and per-pair wire-equality invariants.
