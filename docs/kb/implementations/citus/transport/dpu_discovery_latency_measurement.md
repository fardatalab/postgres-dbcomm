# DPU per-command discovery latency (D) — measurement, method, and findings

<!-- kb-summary: The S6 Track B measurement of the DPU's per-command command-discovery latency D: a same-clock censoring upper bound on "host publishes a role-1 command → the DPU notices it". Result: D ~38µs, cadence-limited (~44µs poll interval) — the largest single DPU-INGRESS component but NOT the dominant per-command latency. Records the method (default-off DPU-local spans), the two lifetime bugs found and fixed, and the bottleneck question the result raises. -->

## Current status

**COMPLETE (validated 2026-07-17).** Per-command DPU command-discovery latency **D ≤ ~38µs on average**
(same-clock censoring upper bound; min ~15µs, max ~175µs over 40 role-1 commands at `-c1`), and it is
**cadence-limited** — set by the DPU's ~44µs command-ring poll interval, D ≈ cadence/2. Discovery is the largest
single component of DPU-side command *ingress* (~65µs total) but a small fraction of the full per-command path. The
alternative instrument (the bridge-ABI "echo marker", §2.1 of the plan) was **not needed** and was skipped.

This doc is the canonical findings record. The design/plan is in
[`s6_native_homer_dpu_and_latency_plan.md`](./s6_native_homer_dpu_and_latency_plan.md) (§2, §2.5.x, §2.6); the code is
the default-off `HOMER_DPU_DISCOVERY_SPANS` instrumentation in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c` (landed citus `f5f4480f3`).

## The question

The DPU discovers host-published SQL commands by **busy-polling**: on every scheduler pass it DMA-reads each host
ring's "publish line" and checks whether the publication epoch advanced. D is the latency of that discovery — from the
host publishing a role-1 (`FRONTEND_CONTROL_SLOT`) command to the DPU noticing it. The S6 question: **does this
discovery cadence dominate per-command latency?** (Background: the old 235µs synchronous START round trip was already
removed by P-START, so the remaining suspect on the DPU side was discovery cadence.)

**You cannot compute an absolute D** — the host publish and the DPU accept are different clock domains and no
correlated timestamp crosses the wire. So we measure a **censoring UPPER BOUND** on the DPU's own `CLOCK_MONOTONIC`.

## Method — DPU-local discovery spans

Default-off compile gate `HOMER_DPU_DISCOVERY_SPANS` (mirrors `HOMER_SERVICE_PEER_TRANSPORT_STATS`): OFF ⇒ compiled
out, zero perf-build cost; ON ⇒ allocation-free, bounded, single-threaded (no atomics). Four per-role spans
(count/total/min/max + a pow2-ns histogram), recorded in the grouped-control-read and command-pull DMA paths:

- **`grouped_submit_interval`** — interval between consecutive command-ring poll submits (the poll **cadence**).
- **`grouped_submit_to_accept`** — submit → fresh-epoch accept (the discovery DMA read cost).
- **`censoring_bound_D`** — the D bound itself: `fresh_accept_now − prev_stale_SUBMIT`, recorded at a fresh-epoch
  accept, where `prev_stale_SUBMIT` is the submit timestamp of the most recent poll that came back with **no** new
  epoch. Reported only when a prior empty poll witnessed "nothing there" (else the command is interval-censored and
  skipped — honest censoring).
- **`command_pull_submit_to_accept`** — command-body-fetch DMA cost after discovery.

**Two load-bearing method rules** (each was violated in a first cut and caught by adversarial review — see below):

1. **The submit timestamp is stamped BEFORE `doca_task_submit_ex`.** DOCA DMA is asynchronous, so the hardware can
   sample the host line before a *post*-submit clock read; anchoring on the pre-submit edge makes `submitNs` a provable
   lower bound on the sample instant, so `fresh_accept − submitNs ≥ true D` (a genuine upper bound). This rests on the
   DOCA contract that a task's DMA sampling follows its successful submission — a foundational DMA property, not
   verifiable from repo source.
2. **Per-command anchor, not the `discoveredReady` latch.** D is anchored at the per-command fresh-epoch accept
   (`ringRuntime->acceptedPublishedEpoch = line->publishedEpoch`), filtered to role 1 — NOT the one-shot
   `discoveredReady` transition, which fires ~once per arming cycle and would collapse to a single sample at `-c1`.

Storage: engine-level (`HomerDpuDmaEngine.engineSpans[role][kind]`, keyed by descriptor role × span kind), dumped per
`(role, span)` at `HomerDpuDmaDestroy`. NOT per-ring — see bug 2.

## The two bugs found (methodology lessons)

Both are "two events I assumed coincided actually don't" — the recurring hazard of instrumenting an **asynchronous,
lifecycle-managed** subsystem: the probe is a participant, subject to the same timing and teardown rules as the code
it measures.

1. **Async-DMA timestamp (would UNDER-report the bound).** The first cut stamped `submitNs` *after*
   `doca_task_submit_ex`. Because the DMA can sample the host line before that clock read, a host publish in the gap
   made `fresh_accept − submitNs` *smaller* than the true D — a censoring "upper bound" that was not one. Caught by
   codex adversarial review; fixed by stamping before the submit. **A diagnostic that lies is worse than none;** had we
   measured with this bug, the D bounds would have been confidently too small.
2. **Reclaim-before-dump (empty dump).** The first cut stored the aggregates on per-session `ringRuntime`. That is
   **freed at session close** — `HomerDpuDmaReclaimDetachedImports` → `HomerDpuDmaClearHostMmapImport` does
   `free(import->ringRuntime); memset(import, 0, …)` (`homer_service_dpu_dma.c:9668-9670`) — which runs BEFORE the
   service-shutdown dump. At `-c1` every session is reclaimed first, so the dump was always empty. The gate *passed*
   but produced zero rows. **Root-caused, not patched blind:** the key was distinguishing "instrument never fired"
   (would need moving the probe) from "data freed before harvest" (a small storage-lifetime fix). A relayed command
   *must* have been DMA-staged, and staging *is* the instrumented path, so the hooks had fired — confirming the
   storage-lifetime reading. Fixed by moving aggregates to the engine (calloc'd, freed only after the dump).

## Results (validated `-c1`, node A / farnet0 DPU, 40 role-1 commands)

Gate PASS (`spawn_pairs=1`, 5/5 tx, 0 failed, sane `abalance`); clean `-B` rebuild proven diagnostic-string-free
(safety rule 6). Node A `[dpu-span]` role=1 (`FRONTEND_CONTROL_SLOT` command ring):

| span | count | min | avg | max |
|---|---|---|---|---|
| **`censoring_bound_D`** (UPPER BOUND on D) | 40 | 14.8µs | **38.4µs** | 174.9µs |
| `grouped_submit_interval` (poll cadence) | 2080 | 9.1µs | 44.6µs | 22.2ms (idle outlier) |
| `grouped_submit_to_accept` (discovery DMA read) | 40 | 2.6µs | 10.6µs | 25.6µs |
| `command_pull_submit_to_accept` (body-fetch DMA) | 40 | 9.4µs | 17.5µs | 62.1µs |

(Role=7, the DPU→host payload/credit ring, also recorded: ~45µs cadence, ~38µs censoring bound over 6 samples — the
result-return leg has a similar discovery cost.)

**Reading:**

- **D is cadence-limited at tens of µs.** Bound ~38µs avg, cadence ~44µs, D ≈ cadence/2 — the DPU notices a
  freshly-published command within roughly one poll. **2080 polls served 40 commands (~52 polls/command)**, so the ring
  is swept far faster than commands arrive: D is bounded by the poll interval, not by scan starvation. The measurement
  landing at ~cadence/2 is *positive* evidence the instrument is faithful, not merely that the number is small — this
  is the textbook signature of a polled (not event-driven) discovery.
- **Discovery is the largest single DPU-INGRESS component, but NOT the dominant per-command latency.** DPU-side command
  ingress ≈ D(~38µs) + discovery read(~10µs) + command pull(~17µs) ≈ **~65µs upper bound**. This is a *good* result for
  the offload: the whole point of pushing transport onto the DPU is to make it nearly free, and it is.

## Conclusion, and the bottleneck question the result raises

Discovery cadence does **not** dominate per-command latency, so the S6 latency work does not point at the poll loop.
The only lever on D here is the poll cadence (poll role-1 more often, trading DPU CPU/DMA bandwidth) — not warranted.

**Where the real per-command cost is: NOT YET MEASURED — hypotheses only.** The `-c1` full figure observed
(~9.3ms/tx) was a **debug** build (verbose logging + stats macros on), so it is not a real latency and not a valid
target. Ranked hypotheses for the real (stripped) bottleneck, none measured:

1. **WAL fsync on COMMIT** — the classic pgbench TPC-B `-c1` bottleneck (each transaction commits; no group-commit
   amortization at `-c1`; synchronous fsync on farnet1's PostgreSQL).
2. **Serialized cross-node round trips** — at `-c1` nothing pipelines; each command waits for the full
   host→A→B→backend→A→host path, and the result leg repeats discovery + relay.
3. **Backend SQL execution** on node B (small per statement × 5). The backend is spawned **once per session** (single
   `dpu_spawn`), so spawn is amortized, not per-command.

**To turn hypothesis into measurement** (cheap now that the span harness exists): (a) a stripped `-c1` baseline for a
real per-tx number; (b) `synchronous_commit=off` / `fsync=off` A/B to isolate commit cost; (c) extend the same span
technique to the peer-transport **command-post → result-receive** span on node A to isolate the cross-node round trip.

## Reproduce

Build node A's DPU service with the macro (the diagnostic build is sticky — safety rule 6 requires a forced clean
rebuild + diagnostic-absence proof afterward):

```
make service-bin COPT="-DHOMER_DPU_DISCOVERY_SPANS=1"    # node A only; node B normal (macro off, protocol-neutral)
# run the standard -c1 --homer-dpu-command gate, stop node A's DPU CLEANLY (a kill -9 skips the shutdown dump)
# grep the node-A DPU log for '[dpu-span]'; read the role=1 rows
make -B service-bin                                       # force clean rebuild; prove 'dpu-span'/'HOMER_DPU_DISCOVERY_SPANS' absent
```

Dump line format: `[dpu-span] role=<N> span=<name> count=.. min_ns=.. avg_ns=.. max_ns=.. hist=<24 pow2-ns buckets>`.
Not all four rows are guaranteed: zero-count spans are omitted, `grouped_submit_interval` needs ≥2 submits, and
`censoring_bound_D` needs a prior empty poll before a fresh accept.

## Cross-links

- Plan/design: [`s6_native_homer_dpu_and_latency_plan.md`](./s6_native_homer_dpu_and_latency_plan.md) — §2 (Track B
  design), §2.5.1–2.5.3 (grounding + the two bug corrections), §2.6 (this result + the skip-echo-marker decision).
- The busy-poll discovery model this measures: `homer_service_dpu_dma.c:204-208` ("an event system layered on a
  per-ring poll has moved the poll, not removed it") and the grouped-control read / command-pull paths.
- Measurement provenance rules: `../../../operations/farnet_diagnostics_and_baselines.md` (diagnostic builds, historical
  measurements) and `farnet_operator_runbook.md` (the gate procedure).
