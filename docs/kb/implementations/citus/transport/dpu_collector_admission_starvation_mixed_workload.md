# The mixed-workload hang — investigation record and current frontier

<!-- kb-summary: The mixed pgbench+basebackup hang is localized to a budget-starved foreground recv-CQ OPEN response; Option B exact recv demand is implemented and syntax-verified, with non-pre-armed SCALE=150 hardware validation still pending. -->

**Status (2026-07-20, LATE): REPRODUCED + LOCALIZED to F1→F2. The response is POSTED but farnet1 NEVER
RECV-PROCESSES it on the outgoing FOREGROUND result connections.**
- **IMPLEMENTATION STATUS (2026-07-20): Option B is implemented and statically verified; hardware validation is
  NOT YET RUN.** `payloadOpensAwaitingResponse` now arms the shared generation-bearing exact-demand bitmap from
  service-result OPEN publish through every WAIT exit/reset (`remote_execution_peer_transport_rdma.c:1259`,
  `:3200`, `:3252`; `tuple_sink_service_process.c:40309`, `:41546-41594`). The exact action is enumerated from
  the shared aggregate count and now actually bypasses the six-collector budget
  (`tuple_sink_service_process.c:13005`, `:48130`). Both service translation units pass the compile-database
  `gcc -fsyntax-only` flags with the diagnostic OFF and with `HOMER_RECV_LIVENESS_DROP_DIAG=1`.
- **REPRODUCTION RECIPE FOUND:** non-pre-armed launch order **+ `SCALE=150`** (a DB big enough that the basebackup
  sustains a real transfer window). `SCALE=1` is why the prior three runs passed — the basebackup finished before
  any overlap. Candidate `candidate-20260720-190749`, citus `99aa04a42`, gate `-c4`: all 4 clients time out on
  `sql_execute kind=6 seq=5`, `0/8000`, basebackup also stalls. The historical hang, reproduced on demand.
- **LOCALIZED (VERIFIED from the `[resp-deliv]` probe + the raw candidate interval):** each of the 4 gate result
  streams starts its peer-open, allocates a NEW outgoing FOREGROUND (`class=2`) payload connection, establishes
  the RDMA transport, binds its byte-ring — then `BOUND BUT NEVER ARMED`. farnet0 (responder) **F1-posted all 4
  responses** (its conn_gen=3–6). farnet1 (requester) **F2 recv-arms ONLY on conn_gen=1 and conn_gen=2** (the
  latter is the CRITICAL command connection — its F2 lines interleave with `landed peer command sequence=1..5`).
  farnet1 **NEVER F2 recv-arms on the 4 FOREGROUND result connections.** No `did not match an outstanding op`,
  no `latched pending synchronous`, no `drain failed`. ⇒ **The stall is F1→F2: the OPEN responses are posted by
  farnet0 but never enter farnet1's recv path on the outgoing foreground result connections**, while the CRITICAL
  command connection is recv-polled fine.
- **ROOT CAUSE — FOUND AND VERIFIED IN CODE (2026-07-20):** the FOREGROUND recv-CQ **liveness action** is the
  ONLY path that discovers foreground payload WIMMs (`HomerServiceMachineBaselineAppendRecvCqLivenessAction`
  comment, `tuple_sink_service_process.c:13043-13047`). It competes for the shared **6-collector budget**, and
  when that budget is full it is **silently dropped** — `HomerMachineBaselineBudgetTryReserve(...)` fails →
  `return true` with **no action appended and NO diagnostic** (`:13049-13052`). A pending `STREAM_WAIT_PEER_OPEN`
  op does NOT create a demand-driven recv poll: the machine-baseline peer collector bundle is only
  `PEER_CM_SETUP` + `PEER_CLOSE_LIFETIME`, NOT `PEER_RECV_CQ` (`:12454-12457`). By contrast CRITICAL command
  traffic HAS a separate **exact-demand** recv path (`remote_execution_peer_transport_rdma.c:2097,:2419,:2981`)
  that FOREGROUND lacked entirely — **that missing path is the real asymmetry.** ⚠ CORRECTION (found during the
  fix): the exact-demand *append* was NOT budget-free as an earlier draft claimed — it too called
  `BudgetTryReserve` and could be dropped. Critical simply didn't hang in the run (its demand armed early /
  enumerated before the burst of foreground opens saturated the budget), so its non-hang was timing/order, not
  structural immunity. The fix therefore makes the exact-demand append truly budget-free for BOTH classes AND
  adds the foreground exact demand foreground never had. ⇒ Under MIXED load the 6-collector budget saturates (the
  reproduced run's admission line
  confirms: `granted collectors=6 | DROPPED collectors=1-2`), the foreground liveness action is dropped every
  pass, the OPEN-response CQE is never polled, and the result stream is BOUND BUT NEVER ARMED. Teardown drops the
  load → budget frees → the poll runs (in run 6, the open completed exactly then).
- **This single mechanism explains everything:** the `6392a1853` class-admission fix didn't help (WRONG action —
  control-mailbox, not recv-CQ liveness); the `[starve-diag]` census read 0 (this drop emits NO diagnostic);
  pre-arming masks it (budget not saturated during the open window); `SCALE=150` is required (sustained load
  keeps the budget saturated); "completes at teardown" (load drops).
- **Residual (small):** F2 fires only on a successful CQ poll, so it cannot formally separate "never polled" from
  "polled-but-empty." But the mechanism is fully verified in code, the admission line confirms saturation, and RC
  delivery is reliable — "never polled" is overwhelmingly supported. The implemented fix plus a fresh recipe
  reproduction must still confirm it.
- **⚠ CORRECTION to the causation-run conclusion:** that run "refuted" the class-admission fix, but it was
  PRE-ARMED and **never reproduced the hang**, so it tested nothing about the fix under the failure. The fix's
  relevance to the hang was UNDETERMINED, not refuted. (It is still very likely not the hang fix — but because
  this frontier is the recv-CQ poll, not the mailbox action — not because a pre-armed pass showed anything.)
- **NEXT — HARDWARE CONFIRMATION:** give the foreground recv-CQ discovery a path that is NOT
  silently droppable by the 6-collector budget when a peer-open is pending on that connection.
  **CHOSEN: Option B — exact recv demand for foreground (user decision 2026-07-20).** Keep the earlier
  reserved-lifecycle fix as-is (right tool for a FIXED collector set); use exact-demand here (right tool for a
  DYNAMIC per-connection set); both converge long-term on the unified scheduler
  ([`homer_unified_aggregate_action_scheduler_plan.md`](../../../future-directions/citus/transport/homer_unified_aggregate_action_scheduler_plan.md)).

  **IMPLEMENTATION PLAN (Option B), all `remote_execution_peer_transport_rdma.{c,h}` unless noted:**
  The exact-demand bitmap `HomerCriticalRecvDemandSet` (`:851`) is structurally general, but BOTH the arm
  (`SetCriticalRecvDemand` `:2419`, gated CRITICAL) and the consume-revalidation
  (`HasCriticalClientCompletionDemand` `:2097` = `CRITICAL && clientCommandsAwaitingTerminal>0`) are
  command-specific. **Implementation correction:** the plan assumed the append path was already budget-free,
  but current code still called `HomerMachineBaselineBudgetTryReserve(... collectorGrant=true ...)`; leaving it
  unchanged would silently drop the new exact action under the same saturated six-collector budget. The landed
  implementation removes that gate and also enumerates from the shared `demandedConnectionCount` rather than the
  critical-only summary. Then add a PARALLEL foreground demand signal OR'd into the shared machinery:
  1. **New field** `uint32_t payloadOpensAwaitingResponse` on the connection (beside `clientCommandsAwaitingTerminal`
     `:1254`).
  2. **New checks:** `HasPayloadOpenRecvDemand(c)` = `FOREGROUND_PAYLOAD && payloadOpensAwaitingResponse>0`;
     `HasExactRecvDemand(c)` = critical OR payload. Replace `HasCriticalClientCompletionDemand` at the CONSUME
     sites (`:2569`, `:9660`) with `HasExactRecvDemand`. (Leave the arm-site gate as-is; add a parallel arm.)
  3. **New Set/Clear** `Set/ClearPayloadOpenRecvDemand` mirroring the critical pair but gated FOREGROUND, touching
     the SAME bitmap + `demandedConnectionCount` (NOT the critical-specific
     `criticalClientCompletionDemandConnectionCount`).
  4. **New public arm/clear** `Set/ClearPeerConnectionPayloadOpenAwaitingResponseRdma(handle, generation, …)`
     mirroring the `ClientCommandAwaitingTerminal` pair (`:2995`/`:3026`): on 0→1 set demand, on 1→0 clear;
     revalidate generation.
  5. **Arm/clear lifecycle** (the risk — must be leak-free):
     - ARM in the async op just after it publishes and enters `STREAM_WAIT_PEER_OPEN`
       (`tuple_sink_service_process.c:41443-41444`), on `asyncOp->peerConnectionHandle`, ONLY for the service-
       result foreground open (`asyncOp->clientSqlResultOpen`). Stamp `asyncOp->payloadRecvDemandArmed=true` +
       the armed connection handle+generation so clear targets the exact incarnation.
     - CLEAR whenever the op leaves WAIT: on COMPLETED, on every FAILED path, and in
       `TupleSinkServiceResetLocalControlAsyncOp` — guarded by `payloadRecvDemandArmed` so it fires exactly once.
     - RESET: extend the connection-reset clear (`:2967`) to clear payload demand too (today it only clears when
       `clientCommandsAwaitingTerminal>0`), so a reset under an armed foreground open cannot leak a bit.
  6. **The diagnostic** (independent, do regardless): at the silent drop (`tuple_sink:13051`) emit a bounded
     `[recv-liveness-drop]` line (conn idx/gen/class) so a budget refusal on the sole foreground-WIMM discovery
     path is never again invisible. Cross-cutting rule recorded: no budget-refusal `return true` without a drop
     signal.
  Validate by reproduction (non-pre-armed + `SCALE=150`) — a pass under that recipe is a genuine confirmation
  (unlike the pre-armed causation run). Implementation is complete and awaits adversarial diff review before
  hardware validation. The mixed workload passes under the current PRE-ARMED procedure — and so does the
control-mailbox fix REVERTED (causation-run section), so **class-admission starvation was NOT the mechanism** and
"the fix made it pass" does not hold (hardening only). Two static traces then refuted every remaining
code-reachable hypothesis:
- **Connection-reuse REFUTED:** the `generation 2→3` was TWO DIFFERENT connections (`connection_generation=2` =
  incoming COMMAND connection `dpuResultOriginConnectionGeneration` `:43142`; `peer_generation=3` = outgoing
  PAYLOAD connection `:27959`), consecutive only by the global counter. B's `STREAM_WAIT_PEER_OPEN` completion
  reads only B's own op state; it does NOT depend on A's session/close/connection.
- **"Completes at teardown" was IMPRECISE — the correction matters:** the `ready` line is emitted ONLY by the
  LIVE progress pump (`:41918`, `:49875`); shutdown has no pump and reports waiting ops as LOST, not completed
  (`remote_execution_peer_transport_rdma.c:7773`, `:6388`). So the op completed while the service was **still
  live**, ~1M passes late — NOT during teardown. The "open completes when A's connection is freed" correlation
  is a **coincidence of timing**, on DIFFERENT nodes (A's release is on farnet1; if the delay were a
  responder-side latch it would be on farnet0). Not a causal edge.
- **Suspect A (`pendingSyncResponse` head-of-line latch) REFUTED from retained logs:** its unconditional probe
  `latched pending synchronous control response` (`remote_execution_peer_transport_rdma.c:10793`) fired **ZERO**
  times on BOTH DPUs; `exact control mailbox drain failed` also zero. farnet0's response for B never latched — it
  was posted normally.
- **Suspect B (`exactConnectionGeneration` ABA) REFUTED as the cause:** the op tuple
  (opIndex/opGeneration/messageSequence/requestKind, `:10490`,`:14714`) prevents aliasing, and after a reset the
  machine-baseline re-evaluates the op and FAILS it promptly (`:50498`,`:47242`) — it cannot produce a 1M-pass
  wait. It IS a real latent defensive-check omission (capture-but-never-compare) worth closing, but not this
  bug.

⇒ **The response was POSTED by farnet0. The delay is on FARNET1's DELIVERY CHAIN** — one of four frontiers with
NO existing log marker: (2) recv-CQ WIMM arrival arms the mailbox-ready bit (`:1742`), (3) mailbox matching moves
the op to COMPLETED (`:10506`), (4) local owner polling consumes it and prints `ready` (`:41918`). **Only a
reproduction that TIMESTAMPS all four frontiers with connection index/generation + op index/generation/sequence
can localize it.** ⇒ **PROBE LANDED: `HOMER_PEER_RESPONSE_DELIVERY_DIAG` (citus `99aa04a42`)** stamps all four
frontiers with `[resp-deliv] F<n>` lines; operator detail in
[`farnet_diagnostics_and_baselines.md` §1.2d](../../../operations/farnet_diagnostics_and_baselines.md).
⚠ **REPRODUCTION IS UNCERTAIN:** the hang has NOT reproduced on the last three runs (two pre-armed, one
sequential-in-disguise); the six original hangs predate the pre-arming workaround. A reproduction run must use
the ORIGINAL non-pre-armed order and MAY simply pass — which is itself a finding (⇒ accept pre-arming as
supported). The probe is useless without a live hang to observe. Three real defects were still found and fixed along the way (collector-admission starvation; a
watchdog blind to the state it existed to catch; control-mailbox class starvation), and **eleven** candidate
causes/suspects were refuted.
**Code baseline:** citus `8f4d20e60` (fix `6392a1853` + the revert scaffold).
**Line numbers are `src/backend/distributed/utils/homer/tuple_sink_service_process.c` unless stated.**

> ## ⛔ THE MIXED WORKLOAD HAS NEVER PASSED. Six runs. The byte-ring pool it was written to stress is EXONERATED.

> ## ⛔ "NEVER ESTABLISHES" WAS WRONG — IT IS A DELAY, NOT A FAILURE.
> An earlier version of this file said the peer binding *never activates*. Run 6 line 214 disproves that:
> `service-owned P3 result peer-open ready ... detail=peer stream bound`. It arrives at teardown. **A blocked
> resource and a failed one demand completely different fixes, and this file sent the investigation at the
> wrong one for a full run.**

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
- ~~Why the basebackup byte-ring binds appeared on farnet1 this run and farnet0 in the previous one.~~
  **RESOLVED — there was no inversion.** The validator's summary mis-attributed the lines; the raw logs put both
  `basebackup receive-open` lines in **farnet0's** DPU log, which is where the runbook topology requires them
  (`basebackup_consumer.sh` hardcodes `HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200`, farnet0's own DPU, and the
  line is emitted receiver-side — `tuple_sink_service_process.c:39255`, whose own comment says "confirm the
  pairing tag reached farnet0"). **No run-shape violation and no runbook ambiguity.** The two runs agree, and
  the receiver/sender topology argument in this doc STANDS.
  ⚠ The census attribution was re-verified independently against the raw logs and IS correct: farnet1 carries
  the 1,514,591 dispatch drops. **A validator's factual summary is a claim to verify, not a fact to inherit —
  the retained raw logs are what made that checkable.**
- **A teardown defect, separate from this one:** the Arm B consumer survived sender exit *and* both DPU
  SIGTERMs, requiring an identity-verified reap.

---

> ## 🎯 ROOT CAUSE LOCALIZED 2026-07-20 (run 6, citus `434c7e9e7`). READ THIS FIRST — THIS DOC'S TITLE IS NOW TOO NARROW.
>
> **The mixed-workload hang is NOT collector starvation.** That was one real defect on the way (fixed,
> below), but the hang survives it. The chain, established over six runs:
>
> ```
> mixed workload -> the gate's result stream is the SECOND payload stream on farnet1
>   -> its peer binding NEVER ACTIVATES  (peer_binding_active=0, locally_initiated=0, generation=0)
>   -> TupleSinkServiceArmServiceResultPayloadEgress never arms the mirror egress
>   -> no result bytes egress; the EOS gate (completedHead >= eosPostedSourceTail) never releases
>   -> DPU_PEER_COMPLETION_EGRESS collapses 55,755 grants -> 5   (ZERO admission drops: ARMING, not admission)
>   -> the result never returns; pgbench times out on `sql_execute kind=6 sequence=5`
>   -> the session cannot close (outstanding_sequence=5) -> close-drain wedges
>   -> the SINGLE-CLIENT setup server blocks ALL later DPU setup
> ```
>
> **WITNESSED**, farnet1, run 6:
> ```
> ALARM service-owned P3 result stream BOUND BUT NEVER ARMED for 1000000 service passes
>   blocked_by=peer_binding_not_active peer_binding_active=0 peer_binding_locally_initiated=0
>   uses_dpu_mirror_source=1 mirror_mr=0xc7168397fae0 peer_generation=0
> ```
> The mirror source and MR are FINE. All three peer-binding fields are zero — the binding was never
> established at all, not "failed". The result peer-open **starts** (`started service-owned P3 result
> peer-open ... connection_generation=2`) and never completes back onto the stream.
>
> ### The A/B that proves it is the arming, not anything downstream
>
> | | peer-open | mirror bind | egress armed |
> |---|---|---|---|
> | gate alone (**passes**) | line 32 | line 61 | **line 63** — two lines later |
> | mixed (**hangs**) | line 110 | line 118 | **line 200** — only at teardown |
>
> ### ⛔ THREE THINGS THAT ARE **NOT** THE CAUSE — each cost a cycle; do not re-chase
>
> 1. **Collector admission starvation.** Real, measured, FIXED (below). Hang survives it.
> 2. **A torn 64-byte grouped-control snapshot.** REFUTED: the probe built to catch it fired on a
>    PASSING run (benign first-publication, `prev_epoch=0 tail=0`). The DOCA ascending-fetch-order
>    platform assumption survived.
> 3. **Peer-transport connection resets / "churn".** REFUTED: all generations are destroyed
>    TOGETHER at teardown with `reason=7`. `generation=2 posted=8 retired=1 flushed=7` is a
>    teardown artifact of an INCOMING connection, not a mid-run reset. There is no churn.
>
> Also eliminated: byte-ring pool capacity (3 of 16 slots used), machine admission (zero machine
> drops, no candidate overflow), and ring DISCOVERY (`DPU_COMMAND_PULL` 115,085 -> 17 is a
> CONSEQUENCE — the counter measures work that happened, and no transactions completed).
>
> ### ⚠⚠ THE METHODOLOGICAL LESSON, PAID FOR THREE TIMES
>
> **Three of the four dead ends were CONSEQUENCES mistaken for causes**, and each time the tell was
> identical: *a counter that measures work performed collapses when no work happens.* Discovery
> counts, dispatch grants, and close-drain state all collapsed because the result never came back —
> not because they were broken. **Before believing a collapsed counter is a cause, ask what it counts
> when the system is merely idle.**
>
> ### ⛔ FOUR MORE THINGS THAT ARE **NOT** THE CAUSE (2026-07-20, from retained run-6 logs, no new run)
>
> 4. **`PEER_CM_SETUP` starvation.** REFUTED by its own denominator: **271,707 `budget` drops against
>    12,409,722 grants** — a 2% rate. The `grants` column added to the exit census two commits earlier
>    is what caught this; the raw drop count alone looked damning.
> 5. **`PEER_RECV_CQ` never granted.** It has **zero** grants — in *every* run, **including the passing
>    ones** (`stage1`, `stage2`, `control_*`, `arm_a`, `arm_b`). Normal, not a discriminator.
> 6. **Malformed OPEN geometry.** farnet0 provisioned the receive sink with
>    `payload_ring_slot_count=0 payload_ring_slot_bytes=0` — identical in every passing run.
> 7. **The farnet0 (receiver) side generally.** It received and serviced the OPEN normally: run 6 farnet0
>    line 60, `peer-provisioned receive sink ... peer_session=5927597253484435
>    peer_sink=6507436609316216724`. **The request crossed. The RESPONSE was not consumed on farnet1.**
>
> ### ⚠⚠ THE METHODOLOGICAL LESSON, PAID FOR THREE TIMES — AND ITS COROLLARY, PAID FOR TWICE MORE
>
> **Three of the seven dead ends were CONSEQUENCES mistaken for causes**, and each time the tell was
> identical: *a counter that measures work performed collapses when no work happens.* Discovery
> counts, dispatch grants, and close-drain state all collapsed because the result never came back —
> not because they were broken. **Before believing a collapsed counter is a cause, ask what it counts
> when the system is merely idle.**
>
> **COROLLARY (causes 5 and 6): an anomaly is only evidence if it DISTINGUISHES the failing run from a
> passing one.** Both died to a control comparison, not to argument. Diff the census against a green
> run *before* building a theory on any number. **And never read a drop count without its denominator**
> (cause 4).
>
> ### The frontier now — a BLOCKED open, and the prime suspect
>
> The op is parked in `STREAM_WAIT_PEER_OPEN`, and both neighbours are excluded by evidence:
> `mirror_mr` non-null proves `STREAM_REGISTER_MIRROR` completed, and a publish failure in
> `STREAM_START_PEER_OPEN` would have printed a `failed` completion line. **So the request was
> published and the response was never delivered to the op.**
>
> The release is exact: the open completes the instant
> `payload connection release: session=...434 freed 1 outgoing connection(s) for reuse` appears, with
> the connection generation moving 2 → 3.
>
> **PRIME SUSPECT — control-mailbox class admission** (see the
> `TupleSinkServiceAppendReadyControlMailboxActionsRdma()` entry in [`CONTRACTS.md`](./CONTRACTS.md)):
> 8 actions are enumerated, **2** are planned, and until 2026-07-20 the enumeration walked traffic
> classes in strict priority with **no rotation across classes** — so a ready `FOREGROUND_PAYLOAD`
> mailbox (which is what carries the P3 OPEN response) could be discarded indefinitely, **without
> incrementing any drop counter**. That silence is why six runs of admission reports showed nothing.
>
> ⚠ **THE OBVIOUS VERSION OF THE STORY IS WRONG.** "The basebackup crowds out the gate" **cannot** be
> the mechanism: `BULK_PAYLOAD` ranks BELOW `FOREGROUND_PAYLOAD`. The prime-suspect writeup above
> guessed `CRITICAL_CONTROL` holds ≥2 ready actions every pass — **the validation census refuted that**
> (next section).

> ## ✅ VALIDATED 2026-07-20 (`candidate-20260720-165458-r2`, citus `6392a1853`): THE MIXED WORKLOAD PASSES — but did the fix work for the RIGHT REASON?
>
> **The workload passes.** Gate `-c4 -t6000` = 24000/24000, basebackup 23.26 GB / 44,372 ring laps, **329 s of
> proven concurrent overlap** (not sequential-in-disguise — the validator caught attempt 1 being exactly that,
> `sender_end` 5 s BEFORE `gate_start`, and re-ran with the gate pre-armed). All checkers green. **The six-run
> deterministic hang did not reproduce.**
>
> ### ⚠ Causation is strong-circumstantial, NOT absolute — and the census refuted the specific hypothesis
>
> The `[mailbox-diag]` census, IDENTICAL on both DPUs:
> ```
> traffic_class        ready_passes   starved_passes   starved%
> FOREGROUND_PAYLOAD             8              0        0.00%
> BULK_PAYLOAD                   8              0        0.00%
> MAINTENANCE                    6              0        0.00%
> ```
> **No `CRITICAL_CONTROL` row** — the printer skips classes that were never ready, so CRITICAL was ready on ZERO
> passes. ⇒ **The "CRITICAL holds both slots" theory is dead.** If this fix is what unblocked the hang, the
> trigger was a DIFFERENT class taking both slots — most plausibly ONE class taking both its DIRECTIONS, which is
> exactly what the new "at most one action per class in passes A/B" bound prevents.
>
> **Why this is not merely `starved%=0` because the fix is in** (that alone would be tautological — a working
> fix and "never the cause" look identical):
> 1. **`FOREGROUND_PAYLOAD ready_passes = 8`, not ~1,000,000.** Under the original hang the P3 response sat
>    ready-but-unconsumed for ~1M passes; this probe would have counted every one. Eight means the mailbox now
>    drains within a pass or two of becoming ready. That is a genuine negative for "still stuck."
> 2. **The fix is in the exact localized path** — the response is published by the peer and not consumed on
>    farnet1, and control-mailbox admission is what decides whether that response is consumed.
> 3. **The hang was 6/6 deterministic, including at `-c1`** (not a race). A deterministic structural wedge,
>    removed by a structural change to this subsystem — not a timing perturbation.
>
> **What is still NOT proven:** the census cannot show the ORIGINAL defect, because the fix is in the build that
> produced it. The exact starvation trigger (which class monopolized both slots) is inferred, not observed.
>
> ### To prove causation EXACTLY (one run, optional)
>
> Revert ONLY `TupleSinkServiceAppendReadyControlMailboxActionsRdma()` to the single strict-priority loop, keep
> `HOMER_CONTROL_MAILBOX_STARVE_DIAG=1`, run mixed. A class at `starved% ≈ 100%` with `ready_passes` in the
> hundreds of thousands nails it; anything else means the fix helped by a mechanism other than the one designed,
> and the frontier returns to the blocked-resource question (the open completes on `freed 1 outgoing
> connection(s) for reuse`, generation 2 → 3).
>
> ### ⛔ RESULT OF THAT RUN (`causation-20260720-173641`, citus `8f4d20e60` + `REVERT_STRICT_PRIORITY=1`): THE REVERT PASSED. THE FIX IS REFUTED AS THE CAUSE.
>
> The reverted (pre-fix strict-priority) build ran the SAME pre-armed mixed workload and **PASSED — no hang**,
> gate 24000/24000, basebackup 23.26 GB. Census, identical both DPUs:
> ```
> traffic_class        ready_passes   starved_passes   starved%
> FOREGROUND_PAYLOAD             4              0        0.00%
> BULK_PAYLOAD                   4              0        0.00%
> MAINTENANCE                    3              0        0.00%
> ```
> **`starved% = 0` even with the OLD code, `ready_passes` single digits.** Per the prediction directly above,
> "anything else means the fix helped by a mechanism other than the one designed" — so:
> - **Class-admission starvation was NOT the mechanism.** It did not occur even reverted; the path was never
>   contended (single-digit `ready_passes` in both builds). The workload as run does not exercise it at all.
> - **The two passes (fixed and reverted) are procedural, not the fix.** Both pre-armed the gate. Between the
>   last hanging run and the first pass, no code change explains the flip except this fix — which the revert now
>   shows is irrelevant to the outcome. Pre-arming avoids the run-6 open-during-close contention.
> - **The frontier returns to exactly the blocked-resource question** named above: the open completes on
>   `freed 1 outgoing connection(s) for reuse`, generation 2 → 3 — a connection-lifecycle/reuse interaction, not
>   mailbox scheduling. **This is where the investigation resumes.**
>
> **Methodological win, not a loss:** the causation run did its job — it FALSIFIED a plausible, already-committed
> story before it hardened into accepted fact. This is the eighth refuted cause, and the first one refuted by a
> deliberately-constructed experiment rather than by re-reading logs. The mailbox fix stays as hardening; the
> claim that it fixed the hang does not.

> ## ⚡ VALIDATED 2026-07-20 (`candidate-20260720-121352`, citus `f84e49bad`): THE FIX WORKS AND THE HANG REMAINS.
>
> **The starvation is gone.** All EIGHT reserved collectors record **ZERO** `budget` drops, on **both** DPUs,
> in **every** stage — including the still-failing mixed run. Gate `-c1` 200/200, gate `-c4` 8000/8000,
> four-role basebackup 23.26 GB: all **PASS**.
>
> **The mixed workload still FAILS**, with the identical `sql_execute kind=6 sequence=5` signature.
>
> ⇒ **Collector-admission starvation was REAL and is FIXED, but it was NOT the whole cause.** A second,
> upstream defect is now the frontier. Do not read the earlier sections of this doc as a solved case.
>
> ### The new frontier: ring DISCOVERY collapses on the receiver DPU
>
> farnet0's DPU, gate-alone (PASSES) vs mixed (FAILS), same binary:
>
> | collector | gate alone | mixed |
> |---|---|---|
> | `DPU_COMMAND_PULL` | 115,085 | **17** |
> | `DPU_PAYLOAD_PULL` | 115,085 | **17** |
> | `DPU_BACKEND_COMMAND_STAGE` | 57,398 | **8** |
> | `DPU_STAGED_COMMAND_DISPATCH` | 57,398 | **8** |
>
> `COMMAND_PULL` and `PAYLOAD_PULL` are appended under ONE shared predicate,
> `facts.totalDiscoveredReadyRingCount > 0` (`tuple_sink_service_process.c:46718`-`:46726`), and both read
> **17**. **With zero budget drops, this is an ARMING failure, not an admission failure** — the ready-ring
> count is essentially always 0, so the client's command ring is never discovered. Everything downstream is
> idle for want of input, which is why DISPATCH now shows grants≈drops≈0 instead of 3-against-1.5M.
>
> `DPU_GROUPED_CONTROL_READ` — the collector that discovers host commands — was granted **21,762,205** times
> and discovered nothing.
>
> ### ⚠ UNRESOLVED, AND IT MAY INDICT THIS FIX
>
> The same counter in the PRE-fix mixed run was **3,084**; post-fix it is **17**. Normalizing for run length
> (`GROUPED_CONTROL_READ` 53.2M pre vs 21.8M post) still leaves post-fix far lower. **Two live explanations:**
> the fix made discovery collapse earlier, or the run simply wedged sooner. **This is NOT settled and the fix
> must not be called clean until it is.**
>
> **The experiment that settles it:** re-run mixed with this change REVERTED on the same instrumented build
> and compare `DPU_COMMAND_PULL` on equal footing. Do that BEFORE any further scheduler work.
>
> ### Behaviour that DID change for the better
>
> - The basebackup now moves **~1.4 GB** before wedging; pre-fix it never started (zero binds on the sender).
> - The consumer now terminates itself with an explicit `stream terminated FAILED by sender abort …
>   TRUNCATED` error instead of hanging past both DPU SIGTERMs and needing an identity-verified reap.
>
> ### Also observed
>
> - `DPU_SPAWN` recorded **163** `budget` drops on farnet1 — the unreserved collector already flagged below as
>   meeting the silent-hang criterion. Watch it.
> - The teardown `ALARM arena unbind found NO import … LEAKED` recurs, unchanged, on farnet1.

## ✅ THE FIX AS IMPLEMENTED (2026-07-20, citus tree; validated as REMOVING THE STARVATION, but the mixed workload still fails)

**Shape: reserve the whole command-plane pipeline, and stop double-charging reserved grants.**

1. `HomerServiceProgressCollectorReservedLifecycleAdmission()` grows from 2 members to **8**: the two
   listeners plus all six selected-DPU command-plane stages (`STAGE`, `DISPATCH`, `PUBLISH`, `EXECUTE`,
   `COMPLETION_PULL`, `COMPLETION_PUSH`).
2. `MAX_LIFECYCLE_COLLECTOR_GRANTS` 2 → **8**, preserving **cap == membership** (the order-independence rule).
3. A reserved grant is billed to the reserved line **only** — it no longer increments
   `planGrants`/`collectorGrants`.
4. `MAX_GRANTS` (16) and `MAX_COLLECTOR_GRANTS` (6) are **UNCHANGED**.
5. The `PROGRESS POLICY-ADMISSION DROPS` line now prints `lifecycle=` grants and cap.

**Why the whole pipeline and not just the measured stages.** The six are joined by BOUNDED queues, so
guaranteeing a subset relocates the wedge to whichever stage is left out. That was not hypothetical: the first
version of this fix reserved only `DISPATCH` and `COMPLETION_PULL`, and refutation showed the wedge would move
to `PUBLISH`, whose `backendCommandPublishSlots[]` is 16 deep and whose overflow path reports an internal
`blocked` and silently retains the command. **Reserve the pipeline, or none of it.**

### ❌ REJECTED, AFTER BEING APPROVED — "compensating cap resize" (raise the caps by the size of the reserved set)

This was the agreed plan and it does not survive its own arithmetic once the reserved set is the full pipeline.
Reserved members are **interleaved** through the fixed order (ordinals 1, 2, 10–14, 16), so if reserved grants
are billed to the shared cap `C`, the count at `DPU_PAYLOAD_PULL` (ordinal 15) is already
`7 + min(7, C-2)` — which is `>= C` for **every `C < 15`**. That rejects the 24-million-grant **bulk data
path** in every busy pass. Raising `C` to 15 to rescue it then starves `DPU_SPAWN` (18), whose starvation is a
peer hung in `WAIT_PEER_OPEN` with no error on either side.

**⇒ A cap cannot be sized to spare everyone; it can only choose a different victim.** The double-charging, not
the cap size, was the amplifier — so the fix removes the double-charge and leaves the caps alone.

### What the refutation got right, and what it got wrong

- ✅ **RIGHT, and it changed the design:** guaranteeing a subset of a bounded-queue pipeline relocates the hang
  to `PUBLISH`; the "shared budget unchanged" invariant as originally written was inaccurate; the pgbench
  client does NOT "wait forever" (it has a 30 s deadline and prints the `kind/sequence` line); the admission
  diagnostic did not expose the lifecycle line; the `CONTRACTS.md` entry was stale against the staged source.
  All corrected.
- ❌ **WRONG:** *"`DISPATCH` is the wrong collector — `SQL_EXECUTE` is owned by `STAGE`."* The ownership claim
  is right but the conclusion is not. The staged-command queue is **HEAD-BLOCKING**, and ownership is
  partitioned: `START_COMMAND` → `STAGE`, **everything else → `DISPATCH` (the catch-all)**. Starving `DISPATCH`
  leaves a non-START command immortal at the head, making every START behind it unreachable. The tree documents
  this exact wedge as having already happened once with `POLL_COMMAND_COMPLETION`. The measurement confirms it:
  `STAGE` was granted **660,180** times and achieved nothing, because it kept peeking a head it does not own.

### Residual risks to watch on the validation run

- **Per-phase work rises**: worst case `MAX_GRANTS + MAX_LIFECYCLE_COLLECTOR_GRANTS` = **24** actions, up from
  16 — but only when all eight stages simultaneously have real work.
- **`COMPLETION_PULL` is not a cheap grant**: it scans all imports/rings. Guaranteeing it makes that scan
  every-pass while any command is outstanding. Watch its cost.
- **`DPU_SPAWN` (18) remains unreserved** while meeting the silent-hang criterion. First suspect if a *session
  bring-up* (rather than a command) hangs.
- **No coupling validation exists** — the caps are independently env-overridable, so an override can silently
  break the `cap == membership` rule.

## The fix space that was considered (kept for the reasoning, not as a menu)

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

## Deferred fix: close-drain blast radius (design blocked on the hang trace)

**The defect (latent, only fires when a command is genuinely stuck — i.e. the hang):** the DPU setup-TCP
server is single-client ([`homer_service_dpu_setup_tcp.c:445`](../../../../../citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_setup_tcp.c)),
and its close-drain has two unbounded gates that `return true` forever — gate 1 = DOCA tasks in-flight (`:759`),
gate 2 = the semantic callback `HomerServiceDpuClosingSetupSemanticDrained` (`:783`). A session whose command
never completes wedges gate 2, and *"the server accepts ONE client at a time, so every later setup is blocked"*
(`:379`). One stuck session → total setup outage.

**Chosen repair (2026-07-20): Option A — bounded wait → terminal, gate-2-scoped.** Gate 2 is the safe case
(DOCA already at 0, so detaching host mmap is safe); gate 1 stays unbounded for now (force-finalizing with DOCA
in-flight would free host memory a task still reads).

**⛔ THE TRAP that reshaped the fix:** the plan assumed the terminal action could just TRIGGER the existing
finalize path on a deadline. It cannot. `HomerServiceDpuResetSelectedSessionForClose`
([`tuple_sink_service_process.c:43807-43814`](../../../../../citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c))
is **drain-ASSUMING**: it re-checks `SelectedSessionCloseWorkDrained` and RETURNS AN ERROR on an undrained
session — it does not forcibly reset. Force-calling it on the wedged (undrained) session errors mid-loop,
releasing the accept slot but leaving sessions occupied and the host mmap mapped (the detach at `:805` never
runs) → a leak.

**⇒ Preferred design is variant (c):** after the deadline, force the stuck command to **terminal-failed** (the
same state a real failure produces) so `SelectedSessionCloseWorkDrained` returns true and the existing,
well-tested finalize path runs UNCHANGED. Only new code is the surgical command-state flip; no new teardown, no
leak.

**STATUS: DE-ENTANGLED and ready to implement (2026-07-20, after the hang trace).** The trace REFUTED the
connection-reuse hypothesis and confirmed A's close does NOT gate B's open — so variant (c) is now
well-specified and independent of the open hang: force **A's own** stuck outstanding command to terminal-failed
(the command that defers A's `TupleSinkServiceResetSession` at `tuple_sink_service_process.c:25365`,25454) so
`SelectedSessionCloseWorkDrained` returns true and the existing finalize path runs unchanged. No longer blocked
on the hang. **DEFERRED to a future session (user decision 2026-07-20)** — fully specified here; only fires when
a command is genuinely stuck, so the latent risk is bounded. Pick up by implementing the wall-clock deadline +
the command-terminalization flip, then the existing finalize path.

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
