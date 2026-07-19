# S7 — host-service Homer retirement: detailed map and plan

<!-- kb-summary: The three-bucket (DELETE / KEEP / GUT-THE-ARM) map of the host-service Homer surface that S7 retires, verified against code, with per-sub-item checklists, boundary hazards, and the one open scoping decision. -->

> ## ⚠ STATUS — read first
>
> **SCOPING COMPLETE (2026-07-17).** Verified against code at **citus `7e08343f2` / postgres `af9c097ee9c`**
> (branch `homer-dpu-migration`). The detailed `file:line` lists below were enumerated by a read-only
> exploration pass and **structurally spot-verified** (the load-bearing/contradicting claims were read in code —
> see §2); **each individual site is still re-confirmed at implementation time.** A KB index is a pointer and a
> warning, never a substitute for reading the `file:line`.
>
> **⟳ UNBLOCKED 2026-07-17 — S6 has LANDED** (Track A fold + peer-arm gut + Stage 2, all committed; the §1 gate
> — "native `--homer-dpu` still uses the host arm" — is dissolved). **RE-GROUNDED at citus `3d5047146` / postgres
> `58af2f7c5a3`**: the `file:line` anchors below were scoped at the older `7e08343f2` and have DRIFTED — see the
> **"## ⟳ RE-GROUNDING 2026-07-17"** section immediately below for the current S7.1 map + FOUR material
> corrections.
>
> **S7.1(a) COMPLETE — VALIDATED PASS (2026-07-17).** Pgbench fail-closes plain `--homer`; every surviving Homer
> session opens selected-DPU and drains SQL results through role 7. Deleted from both repos: the client-side host
> control region, named-mailbox/direct-command, and host result-sink APIs/state (incl. the stable-binding prearm
> optimization). Shape: postgres pgbench.c +110/-540 (host arms + result-sink + stable-binding), basebackup_homer.c
> -12 (dead host control state); citus homer_client.c ~-3230 + remote_execution_client.h -180 (host
> command/control/mailbox/result-sink APIs + types/fields + the `-Wunused` direct-command / host-control-helper tail).
> **Executed as ONE folded gut+delete + ONE scoped validation round (owner decisions).** Validation: build
> (Citus→PG relink) CLEAN ⇒ no orphaned refs; selected-DPU gate PASS (transport line + DPU-spawn `HOMER_EVENT`
> begin/complete, `gate_check` 5tx/1spawn/5 decoded, 3×800/800 repeats, `peer_host_spawn_retired` absent);
> four-role basebackup PASS (44,362 laps, `published_tail==consumed_head` closure); positive retirement check =
> plain `--homer` exits 1 with the S7.1 fatal. Disclosed gap: `teardown_checks` INCONCLUSIVE — its
> stage2b/mirror/control/dma ledger families are absent for this *client-surface* workload (not a defect; the DMA
> teardown paths are untouched by (a), and alarm/frontier checks + tail-closure are clean). Env-repair provenance:
> the interrupted prior attempt left stale DPU services + shm, cleaned and re-preflighted clean before the candidate.
> S7.1(b-e) and S7.2+ remain unimplemented.
>
> **KEY VERIFIED FINDINGS (S7.1(a) — recorded so (b-e) don't re-derive them):**
> - **Plain `--homer` MUST be fail-closed at parse** (pgbench.c main option-validation, after the two existing
>   `--homer-dpu*` require-`--homer` checks): plain `--homer` was a valid host-service invocation, so deleting the
>   host arms without the fail-close would leave it hitting deleted code. The fail-close makes
>   `homer_mode ⟹ homer_dpu_selected_command ⟹ homer_dpu_result_relay` an INVARIANT — which is what makes the
>   arm-collapse provably safe. Contract: `CONTRACTS.md` "homer_mode / homer_dpu_selected_command / ...".
> - **Relay-vs-host field split (the entanglement trap):** the host result-sink + stable-binding machinery is INERT
>   on the relay path but shares per-command state with the KEPT relay drain. KEEP
>   `homer_pending_result_contract(_valid)`, `homer_pending_result_sink_bound`, `homer_pending_command_kind`/`sequence`,
>   `homer_pending_drained_rows`, `homer_last_abalance`; DELETE `homer_result_sink*`, `homer_stable_result_*`, and
>   (codex-flagged, main-agent-verified write-only-after-retirement) `homer_pending_result_mode` +
>   `homer_pending_drain_target`.
> - **Guards:** the 3 `session->control`-validity guards (Start/Peek/Try) collapse by dropping the
>   `session->control == NULL &&` conjunct → `... !session->commandDpuStreamOpen`; `HomerClientWaitCommandCompletion`
>   collapses to `if (session == NULL)`; `HomerClientAckCommandCompletion` never carried the conjunct.
>
> **✅ DECISION SETTLED (owner, 2026-07-17): DELETE the Homer COPY scaffolding** (§9, Option A). §3.4 is the
> final DELETE set. What settled it: the re-plumb reuses the DPU transport substrate/API (KEEP bucket, §4),
> while the COPY-specific glue we delete is written against the *host* transport S7 removes — so Option B would
> preserve code referencing deleted APIs, useless even as a template.
>
> **Parent plan:** [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md) §S7
> (`~L2896`). This doc expands that outline and **corrects two stale entries in it** (§2).

---

## ⟳ RE-GROUNDING 2026-07-17 (post-S6/Stage-2, at citus `3d5047146` / postgres `58af2f7c5a3`)

S6 (the S7 gate) has LANDED — Track A folded native `--homer-dpu` onto the selected-DPU path + gutted the two
PEER host-spawn arms, and Stage 2 (per-session SOURCE ring) closed the `-c2` blocker. Those commits rewrote
`pgbench.c`, `tuple_sink_service_process.c`, and `homer_service_dpu_dma.c`, so the scoping-era anchors (§3–§6,
scoped at `7e08343f2`) drifted. **The plan's STRUCTURE, buckets, and decisions are unchanged and sound** — this
section refreshes the S7.1 anchors and records FOUR material corrections. (S7.2/S7.3 anchors are re-confirmed
when we reach them, per §1a.) Provenance: a codex-explore read-only pass, spot-verified by the main agent (the
STARTUP_WAIT finding was read directly). Each site is still re-confirmed at implementation time (§1a).

**FOUR material corrections (more than a line shift):**

1. **§1/§5 discriminator RENAMED.** The gate is now `homer_dpu_selected_command = homer_dpu_mode ||
   homer_dpu_command_mode` (`pgbench.c:9031`); native `--homer-dpu` IS the selected-DPU path, so §5's
   `homer_dpu_command_mode` references are stale. Current pgbench forks: host-control-map guard
   `if (!homer_dpu_selected_command)` `pgbench.c:9377-9385` (close, guarded by `thread->homer_control_open`,
   `:9654-9657`); `openHomerSession` selected `:9841-9856` vs host `else if` `:9864-9879`; `finishHomerSession`
   selected `:9754-9760` vs host `:9762-9766`; `HomerDrainPendingResultSink` DPU `:3946-3947` vs host
   `:3949-3956`; `HomerApplyCommandCompletion` DPU `:4015-4077` vs host-SHM `:4078+` (host sink open `:4102-4117`,
   drain `:4137-4142`).

2. **§3.2 "four callers" is stale — the TWO PEER arms are ALREADY GUT (Track A).**
   `TupleSinkServiceSubmitBackendSpawnRequest` (def `tuple_sink_service_process.c:22937`) now has **two real
   LOCAL callers** — local command-session open `:40285-40287`, local START fallback `:44304-44307` — plus **two
   PEER fail-closed replacement arms**: peer-OPEN emits `peer_host_spawn_retired` + `exit(1)` at `:44713-44725`
   (former host call left commented `:44696-44711`; selected-DPU spawn is `TupleSinkServiceBeginDpuBackendSpawn`
   `:44623-44673`); peer-START `!backendLoopActive` arm at `:44935-44947` (former call commented `:44920-44933`).
   ⇒ **S7.1(b)'s gut-first step is DONE for the peer arms.** What REMAINS: gut+delete the two LOCAL callers,
   delete `SubmitBackendSpawnRequest` wholesale, and delete the peer fail-closed shells + their commented history.

3. **§3.3 pump — `LOCAL_CONTROL` is in TWO pump modes, not just MAIN_LOOP (wrong-neighbor trap).**
   `TUPLE_SINK_SERVICE_PUMP_LOCAL_CONTROL` (`:2807`) is in **both** `..._PUMP_MAIN_LOOP` (`:2816-2822`) **and**
   `..._PUMP_STARTUP_WAIT` (`:2824-2827`, used `:24419-24420`); `HEARTBEAT` (`:2812`) is main-loop-only. So
   dropping the host-control pump arm must handle the STARTUP_WAIT path too. ⚠ **CORRECTED (see the "S7.1(b–e)
   MAPPED" section above, finding 1): the control region is NOT host-role-private — `main()` maps and pumps it
   for BOTH roles.** It is merely FUNCTIONALLY inert on the DPU role (DPU commands flow via DMA-pulled staged
   slots, sharing only the dispatcher function). (c) is therefore a live-DPU-daemon scheduler edit, its own round.
   `TupleSinkServicePumpOnce` is now progress-policy/ready-set based (`:52280-52443`), collecting local-control
   whenever `controlState != NULL` (`:52323-52334`); ready-set collector arms local-control `:15858-15865`,
   heartbeat `:15937-15942`. Daemon: `TupleSinkServiceMapControlRegion` def `:25828`; `main` maps `:52672-52673`,
   pumps `:52727-52728`, unmaps `:52929`.

4. **§2 corrections STILL HOLD (re-verified).** (i) `SubmitBackendSpawnRequest` is pure-host — rejects the DPU
   arm `:22954-22961`; DPU spawn is `TupleSinkServiceBeginDpuBackendSpawn:22731`. (ii)
   `HomerClientOpenSqlResultReceiveStreamSelectedDpu` still has NO C/H source occurrence (only historical KB
   text); role-7 is folded into `HomerClientOpenSqlSessionSelectedDpu` (descriptor `homer_client.c:3354`, receive
   init `:3474-3490`) + `HomerClientPollSqlResultDpuReceive` `:5052-5065`.

**Refreshed S7.1 anchors — client host surface (§3.1):** `HomerClientOpenControl` def `homer_client.c:425`
(pgbench `:9379`); `HomerClientCloseControl` def `:514` (pgbench `:9656`; + guarded basebackup closes
`basebackup_homer.c:476`/`:518`); `HomerClientOpenSqlSession` def `:1572` (pgbench host `:9871`);
`HomerClientCloseSession` def `:1703` (pgbench host `:9762`); `HomerClientOpenBaseBackupStream` def `:2793` — no
source caller (dead, delete). **§6:** DPU slot translation still present — `HOMER_SERVICE_DPU_SPAWN_MAX_PENDING`
DPU-slot-based (`homer_service_dpu_spawn.h:95`), assignment starts at the DPU partition
(`homer_service_dpu_spawn.c:230`).

---

## ⟳ S7.1(b–e) MAPPED + ROUND STRUCTURE 2026-07-17 (codex-explore map, MAIN-AGENT VERIFIED in code)

Verified at citus `effd2678d` / postgres `108c918d80a` (after S7.1(a) landed). A codex-explore pass mapped
(b–e); the main agent then VERIFIED every load-bearing claim by reading the code (and caught TWO gaps the map
missed — findings 1 and 2). Result: **the rest of S7.1 is NOT one round.** Four findings overturn the earlier
scoping; the round plan below SUPERSEDES any "one folded pass for b–e" reading.

### The four verified findings

1. **(c) — the control region is NOT host-role-private (§3.3 / RE-GROUNDING-pt3 were wrong to imply "host-only
   ⇒ simple pump-arm gut").** `main()` maps `/citus_remote_execution_control_v27` UNCONDITIONALLY
   (`tuple_sink_service_process.c:52673`, AFTER the `dpuDmaState.enabled` branch), wires `controlState` into the
   peer request context (`:52676`) and progress registry (`:52714`), sets the global
   `ActiveReadyBitmapControlRegion` (`:25906`), and pumps `LOCAL_CONTROL` in BOTH `MAIN_LOOP` (`:52727`) and
   `STARTUP_WAIT` (`:2824-2827`). So the DPU service maps/pumps/registers it. **BUT it is FUNCTIONALLY INERT on
   the DPU role** (VERIFIED): DPU commands arrive by DMA-pulled staged slots and share only the dispatcher
   FUNCTION `TupleSinkServiceDispatchLocalControlSlot` (`:45000`), NOT the SHM region — the `:47727-47731`
   comment states this outright ("pulled by DMA, rather than through a host-service SHM control region"); the
   `ActiveReadyBitmapControlRegion` reads are stats (`:9458`) + guarded no-op clears (`:14979`). ⇒ (c) IS
   removable in S7.1, but it is a live-DPU-daemon scheduler edit (map/unmap, LOCAL_CONTROL in both masks,
   registry wiring, ready-bitmap global, region ABI, heartbeat which writes into the region) — KEEPING the shared
   dispatcher + DPU staged path. **Its own round (B); exact sites re-mapped at Round B execution.**

2. **(e) — `EnableExperimentalTupleSinkRouting` is REPURPOSED and now GATE-LOAD-BEARING (§1b/§3.4 "delete the
   GUC + backing var" is WRONG — the name says COPY, the meaning is "SQL result substrate is on").** The backend
   bridge FORCE-SETS it true so the socketless SQL backend materializes row results via the tuple-sink substrate
   (`remote_execution_backend_bridge.c:1383-1392` with the explaining comment, and `:2009`);
   `ExperimentalTupleSinkBatchTupleTarget` is likewise read by the bridge (`:1487`) and frontend
   (`homer_frontend.c:1371`). ⇒ KEEP both vars. Deletable in (e-COPY): the user-visible GUC REGISTRATION
   (`shared_library_init.c:2540`), the COPY error-hint/getter (`homer_tuple_queue_frontend.c:843`/`:995-1000`),
   the COPY frontend hooks in `multi_copy.c`, `homer_citus_xact.c` + its `transaction_management.c` call sites,
   and the backend-bridge COPY-ingest arm `case CITUS_REMOTE_EXEC_COMMAND_COPY_INGEST_FROM_TUPLE_SINK:`
   (`remote_execution_backend_bridge.c:3150-3163`) + `worker_tuple_sink_insert.c` — **keep the enum VALUE**
   (removing it renumbers the bridge ABI ⇒ Rule 10; an unhandled kind falls to the existing `default:` error).

3. **(e-API) — the `RemoteExecutionSession` frontend API is STILL CALLED by the S7.2 UDF**
   (`remote_exec_pgbench_transaction.c:114-117` → `StartRemoteExecutionCommand` +
   `WaitForRemoteExecutionCommandCompletion`). ⇒ its deletion (§3.4) CANNOT happen in (e); it MIGRATES INTO the
   7.2 round (round C), together with the UDF removal. Also KEEP the API DEFS in `homer_frontend.c` until then.

4. **(b)+(d) are coupled.** The host CAS §6 targets is INSIDE `TupleSinkServiceSubmitBackendSpawnRequest`
   (deleted wholesale by (b)); the host slot constants `HOST_SLOT_FIRST`/`HOST_SLOT_COUNT` become dead only once
   (b) removes their reader. ⇒ (d) is (b)'s tail; fold together. KEEP `DPU_SLOT_*`, all DPU translations, the
   32-slot ABI cardinality, and the range-agnostic postmaster scan until S7.3.

### ⚠ ROUND A OUTCOME — SPLIT into A' (LANDED) + deferred e-COPY (2026-07-18)

Round A was executed as one folded (b)+(d)+(e-COPY) deletion (citus, 20 files, −2967) and reviewed clean (self +
independent codex-explore: KEEP-SET, base-COPY-vs-pre-Homer-ancestor `2545ab065^`, orphan sweep — all clean).
**Validation FAILED**: a reproducible segfault in the DPU-spawned backend's SQL-result-WRITE path
(`TupleSinkCheckDirection`/`TupleSinkCheckBatchWritable`, `homer_tuple_queue_frontend.c:701`/`:761`, dereferencing
a GARBAGE tuple-sink handle) on the `SELECT abalance` statement, plus a *secondary* DPU
`memcpy task_kind=8` (= `HOMER_DPU_DMA_TASK_KIND_BACKEND_COMPLETION_CONTROL_READ`, role 3) I/O failure. The
four-role basebackup PASSED.

**Root cause — NOT a Round A logic defect (trace + bisect, VERIFIED).** A codex-explore trace refuted the
direct-cause theories: the deleted tx-finalization path never touched the backend's `RemoteExecBackendSessionState`
/result-sink (the handle is allocated in `TopMemoryContext`, survives `CommitTransactionCommand`); `task_kind=8`
reads host completion-control into a DPU-local buffer and cannot write backend heap, so the DPU failure is
secondary to the backend's death. The ONLY Round A change *inside* the crashing function is e-COPY's removal of
the dead COPY-only `RemoteExecCompletionPublishContext publishContext` STACK local from
`ExecuteRemoteExecBackendCommand` — semantically inert for SQL, but it **shifts the stack frame layout**, exposing
a **PRE-EXISTING latent stack overwrite / use-after-scope** in the SQL result path that S7.1(a) was merely lucky
with. **Bisect CONFIRMED (2026-07-18)**: reverting e-COPY (keeping only b+d) makes the gate PASS cleanly (7
invocations, 0 crashes, `gate_check` 20/20 decoded + `spawn_pairs=8`, 3×1200/1200 repeats ~71 tps, basebackup
44,362 laps + `frontier_checks` PASS); the crash reproduces only with e-COPY applied.

**Decision (owner-approved 2026-07-18): land Round A' = (b)+(d) now; DEFER e-COPY.**
- **Round A' = (b)+(d)** — host spawn claimant retirement + dead D7 host constants — **VALIDATED PASS 2026-07-18**.
  3-file citus diff: `remote_execution_backend_bridge.c` (backend channel-mode = `SELECTED_DPU_DMA`-only at both
  accept sites + collapsed guard), `tuple_sink_service_process.c` (`TupleSinkServiceSubmitBackendSpawnRequest`
  deleted wholesale + local OPEN/START fail-close), `remote_execution_backend_protocol.h` (`HOST_SLOT_*` removed,
  `DPU_SLOT_*` + 32-slot ABI + range-agnostic scan kept, D7' comment).
- **Round A'' = e-COPY + the latent-bug fix — VALIDATED PASS 2026-07-18, LANDED.** The latent bug was LOCALIZED
  (revert-bisect + codex-explore static analysis, mechanism verified in code) to an **uninitialized
  `RemoteExecSqlResultDestReceiver.currentBatchHandle`**: `InitRemoteExecSqlResultDestReceiver` `memset`s only up
  to `offsetof(..., resultQueueDescriptor)` (to skip clearing the ~33KB embedded contract), and neither Init nor
  `RemoteExecSqlDestStartup` sets `currentBatchHandle`; `RemoteExecSqlResultReserveBatch` skips reserving when it
  sees a non-NULL handle, so an uninitialized garbage pointer flowed into `TryAppendTupleViewToCitusTupleSinkBatch`
  → segfault in `TupleSinkCheckBatchWritable`/`Direction`. The deleted `publishContext` stack local had
  accidentally zeroed that frame region. **Fix:** explicit NULL-init of the receiver's whole trailing pointer tail
  in Init (`remote_execution_backend_bridge.c`; see the CONTRACTS entry
  `RemoteExecSqlResultDestReceiver.currentBatchHandle`). With the fix, full Round A (e-COPY, `publishContext`
  gone) PASSES: the exact `SELECT abalance` statement decodes 20/20, no segfault, gate + basebackup clean, ~71 tps
  (== the b+d-only run). ⇒ **S7.1(a)–(e) are COMPLETE except the (e-API) `RemoteExecutionSession` frontend API
  deletion, which stays coupled to S7.2 (Round C).** `teardown_checks` INCONCLUSIVE across all runs = pre-existing
  log-content gap, not a defect.

### Round structure (SUPERSEDES the "b–e in one pass" reading; owner-approved 2026-07-17)

- **Round A ⚠ SPLIT at validation → A' landed, e-COPY deferred (see "ROUND A OUTCOME" above).** Original scope was
  (b) + (d) + (e-COPY). Host spawn claimant (fn + 2 LOCAL callers `:40276`/`:44304`; KEEP the two
  peer fail-close guards `:44713`/`:44935`, drop only their commented history `:44696`/`:44920`; remove the host
  arm from `TupleSinkServiceFillBackendSpawnRequest` + the `HOST_SERVICE_SHM` acceptance/host-invalid-arena
  checks in `remote_execution_backend_bridge.c:3332-3374`, KEEP the enum type + `SELECTED_DPU_DMA` +
  range-agnostic scan) + dead host slot constants + adjacency-comment rewrite + COPY-only scaffolding per finding
  2. Validation: gate + 4-role basebackup.
- **Round B = (c) — VALIDATED PASS 2026-07-18 (gate 20/20 decoded, spawn_pairs=4, basebackup 44,363 laps, DPU
  setup listener live across the workload + peer reconnect, ~71-74 tps, no regression).** isolated (finding 1).
  Retires the host control-region protocol on BOTH ends together:
  the DAEMON side (`TupleSinkServiceMapControlRegion`/unmap, `LOCAL_CONTROL` in both pump masks, progress-registry
  `controlState` wiring, `ActiveReadyBitmapControlRegion` global, region ABI) AND the BACKEND side
  (`remote_execution_backend_bridge.c`: the now-dead `if (!selectedDpuBackendChannel)` `RemoteExecMapControlRegion`
  arm ~:2751, the dead completion-ready-bitmap validation ~:2710-2720, the `controlRegion`/`controlFileDescriptor`
  locals ~:2653-2656, the session-state assignment ~:2875 and cleanup ~:3179/:3221, plus
  `RemoteExecBackendMarkCompletionReady` + the completion-bitmap fields — these are the SAME ready-bitmap protocol
  as the daemon global, so they co-retire). ⚠ **Round A intentionally LEAVES the backend-side dead arm** (decision
  2026-07-18): it became unreachable as a side-effect of Round A's host-CHANNEL removal, but per §12 the
  control-region arms are (c)'s scope, and cleaning only the backend half would split one protocol across two
  commits. It is safe to leave one round (unreachable, commented; the selected-DPU path already tolerated
  `controlRegion==NULL` pre-Round-A). KEEP the shared dispatcher + DPU staged path + the Tier-2 named-mailbox
  branch. Validation: gate + basebackup; confirm the DPU setup listener stays live.
  **⟳ (c) MAPPED + VERIFIED 2026-07-18 (codex-explore map; KEEP-claims re-verified in code by main agent) — THREE
  scope refinements:** (i) **the control-region ABI is NOT wholesale-deletable** — `CitusRemoteExecControlSlot` +
  `CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION` are SHARED with the DPU DMA protocol (`homer_service_dpu_dma.c`
  sizes command descriptors from the slot + checks the version, `:2307`/`:2487`/`:6044+`) → **KEEP**; only the
  region AGGREGATE `CitusRemoteExecControlRegion`, the POSIX name `/citus_remote_execution_control_v27`, and the
  ready-bitmap line are deletable. (ii) **`ServiceHeartbeatState.servicePass` is the DPU-result pass clock**
  (`streamEntry->dpuResultPeerBoundServicePass` at `:11131`/`:30352`/`:42044`; machine-baseline collector) → KEEP
  the per-pass increment (`HomerServiceBeginProgressPass`); delete ONLY the heartbeat PUBLICATION
  (`HomerServicePublishHeartbeat`'s write to `controlRegion->heartbeatCounter`). (iii) **full region/name deletion
  is COUPLED to S7.3** (like e-API↔S7.2): after (c) removes the DAEMON servicing + the BACKEND dead arm,
  `CitusRemoteExecControlRegion` is still referenced only by the Tier-2 host-SHM frontend (`homer_frontend_shm.c/.h`
  + its `homer_frontend_control.c` open/START/POLL fallback arms). ⇒ **(c) SCOPE = daemon control-region servicing
  removal + backend dead-arm removal, KEEPING the region type/name + the Tier-2 host-SHM frontend; the
  aggregate/name/ready-bitmap-line + `homer_frontend_shm.c` DELETE moves to S7.3.** ⚠ CORRECTED (validated 2026-07-18): (c) ALSO
  removes `citus_remote_execution_control_v27` from the DAEMON binary — combined with (a) removing it from pgbench +
  libhomer_client, the runbook agreement-probe's three greps now expect EMPTY; the name persists ONLY in `citus.so`
  (Tier-2 host-SHM frontend) until S7.3. Runbook probe + shm-cleanup pattern (`citus_remote_exec_res_v*`) updated in
  the (c) commit. **Execution is NOT purely mechanical:** the
  DPU staged-command path reuses the local-control async scheduler machinery — GUT its SHM-owner arms (keep DPU/
  service-result owners; `TupleSinkServiceProgressLocalControlAsyncOp` already ignores `controlState`); and
  GUT-then-RESERVE (do NOT shrink) the completion-bitmap wire fields in the spawn ABI to avoid a bridge-ABI change
  ⇒ no DPU TCP smoke. RISK VERDICT (verified): clean bounded deletion, no hidden DPU dependency on the region /
  ready-bitmap / published heartbeat.
- **Round C = S7.2 ⏸ PAUSED (owner, 2026-07-18) — pending the UNIFICATION design.** Original scope: UDF removal +
  (e-API) frontend API removal (finding 3). **Round-C map completed and VERIFIED in code (citus `e9dd4676c`):**
  the dead surface is LARGER than "UDF + 6 API funcs" — the UDF is the sole external caller of the *whole* ①
  frontend API surface (the 6 command funcs + result/recv/borrow APIs `homer_frontend.c:852`/`:1492`/`:1523`/
  `:1563` + the 5 command-spec initializers in `homer_citus_policy.c`), AND e-COPY left **dormant COPY-sender
  residue** in `homer_frontend.c` (`TryReserveRemoteSendBatch:1172`, statics `WaitForRemoteSendBatchReserve:999`
  / `RemoteExecutionSessionCurrentCopyCommandFailed:965`, `FlushRemoteSendStream`, …) with zero callers outside
  the file. A literal "delete UDF + 6 funcs" would NOT compile (the residue still calls
  `PollRemoteExecutionCommandCompletion`). **DROP FUNCTION decision (owner): source-only, no explicit DROP**
  (remove the `#include` from `sql/citus--13.1-1--13.2-1.sql:5` + delete `latest.sql`; the rig reinstalls).
  **Why paused:** deleting the `RemoteExecutionSession` API would discard the intent-driven design the future
  backend COPY re-plumb needs. Instead we PROMOTE ① into a unified frontend-safe core — see
  [`../../../future-directions/citus/transport/homer_unified_session_core_plan.md`](../../../future-directions/citus/transport/homer_unified_session_core_plan.md).
  S7.2 resumes only after that design settles. ⚠ **CORRECTED (2nd review, 2026-07-18): S7.3 is NOT independent** —
  ①'s command session opens through Tier-2 (`HomerFrontendDmaOpenCommandSession`), so removing Tier-2 breaks the
  UDF. Under the owner's "keep ① live as reference until the core lands" decision, **S7.2 + S7.3 + the D7′
  all-slots assertion all BUNDLE into the FINAL retirement stage of the unification track (Stage 4)**, after the
  core is built. Sequencing + rationale: unification doc §11.3.
  - **UPDATE 2026-07-18 — EXECUTION STARTED.** The unification design is settled and the owner chose to BUILD
    THE CORE NOW (Stages 1–4; Stage 5/COPY deferred). So Round C = S7.2 is no longer just "paused pending
    design"; it lands as **unification Stage 4** once the core is built. **Stage 1 (frontend-safe intent-spec
    header split — `homer_session_spec.h`) is implemented and under validation.** Track progress in the
    unification doc §11.3/§11.7.
- **Round D = diagnostic-gate cleanup.** Validation: gate.

**KEEP-SET that MUST survive Round A** (verified load-bearing): `EnableExperimentalTupleSinkRouting` +
`ExperimentalTupleSinkBatchTupleTarget` vars (gate SQL result substrate); the `RemoteExecutionSession` API DEFS
(7.2 UDF); the `CITUS_REMOTE_EXEC_COMMAND_COPY_INGEST_FROM_TUPLE_SINK` enum VALUE; the two peer fail-close
guards; `TupleSinkServiceDispatchLocalControlSlot` + the DPU staged path; base Citus COPY
(`multi_copy.c`/`local_multi_copy.c` core machinery); the client-SQL branch of
`TupleSinkServiceConsumeCompletionMailbox` (`:23363-23375`); `HomerServicePersistentSendCompletionCallbacks`
(the F7 neighbor of the deleted `TupleSinkServicePublishPeerCommandCompletion`).

---

## 0. What "host-service" means here — the classifier (read before anything else)

Two facts decide every bucket assignment below. Get either wrong and you delete a live neighbor.

1. **"Host-service" is a ROLE, not a binary.** The daemon is `tuple_sink_service_process.c`'s `main()`, and
   **the same executable is the DPU service.** It selects role by environment
   (`HOMER_SERVICE_ENABLE_DPU_DMA=1 HOMER_SERVICE_ENABLE_DOCA_DMA=1`, `tuple_sink_service_process.c:28138`):
   the **host role** is the `dpuDmaEngine == NULL` branch (`:18566`, `:31878`); the **DPU role** has the engine.
   ⇒ S7 deletes the **host role's code paths + the host control region + the host invocation**. The **binary,
   `main()`, and every DPU-role path STAY** (they are the DPU service — §4).

2. **The COPY surface is split by PROVENANCE, not by filename.** Homer did not rewrite Citus COPY; it *branched*
   off it (`if (EnableExperimentalTupleSinkRouting) …`). So the cut is literally a diff against the pre-Homer
   tree: **Homer-authored code (ours) → DELETE; pre-Homer Citus code → KEEP untouched.** Classifier = `git log
   --diff-filter=A` / `git blame` + the `/homer/` path convention. Worked example (VERIFIED):
   `worker_tuple_sink_insert.c` lives under `.../worker/homer/` and was added by us in `a0c26ccfd`
   (2026-04-01) — **ours, DELETE**; `multi_copy.c` / `local_multi_copy.c` / `shared_library_init.c` are
   original Citus files whose base COPY machinery stays, with only our *hooks* removed.

---

## 1. Ordering and prerequisites

- **S6 MUST precede S7 (VERIFIED).** At HEAD, plain `--homer-dpu` sets result-relay mode but **still maps the
  host control region and opens `HomerClientOpenSqlSession`**; only `--homer-dpu-command` selects
  `HomerClientOpenSqlSessionSelectedDpu` (`pgbench.c:9825-9859`, discriminator `homer_dpu_command_mode`).
  Deleting the host session-open before S6 wires `--homer-dpu` onto the selected path **breaks `--homer-dpu`.**
- **S7.0 is already DONE** (postgres `22a8f9951e6`): the gate client no longer boots through the host control
  region, so the anti-fallback property is structural. The old "capture the `--homer` baseline first"
  precondition is **waived** (owner, 2026-07-13 — you cannot baseline a corpse).
- **Retirement pattern = gut-first, delete-later** (parent plan `~L2899`): replace each host arm's body with a
  loud `elog(FATAL, "host-service … is retired (S7.x); use the DPU path")`, keep the shell one cycle, then
  delete. A retired-but-present branch turns "a forgotten caller still needs this" into an attributable crash.
  **Made precise in §1a (the two-step + what the tripwire proves), and §1b (the dead-data sweep).**
- **Intra-stage ordering constraint (INFERRED, confirm at implementation):** delete the control-region
  *mapping* **only after** gutting the local-control/heartbeat pump arms. The daemon's internal control-state
  pointer is currently non-null across several scheduler interfaces; unmapping first would leave those reading
  a dead pointer. Gut the arms (§5 daemon), *then* drop the mapping.

---

## 1a. Retirement mechanics — the two-step made precise

**Per sub-item, in the §12 order — NOT one global gut-all** (the S6 gating and S7.2/S7.3 ordering forbid a
monolithic pass, and a `FATAL` in a monolithic pass is unattributable). For each sub-item's arms:

1. **Gut to `FATAL`, keep the shell** (function, signature, call sites). **Validate the FULL surviving workload
   set** — gate + 4-role basebackup + DPU TCP smoke + at least one frontend-agent-enabled run. Different host
   arms sit on different paths (spawn / client control-map / basebackup-close / backend command-map are four
   distinct workloads); one workload trips only its own. **No `FATAL` fires ⇒ no surviving path reached it.**
2. **Delete** the gutted arms, their now-dead functions, and their call sites; **re-validate the same set**
   (catches compile/link breakage and any path the deletion re-routed).

**⚠ What the tripwire proves — and what it does NOT.** "No `FATAL` fired" proves *the kept paths don't reach
this arm*, which **is** the retirement safety property (we have accepted the host-service workloads — plain
`--homer`, COPY — are gone and not returning). It is **not** proof of universal deadness — that over-read is
the recorded `a3cdd5f3c` error (*"the WARN never fired, so this deletion is a no-op"* — a necessity argument
counting only observed cases; see [`copy_path_revival_contract.md` §1](./copy_path_revival_contract.md)). Run
the tripwire against the **kept** paths; read its silence as exactly "the kept paths don't need this."

**The tripwire is for the GUT-THE-ARM bucket (§5) — the shared-function risk surface.** Genuinely-dead,
no-caller code (e.g. `HomerClientOpenBaseBackupStream`, §3.1) has nothing to trip it — **just delete it** in
step 2 without a FATAL cycle.

## 1b. Dead data — structs, members, module vars, enum arms (sweep AFTER the readers go)

The buckets are function/branch/call-site centric **on purpose: dead DATA is discovered, not enumerated
upfront.** A member looks alive until its *last reader* is deleted, and guessing the last reader is the
wrong-neighbor error this whole map exists to avoid. So **after each sub-item deletes its code, sweep for what
is now unreferenced** (`-Wunused-function`/`-variable`, and `git grep` the type/field/var names) and delete it
**in the same sub-item's step 2.** The compiler + grep are authoritative post-deletion; a pre-deletion list is
a guess.

**Seed list already surfaced** (delete when their readers go; verify unreferenced first):
- `HomerClientControl` struct + host-only fields — `remote_execution_client.h:32`/`:129`/`:241` (§3.1).
- GUC backing vars — `EnableExperimentalHomerDpuFrontend` (`homer_frontend_dma.c:67`, Tier-2/S7.3).
  ⚠ **`EnableExperimentalTupleSinkRouting` is NOT deletable** — REPURPOSED as the gate's SQL result substrate
  flag (see "S7.1(b–e) MAPPED" finding 2); only its user-visible GUC *registration* + COPY-only readers go.
- Tier-2 `dpuControlChannel` state — `homer_frontend_internal.h:23` (§3.6).
- The `HOST_SERVICE_SHM` spawn-mode enum **arm** (§5, `:22444`) — remove the arm; **keep the enum type**
  (native DPU uses its siblings).
- The host control-region ABI layout — `homer_shm_channel_abi.h` (`/citus_remote_execution_control_v27`, ver
  `34U`): deletable **wholesale** once §3.3's mapper is gone (no surviving reader).
- Host-only session-state members — identified at S7.1 deletion, not before.

**⚠ ABI vs private — the one place "just delete it" is wrong.** Private/module-level dead data deletes freely,
compiler-guided. **Shared-ABI structs do not:** the spawn region (`/citus_remote_exec_backend_spawn_v15`) and
the bridge protocol are read by the postmaster and the DPU, so shrinking a struct or **renumbering an enum**
there is a cross-component ABI change — **both DPUs rebuilt/redeployed** (CLAUDE.md rule 10) — and §6 already
fixes the 32-slot spawn cardinality as *kept*. Only the host control-region ABI (host-role-private) goes
wholesale.

---

## 2. Corrections to the parent migration plan found during scoping (VERIFIED in code)

Both were written at S3-era HEAD (~10 stages ago) and are now stale. **Strike them where stated when S7 lands.**

1. **`TupleSinkServiceSubmitBackendSpawnRequest` is now PURE HOST, not a two-armed function.** Its body
   *refuses* to run on the DPU arm (`tuple_sink_service_process.c:22906-22914`: *"refusing synchronous backend
   spawn on the DPU arm"*); DPU spawn is a **separate** `TupleSinkServiceBeginDpuBackendSpawn` (`:22684`,
   arm-active guard `TupleSinkServiceDpuBackendSpawnArmActive` `:22476`). ⇒ The plan's *"one function … the
   shm_open arm is an `else`"* (`~L2692`) is **wrong now**: S7.1 **deletes the function wholesale**, and the
   gut-the-arm is at the **call sites** (peer-OPEN `if/else` `:44562`/`:44613`, START host-fallback `:44243`).
2. **`HomerClientOpenSqlResultReceiveStreamSelectedDpu` does not exist** (git grep empty, both trees). Role-7
   result receive is folded into `HomerClientOpenSqlSessionSelectedDpu` (`homer_client.c:3354` descriptor,
   `:3474` init) + `HomerClientPollSqlResultDpuReceive` (`:5052`). The plan's MUST-NOT-touch entry (`~L2941`)
   names a stale symbol — replace with those folded portions (all KEEP, §4).

---

## 3. Bucket DELETE — host-service-only

### 3.1 Host pgbench client surface (`citus-dbcomm/src/bin/homer_client.c`, `postgres-citus/.../pgbench/pgbench.c`)
- `HomerClientOpenControl` — def `homer_client.c:424`, `shm_open` (no `O_CREAT`, maps only) `:435`; decl
  `remote_execution_client.h:432`; sole real opener `pgbench.c:9363`. (Basebackup has two *dead guarded* calls
  `basebackup_homer.c:474`/`:516` whose `control_open` field is never truly assigned — remove with the rest.)
- `HomerClientCloseControl` — def `homer_client.c:513`; pgbench caller `pgbench.c:9640`.
- `HomerClientOpenSqlSession` (host-SHM command session) — def `homer_client.c:1566`; decl `:437`; sole caller
  `pgbench.c:9855`. Its singly-rooted mailbox subtree is host-only: name/map helpers `:675`,
  `HomerClientOpenCompletionMailbox` `:824` (called only `:1686`), `HomerClientOpenBackendMailboxes` `:958`
  (called only `:1681`).
- `HomerClientCloseSession` — def `homer_client.c:1702`; host caller `pgbench.c:9746`.
- `HomerClientOpenBaseBackupStream` (legacy **host-control** opener, distinct from the SelectedDpu one) — def
  `homer_client.c:2786`; **no caller at HEAD** — dead, delete.
- After the arms go: remove `HomerClientControl` struct `remote_execution_client.h:32` and its host-only fields
  (`HomerClientSession.control` `:241`, `HomerClientBaseBackupStream.control` `:129`). **⚠ NOT mechanical
  (codex-flagged + main-agent-verified 2026-07-17 by reading the guards):** `session->control` is still read on the
  SELECTED-DPU-reachable path — as a validity CO-condition `(session->control == NULL && !session->commandDpuStreamOpen)`
  in the arg-guards of the shared command fns `HomerClientStartCommandWithCompletionFlags` (`homer_client.c:6381-6382`,
  and the gate calls it via `pgbench.c:4255`), `HomerClientPeekNextCompletionEvent` (`:6696-6697`),
  `HomerClientTryCommandCompletion` (`:7019-7020`). The real host/selected discriminant there is `commandDpuStreamOpen`,
  NOT `control` (each guard even keeps an "Original host-only guard" comment showing the pre-migration `control == NULL`
  form). So SEQUENCE within (a): first retire host session-creation so `control` is universally NULL, simplify those
  three guards (drop the `session->control == NULL &&` conjunct → just `!session->commandDpuStreamOpen`), and gut the
  §5 host subarms these fns carry, THEN delete the field/type. `HomerClientBaseBackupStream.control` is read only in the
  dead legacy close branch (`homer_client.c:5828`, deleted with the basebackup-close host arm §5).

### 3.2 Host spawn claimant
- `TupleSinkServiceSubmitBackendSpawnRequest` — **delete wholesale** (def `tuple_sink_service_process.c:22889`;
  host region open `:22940`, host-range CAS `:22990`, response/release loop `:23025`). Its four callers, all
  host arms, are gutted then removed: local command-session open `:40224`, local START fallback `:44244`,
  peer-OPEN host arm `:44613`, peer-START host fallback `:44809`.

### 3.3 Daemon host role — control region + host pump arms
- Control region `/citus_remote_execution_control_v27` (`homer_shm_channel_abi.h:25`; protocol ver `34U`
  carried inside, `homer_abi_version.h:48`). Mapper `TupleSinkServiceMapControlRegion`
  `tuple_sink_service_process.c:25775` (create/map/reset `:25787`/`:25848`); `main` calls it unconditionally
  `:52554`. **Gut** `main`: delete the control-map call `:52554`, the control-name env override `:52393`, the
  unmap `:52810`. **Gut** `TUPLE_SINK_SERVICE_PUMP_MAIN_LOOP` (`:2807`): drop
  `TUPLE_SINK_SERVICE_PUMP_LOCAL_CONTROL` + `..._HEARTBEAT`; keep peer/remote-client/completion/payload/DPU.
  **Gut** `TupleSinkServicePumpOnce` local-control/heartbeat prerequisites + collectors `:52181`/`:52194`/`:52204`.

### 3.4 Backend-to-backend COPY frontend — DELETE (§9 settled: Option A)
The deletable boundary is the **`RemoteExecutionSession` frontend API and its COPY/UDF callers**, *not* the
lower tuple-queue substrate (shared with selected-DPU result delivery + basebackup — KEEP, §4).
- Frontend API `homer_frontend.c`: `OpenRemoteExecutionSession` `:329`, `CloseRemoteExecutionSessionDataPlane`
  `:495`, `CloseRemoteExecutionSession` `:539`, `StartRemoteExecutionCommand` `:605`,
  `PollRemoteExecutionCommandCompletion` `:672`, `WaitForRemoteExecutionCommandCompletion` `:716`; decls
  `homer_frontend.h:449`.
- Non-UDF callers to remove (**our Homer code**): worker receive
  `worker/homer/worker_tuple_sink_insert.c:336`/`:418`/`:432`; COPY hooks in `multi_copy.c` (`:2698`…`:3383`,
  full list in the checklist); transaction cleanup `homer_citus_xact.c` (`:89`…`:417`).
- Enabling GUC `citus.enable_experimental_tuple_sink_routing` — reg `shared_library_init.c:2540`; backing var
  `homer_tuple_queue_frontend.c:45`; getter `:841`; policy wrapper `homer_citus_policy.c:639`.
- **KEEP the original Citus COPY** (`multi_copy.c`/`local_multi_copy.c` base machinery) — remove only our hooks.

### 3.5 S7.2 — the UDF `citus_remote_exec_pgbench_transaction`
- C: `PG_FUNCTION_INFO_V1` `worker/homer/remote_exec_pgbench_transaction.c:30`, def `:324`, + all file-local
  helpers (`:59`/`:101`/`:130`/`:151`/`:259`/`:290`). Extern `worker_protocol.h:61`.
- SQL: create `sql/udfs/citus_remote_exec_pgbench_transaction/latest.sql:1`; upgrade inclusion
  `sql/citus--13.1-1--13.2-1.sql:6`. **No source drop reference exists** — decide whether to add an explicit
  later-version `DROP FUNCTION` (source deletion alone does not clean already-installed catalogs — INFERRED).
- Build is via the `worker/homer` wildcard (`Makefile:36`); **do not `rm -r worker/homer`** — the COPY worker
  file lives there too.
- ✅ The **standing gate does not call this UDF** (VERIFIED: gate path is `sendHomerCommand` `pgbench.c:4508`
  → `HomerStartCommand` `:4204` → `HomerClientStartCommandWithCompletionFlags` `:4247`).

### 3.6 S7.3 — the Tier-2 DMA frontend
- GUC `citus.enable_experimental_homer_dpu_frontend` (⚠ confusable — see §7) — reg
  `shared_library_init.c:2597`; var `homer_frontend_dma.c:67`; getter `:184`; callers
  `homer_frontend_control.c:649`/`:804`.
- Tier-2 API roots `homer_frontend_dma.c` (`HomerFrontendDmaOpenCommandSession`, `…StartCommand`,
  `…PollCommandCompletion`, `…CloseControlChannel`, bridge/setup smokes) + all cross-file calls in
  `homer_frontend_control.c` (`:804`/`:1092`/`:1190`/`:1269`/`:1287`) and `homer_frontend.c:790`/`:585`; public
  decls `homer_frontend_dma.h:27`; old `dpuControlChannel` state `homer_frontend_internal.h:23`.
- **The last host-resident spawn claimant**: `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`
  (`homer_frontend_dma_lifecycle.c:188`) + its lifecycle mailbox API; production caller `homer_frontend_dma.c:1367`;
  smoke `homer_frontend_dma_smoke.c:118`. **⚠ This already VIOLATES D7** — it CASes **all** spawn slots
  (`:250`), not just the host range. **D7′ is not true until this is gone** (§6).
- Tier-2 smoke + build wiring: `homer_frontend_dma_smoke.c:131`; `citus-dbcomm/Makefile:49`/`:66`/`:197`/`:336`;
  extension object `src/backend/distributed/Makefile:49`.

---

## 4. Bucket KEEP — must-not-touch / shared (delete any of these and you break the gate or basebackup)

- **Selected-DPU / basebackup openers (the corrected MUST-NOT-touch set):**
  `HomerClientOpenSqlSessionSelectedDpu` (`homer_client.c:3061`, sets `session->control = NULL` `:3159`),
  `HomerClientOpenBaseBackupStreamSelectedDpu` (`:3765`, basebackup caller `basebackup_homer.c:337`),
  `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:4134`, caller `pg_basebackup.c:2418`), and the
  **folded role-7 receive** that replaces the stale symbol: `homer_client.c:3354`/`:3474` +
  `HomerClientPollSqlResultDpuReceive` `:5052`.
- **Shared DPU transport / dispatch / session-create:** `HomerClientDpuSubmitControlRequest`
  (`homer_client.c:2698` — SQL *and* basebackup), `TupleSinkServiceDispatchLocalControlSlot`
  (`tuple_sink_service_process.c:44872` — reached by DPU staged commands `:47718`),
  `TupleSinkServiceHandleOpenSession`/`…HandleStartCommand` (selected-DPU enters their common bodies),
  `TupleSinkServiceCreateSession` (`:26522` — shared across host/DPU/peer/basebackup; **blanket SHM deletion is
  NOT a valid boundary** — narrow only its host-only arms, §5).
- **DPU daemon + spawn infra:** `main`/DPU setup/loop (`:52328`…`:52811`) and its build target
  (`citus-dbcomm/Makefile:6`/`:58`/`:76`/`:312`); native DPU spawn `TupleSinkServiceBeginDpuBackendSpawn`
  (`:22684`) + arm-active (`:22476`); postmaster spawn region `/citus_remote_exec_backend_spawn_v15`
  (`remote_execution_backend_protocol.h:35`, mapped `remote_execution_backend_bridge.c:1746`, consumed
  `:3313`/`:3446`).
- **Native frontend-agent GUC `citus.enable_homer_dpu_frontend_agent`** (KEEP — the standing gate uses it, §7):
  reg `shared_library_init.c:2605`; gates arena/worker `remote_execution_backend_bridge.c:3495`; agent exports
  the spawn region `homer_frontend_agent.c:748`/`:765`/`:1497`.
- **`sessionUID` (S7.4 — KEEP, confirmed):** role-7 rings are host-exported across PCIe by SQL *and*
  basebackup, resolved by exact nonzero UID (`homer_service_dpu_dma.c:8233`, dup = error `:8344`). ABI fields
  `remote_execution_client.h:339`, `homer_control_abi.h:355`, `remote_execution_peer_control_protocol.h:149`/`:218`,
  `homer_dpu_bridge_abi.h:313`; mint `pgbench.c:9793`; propagation across the service (`:30916`, `:37068`,
  `:37247`, `:37854`).
- **`TupleSinkServicePublishPeerCommandCompletion`** (`:21766`, sole caller `:23324`): source-documented
  *dead at HEAD* (non-client b2b-COPY terminal publisher) but **not statically unreachable** — it goes with the
  COPY decision (§9), not on its own. Do **not** delete its neighbor
  `HomerServicePersistentSendCompletionCallbacks` (the F7 fix, RETIRE-ONLY, transport-wide — the classic
  wrong-neighbor).

---

## 5. Bucket GUT-THE-ARM — the risk surface (one function, delete only the host arm)

- **pgbench** (`pgbench.c`): `threadRun` — delete host control-map `if (!homer_dpu_command_mode)` `:9361` + its
  close `:9638`, keep the selected open loop `:9371`. `openHomerSession` — keep `if (homer_dpu_command_mode)`
  selected `:9825`, delete host guard/open `:9848`/`:9855` (**safe only after S6**). `finishHomerSession` —
  keep selected `:9738`, delete host close `:9746`. `HomerDrainPendingResultSink` — keep DPU `:3938`, delete
  host `:3941`/`:3947`. `HomerApplyCommandCompletion` — keep DPU `:4007`, delete host `:4070`/`:4103`/`:4108`.
- **Shared `homer_client.c`:** `HomerClientStartCommandWithCompletionFlags` — keep selected
  (`:6373`/`:6456`/`:6469`/`:6556`), delete host mailbox/reserve/submit (`:6440`/`:6460`/`:6588`).
  `HomerClientPeekNextCompletionEvent` — keep role-6 `:6728`, delete SHM arms `:6773`/`:6795`.
  `HomerClientAckCommandCompletion` — keep `:6936`, delete SHM `:6974`. `HomerClientWaitCommandCompletion` —
  keep `:7094`/`:7124`, delete host heartbeat `:7099`/`:7131`. `HomerClientCloseBaseBackupStreamInternal` —
  keep `if (stream->selectedDpu)` `:5729`, delete host-control close `:5828`.
- **Spawn call sites (service):** `TupleSinkServiceFillBackendSpawnRequest` — keep common `:22391` + selected
  `:22448`, delete the `arenaSlotIndex==INVALID ? HOST_SERVICE_SHM` `:22444` and the host caller `:22915`.
  `TupleSinkServiceHandlePeerOpenCommandSessionRequest` — keep the DPU arm `:44562`+`:44599`, delete the host
  `else` `:44613`. `TupleSinkServiceHandleStartCommand` — keep common `:44216`, delete only host spawn fallback
  `:44243`. `ProcessSpawnRequestSlot` (`remote_execution_backend_bridge.c`) — delete `HOST_SERVICE_SHM`
  acceptance `:3333` + host invalid-arena guard `:3368`, keep selected checks `:3342`/`:3352` and the
  range-agnostic scan `:3446`.
- **Backend command loop** `ExecuteRemoteExecBackendCommand` (`remote_execution_backend_bridge.c:2681`): keep
  the `SELECTED_DPU_DMA` enum + `arenaBackend` classification (`:2725`/`:2733`) and the native arena arms
  (`:2800`/`:2976`/`:3074`/…); delete host control mapping `:2783`, the named-mailbox `else` `:2858` (serves
  host **and** legacy Tier-2), and the legacy Tier-2 result arm `:2920`.
- **Daemon `TupleSinkServiceCreateSession`** (`:26522`): keep session creation; narrow only host-only resource
  arms — common mailbox `:26554`, client completion mailbox condition `:26564`, broad non-client peer
  completion-ring alloc `:26575` (helper `TupleSinkServiceCreatePeerCommandCompletionRing` `:22188`).
- **`TupleSinkServiceConsumeCompletionMailbox`:** keep the client-SQL branch `:23307`, delete the non-client
  terminal-publisher arm `:23324` (with §9).

---

## 6. D7 → D7′ dissolution (NOT a blind delete)

- Delete host CAS `tuple_sink_service_process.c:22990` and the host/DPU range constants + adjacency checks
  `remote_execution_backend_protocol.h:89`. Keep total ABI cardinality
  `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_COUNT` (`:37`) and the postmaster range-agnostic scan
  (`remote_execution_backend_bridge.c:3446`).
- **REWRITE (not delete)** the DPU-range translations in `homer_service_dpu_spawn.c:207` / `.h:95` and the
  ~16 sites in `homer_service_dpu_dma.c` (`:875`, `:1550`, `:7469`…`:14808`) — these encode "the DPU owns
  slots 16..31"; once the DPU is the sole claimant they either simplify to whole-array or must be re-based.
- **⛔ D7′ ("exactly one claimant") is not true until Tier-2's all-slot CAS
  (`homer_frontend_dma_lifecycle.c:250`) is also gone (S7.3).** Two host-resident claimants exist today; the
  D7 partition is scaffolding for exactly that window.
- **Open sub-decision (INFERRED, defer):** D7′ can use all 32 ABI slots, or retain a neutral 16-entry DPU
  pending capacity (`homer_service_dpu_spawn.h:95`). Not blocking; record when we reach S7.3.

---

## 7. The two confusable DPU-frontend GUCs — boundary hazard (RESOLVED)

Near-identical names, opposite buckets — exactly the wrong-neighbor shape:

| GUC | gates | bucket | evidence |
|---|---|---|---|
| `citus.enable_homer_dpu_frontend_agent` | native arena / frontend agent (the spawned backend becomes a Homer frontend) | **KEEP** | **the standing gate sets it** (runbook `:314`); gates `remote_execution_backend_bridge.c:3495` |
| `citus.enable_experimental_homer_dpu_frontend` | Tier-2 DMA frontend channel (old `citus_remote_exec_pgbench_transaction` UDF recipe) | **DELETE** (S7.3) | `homer_frontend_dma.c:184`; UDF recipe migration-plan `~L2763` |

**No hazard to the gate:** the gate depends on the KEEP GUC, never the DELETE one. *(This corrects the project
memory note that described the experimental/Tier-2 recipe as "the selected-DPU gate".)*

---

## 8. Boundary hazards — the one-line "do not cut this neighbor" list

1. **S6 first** — `--homer-dpu` still enters the host arm (`pgbench.c:9825-9859`).
2. **Same daemon, two roles** — deleting the binary removes the DPU service (`:52432-52579`).
3. **Confusable GUCs** — §7.
4. **Shared `SELECTED_DPU_DMA` enum** — denotes both legacy Tier-2 *and* native arena backends; discriminant is
   `arenaBackend` (`remote_execution_backend_bridge.c:2734`). Deleting enum acceptance breaks native DPU.
5. **Shared dispatcher / START handler** — `TupleSinkServiceDispatchLocalControlSlot` (`:47718`) and
   `…HandleStartCommand` common body (`:44216`) serve DPU staged commands.
6. **Shared basebackup carrier/close** — SQL + basebackup converge in `HomerClientBaseBackupStream`
   (`remote_execution_client.h:287`) and `HomerClientCloseBaseBackupStreamInternal` (`homer_client.c:5729`).
7. **Role-7 is host-exported RDMA, not host-service SHM** — keep it (`homer_client.c:3354`/`:4343`).
8. **Postmaster spawn region survives** — native agent attaches it (`homer_frontend_agent.c:748`).
9. **Session creation is shared** — narrow, do not blanket-delete (`tuple_sink_service_process.c:26522`).
10. **F7 callbacks ≠ COPY publisher** — keep `HomerServicePersistentSendCompletionCallbacks` (RETIRE-ONLY)
    while deleting `TupleSinkServicePublishPeerCommandCompletion` next door.

---

## 9. ⛔ OPEN DECISION — the Homer COPY scaffolding: delete, or leave dormant?

The COPY *feature* is broken at HEAD and slated for a future re-plumb onto the DPU plane. Our Homer COPY
scaffolding (§3.4: the `RemoteExecutionSession` frontend API, `worker_tuple_sink_insert.c`, the routing hooks +
GUC, `homer_citus_xact.c` cleanup, `TupleSinkServicePublishPeerCommandCompletion`) is **all ours (Homer),
host-service, and COPY-only** — touched by neither the DPU gate nor basebackup.

- **Option A — DELETE it (recommended, and what §3.4 is written to).** Consistent with "all host-service paths
  go"; the code is broken anyway; and the **re-plumb *surface* is already preserved in the KB**
  ([`copy_path_revival_contract.md` §2.5](./copy_path_revival_contract.md)) — so no design knowledge is lost,
  only dead code. The original Citus COPY (base `multi_copy.c`/`local_multi_copy.c`) stays; we remove only our
  hooks.
- **Option B — leave it dormant** (gut its host-service transport dependency to a `FATAL`, keep the scaffolding
  as reference for the re-plumb). Keeps our COPY code in-tree; costs carrying broken, host-coupled code through
  every future substrate change.

**DECISION: A — DELETE (owner-confirmed 2026-07-17).** Rationale: the KB contract is the durable spec for the
re-plumb; carrying dead host-coupled code is exactly the maintenance tax the resource-retirement audit kept
paying. **The point that made it airtight:** the scaffolding is written against the *host*
`RemoteExecutionSession` transport that S7 itself deletes, so Option B would preserve code referencing removed
APIs — useless as a working template. The re-plumb instead reuses the DPU transport substrate/API (all KEEP,
§4) plus the COPY-specific control-plane spec in [`copy_path_revival_contract.md` §2.5](./copy_path_revival_contract.md).

> **Note for the eventual re-plumb (owner, 2026-07-17):** COPY drives the byte-ring **coordinator→worker
> (ingest)**, the opposite direction from the SELECT gate's **worker→client (result)**. The substrate is
> direction-agnostic (basebackup proves bulk wrap), but the ingest-side session open + command handler are
> COPY-specific new work, not a reuse of the gate path.

---

## 10. CONTRACTS / KB updates S7 will require

- `CONTRACTS.md:57-62` (host services "retain both mapping arms"; "unflagged tuple/COPY streams require a host
  service") — **rewrite**: after S7 there is no host service; the DPU is the only role.
- `CONTRACTS.md:776` (`byteRingControl->flags/terminalStatus` "HOST-SERVICE-era") — becomes simply "removed".
- Strike the two stale parent-plan entries (§2) at `dpu_command_plane_migration_plan.md:~2692` and `~2941`.
- Replace D7 with **D7′** in the parent plan once §6's last-claimant is gone.
- Update the project memory note that conflates the two GUCs (§7).

---

## 11. Cross-links

- Parent: [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md) §S7, §D7/D7′, §S3.5.
- COPY re-plumb surface (the reason Option A loses no knowledge):
  [`copy_path_revival_contract.md`](./copy_path_revival_contract.md) §2.5.
- Resource-retirement audit (a *different* kind of retirement — lifetime fixes, complete):
  [`resource_retirement_contract_audit.md`](./resource_retirement_contract_audit.md).
- Live contracts touched: [`CONTRACTS.md`](./CONTRACTS.md).

---

## 12. Per-sub-item deletion checklist (feeds the gut-first → delete-later pattern)

- **S7.0** ✅ DONE (postgres `22a8f9951e6`).
- **S7.1** — gut then remove: **(a) client host surface §3.1 ✅ DONE — VALIDATED PASS 2026-07-17 (folded whole-surface incl. result-delivery; see STATUS block)**;
  (b) host spawn claimant §3.2 + its call-site arms
  §5 — ⟳ **the two PEER arms are ALREADY gut (Track A); only the two LOCAL callers + the peer-shell/commented
  deletion remain**; (c) daemon control region + host pump arms §3.3 (**after** gutting local-control/heartbeat,
  §1 — ⟳ note `LOCAL_CONTROL` is ALSO in `STARTUP_WAIT`, not just the main loop, and confirm it is host-only
  there); (d) D7 host CAS + range constants, rewrite DPU translations §6; (e) COPY scaffolding §3.4 **iff Option
  A** (§9). **Precondition: S6 green ✅ (landed 2026-07-17).** ⟳ All anchors + the discriminator rename are
  refreshed in the "RE-GROUNDING" section at the top.
- **S7.2** — UDF §3.5; decide on an explicit later-version `DROP FUNCTION`.
- **S7.3** — Tier-2 DMA frontend §3.6; this removes the **last** host-resident spawn claimant ⇒ **assert D7′**.
- **S7.4** — confirm `sessionUID` stays (§4); record the confirmation in code + CONTRACTS.

---

## 13. STAGE 4 EXECUTION PLAN — current-HEAD (citus `f6f9c1734` / postgres `53249768c70`), verified 2026-07-19

The parent plan's "Stage 4" = the FINAL host-service retirement, executed now that the unified core has landed
(Stages 1–3.5). **Re-grounded at current HEAD (codex-explore map + MAIN-AGENT VERIFIED by grep, worktree excluded).**

### 13.0 Verified current state (what's already gone; what's left)
- **DONE + VALIDATED:** S7.1(a) client host surface; S7.1(b) host spawn claimant; S7.1(c) daemon host servicing
  (**VERIFIED gone: no `TupleSinkServiceMapControlRegion`/`RemoteExecMapControlRegion` in the main tree** — the §3.3
  targets no longer exist); S7.1(d) D7 host constants; COPY *integration hooks* (`worker_tuple_sink_insert.c`,
  `multi_copy.c` hooks, `homer_citus_xact.c` — **VERIFIED absent from the main tree**).
- **LEFT for Stage 4:** S7.2 (UDF + old opaque ① `RemoteExecutionSession` API + dormant COPY sender residue) +
  S7.3 (Tier-2 DMA frontend) + D7→D7′. These are what §13.1 below sequences.
- **⟳ ROUND 1 DONE (citus `39bed1051`, validated 2026-07-19):** the executable EDGES of the retiring Tier-2 command
  path were gutted (see §13.2-R1 RESULT).
- **⟳ ROUND 2 DONE (citus `a12c48667`, validated 2026-07-19):** the Tier-2 cluster + UDF are DELETED and the dead
  decl/ABI/Makefile/SQL trimmed (see §13.3-R2 RESULT).
- **✅ ROUND 3 DONE + VALIDATED (2026-07-19, run `candidate-20260719-161556`; see §13.4-R3 RESULT):** the DPU
  spawn allocator was widened from slots[16..31] to the whole 0..31 region (`DPU_SLOT_FIRST/COUNT` + `CLIENT_OWNED`
  deleted, translations collapsed to identity, capacity 16→32). Proven live by first-spawn `slot=0`. **STAGE 4 COMPLETE.**
- **⚠ Grep hygiene:** a stale pre-S7 agent worktree `.claude/worktrees/agent-a4b43596e72023cf3/` still contains the
  removed files (control-region mappers, COPY hooks, etc.). **It is NOT the main tree — exclude `.claude/worktrees/`
  from every grep** or it produces false "still present" hits (it fooled the first grounding pass).

### 13.1 The COMPILE-DEPENDENCY finding that sets the round order (VERIFIED by grep)
The old ① API is DEFINED in `homer_frontend.c` and CALLED by three deletable files: the UDF
(`remote_exec_pgbench_transaction.c`) **and** the Tier-2 files `homer_frontend_control.c` + `homer_frontend_dma.c`
(`OpenRemoteExecutionSession → OpenCommandSessionThroughLocalService → HomerFrontendDmaOpenCommandSession`). So
**S7.2 (delete ①) and S7.3 (delete Tier-2) CANNOT be separately-compiling commits** — they are ONE mutually-referencing
cluster. The ONLY ① reference left in a KEPT file is a stale COMMENT (`tuple_sink_service_process.c:23471`), not a call
⇒ once the cluster is gone the kept tree builds. Round order therefore = **gut kept→cluster edges, then delete the
cluster whole, then D7′.** `RemoteExecutionSessionIntentSpec` is KEPT (promoted to the core, `homer_session_spec.h`;
used by `homer_client.c`/`homer_session_core.c`) — a wrong-neighbor of the deleted `RemoteExecutionSession` handle.

### 13.2 Round 1 — GUT executable edges ONLY (leave ALL shared types intact). Behavior-preserving. Validate: gate + basebackup + smoke.
**⚠ CRITICAL SEQUENCING (refutation #1, VERIFIED):** the cluster `.c` files are compiled by a Makefile WILDCARD
(`DIST_EXTENSION_OBJS = patsubst(%.c,%.o, wildcard $(dir)/*.c)`, `src/backend/distributed/Makefile:44`; `OBJS`
filters out ONLY `HOMER_SERVICE_OBJS`, `:72` — editing `HOMER_FRONTEND_OBJS` does NOTHING). So a cluster file is
compiled until its `.c` is DELETED. Therefore Round 1 may remove executable EDGES but must **NOT** trim any
shared TYPE/DECL the still-compiled cluster needs — those trims move to Round 2 (atomic with the `.c` deletes).
Round 1 = only:
- `shared_library_init.c`: remove the experimental Tier-2 GUC REG (`citus.enable_experimental_homer_dpu_frontend`,
  ~`:2562-2568`); KEEP the native agent GUC beside it (~`:2570`). (Safe: the backing var stays defined in the
  still-compiled `homer_frontend_dma.c`.)
- `remote_execution_backend_bridge.c` `ExecuteRemoteExecBackendCommand`: gut the legacy named-mailbox branch
  (~`:2316-2354`) + its named result-queue arm (~`:2374-2429`) **and the associated residue the refutation flagged**:
  legacy locals (`:2187`), name formatting/logging (`:2249`), exception cleanup (`:2650`), normal cleanup (`:2703`),
  and the now-unreachable-but-crashing invalid-arena path — after the named arm is gone, an `ARENA_SLOT_INVALID`
  backend leaves `commandMailbox==NULL` dereferenced at `:2523`; either remove the postmaster's permit of that Tier-2
  value (`:2757`/`:2764`) or fail-fast on it. KEEP the native arena branch (`:2258-2315`, `:2430`), the shared
  `SELECTED_DPU_DMA` value + its `arenaBackend` discriminator (`:2528`/`:2634`/`:2678` — native slot release), and the
  command/completion SHM prefixes (native staging mailboxes still use them, `tuple_sink_service_process.c:21145`/`:21153`/
  `:21518`/`:40386`/`:40396`). Do NOT touch `RemoteExecFormatMailboxNames`'s DEFINITION (`:1245`) if still referenced —
  remove only its now-dead callers.
- `tuple_sink_service_process.c:23471`: fix the now-stale ① comment (the only ① reference left in a KEPT file).
- **NOTHING in `homer_frontend.h`, `homer_shm_channel_abi.h`, `worker_protocol.h`, or the `.sql` yet** — deferred to
  Round 2 (they hold types/externs the wildcard-compiled cluster still needs).

### 13.2-R1 RESULT — EXECUTED + VALIDATED GREEN (citus `39bed1051`, 2026-07-19)
Round 1 landed exactly as scoped above. Three files changed (`shared_library_init.c`,
`remote_execution_backend_bridge.c`, `tuple_sink_service_process.c`; **−199/+75**), NO file/API/header/ABI/DPU change.
- **What landed:** legacy Tier-2 arm (per-session named-shm mailboxes + shm_open'd result queue, `arenaSlotIndex==INVALID`)
  replaced by an `ereport(FATAL)` fail-fast (this is BOTH the edge removal AND the NULL-deref defense); legacy result arm
  removed (`else if (…&&arenaBackend)`→`if (arenaBackend)`); 6 legacy mailbox locals + their dead munmap/close in both
  exit paths removed; orphaned static `RemoteExecFormatMailboxNames` removed (SHM prefixes KEPT); experimental GUC
  `enable_experimental_homer_dpu_frontend` de-registered (backing var stays in still-compiled `homer_frontend_dma.c`);
  one stale ① doc-comment reworded.
- **Refutation of the diff (codex-explore, every claim main-agent VERIFIED):** 5/5 load-bearing claims SOUND (NULL-deref
  safety, build-safety = no shared-decl trim, no new unused-`-Werror` orphans, gate/basebackup drive only the arena arm,
  removed cleanup was a no-op for arena). It caught ONE real MISSED edge — the now-dead per-session-fd munmap/close arm in
  **`RemoteExecCleanupSessionResultQueue`** (`remote_execution_backend_bridge.c:~709`; `resultQueueFileDescriptor` is only
  ever `-1` after the legacy arm is gone) — plus 3 stale comments (incl. a "used to name `OpenRemoteExecutionSession()`"
  slip that reintroduced the deleted symbol name in a kept file); all fixed, recompiled clean.
- **⚠ Documented assumption (not a defect):** the arena arm's NULL-deref safety is NOT purely function-local — it also
  relies on the postmaster arena-slot bounds check (`remote_execution_backend_bridge.c:~2640`) and the fact that the only
  in-tree launcher copies that validated index into the child. An out-of-tree caller of exported `StartRemoteExecBackend()`
  could bypass it; none exists in-tree. (See CONTRACTS entry for the arena-required invariant.)
- **Validation** (run `candidate-20260719-133850`, full acceptance set, manual runbook): **PASS** on all three.
  GATE 5/5 decoded abalance / 0 failed; DPU-arena spawn ran (`slot=16`, real `launched_pid`, `bridge_generation
  4663012762262649`); **the new fail-fast string NEVER fired** and `peer_host_spawn_retired` alarm ABSENT → surviving arena
  arm ran, retired arm not reached. Four-role basebackup PASS (23.26 GB, `full_laps=44365`, past wrap). DPU TCP smoke PASS
  (6 legs). Perf `-c1 -t2000` stripped: 307.7/309.9/306.1 tps vs 290–299 band → no regression. Co-arming + Rule 12 honored;
  teardown ledgers balanced; clean preflight before/after.

### 13.3 Round 2 — DELETE the cluster + trim its now-dead decls, ALL AT ONE BOUNDARY. No behavior change (unreachable). Validate: build + gate.
Deleting the `.c` files drops them from the wildcard, so the decl/ABI/extern trims are now safe **only here**:
- Whole-file delete: `homer_frontend.c`, `homer_citus_policy.c/.h`, `remote_exec_pgbench_transaction.c`,
  `homer_frontend_control.c/.h`, `homer_frontend_dma.c/.h`, `homer_frontend_dma_lifecycle.c/.h`,
  `homer_frontend_shm.c/.h`, `homer_frontend_internal.h`, `src/bin/homer_frontend_dma_smoke.c`; delete the UDF
  `sql/udfs/citus_remote_exec_pgbench_transaction/latest.sql`. (Do NOT `rm -r` `worker/homer` or `utils/homer` — KEPT
  native/COPY-worker files live alongside; the wildcard drops only the removed `.c`.)
- `homer_frontend.h`: trim the old ①/command/batch/opaque-session decls (`:98`/`:252`/`:350` etc.); KEEP the live
  tuple-substrate decls (`:30-38`). KEPT includers are only `shared_library_init.c:116` + `remote_execution_backend_bridge.c:55`.
- `homer_shm_channel_abi.h`: remove the Tier-2 control-region name + aggregate + ready bitmap (`:25`, `:54-80`); KEEP
  `CitusRemoteExecControlSlot` + states (`:30-47`) — native client/DPU/service still use them (`homer_client.c:1592`/`:1603`,
  `homer_service_dpu_dma.c:14447`, `tuple_sink_service_process.c:41741`).
- `worker_protocol.h`: remove the UDF extern (`:57`).
- `remote_execution_backend_protocol.h:41`: remove `CITUS_REMOTE_EXEC_RESULT_QUEUE_SHM_PREFIX` — now dead (only the
  deleted lifecycle+smoke used it). KEEP the command/completion prefixes (native uses them).
- SQL/catalog: remove the UDF include from `sql/citus--13.1-1--13.2-1.sql:5`. **⟳ DECISION CHANGED at implementation
  (see §13.3-R2 RESULT): NO version bump / DROP FUNCTION.** The bump-to-`13.2-2`-plus-DROP originally planned here would
  force `multi_extension` regress maintenance (a new version snapshot + downgrade) for a test the farnet flow never runs,
  for no deployment benefit — `13.2-1` is a dev-only version, so removing the `#include` is enough: fresh installs never
  create the UDF, and a pre-existing dev install keeps a dormant `pg_proc` entry nothing calls, cleared on the next fresh
  (re)create.
- Makefile: drop the Tier-2 smoke binary wiring (top-level `Makefile:33`/`:55`/`:221`).
- **Pre-commit doc/comment sweep (refutation #6):** rewrite the `CONTRACTS.md` `CitusRemoteExecSessionKey` entry
  (`:53`/`:86`/`:101`) — the key builder is now Push B's `HomerOpenBuildSqlRequest` (`homer_client.c:2143`/`:2157`), not
  the deleted `BuildControlSessionKeyFromIntent`; fix the `:95` "① compiled through `homer_frontend.h`" and `:1009`
  "`HomerFrontendDmaOpenDocaDevice` unchanged" entries (both name deleted symbols); update
  `homer_current_implementation_checkpoint.md:105`/`:107` (UDF-as-current-pgbench-wrapper); fix code comments naming
  deleted functions (`homer_client.c:1045`, `remote_execution_client.h:300`, `homer_session_spec.h:22`).

### 13.3-R2 RESULT — EXECUTED + VALIDATED GREEN (citus `a12c48667`, 2026-07-19)
Round 2 landed as the atomic whole-cluster delete + now-dead decl/ABI/Makefile/SQL trims. **35 files, −8452/+111**,
citus-only, behavior-neutral (only unreachable code).
- **What landed:** 15 whole-file deletes (the Tier-2 cluster + UDF + smoke); `homer_frontend.h` trimmed to the 3
  tuple-substrate externs; `homer_shm_channel_abi.h` dropped the dead control-region aggregate/bitmap/name (KEPT
  `CitusRemoteExecControlSlot` + states + completion prefixes); `worker_protocol.h`/`remote_execution_backend_protocol.h`
  dead-decl trims; Makefile smoke-wiring + inert `HOMER_FRONTEND_OBJS` corrected; UDF `#include` removed (no bump).
- **⟳ Catalog decision (changed from §13.3 plan, owner-approved):** NO `13.2-2` bump / DROP — `#include` removal only.
  Rationale in the §13.3 SQL bullet above. The diff refutation independently reached the same place (it flagged the
  bump's missing `multi_extension` coverage + missing downgrade as the cost avoided).
- **Refutation of the diff (codex, every claim main-agent VERIFIED):** deletion boundary SOUND (no surviving
  compiled/test caller of any removed symbol/UDF; keep-boundary correct). Caught what a clean build does NOT: stale
  Makefile `HOMER_FRONTEND_OBJS` membership (inert, corrected) + ~13 stale comments in kept code describing the removed
  cluster as live (all fixed). A symbol-by-symbol check on `homer_shm_channel_abi.h` also corrected the plan's
  line-only trim — the completion prefixes (`:26-27`) between the removed name and structs are load-bearing for the
  native service and were KEPT.
- **Comment style (owner feedback):** code/SQL/Makefile comments are current-truth first, sparse "formerly", NO
  stage/tier labels or deleted-file lists; the migration record lives in the KB/commit, not the source.
- **KB sweep:** CONTRACTS repointed (`CitusRemoteExecSessionKey` builder → `HomerOpenBuildSqlRequest`; the
  arena-required invariant's INVALID producer is now GONE; `HomerFrontendDmaOpenDocaDevice` refs removed); the
  "current implementation checkpoint" banner marks the cluster deleted.
- **Validation** (run `candidate-20260719-150756`, build-clean + THE GATE, manual runbook): **PASS**. Complete-target
  build + install exit 0; `citus.so` relinks with none of the deleted objects; fresh `CREATE EXTENSION citus` → 13.2-1
  with the UDF ABSENT (`to_regprocedure(...) IS NULL`). GATE 5/5 decoded / 0 failed; DPU-arena spawn ran (`slot=16`,
  real `launched_pid`); `peer_host_spawn_retired` ABSENT both DPU logs; gate/alarm/teardown checkers PASS; peer artifact
  SHA-256 parity. Basebackup+smoke were optional insurance (deletion touches neither path) — not run.
- **⚠ Operational note:** the native DPU trees were found stale/off-lineage (commits `4a5d189`/`f455568`, far behind
  HEAD) and were refreshed to this candidate snapshot for the run — a candidate-provenance refresh, NOT an ABI redeploy.
  (Reinforces the standing DPU-tree-drift hazard; the DPUs are now current as of this candidate.)

### 13.4 Round 3 — D7 → D7′ (all-32; USER-decided). Validate: gate + basebackup + smoke.
**Refutation #4 corrections folded in.** The last host spawn CAS died with `homer_frontend_dma_lifecycle.c` in Round 2
(VERIFIED: it was the only spawn-slot CAS `:250`/`:255`; `HomerServiceDpuSpawnBegin` uses a deterministic free-list slot,
no CAS; `HomerFrontendAgentBindArenaSlot` is an ARENA-slot CAS, a native KEEP neighbor, NOT a spawn claimant). So Round 3 is:
- Delete the now-unused `CLIENT_OWNED` spawn state (`remote_execution_backend_protocol.h:127`) + host/DPU range constants
  + adjacency checks; KEEP total ABI cardinality `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_COUNT` + the range-agnostic
  postmaster scan (`remote_execution_backend_bridge.c:2844`/`:2850`) + the 32-slot wire descriptor check
  (`homer_service_dpu_dma.c:12543`).
- **REWRITE, do NOT delete, the LIVE DPU-range translation sites** (the spawn state machine calls them):
  `homer_service_dpu_spawn.c` (`:230` absolute assign, `:326`/`:353`/`:449`/`:467`/`:512`) + ~10 sites in
  `homer_service_dpu_dma.c` (`:7577`/`:7584`/`:7757`/`:7909`/`:8014`/`:8128`/`:8231`/`:8266`/`:8300`/`:11793`/`:12582`/`:12597`)
  — rebase "DPU owns 16..31" to the whole 0..31 space.
- **all-32 (USER, 2026-07-19):** grow capacity + local storage to 32 — `HOMER_SERVICE_DPU_SPAWN_MAX_PENDING`
  (`homer_service_dpu_spawn.h:95`) → total count, `entries[]` (`:172`), runtime `spawnSlots[]` (`homer_service_dpu_dma.c:974`),
  spawn buffers (`:14857`/`:14970`); remove `DPU_SLOT_FIRST/COUNT`. (Correctness note: the choice does NOT affect the
  current gate — native spawn acquires one of 16 ARENA slots first, `homer_service_dpu_dma.c:6478`, failing after 16 at
  `:6573`, before reaching spawn — so all-32 changes capacity/storage, not gate behavior. all-32 avoids the neutral-16
  under-spec where absolute-assign/reverse-translate/remote-address `:230`/`:7584`/`:12597` must agree on a physical subset.)
- assert D7′ (exactly one claimant = the DPU).

### 13.4-GROUNDED — concrete rebase at HEAD `a12c48667` (grep-authoritative, 2026-07-19)
Re-grounded by grep on the actual constants; the §13.4 line numbers above were pre-R2 estimates and drifted. The
**two index spaces** are the key: `spawnSlotIndex` (uint32_t, `homer_service_dpu_spawn.h:145`) is the GLOBAL index into
the HOST's 32-slot `region->slots[]` — it addresses host memory at `homer_service_dpu_dma.c:12594`
(`hostRingAddress + spawnSlotIndex*slotBytes`); `localBufferIndex` is the DPU-LOCAL index into `engine->spawnSlots[]` +
the local spawn buffers. Today `localBufferIndex = spawnSlotIndex - DPU_SLOT_FIRST(16)` because the DPU owns only the
UPPER HALF. **D7′ gives the DPU the whole region ⇒ `spawnSlotIndex` widens to 0..31 and the map becomes the IDENTITY
`localBufferIndex = spawnSlotIndex`.**

**Design decision (improvised, recorded):** KEEP `localBufferIndex` as an explicit identity alias
(`localBufferIndex = spawnSlotIndex`), do NOT collapse the variable. It is SEMANTICALLY correct (host vs DPU-local
address spaces that now coincide numerically, not the same thing) and minimizes blast radius (downstream
`engine->spawnSlots[localBufferIndex]` / `HomerDpuDmaSpawnBufferForIndex(...,localBufferIndex)` untouched).

**Two roles of the removed constants** (this is why the rebase is mechanical, not a redesign):
- `DPU_SLOT_FIRST` (=16) is a BASE OFFSET for the global↔local map ⇒ becomes 0 ⇒ every `- DPU_SLOT_FIRST` and
  `DPU_SLOT_FIRST +` term vanishes (identity); every `spawnSlotIndex < DPU_SLOT_FIRST` lower-bound is `< 0` on a uint
  (always false) ⇒ drop it, keep only the upper bound.
- `DPU_SLOT_COUNT` (=16) is a genuine CAPACITY ⇒ becomes `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_COUNT` (32).

**Exact edit set (grep-complete at HEAD; excl. `.claude/worktrees/`):**
- `remote_execution_backend_protocol.h`: remove `DPU_SLOT_FIRST`/`DPU_SLOT_COUNT` (`:66`/`:67`) + their `#if`
  static-assert (`:68-71`); remove `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_CLIENT_OWNED = 1` (`:115`) — REQUEST_READY=2 /
  RESPONSE_READY=3 stay explicitly numbered so the wire ABI is byte-identical (value 1 becomes a retired gap); refresh
  the D7′ comment block (`:42-65`) to "widening DONE, DPU owns the whole 0..31 space". KEEP `SPAWN_SLOT_COUNT 32U`
  (`:37`) + the arena-slot + mailbox constants.
- `homer_service_dpu_spawn.h:95`: `HOMER_SERVICE_DPU_SPAWN_MAX_PENDING = …SPAWN_SLOT_COUNT` (was `…DPU_SLOT_COUNT`) →
  `entries[]` (`:172`) grows to 32. Fix the `:145` "16..31" comment → "0..31".
- `homer_service_dpu_spawn.c:230`: `entry->spawnSlotIndex = entryIndex` (was `DPU_SLOT_FIRST + entryIndex`). Fix the
  `:218` busy-error string + `:226-229` "owns absolute slot 16+i" comment.
- `homer_service_dpu_dma.c`: `spawnSlots[…SLOT_COUNT]` (`:974`); loops/alloc `…SLOT_COUNT` (`:1713`/`:14855`/`:14968`);
  bounds `localBufferIndex < …SLOT_COUNT` (`:9893`/`:11644`/`:11791`). The 9 translation guards
  (`:7577-79`/`:7757-59`/`:7909-11`/`:8014-16`/`:8128-30`/`:8231-33`/`:8266-68`/`:8300-02`/`:12580-82`): drop the
  `< DPU_SLOT_FIRST` lower bound + rewrite the upper to `spawnSlotIndex >= …SLOT_COUNT`. The 9 translations
  (`:7584`/`:7765`/`:7916`/`:8021`/`:8135`/`:8237`/`:8273`/`:8306`/`:12587`): `localBufferIndex = spawnSlotIndex`. The
  consistency check `:11792`: `spawnSlotIndex == localBufferIndex` (was `== DPU_SLOT_FIRST + localBufferIndex`). The
  host-address `:12594` uses the GLOBAL `spawnSlotIndex` UNCHANGED — it now addresses host slots 0..31 (the whole point).
- **KEEP (verified range-agnostic):** the postmaster scan (`remote_execution_backend_bridge.c`, scans all
  `SPAWN_SLOT_COUNT`, dispatches on `state`) + the 32-slot wire descriptor check (`homer_service_dpu_dma.c:12543`).
- **NOTE — plan sites that grep did NOT confirm:** `homer_service_dpu_spawn.c:326/:353/:449/:467/:512` do NOT touch the
  range constants (they loop in local entry-index space already) ⇒ no rebase there. The §13.4 estimate over-counted.

**Load-bearing SAFETY claim (must refute):** D7′ correctness rests ENTIRELY on "no second claimant writes
`region->slots[0..15]`". R2 deleted the host frontend cluster (the old CAS producer). Round 3 must PROVE nothing else
still writes the lower half before the DPU widens into it — a stale producer would reintroduce the exact race D7′ forbids.

### 13.4-R3 RESULT — EXECUTED + REFUTED CLEAN; VALIDATION PENDING (citus working tree on `a12c48667`, 2026-07-19)
Round 3 landed as the grep-grounded identity rebase. **5 files, +86/−146** (net −60), citus-only, DPU-side spawn
allocator. NOT yet committed — held for the full acceptance set (live-path change).
- **What landed:** `remote_execution_backend_protocol.h` — deleted `DPU_SLOT_FIRST`/`DPU_SLOT_COUNT` + their `#if`
  static-assert; retired `CLIENT_OWNED = 1` (kept `REQUEST_READY=2`/`RESPONSE_READY=3` explicit ⇒ wire ABI byte-identical);
  reworded the single-claimant block. `homer_service_dpu_dma.c` — 9 bounds checks collapsed
  `< FIRST || >= FIRST+COUNT` → `>= SPAWN_SLOT_COUNT` (the `< FIRST` lower bound was dead on a uint once FIRST=0); 9
  translations `spawnSlotIndex - FIRST` → identity `spawnSlotIndex`; consistency check `== FIRST + local` → `== local`;
  capacity sites (`spawnSlots[]`, facts loop, alloc, `SpawnBufferForIndex` bound, callback/retire bounds) `DPU_SLOT_COUNT`
  → `SPAWN_SLOT_COUNT` (16→32). `homer_service_dpu_spawn.{c,h}` — `MAX_PENDING`/`entries[]` → 32, `spawnSlotIndex = entryIndex`,
  comment/error-string fixes. `remote_execution_backend_bridge.c` — sparsified R1's verbose S7/Stage-4/Tier-2 comments
  (comment + diagnostic-text only; the fail-fast `ereport` message reworded, no logic/control-flow change).
- **Design decision (improvised, recorded):** kept `localBufferIndex` as an explicit identity alias
  (`= spawnSlotIndex`) rather than collapsing the variable — it is semantically correct (host `region->slots[]` index vs
  DPU-local `spawnSlots[]`/buffer index, two address spaces that now coincide numerically) and minimizes blast radius.
- **KEPT (verified):** `SPAWN_SLOT_COUNT 32U`; the host `region->slots[]` was ALREADY 32 — R3 just lets the DPU use all
  of it; the range-agnostic postmaster scan; the 32-slot wire descriptor check (`homer_service_dpu_dma.c:12524`); the
  arena constants `ARENA_SLOT_COUNT`/`HOMER_FRONTEND_ARENA_SLOT_COUNT = 16` (a DISTINCT capacity — name-scoped rebase
  never touched them; tree-wide sweep confirms).
- **Diff refutation (codex-explore continuation, every claim main-agent VERIFIED):** arithmetic equivalence SOUND for all
  9 bodies (uint predicate reduces to `< 32`; identity == hypothetical `−0`; accepting `0..15` is intentional, not a
  dropped rejection); capacity coherence SOUND; host-address uses the global index UNCHANGED; ABI/wire layout unchanged;
  the sole-claimant SAFETY proof holds (DPU writes REQUEST_READY+FREE, postmaster writes only RESPONSE_READY,
  `EnsureSpawnRegionMapped` is create-once so slots 0..15 start FREE — see §13.4-GROUNDED). It caught 5 stale-COMMENT
  issues (no logic): "all 16" API comment, my off-by-one `0..SLOT_COUNT` wording, and 3 R2-residue "host producer /
  host-service submitter" mentions (incl. `dma.c` release comment that directly contradicted the sole-claimant
  invariant). ALL FIVE FIXED + re-verified; final residue sweep clean (remaining "host producer" hits are the byte-ring
  data path — a valid, current, different concept).
- **VALIDATION: PASS** (run `candidate-20260719-161556`, full acceptance set, both ARM DPUs rebuilt+redeployed to the R3
  snapshot). Protocol versions unchanged (backend `16U`, bridge `5U`) — matches the no-ABI-bump premise.
  - **R3 SIGNATURE PROVEN:** first-allocated `spawnSlotIndex = 0` in BOTH the human line
    (`DPU backend spawn begin/COMPLETED ... slot=0 ... launched_pid=1196021`) and the structured
    `HOMER_EVENT component=dpu_spawn event=begin/complete ... slot=0`. Under the retired R2 binary this reads `slot=16`;
    `slot=0` is direct proof the R3 widened-range allocator is the live binary (also a redeploy-provenance check). The
    4-client repeat produced 4 more spawn/complete pairs, all `slot=0` (each freed before the next).
  - **Sole-claimant invariant held under load:** the second-claimant alarm ("a second claimant is writing the DPU-owned
    spawn region") was ABSENT on BOTH DPU logs over the bracketed candidate interval.
  - GATE: 5/5 decoded abalance / 0 failed (1c debug), 20/20 / 0 failed (4c); `gate_check`+`alarm_check` PASS both.
    Four-role basebackup PASS (`full_laps=44365`, past wrap thousands of times). DPU TCP smoke PASS (both ends `ok`).
    Co-arming verified (bridge_generation matched, no `ATTACH rejected`); host-service absent via `/proc/PID/exe`; clean
    preflight before/after; teardown ledgers balanced.
  - **Runbook fix (R2 residue surfaced here):** the build command listed the `frontend-dma-smoke` target R2 DELETED
    (a12c48667); removed from `farnet_operator_runbook.md` build list in this KB commit.
- **Stage 4 is now COMPLETE** (R1+R2+R3 all landed+validated). The host-service retirement is done; the DPU is the sole
  backend-spawn claimant across the whole region.

### 13.5 D7′ slot count — RESOLVED: all-32 (USER, 2026-07-19). (Rationale + blast radius folded into §13.4.)

### 13.6 Validation cadence + KEEP guard
Rounds 1 and 3 change live-path behavior → full acceptance set (gate + four-role basebackup + DPU TCP smoke). Round 2
deletes only unreachable code → build-clean + gate (basebackup+smoke optional insurance). KEEP guard for every round:
the unified core + shared dispatcher (`homer_client.c` `HomerClientOpenSelectedDpuOperation:2397`, `homer_session_core.c`),
the four-role basebackup typed calls, `TupleSinkServiceDispatchLocalControlSlot`/native spawn, the frontend agent +
its `enable_homer_dpu_frontend_agent` GUC, byte-ring pool + `HomerByteRingSinkDrain`, `sessionUID`, and
`HomerServicePersistentSendCompletionCallbacks` (the F7 wrong-neighbor of the deleted COPY publisher).

### 13.7 Refutation record (codex-explore on §13, 2026-07-19; every claim MAIN-AGENT VERIFIED in code)
- **BLOCKER (fixed): Round 1 header/ABI trims break the WILDCARD-compiled cluster build.** `homer_frontend.h` +
  `CitusRemoteExecControlRegion` trims moved to Round 2 (atomic with the `.c` deletes). VERIFIED via `Makefile:44`/`:72`
  (wildcard OBJS; `HOMER_FRONTEND_OBJS` is inert) — the still-compiled UDF/`homer_frontend_shm.c` need those types.
- **Round 1 bridge gut was incomplete:** added the setup/cleanup/formatting residue (`:2187`/`:2249`/`:2650`/`:2703`) and
  the invalid-arena crash arm (`commandMailbox==NULL` deref at `:2523` once the named arm is gone). KEEP `arenaBackend`
  + native SHM prefixes.
- **Round 2 additions:** dead `CITUS_REMOTE_EXEC_RESULT_QUEUE_SHM_PREFIX` removal; concrete DROP FUNCTION (no existing
  script drops it); the doc/comment sweep (CONTRACTS `CitusRemoteExecSessionKey`/①/`HomerFrontendDmaOpenDocaDevice`
  entries, checkpoint doc, code comments).
- **Round 3 rewording:** the host CAS dies in Round 2 (not a separate delete); the DPU-range translations are LIVE and
  must be REBASED (not deleted); all-32 does not change gate correctness (arena-slot cap gates first).
- **Confirmed SOUND:** Round 2 file set is link-complete once trims move; ordering gut→delete-cluster→D7′ is the minimal
  compile-safe sequence; the DPU is genuinely the sole spawn claimant after Round 2.
