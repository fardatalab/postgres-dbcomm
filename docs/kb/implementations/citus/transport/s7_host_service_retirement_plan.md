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
   dropping the host-control pump arm must handle the STARTUP_WAIT path too — and S7.1 must FIRST confirm
   STARTUP_WAIT's local-control is host-role-only (the control region is host-private) before removing it.
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
- GUC backing vars — `EnableExperimentalTupleSinkRouting` (`homer_tuple_queue_frontend.c:45`),
  `EnableExperimentalHomerDpuFrontend` (`homer_frontend_dma.c:67`).
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
