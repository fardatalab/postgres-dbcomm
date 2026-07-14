# Defect B — twelve DPU collectors share one feedback struct, and it defeats the starvation escape hatch

**Status:** PLAN, not yet implemented. **FB-0 (instrumentation) DONE and MEASURED — see the banner: it refuted BOTH the prior review's mechanism AND my liveness escalation.**
**Justification now rests on MEASURABILITY (S6 blocker) + a genuinely INVERTED feedback signal — not on latency, and not on liveness today.** **Blocks:** command-plane **S6** (the plan states this explicitly:
*"⚠ PREREQUISITE: fix B (per-collector feedback) BEFORE recording any S6 number"*).
**Code baseline:** citus `a551bcf6e`.
**All line numbers below are `src/backend/distributed/utils/homer/tuple_sink_service_process.c` unless stated,
and were re-derived from the tree at this SHA — the numbers in
[`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
§0b are STALE (they predate several thousand inserted lines).**

---

> ## ⛔⛔ MEASURED, 2026-07-14 (FB-0). **BOTH EARLIER STORIES WERE WRONG. HERE IS WHAT THE HARDWARE SAYS.**
>
> ### ❌ REFUTED — "grouped-control is STRUCTURALLY IMMUNE to blind backoff"
>
> That claim was VERIFIED-labelled here and it is **FALSE**. Widened starve-diag, real 4-machine gate run:
>
> ```
> node B (farnet1 DPU):  DPU_GROUPED_CONTROL_READ   feedback-backoff   1,420,542
> node A (farnet0 DPU):  DPU_GROUPED_CONTROL_READ   feedback-backoff   1,991,772
> ```
>
> It is the **ONLY** collector suppressed by feedback-backoff on either node. The backoff predicate returns
> "not suppressed" whenever `knownExpectedWork` holds — so **the measurement PROVES `knownExpectedWork` was
> false 1.4M+ times.** The immunity was asserted from the *name* of a guard that was never checked to be armed.
>
> ### ❌ ALSO REFUTED — my own escalation: "the bound is defeated ⇒ this is a LIVENESS bug"
>
> Equally wrong, and refuted by the metric I built to test it:
>
> ```
> DPU_GROUPED_CONTROL_READ:  max_backoff_since_grant = 1 (node B) / 2 (node A)     [bound = MAX_SKIP_GRANTS = 4]
>                            grants                  = 4,549,555 / 3,816,775
> ```
>
> **Bounded at 1–2, and granted 4.5 MILLION times.** The escape hatch is not defeated in practice: the
> collector is granted so constantly that its OWN grants keep the shared clock fresh. **Aliasing only bites a
> collector that is RARELY granted** — which is exactly the setup-TCP listener's situation for four days.
>
> ⇒ **It starves nothing today. It is NOT a latency fix and NOT a liveness fix on this workload.**
>
> ### ⛔ AND THE MECHANISM I PUBLISHED FOR IT WAS BUILT ON A STALE COMMENT
>
> I claimed `knownExpectedWork` is false because "the arena imports 49 rings of which ZERO are enrolled"
> (`tuple_sink_service_process.c`, at the discovery-arming site). **That sentence was FALSE, and its own
> correction had already been written a day earlier IN ANOTHER FILE** (`homer_service_dpu_dma.c`, *"CORRECTED
> July 13, 2026"*): role-5 result rings carry `HOMER_DPU_BRIDGE_RING_FLAG_PUBLISH_LINE`
> (`homer_frontend_agent.c:962`) and **are** enrolled. The stale sentence is now deleted.
>
> **What is actually true: `groupedControlRingCount == 0` at the moments of suppression (forced by the
> predicate). WHY it is 0 so often is NOT established. Do not invent a reason — measure it.**
>
> > **THE LESSON, TWICE IN ONE DAY, IN BOTH DIRECTIONS:** the first review reasoned from the NAME of a guard;
> > I reasoned from a COMMENT that had already been refuted elsewhere. **Neither of us reasoned from a NUMBER.**
> > A guard you have not watched fire is a hypothesis. A comment that survives its own refutation is an
> > instruction to write the next bug.
>
> ### ⚠ AND THE ANSWER WAS THREE LINES FROM WHERE I WAS READING
>
> The comment immediately above `:10493` says: *"…which starved the sibling `DPU_PE_DRAIN` collector — and with
> it the DPU setup-TCP accept loop — **permanently**."* The setup-listener incident produced **three** scars —
> a private source, `knownExpectedWork`, and the bypass list — and one of them already immunized the very
> collector I spent this analysis claiming was exposed.
>
> > **THE PROCESS FAILURE, NAMED: I established the mechanism LAST.** I asserted the latency cause, then
> > argued it better, and only *then* went looking for what protected it. **"What makes this safe today?" is
> > the FIRST question, not the last** — because on a codebase this scarred, the answer is usually already in
> > the file, written by the last person who got burned.
>
> ### ✅ WHAT SURVIVES (C1, C2, C3 — all VERIFIED)
>
> - **C1 ✅** Every *admitted* collector gets its OWN action grant and its OWN `HomerProgressResult`
>   (`HomerServiceExecuteProgressExecutionPlan` `:48124`/`:48128`; dispatcher `:47877`;
>   `HomerServiceExecuteDpuDmaAction` has a local result, `:46751`). **So this is a routing fix, not a redesign.**
>   ⚠ *Correction:* "each is granted" is FALSE — admission is not guaranteed (see the quota finding below). The
>   right statement is *"every ADMITTED collector receives a separate grant and result."*
>   ⚠ The builder is `HomerServiceBuildMachineBaselineProgressActionPlanPhase` (`:11646`) — the name
>   `HomerMachineBaselineCompileExecutionPlan` used in CLAUDE.md and §4 is **stale**.
> - **C2 ✅ (more strongly than claimed)** The legacy one-action compiler is dead: startup **rejects** DPU
>   enablement unless the policy is machine-baseline (`:48633`), and the coarse ready-set builder admits no DPU
>   source (`:14017`).
> - **C3 ✅** Nothing reads `dpuDmaFeedback` as an engine-wide aggregate. The `repeatedlyEmpty` reader (`:12288`)
>   is **dormant** — adaptive-bounded jumps over its only call site (`:12388`/`:12400`). **Safe to split.**
>
> ### 🆕 THE REVIEW FOUND A HALF-FIX I WOULD HAVE SHIPPED — a SECOND feedback writer
>
> **`HomerServiceRecordProgressGrantBudgetFeedback` (`:14663`)** resolves through
> `HomerServiceProgressFeedbackForSource(&grant->source)` (`:14683`) — **outside** `FinishProgressGrant`, and it
> is called after **every** executed action (`:48137`/`:48147`). FB-1 as written routed only the ~31
> `FinishProgressGrant` sites, so **the budget feedback would have stayed aliased.** A fix that looks complete
> and is not.
>
> ### ✅ THE BETTER DESIGN CAME OUT OF THE OBJECTION — key on `SourceId.index`, not on `actionKind`
>
> The source-ID contract **already says so** (`:2931`): *"**kind** selects the executor family, **index**
> selects the fixed slot inside that family."* `COMMAND_RING` / `COMPLETION_RING` / `PAYLOAD_STREAM` already
> route feedback by **kind + index** (`:14220`-`:14249`). **The DPU family is the only one that always uses
> index 0.**
>
> **FB-1 (REVISED): give each DPU collector its own source INDEX, keep ONE source kind.**
> `HomerServiceMachineBaselineCollectorSource` (`:10614`) already knows the collector kind when it builds the
> source ref — stamp the index there. Then `HomerServiceProgressFeedbackForSource` routes both writers by
> kind+index, and:
> - **both** feedback writers are fixed, automatically;
> - **zero** finish sites are touched (vs ~31);
> - **no new `HomerProgressSourceKind`** — so CLAUDE.md's four-hand-edited-switch hazard is never armed.
>
> **This supersedes §4/FB-1's "route by action kind" entirely.** It is not a tweak of my design; it is a better
> one, and it was *inside the objection*.
>
> ### 🆕 A SEPARATE STARVATION SURFACE, unrelated to feedback — **`MAX_COLLECTOR_GRANTS = 6`** (`:185`)
>
> **Twelve** DPU collectors, a **fixed append order**, and a **six**-grant collector quota per plan
> (`:10571`). Grouped-control is appended early enough to be safe under defaults; **the later DPU pipeline
> collectors are not guaranteed admission at all.** This has nothing to do with aliasing and is not fixed by
> FB-1. **Record it; do not fold it in silently.**
>
> ### 🔎 THE ~223 µs IS UNEXPLAINED AGAIN — and my *alternative* was ALSO stale
>
> §3's fallback ("four 1024-slot scheduler-fact scans per pass") is **wrong**: the DPU candidate builder has
> **one** call site (`:45898`) and the other phases return before it (`:45851`, `:45887`). **One scan per pass,
> not four.**
>
> **The reviewer's hypothesis (INFERRED, NOT VERIFIED — do not act on it until the mechanism is proven):** the
> cross-pass DPU pipeline. Grouped-control submission is explicitly **non-blocking and does not run
> `doca_pe_progress`** (`homer_service_dpu_dma.c:1487`), and PE-drain candidacy is decided from facts sampled
> **before** the phase executes (`:44497`, `:44623`). **So from an idle state, a newly submitted read cannot be
> harvested until a LATER service pass.** The quantity to measure is therefore **the service pass cadence**, not
> the feedback. *Measure it before believing it.*

## 0. The defect in one sentence

> **`HomerServiceExecuteDpuDmaAction` (`:46743`) knows exactly which collector it just ran — it switched on
> `actionGrant->actionKind` to run it — and then throws that identity away by finishing the grant against the
> shared `HOMER_PROGRESS_SOURCE_DPU_DMA` source, so all twelve DPU collectors' feedback lands in one struct.**

The per-collector result **already exists**. Only the *routing* of it is broken. That is why this fix is a
day's work and not a redesign, and it is why the earlier framing ("the DPU family needs per-collector
accounting built") over-scoped it.

---

## 1. VERIFIED facts (each checked in the tree, not inherited from the older docs)

| # | fact | site |
|---|---|---|
| V1 | One shared feedback struct for the whole DPU family: `HomerProgressSourceFeedback dpuDmaFeedback;` | `:3792` |
| V2 | **Twelve** collectors fall through to it on the READ path (`HomerServiceProgressCollectorFeedbackForKind`) | `:41662`-`:41674` |
| V3 | The WRITE path (`HomerServiceProgressFeedbackForSource`) keys on **source kind**, and `HOMER_PROGRESS_SOURCE_DPU_DMA` returns the one shared struct with **no index** — while `COMMAND_RING` / `COMPLETION_RING` / `PAYLOAD_STREAM` all select a per-index struct from an array | `:14236`-`:14237` vs `:14220`-`:14233` |
| V4 | All three backoff inputs are copied from that one struct | `:44832`-`:44834` |
| V5 | `lastTouchedTick = tick` is written **UNCONDITIONALLY**, *before* the productive/empty branch — so an **empty** sibling grant refreshes the clock exactly as a productive one does | `:14271` (branch at `:14439`) |
| V6 | The blind-backoff escape hatch reads that shared clock: `tick - lastCollectedTick >= 4 ⇒ do not skip` | `:10517`-`:10521` |
| V7 | `DPU_GROUPED_CONTROL_READ` — the collector that **discovers host commands** — is NOT on the bypass list | `:10442`-`:10455` |
| V8 | Per-collector ACTION kinds already exist, 1:1 with the collectors (`HOMER_PROGRESS_ACTION_DPU_GROUPED_CONTROL_READ`, `…_DPU_PE_DRAIN`, …) | `:2779`-`:2795` |
| V9 | `HomerServiceExecuteDpuDmaAction` dispatches on `actionGrant->actionKind`, produces a **per-action** `grantResult`, and then calls `HomerServiceFinishProgressGrant(sourceRef, …)` — discarding the action identity | `:46743`, and every `FinishProgressGrant` call in `:46772`-`:47545` |
| V10 | The guard comment says *"Do not add a twelfth aliasing collector"* — and the switch now lists **exactly twelve** | `:3808` vs `:41662`-`:41673` |

### 1.1 ⚠ The code already contains this bug's own post-mortem — for a DIFFERENT collector

`:2570`-`:2578`, on `HOMER_PROGRESS_SOURCE_DPU_SETUP_LISTENER`:

> *"S3.1b: the DPU setup-TCP listener is its OWN source, deliberately not part of `HOMER_PROGRESS_SOURCE_DPU_DMA`.
> Sharing that source is what starved it: the eleven DPU collectors alias one `HomerProgressSourceFeedback`, so
> **every granted DPU sibling reset the listener's blind-backoff starvation clock and the escape hatch never
> opened.**"*

That is the four-day silent DPU-setup-listener starvation (CLAUDE.md hazards §8.1: *"wedged the DPU setup
listener for four days without a single log line"*). **It was diagnosed correctly and fixed for exactly one
collector.** The other twelve were left aliased, and V5+V6 confirm the mechanism is intact for them.

> **The rule this earns: when you fix an aliasing bug by un-aliasing ONE victim, you have not fixed the
> aliasing. You have fixed the victim you happened to be looking at.** The remaining victims are silent by
> exactly the same mechanism, and the comment you write to explain the fix becomes the strongest possible
> evidence that you knew and moved on.

---

## 2. The mechanism, precisely

The blind-backoff skip (`HomerMachineBaselineFeedbackBackoffSuppresses…`, `:10480`-`:10525`) fires when
**all** of:

```c
!collector->knownExpectedWork && collector->waitingMachineCount == 0   /* :10493 */
&& collector->recentEmptyPolls      >= 8                              /* :10497, shared  */
&& collector->recentProductivePolls == 0                              /* :10498, shared  */
&& (tick - collector->lastCollectedTick) < 4                          /* :10517, shared  */
```

The last three are **all read from the shared struct** (V4). And the writer (V5, `:14439`-`:14453`) makes them
move together:

- an **empty** grant of *any* DPU sibling: `consecutiveEmptyGrants++`, `consecutiveProductiveGrants = 0`, **and
  `lastTouchedTick = tick`**;
- a **productive** grant of *any* DPU sibling: `consecutiveEmptyGrants = 0`.

So during an idle window — **exactly the window that precedes a command arriving** — the siblings poll, find
nothing, and *each empty poll simultaneously*:

1. pushes the shared empty streak toward the ≥ 8 threshold, **and**
2. **refreshes the shared clock that the escape hatch is watching.**

> **The two conditions that must BOTH hold for a suppression are reinforced by the SAME event.** The escape
> hatch at `:10517` is written to mean *"if I have not been touched in 4 grants, stop skipping me"* — its own
> comment (`:10511`-`:10515`) says it *"keeps an otherwise idle service from suppressing unknown-arrival
> collectors indefinitely."* **Under aliasing it means "if NO DPU collector has been granted in 4 grants",
> which on a live service is never.** The safety property the comment claims is not the one the code has.

And `DPU_GROUPED_CONTROL_READ` is **blind by construction** — discovering work the DPU cannot otherwise know
about is its entire job — so `knownExpectedWork` is false for it *by design*. The **only** thing standing
between the discovery collector and suppression is `waitingMachineCount > 0`.

---

## 3. ⚠ WHAT IS **NOT** ESTABLISHED — and the overclaim that was made and retracted

**CLAIMED (2026-07-13, by me, and it was an OVERCLAIM):** *"the aliased feedback explains the measured
~223 µs host-publication discovery latency."*

**Why it was wrong to say:** it was asserted **before reading the backoff code.** Taken at face value, the
backoff's own bound is `MAX_SKIP_GRANTS = 4` (`:228`) — **at most four skipped grants**, which cannot produce
223 µs unless a grant costs ~56 µs. The claim only acquires teeth via V5+V6 (the shared clock defeating the
escape hatch), which is a *different* argument that I had not made yet. **A confident causal story asserted
ahead of its mechanism is exactly the failure this project keeps paying for.**

**What is honestly OPEN:** *does the skip actually fire on our workload, and for how many passes?*

- If it **does not fire**, defect B is a **correctness + measurability** bug (still a hard S6 blocker, still
  worth fixing) and the 223 µs lives somewhere else — the leading alternative being the per-pass scheduler-fact
  scans (`HomerDpuDmaGetSchedulerFacts` is called once per each of four plan phases, and each call walks the
  1024-slot import table — see the scheduler doc's "What remains true after the fix").
- If it **does fire**, the 223 µs is localized to a line, and we fix it here.

**This is a question for a probe, not for an argument.** See FB-0.

---

## 4. THE PLAN

### FB-0 — PROBE FIRST. Do not fix what is not yet localized.

The counter **already exists**: `HomerMachineBaselineRecordFeedbackBackoffSkip` (`:10415`) bumps
`lastPlanFeedbackBackoffSkipCount` and ORs the collector into `lastPlanFeedbackBackoffCollectorMask`
(`:10423`-`:10427`). It is unusable as-is for two reasons:

1. it is **per-plan** — reset every plan build (`:10335`-`:10336`), so it answers "did the *last* plan skip",
   not "did this run skip";
2. it is only printed inside the stats dump (`:8435`-`:8436`).

**The change (small, and it is a diagnostic, not a fix):**
- a cumulative `uint64_t feedbackBackoffSkipsByCollector[HOMER_PROGRESS_COLLECTOR_COUNT]`, incremented in
  `HomerMachineBaselineRecordFeedbackBackoffSkip` — one add on a path that is already taken;
- dumped once at service exit, **naming each collector**, not a bare total. (Hazard: *"a guard that ANDs N
  conditions must report WHICH one failed"* — a bare skip total is the same decoy in another costume.)
- ⚠ **Bound the output** and **name what was measured** — `feedback_backoff_skips[DPU_GROUPED_CONTROL_READ]=N`,
  not `skips=N`.

**Run the gate. Read the number.** Then, and only then, decide whether FB-1 is a latency fix or a correctness
fix — and **say which one it turned out to be**, rather than letting the fix inherit the credit.

### FB-1 — THE FIX: give each DPU collector its own feedback

**The insight that makes it small (V8+V9): the action identity is already in hand at the finish site.**

1. `HomerProgressSourceFeedback dpuDmaFeedbacks[HOMER_DPU_FEEDBACK_SLOT_COUNT]` replaces the single
   `dpuDmaFeedback` (`:3792`).
2. A total, compile-time-checked map collector-kind → feedback slot. `_Static_assert` that every
   `HOMER_PROGRESS_COLLECTOR_DPU_*` has a slot. (⚠ CLAUDE.md regression-sweep rule: **`-Wswitch` protects
   nothing here** — nearly every switch has a `default:` arm. Enumerate by hand.)
3. **WRITE path:** `HomerServiceFinishProgressGrant` must carry the action/collector identity for the DPU
   family. `HomerServiceExecuteDpuDmaAction` (`:46743`) has it already; it is discarded at each of the ~25
   `FinishProgressGrant(sourceRef, …)` calls in `:46772`-`:47545`.
4. **READ path:** `HomerServiceProgressCollectorFeedbackForKind` (`:41635`) — the twelve cases at `:41662`-`:41674`
   index the array instead of returning the shared struct.
5. Update the `:3804`-`:3808` comment: it says *eleven* and forbids a twelfth. Both facts are now wrong.

**Rejected alternative — twelve new `HomerProgressSourceKind` members.** It would fix the write path
"naturally", but CLAUDE.md's regression sweep is explicit: a new `HomerProgressSourceKind` needs **four**
hand-edited switches, *and the fourth is not a switch* (`HomerMachineBaselineCompileExecutionPlan`'s phase
body) — miss it and the collector is *armed on every pass and granted on none*, which "looks exactly like an
idle service." Twelve of those is twelve chances to reproduce the bug we are fixing. **Route by action kind;
do not multiply source kinds.**

### FB-2 — the SECOND aliasing instance (scope decision needed)

`DPU_PEER_COMMAND_EGRESS` and `DPU_PEER_COMPLETION_EGRESS` alias **`peerSendCqFeedback`** together with
`PEER_SEND_CQ` (`:41656`-`:41659`). **Same defect, different struct**, and it was not in defect B's original
statement. Fold in or split out — **decide, do not silently skip.**

---

## 5. Falsifiable claims — REFUTE THESE BEFORE BUILDING

1. **C1.** The per-action `grantResult` in `HomerServiceExecuteDpuDmaAction` is genuinely per-collector — i.e.
   under the machine-baseline policy each of the twelve is granted and finished *separately*, not as phases
   inside one aggregate call. **If C1 is false, FB-1 is a redesign, not a routing change, and the whole plan
   changes shape.** *This is the one I am least sure of.*
2. **C2.** The `one-action compatibility compiler` (`HomerServiceProgressActionKindForSourceKind`, `:10025`,
   which maps `DPU_DMA → HOMER_PROGRESS_ACTION_DPU_PE_DRAIN` at `:10060`) is reached only under
   non-machine-baseline policies. **If it is reachable under the policy we actually run, then eleven of the
   twelve collectors are not granted at all under that path**, and the aliasing is the *lesser* problem.
3. **C3.** No consumer of `dpuDmaFeedback` depends on it being *shared* (i.e. nothing reads it as an
   engine-wide aggregate). Splitting it must not silently zero a signal someone relies on. `:12288`
   (`repeatedlyEmpty … >= 8U`) is one such reader — **check what it governs.**
4. **C4.** `waitingMachineCount > 0` is what currently prevents a permanent discovery stall. If that is false,
   there is a **live hang** here, not merely a latency cost.

---

## 6. Acceptance

- **FB-0:** the gate runs, and the per-collector skip counts are reported. **The number decides the framing.**
- **FB-1:** no `ALARM`; the gate passes `2000/2000`, 0 failed; the 4-role basebackup still passes (it is the
  only workload that wraps the byte ring — a green gate says nothing about it, per CLAUDE.md);
  the DPU TCP transport smoke passes.
- **Report the before/after discovery latency with the SAME `-t` and the SAME logging state.** A `--debug` run
  is ~10% slower and must never be compared to a stripped one (audit §22.10.2).
- **If FB-1 does not move the 223 µs, SAY SO.** The fix is justified as a correctness fix regardless; letting
  it quietly take credit for a latency it did not cause is how the next stale claim gets born.

---

## Related

- [`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
  — where defect B was named (§0b). ⚠ its line numbers are stale; this doc re-derives them.
- [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md) — S6, which this blocks.
- [`resource_retirement_contract_audit.md`](./resource_retirement_contract_audit.md) — the P0 set, now COMPLETE.
- [`post_gate_basebackup_open_stall.md`](./post_gate_basebackup_open_stall.md) — the ~223 µs discovery period
  was measured there; this doc proposes (but has NOT proven) a cause.

---

## FB-0 — LANDED. The instrumentation, and what the numbers actually said.

**The old diagnostic could not have seen this bug.** `HOMER_DPU_SETUP_STARVE_DIAG` reported exactly FOUR
collectors — `PE_DRAIN`, `SETUP_LISTENER`, `SPAWN`, `DOORBELL` — i.e. **the victims someone already knew about**
(three of which have since been given their own un-aliased source kinds). It was structurally **blind to all
twelve** collectors that still alias `dpuDmaFeedback`, including the one being starved.

And its rate limiter was `static uint64_t dropN` **inside the macro body — one counter shared across every kind
that site reported.** A shared rate limit across categories is a FILTER, not a limit: the noisiest collector
eats the print budget and the STARVING one never prints. *(Identical to the defect fixed in L0's per-phase
counters. Same shape, second occurrence.)*

**FB-0 (renamed `HOMER_COLLECTOR_STARVE_DIAG`):** every collector kind, a `(kind, reason)`-keyed limiter, a
name decoder with **no `default:` arm** (so `-Wswitch` fails the build when a collector is added), and a totals
summary at teardown, printed **before** the session-reset loop — whose own unrelated fail-stop otherwise
silences it on exactly the runs that need it.

### ⚠ THE ACCEPTANCE METRIC IS NOT THE DROP TOTAL — AND GETTING THIS WRONG WOULD HAVE FAILED A WORKING FIX

The defect is **UNBOUNDEDNESS, not frequency.** A total cannot see it:

| | total | max run |
|---|---|---|
| broken (hatch defeated) | huge | huge |
| **fixed** (hatch works) | **STILL HUGE** | tiny |

I originally predicted "the 1.4M count collapses." **It would not have.** Grading FB-1 on the total would have
marked a *working* fix as a failure.

⚠ **AND THE FIRST REPLACEMENT METRIC WAS ALSO WRONG** (Codex, VERIFIED): "max consecutive rejections ≤ 4" is
**not** the bound the scheduler implements. The predicate bounds *global-grant AGE*
(`HomerProgressFeedbackTick - lastCollectedTick < MAX_SKIP_GRANTS`), not a rejection count — and several
planning decisions can occur at the same tick. Worse, my counter reset on **plan append**, while the scheduler's
clock advances on **execution**; an executor short-circuit falsely resets it.

**⇒ THE CORRECT FB-1 CRITERION:** at every feedback-backoff rejection, record the rejected collector's **OWN**
feedback age. **After FB-1 there must be ZERO rejections at own-age ≥ MAX_SKIP_GRANTS.** Separately measure
eligible→executed distance, classified by final drop reason, because source-lookup and budget remain
independent gates.

### The measurements (gate, both DPUs)

| collector | reason | node B | node A |
|---|---|---|---|
| **`DPU_GROUPED_CONTROL_READ`** | **feedback-backoff** | **1,420,542** | **1,991,772** |
| `BACKEND_COMPLETION_RING` | no-source | 1,182,200 | — |
| `PAYLOAD_FRONTIER` | no-source | 8,004 | 1,377,246 |
| `PEER_SEND_CQ` | budget | 22,985 | 39,183 |
| `COMMAND_SEND_CQ` | already-planned | — | 29,425 |

- **`no-source` in the millions** — `BACKEND_COMPLETION_RING` and `PAYLOAD_FRONTIER` are appended by the generic
  machine-wait bridge but have **no mapping** in `HomerServiceMachineBaselineCollectorSource`, so they hit
  `default: return false` and are dropped on **every pass, forever**. VERIFIED: they consume **no budget**
  (source lookup precedes reservation) and cannot exhaust the candidate array (the enum has < 64 kinds). Stale
  scaffolding noise — wasted scans and a misleading diagnostic, **not** a liveness defect.
- **`already-planned`** on `COMMAND_SEND_CQ` is expected double-consideration. Benign.

---

## §FB-1' — WHAT THE SHARED FEEDBACK IS ACTUALLY DOING. **It is not weak guidance. It is INVERTED guidance.**

Tracing every LIVE consumer of `dpuDmaFeedback`:

- the **adaptive-bounded** reader — **DEAD** (unconditional `goto` jumps its only call site; the comment says the
  block "is intentionally retained as design scaffolding, but it is not safe to execute yet");
- the **item budget** — does **not** read feedback (`readyItemCountHint` / `waitingMachineCount`);
- the **blind backoff** — **the only live consumer**;
- and of the twelve aliased collectors, those armed from exact counts have `knownExpectedWork = true` and never
  reach the backoff test at all.

> ### ⇒ **TWELVE COLLECTORS WRITE INTO A STRUCT THAT EXACTLY ONE OF THEM READS.**
>
> `DPU_GROUPED_CONTROL_READ` is the only collector that is **both aliased AND blind**. So the signal deciding
> whether to suppress **discovery** is *"have the other eleven found work lately?"*
>
> **And that is ANTI-CORRELATED WITH NEED.** Discovery exists to find host commands nothing else can see — *"a
> host tail update has NO SEPARATE DOORBELL"*. The eleven siblings all go empty **precisely when the DPU is
> idle**, which is exactly when a freshly-published host command sits undiscovered. The shared empty-streak
> crosses its threshold **at the moment discovery matters most.**
>
> What saves us today is luck: the collector is granted 4.5M times, so its OWN grants reset the streak faster
> than the siblings can build it. **The mechanism is wrong; the workload hides it.**

**⇒ This upgrades FB-1's justification, and it is the honest one:** per-collector feedback makes *"I have polled
8 times and found nothing"* a statement **about the collector itself**, which is the intended semantics. Today it
means *"the other eleven found nothing"* — a different question, and the wrong one.

### ⚠ THE TEMPTING WRONG MOVE — putting DPU_GROUPED_CONTROL_READ on the bypass list

It fits the bypass list's own stated criterion **exactly**: *"arrivals external and unobservable without a
syscall; 'no arrivals so far' can never justify 'stop looking'."* One line. **DO NOT DO IT.**

The bypass list's members are *lifecycle* collectors that poll a socket and are cheap when idle. Grouped-control
**submits DMA reads** and competes for the 6-collector quota every pass. **A permanently-eligible discovery
collector is exactly what starved PE_DRAIN and wedged the DPU setup-TCP accept loop for four days.** The backoff
for it is LEGITIMATE. What is broken is the SIGNAL it backs off on. Fix the signal, not the eligibility.

### Two corrections FB-1 must absorb (Codex, both VERIFIED by me)

1. **THE CORE IS ALIASED TOO.** `HomerServiceProgressCoreForSource` ignores `id.index` for `DPU_DMA`, while
   `COMMAND_RING` / `COMPLETION_RING` index theirs. `HomerProgressSourceCore` carries real mutable state
   (`readyReasonMask`, `blockedReasonMask`, `creditBlocked`, `outstandingSignaledWrCount`) and every grant
   last-writer-wins it. Feedback-only indexing yields an **INCOHERENT ref**: `(DPU_DMA, index=7)` resolves
   feedback slot 7 but core slot **0**, and both resolvers are called side by side. Nothing live breaks today
   (the adaptive path that would gate on it is dead), but the abstraction becomes dishonest. **Index the core
   with the same map, or make it explicitly immutable family metadata and stop writing per-grant hints into it.**
2. **FB-1 IS NOT A TWELVE-COLLECTOR LIVENESS FIX.** `MAX_COLLECTOR_GRANTS = 6` remains. Grouped-control is
   budget-safe only by **fixed append order**, not by reservation (reserved admission names only the setup
   listener and doorbell), and that safety is **configuration-dependent** (the caps are env-tunable to 1). Every
   newly-admitted grouped grant consumes one of six collector slots, so FB-1 can **RELOCATE** the loss to later
   DPU collectors. The measured `DPU_PAYLOAD_PULL budget` is exactly compatible with that residual defect.

### Confirmed SAFE to split (VERIFIED)

- Nothing reads `dpuDmaFeedback` as an engine-wide aggregate (its only such reader is behind the dead `goto`).
- Nothing dedups on DPU source identity: `HomerProgressSourceRefSame`'s only consumer,
  `HomerProgressSourcePlanContains`, is also behind that `goto`. Machine-baseline dedups by
  `collectorPlanned[kind]`. The ready set does no identity dedup, and machine-baseline collector refs never
  enter it.
- **Zero `FinishProgressGrant` call sites need editing** — both feedback writers resolve through
  `HomerServiceProgressFeedbackForSource`.

### Stale comments deleted in this commit (all three were actively misleading)

- *"the arena imports 49 rings of which ZERO are enrolled"* — **FALSE**, corrected a day earlier in another file.
  **This one cost a wrong published mechanism.**
- *"the ELEVEN DPU collectors"* ×2 — there are **TWELVE** (enumerated).
- *"Do not add a TWELFTH aliasing collector"* — **a twelfth was added anyway.** A guard phrased as a magic count
  rots the moment it is disobeyed, and then **the guard itself hides the regression.** Replaced with a rule that
  names no number: *a new DPU collector must not join that shared case-label block; give it a 1:1 triple.*
