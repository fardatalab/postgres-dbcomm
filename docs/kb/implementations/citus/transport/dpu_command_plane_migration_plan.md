# DPU command-plane migration ("Stage B") — plan

**Status (July 9, 2026): PLANNED, not started. Blocked on `--homer-dpu -c 1` and `-c 2` (see
[dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) Stages 1–2).**

**Goal.** Move the SQL command/completion plane onto the DPU, so a session's command spine and its
result relay live in the SAME process. Today they do not: pgbench's `CLIENT_SQL_SESSION` is created in
the HOST service's control SHM, while the role-7 result relay lives in the DPU-native service. That
split is deliberate and currently correct — the two legs are bound by a unique `sessionUID`, a
rendezvous rather than a shared table (see
[session_identity_and_pairing.md](../../../future-directions/citus/transport/session_identity_and_pairing.md)).
Unifying ownership is the migration's endgame, not a prerequisite for anything currently in flight.

Related: [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md),
[cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md).

---

## Correcting two premises before planning

### 1. `HomerFrontendDmaOpenCommandSession` is leg 1 of THREE, not "the DPU↔DPU spine"
It is the **host-frontend → its LOCAL DPU** command opener, and it works (verified: opens a DOCA
control channel, exports/imports bridge + backend mailboxes, submits a HOST socketless-backend spawn,
supports START/POLL — `homer_frontend_dma.c:1039`, `:1352`, `:1093`;
`homer_frontend_dma_lifecycle.c:192`, `:283`). The full spine needs three legs:

| Leg | What | Status |
|---|---|---|
| **L1** host frontend → local DPU | command-session open over the DPU control slot | **exists**, but only for a *PG backend* (`homer_frontend_control.c:803`). The **client library has no equivalent** — `HomerClientOpenSqlSession` (`homer_client.c:1518`) maps a HOST service's control SHM. |
| **L2** DPU → peer DPU | command-session peer-open | **net-new.** Gates: peer open rejects non-SQL/client-SQL opKind (`tuple_sink_service_process.c:37385`) + mailbox checks (`:37395`, `:37406`). |
| **L3** remote DPU → remote HOST | spawn the socketless backend | **UNKNOWN — investigate first.** The backend is a PostgreSQL process and must run on the host. `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest` (`homer_frontend_dma_lifecycle.c:192`) mmaps the spawn region **locally** (host-side). Whether a DPU service can reach the host's `citus_remote_exec_backend_spawn_v*` region at all is not established. |

**L3 decides how big B is.** If no DPU→host spawn path exists, B must build one (DMA write into the
host spawn region + a host-side consumer), which is materially larger than "relax three gates".

### 2. The UDF retires at the END of B, not after `--homer-dpu` validates
The checkpoint's "step 8" says delete `citus_remote_exec_pgbench_transaction` once `--homer-dpu` is
validated. **That precondition is wrong.** `--homer-dpu` opens its command session through host SHM,
so validating it proves nothing about the frontend DPU command channel. And the UDF is the **only
non-smoke caller** of `HomerFrontendDmaOpenCommandSession` — the very code B promotes. Deleting it
early removes B's only working exercise of its own foundation. It goes in **B4**.

---

## Stages

### B0 — Blocking investigation (read-only, cheap). Do this FIRST.
Answer with code, not assumption. Each answer changes the plan below.

- **B0.1 (decides B's size).** Can a DPU-native service submit a backend-spawn request into the HOST's
  `citus_remote_exec_backend_spawn_v*` region? Trace `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`
  (`homer_frontend_dma_lifecycle.c:192`) and `TupleSinkServiceSubmitBackendSpawnRequest`. Is the region
  reachable over the DMA bridge (is it exported in any mmap descriptor?), or is it strictly host-local
  shm? If strictly local: design the DPU→host spawn submission (new descriptor role + a host-side
  consumer) and size it before committing to B.
- **B0.2.** Does the peer transport (9717) support a command-session peer-open at all today, or only
  tuple-sink/payload opens? Read the peer open handler around `:37385` and enumerate exactly what
  `OP_CLIENT_SQL_SESSION` would need.
- **B0.3.** What does `HomerServiceDpuFindOrCreateSelectedSession` (`:38653`, called `:39099`) track? Can
  it host a full `CLIENT_SQL_SESSION` lifecycle (open / START / POLL / close), or is it
  command-staging-only? If it can, the DPU already has most of a session table and B2 gets smaller.
- **B0.4.** Which mailboxes does a DPU-opened command session need, and where do they live? `REGISTER_MEMORY`
  unconditionally requires `sessionState->commandMailbox.mailbox` (`:35238`). Is a DMA'd mailbox the
  right answer, or is the check simply wrong for DPU-opened sessions?

**Deliverable:** a written answer per question with `file:line`, and a revised size estimate for B1–B4.
**Gate:** none (read-only). **Do not start B1 before B0 lands.**

### B1 — Truth-in-comments (trivial, unblocks everyone reading this code)
- Fix the stale GUC help text (`shared_library_init.c:2596`) — it claims the path "stops after
  bridge-memory setup with an explicit not-implemented error", which is false in DOCA builds.
- Document that `RemoteExecutionRejectDpuFrontendChannelIfSelected` (`homer_frontend_control.c:644`,
  `:721`, `:1099`) guards *unsupported tuple-sink/fallback ops*, while command-session open **bypasses
  it** (`:803`). These are different paths, not a contradiction — say so in both places.
- Comment `HomerFrontendDmaOpenCommandSession` as the L1 opener and name the missing L2/L3 legs.

**Gate:** compiles; no behavior change.

### B2 — Client-side `HomerClientOpenSqlSessionSelectedDpu` (single-node)
Give the client library the L1 opener the PG backend already has. **Single node only, no peer leg** —
this is the smallest change that proves a client can own a command session on a DPU.

- **B2.1** New client entry point in `src/bin/homer_client.c`: combine the SQL command-session request
  fields (`:1558`–`:1575`: `sessionKey.opKind = OP_CLIENT_SQL_SESSION`, `clientSqlResultDpuRelay`,
  `sessionUID`) with the selected-DPU export / control-slot machinery basebackup already uses
  (`:2644`, `:2675`, `:2727`, `:2783`). Emit the open into the DPU control slot, not host SHM.
- **B2.2** Service: admit `OP_CLIENT_SQL_SESSION` arriving over the DPU control slot. Relax
  `TupleSinkServiceOpenRequestNeedsAsyncLocalControl` (`:34817`) and
  `TupleSinkServiceProgressCommandOpenAsyncOp` (`:35172`). Keep the rejection for genuinely unsupported
  opKinds — relax by *allow-list*, not by deleting the check.
- **B2.3** `REGISTER_MEMORY` mailbox requirement (`:35238`): per B0.4, either give the DPU-opened session
  a DMA'd `commandMailbox` or make the requirement conditional. Prefer giving it a real mailbox;
  a conditional check invites a second silent divergence.
- **B2.4** Backend spawn: reuse the existing HOST-side leg unchanged (single-node ⇒ the client and the
  backend are on the same host, so `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest` still applies).
- **B2.5** pgbench: add `--homer-dpu-command` (or fold into `--homer-dpu`; decide once B2.1 exists) to
  select the new opener.

**Gate:** single-node pgbench opens its command session on the LOCAL DPU service, drives a socketless
backend on the same host, and executes the standard TPC-B script correctly at `-c 1`. Verify from the
DPU service log that the command session was created there, and that **no host Homer service is in the
command path**. Basebackup + `--homer-dpu` result relay regressions must both still pass.

### B3 — DPU↔DPU peer command-session open (the net-new leg)
- **B3.1** Relax the peer command-open gates: opKind rejection (`:37385`) and the following mailbox
  checks (`:37395`, `:37406`). Allow-list, as in B2.2.
- **B3.2** The receiving DPU must spawn a socketless backend on ITS host. **This is B0.1.** If no path
  exists, build it: a spawn-request descriptor DMA'd into the host spawn region + a host-side consumer.
  Treat this as its own sub-stage with its own smoke.
- **B3.3** Thread `sessionUID` through the DPU↔DPU command open exactly as the host↔host path does today
  (`session_identity_and_pairing.md:58-62`), so the result relay's existing rendezvous keeps working
  unchanged.

**Gate:** cross-node `pgbench --homer --homer-dpu` from farnet0 to farnet1 with the command session
owned by the DPU services and **no host Homer service running at all**. Correct decoded results at
`-c 1`, then `-c 2`. Basebackup regression.

### B4 — Collapse the split; retire the superseded paths
Only after B3's gate passes.

- **B4.1** The `CLIENT_SQL_SESSION` now lives in the DPU service beside the relay. Re-examine whether the
  `sessionUID` rendezvous is still needed for the role-7 ring. **Expectation: YES, keep it** — the ring is
  still exported by a host process and imported across PCIe, so the rendezvous spans a boundary that does
  not disappear. Do not remove it; confirm in code and record the reasoning.
- **B4.2** Retire `HomerClientOpenSqlSession` (host-SHM command path) for pgbench.
- **B4.3** Retire `citus_remote_exec_pgbench_transaction` + its UDF/extern/build refs (checkpoint "step 8").
  **Now**, not earlier: until B2 lands, it is the only non-smoke caller of the code B promotes.
- **B4.4** The GUC becomes the only path ⇒ remove `citus.enable_experimental_homer_dpu_frontend` and its
  gates, or flip its default and keep it as a kill switch. Decide with a measurement, not a preference.

**MUST NOT touch, at any stage** (basebackup calls the first unconditionally; `--homer-dpu` result
delivery rides the third): `HomerClientOpenBaseBackupStreamSelectedDpu` (`homer_client.c:2551`, from
`basebackup_homer.c:289`), `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:2922`),
`HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`, via `:3925`, `pgbench.c:9559`).

---

## Standing risks

1. **"Working code with no validated consumer" is not dead code.** The selected-DPU backend-spawn spine
   compiles, is exercised only by `homer_frontend_dma_smoke.c:118`, and looks exactly like scaffolding.
   Basebackup does NOT use it (its selected-DPU open is `OP_TUPLE_SINK` / `sessionKey.opKind =
   OP_BASE_BACKUP`, `homer_client.c:2795`/`:2809`). Audit by "what would call this when the migration
   finishes", not by "what calls this today".
2. **Regression sweep.** Three regressions landed July 6–8 unnoticed because each validation built one
   target and ran one workload. Before and after every B stage: build ALL targets (`service-bin`,
   `client-bin`, every smoke) and run basebackup + `--homer-dpu` + COPY.
3. **Baselines need a commit SHA**, not a date. A June-6 COPY baseline was cited as evidence that COPY
   works today; it does not (see the regression doc).
4. **Error propagation is a debuggability tax.** An aborted byte-ring stream hangs the client with no
   error. Every fault on this path arrives as a hang. Fix before the next hard bug, not during one.
