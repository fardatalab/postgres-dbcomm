# S6 — native `--homer-dpu` bring-up + per-command latency attribution

<!-- kb-summary: The two-track S6 plan — (A) fold native `--homer-dpu` onto the selected-DPU command path (no host service; fail-closed via a pulled-forward S7.1 gut), and (B) bound the DPU discovery latency with a same-host-clock discovery-echo marker (the causal A/B was confounded). Converged after three refutation passes. -->

> ## ⚠ STATUS — DESIGN, converged after THREE refutation passes (2026-07-17)
>
> Grounded at **citus `7e08343f2`**. Three independent codex refutations drove the design (§6 records all three,
> both sides of every correction). **The design has converged and is APPROVED FOR IMPLEMENTATION (owner, 2026-07-17).**
>
> **Owner decisions (2026-07-17):**
> - Track A closes the node-B host-spawn fallback **fail-closed by gutting the peer arm to a `FATAL`** — which
>   IS S7.1's gut-first step, pulled forward. No peer-ABI change (§1.4).
> - Track B abandons the causal A/B (confounded) for a **same-host-clock discovery-echo marker** (§2).
>
> **Parent:** [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md) §S6 (`~L2880`).
> Track A gated behind Stage 3 (✅). Track B can start now on the existing `--homer-dpu-command` gate.

---

## 0. The recon finding that reframes everything (VERIFIED)

**The synchronous START round trip is ALREADY GONE — P-START is implemented.** Selected START is fire-and-forget
(`homer_client.c:6556`; the submitter returns on `fireAndForget` at `:2741` without waiting). The 235 µs wait no
longer exists; the future-direction doc's "PLANNED" was stale (corrected). ⇒ Track B is *"does the DPU discovery
cadence dominate?"*, not "instrument the round trip."

---

## 1. TRACK A — native `--homer-dpu` bring-up (CLI consolidation + fail-closed gut)

### 1.1 The change — fold `--homer-dpu-command` into `--homer-dpu`, at TWO sites

Introduce `homer_dpu_selected_command = homer_dpu_mode || homer_dpu_command_mode`, used at the **only two**
command-selection sites (both passes VERIFIED): the host-control mapping guard `pgbench.c:9361` and the
selected-vs-host opener `pgbench.c:9825`. Keep `--homer-dpu-command` as a deprecated alias one cycle, then
delete.

### 1.2 Why only two sites (correcting the first draft)

Result-drain (`pgbench.c:3938`/`:4007`) and close (`:9738`) are **not** flag forks — they cascade from the
session's `commandDpuStreamOpen`, which the selected opener sets (`homer_client.c:3474`). VERIFIED (both passes):
the whole client cascade — START (`:6390`), completion (`:6702`/`:6728`), ACK (`:6936`/`:6960`), wait
(`:7084`), result drain (`:5095`), close (`:3591`) — keys on that session state, so flipping the opener
propagates. Role 7 survives (`:9015`, `:5095`); basebackup is untouched (separate openers/state,
`homer_client.c:4134`).

### 1.3 Semantic cleanup the fold requires (from both passes)

The diagnostic (`pgbench.c:9842`/`:9850`), stale comments (`:327`/`:336`), help text (`:1236`), duplicated
validation (`:8963`/`:8985`), transport reporting (`:8094`), the gate invocation
`tools/farnet_validation/remote/gate.sh:6`, **and — the third pass's catch — the validator's hard-coded
transport matcher `tools/farnet_validation/checks.py:97`** (`gate_check` matches the literal
`homer-dpu-command (implies dpu result relay)`; without updating it, the native `--homer-dpu` gate is
INCONCLUSIVE even when the code path is correct). Update the checker + its tests alongside `gate.sh`.

### 1.4 The node-B host-spawn fallback — fail-closed by pulling S7.1's gut forward

**VERIFIED (first pass):** the pgbench fold does not reach node B's spawn choice.
`TupleSinkServiceHandlePeerOpenCommandSessionRequest` independently checks
`TupleSinkServiceDpuBackendSpawnArmActive()` (`tuple_sink_service_process.c:22476`); if false it falls back to
the host-SHM spawn `TupleSinkServiceSubmitBackendSpawnRequest` (`:44613`, `HOST_SERVICE_SHM` + `shm_open`
`:22940`). So "no host service anywhere" was too strong.

**Why NOT "defer + prove per-run" (the third pass killed that option):**
- The S3.5 deprecation LOG is a **one-shot process-static** (`hostSpawnDeprecationLogged`, `:22930`): if any
  earlier host spawn consumed the one shot, a later S6 fallback runs `shm_open` with **no new log line** — so
  "no S3.5 LOG this run" does NOT prove no fallback.
- `/proc/PID/exe` **cannot distinguish** the arms (both launch a postgres backend; the real distinction is
  `backendChannelMode` from `arenaSlotIndex`, `:22444`).

**⇒ Fail-closed the cheap way (owner decision): gut the PEER host-spawn arm to a `FATAL`.** Replace the `else`
host arm at `:44613` (peer OPEN) and the peer START host fallback at `:44809` with
`elog(FATAL, "host-service peer backend spawn is retired (S7.1, pulled forward for S6); DPU spawn arm must be
active")`. This **is** S7.1's gut-first step on those two call sites, pulled forward — no peer-ABI change, no
throwaway. The healthy gate (DPU arm active) never reaches it; if it does, it is loud.

**Scope:** the two PEER (cross-node) arms only. Leave the LOCAL host arms (`:40224`, `:44244` = plain
`--homer`, removed wholesale by S7.1).

**The positive per-run proof stays the gate's structured `dpu_spawn` events** (NOT the S3.5 LOG): `gate_check`
(`checks.py:103-129`) requires correlated `dpu_spawn begin/complete` (from `HomerServiceDpuSpawnBegin`,
`homer_service_dpu_spawn.c:246`/`:552`) and passes only with DPU-spawn evidence. The gut *prevents* the silent
fallback; the events *positively prove* the DPU arm ran.

**Decided — `FATAL` (owner, 2026-07-17)**, over an error-response: the gut is a should-never-fire tripwire, so
maximal loudness matches S7.1's pattern. Consequence accepted: node B's DPU service is single-threaded, so if
the tripwire ever fires it crashes the whole service (not just this OPEN) — the intended blast radius for a
"must not happen" guard.
**Startup-race check (implementation):** confirm the gate attaches the frontend-agent doorbell BEFORE pgbench
sends OPENs, so the tripwire does not fire on a cold-start race.

### 1.5 The gate + `-c 2` handoff

`pgbench --homer --homer-dpu -c 1` cross-node: 200 tx, 0 failures, sane `abalance`, and the `dpu_spawn` proof
(§1.4). `-c 2` is blocked by the singleton `engine->tupleSourceRing` (`homer_service_dpu_dma.c:844`, read from
the singleton at `:13812`) → byte-ring pool Stage 2. **S6 proper stops at `-c 1`.**

---

## 2. TRACK B — bound the DPU discovery latency (D) with a same-host-clock echo marker

### 2.1 The primary instrument — a discovery-echo marker (both passes converged; the causal A/B was confounded)

**You cannot compute an absolute D** (host-publish and DPU-accept are different clocks). The first correction
proposed a causal A/B (vary grouped-scan cadence, compare host M1); **the third pass proved that confounded and
it is DROPPED:**
- No isolated grouped-cadence knob exists — only global machine-baseline grant budgets
  (`tuple_sink_service_process.c:14204`); grouped-read shares the task-slot pool + PE-drain with command-pull
  (`homer_service_dpu_dma.c:12719`/`:5831`), so a reserved grant removes admission displacement but not physical
  contention.
- **The killer (VERIFIED `:8641-8648`):** the grouped scan is **multi-ring** — it discovers role-1 command AND
  role-5 SQL-result AND DPU→host payload/credit publish lines. Since M1 ends at role-7 result-visibility, a
  cadence change confounds **command discovery** with **result-credit discovery**. A single scan touching many
  rings cannot be isolated by grant accounting.

**⇒ The clean instrument: a default-off discovery-echo marker.** At **fresh role-1 acceptance** on the DPU
(`homer_service_dpu_dma.c:14130`), echo the accepted publication epoch back to host A on a **separate diagnostic
line** (NOT the completion slot — that would corrupt the live completion protocol). The host retains its role-1
publication timestamp (`homer_client.c:2736`) and timestamps the echo observation — **both on the host clock:**

> **`H_echo − H_publish = D + echo_return_overhead`** — a tight, same-host-clock UPPER BOUND on D, with **zero
> scheduler perturbation** and **no multi-ring confound.** Subtract a **marker-only baseline** (echo with no
> command in flight) to characterize `echo_return_overhead`; a large residual is defensible evidence that
> discovery dominates.

### 2.2 M1 — the host-A user-visible total (endpoints + stratification + power)

- Endpoints corrected: start at role-1 **publication** (`homer_client.c:2730`/`:2736`), end at
  **result-visible + ACK** (role-7 drain-to-EOS `pgbench.c:4057` + ACK `:4428`, not role-6 first-visible).
- **Stratify by command kind + result mode** — M1 mixes result-draining and non-result commands
  (`pgbench.c:4057` vs `:4148`); do not pool them.
- ⚠ **A null M1 cannot refute dominance without an equivalence margin + confidence interval + power** (third
  pass). Track B can *confirm* a large discovery effect; *refuting* dominance needs the statistical design
  (predefined margin, interleaved A/B, per-stratum CIs). M1 is the right user-visible outcome, but the marker
  (§2.1) is the primary D instrument precisely because it doesn't depend on that power argument.

### 2.3 Supporting DPU-A-local spans (corroboration, same-clock)

On the existing `HomerDpuDmaMonotonicNowNs` (`:6721`, file-local — expose it): grouped-read submit→accept
(`:12842`→`:14130`) and command-pull submit→accept (`:13011`→`:14256`), submit timestamp stored in
**task/ring-owned state** (`:12747`). **Correlation key (STRENGTHENED, third pass):** the draft key
`(bridgeGeneration, import, ring, acceptedPublishedEpoch, commandOrdinal)` is **not unique** — ring tenancy
reset zeroes the runtime and can repeat `(epoch, commandOrdinal)` (`:4961`). Add **`ringGeneration` +
`tenancyGeneration`** (production response ownership uses the full identity, `tuple_sink_service_process.c:41114`).
`bridgeGeneration` is collision-resistant, not unique (`homer_client.c:3170`) — fine for a bounded S6 run, not a
durable key. Report the censoring envelope `0 ≤ D < fresh_accept − prev_stale_submit`, not a point estimate.

### 2.4 Instrumentation shape & venue

Default-off compile macros (like Stage 3's `HOMER_SERVICE_PEER_TRANSPORT_STATS`), allocation-free, bounded; the
echo line is a **new default-off diagnostic export field**; reuse the `calls_in_phase`/`ms_in_phase` discipline.
**Diagnostic, not validation-bearing `HOMER_EVENT`s** — no CONTRACTS stable-event additions. PTP only if an
exact one-way D is ever required (not for the upper-bound marker). **Run first on the existing
`--homer-dpu-command` gate.**

### 2.5 Stage B.1 — DPU-local spans: the concrete implementation (BUILD THIS FIRST)

> **Owner decision (2026-07-17): STAGE it.** Implement these DPU-local spans first — no bridge/ABI change,
> default-off — and escalate to the echo marker (§2.1, which costs a diag bridge line + a two-DPU redeploy under
> rule 10) **only if the local censoring bound is inconclusive.** The local spans cannot *pin* D (the DPU does
> not know when the host published relative to its DMA sample), but they can often *bound* it hard enough to
> answer "does discovery dominate," at zero ABI cost — the "cheap external bound first" lesson (`rping`).

**Precise discovery point:** for a role-1 command, D ends at the `discoveredReady` transition
(`homer_service_dpu_dma.c:14191`, the `else if (!DpuToHostByteRing && !discoveredReady)` arm) — NOT the generic
epoch-stamp at `:14130` (which fires for every ring on the snapshot). `:14191` is where the DPU has noticed and
enqueued THIS command.

**All behind a new default-off macro (e.g. `HOMER_DPU_DISCOVERY_SPANS`):** OFF ⇒ compiled out, zero overhead;
ON ⇒ allocation-free static aggregates, bounded histograms, no extra DMA / no scheduler perturbation; clock =
`HomerDpuDmaMonotonicNowNs` (`:6721`). Keyed per `(importIndex, ringIndex)`, primarily role-1:

1. **Grouped-read submit→accept** — stamp `submitNs` into the **task owner** (identity at `:12747`) at submit
   (`:12842`); on FRESH accept (`:14130`/`:14191`) record `now − submitNs`. Read submitNs from the SAME
   task/ring owner (a global would mis-pair a reused slot).
2. **Per-ring submit INTERVAL (cadence)** — `now − lastSuccessfulSubmitNs` at each successful submit.
3. **Censoring bound `0 ≤ D < fresh_accept − prev_stale_SUBMIT`** — per ring, on STALE accept (`:14045`) set
   `prevStaleSubmitNs = taskOwner.submitNs`; on FRESH accept record `now − prevStaleSubmitNs`, then invalidate.
   ⚠ **`prev_stale_SUBMIT`, not `prev_stale_accept`** — the accept edge is unsafe (the host may publish after
   the stale DMA sampled memory but before its callback ran; that would shrink the bound below the true D).
4. **Command-pull submit→accept** (secondary) — `:13011` submit → `:14256` accept.

Aggregate per span: count / total / max / min + a power-of-two ns histogram (~24 buckets), static/file-local.
Dump (histograms + stale/fresh counts, labeled per ring/role/span) at DPU service shutdown (teardown path in
`tuple_sink_service_process.c`) and/or an env trigger.

**Measure:** default-off keeps perf builds clean. To read D, build **node A's DPU service** (the client's local
DPU — farnet0 DPU — which discovers the client's role-1 publication) with the macro ON, run the existing
`--homer-dpu-command` gate, read the shutdown dump. A large `submit_interval` / `submit→accept`, or a large
`censoring_bound_D` relative to per-command time ⇒ discovery dominates ⇒ the DPU poll cadence is the target (and
the echo marker §2.1 can pin it if the bound is not tight enough to conclude).

### 2.5.1 Grounding + the anchor correction (VERIFIED against citus `c0b9f06bd`, 2026-07-17)

Before implementing, every §2.5 site was re-read in the real tree. Two provenance facts first:
- The real path carries a `utils/` segment: `src/backend/distributed/utils/homer/homer_service_dpu_dma.c`.
- **`homer_service_dpu_dma.c` is UNCHANGED** from the plan's grounding SHA `7e08343f2` to HEAD `c0b9f06bd`
  (Stage 3, "replace broad send-CQ polling with exact demand"), so every DMA-file line below is current.
  **`tuple_sink_service_process.c` was REWRITTEN by Stage 3 (723 ins / 711 del)** — its line numbers drifted;
  that matters for Track A (§1.4 fatal-gut sites must be re-found by symbol), not for these DMA-file spans.

**THE CORRECTION — anchor the per-command D at the fresh-epoch accept, NOT the `discoveredReady` latch.**
- *Drafted (§2.5 "precise discovery point"):* D ends at the `discoveredReady` transition
  (`homer_service_dpu_dma.c:14191`).
- *Refuted by grounding:* `:14191` is a ONE-SHOT LATCH — its guard is `else if (... && !ringRuntime->discoveredReady)`
  and it is reset only when the ready-ref is consumed (`HomerDpuDmaClearDiscoveredReady`, called from the
  command-pull accept at `:14332`; the reset itself sets `discoveredReady = false` at `:9339`). So it fires ~once
  per arming cycle; at the `-c1` scope of S6 proper (§1.5) that collapses to ~ONE D sample, not a distribution.
  This is the KB's own "never one-shot; sample repeatedly" rule applied to probe placement.
- *Corrected anchor:* the **fresh-epoch grouped-control accept** at `:14130`
  (`ringRuntime->acceptedPublishedEpoch = line->publishedEpoch;`), **filtered to role 1 =
  `HOMER_DPU_BRIDGE_DESCRIPTOR_ROLE_FRONTEND_CONTROL_SLOT`** (`homer_dpu_bridge_abi.h:108`). A new command advances
  the publication epoch, so this fires once PER COMMAND.
- *Load-bearing assumption, VERIFIED:* the command ring keeps getting grouped-read-polled every scheduler pass —
  the scan's submit (`:1717`) is gated ONLY by `!pollQuiesced` (`:1703`) and `!controlReadInFlight` (`:1713`),
  **not** by `discoveredReady`. So each command's arrival is genuinely caught by a fresh grouped-read accept.

**Verified sites (all in `homer_service_dpu_dma.c` unless noted):** clock `HomerDpuDmaMonotonicNowNs` `:6722`
(file-local, callable directly — no "expose" needed); grouped submit `HomerDpuDmaSubmitOneGroupedControlRead`
`:12621`, owner populated `:12747`, DMA submit success `:12842`; stale accept (epoch unchanged) `:14045`;
fresh-epoch consume `:14130`; command-pull submit `HomerDpuDmaSubmitOneCommandPull` success `:13011`; command-pull
accept `HomerDpuDmaAcceptCommandPullSlot` success `:14338`; dump venue `HomerDpuDmaDestroy` `:1367` (the sole clean
teardown, called from `tuple_sink_service_process.c:48846`). Owner struct `HomerDpuDmaTaskOwner:301`; ring runtime
`HomerDpuDmaRingRuntime:464`.

**Design decisions (improvised during grounding, recorded per KB discipline):**
1. **Aggregate storage — SUPERSEDED, now ENGINE-level (see §2.5.3).** The first cut put the four `HomerDpuDmaSpanStat`
   on per-ring `ringRuntime` and reasoned "the role-1 command ring is a per-session export, never arena-reset, so the D
   target is safe." **Validation refuted this** (§2.5.3): being *per-session* is exactly what gets the ring
   **full-freed at session close** (`HomerDpuDmaClearHostMmapImport` does `free(import->ringRuntime)` + memset,
   `:9669`), a STRONGER teardown than the arena tenancy reset I guarded against, and it runs BEFORE the shutdown dump —
   so the dump was always empty. Aggregates now live on `HomerDpuDmaEngine.engineSpans[role][kind]` (outlives reclaim);
   only the transient pairing scratch (`spanLastSubmitNs`, `spanPrevStaleSubmitNs`, per-task `submitNs`) stays per-ring.
2. **`submitNs` on `slot->owner`** (per-task, `HomerDpuDmaTaskOwner`), NOT a per-ring/global scalar — the grouped read
   is single-ring (`:12763`) and the slot travels submit→completion→accept, so a reused slot cannot mis-pair.
3. **Censoring bound recorded ONLY when `spanPrevStaleSubmitNs != 0`** (an empty poll was observed since the last
   fresh accept). Back-to-back commands with no intervening empty poll are interval-censored and skipped — there is no
   "last known empty" witness, so no honest bound.
4. **Macro `HOMER_DPU_DISCOVERY_SPANS`, default 0**, mirroring `HOMER_SERVICE_PEER_TRANSPORT_STATS`
   (`remote_execution_peer_transport_rdma.c:429`: `#ifndef/#define …0`, `#if …` guards, enabled by a build `-D`).
   OFF ⇒ compiled out. Dump runs in `HomerDpuDmaDestroy` before `HomerDpuDmaFreeStage5Scaffolding`, on BOTH the clean
   and the Scenario-E leak-abandon (`:1408` early return) paths — `ringRuntime` is valid on both.

### 2.5.2 Adversarial review outcome (codex, 2026-07-17) — one real bug, two refinements

Reviewed the diff before any DPU build. **One load-bearing bug (both sides recorded):**

- **Drafted:** stamp `submitNs` on the success path AFTER `doca_task_submit_ex()` returns.
- **Refuted (the money finding):** DOCA DMA is **asynchronous** — the engine harvests completions later via
  `doca_pe_progress`, so the hardware can sample the host publish line AFTER the submit is armed but BEFORE the
  post-submit clock read. If the host publishes in that window, the stale read's recorded `submitNs` is LATER than
  the instant the DMA actually sampled "nothing there," so `fresh_accept − submitNs` can fall **BELOW the true D** —
  a censoring "upper bound" that is not one (a lying diagnostic). Same defect hit `grouped_submit_to_accept` and the
  command-pull span. Mechanism verified in-repo (not inherited): the whole engine is completion-harvested async.
- **Corrected:** stamp `submitNs` in program order **BEFORE** `doca_task_submit_ex()` (both submit hooks). The clock
  read then provably precedes the arm, which precedes any DMA sample, so `submitNs ≤ sample_instant` and the bound
  is a true upper bound (`D ≤ fresh_accept − submitNs`), with only small submit-call overhead as slack. The interval
  (item 2) now measures issue→issue between pre-submit reads; on submit failure the slot is retired (submitNs moot).

**Two refinements (no code change, interpretation only):**
- The per-command anchor is valid **for the serialized `-c1` role-1 producer** (STARTs are serialized and reserve one
  slot; the producer advances tail+epoch once per publish), NOT as a generic epoch property. Poll submission is
  gated by MORE than `!pollQuiesced && !controlReadInFlight` — also scheduler grant budget, active-import state, role
  enrollment, and backpressure. ⇒ `censoring_bound_D` is a valid upper bound but is **scheduler-contention-dependent**,
  not an isolated "pure discovery cadence." (Consistent with §2 killing the causal A/B; the bound is a bound.)
- Recording is done for **all enrolled roles** (identity-safe — no mis-pairing, VERIFIED), but only the **role-1
  (`FRONTEND_CONTROL_SLOT`) `censoring_bound_D`** is a command-discovery D bound. Other roles' "fresh epoch" means
  something else (a DPU→host payload ring's fresh line is the host consumed-head/backpressure, not command discovery).
  The dump labels role and points at role 1; interpret only that row for D.

**Confirmed safe (VERIFIED by review):** no wrong-slot/ring pairing (owner-carried identity, acceptance re-checks
kind/generation/ring); default-off compiles out; tenancy reset clears the added per-ring fields cleanly via the
whole-struct memset; the dump null-checks and skips detached imports.

### 2.5.3 Validation finding + fix — the empty dump → engine-level aggregates (2026-07-17)

First live measurement (node A DPU built with the macro, `-c1` `--homer-dpu-command` gate): **the gate PASSED
(`spawn_pairs=1`, 5/5 tx, 5 decoded `abalance`, no alarms) but the `[dpu-span]` dump was EMPTY** — header + footer,
zero rows. Root-caused, not patched blind:

- **Diagnosis — H1 (hooks DID fire), confirmed by codex + own verification.** Node A discovers the host's role-1
  command via the instrumented DMA path (`HomerFrontendDmaPublishControlSlotRequest` → node-A grouped-read
  `HomerDpuDmaSubmitOneGroupedControlRead`/`AcceptGroupedControlSnapshot` → command-pull) and only THEN relays to the
  peer. A command cannot be relayed unless it was DMA-staged, and staging IS the instrumented path — so samples were
  recorded. (Not an alternate COMCH/host-service receive path.)
- **The bug — VERIFIED in the node-A log + code.** The samples lived on **per-session `ringRuntime`**, which is
  **freed at session close**: `HomerDpuDmaReclaimDetachedImports` → `HomerDpuDmaClearHostMmapImport` does
  `free(import->ringRuntime); memset(import, 0, …)` (`homer_service_dpu_dma.c:9668-9670`), and the node-A log shows
  "reclaiming host-detached import" ~35 lines BEFORE the shutdown dump. The dump then skipped the emptied import slots.
  Same **lifetime-mismatch shape** as the review's timestamp bug — I assumed data-lifetime ≥ dump-time; at `-c1` every
  session is reclaimed first.
- **The fix — engine-level aggregates.** The four `HomerDpuDmaSpanStat` moved from `ringRuntime` to
  `HomerDpuDmaEngine.engineSpans[HOMER_DPU_SPAN_ROLE_SLOTS=9][HOMER_DPU_SPAN_KIND_COUNT=4]`, keyed by descriptor role ×
  span kind. The engine is `calloc`'d (zero-init) and freed only in `HomerDpuDmaDestroy` AFTER the dump, so it outlives
  every import reclaim. The three aggregate-recording hooks now write `engine->engineSpans[descriptor->descriptorRole]
  [kind]` (role bounds-checked); the pairing scratch stays per-ring. Single-threaded service ⇒ no atomics. Dump
  rewritten to iterate `[role][kind]`; line format changed `import=/ring=/role=` → `role=/span=`. All four DOCA×macro
  configs compile clean; net B.1 diff +250.
- **Interpretation consequence:** aggregates now sum **by role across the whole run** (all role-1 discovery samples
  together) — exactly the D distribution wanted. Per-ring granularity is gone, fine at `-c1`.

### 2.6 Track B measurement RESULT (validated, 2026-07-17)

**Validated run:** node A DPU built with `COPT="-DHOMER_DPU_DISCOVERY_SPANS=1"`, `-c1` `--homer-dpu-command` gate
(5 tx ≈ 40 role-1 commands), gate PASS (`spawn_pairs=1`, 5/5 tx, 0 failures, sane `abalance`), clean-rebuild
diagnostic-absent proven (rule 6). Node A `[dpu-span]` role=1 (FRONTEND_CONTROL_SLOT command ring):

| span | count | min | avg | max |
|---|---|---|---|---|
| `censoring_bound_D` (UPPER BOUND on D) | 40 | 14.8µs | **38.4µs** | 174.9µs |
| `grouped_submit_interval` (poll cadence) | 2080 | 9.1µs | 44.6µs | 22.2ms (idle outlier) |
| `grouped_submit_to_accept` (discovery DMA read) | 40 | 2.6µs | 10.6µs | 25.6µs |
| `command_pull_submit_to_accept` (body-fetch DMA) | 40 | 9.4µs | 17.5µs | 62.1µs |

(Role=7 DPU→host payload/credit ring also recorded: ~45µs cadence, ~38µs censoring bound over 6 samples.)

**Reading:**
- **D is cadence-limited at ~tens of µs.** The censoring UPPER BOUND on per-command discovery latency D averages
  **~38µs** (min ~15µs, max ~175µs); the poll cadence averages ~44µs (mode ~16–32µs). D ≈ cadence/2, as expected for
  closed-loop arrival — the DPU notices a freshly-published command within roughly one poll interval. 2080 grouped
  submits vs 40 fresh accepts ⇒ ~52 polls between commands, so the ring is polled far faster than commands arrive:
  **D is bounded by the poll interval, not by scan starvation.**
- **Discovery is the largest single DPU-INGRESS component, but NOT the dominant per-command latency.** DPU-side command
  ingress ≈ D(~38µs upper bound) + discovery read(~10µs) + command pull(~17µs) ≈ **~65µs upper bound** — real and
  measurable, but small against the full per-command path (cross-node round trip + backend spawn/exec/result-relay
  dominate; the `-c1` debug run was ~9.3ms/tx ≈ ~1.2ms/command, so discovery is low-single-digit % of the whole).
- **Lever if D reduction is ever wanted:** tighten the command-ring poll cadence (poll role-1 more often), trading DPU
  CPU/DMA bandwidth for a lower discovery floor. Not warranted by these numbers.

**RECOMMENDATION (pending owner call): the echo marker (§2.1) is NOT needed.** The local censoring bound is conclusive:
a true same-clock upper bound (post-review timestamp fix), consistent with the independently-measured cadence, placing
D firmly in the tens-of-µs, cadence-limited range — modest, not dominant. The bridge-ABI diag line + two-DPU redeploy
(rule 10) the echo marker costs is not justified. If accepted, **Track B is COMPLETE at the DPU-local-spans stage.**

---

## 3. Sequencing

1. **Track B first** on the existing `--homer-dpu-command` gate — the echo marker bounds D before any Track A
   change.
2. **Track A** in parallel: this design → implement (fold + fatal-gut + `checks.py`) → gate.
3. **`-c 2` / byte-ring Stage 2** follows (separate plan).

## 4. Doc hygiene

- [`remove_synchronous_start_round_trip.md`](../../../future-directions/citus/transport/remove_synchronous_start_round_trip.md):
  ✅ corrected to IMPLEMENTED.
- Fix when Track B lands: `homer_client.c:2585`/`:2589` (stale `HOMER_CLIENT_CONTROL_WAIT_TIMING` text).

## 5. Cross-links

- Parent S6: [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md) `~L2880`.
- Transport S6 rides: [`dpu_crossnode_command_completion_bridges_design.md`](./dpu_crossnode_command_completion_bridges_design.md).
- The node-B fallback the fatal-gut pulls forward: [`s7_host_service_retirement_plan.md`](./s7_host_service_retirement_plan.md) §3.2.
- `-c 2` blocker: [`dpu_byte_ring_pool_per_session_plan.md`](./dpu_byte_ring_pool_per_session_plan.md).

---

## 6. Refutation record — three passes (2026-07-17), both sides

Pass 1 + Pass 2 attacked the first draft independently; Pass 3 attacked the corrected design.

### Track A
- **Drafted:** opener flip makes native `--homer-dpu` structurally no-host-service; four forks change.
- **Refuted:** (P1+P2) result-drain/close are not flag forks — two sites, not four; the client cascade is
  total. (P1) node B falls back to host-SHM spawn (`:44613`) if its DPU arm is inactive. (P3) the "defer +
  prove per-run" fallback proof was **defective** — the S3.5 LOG is one-shot process state, `/proc/exe` can't
  distinguish arms; the real per-run proof is the structured `dpu_spawn` events (`checks.py:103`). (P3) the
  cleanup missed the validator's transport matcher (`checks.py:97`).
- **Corrected:** two-site fold + full cleanup incl. `checks.py`; the node-B fallback closed **fail-closed** by
  gutting the peer arm to a FATAL (S7.1 pulled forward), with `dpu_spawn` events as positive proof.

### Track B
- **Drafted:** `D = cadence/2 + scan→accept`, keyed by `(serviceSessionId, commandSequence)`.
- **Refuted:** (P1+P2) `cadence/2` category-wrong (grant-driven, multi-ring, closed-loop arrival);
  `scan→accept` conflates DMA + scheduler; correlation key unusable (no command identity at discovery);
  M1 endpoints misplaced.
- **First correction:** causal A/B (vary cadence, compare M1).
- **Refuted again (P3):** the A/B is **confounded** — no isolated cadence knob, shared task-slots/PE-drain, and
  the grouped scan is multi-ring (`:8641-8648`) so cadence confounds command with result-credit discovery; the
  correlation key is non-unique across tenancy resets (`:4961`); a null M1 can't refute dominance without a
  power argument.
- **Corrected:** the **discovery-echo marker** (§2.1) as primary — a same-host-clock upper bound on D with zero
  scheduler perturbation (both P1 and P3 independently proposed it); strengthened correlation key; stratified M1
  with an explicit equivalence/power caveat; local spans as corroboration.
