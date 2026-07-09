# DPU command-plane migration — detailed plan

> **Naming.** A SEPARATE workstream from the byte-ring pool plan, not its "Stage 3". The pool plan owns
> data-plane *ring ownership*. This doc owns the *command plane*. Stages here are numbered
> independently — cite them as "command-plane S1a", etc.

**Status (July 9, 2026): PLANNED, agreed with the user, not started. This is a PREREQUISITE for
`pgbench --homer-dpu`, not a follow-on.**

---

## Intent (stated by the user)

> The client runs on a separate node from the PG server/backend. Both the client binary and the server
> Postgres binary use the **Homer frontend** to send and receive commands / completions / tuple results.
> No libpq. Two nodes talking to each other **through their own DPU**, which performs DMA to and from
> the host (Homer frontend).

The Homer *service* IS the DPU-native service. **Host Homer services are bring-up scaffold, not the
target** (plain `--homer`, the non-DPU path, keeps them).

## The wall, and why it is architectural

The DPU DMA engine's mirrored ranges come ONLY from `engine->hostMmapImports`, populated solely by
`HomerDpuDmaImportHostMmapDescriptorForSetup` (`homer_service_dpu_dma.h:245`) — i.e. by a **host
frontend exporting its ring to the engine over the DPU TCP setup socket**. The engine exists to import
HOST memory across PCIe. **A host-resident service has nothing to import.**

Today `pgbench` opens its command session via `HomerClientOpenSqlSession(HomerClientControl *)`
(`pgbench.c:9535` -> `homer_client.c:1518`), a mapped HOST-service control SHM. Only the SQL *result*
receive ring is selected-DPU, bound after the command open (`pgbench.c:9559`). Payload streams are
created by the service that owns the session, so the `--homer-dpu` result stream is born in the HOST
service, whose engine can never hold an import for it. Observed three times: farnet1 binds a
`purpose=0` MIRROR slot, then silence — `HomerDpuDmaMirroredByteRangeReadyForServiceSink` iterates an
empty import table, egress never fires, and the client hangs on its first `SELECT`.

**A Homer frontend is, definitionally, the host process that exports its rings to the DPU beside it.**
The host service was never a frontend. That is the whole bug.

## Trust tiers — "compiles" is not "works"

Classify every component before depending on it. (Fourth instance of this failure mode today, after the
`mode=rdma` runbook example, the June-6 COPY baseline, and the "cache is advisory" comment: *a written
artifact describing a path was trusted as evidence the path works*.)

**TIER 1 — EXERCISED.** Some validated workload runs it today; breaking it would be noticed.
- Client-library selected-DPU machinery in `homer_client.c` (`HomerClientOpenBaseBackupStreamSelectedDpu`,
  `...ReceiveStreamSelectedDpu`, `HomerClientOpenSqlResultReceiveStreamSelectedDpu`) — cross-node DPU
  basebackup moves ~23 GB through it.
- The DPU service's setup-TCP + `HomerDpuDmaImportHostMmapDescriptorForSetup` import path.
- The DPU<->DPU RDMA peer transport for payload, and the `CRITICAL_CONTROL` class its peer-opens ride.
- The backend-spawn region and its postmaster hook (every `--homer` run forks socketless backends
  through it).

**TIER 2 — EXISTS, UNEXERCISED.** Reachable only from a smoke or the deprecated UDF. Documentation of a
mechanism, not a component. `homer_frontend_dma.c`, `homer_frontend_dma_lifecycle.c`, the GUC-gated
sites in `homer_frontend_control.c`, `citus_remote_exec_pgbench_transaction`, **and the
`backendChannelMode = SELECTED_DPU_DMA` handling in `remote_execution_backend_bridge.c`**
(`:2572`, `:2625`, `:2637`, `:2646`, `:2655`, `:2669`).

An earlier revision of this doc said `HomerFrontendDmaOpenCommandSession` should be "kept and promoted".
**Retracted.** It compiles; nothing validated drives it; its GUC help text still claims the path is
not-implemented. Build on the Tier-1 client template instead.

---

## Target architecture — symmetric frontends

| Process | Exports to its LOCAL DPU |
|---|---|
| `pgbench` (node A) | command ring, completion ring, role-7 result ring |
| socketless PG backend (node B) | command mailbox (role 2), completion mailbox (role 3), result byte-ring |
| postmaster (node B) | the backend-spawn region (once, at startup) |

The two DPU services talk RDMA to each other. No host Homer service is in the `--homer-dpu` path.

Bridge descriptor roles for the backend mailboxes **already exist** in the ABI: role 2 = backend
command, role 3 = backend completion (`homer_dpu_bridge_abi.h:81-82`, shapes at `:290`, `:295`).

---

## Design decisions

### D1 — the spawned backend becomes a Homer frontend (INVERTS the Tier-2 arrangement)
Under today's (Tier-2) `SELECTED_DPU_DMA`, the **frontend** creates and exports the mailboxes
(`homer_frontend_dma_lifecycle.c:103`, shm created at `:137`/`:146`/`:155`; exported at
`homer_frontend_dma.c:1269`/`:1273`/`:1277`) and the **backend merely `shm_open`s them**
(`remote_execution_backend_bridge.c:2637`-`:2669`).

Cross-node that cannot work: the frontend is on node A, the backend on node B. So **invert it** — the
backend CREATES its mailboxes and its result byte-ring, and EXPORTS them to its own local DPU. Since
the Tier-2 code is unexercised, rewriting costs little and buys the symmetry above.

*Rejected:* have node A's client create node B's mailboxes. Impossible — POSIX shm is node-local, and
the backend must `shm_open` by name on its own host.

### D2 — spawn trigger: postmaster exports the region; DPU DMA-writes the slot; doorbell over the existing setup socket
**The postmaster OWNS the spawn region.** It creates it: `shm_open(O_CREAT|O_RDWR)`
(`remote_execution_backend_bridge.c:1709`), `ftruncate` (`:1730`), `mmap` (`:1740`), initialises
`slotCount`/`postmasterPid` (`:1758`). Region name `/citus_remote_exec_backend_spawn_v14`
(`remote_execution_backend_protocol.h:28`), fixed slot count (`:30`), slot states (`:47`).
**So there is no "DOCA-export a region you do not own" problem** — an earlier note claimed there was;
corrected. The postmaster calls a Homer frontend API and exports its own region.

**The poller is a SIGUSR1 hook, not a poll loop.** Installed at `:3111`; entered at `:3081`; scans
slots at `:3083`; waits on `slot->state == CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_REQUEST_READY` at
`:3091`; forks via `ProcessSpawnRequestSlot` at `:3097`. Today's submitters write the fields, store
`REQUEST_READY`, then `kill(postmasterPid, SIGUSR1)` (`tuple_sink_service_process.c:18833`, `:18845`,
`:18924`, `:18927`).

**A DPU cannot signal a host process.** But the postmaster already blocks in a `select()`-style main
loop, and the DPU **setup TCP socket is already open** between the postmaster (frontend) and its local
DPU. So:

1. Postmaster calls a Homer frontend API at startup: DOCA-mmap its own spawn region and export it over
   the setup TCP; keep that socket open.
2. Add the setup socket fd to the postmaster's wait set.
3. The DPU service DMA-writes the spawn request fields, then `REQUEST_READY` (**fields before the state
   word, with a release barrier** — the existing submitters rely on that ordering), then writes one
   doorbell byte on the setup socket.
4. The postmaster wakes on the fd and runs the **existing** slot scan + `ProcessSpawnRequestSlot`.

*No polling, no sleep, no background worker; the SIGUSR1 hop is replaced rather than emulated.*
*Rejected:* a bgworker or standalone agent polling a doorbell word and raising SIGUSR1 — an extra
process and a poll loop to solve what an already-open fd solves. *Rejected:* shortening the
postmaster's `select()` timeout — polling by another name.

### D3 — the client opener is built on the Tier-1 basebackup template
`HomerClientOpenSqlSessionSelectedDpu` in `homer_client.c`, cloned from the exercised selected-DPU
setup: env/default parsing (`:2551`), buffer layout with host publish lines / DPU credit lines / control
slot (`:2560`), aligned export buffer (`:2647`), bridge header (`:2670`), DOCA mmap export (`:2690`,
via `:2011`/`:2028`/`:2044`), descriptors (`:2716`, role-1 frontend control at `:2724`, `:2736`), setup
TCP (`:2145`, `:2782`), control-slot submit (`HomerClientDpuSubmitControlRequest`, `:2233`).

Add the SQL command-session request fields currently built only for host SHM:
`CITUS_REMOTE_EXEC_CONTROL_OP_COMMAND_SESSION` (`:1558`), `clientSqlResultDpuRelay` (`:1560`),
`sessionUID` (`:1567`), db/user/opKind (`:1573`-`:1575`), peer endpoint (`:1584`).

**`backendCpu` gap:** it is not part of `HomerClientOpenSqlSession` today; it appears only in spawn
requests (`remote_execution_backend_protocol.h:112`). The DPU-native open must either carry it and have
the DPU forward it into the spawn request, or fall back to service policy. **Decide in S1a.**

---

## Stages

### S0 — Spike (sanity, not a gate)
Standalone test: a process `shm_open`s + `mmap`s a POSIX shm region **it created**, DOCA-exports the
mapping to the DPU, the DPU DMA-writes into it, the host reads the value back. Confirms the export path
for D2. Do it because it costs minutes, not because the design depends on the answer.

### S1a — client-side `HomerClientOpenSqlSessionSelectedDpu` (clone the Tier-1 template)
- **S1a.1** New entry point in `src/bin/homer_client.c`. Export buffer carrying: frontend control slot
  (role 1), command ring, completion ring, role-7 result ring. Emit an `OP_COMMAND_SESSION` open into
  the DPU control slot.
- **S1a.2** Service: admit `OP_CLIENT_SQL_SESSION` arriving over the DPU control slot. Relax by
  **allow-list**, not by deleting checks: `TupleSinkServiceOpenRequestNeedsAsyncLocalControl`
  (`tuple_sink_service_process.c:34817`) and `TupleSinkServiceProgressCommandOpenAsyncOp` (`:35172`).
- **S1a.3** `REGISTER_MEMORY` unconditionally requires `sessionState->commandMailbox.mailbox`
  (`:35238`). Under D1 the backend supplies a real mailbox, so **prefer satisfying the check over
  relaxing it** — a conditional check invites a second silent divergence.
- **S1a.4** Resolve the `backendCpu` gap (D3).
- **S1a.5** pgbench: select the new opener (fold into `--homer-dpu`, or a separate flag — decide once
  S1a.1 exists).

**Gate:** compiles; the DPU service logs an `OP_COMMAND_SESSION` open arriving over the DPU control
slot. No end-to-end claim yet.

### S1b — extract the shared Homer-frontend export module
Now load-bearing, because **three** processes need it: the client library, the socketless backend (D1),
and the postmaster (D2). Extract from the Tier-1 `homer_client.c` machinery — **not** from the Tier-2
`homer_frontend_dma.c`.

Surface: "open a control channel to my local DPU; export these regions; submit this control slot;
receive this response." Must be linkable into both `libhomer_client.a` and the PostgreSQL backend.

**Gate:** the selected-DPU basebackup regression still passes (the module's only validated consumer at
this point). Build all targets, including the smokes.

### S2 — backend becomes a Homer frontend (D1)
- **S2.1** The spawned socketless backend CREATES its command mailbox, completion mailbox, and result
  byte-ring (rather than `shm_open`ing frontend-created ones).
- **S2.2** It exports all three to its LOCAL DPU via the S1b module, using bridge roles 2 and 3 for the
  mailboxes and the existing host->DPU payload role for the result ring.
- **S2.3** Retire the Tier-2 `SELECTED_DPU_DMA` mailbox-open path in
  `remote_execution_backend_bridge.c` (`:2572`-`:2669`) — superseded, and never exercised.

**Gate:** the DPU service's engine shows a host mmap import for the backend's result ring
(`HomerDpuDmaImportHostMmapDescriptorForSetup`) — the exact thing whose absence caused the three-run
hang.

### S3 — spawn trigger (D2)
- **S3.1** Postmaster: at startup, DOCA-mmap its own spawn region and export it over the setup TCP;
  keep the socket.
- **S3.2** Postmaster: add the setup socket fd to its main-loop wait set; on readable, run the existing
  slot scan.
- **S3.3** DPU service: replace `TupleSinkServiceSubmitBackendSpawnRequest`'s `shm_open`
  (`tuple_sink_service_process.c:18833`+) with a DMA write into the imported spawn region — **fields
  first, then `REQUEST_READY` with a release barrier** — then one doorbell byte on the setup socket.
- **S3.4** Keep the host-service `shm_open` submitter intact for the non-DPU `--homer` path.

**Gate:** a DPU service causes a socketless backend to be forked on its own host. Assert the ordering
(fields before state word) with a debug check.

### S4 — SINGLE-NODE end-to-end (the de-risking gate)
Client, PostgreSQL, and ONE DPU on the same host. **No peer leg, no RDMA, no second DPU.** The client
opens its session on the local DPU; the DPU triggers the spawn; the backend exports its mailboxes to the
same DPU; commands, completions and tuple results all flow host<->DPU by DMA.

**Gate:** a `SELECT` returns correct decoded values. Validates D1+D2+D3 together with the smallest
possible blast radius, and is the first proof the architecture works at all.

### S5 — DPU<->DPU command peer-open
Smaller than feared: `CRITICAL_CONTROL` is already the class basebackup's DPU<->DPU peer-opens ride
(`remote_execution_peer_transport_rdma.h:73-75`; validation at `.c:2459`; command peer-open uses it at
`tuple_sink_service_process.c:35316`, `:35427`, `:35430`). The transport is Tier-1; what is unvalidated
is an `OP_COMMAND_SESSION` over it.

- **S5.1** Relax the peer command-open gates by allow-list: opKind rejection (`:37385`) and the
  following mailbox checks (`:37395`, `:37406`).
- **S5.2** Thread `sessionUID` through the DPU<->DPU command open exactly as the host<->host path does,
  so the result relay's existing rendezvous keeps working unchanged.
- **S5.3** The receiving DPU triggers the spawn on ITS host via S3.

**Gate:** a cross-node `OP_COMMAND_SESSION` reaches the remote DPU, spawns a backend, and a completion
comes back.

### S6 — `pgbench --homer --homer-dpu -c 1`, cross-node
**Gate:** 200 transactions, zero failures, sane / non-constant decoded `abalance`, and no host Homer
service anywhere in the command path. Then `-c 2`, which unblocks byte-ring pool Stage 2
(`tupleSourceRing` per-session).

### S7 — retire the superseded paths
Only after S6.
- **S7.1** `HomerClientOpenSqlSession` (host-SHM command path) for pgbench.
- **S7.2** `citus_remote_exec_pgbench_transaction` + its UDF/extern/build refs (checkpoint "step 8").
- **S7.3** The Tier-2 `homer_frontend_dma*` path and its GUC, now genuinely superseded by S1b.
- **S7.4** Re-examine whether the `sessionUID` rendezvous is still needed. **Expectation: YES, keep it**
  — the role-7 ring is still exported by a host process and imported across PCIe, a boundary that does
  not disappear when the session table moves. Confirm in code; record the reasoning.

**MUST NOT touch, at any stage** (basebackup calls the first unconditionally; `--homer-dpu` result
delivery rides the third): `HomerClientOpenBaseBackupStreamSelectedDpu` (`homer_client.c:2551`, from
`basebackup_homer.c:289`), `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:2922`),
`HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`).

---

## Open questions

1. **`backendCpu`** (D3): carry it through the DPU control slot into the DPU-built spawn request, or use
   service policy? Decide in S1a.
2. **Does the socketless backend need DOCA at startup?** Under D1 it must export its rings, so yes. That
   adds a DOCA dependency to every Homer backend. Acceptable per the stated intent, but it means a
   backend cannot start if DOCA init fails — decide the failure policy.
3. **Result byte-ring ownership.** Today the service creates the producer `sendQueue`. Under D1 the
   backend must own and export it. Audit what else reads `stream.sendQueue` on the service side.
4. **Postmaster wait-set surgery** (S3.2) is a Postgres-fork change. Confirm where `ServerLoop` builds
   its fd set, and that adding one fd is safe with respect to `DetermineSleepTime()`.

## Standing risks

1. **"Working code with no validated consumer" is not dead code, and it is not trustworthy either.**
   Audit by "what would call this when the migration finishes", then check whether anything calls it
   *today*. Both answers matter.
2. **Regression sweep.** Three regressions landed July 6-8 unnoticed because each validation built one
   target and ran one workload. Before and after every stage: build ALL targets (`service-bin`,
   `client-bin`, every smoke) and run basebackup + `--homer` + COPY.
3. **Baselines need a commit SHA**, not a date.
4. **Error propagation is a debuggability tax.** An aborted byte-ring stream hangs the client with no
   error; it has now cost three separate investigations. Fix before the next hard bug.

## Related
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) — the descriptor split,
  and the three `--homer-dpu` bring-up runs that exposed the wall.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) — pool Stage 2 is
  blocked behind S6.
- [session_identity_and_pairing.md](../../../future-directions/citus/transport/session_identity_and_pairing.md)
  — `sessionUID` as the cross-boundary binding key; survives this migration.
