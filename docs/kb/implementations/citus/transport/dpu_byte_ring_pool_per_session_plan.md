# Per-session DPU byte-ring resource pool + lifetime protocol (plan + design)

## Scope

- **What this doc is:** the design + staged implementation plan for making every
  DPU data-carrying byte-ring a **per-session/per-stream** resource, bound and
  released through one unified protocol, so multiple concurrent cross-node DPU
  sessions (basebackup first, then `--homer-dpu` tuple/pgbench) stop cross-wiring
  each other's ring state. Grounds the fix that supersedes the "single active
  stream" caveat in [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) and the
  Stage-5 concurrency block in
  [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md).
- **Status:** **Stage 1 COMPLETE — concurrent cross-node DPU basebackup VALIDATED
  (July 9, 2026).** The acceptance gate passed: two concurrent 2-session backups
  (tags 1 & 2) both complete with byte conservation (~23.28 GB each, agree to
  <0.0001%), each session binding its OWN landing + mirror byte-ring slots (sender
  DPU: two `purpose=0` binds slot 0 / slot 1; receiver DPU: two `purpose=1` binds
  slot 0 / slot 1), with NO `RECV_CQ_FAILURE` / `observed>posted` / `DISCONNECT` /
  `ambiguous receive-relay resolve`. The original reset-before-any-byte-moved bug is
  fixed. Remaining: the deferred mirror-cache cleanup, Stage 2 (tuple-source for
  concurrent `--homer-dpu`), and the connection-binding-fix disposition. Plan mirror
  file was `~/.claude/plans/here-s-a-snippet-on-linked-papert.md`; this doc is canonical.
- **Progress:**
  - **Stage 0a — DONE + validated (single-session, byte-identical).** New module
    `homer_dpu_byte_ring_pool.c/.h` created; wired into the engine (pool on
    `HomerDpuDmaEngine`, inited in the DOCA lifecycle over the existing landing
    region as 1 region / 1 slot, `HomerDpuDmaGetByteRingPool` accessor), the service
    landing ensure/teardown (bind LANDING / unbind), and all six DPU-linking build
    rules (`Makefile`). Validation: 3 back-to-back single-session cross-node DPU
    basebackups, byte conservation 0.000134% spread (~23.28 GB), the new
    `[homer-service] byte-ring bind: ... purpose=1 slot=0 region=0` line present on
    the farnet0 DPU each run (pool path exercised), no `RECV_CQ_FAILURE` /
    `observed>posted` / `DISCONNECT`. Mirror + tuple-source rings untouched.
  - **Stage 0b — DONE + validated (single-session, byte-equivalent).** Multi-region
    DOCA arena: region 0 IS `engine->landingRegion` (enlarged to N slots), extra
    regions 1..R-1 are separate allocations + DOCA mmaps; `HomerDpuByteRingPool`
    inited over all R regions (R=2, 4 slots/region = 8 slots), `slotStorageBytes =
    HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`. Overflow + 96 MiB per-region ceiling
    guards. Aliasing-free: no double-free/double-mmap-destroy (region 0 freed once as
    landingRegion; extras in their own loops). DMA source deliberately UNCHANGED
    (transitional) -> single-session always binds slot 0 == region-0 base. Validation:
    3x single-session basebackup, byte conservation 0.00012% (~23.28 GB), bind line
    `slot=0 region=0`, farnet0 DPU started cleanly with 2 arena mmaps, no reset.
    TODO(Stage 1): add an explicit arena-geometry startup log line (validator noted
    the allocation is only inferable from the absence of errors today).
  - **Stage 1a-landing — DONE + validated (single-session, no regression).** The LANDING
    DMA relay-source now reads the bound slot's storage (threaded via the handle:
    `storageAddr`/`storageBytes`/`mmap`) instead of `engine->landingRegion` — the
    transitional aliasing is removed for landing. `HomerDpuByteRingHandle` gained
    `storageBytes`; `HomerDpuDmaSubmitByteRingWrite`/`...SubmitOneByteRingTask` gained
    `(dpuRingSourceBase, dpuRingSourceBytes, dpuRingSourceMmap)` (LANDING branch uses them;
    PULL/TUPLE_SOURCE pass NULL). Relay passes the stream's stored landing handle. Added the
    arena startup log. Validation: 3x single-session, byte conservation 0.00015% (~23.28 GB),
    `byte-ring arena ready: regions=2 slots=8 slotStorage=8388608`, bind `slot=0 region=0`,
    no reset. NOTE: `dpuRingSource*` will be renamed to a neutral `dpuRingBase*` in
    Stage 1a-mirror (a task uses its DPU-local ring as source for landing OR dest for a
    mirror pull — one param, both uses).
  - **Stage 1a-mirror — DONE + validated (single-session, no regression).** The sender
    MIRROR ring (host→DPU DMA-pull DEST + RDMA-egress SOURCE) is now a per-session bound
    slot instead of the engine singleton. `TupleSinkServiceEnsureDpuMirrorMemoryRegion`
    binds a MIRROR slot + registers the slot's storage as the egress MR; the engine
    resolves the mirror slot by `descriptor->serviceSinkId` (== the sender stream's
    `serviceStreamId`) via `HomerDpuDmaResolveMirrorSlot` and threads it into the pull
    dest (`SubmitByteRingSmokePull`→`SubmitOneByteRingTask`) + the egress source
    (`HomerDpuDmaFillMirroredByteRange`); `dpuRingSource*`→`dpuRingBase*` neutral rename.
    Stream reset unconditionally unbinds all of the stream's slots.
    **DEADLOCK found + fixed:** the mirror was bound lazily at egress, but the pull loop
    requires it bound first (it requeue-spins otherwise) and egress needs pull output →
    circular deadlock (98% CPU spin, no `purpose=0` bind line). FIX: bind+register the
    mirror slot at STREAM_REGISTER_MIRROR (stream open, connection already ensured), before
    any pull; the egress `EnsureDpuMirrorMemoryRegion` is then idempotent. General lesson:
    converting a singleton to a per-session bound resource can create an ordering
    deadlock — hoist the bind upstream of every consumer. Validation: 3x single-session,
    8-12s (no hang), byte conservation 0.00013% (~23.28 GB), `purpose=0` MIRROR bind on the
    sender DPU + `purpose=1` LANDING on the receiver, no reset/mirror-slot error.
    **CLEANUP TODO (fold into Stage 1b build):** `HomerDpuDmaResolveMirrorSlot` re-Finds
    every call (the added `HomerDpuDmaRingRuntime` mirror-slot cache fields are written but
    never read for short-circuit) and const-casts `engine`/`import` in the egress path to
    write them; since re-resolving is cheap (O(8 slots)) and actually safer against stale
    bindings, drop the cache fields + `ClearMirrorSlotCache` and make the resolver a pure
    non-caching `Find` (removes the const-casts).
  - **Stage 1b — DONE + validated (CONCURRENT ACCEPTANCE GATE PASSED).** Resolver fix:
    `HomerServiceFindPeerBoundReceiveRelayStream` gained a `launchDiscriminatorTag` param;
    it now requires `sessionState->launchDiscriminatorTag == tag` in addition to base
    compatibility, and uses strict-unique-match (count all matches; return the sole match;
    return NULL and log `ambiguous receive-relay resolve` on >1 rather than guessing;
    NULL-retry on 0). Caller passes `request->launchDiscriminatorTag`. Validation: the
    concurrent 2-session basebackup completed with byte conservation on both, distinct
    per-session landing+mirror slots, no reset, no ambiguous-resolve. This closes the
    concurrent-basebackup goal.
  - Stage 2 (tuple-source for concurrent `--homer-dpu`): pending. Deferred cleanup: the
    mirror-slot cache + const-casts (see Stage 1a-mirror note).
- **Doc type:** implementation plan / in-flight.
- **Source:** `src/backend/distributed/utils/homer/homer_service_dpu_dma.c` (+`.h`),
  `.../tuple_sink_service_process.c`, and the new module
  `.../homer_dpu_byte_ring_pool.c` (+`.h`).

## Why — the falsified premise and the confirmed root cause

**Goal:** run multiple concurrent cross-node DPU basebackup sessions between the
same node pair with byte-conservation and no reset.

**Decision history (what did NOT work):** we first bound the outgoing PAYLOAD
*RDMA connection* per session — `ownerServiceSessionId` on the 64-slot
`outgoingConnections[]` pool in `remote_execution_peer_transport_rdma.c`; `Find` =
prefer-own → adopt-free → create-new; release-to-free at `ResetSession`. It PASSED
single-backup + sequential reuse (**Test 1**), but the concurrent 2-session
basebackup (**Test 2**) **FAILED identically**, which **falsified the "shared
connection / shared recv-CQ" root-cause model**: the run logs prove both sessions
got FULLY SEPARATE outgoing connections (sender slots 0/1) AND separate incoming
connections (receiver `connection_index` 0/1, generations 1/2), yet the identical
`payload sender credit advanced beyond posted tail token=4098 observed=29360576
posted=0` → `HOMER_PEER_RESET_RECV_CQ_FAILURE` → `CM DISCONNECTED` reset still hit
session 2's *own fresh* connection.

**Confirmed root cause (contamination is ABOVE the RDMA connection).** The
receiver's per-stream consumed-head is read from
`streamEntry->stream.localReceiveByteRing.control->consumedHead`
(`tuple_sink_service_process.c:12752`). For a DPU-relay stream that ring is backed
by the **single per-engine landing region** — `HomerServiceEnsureLocalReceiveByteRing`
(`:16401-16447`) sets `mappingAddress = landingBase`, and
`HomerDpuDmaGetLandingRegionMemory` returns the one `engine->landingRegion`. Two
concurrent receive streams alias the SAME control block, so session B reads session
A's ~29 MB `consumedHead` and RDMA-writes it to B's own (correct, per-stream) sender
head-mirror → `observed(29 MB) > posted(0)` → reset. The credit **dispatch** is
correct (keys by token + connection; every binding check in
`TupleSinkServiceDispatchPeerPayloadDoorbell` `:27289-27294` passes — hence the error
comes from `HomerServiceApplyPayloadSenderCreditDoorbell` `:27228`, not a dispatch
"binding mismatch"); only the credit **VALUE** is cross-wired via the shared ring.

**Audit — three engine-owned singleton data rings** (`homer_service_dpu_dma.c:505-508`),
all shared across sessions:
- `engine->mirrorRing` — sender-DPU egress source (host→DPU pull target + RDMA
  egress). Used by basebackup (sender side).
- `engine->landingRegion` — receiver-DPU RDMA landing target + relay DMA source,
  incl. its `CitusHomerPayloadByteRingControl` control block. Used by basebackup
  (receiver side).
- `engine->tupleSourceRing` — receiver-DPU post-deform second ring (two-ring
  tuple/pgbench path only).

Plus a dispatch bug: `HomerServiceFindPeerBoundReceiveRelayStream` (`:23157`) is an
explicit "single-active-receive-stream" first-match scan that can attach relay work
to the wrong session.

**Correctly per-session already (do NOT touch):** producer `sendQueue` (per-stream,
`:21687`), `localSenderHeadMirror` (per-stream `calloc`+MR, `:16512` — the reference
pattern), and the DPU→host role-7 target rings (already keyed by unique `sessionUID`
via `HomerDpuDmaFindDpuToHostByteRingRef` `:4958` — the correct-resolver template).

**Invariant we enforce:** every data-carrying byte-ring — storage + control block +
metadata — is owned by and bound to a single stream/session. No ring may be
engine-owned, shared, or assume a single active stream. Legitimately-shared engine
machinery (DOCA device/contexts/PE/task pools, RDMA CM listener) stays shared, like a
NIC. The host-service byte-ring path is being deprecated; the DPU path is the focus.

## Design — a multi-region arena of homogeneous byte-ring slots, one protocol

Not "carve 3 singletons." Instead a pool of preallocated, per-session-bindable slots.

### Homogeneous slots (documented design assumption)
Every slot is one byte-ring with the **uniform superset layout**
`[ control block(slotControlBytes) | payload storage(slotStorageBytes) ]`, so ANY
slot serves ANY purpose (mirror/landing/source) from one pool. Grounding: the payload
storage is ALREADY uniform across purposes today —
`mirrorRingBytes == tupleSourceRingBytes == HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`
(8 MiB, verified `homer_service_dpu_dma.c:9931-9933`); the control block is NOT (only
landing reserves an inline `landingPayloadOffset` prefix; mirror tracks head/tail in a
separate `controlCells` staging pool, tuple-source has none). We make it uniform BY
DESIGN: give every slot the control prefix — landing uses it, mirror/source leave it
unused (≈64 wasted bytes/slot). **Document this homogeneity assumption in code +
here;** a future purpose needing a different size/control layout must revisit it (widen
the uniform slot or add a typed pool).

### Multiple registered regions (design principle — start with 2)
A single DOCA-registered region has a size ceiling (~100 MB — KB: raising the landing
region to 100 MB crashed DOCA), so the pool spans **R registered regions** (default
**2**), each its own DOCA mmap kept under the ceiling, holding `slotsPerRegion` slots.
Total slots = Σ regions. A slot belongs to exactly one region; **its absolute address
AND that region's DOCA mmap** are what the RDMA MR advertisement (landing) and the DMA
source `doca_buf_inventory_buf_get_by_addr` use — so the pool/handle must surface both.
The design generalizes to any R; 2 is the starting point.

### Sizing = plenty
`slotStorageBytes` default `HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES` (8 MiB — the max the
current geometry needs; the real basebackup ring at `slots=4,bytes=524288` ≈ 2 MiB fits
inside). Per-region slot count from a constant; with R=2 regions under the ceiling we
get plenty (e.g. 2×8 = 16 slots ≈ 128 MiB across two ~64 MiB mmaps) — well above
expected concurrency. To scale further: add regions, raise the per-region count, or
shrink `slotStorageBytes` toward the real geometry.

### Exhaustion = fatal now, interface stays open
`Bind` **returns an error** on a full pool; the *call-site policy* is fatal (log +
`exit(1)`, matching existing fatal guards) since we size to never run out. Keeping the
error on the API (not asserting inside the pool) leaves room to add a graceful handler
later (evict an idle FREE slot / queue the open / add a region) without changing the
interface.

### One unified bind/lifetime protocol for ALL uses
Sender mirror, receiver single-ring (landing == DMA source, basebackup), receiver
two-ring (landing + separate source, tuple). A stream **binds** the ring(s) it needs
(1 for single-ring/mirror, 2 for two-ring), **unbinds** at close. Acquire = prefer-own
→ adopt-free → error. Release = free the slot (fully free — no persistent per-slot
RDMA/DOCA state; the RDMA MR is re-registered per bind, the region mmap is
whole-region). Session teardown cascades. Keyed by `(serviceStreamId, purpose)`; a
session owns its streams. Mirrors the connection-pool contract, generalized to N
homogeneous ring slots across R regions.

## Refactor — extract ring resource management into `homer_dpu_byte_ring_pool.c/.h`

Pull the byte-ring resource management out of the 37k-line
`tuple_sink_service_process.c` (and the ring-arena parts of `homer_service_dpu_dma.c`)
into a dedicated module with a clean, testable API. The engine keeps physical region
allocation + DOCA mmaps and hands the regions to the pool at init; the service keeps
stream orchestration + RDMA MR registration and calls the pool API. The pool owns: the
multi-region slot table + ownership, bind/unbind/find/session-cascade, and the
addressing accessors (which resolve a bound slot's control/storage address + its region
DOCA mmap).

### Concrete Stage-0 API (`homer_dpu_byte_ring_pool.h`)

```c
/* Ring role a slot is bound for. Slot LAYOUT is uniform (homogeneous — see design note);
 * purpose is only a binding key so a two-ring stream can hold distinct landing+source slots
 * and the engine mirror-pull loop can Find a producer stream's mirror slot. */
typedef enum HomerDpuByteRingPurpose {
    HOMER_DPU_BYTE_RING_PURPOSE_MIRROR  = 0,  /* sender egress source (host->DPU pull + RDMA egress) */
    HOMER_DPU_BYTE_RING_PURPOSE_LANDING = 1,  /* receiver RDMA landing + (single-ring) DMA relay source */
    HOMER_DPU_BYTE_RING_PURPOSE_SOURCE  = 2,  /* receiver two-ring post-deform DMA source */
    HOMER_DPU_BYTE_RING_PURPOSE_COUNT   = 3,
} HomerDpuByteRingPurpose;

/* One registered region (one DOCA mmap), kept under the DOCA single-region size ceiling. */
typedef struct HomerDpuByteRingRegion {
    void             *base;      /* engine-owned region buffer */
    uint64_t          bytes;
    struct doca_mmap *mmap;      /* the DOCA mmap registered over [base, base+bytes) */
    uint32_t          firstSlot; /* global index of this region's first slot */
    uint32_t          slotCount; /* slots in this region */
} HomerDpuByteRingRegion;

typedef struct HomerDpuByteRingSlot {
    uint64_t                ownerStreamId;   /* serviceStreamId; 0 == free */
    uint64_t                ownerSessionId;  /* parentServiceSessionId (cascade + diagnostics) */
    HomerDpuByteRingPurpose purpose;         /* valid only while inUse */
    uint32_t                regionIndex;     /* FIXED at init: which region this slot lives in */
    uint64_t                offsetInRegion;  /* FIXED at init: byte offset within that region */
    bool                    inUse;
} HomerDpuByteRingSlot;

typedef struct HomerDpuByteRingPool {
    HomerDpuByteRingRegion *regions;         /* regionCount regions */
    uint32_t                regionCount;
    HomerDpuByteRingSlot   *slots;           /* calloc(totalSlotCount) */
    uint32_t                totalSlotCount;
    uint64_t                slotControlBytes; /* == engine landingPayloadOffset */
    uint64_t                slotStorageBytes; /* default HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES */
    uint64_t                slotBytes;        /* slotControlBytes + slotStorageBytes */
} HomerDpuByteRingPool;

/* A bound slot. Carries everything callers need without re-resolving (regions are fixed for
 * the pool's life): resolved control/storage addresses + the region's DOCA mmap for get_by_addr. */
typedef struct HomerDpuByteRingHandle {
    uint32_t          slotIndex;   /* UINT32_MAX == invalid */
    void             *controlAddr; /* region.base + offsetInRegion */
    void             *storageAddr; /* controlAddr + slotControlBytes */
    struct doca_mmap *mmap;        /* region.mmap covering the slot (for DMA get_by_addr) */
} HomerDpuByteRingHandle;

/* Init over engine-owned, already-DOCA-mmapped regions. Called once at engine startup. */
bool HomerDpuByteRingPoolInit(HomerDpuByteRingPool *pool,
                              const HomerDpuByteRingRegion *regions, uint32_t regionCount,
                              uint64_t slotControlBytes, uint64_t slotStorageBytes,
                              char *err, size_t errlen);
void HomerDpuByteRingPoolDestroy(HomerDpuByteRingPool *pool);  /* frees slots[]/regions[]; region bufs freed by engine */

/* Bind: prefer-own ((streamId,purpose) already bound -> its handle, idempotent across the
 * multi-step async open) -> adopt first FREE slot (stamp owner+purpose) -> false + "byte-ring
 * pool exhausted" (caller policy decides fatal). streamId==0 is a hard reject. */
bool HomerDpuByteRingBind(HomerDpuByteRingPool *pool, uint64_t sessionId, uint64_t streamId,
                          HomerDpuByteRingPurpose purpose, HomerDpuByteRingHandle *out,
                          char *err, size_t errlen);
/* Find: locate an already-bound (streamId,purpose); *found=false if none. Never mutates. Used
 * by the engine mirror-pull loop to resolve a producer stream's mirror slot. */
bool HomerDpuByteRingFind(const HomerDpuByteRingPool *pool, uint64_t streamId,
                          HomerDpuByteRingPurpose purpose, bool *found, HomerDpuByteRingHandle *out);
void HomerDpuByteRingUnbind(HomerDpuByteRingPool *pool, uint64_t streamId);          /* free ALL purposes of streamId; no-op if none */
void HomerDpuByteRingUnbindSession(HomerDpuByteRingPool *pool, uint64_t sessionId);  /* cascade across streams+purposes */
```

Config: constants `HOMER_DPU_BYTE_RING_POOL_REGIONS` (default 2) +
`HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` (default plenty under the ceiling) in the engine
header, optional env overrides read at `tuple_sink_service_process.c:42477`. The pool
lives on the engine as `HomerDpuByteRingPool byteRingPool;`, replacing the
`mirrorRing/landingRegion/tupleSourceRing` pointers + their mmaps; the three
`HomerDpuDmaGet{LandingRegion,ByteStreamMirror,TupleSourceRing}Memory` getters retire
into the accessors.

## Stages (each independently buildable + validatable; basebackup first)

### Stage 0a — pure refactor (NO behavior change)
Create `homer_dpu_byte_ring_pool.c/.h`; move the ring resource-management *bodies* behind
the API — the DPU branches of `HomerServiceEnsureLocalReceiveByteRing` (`:16401-16451`),
`TupleSinkServiceEnsureDpuMirrorMemoryRegion` (`:16690`), the tuple-source obtain
(`:31551`), and the ring-teardown in `HomerServiceResetPayloadStreamEntry`
(`:23271-23279`) — leaving thin service wrappers at the call sites. Keep today's behavior:
init the pool with **1 region, 1 slot** == the existing landing region, so every stream
binds slot 0 (still aliased — identical to today). Wire the new TU into the DPU
Makefile/meson. **Validation:** rebuild DPU; single-session basebackup + single-session
`--homer-dpu` smoke are byte-identical (pure code motion — the N=1 identity is the
correctness proof of the extraction).

### Stage 0b — multi-region arena (DOCA-allocation de-risk; DMA source untouched)
**Design decision (refinement of the original 0b/1 split, July 9):** the landing ring's
address is consumed in two places — the RDMA-target advertisement (service-side, already
handle-parameterized in Stage 0a) and the DMA relay SOURCE (engine-side,
`engine->landingRegion + landingPayloadOffset + ringOffset` at `homer_service_dpu_dma.c:9298`,
mmap `:9365`). Threading the DMA source per-slot is delicate DOCA-addressing work. To keep
each stage small and independently validated, Stage 0b does ONLY the multi-region DOCA arena
allocation + pool init, and keeps the DMA source path UNCHANGED by **transitionally aliasing**
`engine->landingRegion`/`landingMmap`/`landingRegionBytes` to the arena's region-0 slot-0.
Because single-session always binds slot 0 (first-free), this is byte-equivalent; it isolates
"does the multi-region DOCA arena allocate + mmap + still work single-session" from the
addressing plumbing. The aliasing is transitional and REMOVED in Stage 1 (when the DMA source
threads the per-slot base+mmap). Rationale: smallest safe increment; isolates the flaky DOCA
multi-mmap allocation; a whole-arena single-stage migration would be one large hard-to-debug
change.

Concretely: allocate **R=2 regions** (2 DOCA mmaps via the existing landingMmap
create/set_memrange/set_permissions/add_dev/start pattern), each holding
`HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` slots of `landingPayloadOffset +
HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`; `HomerDpuByteRingPoolInit` over both; set
`engine->landingRegion = region0.base`, `landingMmap = region0.mmap`, `landingRegionBytes =
slotBytes` (so the unchanged DMA source bounds to slot 0). Per-region ceiling check. Free the
region bufs + destroy the R mmaps in engine destroy. Mirror + tuple-source stay their own
singletons (migrated in Stage 1/2). **Validation:** rebuild DPU; single-session basebackup
byte-conservation unchanged; the byte-ring bind line now shows the arena geometry (region/slot
from a >1-slot pool).

### Stage 1 — per-session binding for BASEBACKUP + resolver fix (ACCEPTANCE GATE)
Basebackup uses a **mirror** slot (sender DPU) + a **single landing** slot (receiver DPU,
landing == DMA source); no tuple-source.
- **Bind at open:** the landing wrapper calls `HomerDpuByteRingBind(pool, sessionId,
  serviceStreamId, LANDING, &h)` → `mappingAddress = h.storageAddr` (replaces the old
  landing base at `:16422`), map the control block at `h.controlAddr`, fit-check vs
  `slotStorageBytes`, keep the `memset(control,0)` (`:16446`) now slot-scoped, advertise the
  slot sub-range via the REMOTE_WRITE MR (`:16466-16489`); the mirror wrapper binds
  `MIRROR`. Store handles on the stream (`dpuLandingRingHandle`, `dpuMirrorRingHandle`).
- **DMA-source addressing:** add `(void *slotStorageBase, struct doca_mmap *slotMmap)` params
  to `HomerDpuDmaSubmitByteRingWrite` (`:2972`), `...Terminal` (`:3314`),
  `HomerDpuDmaSubmitOneByteRingTask` (`:9126`); landing source `:9281` → `slotStorageBase +
  ringOffset`; the `doca_buf_inventory_buf_get_by_addr` uses `slotMmap` (the slot's region
  mmap, not the single engine landing mmap); bounds `:9276` / ring-fit `:3106` use
  `slotStorageBytes`. Basebackup relay `HomerServicePumpIncomingByteRingDpuRelay` (`:30936+`)
  passes the landing handle's `storageAddr` + `mmap`.
- **Mirror addressing** (engine-internal pull loop): resolve via `HomerDpuByteRingFind(pool,
  serviceSinkId, MIRROR, ...)` cached on `HomerDpuDmaRingRuntime` (`mirrorStorageBase` +
  `mirrorMmap`), threaded into pull dest (`:9256`), pull submit (`:2534/:2612/:2635`), egress
  fill `HomerDpuDmaFillMirroredByteRange` (`:4050/:4069`) — every `% mirrorRingBytes` / wrap
  uses `slotStorageBytes` and the resolved base.
- **Resolver fix** `HomerServiceFindPeerBoundReceiveRelayStream` (`:23160-23197`): add a
  `launchDiscriminatorTag` param, require `sessionState->launchDiscriminatorTag == tag` (same
  discriminator as `TupleSinkServiceFindBaseBackupOwnerSession` `:21753`); adopt strict
  unique-match from `HomerDpuDmaFindDpuToHostByteRingRef` (>1 ⇒ hard error/NULL; 0 ⇒ caller
  retries; never first-match-guess). Caller `:33901` passes `request->launchDiscriminatorTag`.
- **Unbind + cascade:** `HomerDpuByteRingUnbind(pool, serviceStreamId)` in
  `HomerServiceResetPayloadStreamEntry` (before the pointer NULLs); belt-and-suspenders
  `HomerDpuByteRingUnbindSession(pool, serviceSessionId)` in `TupleSinkServiceResetSession`
  (next to the connection release).
- **Validation (acceptance gate):** rebuild both DPUs; **2 concurrent cross-node DPU
  basebackup sessions** (tags 1 & 2, `slots=4,bytes=524288`, receivers-first) complete with
  byte conservation, **no** `RECV_CQ_FAILURE`/`observed>posted`/`DISCONNECT`, distinct
  per-session landing+mirror slot bases in DPU logs; single-session + sequential-reuse
  regression pass.

### Stage 2 — two-ring (tuple-source) for concurrent `--homer-dpu` tuple/pgbench

**Workload (corrected).** The Stage-2 workload IS `pgbench --homer --homer-dpu`, a real pgbench
flag (`postgres-citus/src/bin/pgbench/pgbench.c:306-312`, gate `:8751-8790`). `--homer` is the
direct pgbench-client → socketless-backend Homer path (no libpq, no extra backend on the measured
path). `--homer-dpu` is a *delivery variant layered on it*: the command/completion control path is
unchanged; only RESULT-tuple delivery moves off the passive host-shm result sink and onto the DPU
two-ring deform relay, arriving in a client-exported role-7 DPU→host ring drained per command to
its in-band EOS. Requires `--homer`, a remote peer, `-M simple`.
Do NOT confuse this with `citus_remote_exec_pgbench_transaction` + `citus.enable_experimental_
homer_dpu_frontend`, which are the *backend COMMAND channel* through the DPU (Stage 3 below).

**Stage 1 already covers most of the pgbench path.** The MIRROR and LANDING binds are generic, not
basebackup-specific: LANDING via `HomerServiceEnsureLocalReceiveByteRing`, whose single call site
(`tuple_sink_service_process.c:25746`) is gated on `HomerServicePayloadStreamUsesByteRing()`; MIRROR
at payload-stream open (`:29040`, `:35881`). So `--homer-dpu` already gets per-session MIRROR +
LANDING. **`engine->tupleSourceRing` is the LAST aliased data ring in the system.**

**The aliasing, confirmed.** `HomerServicePumpIncomingTupleViewDpuTwoRingRelay` (`:31564`) — per-
stream code — calls `HomerDpuDmaGetTupleSourceRingMemory(engine, ...)` (`:31681`), which returns the
single engine buffer (`homer_service_dpu_dma.c:4109`), and passes `(char *)sourceRingBase,
sourceStorage` into the deform write at `:31804` → `HomerServiceTupleSourceRingProduceRecord`
(`:31450`), which does `producedTail % sourceStorage` into that base. Two live streams ⇒ two writers,
one buffer. Both ENDPOINTS are already per-session (client `st->homer_session.sqlResultDpuStream`
in per-client `CState`; DPU→host target resolved by unique `sessionUID` via
`HomerDpuDmaFindDpuToHostByteRingRef`) — only the intermediate staging ring is shared, which is why
it looks correct from either end.

**Expected symptom differs from Stage 1 — this one is SILENT.** Stage 1's shared landing ring fed a
defensive invariant (`observed > postedTail`) and reset loudly. The tuple-source ring feeds nothing
but `memcpy` offsets, so aliasing yields *corrupted/interleaved tuple bytes*, not a crash. **The
repro must assert on decoded tuple VALUES, not exit status** (pgbench logs `homer_last_abalance`
under `-d` precisely for this).

**Repro is one command, not a two-process runbook.** Each pgbench client is its own Homer session
with its own role-7 stream, so `pgbench --homer --homer-dpu ... -c 2 -j 2` would produce two live
streams over the one shared ring — *if the path ran at all*. It does not (see STATUS below).

### STATUS (July 9, 2026): Stage 2 is BLOCKED — its acceptance gate does not exist

An attempted `-c 2` repro returned **INCONCLUSIVE**: `-c 1` never reached a working baseline.
Investigation established that **`pgbench --homer-dpu` has NEVER run end-to-end — not at `-c 2`,
not at `-c 1`, not ever.** (An earlier revision of this note guessed "probably only exercised at
`-c 1`". That was wrong; corrected here.) The checkpoint doc says so in its own words: code steps
1-6 landed, but *"step 7 the 3-machine validation"* was never performed — Part 3.5.1 (`sessionUID`)
and then Stage 1 (basebackup) took priority. See `cross_node_dpu_migration_checkpoint.md`, "Step 7".

Consequences:
1. **The `tupleSourceRing` aliasing is real but currently UNREACHABLE.** The two-ring relay is gated
   on the per-stream `dpuRelayResultStream` flag — only a flagged pgbench SQL-result stream relays.
   Basebackup and non-DPU pgbench never touch `tupleSourceRing`. Stage 2 fixes a latent bug in a
   path that has never executed.
2. **Stage 2's gate ("2 concurrent sessions → correct decoded results") presupposes ONE session
   producing correct decoded results.** It doesn't.
3. **Stage 3's precondition is unmet.** Stage 3 == the checkpoint's "step 8", which states removal
   of `citus_remote_exec_pgbench_transaction` *"comes AFTER validation confirms the new path."*
   Retiring the UDF harness before `--homer-dpu` works would delete the only functioning DPU command
   path and leave nothing behind it.

**Resequenced (decided with the user, July 9, 2026):** do the checkpoint's **step 7 bring-up first**
(get `--homer-dpu -c 1` correct end-to-end), THEN Stage 2 with a real before/after gate, THEN Stage 3.

Two failures block the bring-up, in order:
- **Failure A (blocking).** `client SQL result DPU receive referenced an unknown parent session`
  (`tuple_sink_service_process.c:33797`), after
  `TupleSinkServiceFindSessionById(sessionStates, request->parentServiceSessionId)` returns NULL at
  `:33794`. Structural cause under investigation: `HomerClientOpenSqlSession`
  (`homer_client.c:1518`) creates the `CLIENT_SQL_SESSION` in the **host** service's control SHM,
  while `HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`) places its OpenSession control
  slot inside the buffer it exports to the **DPU** (`controlSlotOffset` ~`:3555`) and names that
  host session as `parentServiceSessionId` (`:3817`). Two disjoint processes, two disjoint
  `sessionStates`. Note the handler's own comments (`:33778-33790`, `:33801-33805`) say this open
  "only has to succeed and name the parent command session" and call the sessionUID check
  "diagnostic, NOT load-bearing for resolve" — the real relay self-arms on the role-7 import — so
  the `sessionState == NULL` rejection may be an over-strict check that Part 3.5.1 made redundant.
  Contrast basebackup's selected-DPU RECEIVE, which works and appears to create a self-contained
  session rather than naming a parent.
- **Failure B (behind A).** With a host-service-owns-DOCA workaround, transactions executed and then
  hung on the first relay. Receiver looped `payload failure delivery failed without peer binding`;
  sender reported `tuple-view batch header mismatch ... batch_slot_capacity=32712 publish_bytes=56
  expected_slot_capacity=1024` — a result-queue geometry disagreement on the SEND side.

#### Classification (read-only trace, July 9, 2026) — BOTH are code gaps, not misconfiguration

**The host/DPU `sessionStates` split is DELIBERATE, not an accident.**
`session_identity_and_pairing.md:112-120` states it outright: *"There are two 9717 peer legs, same
binary, different instances… the sender's SEND session is owned by the farnet1 DPU service… SQL's
cross-node command spine, by contrast, lives entirely in the host service on both ends… The linkage
would have to travel DPU↔DPU, not host↔host"*, and *"The DPU↔DPU command-session path is net-new,
not reuse."* Part 3.5.1 therefore chose a stable unique id (`sessionUID`) as the cross-boundary
binding key **so the two session tables never need to be shared** — a rendezvous, not a shared
table: `HomerDpuDmaFindDpuToHostByteRingRef` (`homer_service_dpu_dma.c:5172`) requires nonzero,
exact, UNIQUE sessionUID and treats multiple matches as a hard error.

**`sessionUID` is the FINAL binding design, not a stopgap.** `session_identity_and_pairing.md:37-45`:
"the CROSS-NODE-STABLE binding id… the DPU relay pump resolves *which role-7 ring* to DMA landed bytes
into by this value, not by role alone"; and "we kept `sessionUID` (the earlier confusion was missing
docs, not the name)". It survives ANY ownership model because the boundary it spans is **host↔DPU
across PCIe**: the role-7 ring is exported by a host process (pgbench) and imported by the DPU
service, so something must say which imported ring belongs to which stream. Moving the command
session onto the DPU would change *which table holds the session*, not this rendezvous.

**CORRECTION.** An earlier revision of this note claimed "unifying ownership is the eventual DPU
end-state". That was an unsupported inference and is retracted. The doc states no such roadmap; for
the case it examined it *declined* to build a cross-node control channel ("at the cost of an entire
cross-node control channel") and chose the receiver-session base-compat+tag scan instead, which landed
and validated July 8. Whether SQL's command spine eventually moves DPU↔DPU is an OPEN question.

**What "net-new DPU↔DPU command spine" actually means (it is narrower than it sounds).** Already
built: DPU↔DPU RDMA peer transport + payload streams + credit doorbells; per-session ring
bind/unbind (this pool); DPU→host role-7 rings + sessionUID rendezvous; a working client→DPU control
channel (the DPU DMA-pulls a `FRONTEND_CONTROL_SLOT` from host memory,
`homer_service_dpu_dma.c:8536`, dispatched into `TupleSinkServiceHandleOpenSession` — this is how the
role-7 open already reaches the DPU); a backend-spawn path tagging
`backendChannelMode = SELECTED_DPU_DMA`; and `HomerFrontendDmaOpenCommandSession`, which already
carries a `sessionIntentSpec->opKind`. Genuinely missing per the doc (corroborated): the async
command-open machine gates hard on `opKind == CLIENT_SQL_SESSION` (`tuple_sink_service_process.c:22108`,
`:25472`), `REGISTER_MEMORY` unconditionally requires a `commandMailbox`, and there is no
DPU-control-slot command-session *opener on the receiver* (it emits only `OP_TUPLE_SINK`) — "plus
three service-side gate relaxations". Physical constraint: the socketless backend must run on the
HOST (it is a PostgreSQL process), so even a DPU-owned spine needs the DPU to reach the host's
backend-spawn region — which `SELECTED_DPU_DMA` + `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`
already do.

**Stage 3 re-scope warning (third piece of evidence).** `HomerFrontendDmaOpenCommandSession` — the
code Stage 3 proposed retiring — has now turned up three times as the closest existing thing to a
piece we need (template for a client-side DPU command open; the existing opKind-carrying opener; half
the backend-spawn spine). Stage 3's framing looks backwards: the retirement candidate may be the
HOST-SHM command path, not the DPU one. Re-scope before starting it.

**Follow-up decided (July 9, 2026): DROP the role-7 parent lookup entirely.** The `26b2e1994` fix made
a local parent optional. It should be removed outright: its only purpose is to compare the ring-open's
sessionUID against the parent command session's — but in the intended split topology the DPU service
never holds that session, so the diagnostic **can never fire**; it fires only in the same-process
workaround we do not intend to support. Keep `parentServiceSessionId` on the wire for log
traceability only. **Replace it with a diagnostic that CAN fire:** if pgbench ever mints different
sessionUIDs for the command open vs the ring open, `HomerDpuDmaFindDpuToHostByteRingRef` never
resolves and the relay retries forever — a SILENT HANG, same failure class as the tuple corruption.
Add a bounded-retry warning in the relay resolve naming the unresolved sessionUID and the role-7 rings
actually imported.

**Failure A = code gap. Minimal, design-consistent fix:** in the SQL role-7 rendezvous branch
(`tuple_sink_service_process.c:33791`), stop *rejecting* when `parentServiceSessionId` does not
resolve in the LOCAL `sessionStates` (`:33794` -> `:33797`). The DPU service legitimately cannot see
a host-service-created command session. Keep the lookup as a DIAGNOSTIC (it still catches client
mis-wires in same-process deployments), and let the load-bearing bind be the `sessionUID` match the
relay pump already performs (`:31646-31654`). The handler's own comments already say this open
"only has to succeed and name the parent command session" (`:33788`) and that the sessionUID check
is "diagnostic, NOT load-bearing for resolve" (`:33802`) — the rejection contradicts its own docs.
Basebackup escapes this because its selected-DPU RECEIVE mints a self-contained session from the
bridge generation (`homer_client.c:3028`) and never names a parent; `parentServiceSessionId` appears
ONLY in the SQL-result path (`homer_client.c:3518/3590/3797/3817`). pg_basebackup has no command
plane at all — walsender produces bytes in-process — so it has nothing to reconcile.

**Failure B = code gap (queue geometry), independent of A.** The sender validator requires
`batchHeader->slotCapacityBytes == streamEntry->stream.slotCapacityBytes`
(`tuple_sink_service_process.c:27568`). `stream.slotCapacityBytes` holds the NEGOTIATED semantic
value (`REMOTE_EXEC_SQL_RESULT_SINK_SLOT_CAPACITY_BYTES = 1024`,
`remote_execution_backend_bridge.c:77`, stored at `:23513`). But for a byte-ring-backed stream the
queue descriptor handed to the PG backend recomputes capacity from PHYSICAL ring storage:
`queueDescriptor->slotCapacityBytes = queueMapping->byteRingBytes / slotCount` (`:33539`), and the
frontend subtracts the transport prefix (`homer_tuple_queue_frontend.c:952`, stored `:959`) before
stamping it into the batch header (`:1787`). Arithmetic confirms: `8388608 / 256 = 32768`, minus
`sizeof(CitusTupleSinkTransportHeader) = 56` -> **32712**, exactly as observed.
The tell: `:33543` is a `direction == SEND && IsBaseBackup` special case, and `:23516` similarly
reconciles basebackup's byte-ring geometry — the SQL byte-ring case falls to the `else` at `:23527`
and keeps the negotiated 1024. **Basebackup's byte-ring geometry was reconciled; the SQL DPU-relay
stream's never was.**
OPEN DESIGN QUESTION for the B fix: which capacity is authoritative — the negotiated semantic 1024,
or the byte-ring-derived 32712? A byte ring has no slots, so "slot capacity" degenerates to a max
object size; forcing 1024 would silently cap batching.

#### Failure A FIXED, Failure B REPRODUCED under the intended topology (July 9, 2026)

Bring-up run with `26b2e1994` deployed to both DPU trees, intended split topology (host services both
ends + DPU-native relay services both ends), `pgbench --homer --homer-dpu -c 1 -j 1 -t 200`:
- **Failure A is gone.** No "unknown parent session". The farnet0 DPU service accepted the role-7 open
  and pgbench progressed through session warmup, `BEGIN`, and `UPDATE ... rows=1`.
- **Failure B reproduced verbatim** on the farnet1 HOST service, on the first `SELECT abalance`:
  `batch_slot_capacity=32712 publish_bytes=56 expected_slot_capacity=1024`
  (`tuple_sink_service_process.c:27574`), then `payload stream marked ABORTING for peer reset`. So B is
  NOT a workaround-topology artifact.

**Root-cause suspect (strong, but see the unresolved question).** `TupleSinkServicePayloadRingBytes`
(`:6246`) was changed by the mirror-1:1 work to return the CONSTANT
`HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES` (8 MiB) instead of `slotCount * payloadSlotBytes`; its own
comment (`:6234`) says *"slotCount (the count) no longer sizes the ring; slotCount is retained only in
the signature for call-site stability and is intentionally unused here."* But
`queueDescriptor->slotCapacityBytes = queueMapping->byteRingBytes / streamEntry->stream.slotCount`
(`:33539`) still divides by `slotCount`. That division only recovers the negotiated per-record capacity
when `slotCount * slotCapacityBytes == HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`. It no longer does.
SQL result sink: `slotCount=256`, `slotCapacityBytes=1024` (`remote_execution_backend_bridge.c:69,77`)
⇒ `8388608/256 = 32768`, minus `sizeof(CitusTupleSinkTransportHeader)=56` ⇒ **32712**, exactly as
observed. The producer stamps that derived value into the batch header
(`homer_tuple_queue_frontend.c:1787`, from `sinkHandle->slotCapacityBytes` set at `:959`), while the
sender validator (`:27568`) compares it against the negotiated `stream.slotCapacityBytes` = 1024.

**UNRESOLVED — resolve this BEFORE fixing.** By that arithmetic Citus COPY should fail too:
`CITUS_TUPLE_SINK_DEFAULT_SLOT_COUNT=8`, `CITUS_TUPLE_SINK_DEFAULT_SLOT_CAPACITY_BYTES=524288`
(`homer_abi_version.h:29,31`) ⇒ `8388608/8 - 56 = 1048520 != 524288`. But COPY demonstrably works.
So either COPY's SEND descriptor does not take the `:33539` byte-ring branch, or its
`stream.slotCapacityBytes` is reconciled elsewhere, or its producer is a slot queue rather than a
byte-ring producer. **Which it is determines whether `:33539` is dead-wrong for every byte-ring stream
or only for this one**, and therefore whether the fix is local or shared-path. Do not fix blind.

Fix options once that is settled: (i) descriptor carries the negotiated `payloadSlotBytes` (caps batch
size at 968B payload — safe, but may cost batching); (ii) `stream.slotCapacityBytes` adopts the
byte-ring-derived value for byte-ring streams (better batching, matches "a byte ring has no slots");
(iii) validator compares `<=` instead of `==` (loosest; loses a real geometry check).

#### Secondary robustness gap found during the bring-up (record, do not fix yet)
An ABORTED byte-ring stream leaves the pgbench client hung FOREVER with no client-visible error — the
service marks the stream ABORTING and resets the peer, but no error/EOS ever reaches the client. Same
"silent" failure class as the tupleSourceRing aliasing. Worth a dedicated error-propagation fix.
(Also: `timeout` + `sudo` did not deliver SIGTERM to pgbench; use `pkill -9` in runbooks.)

**Change.**

**Change.** Two-ring stream binds a SECOND handle (`SOURCE`). Bind at the tuple-source obtain
(`:31681`); deform write uses `sourceBase = sourceHandle.storageAddr` with `% slotStorageBytes`; DMA
read reuses the `(dpuRingBase, dpuRingBytes, dpuRingMmap)` params already threaded through
`HomerDpuDmaSubmitByteRingWrite` in Stage 1a. Unbind the source handle alongside landing (the
existing `HomerDpuByteRingUnbind(pool, serviceStreamId)` frees ALL purposes of the stream, so no new
unbind call is needed). Receive resolver here is already per-`sessionUID`-correct.

**Slot sizing is already correct by construction.** The relay enforces
`hostRingStorageBytes == sourceStorage` (`:31701`) so one `absoluteStart` addresses both the source
(DMA read) and the host role-7 ring (DMA write). Both are `HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`,
which IS the pool's `slotStorageBytes` — so a bound SOURCE slot satisfies that check without
touching it. The homogeneous-slot design assumption is load-bearing here, not merely convenient.

**Finish the invariant in the same commit.** After the bind lands, delete `engine->tupleSourceRing`,
`engine->tupleSourceRingMmap`, `engine->tupleSourceRingBytes`, and
`HomerDpuDmaGetTupleSourceRingMemory` (one caller), exactly as `mirrorRing` was retired. That
completes "no engine-owned data-carrying byte-rings". Relocate, do not delete, the geometry rationale.

**Validation:** `-c 2 --homer-dpu` repro FIRST (must fail on decoded values); then after the fix,
2 concurrent sessions → disjoint SOURCE slot indices in the DPU log, correct decoded results,
plus `-c 1` regression and a basebackup regression (shared bind/unbind code). Build the smoke
targets too, not just `service-bin`.

### Stage 3 — RE-SCOPED (July 9, 2026): promote the DPU command channel, do NOT retire it

**The original framing was backwards.** Stage 3 originally proposed retiring the server-side DPU
frontend command channel. A read-only trace (verified independently) shows that code is the closest
existing implementation of the command spine the DPU migration needs.

**Verified findings**
- `HomerFrontendDmaOpenCommandSession` (`homer_frontend_dma.c:1039`) is NOT a stub in DOCA builds. It
  opens a DOCA control channel, exports bridge/backend mailboxes, imports them into the DPU service,
  submits a HOST socketless-backend spawn (`HomerFrontendDmaSubmitBackendSpawn`, `:1352` →
  `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`, `homer_frontend_dma_lifecycle.c:192/283`), and
  supports START/POLL (`HomerFrontendDmaStartCommand`, `:1093`).
- **The GUC's help text is STALE.** `shared_library_init.c:2596` still claims the path "stops after
  bridge-memory setup with an explicit not-implemented error". DOCA builds go well beyond that.
- The reject gate and the open gate are DIFFERENT paths, not a contradiction:
  `RemoteExecutionRejectDpuFrontendChannelIfSelected` rejects unsupported *tuple-sink/fallback* ops
  (`homer_frontend_control.c:644`, `:721`, `:1099`), while **command-session open bypasses it** and
  calls the real opener (`homer_frontend_control.c:803`, verified: it calls and `return`s).
- **Basebackup does NOT use `backendChannelMode = SELECTED_DPU_DMA`.** Its selected-DPU open emits
  `CITUS_REMOTE_EXEC_CONTROL_OP_TUPLE_SINK` with `sessionKey.opKind = OP_BASE_BACKUP`
  (`homer_client.c:2795`, `:2809`) — a tuple-sink open, not a command session. So the DPU backend-spawn
  spine is live ONLY behind the experimental frontend command path and `homer_frontend_dma_smoke.c:118`.
  It is *working* code with *no validated consumer* — which is exactly why it read as dead.
- The DPU service does not put selected-DPU sessions in `sessionStates[]`. Ordinary command sessions
  are created by `TupleSinkServiceHandleOpenCommandSession` (`:33635`); selected-DPU semantic state is
  created lazily by `HomerServiceDpuFindOrCreateSelectedSession` (`:38653`, called `:39099`) in a
  SEPARATE table. This independently confirms the removed role-7 parent lookup could never have
  resolved on the DPU, regardless of deployment.

**Corrected per-item verdicts**
| Item | Verdict |
|---|---|
| GUC `citus.enable_experimental_homer_dpu_frontend` + its two gates | **KEEP; fix the stale help text.** The reject gate still guards unsupported non-command paths; the open gate IS the command path. |
| `HomerFrontendDmaOpenCommandSession` + frontend DMA lifecycle | **KEEP AND PROMOTE.** This is the selected-DPU command opener and the HOST backend-spawn leg. |
| `citus_remote_exec_pgbench_transaction` UDF | **KEEP AS-IS; retire later**, per checkpoint step 8 — only after `--homer-dpu` is validated. No source caller besides UDF invocation. |

**MUST NOT touch** (basebackup calls the first unconditionally; `--homer-dpu` result delivery rides the
third): `HomerClientOpenBaseBackupStreamSelectedDpu` (`homer_client.c:2551`, called from
`basebackup_homer.c:289`), `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:2922`),
`HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`, via `:3925`, `pgbench.c:9559`).

**The real remaining delta (bounded, not net-new).** The DPU command spine is *mostly built below the
client API*: bridge import, frontend control-slot pull, backend mailbox publication, selected-DPU
backend spawn, and the frontend completion path all exist. What is missing:
1. A client-side `HomerClientOpenSqlSessionSelectedDpu` — combine the SQL command-session request
   fields (`homer_client.c:1558-1575`) with the selected-DPU export/control-slot machinery basebackup
   already uses (`:2644`, `:2675`, `:2727`, `:2783`). Today `--homer-dpu` still opens its command
   session through host-service SHM (`HomerClientOpenSqlSession`, `:1518`, from `pgbench.c:9559`).
2. Preserve the HOST backend-spawn leg — the socketless backend is a PostgreSQL process and must run on
   the host (`homer_frontend_dma_lifecycle.c:192`).
3. Three-ish service-side gate relaxations, all small: async routing admits only SQL/client-SQL opens
   (`TupleSinkServiceOpenRequestNeedsAsyncLocalControl`, `:34817`); async command-create rejects
   non-SQL/client-SQL (`TupleSinkServiceProgressCommandOpenAsyncOp`, `:35172`); REGISTER_MEMORY
   unconditionally requires `sessionState->commandMailbox.mailbox` (`:35238`). The peer command-session
   open has the same opKind rejection plus mailbox checks (`:37385`, `:37395`, `:37406`).

**Note on ordering.** None of this is a prerequisite for `--homer-dpu`; it is the path toward unified
session ownership (the OPEN question recorded above). Do not start it before `--homer-dpu -c 1` is
validated.

## Safety, gates, and caveats (strict)

1. **Drain-safe unbind.** Release runs in `HomerServiceResetPayloadStreamEntry`, reached only
   via `TupleSinkServiceMaybeReclaimSinkAndSession` (`:24850`, requires `!peerBindingActive`),
   and `peerBindingActive` clears only in `HomerServiceClearPayloadStreamPeerBinding`
   (`:24293`), gated by `HomerServicePayloadCloseCanClearBinding` → for a byte-ring receiver
   requires `HomerServiceReceivePayloadTransportIsDrained` (`publishedTail==consumedHead`). So
   the far-sender RDMA-write side is quiescent before release; the relay DMA-read side is
   covered by `byteRingWriteInFlight` (`homer_service_dpu_dma.c:391`) + the existing
   `MarkImport…CloseReleased` releases at `:24338/:24365`. **Add a loud assertion** (near the
   `:23301` `exit(1)`) that a landing/source slot is not released while `byteRingWriteInFlight`
   (query `HomerDpuDmaGetByteRingWriteFrontier` `writeInFlight` `:5124`).
2. **Homogeneity is a documented design assumption** — uniform slot size + control-block prefix;
   landing uses the prefix, mirror/source leave it unused. Comment it at the pool + call sites
   and in [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md). Revisit if a future purpose needs
   a different size/control layout.
3. **Never a whole-region memset.** The landing control memset (`:16446`) stays slot-scoped
   (operates on `localReceiveByteRing.control`, now `h.controlAddr` inside the slot).
4. **Exhaustion is a clean error, not aliasing.** `Bind` fails → open fails (fatal per call-site
   policy now; API open for a future graceful handler). Never silently share a slot.
5. **Slot identity across re-open.** `serviceStreamId` is monotonic (`:23333`); a re-opened
   stream gets a fresh id ⇒ fresh slot. Assert an adopted-free slot's previous `ownerStreamId`
   differs from the requester.
6. **Refactor safety.** Stage 0a is pure code motion — prove byte-identity via the single-session
   regression before adding pool logic; do not mix behavior change into 0a.
7. **Resolver strict-unique vs tag reuse.** `(base-compat + tag)` must be unique per live pairing;
   a reused tag while an old session drains ⇒ strict finder refuses ⇒ open retries (correct).
   Confirm the tag allocator is drain-safe or also scope by `sessionUID`.
8. **Per-region mmap addressing.** The DMA `get_by_addr` must use the slot's REGION mmap (from the
   handle), not a single engine mmap — the whole point of multi-region. The mirror engine-side
   `Find` vs the landing/source explicit-param seam is deliberate; comment both.

## Connection-binding fix disposition

**Status: KEPT, committed with Stage 0a. Reclassified as fault-isolation hardening, not
correctness for this bug.**

### How it works
`outgoingConnections[]` is a 64-slot pool keyed by `(peerNodeId, peerHost, peerControlPort,
trafficClass)`. Before the fix `TupleSinkServiceFindOutgoingPeerConnection` returned the FIRST
endpoint/class match regardless of user, so two concurrent sessions to the same peer shared one
QP / send-CQ / recv-CQ. The fix added `ownerServiceSessionId`
(`remote_execution_peer_transport_rdma.c:591`) and made the finder (`:6310`) a three-way scan,
gated on `TupleSinkServiceTrafficClassRequiresSessionOwnership` (`:2482`):
- **prefer-own** (`:6344`) — pins a session to the connection it is mid-way through establishing
  across the multi-step async open.
- **adopt-free** (`:6350`, stamp at `:6364`) — reuse a released, still-established sibling in one
  step.
- **skip owned-by-other** (`:6355`) — never co-own; fall through to NULL → create new.

Release is `TupleSinkServiceReleaseOutgoingPeerConnectionsForSessionRdma` (`:8413`), called from
the `ResetSession` funnel: it flips `owner → 0` but KEEPS the RDMA connection established (no
teardown, no generation bump, no QP/CQ churn). It is a full pool scan, not a per-stream unbind,
because a session may bind several payload streams to its one owned connection (`:8404`).
`CRITICAL_CONTROL` intentionally stays shared (its `localMailbox` ring is a correlated,
multiplex-safe shared ring). The predicate at `:2482` is a one-line scope toggle.

### Why we originally thought we needed it, and what survived
The **stated** motivation is written into `:2475-2477`: *"only PAYLOAD-class connections carry a
per-session recv-CQ that must not be shared."* That was the HYPOTHESIS, and Test 2 **falsified**
it — separate connections, separate recv-CQs, identical `RECV_CQ_FAILURE`.

Root cause of the falsification: the credit doorbell dispatch keys off the **token** carried in
the RDMA WRITE-WITH-IMMEDIATE's immediate data, not off the connection the completion arrived on.
`TupleSinkServiceDispatchPeerPayloadDoorbell` (`tuple_sink_service_process.c:27385`) decodes it via
`HomerDecodePayloadDoorbellToken` (`:27401`) → `(streamIndex, tokenGeneration)` → `streamEntries[
streamIndex]`, then applies credit in `HomerServiceApplyPayloadSenderCreditDoorbell` (`:27330`).
Credits therefore route to the right stream even on a shared connection. In the original bug the
dispatcher's binding checks all PASSED; the error came from `Apply`'s `observed > postedTail`. The
credit was delivered to the correct stream carrying the WRONG VALUE — the other session's
`consumedHead`, read from the shared landing ring. Connection identity is a *transport-lane*
property; the bug lived in *per-stream state*.

Reasons that DO survive, and are why we keep it:
1. **Fault isolation / blast radius.** The connection is the reset unit: one session's payload-
   connection error → reset → `CM DISCONNECTED` → every session bound to it dies. Sharing means one
   basebackup's fault kills its innocent neighbour. Holds regardless of credit-routing correctness.
2. **Teardown safety.** Without an owner, session A's `ResetSession` tears down (or generation-
   bumps) a connection session B is actively using. Owner + release-to-free makes teardown
   well-defined.
3. **Restored strength of the dispatcher's binding check.** The stream stores `peerConnectionHandle`
   + connection generation and the dispatcher validates them; under sharing that check degenerates
   to "yes, that's the one connection" and cannot discriminate.
4. (Softer) Two concurrent multi-GB streams sharing a QP serialize their RDMA WRITEs and interleave
   their credit WIMMs — head-of-line coupling.

**Open item:** the comment at `:2475-2477` still states the falsified recv-CQ rationale. It must be
rewritten to say *fault isolation + teardown safety*. Proving the binding is strictly unnecessary
would require reverting it and re-running the concurrent test; keeping it is the safe default.

## Follow-up cleanups after Stage 1 (mirror-slot / vestigial singleton)

Done as a post-Stage-1 cleanup pass. Recorded here because it turned up a real defect, not just
style.

**What was actually wrong (an earlier read of this was mistaken).** The mirror-slot cache on
`HomerDpuDmaRingRuntime` is NOT write-only: `HomerDpuDmaResolveMirrorSlot`
(`homer_service_dpu_dma.c:4038`) does short-circuit on it, and two consumers read it as the
AUTHORITATIVE geometry — `HomerDpuDmaMirroredByteRangeMatchesRingFill` (`:4497`, computes
`releasedByteTail % dpuMirrorStorageBytes` and address-checks against `dpuMirrorStorageBase`) and
the byte-ring pull-acceptance guard (`:9883`). Deleting the cache, as first proposed, would have
broken both. Only `dpuMirrorSlotIndex` was genuinely dead. The `ClearMirrorSlotCache` comment
claiming the cache is "advisory" was itself wrong and has been corrected.

**The real defect the const-casts pointed at.** `HomerDpuDmaFillMirroredByteRange` declared a
`const HomerDpuDmaEngine *` and then cast it away, because it genuinely mutates the cache. Chasing
that lie surfaced:
- `HomerDpuDmaCopyByteRingSmokeBuffer` still `memcpy`'d from the retired singleton
  `engine->mirrorRing` — a buffer nothing writes since Stage 1a-mirror redirected PULL into the
  per-session slot. Its byte-ring pull verification was silently reading stale zeros.
- **`dpu-tcp-transport-smoke-bin` did not COMPILE at Stage-1 HEAD** (`too few arguments to
  HomerDpuDmaSubmitByteRingSmokePull` — Stage 1a-mirror widened the signature but never updated the
  two smoke call sites). Nobody noticed because every validation built only `service-bin`.
  **Caveat for future stages: build the smoke targets too, not just `service-bin`.**

**Changes applied:** removed the write-only `dpuMirrorSlotIndex`; corrected the cache comments;
de-consted `HomerDpuDmaFillMirroredByteRange` + `HomerDpuDmaMirroredByteRangeReadyForServiceSink`
and dropped the casts; repointed `HomerDpuDmaCopyByteRingSmokeBuffer` at the bound MIRROR slot; the
standalone smoke now performs its own MIRROR bind (never unbound — engine teardown at process exit
reclaims it; production MUST unbind); deleted the callerless
`HomerDpuDmaGetByteStreamMirrorMemory`; removed the vestigial `mirrorRing` / `mirrorRingMmap` /
`mirrorRingBytes` (buffer, DOCA mmap create/start/destroy, free).

**Invariant relocated, not lost.** The 1:1 sizing invariant (a MIRROR slot's payload storage MUST
equal the host source ring's storage so `absolute % slotStorageBytes` and `absolute %
ringStorageBytes` coincide, carrying the host wrap-gap protocol over so no record's framing header
straddles the mirror wrap; both from `HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES`) was documented on the
deleted `mirrorRingBytes`. It still holds — it now governs EVERY mirror slot — and has been moved to
the `HomerDpuByteRingPoolInit` call site where `slotStorageBytes` is supplied.

### Validation (July 9, 2026) — PASS
Both DPUs rebuilt natively (aarch64 `service-bin`, `CPPFLAGS='-D_GNU_SOURCE'`); no host artifact
changed. 4-role runbook; preflight clean before and after.

- DOCA lifecycle survived the `mirrorRingMmap` removal — both DPUs logged
  `byte-ring arena ready: regions=2 slots=8 slotStorage=8388608 slotBytes=8388672`.
- **Slot recycling works:** the 3 sequential Test-1 runs each bound sender `purpose=0 slot=0`
  (unbind → adopt-free reuse).
- **Concurrent sessions take distinct slots:** sender `purpose=0 slot=0` + `slot=1`
  (`offset=8388672`); receiver `purpose=1 slot=0` + `slot=1`.
- Byte conservation: 23,293,876,218 / 23,293,880,826 B sequential; concurrent pair
  23,293,894,138 B each. All 5 senders + 4 receivers rc=0.
- No `RECV_CQ_FAILURE`, `observed>posted`, `ambiguous receive-relay`, or `pool exhausted`.

**Log-reading caveat (cost us a false alarm).** Two `CM event=DISCONNECTED status=0` lines DO
appear in the receiver-DPU log at shutdown (lines 179-180 of 183), after the last stream drained
(`final_tail == final_head == 24831936232`), its sink+session+import were reclaimed, and just
before `DOCA context state 2 -> 0`. `DISCONNECTED` is the SAME line in the catastrophic and the
graceful case; what discriminates them is its position relative to drain/reclaim and whether
`publishedTail == consumedHead` first. A bare grep for the bug's fingerprint matches normal
shutdown. Read the surrounding lines, never the grep count.

**Still UNVALIDATED at runtime:** the smoke fix itself. `HomerDpuDmaSubmitByteRingSmokePull` and
`HomerDpuDmaCopyByteRingSmokeBuffer` are smoke-only entry points — production basebackup traffic
does NOT exercise them, so the 4-role gate does not cover them (a validation report claiming
otherwise was wrong). They are compile-verified only; running
`homer_dpu_tcp_transport_smoke` per CLAUDE.md would close that gap.

## Verification (end-to-end)

Rebuild the aarch64 `service-bin` on **both** DPU trees (`~/dbcomm/citus-dbcomm`) per stage; host
artifacts unchanged (service-only edits). Acceptance = concurrent 2-session basebackup (Stage 1)
and concurrent `--homer-dpu` (Stage 2), each with byte conservation, no reset, distinct
per-session slot bases in DPU logs, plus single-session regressions. The 4-role runbook
(farnet1 host PG + `pg_basebackup` senders; farnet1 DPU sender service; farnet0 DPU receiver
service; farnet0 host `--homer-receive` consumers), env, and ordering are recorded in the scratch
runbook used for Test 1/2.

## Related

- [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) — byte-ring wrap protocol, mirror 1:1
  invariant, and the "single active stream" caveat this plan removes.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md) — the Stage-5
  concurrency block, the falsified connection-binding attempt, and the milestone context.
- [session_identity_and_pairing.md](../../../future-directions/citus/transport/session_identity_and_pairing.md)
  — sessionKey / sessionUID / `(node,tag)` discriminator semantics the resolver fix relies on.
