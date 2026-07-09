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
- **Status:** approved plan, implementation in progress (staged). The mirror plan
  file lived at `~/.claude/plans/here-s-a-snippet-on-linked-papert.md`; this doc is
  the canonical KB copy — keep it in sync as stages land.
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
  - Stage 1a-mirror (mirror per-session: bind + pull dest + egress source), Stage 1b
    (resolver + concurrent acceptance), Stage 2: pending.
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
Two-ring stream binds a SECOND handle (`SOURCE`). Bind at the tuple-source obtain (`:31551`);
deform write `HomerServiceTupleSourceRingProduceRecord` (`:31319`) uses `sourceBase =
sourceHandle.storageAddr` with `% slotStorageBytes`; DMA read reuses the `(slotStorageBase,
slotMmap)` params (`:9270/:9265/:3098`). Unbind the source handle alongside landing. Receive
resolver here is already per-sessionUID-correct (`:31521`). **Validation:** 2 concurrent
`--homer-dpu` sessions → disjoint slot bases, correct decoded results; single regression.

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

The already-implemented per-session outgoing-connection binding
(`remote_execution_peer_transport_rdma.c`) is now **orthogonal hardening, not correctness** for
this bug (credit dispatch keys by token/sessionUID, not connection). Keep it (validated, prevents
QP cross-talk); it stays UNCOMMITTED until Stage 1 validates, then commit together or re-evaluate
simplifying its ownership. Do not remove it as part of this work.

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
