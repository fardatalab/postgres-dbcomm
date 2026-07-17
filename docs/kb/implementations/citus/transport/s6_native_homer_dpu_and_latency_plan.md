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

**In flight at plan-record time:** a codex-worker draft of this section was started, then STOPPED pending this
record + a conversation compaction. After compaction, (re)implement against THIS section; verify no stray
worker edits remain in the citus tree first.

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
