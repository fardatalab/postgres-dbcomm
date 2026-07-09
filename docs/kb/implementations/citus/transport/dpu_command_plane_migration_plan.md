# DPU command-plane migration — plan

> **Naming.** This is a SEPARATE workstream from the byte-ring pool plan, not its "Stage 3".
> The pool plan owns data-plane *ring ownership* (Stages 0a/0b/1/2). This doc owns the
> *command plane*. Its stages are numbered independently — cite them as "command-plane Stage N".
> The pool plan's old Stage 3 ("retire the DPU frontend command channel") was inverted by evidence
> and dissolved into this doc.

**Status (July 9, 2026): PLANNED, not started. REORDERED — this is now a PREREQUISITE, not a
follow-on.**

> **CORRECTION (July 9, 2026, evening).** An earlier revision of this doc said "Unifying ownership is
> the migration's endgame, not a prerequisite for anything currently in flight." **That is wrong**, and
> three `--homer-dpu` bring-up runs proved it.
>
> The DPU DMA engine's mirrored ranges come ONLY from `engine->hostMmapImports`, populated solely by
> `HomerDpuDmaImportHostMmapDescriptorForSetup` — i.e. by a HOST FRONTEND exporting its ring to the
> engine over the DPU TCP setup socket. The engine exists to import HOST memory across PCIe. **A
> host-resident service has nothing to import.**
>
> Today `pgbench` opens its command session via `HomerClientOpenSqlSession(HomerClientControl *)`, a
> mapped HOST-service control SHM, and payload/result streams are created by the service that owns the
> session. So the `--homer-dpu` result stream is born in the HOST service, whose engine (if it even has
> one) can never hold an import for it. Observed: farnet1 binds a `purpose=0` MIRROR slot, then silence
> — `HomerDpuDmaMirroredByteRangeReadyForServiceSink` iterates an empty import table, egress never
> fires, the receiver never sees a byte, and the client hangs.
>
> **`--homer-dpu` therefore cannot work until the SQL command session lives in the DPU-native service.**
> The client-side `HomerClientOpenSqlSessionSelectedDpu` (Stage 2 below) is not an architectural
> nicety; it is what puts the session in the process that owns the engine.
>
> Cascade: byte-ring pool Stage 2 (`tupleSourceRing` per-session) is gated on a `-c 2 --homer-dpu`
> repro, so it is blocked behind this too.

**Intended end state (stated by the user, July 9, 2026):** the client binary and the PostgreSQL server
binary each use the Homer FRONTEND to send/receive commands, completions and tuple results. No libpq.
Two nodes talk to each other **through their own DPUs**, which DMA to and from their local host. The
Homer *service* is the DPU service. Host Homer services are a bring-up scaffold, not the target.

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
| **L3** remote DPU → remote HOST | spawn the socketless backend | **MISSING today (verified), but the mechanism class exists.** See below. |

**L3, verified (July 9, 2026).** There is NO DPU→host backend-spawn path today:
- `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest` reaches the spawn region by `shm_open` +
  `mmap` of a **host-local POSIX shm** (`homer_frontend_dma_lifecycle.c:217`, `:225`) — not DMA.
- **No DPU-side code references the spawn region at all** (grep of `homer_service_dpu_dma.c`,
  `homer_service_dpu_setup_tcp.c`).
- `backendChannelMode = SELECTED_DPU_DMA` does NOT mean "the DPU spawns". It tells a **host-spawned**
  backend to use DPU DMA for its command/completion mailboxes. (An earlier note in this project
  claimed these "already do" DPU→host spawn. **That was wrong**, and is corrected here.)
- The service-side spawner `TupleSinkServiceSubmitBackendSpawnRequest`
  (`tuple_sink_service_process.c:18814`) is called by whichever SERVICE handles the open — local
  (`:33810`) or peer (`:37302`). On the host it works; on the DPU it would `shm_open` a region that
  does not exist there.

**But the capability already exists, and so does the vehicle.** The DPU routinely DMA-writes host
memory (it publishes backend commands/completions into host mailboxes), and the DPU setup TCP socket
already carries the **host PCI mmap export descriptor**. So the likely L3 design is the pattern already
in use, not a new mechanism:
1. the postmaster/bridge adds `citus_remote_exec_backend_spawn_v*` to its mmap export list;
2. the DPU service DMA-writes a spawn slot and rings a doorbell;
3. the postmaster consumes the slot exactly as it does today (it is the same physical shared memory).

Stage 0.1 must CONFIRM this rather than assume it — in particular, whether the postmaster (not just a
client) performs a DPU setup export, and whether a DMA'd write into that region is visible to the
postmaster's poller without extra synchronisation. **The answer sizes Stages 1–4.**

### 2. The UDF retires in the LAST stage, not after `--homer-dpu` validates
The checkpoint's "step 8" says delete `citus_remote_exec_pgbench_transaction` once `--homer-dpu` is
validated. **That precondition is wrong.** `--homer-dpu` opens its command session through host SHM,
so validating it proves nothing about the frontend DPU command channel. And the UDF is the **only
non-smoke caller** of `HomerFrontendDmaOpenCommandSession` — the very code this plan promotes. Deleting it
early removes this plan's only working exercise of its own foundation. It goes in **command-plane Stage 4**.

---

## Stages

### Stage 0 — Blocking investigation (read-only, cheap). Do this FIRST.
Answer with code, not assumption. Each answer changes the plan below.

- **S0.1 (decides this plan's size).** Can a DPU-native service submit a backend-spawn request into the HOST's
  `citus_remote_exec_backend_spawn_v*` region? Trace `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`
  (`homer_frontend_dma_lifecycle.c:192`) and `TupleSinkServiceSubmitBackendSpawnRequest`. Is the region
  reachable over the DMA bridge (is it exported in any mmap descriptor?), or is it strictly host-local
  shm? If strictly local: design the DPU→host spawn submission (new descriptor role + a host-side
  consumer) and size it before committing to Stages 1–4.
- **S0.2.** Does the peer transport (9717) support a command-session peer-open at all today, or only
  tuple-sink/payload opens? Read the peer open handler around `:37385` and enumerate exactly what
  `OP_CLIENT_SQL_SESSION` would need.
- **S0.3.** What does `HomerServiceDpuFindOrCreateSelectedSession` (`:38653`, called `:39099`) track? Can
  it host a full `CLIENT_SQL_SESSION` lifecycle (open / START / POLL / close), or is it
  command-staging-only? If it can, the DPU already has most of a session table and command-plane Stage 2 gets smaller.
- **S0.4.** Which mailboxes does a DPU-opened command session need, and where do they live? `REGISTER_MEMORY`
  unconditionally requires `sessionState->commandMailbox.mailbox` (`:35238`). Is a DMA'd mailbox the
  right answer, or is the check simply wrong for DPU-opened sessions?

**Deliverable:** a written answer per question with `file:line`, and a revised size estimate for Stages 1–4.
**Gate:** none (read-only). **Do not start Stage 1 before Stage 0 lands.**

### Stage 1 — Truth-in-comments (trivial, unblocks everyone reading this code)
- Fix the stale GUC help text (`shared_library_init.c:2596`) — it claims the path "stops after
  bridge-memory setup with an explicit not-implemented error", which is false in DOCA builds.
- Document that `RemoteExecutionRejectDpuFrontendChannelIfSelected` (`homer_frontend_control.c:644`,
  `:721`, `:1099`) guards *unsupported tuple-sink/fallback ops*, while command-session open **bypasses
  it** (`:803`). These are different paths, not a contradiction — say so in both places.
- Comment `HomerFrontendDmaOpenCommandSession` as the L1 opener and name the missing L2/L3 legs.

**Gate:** compiles; no behavior change.

### Stage 2 — Client-side `HomerClientOpenSqlSessionSelectedDpu` (single-node)
Give the client library the L1 opener the PG backend already has. **Single node only, no peer leg** —
this is the smallest change that proves a client can own a command session on a DPU.

- **S2.1** New client entry point in `src/bin/homer_client.c`: combine the SQL command-session request
  fields (`:1558`–`:1575`: `sessionKey.opKind = OP_CLIENT_SQL_SESSION`, `clientSqlResultDpuRelay`,
  `sessionUID`) with the selected-DPU export / control-slot machinery basebackup already uses
  (`:2644`, `:2675`, `:2727`, `:2783`). Emit the open into the DPU control slot, not host SHM.
- **S2.2** Service: admit `OP_CLIENT_SQL_SESSION` arriving over the DPU control slot. Relax
  `TupleSinkServiceOpenRequestNeedsAsyncLocalControl` (`:34817`) and
  `TupleSinkServiceProgressCommandOpenAsyncOp` (`:35172`). Keep the rejection for genuinely unsupported
  opKinds — relax by *allow-list*, not by deleting the check.
- **S2.3** `REGISTER_MEMORY` mailbox requirement (`:35238`): per Stage 0.4, either give the DPU-opened session
  a DMA'd `commandMailbox` or make the requirement conditional. Prefer giving it a real mailbox;
  a conditional check invites a second silent divergence.
- **S2.4** Backend spawn: reuse the existing HOST-side leg unchanged (single-node ⇒ the client and the
  backend are on the same host, so `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest` still applies).
- **S2.5** pgbench: add `--homer-dpu-command` (or fold into `--homer-dpu`; decide once B2.1 exists) to
  select the new opener.

**Gate:** single-node pgbench opens its command session on the LOCAL DPU service, drives a socketless
backend on the same host, and executes the standard TPC-B script correctly at `-c 1`. Verify from the
DPU service log that the command session was created there, and that **no host Homer service is in the
command path**. Basebackup + `--homer-dpu` result relay regressions must both still pass.

### Stage 3 — DPU↔DPU peer command-session open (the net-new leg)
- **S3.1** Relax the peer command-open gates: opKind rejection (`:37385`) and the following mailbox
  checks (`:37395`, `:37406`). Allow-list, as in B2.2.
- **S3.2** The receiving DPU must spawn a socketless backend on ITS host. **This is Stage 0.1.** If no path
  exists, build it: a spawn-request descriptor DMA'd into the host spawn region + a host-side consumer.
  Treat this as its own sub-stage with its own smoke.
- **S3.3** Thread `sessionUID` through the DPU↔DPU command open exactly as the host↔host path does today
  (`session_identity_and_pairing.md:58-62`), so the result relay's existing rendezvous keeps working
  unchanged.

**Gate:** cross-node `pgbench --homer --homer-dpu` from farnet0 to farnet1 with the command session
owned by the DPU services and **no host Homer service running at all**. Correct decoded results at
`-c 1`, then `-c 2`. Basebackup regression.

### Stage 4 — Collapse the split; retire the superseded paths
Only after Stage 3's gate passes.

- **S4.1** The `CLIENT_SQL_SESSION` now lives in the DPU service beside the relay. Re-examine whether the
  `sessionUID` rendezvous is still needed for the role-7 ring. **Expectation: YES, keep it** — the ring is
  still exported by a host process and imported across PCIe, so the rendezvous spans a boundary that does
  not disappear. Do not remove it; confirm in code and record the reasoning.
- **S4.2** Retire `HomerClientOpenSqlSession` (host-SHM command path) for pgbench.
- **S4.3** Retire `citus_remote_exec_pgbench_transaction` + its UDF/extern/build refs (checkpoint "step 8").
  **Now**, not earlier: until command-plane Stage 2 lands, it is the only non-smoke caller of the code this plan promotes.
- **S4.4** The GUC becomes the only path ⇒ remove `citus.enable_experimental_homer_dpu_frontend` and its
  gates, or flip its default and keep it as a kill switch. Decide with a measurement, not a preference.

**MUST NOT touch, at any stage** (basebackup calls the first unconditionally; `--homer-dpu` result
delivery rides the third): `HomerClientOpenBaseBackupStreamSelectedDpu` (`homer_client.c:2551`, from
`basebackup_homer.c:289`), `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:2922`),
`HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`, via `:3925`, `pgbench.c:9559`).

---

## Trust tiers — "compiles" is not "works" (added July 9, 2026, after a third instance)

The project owner's guidance: **the Citus-backend-as-frontend path is most likely outdated. Do not
assume it is correct.** Earlier today a read-only trace concluded `HomerFrontendDmaOpenCommandSession`
is "not a stub" and should be KEPT AND PROMOTED. That conclusion is **retracted**. "Not a stub" only
ever established that the code exists and compiles. Its only consumers are `homer_frontend_dma_smoke.c`
and the deprecated `citus_remote_exec_pgbench_transaction` UDF. Its GUC help text still claims the path
stops with a not-implemented error — which is as easily read as "nobody has looked at either in a
while" as it is "the code is newer than the docs".

Classify every component before depending on it:

**TIER 1 — EXERCISED.** Some validated workload runs this today; changes to it are regression-testable.
- client-library selected-DPU machinery in `homer_client.c`
  (`HomerClientOpenBaseBackupStreamSelectedDpu`, `...ReceiveStreamSelectedDpu`,
  `HomerClientOpenSqlResultReceiveStreamSelectedDpu`) — cross-node DPU basebackup moves ~23 GB
  through it;
- the DPU service's setup-TCP + `HomerDpuDmaImportHostMmapDescriptorForSetup` import path;
- the DPU↔DPU RDMA peer transport for payload;
- the backend-spawn region and its poller (every `--homer` pgbench run forks socketless backends
  through it).

**TIER 2 — EXISTS, UNEXERCISED.** Reachable only from a smoke or the UDF. Treat as documentation of a
mechanism, not as a component. Includes `homer_frontend_dma.c`, `homer_frontend_dma_lifecycle.c`, the
GUC-gated sites in `homer_frontend_control.c`, `citus_remote_exec_pgbench_transaction`, **and the
`backendChannelMode = SELECTED_DPU_DMA` handling inside `remote_execution_backend_bridge.c`.**

**Consequence for this plan.** The client-side `HomerClientOpenSqlSessionSelectedDpu` must be built on
the **Tier-1 basebackup template**, not by extracting or promoting the Tier-2 server-side opener. And an
earlier claim in this project — "the receiving half [SELECTED_DPU_DMA] is already implemented and
honored" — is an overclaim: the branch exists; nothing validated drives it. If it is stale, Leg 2 grows
by however much of the backend-side mailbox handling must be rewritten rather than reused.

This is the same failure mode as the `mode=rdma` runbook example, the June-6 COPY baseline, and the
"advisory" cache comment: **a written artifact (code, doc, or comment) that describes a path was
trusted as evidence the path works.** Nothing executes documentation, and nothing executes an
unexercised branch either.

## Standing risks

1. **"Working code with no validated consumer" is not dead code.** The selected-DPU backend-spawn spine
   compiles, is exercised only by `homer_frontend_dma_smoke.c:118`, and looks exactly like scaffolding.
   Basebackup does NOT use it (its selected-DPU open is `OP_TUPLE_SINK` / `sessionKey.opKind =
   OP_BASE_BACKUP`, `homer_client.c:2795`/`:2809`). Audit by "what would call this when the migration
   finishes", not by "what calls this today".
2. **Regression sweep.** Three regressions landed July 6–8 unnoticed because each validation built one
   target and ran one workload. Before and after every stage here: build ALL targets (`service-bin`,
   `client-bin`, every smoke) and run basebackup + `--homer-dpu` + COPY.
3. **Baselines need a commit SHA**, not a date. A June-6 COPY baseline was cited as evidence that COPY
   works today; it does not (see the regression doc).
4. **Error propagation is a debuggability tax.** An aborted byte-ring stream hangs the client with no
   error. Every fault on this path arrives as a hang. Fix before the next hard bug, not during one.
