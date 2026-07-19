# DPU gate concurrency limits — the byte-ring pool caps the gate at 8 clients

<!-- kb-summary: Current DPU gate concurrency limits imposed by the byte-ring pool and related fixed-capacity resources. -->

**Status: UNDERSTOOD and BOUNDED (2026-07-13). Not a regression; a pre-existing capacity limit, found the first
time the DPU gate was ever run above one client.** Decision: **cap the gate at 8 clients**, raise the knob if we
need more. The engine-fatal that appears above the cap is **gated behind the cap** and is deliberately parked.

> ## ⚠ RE-GROUNDED 2026-07-19 — THE ARITHMETIC BELOW IS PRE-S6 AND STALE. Read this first.
>
> **S6 Stage 2 (`a5e7d2fdb`) changed BOTH sides of the equation**, so the numbers below no longer describe the code:
> - slots/region was raised **4 → 8**, so the pool is now `HOMER_DPU_BYTE_RING_POOL_REGIONS` (2) ×
>   `HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` (8) = **16 slots per DPU** (`homer_service_dpu_dma.h:40-42`), not 8;
> - a `--homer-dpu` session now takes **TWO** receiver slots, not one: **LANDING + SOURCE**
>   (`tuple_sink_service_process.c:19069` + `:19100-19107`), because the per-session SOURCE ring retired the
>   `tupleSourceRing` singleton. A **basebackup** session still takes **ONE** (landing==source, not TUPLE_VIEW_BATCH).
>
> ⇒ The client cap coincidentally stayed ~8 (16 ÷ 2), but **the reasoning below is wrong** — do not re-derive from
> it. Current budget + the mixed pgbench/basebackup workload it constrains:
> [`dpu_byte_ring_pool_per_session_plan.md`](./dpu_byte_ring_pool_per_session_plan.md) "Capstone" section.
>
> **Everything below about the engine-fatal blast radius and the two parked defects remains ACCURATE and is why
> headroom still matters.**

## The limit

**CURRENT (post-S6, authoritative):** each client SQL session consumes **TWO** DPU byte-ring slots (LANDING +
SOURCE) out of `HOMER_DPU_BYTE_RING_POOL_REGIONS` (2) × `HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` (8) = **16 per DPU**
⇒ the gate still caps at **8 clients** (16 ÷ 2), and `-c8` is **exactly 16/16 with ZERO headroom**. Full derivation
(including the mixed pgbench+basebackup case): the `HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` entry in
[`CONTRACTS.md`](./CONTRACTS.md).

> ⚠ **The original sentence read:** *"Each client SQL session consumes ONE DPU byte-ring slot … 4 × 2 regions = 8."*
> Both halves changed in S6 Stage 2 (`a5e7d2fdb`) — slots/region 4→8 AND per-session 1→2 — so the **cap stayed 8
> while the arithmetic behind it became wrong.** The `all 8 slots in use` strings quoted below are historical log
> excerpts from the pre-S6 build; today the same message would read `all 16 slots in use`.

| clients | result |
|---|---|
| 1, 4, **8** | ✅ pass (160/160, 0 failed) |
| 10 | ❌ 8 sessions get a slot, **exactly 2 do not** → partial failure + engine death |
| 12, 16 | ❌ worse |

**The DPU says so itself, and names its own knob:**
```
service-owned P3 result peer-open failed session=6 sink=6 detail=byte-ring pool exhausted:
  no free slot for session=6 stream=6 purpose=0 (all 8 slots in use;
  raise HOMER_DPU_BYTE_RING_SLOTS_PER_REGION / regions)
```

⚠ **`-c 8` sits at exactly 8/8 — zero headroom.** Prefer `-c 4` when you want margin.

## The engine-fatal above the cap — real, but PROVEN gated behind exhaustion

Above 8 clients, the sessions that lose the byte-ring race do not merely fail — **they take the whole DMA engine
with them**:
```
homer DPU DMA: grouped-control semantic validation failed: host publication generation does not match descriptor
  [ring=5 role=5 bound_session=0 accepted_epoch=0 line_epoch=1 accepted_tail=0 line_tail=112
   line_generation=... import_generation=...]
tuple-sink service: DPU command-pull submit failed: DPU DMA engine is in fatal error state
```
`fail:` sets **`engine->fatalError = true`** (`homer_service_dpu_dma.c:13296`) — **one ring's identity check kills
command-pull, byte-ring pull and PE drain for EVERY session on that DPU.** At `-c 10` that is why *five* clients
died when byte-ring exhaustion only explains *two*.

The failing ring's state (`bound_session=0`, `accepted_epoch=0`, `accepted_tail=0` — every field
`HomerDpuDmaResetRingTenancyState` memsets) says the ring was **released**, while its host line still carried the
dead tenant's frontier (`line_epoch=1`, `line_tail=112`, foreign generation). That is *exactly* the state
`tenancyBaselinePending` exists to suppress.

### ✅ PROVEN: a CLEAN session release does NOT trigger it

`-c 8` (below the pool limit ⇒ exhaustion impossible), **run twice with nothing restarted**, so run 1's eight
sessions all released their rings before run 2 began:

> **Zero occurrences** of `semantic validation failed`, `fatal error state`, `PE drain failed`,
> `byte-ring pool exhausted`, `peer-open failed`, or `ALARM` — on **both** DPUs, at **three** checkpoints
> (after run 1, after run 2, and ~20 s later). Both runs 160/160, 0 failed.

So the fatal requires a session that dies with a **FAILED result peer-open**, not merely a released ring. It lives
strictly **downstream of byte-ring exhaustion**, which the 8-client cap makes unreachable.

**This also clears the July 13 discovery-scanner fix** (`0b9569e20`): the new scanner reaches *every* ring on
*every* pass, including released ones, and a clean teardown still produces no fatal.

### Parked, not forgotten — two defects that remain real

1. **BLAST RADIUS.** A per-ring identity failure should fail *that ring and that session*, not the engine. As
   written, one bad ring is a whole-DPU outage. There is no recovery from `fatalError` short of a service restart.
2. **A failed result peer-open leaves the ring in a state that trips the validator.** The exhaustion path does not
   clean up after itself.

Both are only reachable above the cap. Revisit when the cap is raised.

## Raising the cap

`HOMER_DPU_BYTE_RING_SLOTS_PER_REGION` (`homer_service_dpu_dma.h:41`), then rebuild **both DPUs**. ⚠ Fixing the cap
alone is **not** enough for a real multi-client story — the two defects above must be fixed too, or the next
exhaustion (at whatever the new cap is) does the same thing.

⚠ Note also `HOMER_FRONTEND_ARENA_SLOT_COUNT = 16` — the arena caps *sessions* at 16 independently, and the arena
is already **16 × 13.31 MiB ≈ 213 MB** of DPU-mapped host memory, dominated by a 10 MiB result byte ring per slot.
**The route to many sessions is shrinking the per-slot footprint (the S4 union-bloat item), not bumping counts.**

## ⚠⚠ TWO PROCESS FAILURES THAT COST FOUR VALIDATION CYCLES

**1. The client prints the SYMPTOM; the DPU prints the CAUSE — and we read the client.**
The client said `DPU result relay peer-open failed for result stream 10`. The DPU said `byte-ring pool exhausted:
all 8 slots in use; raise HOMER_DPU_BYTE_RING_SLOTS_PER_REGION`. One of those is a diagnosis. **Three hypotheses
were raised and refuted from the source** — busy-poll CPU oversubscription (refuted: dedicated 1:1 CPUs on a
96-core box changed nothing, and the failure point was *uniform*, which starvation is not); a 16-deep pool cliff
(refuted: the cliff is at 8, and the failure is *partial*, which a hard cap is not); a generic release race
(refuted above) — **while the answer sat in a log file naming its own knob.**
**When a client reports a Homer failure, read the DPU log BEFORE opening the source.**

**2. WE WERE GREPPING FOR THE WRONG STRING.** Validation greps the service logs for `ALARM`. **The engine-fatal
lines are not tagged `ALARM`.** So every "zero ALARMs, clean" verdict recorded before 2026-07-13 was blind to the
engine dying — and because the engine can die *after* the last transaction, **a run can PASS while the engine is
dead underneath it.** Always grep:
```sh
grep -aiE 'ALARM|semantic validation failed|fatal error state|PE drain failed|pool exhausted|peer-open failed' \
  <dpu_service.log>
```

## Related
- `post_gate_basebackup_open_stall.md` — the discovery-starvation P0 fixed the same day; this is *not* it.
- `resource_retirement_contract_audit.md` — the blast-radius and failed-peer-open cleanup defects belong there.
- `../../../operations/farnet_operational_hazards.md` — the grep rule and the client-vs-DPU-log rule.
