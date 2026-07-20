# Mixed-workload liveness failure — the 6-slot collector quota starves late collectors

<!-- kb-summary: Diagnosis of the mixed pgbench+basebackup hang: fixed-order collector admission starves late DPU collectors when two workload kinds coexist. -->

**Status: ROOT-CAUSED AND MEASURED (2026-07-19). The mechanism is confirmed; the starved collector is
`DPU_STAGED_COMMAND_DISPATCH`, NOT `DPU_STAGED_COMMAND_EXECUTE` as first predicted. Fix shape is a live design
decision — see "The fix space" at the end. No scheduler change has been made.**
**Code baseline:** citus `31f0e958f` (post-S6, post-S7 Stage 4).
**Line numbers are `src/backend/distributed/utils/homer/tuple_sink_service_process.c` unless stated.**

> ## ⛔ THE MIXED WORKLOAD DOES NOT WORK. Two runs, both FAIL, and the pool is EXONERATED.
>
> The [MIXED workload](../../../../../AGENTS.md) (gate pgbench CONCURRENT with four-role basebackup) has
> **never passed**. It was made policy on 2026-07-19 and failed on its first two attempts. **The byte-ring pool
> — the resource this workload was introduced to stress — is not the cause.**

---

## What was observed

| run | shape | result |
|---|---|---|
| `candidate-20260719-190447` | mixed `-c4 -t2000` | **FAIL** — gate 0/4000, basebackup stalled 458 s |
| `candidate-20260719-193409` | mixed `-c1 -t200` | **FAIL** — gate 0/200, **identical signature**, 457 s |
| (same run, attempt 1) | accidentally **sequential** | ✅ **PASS** both halves — gate 200/200, basebackup 23.26 GB |

Both failures carry the same client-side signature: every client times out on
`sql_execute kind=6 sequence=5`.

### What this ELIMINATES

- **Client concurrency / load / CPU** — `-c1` is one client on a 96-core box, and it fails identically.
- **Byte-ring pool exhaustion** — `-c1` mixed needs `2*1 + 1 = 3` of **16** slots. No `pool exhausted`, no
  `engine->fatalError`, no `ALARM` in either run. See the budget derivation in [`CONTRACTS.md`](./CONTRACTS.md).
- **A stale or mismatched binary** — attempt 1 of the very same run passed both halves sequentially on the
  **same** binaries.

**⇒ The single remaining variable is that the two workload kinds are LIVE AT THE SAME TIME.**

### The asymmetry, and why it matters

In the `-c1` run the stall is not symmetric, and the split lands exactly on the topology:

| DPU | role | basebackup byte-ring binds |
|---|---|---|
| **farnet0** | receiver; gate **client** side | ✅ LANDING bound, immediately after `basebackup receive-open sessionUID=8045551091818816 tag=167728315` |
| **farnet1** | sender; hosts the gate's **DPU-spawned backend** | ❌ **zero** basebackup binds, zero basebackup-tagged lines |

farnet1's walsender sat at `state=R, wchan=0` — ~100 % CPU, busy-spinning in userspace, waiting for a stream
open that never executed. The farnet0 consumer was idle in `hrtimer_nanosleep`; the sender client idle in
`do_poll`. **The side that also carries the gate's backend is the side that starved.**

---

## Mechanism (VERIFIED where labelled; one INFERRED step is what the run below tests)

**VERIFIED — the collector phase is saturated and shedding, continuously, on both DPUs in both runs.**
`HomerServiceReportPolicyAdmissionDrops` (`:10664`, plain `fprintf`, **not** macro-gated, so present in
performance builds) printed on both nodes out to event 16,777,216:

```
PROGRESS POLICY-ADMISSION DROPS event=… phase=2 | granted actions=6 collectors=6 machines=0 payload=0
                                     | DROPPED total=0 collectors=1..3 | caps plan=16 collectors=6 …
```

**VERIFIED — admission is a fixed-order list with no rotation.** The plan body
(`HomerMachineBaselineCompileExecutionPlan`, collector appends at `:13060`–`:13240`) offers collectors in a
hard-coded sequence. Reserved lifecycle admission
(`HomerServiceProgressCollectorReservedLifecycleAdmission`) names **only** `DPU_SETUP_LISTENER` and
`DPU_DOORBELL`. The body has **no `break`/`continue`/early return**: every collector is considered on every
pass, so a shed collector *does* record a `budget` drop rather than silently never being offered.

**VERIFIED — the ordinal positions of the two collectors the failure implicates:**

| ordinal | collector | fits in `maxCollectorGrants = 6`? |
|---|---|---|
| 1–2 | `DPU_SETUP_LISTENER`, `DPU_DOORBELL` | **reserved** — always |
| 3–6 | `COMMAND_SEND_CQ`, `DPU_GROUPED_CONTROL_READ`, `DPU_PE_DRAIN`, `DPU_COMMAND_PULL` | consumes the quota |
| **13** | **`DPU_STAGED_COMMAND_EXECUTE`** | ✗ |
| **14** | **`DPU_BACKEND_COMPLETION_PULL`** | ✗ |
| 18–19 | `DPU_SPAWN`, `PEER_CM_SETUP` | ✗ |

Each append site is guarded by an "is there work?" predicate. **That is why either workload passes ALONE:**
with one workload live, fewer early collectors are armed and the late ones still fit. Add a second workload
and >6 are armed continuously, so positions 13/14 are shed on every pass, forever.

**INFERRED (the gap this doc's run closes) — that the shed collectors are those two.** The drop line reports a
*count*, not an identity. The two-symptom fit is strong but circumstantial:

- `DPU_STAGED_COMMAND_EXECUTE` starved ⇒ basebackup's send-open never executes ⇒ no `STREAM_REGISTER_MIRROR`
  ⇒ no MIRROR bind on farnet1 ⇒ walsender spins forever. **Matches the observed zero binds.**
- `DPU_BACKEND_COMPLETION_PULL` starved ⇒ the SQL result stream never finalizes ⇒ `kind=6 sequence=5` timeout.
  **Matches the observed client signature.**

One mechanism, both symptoms, and silent: no alarm, no exhaustion, no fatal — the service looks *idle*.

### ⚠ This was PREDICTED, and the prediction is already in this KB

[`dpu_collector_feedback_aliasing_defect_b.md`](./dpu_collector_feedback_aliasing_defect_b.md) (§"Two
corrections FB-1 must absorb", item 2) states it outright:

> *"**FB-1 IS NOT A TWELVE-COLLECTOR LIVENESS FIX.** `MAX_COLLECTOR_GRANTS = 6` remains. Grouped-control is
> budget-safe only by **fixed append order**, not by reservation … Every newly-admitted grouped grant consumes
> one of six collector slots, so FB-1 can **RELOCATE** the loss to later DPU collectors."*

This failure is that relocation, observed. What the prediction did not anticipate is the *trigger*: it takes a
**second concurrent workload kind** to arm enough early collectors to push positions 13/14 out of the quota.

---

## The pre-registered A/B run (predictions recorded BEFORE the run)

**Instrument:** `HOMER_COLLECTOR_STARVE_DIAG` — a checked-in diagnostic that names **(collector kind, drop
reason)** per drop (`HomerStarveDiagRecordDrop`, `:11906`), rate-limited per `(kind, reason)` pair, plus a
service-exit census (`HomerStarveDiagReportSummary`, `:12000`). The census prints `grants` beside
`max_backoff_since_grant` **specifically so `max=0, grants=0` reads as "this collector NEVER RAN"** (`:12022`)
— the exact state under test. Four reasons are distinguished: `already-planned` (benign), `feedback-backoff`,
`no-source` (stale scaffolding), **`budget`** (the quota).

**Build:** both DPUs, `--define=HOMER_COLLECTOR_STARVE_DIAG=1` via
[`dpu_build.sh`](../../../../../tools/farnet_validation/remote/dpu_build.sh). Diagnostic-only; no performance
number may be quoted from it.

⚠ The flag is a repeatable, **whitespace-free** `--define=` argument on purpose. A single
space-separated `--cppflags=` string does not survive the ssh hop (ssh forwards a command *string*, not an
argument vector) and needs an extra quoting layer for farnet0's DPU than for farnet1's — hazards §6.3b.

**Design — same binary, two arms, ONE variable (is a basebackup live?):**

| arm | workload | why |
|---|---|---|
| **A (control)** | gate alone, `-c1`, same `-t` | Establishes whether saturation alone is normal |
| **B (test)** | mixed `-c1` (gate + basebackup) | The failing shape |

Restart both DPU services between arms — the exit census is per service lifetime, and safety rule 12 requires a
restart after a failed gate anyway.

⚠ **Why arm A is run fresh rather than reusing FB-0's July-14 census:** that census was taken at
`a551bcf6e`, before S6 and S7. Both collectors *did* exist at that SHA (verified), and it recorded **zero**
budget drops for both while `PEER_SEND_CQ` recorded 22,985 / 39,183 — but the arming set has changed since, so
using it as the control would confound the result with SHA drift. It is corroboration, not the control.

### Predictions — this run can REFUTE the diagnosis

1. **Arm B, farnet1:** `DPU_STAGED_COMMAND_EXECUTE` shows a large nonzero **`budget`** drop count, and
   `grants = 0` (or near-zero) in the backoff table.
2. **Arm B:** `DPU_BACKEND_COMPLETION_PULL` likewise shows nonzero `budget` drops.
3. **Arm A:** both collectors show **zero or negligible** `budget` drops, and healthy nonzero `grants`.

**What would REFUTE this diagnosis:**

- Arm B shows the dominant reason for those collectors is **`feedback-backoff`** or **`no-source`**, not
  `budget` ⇒ the mechanism is a different gate and the quota story is wrong.
- Arm A shows the *same* budget starvation as arm B ⇒ saturation is normal and something else distinguishes
  the failing run.
- Neither collector appears in arm B's census at all, with nonzero `grants` ⇒ they ran fine and the stall is
  downstream of admission entirely.

⚠ **A negative result is only meaningful if the probe was actually compiled in.** `dpu_build.sh` now asserts
`starve-diag` strings are present whenever the macro is requested, because a misspelled macro yields a
**silent binary whose silence is indistinguishable from "the condition never occurred"** — which would be read
as evidence against the hypothesis when it is really a missing probe.

---

---

## ⚡ MEASURED RESULT (2026-07-19, `candidate-20260719-203050`, citus `31f0e958f`)

Instrumented A/B, same binary, one variable. **Arm A (gate alone) PASSED 200/200. Arm B (mixed) FAILED 0/200**
with the `sql_execute kind=6 sequence=5` signature. A third control appeared by accident: Arm B's first attempt
ran **non-overlapping** and **passed both halves** (200/200 + 23.26 GB) in the same session on the same
binaries. Overlap alone flips the outcome.

### Total collector `budget` drops, farnet1 DPU

| | Arm A (gate alone) | Arm B (mixed) |
|---|---|---|
| total | **~870** | **~8,600,000** |

### The starved collectors (farnet1, Arm B)

| collector | ordinal | grants | `budget` drops | Arm A drops |
|---|---|---|---|---|
| **`DPU_STAGED_COMMAND_DISPATCH`** | **11** | **3** | **1,514,591** | absent |
| `DPU_BACKEND_COMPLETION_PULL` | 14 | 7,874 | 1,514,596 | 0 |
| `DPU_PAYLOAD_PULL` | 15 | 24,247,736 | 2,015,105 | 141 |
| `DPU_BACKEND_COMMAND_STAGE` | 10 | 660,180 | 854,414 | absent |
| `PEER_CM_SETUP` | 19 | 18,529,006 | 2,740,606 | 729 |

### ❌ REFUTED — prediction 1: `DPU_STAGED_COMMAND_EXECUTE` is starved

It is not. Arm B: **grants=3, ZERO drops.** The prediction named the wrong link in the chain.

> ### ⚠⚠ WHY IT LOOKED INNOCENT — GENERALIZE THIS, IT WILL RECUR
>
> `STAGED_COMMAND_EXECUTE` (13) is armed **only when `stagedCommandDispatchCount > 0`** (`:46829`) — i.e. only
> when DISPATCH (11) produced something. Dispatch ran 3 times, so execute was armed 3 times, granted 3 times,
> and dropped never. **A perfect scorecard produced entirely by upstream starvation.**
>
> The census doc's rule is "`max=0 grants=0` means the collector NEVER RAN". The subtler form, and the one that
> cost this investigation a wrong prediction, is: **LOW GRANTS + ZERO DROPS = STARVED UPSTREAM. Zero drops is
> not evidence of health.** Always read a collector's grant count against its upstream's.

### ✅ CONFIRMED — the mechanism, localized to `:11560`

**The rejecting line is `maxCollectorGrants` (6) at `:11560`** — VERIFIED, not inferred:

- All four collectors pass `knownExpectedWork = true` at their append sites (`:46826` dispatch, `:46726`
  payload-pull, `:46838` execute), so `blindCollectorGrant` is **false** and the 2-slot blind line at `:11576`
  is **unreachable** for them. (`DPU_GROUPED_CONTROL_READ` *can* be blind — its flag is
  `facts.groupedControlRingCount > 0`, `:46676` — but that is a different collector.)
- `maxPlanGrants` (16) is **not** binding: every `PROGRESS POLICY-ADMISSION DROPS` line in both arms reports
  `DROPPED total=0`.

> ### 🔑 THE AMPLIFIER — THE EFFECTIVE QUOTA IS **4**, NOT 6
>
> A reserved lifecycle grant is admitted by the bypass at `:11544` **and still increments `collectorGrants`**
> (`:11547`, `:11551`). So `DPU_SETUP_LISTENER` and `DPU_DOORBELL` are guaranteed admission *and* permanently
> consume 2 of the 6 collector slots. **Seventeen collectors compete for FOUR.**
>
> This is why a bulk workload is enough to break it: a live basebackup arms ordinals 3, 4, 5, 6, 9 and 10
> (`COMMAND_SEND_CQ`, `GROUPED_CONTROL_READ`, `PE_DRAIN`, `COMMAND_PULL`, `PEER_COMMAND_LANDING`,
> `BACKEND_COMMAND_STAGE`) — **six candidates for four slots.** Ordinal 11 is never reached.

### The apparent ordinal inversion, resolved

`DPU_PAYLOAD_PULL` (15) took 24M grants while `DISPATCH` (11) took 3 — impossible under a naive
"earlier ordinal always wins" model, and it is what forced the model to be corrected. The resolution is that
the two are armed in largely **disjoint sets of passes**, and the drop counts prove it: payload-pull's
**2,015,105** drops are the same order as dispatch's **1,514,591** — those are the *busy* passes, where
everything from ordinal 11 on is shed. In the other ~24M passes fewer early collectors are armed and
payload-pull is admitted. Dispatch is armed only when a staged SQL request coexists with free dispatch and
publish slots (`:46816`) — which, during a mixed run, is exactly when the pass is busy.

**⇒ Dispatch starved ⇒ the SQL command is never dispatched ⇒ `kind=6 sequence=5` timeout. The basebackup's own
send-open rides the same command plane and starves identically. One mechanism, both symptoms.**

### Still NOT established

- **That relieving the quota fixes it.** The causal chain is verified by construction and by the A/B, but the
  decisive test is cheap and has not been run: every cap is env-tunable (`:13858`-`:13877`), so raising
  `HOMER_MACHINE_BASELINE_MAX_COLLECTOR_GRANTS` and re-running the mixed workload tests the whole chain with
  **zero code change**. Do this before implementing anything.
- **Why the basebackup byte-ring binds appeared on farnet1 this run and farnet0 in the previous one.** The
  earlier receiver/sender topology argument in this doc is therefore SUSPECT and must not be relied on.
- **A teardown defect, separate from this one:** the Arm B consumer survived sender exit *and* both DPU
  SIGTERMs, requiring an identity-verified reap.

---

## The fix space (nothing implemented; the numbers now constrain it)

| option | effect | problem |
|---|---|---|
| **Raise `maxCollectorGrants` only** | more slots | moves the cliff; fixed order still means whatever does not fit starves **permanently**. A third workload re-breaks it. |
| **Extend reserved lifecycle admission** to the command-plane collectors | guarantees them *by construction* | reserved grants **consume the shared quota** (`:11547`), so adding members without resizing takes the effective quota 4 → 2 and starves the throughput collectors instead |
| **Stop counting reserved grants against `collectorGrants`** | decouples the two lines | contradicts the deliberate "it really did spend a grant" comment at `:11528`; needs its own justification |
| **Rotation / round-robin** | bounded delay instead of permanent starvation | would demote `DPU_GROUPED_CONTROL_READ`, which is granted ~46M times and needs to run nearly every pass |
| **Unified grant scheduler** | real admission with priorities | large redesign — the long-term answer, not this fix |

The reserved-set docstring's own criterion (`:11457`) — *"Only listeners whose starvation is a SILENT hang
qualify"* — is satisfied by `DPU_STAGED_COMMAND_DISPATCH` on this evidence. But the sizing is a genuine design
decision with hot-path cost, and S6's latency number is still outstanding and would be perturbed by it.

## Consequences beyond this workload

This is **not** a mixed-workload quirk. Any two concurrent workload kinds that together arm more than six
collectors will shed the same late entries. The gate and basebackup happen to be the pair we run.

It is also direct empirical support for
[`homer_unified_aggregate_action_scheduler_plan.md`](../../../future-directions/citus/transport/homer_unified_aggregate_action_scheduler_plan.md),
whose premise is replacing this machine/collector + egress split with a unified grant scheduler with real
admission. **This failure is the concrete case that plan was written for.**

## Related

- [`dpu_collector_feedback_aliasing_defect_b.md`](./dpu_collector_feedback_aliasing_defect_b.md) — FB-0/FB-1;
  predicted this relocation, and is the origin of the starve-diag instrument.
- [`dpu_byte_ring_pool_per_session_plan.md`](./dpu_byte_ring_pool_per_session_plan.md) — the pool work whose
  "functionally complete" claim these runs retracted; the pool itself is exonerated here.
- [`dpu_gate_concurrency_limits.md`](./dpu_gate_concurrency_limits.md) — the engine-fatal blast radius; a
  *different* failure mode, gated behind exhaustion, and NOT what happened here.
- [`../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
  — the arm/execute survey; §0 is the earlier silent-wedge incident of the same family.
