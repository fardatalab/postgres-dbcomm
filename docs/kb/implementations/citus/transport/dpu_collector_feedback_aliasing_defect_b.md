# Defect B — twelve DPU collectors share one feedback struct, and it defeats the starvation escape hatch

**Status:** PLAN, not yet implemented. **Blocks:** command-plane **S6** (the plan states this explicitly:
*"⚠ PREREQUISITE: fix B (per-collector feedback) BEFORE recording any S6 number"*).
**Code baseline:** citus `a551bcf6e`.
**All line numbers below are `src/backend/distributed/utils/homer/tuple_sink_service_process.c` unless stated,
and were re-derived from the tree at this SHA — the numbers in
[`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
§0b are STALE (they predate several thousand inserted lines).**

---

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
