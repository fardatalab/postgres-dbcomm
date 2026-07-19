# DPU command-plane migration — detailed plan

<!-- kb-summary: Detailed staged plan and decisions for moving the Homer SQL command plane onto the DPUs. -->

> **Naming.** A SEPARATE workstream from the byte-ring pool plan, not its "Stage 3". The pool plan owns
> data-plane *ring ownership*. This doc owns the *command plane*. Stages here are numbered
> independently — cite them as "command-plane S1a", etc.

> ## ✅ CURRENT STATUS (2026-07-19) — read THIS box first; the July-13 box below is now HISTORY for S6/S7.
>
> **S0–S5 DONE. S7 (host-service retirement) COMPLETE. S6 is the remaining open stage of this plan.**
>
> - **S7 — ✅ COMPLETE + VALIDATED.** S7.0 (postgres `22a8f9951e6`) + S7.1(a–e) (citus `effd2678d` / `96112ddc6` /
>   `e9db38781` / `e9dd4676c`) landed, then S7.2 (delete the ① `RemoteExecutionSession` API) + S7.3 (delete the
>   Tier-2 DMA frontend) executed as the s7-plan §13 "Stage 4" R1/R2/R3 (citus `39bed1051` / `a12c48667` /
>   `31f0e958f`; final run `candidate-20260719-161556`, first-spawn `slot=0` proving **D7 → D7′**: the DPU is now the
>   SOLE backend-spawn claimant across the whole region). Detail:
>   [`s7_host_service_retirement_plan.md`](./s7_host_service_retirement_plan.md) §13; the ①-promotion detour it
>   triggered reached its endpoint —
>   [`../../future-directions/citus/transport/homer_unified_session_core_plan.md`](../../future-directions/citus/transport/homer_unified_session_core_plan.md).
> - **S6 (native `--homer-dpu` bring-up + per-command latency) — IN PROGRESS, the active frontier.** Landed: Track A
>   fail-close of the peer host-spawn arms (citus `81ce2577c`); Track B default-off DPU-local discovery-latency spans
>   (citus `f5f4480f3`); Stage 2 per-session DPU SOURCE ring, retiring the `tupleSourceRing` singleton (citus
>   `a5e7d2fdb` + `3d5047146` — this is also byte-ring-pool-plan Stage 2); and Stage 2c send-CQE selective signalling
>   / coalescing (citus `7e08343f2`), the "measure the intended system" prerequisite the owner sequenced BEFORE the
>   S6 number. NOT yet closed: end-to-end native `--homer-dpu` bring-up and the attributable S6 latency measurement.
>   Detail: [`s6_native_homer_dpu_and_latency_plan.md`](./s6_native_homer_dpu_and_latency_plan.md).
> - **Still DEFERRED:** unified-core Stage 5 (COPY as the third dispatcher consumer + backend pooling / reuse), until
>   the COPY path is actually (re)designed.
>
> ## ⚠ CURRENT STATUS (July 13, 2026) — the July-10 paragraph below is HISTORY. Read this box first.
>
> **S0–S5 are ✅ DONE. The cross-node DPU gate (`pgbench --homer --homer-dpu-command`) PASSES** and is the
> project's standing regression gate (CLAUDE.md). **S4+S5 landed** (the "remaining" note below is stale).
>
> **The resource-retirement P0 set is COMPLETE** (`resource_retirement_contract_audit.md` — see its status box),
> so **P7b is unblocked**.
>
> ### ⇒ **S6 IS UNBLOCKED** — both stated prerequisites are now met (2026-07-15):
>
> - **"fix B" (per-collector scheduler feedback) — ✅ DONE + VALIDATED** (FB-1, citus `b9c8e513f`). The DPU-DMA
>   collector family is now per-collector (`dpuDmaFeedbacks[]`), so the discovery collector is no longer
>   starved by siblings' grants and an S6 cadence/latency number is attributable and survives re-measurement.
>   Mechanism + result: **[`dpu_collector_feedback_aliasing_defect_b.md`](./dpu_collector_feedback_aliasing_defect_b.md)**.
>   *(Residual, NON-blocking: FB-2 — the `peerSendCqFeedback` `+` cluster — folds into the send-CQE coalescing work.)*
> - **§48a (the teardown `exit(1)` that could contaminate S6's SIGTERM) — ✅ DONE + VALIDATED** (citus `2cf3636c8`;
>   audit §36.10).
>
> **⇒ OWNER DECISION (2026-07-15): finish the send-CQE coalescing FIRST, then S6.** So S6 measures the intended
> (coalesced) send path rather than a pre-coalescing number coalescing would immediately change — the same
> "measure the intended system" reasoning that gated the S6 number on fix B. Design + implementation plan:
> **[`../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`](../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md)**.
>
> ### ✅ S7.1's "capture the cross-node `--homer` baseline first" precondition is WAIVED (owner, July 13, 2026)
>
> It was written in June, when the host-service path still worked. **You cannot baseline a corpse:**
> `pgbench --homer` remote-RDMA is 🔴 broken at HEAD and the standing decision is not to fix it, because
> host-service Homer is being deleted. The purpose the baseline was meant to serve — *"do not gut a path until
> its replacement has a regression net"* — is **already served by the DPU gate**, which is green and is the
> project's standing net. Waiving costs nothing. **Struck.**
>
> ### ✅ **S7.0 — LANDED + VALIDATED** (postgres `22a8f9951e6`, July 13, 2026)
>
> **The gate passed with BOTH host services DOWN**, on a machine that still had the stale
> `/dev/shm/citus_remote_execution_control_v27` that had faked earlier passes (found and removed by that same
> validation). The anti-fallback property is now **structural**: with no control region open, a silent fall back
> to the host-service arm **cannot even be expressed**. The old asymmetric "client-side UP, backend-side DOWN"
> rule is **RETIRED** — start neither.
>
> ⇒ **S7.1 (delete host-service Homer) is UNBLOCKED.** Its other precondition (the cross-node `--homer`
> baseline) was waived above.
>
> *The description below is kept as the rationale — it is why S7.0 was needed, not a statement that it is
> pending.*
>
> **The DPU gate's own client boots through the host-service control region.** `HomerClientOpenControl`
> (`homer_client.c:400`) is `shm_open` with **no `O_CREAT`** — the region is *created by
> `citus_tuple_sink_service`* and only *mapped* by the client — and `pgbench.c` called it under
> `if (homer_mode)`, which `--homer-dpu-command` also sets. But the selected-DPU session open
> (`HomerClientOpenSqlSessionSelectedDpu`, `pgbench.c:9800`) **does not take a control region at all**; only the
> host-service `HomerClientOpenSqlSession` (`:9814`) does. **So under `--homer-dpu-command` the mapping was
> opened, never used, and closed.**
>
> **You cannot delete the host service while the gate's client still boots through a region only that service
> creates.** S7.0 gates the `HomerClientOpenControl` call on `!homer_dpu_command_mode`, mirroring the
> session-open fork, plus a loud guard if the two forks ever disagree.
>
> **It also kills an accident class.** That dead mapping is precisely why gate runs 13–16 passed with the
> farnet0 host service **down**, satisfied by a *stale* `/citus_remote_execution_control_v27` (the gate's
> "trap 2" in CLAUDE.md). With no control region open, **a silent fall back to the host-service arm cannot even
> be expressed** — the anti-fallback property becomes structural instead of procedural.
>
> **Validation:** run the gate with **BOTH** host services down. It must pass. That is a stronger anti-fallback
> proof than the current runbook's asymmetric "client-side UP, backend-side DOWN" dance, which it retires.

**Status (July 10, 2026): IN PROGRESS. S0 ✅, S0b ✅, S1a ✅, S1b ✅, S2 ✅ (citus `008f4f20b`),
S3.0 ✅ (citus `36a61a5a1`), S3.1 ✅ (citus `c2ec17852`), S3.1b ✅ (citus `84ac374ef`).
Post-S3.1b comment-only audit `63b0df0a7` records the second invisible demand and sharpens S3.2's
cadence rule.
S5 turned out to be already working — see "S5 IS ALREADY WORKING" below.
S3.2 ✅ (citus `52fd1ab3d`), S3.3 ✅ (citus `455c7a556`), **S3.3b + S3.2c ✅ VALIDATED (citus `30dfc9e9a`,
July 10, 2026)** — the DPU allocates a frontend-arena slot, the spawned backend binds it and SURVIVES
(the S3.3 gate ERROR is retired). **S3.2b ✅ (citus `f04f87715`)** — arena-slot reaper. **S3.4 ✅ (citus
`81cb0ec45`)** — doorbell-EOF import teardown. **S3.5 ✅ (citus `89820eb36`)** — host-arm deprecation LOG.
**The S3 spawn-trigger stage is COMPLETE.** Remaining: **S4+S5 MERGED → CROSS-NODE end-to-end**
(user-confirmed July 10, 2026: Homer is RDMA-only, no single-node gate). The gate is a cross-node
real-pgbench SELECT (client node A → backend node B, both through their DPUs). Detailed blueprint:
[dpu_crossnode_command_completion_bridges_design.md](./dpu_crossnode_command_completion_bridges_design.md).
Net-new work = two RDMA relay halves (command + completion) on the already-built substrate; results
(role-5→role-7) and peer OPEN/spawn already built. Then **fix B** / **refine A** (D10) before **S6**.

> **Decisions layered on top of the original plan. Read these before implementing anything.**
> - **D5** — `HomerDpuDmaRingRuntime.boundServiceSessionId`. The descriptor says what the host *declared*;
>   the DPU records what it *bound*. Arena rings are declared before any session exists. **It is also the
>   reverse cookie** (`resource → waiter`) — see the scheduler thread below.
> - **D6** — the forward index (`waiter → resource`). A session caches refs to its own rings, built at bind.
>   Supersedes D5's `continue`; kills survey findings 2, 6 and 7. Built in S3.3b, cashed in at S4.0.
>   **It is continuation-graph edge #1.**
> - **D7** — spawn-slot producer partition. **TRANSITIONAL**; expires at S7.1 into **D7′** (exactly one
>   claimant).
> - **D8 / S3.0** — reserve `hostPublishLines[48]` in the arena **now**, while the arena ABI is still `v1`
>   and only S2 has shipped against it. 3 KiB on a 57 MiB region; no writers, no readers. **⏳ Closing
>   window:** later it costs an ABI bump and a synchronised two-DPU redeploy. ✅ **DONE (citus `36a61a5a1`).**
> - **D8b / S3.0** — reserve `dpuCreditLines[48]` as well, and leave `bridgeHeader.dpuCreditOffset` at **0
>   deliberately**. Unlike `hostPublishOffset` (never read), that offset IS read, hard-gates three submit
>   paths, and is a DMA *write destination*. Turning it on before the role-5 descriptor rebase is silent
>   memory corruption. **S4.0b** does both as one edit, and it is a hard prerequisite for a backend
>   producing result bytes. ✅ **Reserved (citus `36a61a5a1`).**
> - **D10 / S3.1b** — **a listener is admitted by a POLL-DUE OBLIGATION, not by a readiness claim.** Its own
>   collector, action and progress source; a durable `pollDue` bit cleared and rearmed only when the poll
>   runs; **reserved admission** that generic collector budgets cannot consume. `knownExpectedWork` then
>   means *"a poll is due"* — true and maintained — rather than *"an item is ready"*, which a listener
>   cannot know without a syscall. ✅ **VALIDATED in citus `84ac374ef`.** Ordering: **S3.2 → fix B →
>   refine A.** ⚠ S3.2's doorbell must copy the now-working setup-listener implementation.
> - **⏭ Ordering revisited after S3 completed (July 10, 2026) — do S4 BEFORE fix B / refine A.** D10's
>   "S3.2 → fix B → refine A" ordering predates S3.3+. Re-examined now that S3 is done, the recommendation is
>   **S4 → fix B → refine A → S6**, for three reasons: (1) **Fix B is a MEASUREMENT prerequisite, not a
>   functional one** — its stated deadline is *"before any S6 number"* (it de-aliases the shared
>   `dpuDmaFeedback` so per-collector cadence is attributable); nothing in S4's functional work (D6 forward
>   index, arena credit lines, role-6 export, driving commands) depends on it, and S4's collectors are
>   exact-work-armed, not blind-backoff, so the aliasing barely touches S4 debugging (and starve-diag
>   counters cover cadence questions meanwhile). (2) **S4 restructures the DPU collectors** — S4.0 cashes in
>   D6 and DELETES the completion-pull scan, changing the very collector set fix B must rework; doing fix B
>   first would churn it, doing it after lets it target the final set. (3) **S4 is the functional payoff** —
>   it makes `--homer-dpu-command` complete a `SELECT` (today it fails at `client_sql_tx_begin`), proving the
>   architecture end to end; fix B/refine A are invisible scheduler plumbing that only gate S6 numbers, which
>   come after S4 regardless. refine A (enrolled-ring bitset, roles 1/4/7) is orthogonal to S4's arena
>   collectors and naturally pairs with fix B.
> - **S3.5's discovery** — basebackup never spawns a socketless backend, so `pgbench --homer` is the *only*
>   working regression net for the spawn path. Do not retire it before its replacement's gate passes.
> - **D9 / the spawn wait — RETRACTED AND CORRECTED.** On the DPU the spin is **not a stall, it is a
>   self-deadlock**: the DMA read that would deliver `RESPONSE_READY` completes only via
>   `doca_pe_progress()`, which the spinning thread is the only one that can call. **S3.7's async responder
>   is folded into S3.3** — there is no interim. Two-phase read (**D9b**): poll the aligned 4-byte `state`
>   word alone; only after that completes, fetch `response`; then write `FREE`.
> - **D9b's corollary, now written into the code** — `HomerDpuBridgeHostPublishLine`'s 64-byte DMA read rests
>   on a **standing platform assumption: ascending-order source fetch**, not on line atomicity and not on
>   per-field atomicity. Keeping the publication word at offset 0 is what makes tearing *conservative*.

> ## 🧭 This plan feeds a much larger scheduler overhaul. Know that before you touch D5, D6, D8 or S4.0.
>
> Three decisions here turned out to be the first hand-built pieces of the async-continuation runtime. The
> *decisions* live in this doc; the *constraints* live in these:
>
> - [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md)
>   — **the root cause.** Everything is polled; a completion queue buys only *aggregation* + *a cookie slot*;
>   the DPU's ready queue is a hand-rolled CQ **missing the cookie**. Defines the forward index (= D6) and the
>   reverse cookie (= D5's field), shows where `HomerDependencyResolve()` lands, and is the source of D8.
>   It also **corrects fact F2 below** (`dpuCreditOffset` *is* read).
> - [`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
>   — the survey of *arm from exact demand, execute by linear scan*. Findings 2/6/7 **are** D6. Finding 1
>   sits on the basebackup egress path and is **unscheduled**.
> - [`homer_continuation_graph_scheduler_plan.md`](../../../future-directions/citus/transport/homer_continuation_graph_scheduler_plan.md)
>   — the destination.
>
> **Do not "fix" the ten scan sites one at a time.** They are one missing edge, ten times.

> **Two decisions supersede the original plan. Read both before implementing S2/S3.**
> - **D2′ — the doorbell agent bgworker.** The doorbell socket *and* the DOCA export live in a
>   `shared_preload_libraries` background worker, **not** the postmaster. **Zero PostgreSQL core
>   changes**, because `postmaster_sigusr1_hook` already exists in our fork and is Tier 1. The original
>   D2 (postmaster owns the fd; new hook into `pm_wait_set`) is **retracted**.
> - **D4′ — the arena is named POSIX shm, addressed by offset.** Backends `shm_open` it; they need not
>   inherit it by `fork()`. Exporter (agent) and readers (postmaster, backends) are different processes
>   at different VAs aliasing the same physical pages. **That aliasing is the single load-bearing
>   assumption of the design — prove it in S0b before writing a line of S2.**

**Runbook for the S1a gate** (all four roles, farnet0 as the client node):
```sh
# farnet0 DPU (client's local DPU) and farnet1 DPU (peer), each as `ubuntu`:
setsid nohup env HOMER_SERVICE_ENABLE_DPU_DMA=1 HOMER_SERVICE_ENABLE_DOCA_DMA=1 \
  HOMER_SERVICE_DPU_SETUP_PORT=9727 HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.200 HOMER_SERVICE_PEER_PORT=9717 \
  ~/dbcomm/citus-dbcomm/build/homer/citus_tuple_sink_service </dev/null > /tmp/svc.log 2>&1 &
#   (farnet1 DPU: PEER_BIND_HOST=10.10.1.201)

# farnet0 host: a host service must still run -- pgbench's HomerClientOpenControl maps its
# control SHM at thread start. It handles ZERO sessions. S7.1 removes this dependency.

# farnet0 host, the client:
env HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
    HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
  pgbench -h /tmp -p 5432 -U dbcomm --homer --homer-dpu-command \
    --homer-database-oid 5 --homer-user-oid 10 \
    --homer-peer-host 10.10.1.201 --homer-peer-port 9717 --homer-peer-node 1 \
    --client-cpu=3 -n -M simple -c 1 -j 1 -t 1 postgres
```
The peer host is the **peer DPU** (`10.10.1.201`), not the peer host service — same rule as the
validated 4-role basebackup topology. Each frontend points at its OWN local DPU.

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

**Every descriptor role this architecture needs ALREADY EXISTS** (`homer_dpu_bridge_abi.h:78-95`) —
a strong sign the design was anticipated. Nothing new is needed in the bridge ABI:

| Role | Meaning | Direction |
|---|---|---|
| 1 | `FRONTEND_CONTROL_SLOT` | DPU pulls the client's control/command requests |
| 2 | `BACKEND_COMMAND_MAILBOX` | DPU writes commands into the backend |
| 3 | `BACKEND_COMPLETION_MAILBOX` | DPU reads completions from the backend |
| 5 | `SQL_RESULT_BYTE_RING` | host-produced; DPU pulls/mirrors the backend's result bytes |
| 6 | `FRONTEND_COMPLETION_EVENT` | DPU writes completions back into client memory |
| 7 | `PAYLOAD_BYTE_RING_DPU_TO_HOST` | DPU writes decoded tuples into the client's result ring |

| Process | Exports to its LOCAL DPU |
|---|---|
| `pgbench` (node A) | role 1 (control slot), role 6 (completion events), role 7 (result ring) |
| socketless PG backend (node B) | role 2 (command mailbox), role 3 (completion mailbox), role 5 (result byte-ring) |
| postmaster (node B) | the backend-spawn region (once, at startup) |

The two DPU services talk RDMA to each other. No host Homer service is in the `--homer-dpu` path.

> **CORRECTED on review.** An earlier draft said the client exports a "command ring" and a
> "completion ring". **There is no such role.** The client's commands are pulled from its role-1
> control slot; its completions are written back via role-6 completion events. Read the role enum,
> not my summary of it.

---

## Design decisions

> ## ⚠ INVARIANT — DMA BEFORE DOORBELL (correctness, not style)
>
> The spawn-slot fields travel over **PCIe DMA**. The doorbell travels over **TCP**. **Nothing orders
> two different channels for you.** If the doorbell overtakes the DMA, the postmaster scans a slot
> whose fields have not landed and forks a backend with garbage `dbOid`/`userOid`.
>
> **DPU side, in this exact order:**
> 1. DMA the request fields → **await DMA completion**
> 2. DMA `state = REQUEST_READY` (release) → **await DMA completion**
> 3. *Only then* send the doorbell frame.
>
> **Host side:** read `slot->state` with **acquire** semantics before reading any field. This is the
> same contract today's submitters already rely on (`tuple_sink_service_process.c:18833`/`:18845`
> write fields, then the state word, then signal).
>
> Assert both halves with debug checks in S3. This is the single easiest thing to get wrong in the
> whole plan, and it fails intermittently and unreproducibly.



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
`slotCount`/`postmasterPid` (`:1758`). Region name `/citus_remote_exec_backend_spawn_v15` (bumped from
`_v14` in S3.1, when the request grew `arenaSlotIndex`; the log quotes further down predate the bump)
(`remote_execution_backend_protocol.h:28`), fixed slot count (`:30`), slot states (`:47`).
**So there is no "DOCA-export a region you do not own" problem** — an earlier note claimed there was;
corrected. The postmaster calls a Homer frontend API and exports its own region.

**The poller is a SIGUSR1 hook, not a poll loop.** Installed at `:3111`; entered at `:3081`; scans
slots at `:3083`; waits on `slot->state == CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_REQUEST_READY` at
`:3091`; forks via `ProcessSpawnRequestSlot` at `:3097`. Today's submitters write the fields, store
`REQUEST_READY`, then `kill(postmasterPid, SIGUSR1)` (`tuple_sink_service_process.c:18833`, `:18845`,
`:18924`, `:18927`).

**A DPU cannot signal a host process.** But the postmaster already blocks in a `select()`-style main
loop, and it is already the TCP *client* of its local DPU's setup listener. So the DPU can wake it by
making a watched fd readable.

#### The doorbell needs a PROTOCOL, not "a byte"

The setup socket already speaks a framed, typed protocol: `HomerDpuComchSetupHeader`
(`homer_dpu_comch_abi.h:62`) = `{protocolVersion, messageKind, headerBytes, totalBytes, ...}` with
`messageKind` in `{SETUP=1, SETUP_ACK=2, CLOSE=3, CLOSE_ACK=4}` (`:33-36`). Two problems with writing a
raw byte onto it:

1. **Framing.** TCP is a byte stream. An unframed byte injected between framed messages is a corruption
   bug, not a notification.
2. **Direction.** The protocol is strictly host-initiated request/response — the host sends `SETUP` /
   `CLOSE`, the DPU only ever *replies* (`SETUP_ACK` / `CLOSE_ACK`). It never initiates. An unsolicited
   doorbell could arrive while the host is blocked waiting for a `CLOSE_ACK`, and the host reader would
   have to demultiplex an interleaved request/response + notification stream.

> ## ⚠ SUPERSEDED — see "D2′ — the doorbell agent bgworker" below
>
> Everything in this D2 section about the **postmaster** owning the doorbell socket and about adding a
> hook to `pm_wait_set` is **retracted**. The framing and the protocol survive; the process that holds
> the socket does not. Read D2′ before implementing.

**Chosen: a DEDICATED doorbell connection on its OWN LISTENER (Option B).**

> **CORRECTED on review.** An earlier draft said "a second connection to the same setup listener".
> **That would deadlock the DPU.** The setup server is a SERIAL state machine with a single
> `clientFd` (`homer_service_dpu_setup_tcp.c:48`, accept guarded by `if (server->clientFd < 0)` at
> `:223`): it accepts one client, reads, acks, closes, then accepts the next. Setup connections are
> **transient** — which is exactly why two concurrent basebackup senders work: each connects,
> exports, disconnects, and the *import* outlives the connection. A PERSISTENT doorbell connection
> would occupy `clientFd` forever and starve every subsequent setup accept.

- The DPU service opens a **separate doorbell listener** (its own port, default `9728`). The
  postmaster connects to it and sends `HOMER_DPU_COMCH_MESSAGE_DOORBELL_ATTACH` carrying
  `bridgeGeneration` + `clientInstanceId` + a `doorbellClass` (`SPAWN`). The DPU replies
  `DOORBELL_ATTACH_ACK`. Bump `HOMER_DPU_COMCH_PROTOCOL_VERSION`.
- Thereafter the connection carries **only** `HOMER_DPU_COMCH_MESSAGE_SPAWN_DOORBELL` frames,
  DPU -> host, unsolicited, unacknowledged: a fixed 16-byte header, no payload.
- **The receiver never parses.** On readable, the postmaster `recv()`s into a scratch buffer until
  `EAGAIN`, discards the bytes, and rescans ALL spawn slots. Rescan is idempotent, so coalescing N
  doorbells into one wake is correct by construction, and a framing bug cannot mis-drive the
  postmaster — it cannot mis-parse what it does not parse. The frame is defined on the wire for
  tooling and future extension, not for this receiver.
- **Connection loss** => close, rescan unconditionally, reconnect with bounded backoff. A lost doorbell
  is only possible if the connection dropped, and reconnect covers it.

*Rejected (Option A): a new `messageKind` on the EXISTING setup socket.* It forces the host reader to
demultiplex notifications interleaved with request/response traffic, puts a protocol parser inside the
postmaster, and — decisively — a persistent connection starves the serial setup server.
*Rejected (Option C): a bgworker or agent that owns the socket and raises SIGUSR1.* An extra process
and a poll loop to solve what an already-watched fd solves.

---

## D2′ — the doorbell agent bgworker (SUPERSEDES D2's process placement)

**Decided July 9, 2026, after scoping the postmaster seam. Zero PostgreSQL core changes.**

### Why D2 was wrong

D2 put the doorbell socket in the postmaster and had it wake on the fd. That needs **four** touch points
in `postmaster.c`, all new core surface:
1. `ConfigurePostmasterWaitSet` (`:1605`) sizes the set exactly:
   `CreateWaitEventSet(NULL, accept_connections ? (1 + NumListenSockets) : 1)`.
2. …so a hook is needed there to add extra events.
3. `WaitEvent events[MAXLISTEN]` (`:1632`) must grow.
4. `ServerLoop` handles only `WL_LATCH_SET` and `WL_SOCKET_ACCEPT` (`:1673`) — a new dispatch arm is
   needed for a readable, non-accept fd.

**But `postmaster_sigusr1_hook` ALREADY EXISTS in our fork** (`src/include/postmaster/postmaster.h:18`,
`src/backend/postmaster/postmaster.c:180`), invoked at the end of `process_pm_pmsignal()` (`:3977`),
which `ServerLoop` runs when the signal handler sets `pending_pm_pmsignal`. **It is TIER 1** — every
`--homer` run spawns socketless backends through it, driven by an *arbitrary non-child process* doing
`kill(postmasterPid, SIGUSR1)` (`tuple_sink_service_process.c:18927`).

So a process that owns the doorbell socket and raises `SIGUSR1` needs **no core change at all**.

*The original rejection of this ("Option C: an extra process and a poll loop") was wrong twice:* it is a
**blocking `recv()`**, not a poll loop; and the "already-watched fd" alternative costs a core patch that
upstream would never take and we would carry forever.

### The design

A `shared_preload_libraries` **background worker** — the **Homer frontend agent** — owns *both* the DOCA
export and the doorbell socket. The postmaster does **no DOCA and holds no socket**.

| Process | Does | Does NOT |
|---|---|---|
| **postmaster** | creates + `mmap`s the arena and spawn region (POSIX shm, as `EnsureSpawnRegionMapped` already does); runs its existing SIGUSR1 hook → existing slot scan | touch DOCA; hold any socket; gain any new core hook |
| **frontend agent** (bgworker) | `shm_open`s both regions, DOCA-exports them via the **S1b** API, sends the setup message, holds the doorbell connection, `recv()`s, `kill(PostmasterPid, SIGUSR1)` | fork backends; parse doorbell frames |
| **socketless backend** | `shm_open`s the arena, binds a mailbox slot | touch DOCA; open a setup connection |

Registration mirrors the existing citus daemon: `InitializeCitusBackgroundWorker` +
`RegisterBackgroundWorker` from `_PG_init` (`shared_library_init.c:512`, right beside
`CitusInstallRemoteExecutionBackendHooks()` at `:525`). Flags `BGWORKER_SHMEM_ACCESS`, **no database
connection**, `bgw_start_time = BgWorkerStart_PostmasterStart`, `bgw_restart_time = 5s`.

### Three properties this buys, beyond "no core patch"

1. **DOCA never runs in the postmaster.** The postmaster forks every backend; inheriting a DOCA device's
   fds and internal state (possibly threads) across `fork()` is exactly the kind of thing that fails
   rarely and mysteriously. The agent is a leaf process. *This is now a positive design property, not a
   hazard we tolerate.*
2. **Open question 6 dissolves.** "The postmaster's doorbell fd must be closed in every forked child" —
   there is no postmaster fd. Backends are siblings of the agent and inherit nothing from it.
3. **The doorbell connection is a DIAGNOSTIC liveness beacon** (downgraded — see below). Because the
   agent holds *both* the export and the persistent doorbell socket, **doorbell EOF ⇒ that exporter is
   gone.** Useful for attributing the failure; **not** a safety mechanism.

> **⚠ CORRECTED (July 9, 2026).** An earlier revision called this a *hard requirement*, on the grounds
> that an agent crash "unpins its pages while the DPU may still be DMA-ing into them — a
> memory-corruption class bug." **That was overstated, twice:**
> - **The postmaster keeps the arena mapped**, so its pages cannot be freed and reused. A stale DMA
>   would land in the arena's *own* pages, not in unrelated memory.
> - With the IOMMU on, a torn-down registration makes the DPU's write **fault**, not land silently.
>
> The real consequence of an agent crash is a **fatal engine error on the DPU service** — and that is
> the behaviour we want. **Decided with the user: the agent must not crash; if it does, that is a bug,
> and failing loudly and fatally is correct.** No graceful re-export, no recovery path.
>
> **Consequently RETRACTED:** an earlier draft proposed reclassifying `DOCA_ERROR_IO_FAILED` on
> command/response/completion tasks from fatal to "export lost, recover". That would weaken a
> *deliberate* guard. `HomerDpuDmaRetireStaleGroupedControlImport`
> (`homer_service_dpu_dma.c:8342`) says so explicitly: *"Treat only that discovery-read failure as
> stale import removal; command, response, and backend-completion task failures remain fatal
> correctness bugs."* Leave it fatal.

### The sequence

1. **Postmaster** (`_PG_init`, `!IsUnderPostmaster`): create + `mmap` the spawn region (unchanged) and
   the new arena. Register the agent bgworker.
2. **Agent**: `shm_open` + `mmap` both regions; `HomerDpuFrontendExportRegion` each (S1b); build one setup
   message carrying **two** mmap exports; `HomerDpuFrontendSendSetup`; then connect the doorbell listener
   and send `DOORBELL_ATTACH`.
3. **DPU**: DMA the spawn slot (fields → await; then `state = REQUEST_READY` release → await), **then**
   send one `SPAWN_DOORBELL`. *(The ⚠ DMA-BEFORE-DOORBELL invariant at the top is unchanged and still
   the easiest thing here to get wrong.)*
4. **Agent**: `recv()` returns; drain to `EAGAIN` **without parsing**; `kill(PostmasterPid, SIGUSR1)`.
5. **Postmaster**: existing hook, existing slot scan, existing `ProcessSpawnRequestSlot`. Rescan is
   idempotent, so coalescing N doorbells into one wake is correct by construction.
6. **Agent dies** → socket closes → DPU sees EOF → invalidates the import. Postmaster restarts the agent
   → fresh export, fresh `bridgeGeneration`.

**Cost:** one signal hop (µs) on a path whose next step is a `fork()` (hundreds of µs). Irrelevant.

**Hygiene.** The agent is an ordinary backend-like process; a failed `recv()` degrades to close / rescan
/ reconnect with bounded backoff. Keep the receiver **parse-free**: it cannot mis-drive the postmaster
with input it never parses.

---

## D4′ — the arena is POSIX shm, addressed by offset (REFINES D4)

D4 said backends inherit the arena "by `fork()` after the postmaster `mmap`s it". **Not required, and it
was the wrong reason.** The arena is a **named POSIX shm object**, exactly like the spawn region, so any
process maps it by name — postmaster, agent, and backends alike. What D4 actually buys survives intact:
**one DOCA context per node, one export, and backends that do no DOCA.**

The load-bearing consequence is that **the exporter and the readers are different processes at different
virtual addresses.** Bridge descriptors already address as `hostRingAddress` (the exporter's base VA) +
`hostRingOffset`, so the DPU writes at the *agent's* VA and the postmaster/backends observe it through
their own mappings of the same physical pages. **That aliasing is the single load-bearing assumption of
this whole design — see S0b.**

### Sizing (measured July 9, 2026)

| Component | bytes |
|---|---|
| `CitusRemoteExecBackendSpawnRegion` (32 slots) | 17,424 |
| `CitusRemoteExecLocalCommandMailbox` (role 2) | **3,202,584** (3.05 MiB) |
| `CitusRemoteExecLocalCompletionMailbox` (role 3) | 270,552 (264 KiB) |
| SQL result byte ring (role 5, 256 × 1024) | 262,144 (256 KiB) |
| **per backend slot** | **≈ 3.56 MiB** |

A **single DOCA-registered region has a ~100 MB ceiling** (recorded in
`dpu_byte_ring_pool_per_session_plan.md:175` — raising the landing region to 100 MB crashed DOCA). So:

| N backend slots | arena size | verdict |
|---|---|---|
| 8 | ≈ 28.5 MiB | fine |
| **16** | **≈ 57 MiB** | **chosen** — ample headroom, ≫ pgbench `-c 4` |
| 32 | ≈ 114 MiB | **over the ceiling** — would need splitting |

**Decision: N = 16.** If it must grow, split the arena across several DOCA mmaps — **the setup ABI already
supports this**: `HomerDpuComchSetupHeader.mmapExportCount` (`homer_dpu_comch_abi.h:75`),
`HomerDpuComchSetupMessageBytesForExports(ringCount, mmapExportCount, ...)` (`:177`), and each descriptor
names its mapping through `mmapExportId` (`:99`). `HomerDpuComchSetupMessageBytes` is just the
`count == 1` convenience wrapper. **This is also why the agent can export the spawn region and the arena
as two mmaps in one setup message.**

> Note the 3.05 MiB command mailbox is the same bloat as the 66 KiB control slot: 64 slots of a
> fixed-size record. Shrinking the ABI unions (S4 perf item, fix 3) shrinks the arena too.

---

## S0b — spike: exporter ≠ reader ✅ **PASSED (July 9, 2026, citus `37cc74b06`)**

**The question.** S0 proved DOCA can export a tmpfs `MAP_SHARED` mapping and DMA into it — but its
exporter and its reader were the **same process**. D2′/D4′ need them to differ: process **A**
DOCA-exports its mapping of a POSIX shm object, the DPU DMA-writes into it, and process **B** — which
mapped the same object by name at a different VA and never touched DOCA — observes the write.

Standard `MAP_SHARED` aliasing says this works. **This project's discipline is not to trust "says."**
It was the single load-bearing assumption of D2′/D4′: had it failed, the whole doorbell-agent design
collapses back to a postmaster-owned export plus the four-point core patch.

**Result.**
```
FORK-READER: exporter_va=0x7f3794bc6000 reader_va=0x7f3794865000 distinct_va=YES
FORK-READER: observed the DPU's DMA write through its OWN mapping state=4 command_seq=7001
             -- exporter != reader CONFIRMED
homer_dpu_tcp_transport_smoke: ok    (rc=0, all six legs, no /dev/shm leak)
```
A process holding **no DOCA handle**, mapping the object at a **different virtual address**, observed
the DPU's PCIe DMA write. The DMA lands in the **page-cache pages of the shm object**; the exporter's
host VA is only an offset key. **D2′/D4′ stand.**

**How to run it:** `--export-posix-shm --fork-reader` on the TCP transport smoke. The child `munmap`s
the mapping it inherited, reserves the vacated range with `MAP_FIXED|PROT_NONE` so the next `mmap`
cannot reuse the exporter's address, `shm_open`s the object by name, maps it at its own VA, and
verifies the DPU's response publication. **The child's exit status gates the smoke's exit code.**

**Two bugs in the first cut of this spike, both instructive:**
- The child got the **same** virtual address — the kernel handed back the hole the `munmap` had just
  left. It "passed" while proving nothing about VA independence. Hence the `MAP_FIXED` placeholder, and
  landing on the exporter's VA is now **INCONCLUSIVE, not a pass**.
- The child's verdict line **never printed**: `_exit()` does not flush stdio. *A spike whose evidence
  can silently vanish is exactly the failure mode this smoke already had once.* The child now flushes
  explicitly; the parent `fflush(NULL)`s before forking so the child cannot duplicate its buffer.

**Caveat.** The spike's reader is a *descendant* of the exporter; in D2′ the reader (postmaster) is the
*parent* of the exporter (agent). Aliasing is symmetric, and the child re-opens the object **by name**
rather than relying on inheritance, so provenance is clean either way. The child also `closefrom(3)`s
before mapping, so it holds **no inherited DOCA descriptor** — matching the postmaster, which never has
one. (`fork()` copies the descriptor *table*; the parent's registration is untouched.)

### The mechanism, from the DOCA 3.2.0118 headers

Read alongside the empirical result. **Documentation alone would NOT have sufficed** — it explains *why*
it works but never promises it.

- `doca_mmap_set_memrange` takes "the start address of the memory range." Its documented restrictions
  concern object state, one-time configuration, and `addr + len` overflow — **nothing about anonymous vs
  file-backed vs tmpfs vs hugepages** (`doca_mmap.h:404-427`).
- `doca_mmap_start()` can fail because "the kernel failed to pin the requested amount of memory"
  (`:124-152`) — so registration **pins pages**.
- The export blob is **opaque**: DOCA calls it a serialized mmap representation and never says whether it
  carries virtual, physical, or IOVA addresses (`:252-261`).
- The DPU-side import is "not backed by local memory" (`:354-398`), and
  `doca_buf_inventory_buf_get_by_addr()` forwards `(addr, len)` into a lookup against the imported range,
  rejecting an address with "no suitable memory range" (`doca_buf_inventory.h:138-204`).
- **No PASID / ATS / SVA / per-process IOMMU domain / bounce buffer** appears anywhere in this path. The
  exposed model is a device *protection domain* (`doca_dev.h:160-181`) plus a device-associated mmap
  memory key (`doca_mmap.h:567-584`) — conventional pinned registered memory.
- NVIDIA's own `dma_copy` sample transports `host_addr` **separately** from the export blob and hands it
  to `buf_get_by_addr` on the DPU (`applications/dma_copy/dma_copy_core.c:709-729`, `:1194-1215`); and
  `file_compression` registers a `MAP_SHARED` **file** mapping (`file_compression_core.c:536-560`).

**Our own ABI already said this**: `hostRingAddress` is *"only an address token for DOCA buffer
construction against the imported mmap"* (`homer_dpu_bridge_abi.h:190-194`).

**Graceful teardown is documented**: `doca_mmap_stop()` invalidates every mmap created from its export
(`doca_mmap.h:154-160`) and `doca_mmap_destroy()` implicitly stops (`:104-122`). **Ungraceful exporter
death is not documented at all** — see the R3 decision in D2′: the agent must not crash, and a fatal DPU
engine error is the correct outcome if it does.

**What DOCA still does not promise:** that `doca_mmap_start()` pins the *tmpfs page-cache* pages rather
than some process-affine representation, nor anything about cross-process cache visibility. That is
precisely the gap S0b closed.

### Prior art: the Tier-2 path already implements exporter ≠ reader

Found while investigating. The GUC-gated, unexercised `SELECTED_DPU_DMA` path is **already exactly this
shape**:
- process A creates named shm objects and maps them `MAP_SHARED`
  (`homer_frontend_dma_lifecycle.c:475-501`) and DOCA-exports those mailbox mappings
  (`homer_frontend_dma.c:1267-1279`);
- process B independently `shm_open`s the same names and maps them `MAP_SHARED`
  (`remote_execution_backend_bridge.c:2610-2674`);
- B polls the **DPU-written publication epoch** through its own mapping (`:423-476`).

So a third piece of the architecture — after the descriptor roles and the transport-agnostic control
dispatcher — turns out to have been anticipated. But it is **Tier 2**: source expressing an assumption is
not evidence the hardware honours it, which is exactly why S0b was worth thirty minutes.

> **Read it before deleting it.** S2.5 retires that path. It is currently the *only* implementation of
> the exporter ≠ reader shape we are about to build — mine it for patterns first.

---

### D4 — the postmaster exports a per-node ARENA; backends inherit it by `fork()`

Setup connections are **transient and serialised** (see D2), so a per-backend export *would* work
mechanically: connect, export, disconnect; the import outlives the connection. But every socketless
backend would then need its own DOCA device context, opened on the session-open path, and every
concurrent session would consume one of the engine's `hostMmapImportCapacity` imports.

**Better, and symmetric with the DPU byte-ring pool we already built:** the postmaster creates ONE
arena at startup — the spawn region plus `N` preallocated sets of {command mailbox (role 2),
completion mailbox (role 3), result byte-ring (role 5)} — DOCA-exports it once, and declares all the
descriptors up front. Backends are `fork()`ed from the postmaster **after** the arena is `mmap`ed, so
they inherit the mapping for free and simply **bind a slot index**. Fatal on exhaustion, exactly as
`HomerDpuByteRingBind` does.

- One DOCA context per node, not per backend. One export. One import. Zero per-backend setup cost.
- **Dissolves open question 2** (does the backend need DOCA at startup? — no) **and open question 3**
  (who owns the result byte-ring? — the arena; the backend binds a slot).
- Session-open latency becomes just the fork.

*Cost:* descriptors must be preallocated for a maximum concurrent-session count, so `ringCount` and
`hostMmapImportCapacity` need sizing (`homer_dpu_bridge_abi.h:119`, `homer_service_dpu_dma.h:57`).
Same trade the byte-ring pool already makes, and the same fatal-on-exhaustion policy.

*Alternative (rejected for now):* per-backend export. Simpler conceptually, and it is closer to
"the backend is literally a Homer frontend", but it pays DOCA init per session and burns an import per
session. Revisit only if the arena's preallocation proves awkward.

> **D4 re-examined (July 9, 2026) — it still stands, but for a DIFFERENT reason than it was decided
> on.** D4 rested on two costs of per-backend export: (a) a DOCA context opened on the session-open
> path, and (b) one `hostMmapImportCapacity` import burned per session. **(b) has evaporated:**
> `hostMmapImportCapacity` defaults to **1024** (`homer_service_dpu_dma.c:785`), which is far above any
> concurrency we will reach. Had that been the only argument, per-backend export would now win.
>
> But a **stronger** argument appeared once S3's shape was settled: under D2 the postmaster **must**
> DOCA-export the spawn region anyway, and it **must** `mmap` it before any fork. Given it is already
> holding a DOCA device and an export, folding the per-session {role-2, role-3, role-5} descriptor sets
> into that same arena is nearly free — and it lets the socketless backend do **no DOCA at all**, which
> is what actually keeps the session-open path short. One DOCA context per node, not per backend.
>
> Recorded because a decision whose stated rationale has quietly become false is a trap for the next
> reader — and because it means the *cheap* thing to reconsider, if the arena's preallocation turns
> awkward, is (b), not the whole design.

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

### S0 — Spike ✅ **DONE (July 9, 2026, citus `4e91aaa63`+)**

**Question.** Every validated DOCA export in this tree exports **anonymous `posix_memalign` heap**
(`homer_client.c:2028`, `homer_dpu_tcp_transport_smoke.c:462`). The postmaster's spawn region is
`shm_open` + `mmap(MAP_SHARED, fd)` — a **tmpfs file-backed** mapping
(`remote_execution_backend_bridge.c:1709`-`:1745`). D2 needs the DPU to DMA into *that*. So the spike is
NOT "can a process DOCA-export memory" (proven); it is **"can DOCA export *this kind* of memory."**
Worth minutes because DOCA has surprised us before (the undocumented ~100 MB single-region ceiling).

**Method.** Rather than a standalone program, added `--export-posix-shm` to the existing TCP transport
smoke (`homer_dpu_tcp_transport_smoke.c`): the client's export buffer becomes `shm_open(O_CREAT|O_EXCL)`
+ `ftruncate` + `mmap(MAP_SHARED)` (unlinked right after mmap) instead of `posix_memalign`. Zero DPU-side
change; reuses the whole harness and its existing DPU→host publication assertions as the proof.

**Result: PASS — both directions, on tmpfs.**
```
client posix-shm export: name=/homer_tcp_smoke_export_3872836 bytes=3542200 addr=0x7f535c176000
client received TCP setup ack generation=1 rings=6 imported_bytes=283
client observed DMA backend command publication published_epoch=7001   <- DPU DMA-WROTE into tmpfs
client observed DMA response publication state=4 command_seq=7001      <- second DPU->host write
```
- **DPU reads from tmpfs**: grouped-control reads, the role-1 command pull, and two byte-ring pulls all
  completed (the run reaches the *later* write leg, so everything before it succeeded).
- **DPU writes into tmpfs**: the two publications above, observed by the host.
- **Control:** the server then failed at *byte-identical* the same point as the anonymous-heap run
  (see "smoke defects" below), so the shm switch is not implicated in that failure.

**Conclusion.** D2's export path is sound. `doca_mmap_set_memrange` + `doca_mmap_export_pci` pin and
export a tmpfs `MAP_SHARED` mapping exactly as they do anonymous heap. The postmaster can export the
spawn region it created. No `-lrt` needed (glibc ≥ 2.34 folds `shm_open` into libc).

#### S0 side-finding: the TCP transport smoke was itself broken (Tier 1.5 → repaired)

Running it — for the first time since it was fixed to *compile* — exposed that it had silently rotted
through the byte-ring pool migration. **A smoke nobody runs is Tier 2, not a test.**

1. **`HomerDpuDmaAcceptByteRingPull` rejected every smoke pull.** `HomerDpuDmaSubmitByteRingSmokePull`
   (`homer_service_dpu_dma.c:2529`) receives the mirror base/bytes/mmap as *parameters* but never
   populated `ringRuntime->dpuMirrorSlotResolved` — the cache that acceptance (`:9850`) demands and that
   egress (`HomerDpuDmaFillMirroredByteRange`, `:4470`) later reads as authoritative. The **production**
   caller `HomerDpuDmaSubmitMirroredByteRingPulls` (`:3553`) happened to resolve first at `:3670`
   (it needs `dpuRingBytes` for its own wrap clamp); the **smoke-only** driver bound its MIRROR slot
   directly and passed the handle in, so the cache stayed empty.
   → **Not a production bug** (basebackup goes through the production scheduler). A *smoke-fidelity* bug.
   **Fixed:** the submitter now resolves the slot itself — one resolution point, precondition true by
   construction — and cross-checks the caller-supplied window against the pool-resolved one, turning a
   silent mirror-aliasing bug into a loud submit-time rejection.
2. **Six failure causes shared one message.** The acceptance check was a 6-term `||` behind
   `"byte-ring pull owner does not match imported ring"`. Each cause has a different fix; the message
   named none of them. **Split into six named failures.** This is what made (1) diagnosable at all.
3. **STILL BROKEN (data plane, deferred):** the smoke's DPU→host write leg calls the retired
   `HomerDpuDmaGetLandingRegionMemory` engine singleton (`:3976`) and passes `dpuRingBase=NULL` to
   `HomerDpuDmaSubmitByteRingWrite` (`homer_dpu_tcp_transport_smoke.c:1741`), so it fails
   `"byte-ring write ring exceeds bound landing slot source"` (`:3195`). It must bind a `LANDING` pool
   slot per role-7 ref. Tracked as a follow-up — **not** on the command-plane critical path (the command/
   completion legs pass, and production basebackup exercises the write path).

**Trust reclassification.** The host↔DPU **command/completion DMA loop is now TIER 1** — genuinely
exercised, both directions, including into tmpfs. This materially de-risks S4: its command plane is
proven; what S4 adds is the real backend and the real client, not new DMA mechanics.

### S1a — client-side `HomerClientOpenSqlSessionSelectedDpu` (clone the Tier-1 template)

> ## ✅ PLAN CORRECTION (July 9, 2026) — S1a is CLIENT-SIDE ONLY
>
> S1a.2/.3/.4 were written against **drifted line numbers** and a false premise: that the DPU
> control-slot path and the host-SHM control path are **two dispatchers**. They are **one**.
> Verified in code (see below). The service already admits an `OP_COMMAND_SESSION` open arriving
> over the role-1 DPU control slot and routes it into the async command-open state machine.
> **Nothing to relax. S1a is the client opener plus pgbench wiring.**
>
> This is the second time reasoning from a remembered line number rather than the code produced a
> wrong plan (cf. the "divisor's third job is tiling" retraction). Re-derive before planning.

**Evidence for the correction:**
- `TupleSinkServiceDispatchLocalControlSlot` (`tuple_sink_service_process.c:37840`) switches **only on
  `requestKind`** — no op-kind allow-list, no transport check. Both callers use it:
  the host-SHM slot pump at `:38058` and the DPU staged-command executor at `:39654`.
- The DPU staged executor builds a `DPU_STAGED_COMMAND` response owner (`:39653`) and
  `...ResponseOwnerCanRegisterAsync` accepts it (`:36240`-ish), so async continuations publish back
  through the Stage-8 pending-response DMA queue.
- `TupleSinkServiceHandleOpenSession` routes `opKind == OP_COMMAND_SESSION` straight to
  `TupleSinkServiceHandleOpenCommandSession` (`:33888`), transport-agnostic.
- `TupleSinkServiceOpenRequestNeedsAsyncLocalControl` (**now `:34904`**, not `:34817`) already returns
  **true** for `OP_COMMAND_SESSION` + `sessionKey.opKind == OP_CLIENT_SQL_SESSION` +
  `destinationNodeId > 0` (`:34918`). It is a *routing* predicate, never a rejection.
- The DMA layer accepts `OPEN_SESSION`/`CLOSE_SESSION`/`REPORT_POST_COMMAND_STATE`/`START_COMMAND`/
  `POLL_COMMAND_COMPLETION` request kinds off the control slot (`homer_service_dpu_dma.c:9825`).

**Sub-step dispositions:**
- **S1a.1** — **THE WORK.** New entry point in `src/bin/homer_client.c`. See "S1a.1 decisions" below.
- **S1a.2** — ~~admit `OP_CLIENT_SQL_SESSION` over the DPU control slot~~ **NO-OP, already admitted.**
- **S1a.3** — ~~`REGISTER_MEMORY` requires `commandMailbox.mailbox`~~ **NO-OP.**
  `TupleSinkServiceCreateSession` calls `TupleSinkServiceEnsureSessionMailboxes` **unconditionally**
  (`:22116`, fatal on failure) and `...EnsureClientCompletionMailbox` for `OP_CLIENT_SQL_SESSION`
  (`:22127`). The mailbox is never NULL for a live session, so the `:35337` check passes by
  construction. (What it is *used for* matters — see the S4 note below.)
- **S1a.4** — **RESOLVED, open question 1 closed.** `backendCpu` is **service-chosen**, not carried by
  the client: `reservedSlot->request.backendCpu = TupleSinkServiceChooseBackendCpu()`
  (`:18907`; chooser at `:871`). It is absent from `CitusRemoteExecOpenSessionRequest`
  (`homer_control_abi.h:297`) and lives only in the spawn struct
  (`remote_execution_backend_protocol.h:112`). **Keep service policy. Do not add a client field.**
- **S1a.5** — pgbench: select the new opener under `--homer-dpu`.

#### S1a.1 decisions (made within the plan's direction; recorded per the "note your reasoning" rule)

1. **Clone, do not extract.** `HomerClientDpuSubmitControlRequest` (`homer_client.c:2233`) and its
   siblings are typed on `HomerClientBaseBackupStream *` but touch only a small control-channel
   substruct. Extracting now would edit the Tier-1 basebackup path, our only regression net. **S1b
   exists precisely to extract.** Duplication is temporary and deliberate.
2. **Reuse `HomerClientBaseBackupStream` as the selected-DPU export carrier.** The name is a misnomer
   — it is really "an exported host region + a role-1 control slot" — but the codebase *already*
   reuses it that way for the SQL result ring (`HomerClientSession.sqlResultDpuStream`,
   `remote_execution_client.h:257`). Adding a second carrier type would duplicate five helpers.
   **S1b renames/extracts.**
3. **Export role 1 only; leave role 7 to its existing opener.** `HomerClientOpenSqlResultReceiveStreamSelectedDpu`
   **already exports a role-1 control slot AND the role-7 ring** (descriptors `[0]`/`[1]`). It is
   Tier-1 and MUST NOT be touched. Two exports per client is fine: `hostMmapImportCapacity = 1024`
   (`homer_service_dpu_dma.c:785`) — which also **softens open question 4** considerably.
   Consolidating the two exports belongs in S1b/S2, not here.
4. **Do NOT call `HomerClientOpenBackendMailboxes`.** Today's `HomerClientOpenSqlSession` `shm_open`s
   the backend command mailbox **by name on its own host** (`homer_client.c:1627`), because the
   client-side host service created it. That is precisely the node-local assumption the target
   architecture destroys. The DPU-native client's commands ride the role-1 control slot
   (`START_COMMAND`), which the smoke already proves the DPU pulls and answers.

#### What S1a does NOT solve (deliberately deferred to S4/S5)

`commandMailbox` is registered at `REGISTER_MEMORY` as a **local RDMA send source**
(`remoteWritable=false`, `:35346`). On a DPU-resident service its `shm_open` still succeeds — it
simply becomes DPU-local memory nobody else maps. **It likely survives the move unchanged.**

> **"Scratch" is a misnomer — and the migration is what would make it true.** The field is called
> `clientSqlCommandScratchRegionHandle` (`:1303`), but it is not a scratch buffer: it is the
> `ibv_reg_mr` registration *of the command mailbox itself*. `sourceSlot = &mailbox->commandSlots[i]`
> and `sourceRecord = &sourceSlot->record` (`:19801`); the RDMA WRITE posts **from that exact address**
> (`:19895`-`:19898`) into the peer's mailbox. An RDMA source needs an lkey — that is all the handle is.
> `remoteWritable=false` because nothing writes into it remotely.
>
> So today there is **no staging copy at all**: the client writes the command into the mailbox (same
> host, shared memory) and the service RDMAs out of those bytes. **Zero copies.**
>
> **The DPU migration spends that property.** Once the mailbox is DPU-local, a command must go
> client control slot → **DMA pull across PCIe** into the DPU mailbox → RDMA out. That pull is work the
> current design does not do, and only *then* would "scratch" be an accurate name.
>
> **The removal is "direct PCI-source RDMA"**: register the *imported host mapping* as an RDMA memory
> region on the DPU so the local QP's send SGE points straight at host memory. The NIC then reads the
> command over PCIe as part of the RDMA WRITE — no `doca_dma` task, no DPU-resident copy. Recorded as
> decided but never mapped out. The reason it is unmapped is a **DOCA/verbs interop question, not a
> Homer design one**: the DPU holds a `doca_mmap` reconstructed from the host's PCI export, and it is
> not established that an `ibv_mr`/lkey usable by the posting QP can be derived from it.
>
> > **⚠ CORRECTED (July 9, 2026).** An earlier revision of this note called direct PCI-source RDMA "a
> > correctness-of-performance requirement, not an optimization," and said to reconsider it **in S4**.
> > **Both claims were wrong.**
> > - **S4 is single-node — it contains no RDMA at all**, so direct PCI-source RDMA cannot apply there.
> >   S4 does introduce a DMA pull, but nothing today runs a single-node command through a DPU, so there
> >   is no baseline for it to regress against.
> > - **The cost is small.** A PCIe DMA read is order 1–2 µs against a warmed c1 transaction of ~238 µs
> >   (~4.2k TPS). Under 1%. It is not a parity threat per operation.
> >
> > What is actually true: the pull begins mechanically at **S5/S6**, and **S6** — cross-node DPU vs
> > today's cross-node host-service `--homer` — is the first comparison against a real number. The risk
> > it poses is to **scaling**, not per-op latency: DMA task slots and PE polling are shared on the DPU,
> > and c4 already shows a shared-transport bottleneck. So: **measure at S6, do not pre-optimize.**
> > This entry exists so that an S6 number below the host-service baseline is not mysterious.
>
> Two smaller notes: the MR covers the whole 64-slot mailbox, registered once per session (cheap). And
> for small commands ≤ `max_inline_data` (124 bytes on this fabric) the registered post still takes
> `IBV_SEND_INLINE` (via `TupleSinkServicePostWriteScatterGatherInternal`), where the HCA copies the payload
> into the WQE and the MR is nearly vestigial — but a pgbench `UPDATE`/`SELECT` record exceeds 124 bytes, so
> those take the registered non-inline post regardless.
> ⚠ **An earlier `useInlineCommandPost` fast path** retired such small commands immediately with BOTH WRs
> **unsignalled**; it was **REMOVED 2026-07-14** because that leaked send-queue WQEs on a compact-only command
> stream (audit §32). All commands now take the one registered path, whose terminal readySeq WR is **signalled**.

### 📌 S4-stage perf item: the command pull moves 67,912 bytes to carry a few hundred

**Measured, July 9, 2026.** `sizeof(CitusRemoteExecControlSlot) == 67912`. `HomerDpuDmaSubmitOneCommandPull`
DMAs the **entire slot** every time — `doca_buf_set_data(srcBuf, srcAddress, sizeof(CitusRemoteExecControlSlot))`
— regardless of the request in it:

| | bytes |
|---|---|
| `CitusRemoteExecControlSlot` (what we DMA) | **67,912** |
| ├─ request union | 33,784 (all of it is `OpenSessionRequest`) |
| └─ response union | 34,112 (`CommandCompletion` is 33,816 of it) |
| `PollCommandCompletionRequest` (actual payload) | 40 |
| a pgbench `START_COMMAND` | a few hundred meaningful bytes |

Harmless today — control slots carry only `OPEN`/`CLOSE`. **From S4 on, `START_COMMAND` is per
transaction**, so this becomes a ~66 KiB PCIe read on the measured path. Same family as
"direct PCI-source RDMA" above: *don't move bytes you don't need.*

**Do not pre-optimize. Measure at S4/S6.** If it bites, the fix ladder, cheapest first:
1. **Two-phase pull.** DMA a small fixed prefix (slot header + request header, ~512 B). The request
   header carries `payloadBytes`; issue a second pull only if the request exceeds the prefix. For
   pgbench that is one ~512-byte DMA instead of 67,912. **No ABI change.**
2. **Advertise the length up front.** The grouped-control read of the host publish line *already*
   precedes every pull, so the client could stamp the request byte length there and the DPU could pull
   exactly that much — **no extra round trip.** Costs a field in `HomerDpuBridgeHostPublishLine`.
3. **Shrink the unions.** 33.8 KiB per side is fixed-size arrays (`CommandCompletion` alone is 33,816).
   An ABI change, but it shrinks the control slot, the client's export buffer, *and* the mailboxes.

#### Why the per-ring scratch buffer is part of THIS item, not a separate one

The obvious-looking cleanup — give each ring its own pull buffer and delete
`commandPullBufferInUse[]` + `HomerDpuDmaFindFreeCommandPullBuffer` — is **not a perf win and is
currently a memory regression**:

- The free-list scan is 64 `bool`s = **one cache line**, a few ns, against a **67,912-byte** PCIe DMA on
  the same request. Three orders of magnitude apart. Optimising the scan is optimising the wrong thing.
- Per-ring ownership costs `67,912 × rings`. At `hostMmapImportCapacity = 1024` that is **66 MiB** of DPU
  memory versus **4.1 MiB** for the 64-slot pool. Lazy allocation fixes the memory but puts an allocation
  on the control path.
- It touches **three** pools (`commandPullBufferInUse`, `commandResponseBufferInUse`, `controlCellInUse`,
  all sized by `commandPullBufferCount`) plus the `localBufferIndex` validation in
  `HomerDpuDmaAcceptCommandPullSlot`.

**Strong suspicion: the global pool exists BECAUSE the slot is 66 KiB.** Per-ring is the natural design —
`ringRuntime->lastCommandBufferIndex` is already per-ring — and whoever wrote it hit the memory wall and
pooled instead. **Shrink the slot (fix 3) and per-ring ownership becomes nearly free**, deleting a shared
mutable pool, an exhaustion mode, and an artificial 64-concurrent-session cap that is inconsistent with
`hostMmapImportCapacity = 1024`.

**So: if the 66 KiB DMA turns out to matter, solve both together.** Do not do the per-ring change alone.
>
> **Rename** `clientSqlCommandScratchRegionHandle` → `clientSqlCommandMailboxRegionHandle` as part of
> the pending scoped rename (see `byte_ring_slot_capacity_regression.md`). A name that describes a
> design we do not have is worse than an unclear one.

`clientCompletionMailbox` does **not**: it is registered **remote-writable** (`:35365`), the peer
RDMA-writes completions into it, and today pgbench reads it directly out of shared memory
(`HomerClientOpenCompletionMailbox`). Cross-node that must become a **DPU→host DMA into role 6**
(`FRONTEND_COMPLETION_EVENT`).

> **⚠ CORRECTED (July 9, 2026).** An earlier revision said *"Nothing writes role 6 today."*
> **False, and it made S4 look bigger than it is.** The service already writes role 6, end to end,
> in the **single-node** shape — which is exactly S4's shape:
>
> ```
> backend writes its role-3 completion mailbox
>   -> HomerServiceDpuAcceptOneBackendCompletion       (DMA-pulls it)      :39348
>   -> HomerServiceDpuEnqueueSelectedCompletionEvent                        :39007
>   -> HomerServiceDpuPublishOneSelectedCompletionEvent                     :39478
>   -> HomerDpuDmaSubmitFrontendCompletionEventPublication  (DMA into role 6)
> ```
>
> Two things are genuinely missing, and they split cleanly across the stages:
> - **S4:** no **Tier-1 client exports role 6.** The only exporter is the deprecated
>   `homer_frontend_dma.c:1617`. `HomerClientOpenSqlSessionSelectedDpu` must export it and drive
>   `START_COMMAND` down its role-1 control slot; the service-side push then already works.
> - **S5:** no **cross-node bridge.** A remote completion lands in `clientCompletionMailbox` via peer
>   RDMA, and nothing turns it into a selected-session completion event. The role-6 publisher is fed
>   only by *locally* DMA-pulled backend completions.
>
> So the push machinery is **TIER 2 — exists, unexercised by any production client**, not absent.
> Classify before depending on it: it compiles and the smoke touches parts of it, which is exactly
> the trust level that has bitten this project repeatedly.

**Why the normal path never tripped the POLL wedge.** Exercised client SQL is *push*-based — it waits
on the pushed completion, never polls (`TupleSinkServiceHandlePollCommandCompletion` says so itself:
*"Normal pgbench client SQL waits on the pushed completion ring directly; this handler is retained for
debug/legacy local-control callers."*). That is precisely why the wedge stayed latent, and why the fix
was hygiene for the deprecated frontend plus the invariant, not a repair of the measured path.

**Gate:** compiles; the DPU service logs an `OP_COMMAND_SESSION` open arriving over the DPU control
slot. No end-to-end claim yet.

### ✅ S1a DONE (July 9, 2026 — citus `3662f60f7`, postgres `1a81c6710ea`)

Landed: `HomerClientOpenSqlSessionSelectedDpu` / `...CloseSqlSessionSelectedDpu` (`homer_client.c`),
`HomerClientSession.commandDpuStream` (`remote_execution_client.h`), pgbench `--homer-dpu-command`,
and a control-path-only gate instrument in `HomerServiceDpuStageOneCommandForDispatch`.
**No service-side change was required**, exactly as the correction above predicted.

**Topology:** pgbench on farnet0 → its LOCAL DPU `10.10.1.200` (setup TCP 9727) → RDMA → peer DPU
`10.10.1.201:9717`. farnet0's HOST service handled **zero** sessions (it is mapped only so
`HomerClientOpenControl` succeeds; S7.1 removes even that).

```
[homer-service] dpu control slot: OPEN_SESSION opKind=2 sessionKeyOpKind=5 destNode=1
                peer=10.10.1.201:9717 sessionUID=11095041325112745 dpuRelay=0
tuple-sink service: established persistent outgoing RDMA peer transport
                host=10.10.1.201 port=9717 node=1 traffic_class=1
tuple-sink service: async local open failed phase=5
                detail=could not open backend spawn region /citus_remote_exec_backend_spawn_v14
```
(`opKind=2` = `OP_COMMAND_SESSION`, `homer_control_abi.h:45`; `sessionKeyOpKind=5` =
`REMOTE_EXEC_OP_CLIENT_SQL_SESSION`, `homer_frontend.h:49`.)

## 🔑 S5 IS ALREADY WORKING — the only blocker is S2+S3

**`phase=5` is `TUPLE_SINK_SERVICE_LOCAL_CONTROL_ASYNC_COMMAND_WAIT_PEER_OPEN`**
(`tuple_sink_service_process.c:34620`-`:34627`; `UNUSED=0` so `WAIT_PEER_OPEN=5`). The error text
came back **from the peer**, over RDMA, and was then DMA'd into pgbench's control slot as readable
text. **No hang.** So the farnet0 DPU had already completed, in order:

| Phase | What it proved |
|---|---|
| `COMMAND_CREATE` | session allocated; `TupleSinkServiceCreateSession` created BOTH mailboxes on the DPU — S1a.3's dissolution confirmed **empirically** |
| `ENSURE_CONNECTION` | DPU↔DPU `CRITICAL_CONTROL` RDMA connection established |
| `REGISTER_MEMORY` | `commandMailbox` + `clientCompletionMailbox` RDMA-registered — they work as **DPU-local memory**, as predicted |
| `START_PEER_OPEN` | peer `OPEN_COMMAND_SESSION` shipped over RDMA |
| `WAIT_PEER_OPEN` | peer DPU **received and handled it**, reaching `TupleSinkServiceSubmitBackendSpawnRequest` (`:37623`) — i.e. it passed every peer command-open gate (`:37385`, `:37395`, `:37406`) |

The peer failed **only** because `TupleSinkServiceSubmitBackendSpawnRequest` `shm_open`s
`/citus_remote_exec_backend_spawn_v14` **on the DPU**, where no postmaster ever created it.

**Consequences for the plan:**
- **S5 needs no allow-list relaxation.** Its gates already pass; `sessionUID` already threads through
  (`:35401`). What S5 still owes is S5.3 (the receiving DPU triggers the spawn on ITS host) — which
  is S3. Re-scope S5 to "confirm a completion comes back", after S3.
- **S2 + S3 are the whole remaining critical path.** Everything on either side of them is validated.
- The DPU-native command open **propagates errors correctly**. The silent-hang failure mode does not
  afflict this path; it afflicts the byte-ring data plane.

**Next: S2 (postmaster arena, D1+D4) then S3 (spawn trigger, D2).** S0 already proved DOCA can export
the tmpfs `MAP_SHARED` mapping the spawn region is made of.

### S1b — extract the shared Homer-frontend export module
Now load-bearing, because **three** processes need it: the client library, the socketless backend (D1),
and the postmaster (D2). Extract from the Tier-1 `homer_client.c` machinery — **not** from the Tier-2
`homer_frontend_dma.c`.

Surface: "open a control channel to my local DPU; export these regions; submit this control slot;
receive this response." Must be linkable into both `libhomer_client.a` and the PostgreSQL backend.

**✅ Linkability confirmed — no new build plumbing needed.** The PostgreSQL *backend* already links
`libhomer_client.a` (`src/backend/meson.build:48-50`), because `basebackup_homer.c:296` calls
`HomerClientOpenBaseBackupStreamSelectedDpu`. `nm` on the installed `postgres` shows 38 `HomerClient*`
symbols. So the postmaster can call the extracted API **directly**; nothing has to move files.

#### S1b design decision — parameter extraction, NOT struct surgery

The natural-looking extraction is to factor the twelve `dpu*` fields of `HomerClientBaseBackupStream`
into a `HomerClientDpuExportChannel` substruct and retype the helpers on it. **Rejected.** The struct's
layout is load-bearing (`remote_execution_client.h:153-157`: backend and frontend objects must agree on
its size), it has ~50 field references across the Tier-1 basebackup paths, and basebackup is our only
end-to-end regression net.

**Chosen:** make the four `static` helpers public with **explicit parameters** and no carrier type —
`HomerDpuFrontendExportRegion` / `...DestroyExport` / `...SendSetup` / `...SendClose` — and leave the
existing `HomerClientDpu*` helpers in place as thin wrappers that pass the stream's fields. Zero struct
change, zero call-site change, and the postmaster gets a surface that needs no client/session type.
Raw `void *` for the DOCA handles, exactly as the struct already stores them, so the header does not
drag DOCA into every translation unit that includes it.

**Gate:** the selected-DPU basebackup regression still passes (the module's only validated consumer at
this point). Build all targets, including the smokes.

### S2 — the arena + the frontend agent (D1 + D4′)

> **New source files, not more of `tuple_sink_service_process.c` (43,090 lines).** Everything below
> lands in dedicated translation units. The existing 43k-line hub gets *call sites only*.

**Two new TUs, one per side** (decided with the user — four was over-split):

| New file (citus `src/backend/distributed/utils/homer/`) | Owns |
|---|---|
| `homer_frontend_agent.{c,h}` | **host side.** Arena ABI + layout; create (postmaster), map (agent/backend), slot bind/unbind. The bgworker itself: registration, DOCA export via the S1b API, setup send, doorbell connect/backoff/recv, `kill(PostmasterPid, SIGUSR1)`. |
| `homer_service_dpu_doorbell.{c,h}` | **DPU side.** The doorbell listener (its own port, **separate** from the serial setup listener), one persistent connection per attached host, **EOF ⇒ invalidate that import**; and the DMA-the-spawn-slot + ring-the-doorbell path (S3). |

- **S2.1** Postmaster creates the arena at `_PG_init` (`!IsUnderPostmaster`, beside
  `EnsureSpawnRegionMapped`): a named POSIX shm object holding `N = 16` slots of
  {role-2 command mailbox, role-3 completion mailbox, role-5 result byte-ring} — **≈ 57 MiB**, under the
  ~100 MB DOCA single-region ceiling. The spawn region stays its **own** object, unchanged, because the
  non-DPU `--homer` host service still `shm_open`s it by name.
- **S2.2** Postmaster registers the frontend-agent bgworker (`InitializeCitusBackgroundWorker` +
  `RegisterBackgroundWorker`, mirroring `maintenanced.c:205`-`:207`).
- **S2.3** Agent maps both regions, DOCA-exports each with `HomerDpuFrontendExportRegion` (S1b), and sends
  **one setup message carrying two mmap exports** (`mmapExportCount = 2`; descriptors keyed by
  `mmapExportId`). *S1b's carrier-free API is precisely what makes this possible — it was worth doing.*
- **S2.4** The spawned backend `shm_open`s the arena and binds a slot index (prefer-own / adopt-free /
  fatal-on-exhaustion, mirroring `HomerDpuByteRingBind`). **No DOCA, no setup connection, no fork
  inheritance required.**
- **S2.5** Retire the Tier-2 `SELECTED_DPU_DMA` mailbox-open path in
  `remote_execution_backend_bridge.c` (`:2572`-`:2669`) — superseded, and never exercised.
  **But read it first:** it is currently the only implementation of the exporter ≠ reader shape (see
  "Prior art" under S0b). Mine it for patterns before deleting it.

**Gate:** the DPU service's engine shows host mmap imports for the arena and the spawn region
(`HomerDpuDmaImportHostMmapDescriptorForSetup`) — the exact thing whose absence caused the three-run hang.

### ✅ S2 DONE — GATE PASSED (July 9, 2026)

**The gate was not observable when it was written.** `homer_service_dpu_setup_tcp.c`'s import loop logged
only on *failure*, so "the engine shows host mmap imports" could only ever be inferred from the ack the
host received — i.e. from the DPU's own word, read on the far side of the wire. Added a success line, one
per exported mapping, on the cold path. Now the gate is a direct service-side observation:

```
tuple-sink service: DPU TCP setup mmap import accepted bridge_generation=321564234350815
  client_instance_id=550566416136833 export_index=0 mmap_export_id=1 descriptor_first=0 descriptor_count=48
tuple-sink service: DPU TCP setup mmap import accepted bridge_generation=321564234350815
  client_instance_id=550566416136833 export_index=1 mmap_export_id=2 descriptor_first=48 descriptor_count=1

homer frontend agent: exported arena (region=59767936 bytes, 48 rings, export_blob=283 bytes) and spawn
  region (region=17424 bytes, 1 ring, export_blob=279 bytes) to DPU 10.10.1.201:9727,
  accepted_rings=49 imported_blob_bytes=562
```
48 arena rings under `mmapExportId=1`, the role-8 spawn region under `mmapExportId=2`, zero DPU errors.
`/dev/shm/citus_homer_frontend_arena_v1` = 59,767,936 bytes. The agent stays resident and idle.

**Regressions, all green on the same binaries:**
- 4-role DPU-relay basebackup: `delivered_bytes=23233747354`, `CLOSE_ACK`, sender rc=0, consumer rc=0,
  19.42 s (prior band ~20.8 s).
- TCP transport smoke, all six legs + `--export-posix-shm --fork-reader`: both ends `ok`, `distinct_va=YES`.
- **GUC default-off verified**: with `citus.enable_homer_dpu_frontend_agent` unset, no agent process and no
  `/dev/shm` arena. Every existing `--homer` / basebackup deployment is untouched.

**What of D5 is actually validated, and what is not:**
- `HomerDpuDmaFindDescriptorRef` matching on `boundServiceSessionId` — **exercised**. The transport smoke
  calls it at five sites (`homer_dpu_tcp_transport_smoke.c:1431`, `:1527`, `:1657`, `:1847`, `:1983`) and
  is green. Basebackup does not touch it.
- The role-3 `boundServiceSessionId == 0` skip — **proven not to fire spuriously** (0 polls over 15 s with
  16 unbound rings imported), and, per the correction above, **proven not to be load-bearing until S4**,
  because the action is never granted without a session. It stays in: from S4 it saves 15 dead-ring polls
  per granted pass, and it costs nothing.
- Everything the arena's 48 rings will eventually do (bind, DMA, publish) is **still Tier 2**. S2 proves the
  *import*, not the *use*.

Commits: citus `008f4f20b`.

---

#### S2 — implementation design, settled by reading the code (July 9, 2026)

Six facts, each verified in source, that together make S2 much **smaller** than the sketch above.
Read these before touching anything; several contradict what a reasonable person would assume.

**F1. `mmapExportCount > 1` is genuinely implemented, not merely declared.**
`homer_service_dpu_setup_tcp.c:749`-`:781` loops `for (exportIndex < parts.mmapExportCount)` and calls
`HomerDpuDmaImportHostMmapDescriptorForSetup` per export, **with rollback** on partial failure. Each
export becomes its **own** `hostMmapImports[]` slot with its own descriptor slice and `ringRuntime[]`
(`homer_service_dpu_dma.c:5593`-`:5653`). *Caveat:* `HomerDpuComchSetupParts.mmapExport` /
`.mmapExportBytes` (`homer_dpu_comch_abi.h:571`-`:572`) surface **only export [0]** — a convenience
shortcut, not the import path. Do not mistake it for a one-export limit.

**F2. The bridge control block's `hostPublishOffset` is never read by the DPU.**
All DPU addressing of *host-published* control lines is per-descriptor:
`hostRingAddress + hostControlOffset` for the control line (`homer_service_dpu_dma.c:8472`, `:9047`,
`:9189`) and `+ hostRingOffset` for the data. So two exports with completely different internal layouts
coexist under one bridge header. This is what makes the arena and the spawn region exportable together.

> **⚠ CORRECTED (July 10, 2026).** The original F2 said this of **both** `hostPublishOffset` *and*
> `dpuCreditOffset`. Half wrong. **`dpuCreditOffset` IS read, and index-addressed:**
> `creditOffset = dpuCreditOffset + ringIndex * sizeof(HomerDpuBridgeDpuCreditLine)`
> (`homer_service_dpu_dma.c:9656`). The DPU-written credit lines are a contiguous array and the DPU treats
> them as one. Only the host-written publish array — the one the DPU would have to *read*, and the one place
> where batching the read would actually pay — goes unindexed.
>
> That asymmetry is not a curiosity; it is the reason grouped-control discovery costs one 64-byte DMA per
> ring per pass. See
> [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md) §6.
> The claim that *nothing* reads these offsets remains true only for `hostPublishOffset`, and that is the
> bug, not the design.

**F3. Roles 2/3/5 do NOT need bridge publish/credit lines.** The Tier-2 prior art proves it:
`homer_frontend_dma.c:1640`/`:1661`/`:1682` all set `hostControlOffset = 0` and
`controlBytes = offsetof(mailbox, slots)`. **The mailbox's own epoch header IS its control block.**
So the arena needs no `HostPublishLine[]`/`DpuCreditLine[]` arrays at all.

**F4. Grouped-control discovery is enrolled BY ROLE, and only for roles 1, 4, 7.**
`HomerDpuDmaDescriptorUsesHostPublishLine` (`homer_service_dpu_dma.c:639`, body ~`:2270`) returns true
only for `FRONTEND_CONTROL_SLOT`, `PAYLOAD_BYTE_RING`, `PAYLOAD_BYTE_RING_DPU_TO_HOST`. Everything else
is skipped at `:1055`. **The arena's 48 rings therefore cost ZERO discovery DMAs**, and a new
spawn-region role is inert until the DPU explicitly writes to it. (Also worth knowing: "grouped" control
reads are currently `controlCount = 1` — one 64-byte DMA per ring, `:8467`. The name is aspirational.)

**F5. There is NO descriptor role that fits the spawn region, and one is required.**
Every mmap export must cover ≥ 1 descriptor (`homer_dpu_comch_abi.h:508`, `:554`), every descriptor must
pass `HomerDpuBridgeDescriptorRoleMatchesShape` (roles 1-7 only, `homer_dpu_bridge_abi.h:283`-`:325`) and
`HomerDpuDmaValidateDescriptorForImport`'s per-role `minControlBytes`/`minSlotBytes` switch, whose
`default:` arm errors with *"host mmap descriptor role is unknown"* (`homer_service_dpu_dma.c:5410`).
Reusing role 2 would make the DPU treat the spawn region as a backend command mailbox. **Role 8 it is.**

**F7. ⚠ THE PLAN HAD A HOLE — pre-exported arena descriptors are UNADDRESSABLE as written.**
This is the finding that mattered. The DPU resolves a backend mailbox by the descriptor's *declared*
session id:
- **Role 2:** `HomerDpuDmaFindDescriptorRef` (`homer_service_dpu_dma.c:5079`) matches
  `descriptor->serviceSessionId == serviceSessionId && descriptor->descriptorRole == role` (`:5123`),
  under a matching `bridgeGeneration` (`:5114`). The key is
  **`(bridgeGeneration, descriptor.serviceSessionId, descriptorRole)`** — not `serviceSinkId`, not
  `sessionUID`, not `(importIndex, ringIndex)`. First match wins; duplicates are not detected (`:5142`).
- **Role 3:** `HomerDpuDmaSubmitBackendCompletionPulls` (`:1205`) scans **every ring of every active
  import** (`:1240`), filters **only by `descriptorRole`** (`:1257`), and submits a control-read DMA per
  match. The session is recovered afterwards from `descriptorRef.serviceSessionId` (`:1284`) via
  `HomerServiceDpuFindSelectedSession` (`tuple_sink_service_process.c:38761`).

But the agent declares all 48 arena descriptors **at startup, before any session exists**, so they carry
`serviceSessionId = 0`. Consequences:
1. Role 2 is unresolvable: `FindDescriptorRef(gen, S, ROLE_2)` never matches a slot declared with 0.
   **This is the real blocker, and it is fatal to S4.**
2. Role 3 polls dead rings: the enumerator would issue a completion-mailbox control-read DMA for each of
   the 16 arena slots, of which at most a few are ever bound.

There is no late-binding key in the descriptor ABI (no `arenaSlotIndex`, no opaque tag), and `ringIndex`
is pinned to the descriptor's table position (`:5365`), so it cannot carry one.

> ### ⚠ CORRECTED BY EXPERIMENT (July 9, 2026) — item 2 as first written was WRONG
>
> The first revision of this section claimed: *"Role 3 breaks the S2 gate itself... From the instant the
> arena is imported, the DPU would submit 16 completion-mailbox control-read DMAs per scheduler pass...
> Importing the arena is enough to trigger it; no session required."*
>
> **Both halves are false, and a probe build proved it.** With the arena imported (48 rings, 16 of them
> role-3, none bound) and the D5 skip **removed**, a temporary counter at the top of
> `HomerDpuDmaSubmitBackendCompletionPulls` recorded **zero calls** across a 15-second window. The
> scheduler never *grants* the `DPU_BACKEND_COMPLETION_PULL` action while no selected-DPU session exists,
> so the enumerator body never runs. The S2 gate is not at risk.
>
> Also wrong: *"each landing in `FindSelectedSession(0)`."* An unbound slot's mailbox is all zeros, so
> `publishedEpoch (0) != consumedEpoch + 1 (1)` and **nothing ever stages**. The semantic layer is never
> reached; the "unknown selected-DPU session" error (`tuple_sink_service_process.c:39380`) cannot fire.
>
> **What is actually true.** From S4 on — as soon as ONE selected-DPU session exists and the action is
> granted — the enumerator iterates **every ring of every import** filtering only on `descriptorRole`
> (`:1250`, `:1257`), so it polls all 16 arena role-3 mailboxes and finds 15 of them dead. That is a
> **silent, perpetual PCIe control-read tax proportional to arena size**, not a startup storm and not an
> error. Silent is worse: it would have surfaced at S6 as an unexplained throughput gap, with nothing in
> any log to point at it.
>
> **This is exactly the failure mode the plan's standing risk #1 warns about**, and I nearly wrote it into
> the plan as a fact. The probe cost four minutes. *Reason from the code; then check the reasoning against
> the machine.*

**F6. ⚠ LANDMINE — `EnsureSpawnRegionMapped()` unconditionally `memset`s the whole region.**
`remote_execution_backend_bridge.c:1709` opens `O_CREAT|O_RDWR` **without `O_EXCL`**, `ftruncate`s to
`sizeof(CitusRemoteExecBackendSpawnRegion)` if the size differs, and `memset`s at `:1756`. It is
idempotent *within* a process (`if (SpawnRegion != NULL) return`), which is why the postmaster's second
call from the SIGUSR1 hook (`:3083`) is harmless. **The agent must never call it** — it would wipe the
postmaster's `postmasterPid`/`slotCount` and any in-flight request. The agent needs an attach-only
helper. The same `ftruncate` is why the arena must be a **separate object**: a single fused object would
be truncated back down by whichever process called `EnsureSpawnRegionMapped` first.

##### Measured sizes (July 9, 2026, `sizeof` on this tree)

| Component | bytes | `controlBytes` | `slotBytes` |
|---|---|---|---|
| `CitusRemoteExecLocalCommandMailbox` (role 2) | 3,202,584 | 24 | 50,040 |
| `CitusRemoteExecLocalCompletionMailbox` (role 3) | 270,552 | 24 | 33,816 |
| SQL result byte ring (role 5) = 64 + 256×1024 | 262,208 | 64 | 1 |
| **per arena slot** (each component 64B-aligned) | **3,735,424** (3.562 MiB) | | |
| **arena, N = 16** | **≈ 57.0 MiB** | | ~43 MB headroom under the ceiling |
| `CitusRemoteExecBackendSpawnRegion` (role 8) | 17,424 | 16 | 544 |

##### Decisions made within the plan's direction (per the "record your reasoning" rule)

1. **New bridge role 8 `BACKEND_SPAWN_REGION`** — shape identical to role 2
   (`{COMMAND, DPU_TO_HOST, FIXED_SLOT, COMMAND_SLOT|FIXED_SLOT_RING}`), distinguished only by
   `descriptorRole`, and deliberately **not** grouped-control enrolled (F4). Its `minControlBytes =
   offsetof(SpawnRegion, slots)` (16), `minSlotBytes = sizeof(SpawnRegionSlot)` (544). Bumps
   `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` 2→3 and `HOMER_DPU_COMCH_PROTOCOL_VERSION` 2→3. An old DPU
   receiving role 8 answers `BAD_DESCRIPTOR` — a clean rejection, not silent corruption.
   *Rejected:* smuggling the spawn-region offset through a `reserved` field of the setup header, to avoid
   the bump. Every other addressable thing in this ABI is self-describing via a descriptor; this would be
   the one exception, and the ABI's own bounds checks would not cover it.
2. **The spawn region's ABI is UNCHANGED in S2.** It is only *exported*, not restructured — no bridge
   prefix/suffix, no version bump, no `_v15` rename. This falls out of F3: its own 16-byte header is a
   legal `controlBytes`. The `arenaSlotIndex` field that `CitusRemoteExecBackendSpawnRequest` will need
   (see below) lands in **S3**, where it has a consumer, together with the `v14 → v15` name bump.
3. **The agent is GUC-gated, default OFF:** `citus.enable_homer_dpu_frontend_agent` (`PGC_POSTMASTER`).
   Without this, every existing `--homer` and basebackup postmaster would start an agent that DOCA-exports
   57 MiB and then fails to reach a DPU setup listener, restarting every 5 s. The arena is created only
   when the GUC is on. Setup host/port/device keep reusing the existing `HOMER_FRONTEND_*` env vars.
4. **Build the `BackgroundWorker` struct directly; do not use `InitializeCitusBackgroundWorker`.**
   That helper hardcodes `bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION`
   (`background_worker_utils.c:47`) and defaults `bgw_start_time = BgWorkerStart_ConsistentState`
   (`background_worker_utils.h:48`). The agent wants **neither**: no database connection, and
   `BgWorkerStart_PostmasterStart` so it is attached before any client opens a session. Ten lines of
   struct fill beats adding a flags field to a shared Citus helper for one Homer-specific caller.
5. **S2.4's backend-side bind: helper in S2, CALL SITE in S3.** Nothing spawns a `SELECTED_DPU_DMA`
   backend until S3 exists, so the call site cannot be exercised in S2 and would be dead-on-arrival code
   validated by nothing. `HomerFrontendAgentBindArenaSlot()` ships in S2 with the arena ABI; S3 calls it.
   Corollary: **the DPU allocates the slot index and stamps it into the spawn request** — it must, since
   it is the party that DMAs into that slot's mailboxes. The backend then does a *checked* claim
   (`FREE → CAS to mine`, fatal otherwise), which also catches a DPU/host allocator disagreement after a
   DPU restart. Slot release is `on_proc_exit` (covers `ereport(FATAL)`); a hard backend crash leaks a
   slot, but that already triggers a postmaster crash-restart cycle which recreates the arena.
6. **S2.5 moves to S7.3.** Retiring the Tier-2 `SELECTED_DPU_DMA` mailbox-open path now would delete the
   only worked example of exporter ≠ reader while S3/S4 are still unwritten — and it is dead code that
   costs nothing to keep. It retires alongside the rest of `homer_frontend_dma*`, which is where it
   belongs. **The plan's instruction to read it first was worth following:** F3 (mailbox-header-as-control-
   block) and F1's multi-export composition both came straight out of it
   (`homer_frontend_dma.c:1261`-`:1304`, `:1590`-`:1697`, `:1704`-`:1735`).

7. **D5 — the DPU BINDS a ring to a session; `HomerDpuDmaRingRuntime.boundServiceSessionId`.**
   *Forced by F7. This is the one place S2 must add real DMA-engine surface.*

   The descriptor says what the **host declared** at setup. A ring of a shared arena has no session at
   that moment. So the DPU needs somewhere to record what it **bound** — and it already has exactly the
   right home: `import->ringRuntime[]`, a DPU-private per-ring array `calloc`'d beside the descriptor
   copy at `homer_service_dpu_dma.c:5593`-`:5594`.

   Add `uint64_t boundServiceSessionId`, **initialised at import from `descriptor->serviceSessionId`**,
   meaning *"the session this ring currently serves, as the DPU understands it."* Then:
   - `HomerDpuDmaSubmitBackendCompletionPulls` skips a ring whose `boundServiceSessionId == 0`
     (an unbound arena slot has no session; do not poll it). **This is the fix for F7's item 2.**
   - `HomerDpuDmaFindDescriptorRef` matches on `boundServiceSessionId` rather than
     `descriptor->serviceSessionId`. **Fix for F7's item 1.**
   - **S3** adds `HomerDpuDmaBindRingSession()` / `...UnbindRingSession()`, called by the slot allocator
     when it stamps `arenaSlotIndex` into the spawn request, and at session close.

   Both changes are **behavioural no-ops today**: every existing exporter declares a nonzero
   `serviceSessionId` (client role-1 at `homer_client.c:2875`; Tier-2 roles 2/3/5 at
   `homer_frontend_dma.c:1647`/`:1668`/`:1694`). So the S2 basebackup + smoke regressions prove the
   plumbing is inert before S3 gives it teeth.

   *Rejected: mutate `import->descriptors[i].serviceSessionId` in place.* It is a DPU-private copy, so it
   would work and needs zero resolver changes — but the descriptor is documented as "what the host sent
   over the cold setup channel" (`homer_dpu_bridge_abi.h:191`-`:194`), and silently making it mean
   something else is a trap for the next reader, and for any diagnostic that prints it.
   *Rejected: add an `arenaSlotIndex` lookup key to the descriptor ABI.* Bigger ABI change, touches every
   resolver, and does nothing about the role-3 polling storm — which is the half that actually breaks.
   *Rejected: per-session export by the backend.* That is the D4 alternative; it reintroduces DOCA init on
   the session-open path, which is the entire thing D4 exists to avoid.

   > **D5 turned out to be more than a lookup fix, and the field is now load-bearing for later work.**
   > `boundServiceSessionId` is the **reverse cookie**: the `resource → waiter` edge. A recv-CQ hands back a
   > `wr_id`; a `doca_pe` task hands back `doca_data` (we use it: `taskUserData.ptr = slot`); a host CPU
   > store into DOCA-exported memory hands back **nothing**, because there is no `WRITE_WITH_IMM` across
   > PCIe. So when a collector learns "ring 37 advanced," the map back to the waiter has to be one we keep.
   > D5 put it, by accident, in exactly the right struct: `HomerDpuDmaRingRuntime` is the *frontier owner*
   > the continuation-graph plan prescribes, and it already carries every frontier field and no waiter.
   > **Keep the field there.** It widens to a `HomerWaitRegistration` later — a type change, not a redesign.
   > See [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md)
   > §7–§8, and the long note at the field itself (`homer_service_dpu_dma.c`).

##### Two pre-existing bugs in `TupleSinkServiceSubmitBackendSpawnRequest`, found while tracing (fix in S3)

Not on S2's path; do not fix them here. But S3 rewrites this submitter's DPU twin and keeps the host one
(S3.5), so fix both then, in both:
1. **Slot reservation is load-then-store, not CAS** (`tuple_sink_service_process.c:18913`). Two concurrent
   submitters can claim one slot. Latent today only because one service is single-threaded — but a host
   service and `homer_frontend_dma_lifecycle`'s own submitter can both be live.
2. **The success path stores `state = FREE` before reading `response.launchedPid`**
   (`:19003` then `:19018`). A racing producer may re-claim the slot between the two, and the postmaster
   `memset`s `slot->response` when it processes the new request. Narrow, real, and it would present as an
   impossible `launchedBackendPid`.
3. (Lesser) The wait loop at `:18976` has no timeout and no postmaster-liveness check.

##### Arena region layout (`/citus_homer_frontend_arena_v1`)

```
+0      HomerDpuBridgeControlBlockHeader   64 B   -- advisory: the DPU takes its copy from the
                                                     setup message (homer_service_dpu_dma.c:5643).
                                                     Kept in-region for gdb/`hexdump` attribution.
+64     HomerFrontendArenaSlot slots[16]          -- each: { commandMailbox | completionMailbox |
                                                     byteRingControl + byteRing }, components 64B-aligned
```
Descriptors 0..47 = arena (`mmapExportId = 1`), ring index `3*i + {0,1,2}` for slot `i`;
descriptor 48 = spawn region (`mmapExportId = 2`). `ringIndex` must equal the descriptor's position
**globally across the message** (`homer_dpu_comch_abi.h:487`), and each export covers a **contiguous**
descriptor slice (`:505`-`:529`).

##### S2 file-by-file

| File | Change |
|---|---|
| `homer_dpu_bridge_abi.h` | role 8 + shape arm; `PROTOCOL_VERSION` 2→3 |
| `homer_dpu_comch_abi.h` | `PROTOCOL_VERSION` 2→3 |
| `homer_service_dpu_dma.c` | one `case` arm in the `minControlBytes` switch (`~:5380`); **plus D5**: `HomerDpuDmaRingRuntime.boundServiceSessionId` (init at `:5594`), the `== 0` skip in `SubmitBackendCompletionPulls` (`:1257`), and `FindDescriptorRef` reading it (`:5123`) |
| `homer_frontend_agent.{c,h}` | **new.** Arena ABI + create/attach/bind/release; the bgworker (register, export both regions, compose the 2-export setup message, send, log). |
| `remote_execution_backend_bridge.c` | attach-only spawn-region helper (F6); call arena-create + bgworker-register from the `!IsUnderPostmaster` branch (`:3119`) |
| `shared_library_init.c` | define the GUC |
| `src/backend/distributed/Makefile` | `utils/homer/homer_frontend_agent.o` into the `citus.so` list (`:50`-`:58`) |

**Deferred out of S2, tracked:** the 3.05 MiB role-2 mailbox is 64 slots × a 50,040-byte record — the same
fixed-size-union bloat as the 66 KiB control slot. Shrinking the ABI unions (S4 perf item, fix 3) shrinks
the arena by roughly 8×. Do not do it here.

##### Found during implementation and review (July 9, 2026)

- **⚠ INSTALL ORDER IS NOW LOAD-BEARING.** `citus.so` is built `-fvisibility=hidden` and has **four
  undefined** `HomerDpuFrontend{OpenDevice,CloseDevice,ExportRegionOnDevice,DestroyRegionExport}` symbols;
  they resolve at `dlopen` time from the **`postgres` executable**, which statically links
  `libhomer_client.a` and is linked `--export-dynamic`. So:
  **citus `make install` (installs the archive + citus.so) → postgres `ninja -C build` (RELINKS postgres
  against the new archive) → `meson install` → only then start PostgreSQL.**
  Starting PostgreSQL with a new `citus.so` against an old `postgres` fails every backend with
  `undefined symbol: HomerDpuFrontendOpenDevice`. (`HomerFrontendAgentMain` survives `-fvisibility=hidden`
  because `PGDLLEXPORT` marks it default-visible — verified with `nm -D`.)

- **The S1b API needed a device-scoped variant.** `HomerDpuFrontendExportRegion` opens a `doca_dev` **per
  call**; the agent exports two regions and they must share one device — opening the same PCI device twice
  from one process is not something DOCA promises, and the Tier-2 path has always shared one device across
  its four exports (`homer_frontend_dma.c:1267`-`:1279`). Added `HomerDpuFrontendOpenDevice` /
  `...ExportRegionOnDevice` / `...DestroyRegionExport` / `...CloseDevice`; the original call is now a thin
  open+export wrapper, so no existing caller changed.

- **`RLIMIT_MEMLOCK` checked, not assumed.** `doca_mmap_start()` pins pages and can fail with "the kernel
  failed to pin the requested amount of memory". `dbcomm` has `ulimit -l unlimited` on farnet1, so a 57 MiB
  arena is fine. Re-check on any new host before blaming DOCA.

- **⚠ A FREE arena slot must always already be zeroed.** The DPU chose `slotIndex` and stamped it into the
  spawn request *before* the postmaster forked the backend, so it knows the index all along; once it binds
  its ring it may DMA-read that slot's completion mailbox at any time. If the backend zeroed a dirty slot at
  bind, the DPU could observe the previous tenant's `publishedEpoch` and then watch it go **backwards**.
  Therefore `HomerFrontendAgentReleaseArenaSlot` zeroes the three components **before** publishing `FREE`,
  and `...BindArenaSlot` zeroes again defensively. *The comment the first draft carried — "no consumer reads
  this slot until the DPU is told its index strictly after this function returns" — was simply false.*

- **Arena slot leak across a postmaster crash-restart (known, loud, deferred to S3).** `_PG_init` does not
  re-run on crash-restart, so `HomerFrontendAgentCreateArena`'s whole-arena `memset` does not re-run either,
  and slots left `BOUND` by SIGKILL'd backends stay `BOUND`. The DPU's allocator table *does* reset (new
  import, new generation), so it will re-offer slot 0 and the backend's **checked claim fails loudly** with
  `arena slot 0 is already bound` — attributable, not silent aliasing. That is the checked claim earning its
  keep. **S3 adds an `ownerPid` reaper to the agent's idle loop** (`kill(pid, 0)`), which also covers a
  hard-crashed backend.

- **`ereport(ERROR)` in the agent IS the retry loop.** ERROR → bgworker `sigsetjmp` → `proc_exit(1)` →
  postmaster restarts after `bgw_restart_time` (5 s) → a **new pid mints a new `bridgeGeneration`**, and
  process exit already tore down every DOCA object. So a DPU service that starts *after* PostgreSQL is
  handled with no extra code. The consequence — the graceful `shutdown:` path is skipped, no `CLOSE` is
  sent, and the DPU keeps a stale import until its next DMA against it fails fatally — is the **decided R3
  behaviour**, not an oversight.

- **New `/dev/shm` object to clean:** `/dev/shm/citus_homer_frontend_arena_v1` (57 MiB). Neither it nor the
  spawn region is `shm_unlink`ed at shutdown, matching existing practice. Added to `AGENTS.md`.

---

## D6 — the forward index: a session holds handles to its rings (decided July 9, 2026)

**Supersedes D5's `continue`. Keeps D5's field.**

> **D6 is continuation-graph edge #1.** It is not a local optimisation. The async-continuation runtime needs
> two edges, in opposite directions, and **neither exists in the engine today**:
>
> | Edge | Direction | Needed by | Today | Status |
> |---|---|---|---|---|
> | **forward index** | waiter → resource | *pushers*: "I have a session; which ring do I DMA into?" | `FindDescriptorRef` scans imports × rings, **per transaction** | **this is D6** |
> | **reverse cookie** | resource → waiter | *collectors*: "ring 37 advanced; who was waiting?" | `FindSelectedSession` scans 64 sessions | D5's field, in weak form; unscheduled |
>
> The reverse cookie is where `HomerDependencyResolve()` lands. Full reasoning — why everything is polled,
> why a completion queue is only *aggregation + a cookie slot*, and why the ready queue is a hand-rolled CQ
> missing the cookie:
> [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md).
> The ten places the missing edges surface as linear scans:
> [`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md).

Both consumers of a session's rings perform a **reverse lookup by linear search**, because the forward map
was never built:

| consumer | has | needs | does |
|---|---|---|---|
| role-2 publish (`...StageOneBackendCommandForPublish`, `:39260`) | the session | its command mailbox ring | `HomerDpuDmaFindDescriptorRef` — scan all imports × rings comparing `descriptor->serviceSessionId` — **once per `START_COMMAND`, i.e. per transaction** |
| role-3 pull (`HomerDpuDmaSubmitBackendCompletionPulls`, `:1205`) | nothing | any ring with a completion | scan all imports × rings filtering on `descriptorRole` (`:1250`, `:1257`), then reverse-map ring → session via `HomerServiceDpuFindSelectedSession` (`:39377`) |

There is exactly one moment when both identities are in hand: **the bind**. So build the map there.

**D6.** When the DPU hands arena slot `k` to session `S` (S3's allocator), it stamps
`boundServiceSessionId = S` on rings `3k+0..2` **and** resolves and caches three
`HomerDpuDmaDescriptorRef`s on `HomerServiceDpuSelectedSessionState`:
`backendCommandMailboxRef`, `backendCompletionMailboxRef`, `resultByteRingRef`.

Consequences:
- role-2 publish becomes a struct field read instead of a per-transaction scan.
- role-3 pull iterates **selected sessions awaiting a completion** — exactly the set that armed the grant
  (`selectedBackendCompletionWaitCount`, `:39966`) — instead of every ring of every import.
- `HomerDpuDmaFindDescriptorRef` survives at **one** call site: bind. A reverse lookup is legitimate
  precisely where the binding is established, and it is then once per session open, not per transaction.
- **D5's `continue` becomes unreachable**, because no ring enumeration remains. Demote it to a one-shot
  loud diagnostic: *"a role-3 ring was enumerated with no binding — grant and iteration bookkeeping
  disagree."* Same shape as the staged-head liveness guard (citus `584e01dcb`).
- **D5's `boundServiceSessionId` field STAYS.** It is the map's key and it is what makes an unbound arena
  ring unresolvable by `FindDescriptorRef`. D6 removes the scan, not the binding.

**This is not an arena workaround.** Every selected session gains the same index — Tier-2, the S1a client,
and the arena alike. The arena only made the missing index impossible to ignore.

**Stale refs are safe by construction.** A `HomerDpuDmaDescriptorRef` carries `bridgeGeneration` and
`ringGeneration`, and every submit revalidates both (`homer_service_dpu_dma.c:1771`, `:2031`). A cached ref
that outlives its import fails loudly instead of addressing the wrong host memory.

**Layering.** `HomerDpuDmaSubmitBackendCompletionPulls` lives in the DMA **engine**, which knows nothing
about sessions — so from where it sits, scanning by role is the only thing it *can* do. The fix moves the
choice up a layer: the engine offers `HomerDpuDmaSubmitBackendCompletionPullForRef(engine, ref)`, and the
service decides which refs. The TCP transport smoke already builds refs via `FindDescriptorRef` at five
sites (`homer_dpu_tcp_transport_smoke.c:1431`, `:1527`, `:1657`, `:1847`, `:1983`), so it converts for free.

*Rejected: a ring-level ready queue.* `HOMER_DPU_DMA_READY_QUEUE_HOST_TO_DPU_BACKEND_COMPLETION` is
**already declared** (`homer_service_dpu_dma.c:113`) and mapped by role (`:5857`), and is **never pushed
and never popped** — dead scaffolding, a fourth instance of "the design was anticipated, the implementation
took a shortcut." The shape does not transfer: for command pull (`:1195`) and payload pull (`:3734`) the
*host* announces readiness by advancing a publish line, and grouped-control discovery pushes the ref. For
backend completion the host announces nothing the DPU can see without a DMA read — **the poll IS the
discovery** — so the queue would have to be refilled every pass with exactly {rings whose session awaits a
completion}. That is a materialization of the session set, plus a queue. Session iteration is that set,
without the queue.

*Rejected: arm per ring instead of per session.* The arming is already correct. A session awaits at most
one completion at a time, so the per-session armed set and the per-ring armed set are the same set. The
arming was never the problem; the execution ignores it.

**Sequencing.** S3 builds the map (bind lives there). **S4.1** switches the consumers, deletes the scan, and
demotes the skip. Do not switch consumers in S3: role-3 iteration by session is only testable once a
session exists.

**Also retracted here:** an earlier note said "S4 will need a `boundServiceSinkId` twin" for the arena's
role-5 ring. That was an assumption. With the ref cached at bind, no `(session, sink)` lookup is needed for
the arena ring at all. What must still be checked in S4 is the **teardown** scans —
`HomerDpuDmaByteRingFrontierForServiceSink` and `HomerDpuDmaMarkImportSenderCloseReleased` search by
`(serviceSessionId, serviceSinkId)` and an arena role-5 descriptor carries `serviceSinkId = 0`. Either they
take the cached ref, or a sink binding is stamped. Decide it there, with the code in view.

---

## D7 — spawn-slot ownership is PARTITIONED BY PRODUCER (**TRANSITIONAL** contract, decided July 9, 2026)

> ### ⚠ EXPIRY, read first.
>
> **This split is scaffolding for a window, not a standing invariant.** It exists solely because two
> producer *classes* coexist between S3 and S7. The moment the host-service arm of
> `TupleSinkServiceSubmitBackendSpawnRequest` is retired (**S7.1**), there is exactly one claimant and the
> split becomes dead weight. Delete the range constants then and replace this section with:
>
> ### D7′ — the END STATE: exactly one claimant
> > Only the DPU service claims spawn slots. It is single-threaded and reaches the spawn region **solely by
> > DOCA DMA**, which offers no compare-and-swap on host memory. Correctness rests entirely on there being
> > **no second claimant**. Any new claimant reintroduces a race that no DPU-side code is able to detect.
>
> The invariant survives its own mechanism: **"exactly one producer" is strictly stronger than "disjoint
> ranges."** The partition disappears because it becomes redundant, not because the hazard went away.

> ### ⚠ CONTRACT (while it lasts) — `slots[0..15]` are the HOST's. `slots[16..31]` are the DPU's.
>
> `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_COUNT` is 32 (`remote_execution_backend_protocol.h:30`). The range
> is split by **producer**:
>
> - **`slots[0 .. 15]` — host-resident producers.** They contend (the host `citus_tuple_sink_service`
>   submitter, and `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`,
>   `homer_frontend_dma_lifecycle.c:188`), so they **must claim by CAS**.
> - **`slots[16 .. 31]` — the DPU service.** It is a single-threaded remote producer that reaches this
>   region **only by DOCA DMA**, which gives `memcpy` semantics across PCIe and **no compare-and-swap on
>   host memory**. It therefore *cannot* perform an atomic claim, and it does not need to: its range is its
>   own, and its free list is DPU-local.
>
> The postmaster consumer (`CitusRemoteExecPostmasterSigusr1Hook` → `ProcessSpawnRequestSlot`) is
> **range-agnostic**: it scans all 32 and dispatches on `state`. No ABI change.
>
> **Violating this split reintroduces a race that no code on the DPU side is able to detect.** Each
> producer asserts that the slot it reserved lies in its own range.

**Three pre-existing bugs, fixed in S3. Note their lifetimes differ — that is the interesting part.**

1. **Reservation is load-then-store, not CAS** (`tuple_sink_service_process.c:18909`-`:18921`). Two
   producers can both read `FREE` and both store `CLIENT_OWNED`. Fix: CAS `FREE → CLIENT_OWNED`, and on a
   lost CAS continue to the next slot rather than `break`. **Applies to the host range only** — the DPU
   cannot CAS, which is the whole reason for D7.

   ⚠ **This fix has a one-stage lifetime and no durable home.** Once the host arm is retired the DPU is the
   sole claimant, CAS is impossible *and unnecessary*, and this code is deleted with the arm. Fix it anyway
   (five lines, and it makes the transition window provably safe), but do not mistake it for the thing that
   protects us long-term. That is D7′.

2. **The success path publishes `FREE` before it reads the response** (`:19004` stores `FREE`; `:19012` and
   `:19018` then read `reservedSlot->response.launchedPid`). This is **not a writer race — it is a
   use-after-release.** The release store *is* the act of handing the slot away; the postmaster `memset`s
   `slot->response` at the top of `ProcessSpawnRequestSlot` (`remote_execution_backend_bridge.c:2980`) once
   a new owner publishes `REQUEST_READY`. The symptom is an impossible `launchedBackendPid`.

   **The fix is NOT a copy.** Hoist one `int32` read above one store:
   ```c
   int32 launchedPid = reservedSlot->response.launchedPid;   /* take it while we still own the slot */
   TupleSinkServiceAtomicStoreU32(&reservedSlot->state, ..._SLOT_FREE);
   sessionState->launchedBackendPid = launchedPid;
   ```
   The value was already being loaded — just too late. The **error path directly above already does this
   correctly**: it `snprintf`s `response.errorMessage` into the caller's buffer *before* releasing
   (`:18991`-`:18997`). Only the success path is inverted.

   **General rule, worth stating once:** a release-store publishes everything before it and *unpublishes*
   everything after it. Once you store the word that says "this resource is available", every field of that
   resource belongs to someone else. Extract before you publish. Ownership is a property of the protocol
   word, not of the address.

   **The rule gets stricter on the DPU side**, where the read and the release are two DMA tasks: the DPU
   must DMA-read the response, **await it**, and only then DMA-write `FREE`. Same family as the
   ⚠ DMA-BEFORE-DOORBELL invariant — nothing orders two DMA tasks for you unless you await them.

3. **The response wait loop (`:18976`) spins with no timeout.** Fix: a **generous** timeout and a loud
   error if it ever expires. **No postmaster-liveness polling** — decided with the user: this is an
   error path, and if it fires at all we have a bug elsewhere. A timeout that has never fired is the
   correct amount of machinery here.

   *(Unlike bugs 1 and 2, this one only matters once the DPU is the waiter: on a host service a hung
   spin stalls one process, but on the DPU it stalls the single scheduler thread that also drives DMA and
   RDMA for every other session. See "the spawn wait" under S3.3.)*

---

## D8 — reserve the arena's host publish-line array NOW (decided July 10, 2026)

> ✅ **DONE — citus `36a61a5a1`.** Arena ABI bumped `v1 → v2`
> (`/dev/shm/citus_homer_frontend_arena_v2`, **59,774,080 bytes**, `+6,144` = 2 × 48 × 64). That bump is
> **host-only**: `HOMER_FRONTEND_ARENA_SHM_NAME` is referenced by exactly one TU and the header is
> citus.so-only, so `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` stays `3` and no DPU redeploy rides on it. That
> asymmetry — host layout private, bridge layout shared — is what D8 was buying.

**What.** Add to `HomerFrontendArena`, at the head, immediately after the advisory bridge header:

```c
/* One HostPublishLine per arena ring, contiguous, index-addressed:
 *     bridgeHeader.hostPublishOffset = offsetof(HomerFrontendArena, hostPublishLines)
 *     line(ringIndex) = hostPublishOffset + ringIndex * 64
 * 48 x 64 B = 3 KiB on a 57 MiB region.  NOTHING WRITES OR READS THESE YET. */
HomerDpuBridgeHostPublishLine hostPublishLines[HOMER_FRONTEND_ARENA_RING_COUNT];
```

and set `bridgeHeader.hostPublishOffset` accordingly (it is `0` today).

**Why now, and why this is not speculative generality.** Roles 2/3/5 are excluded from grouped-control
discovery for a purely *typing* reason: a ring is enrolled iff its `hostControlOffset` points at a 64-byte
`HomerDpuBridgeHostPublishLine`, and theirs point at a 24-byte mailbox header or a
`CitusHomerPayloadByteRingControl` instead. They are not unschedulable — **they are unregistered.** Nobody
allocated them a line.

When we later want the DPU to discover backend completions and SQL-result frontiers with **one** DMA instead
of one per ring, the backend will stamp its line beside the mailbox epoch (a discovery *hint*; correctness
still comes from the mailbox's own epoch — precisely the split role 4 already has), and grouped-control will
read the array. If the space is already there, that lands with **no arena ABI bump and no coordinated
two-DPU redeploy.** If it is not, it costs both.

**Cost today: one struct field and 3 KiB.** Cost after S4/S5/S6 have validated against `v1`: a version bump
and a synchronised rebuild of the host and both DPUs. Buy the door while it is cheap.

**Do NOT also implement the writers or grouping here.** D8 reserves address space and nothing else. Grouping
(`controlCount`, `groupedControlBufferBytes`) and promotion of roles 2/3/5 are separate, later, and must be
done in that order — promotion *before* grouping is a regression, because it makes discovery submit 48
demand-independent DMAs per pass where D6's demand-driven poll submits one per waiting session.

Full reasoning, code pointers, and the migration order:
[`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md).

**Also do, cheaply, alongside D8:**
- ✅ **Make `controlCount` honest** (citus `14e655e11`). `HomerDpuDmaGroupedControlOwner.{controlFirstIndex,
  controlCount}` are **written and never read**, by two writers that contradict each other: task-slot init
  computes `controlCount = groupedControlBufferBytes / 64` and sets `ringIndex = INVALID` (the grouped
  design), and submit overrides both with `controlCount = 1` and a scalar `ringIndex` (the shortcut).
  Asserted and commented. Five ready-queue kinds were born dead behind exactly this kind of silence.
- ✅ **Name D5's `boundServiceSessionId` as the reverse cookie** (citus `722166988`), so widening it to a
  `HomerContinuationRef` is later a type change rather than a redesign.

---

## D8b — reserve the arena's DPU CREDIT-line array too, and leave it SWITCHED OFF (decided July 10, 2026)

> ✅ **Reserved — citus `36a61a5a1`.** `HomerFrontendArena.dpuCreditLines[48]`, another 3 KiB.
> `bridgeHeader.dpuCreditOffset` stays **0**, deliberately.

**Why this is not "D8 for symmetry".** `hostPublishOffset` is written and **never read** by the DPU —
every ring is addressed from its own descriptor. `dpuCreditOffset` is *read*, it *hard-gates* three submit
paths, and it is the **destination of a DMA write**:

```
reject if  import->bridgeHeader.dpuCreditOffset == 0          (:3064, :3286, :3616)
dst      =  descriptor->hostRingAddress + dpuCreditOffset + ringIndex * 64      (:9716)
```

And the arena's role-5 SQL result ring **is** a host→DPU byte ring —
`HomerDpuDmaDescriptorIsHostToDpuByteRing` accepts role 5 (`:5680`) — so
`HomerDpuDmaSubmitMirroredByteRingConsumedHeadPublications` (`:4007`) will try to publish the DPU's
`consumedHead` back to it the moment a backend produces results and the DPU pulls them. **That is S4.**
Today it cannot: the arena declares no credit array, so `dpuCreditOffset == 0` and the publish is rejected.

So the credit array is not speculative like the publish lines. It is a **hard S4 prerequisite** that the
arena was missing, discovered while reserving the publish lines. Same 3 KiB, same closing window.

**Why the offset stays 0.** Every arena descriptor sets `hostRingAddress` to the *component* address
(`&slot.resultRingControl`), not the arena base. A nonzero `dpuCreditOffset` would pass the
`ringBytes`-relative bounds check and DMA a 64-byte credit line **into the middle of the result ring's own
payload storage**. Silent corruption. Zero rejects loudly instead. So the offset is turned on together
with the descriptor rebase, as one edit — **S4.0b**:

```
hostRingAddress   = arenaBase
hostRingOffset    = offsetof(arena, slots[k].resultRingStorage)
hostControlOffset = offsetof(arena, slots[k].resultRingControl)   /* SUPERSEDED -- see below */
ringBytes         = hostRingOffset + RESULT_RING_STORAGE_BYTES     /* ringStorageBytes = ringBytes - hostRingOffset (:9651) */
bridgeHeader.dpuCreditOffset = offsetof(arena, dpuCreditLines)
```

> **⚠ hostControlOffset value SUPERSEDED (July 10, 2026).** For an enrolled ring, `hostControlOffset` is
> the PUBLISH-LINE pointer (the roles-1/4/7 convention), not the byte-ring control address — and S4.0b
> now also enrolls the arena role-5 rings in grouped-control discovery, because a codex-verified trace
> showed **role-5 discovery never existed for any path** (whitelist = {1,4,7} only,
> `HomerDpuDmaDescriptorUsesHostPublishLine` `homer_service_dpu_dma.c:6632`). Set
> `hostControlOffset = offsetof(arena, hostPublishLines[k*3+2])`, `controlBytes = sizeof(HomerDpuBridgeHostPublishLine)`.
> Nothing DPU-side reads the role-5 byte-ring control, so no address is lost. Full expanded S4.0b spec
> (five sub-parts a-e, incl. `boundServiceSinkId` and the protocol v4 bump) lives in
> [dpu_crossnode_command_completion_bridges_design.md](../../implementations/citus/transport/dpu_crossnode_command_completion_bridges_design.md) §4 P0.

Both descriptor forms are legal — the DPU only ever computes `hostRingAddress + <offset>` (`:8684`) — and
the rebase **cannot** accidentally enrol roles 2/3/5 in grouped-control discovery, because enrolment is a
role whitelist (`:5645`), not a typing test. Layout order `[header][hostPublishLines][dpuCreditLines][rings]`
matches what the host frontend already builds at `homer_frontend_dma.c:292-293`.

---

## ✅ D10 — a listener is admitted by a POLL-DUE OBLIGATION, not by a readiness claim (validated July 10, 2026)

> **IMPLEMENTED AND VALIDATED — citus `84ac374ef`.** The setup listener now owns a private
> collector/action/source, a sticky poll-due obligation, and a reserved lifecycle grant. C′ is gone.
> Defect B is **partially** fixed: this collector is now 1:1 with its feedback, while the other eleven
> DPU collectors still alias `dpuDmaSource` / `dpuDmaFeedback`
> (`tuple_sink_service_process.c:38425`-`:38443`).

> **Ordering decided with the project owner after an independent Codex review.** The owner's initial
> preference was "fix the shared feedback state (B) first, because we want a good fix, not a fast fix."
> The review argued that (B)-first is both *incomplete* and *insufficient for the listener*. The agreed
> order is **1 → 2 → 3 → 4** below. Both positions, and why the second won, are recorded here.

### What was wrong, in one line

Before S3.1b, the DPU setup listener's `accept()` was fused inside
`HOMER_PROGRESS_ACTION_DPU_PE_DRAIN`; the current historical comment records that removed control flow at
`tuple_sink_service_process.c:41975`-`:41980`. A backoff policy correct for a DOCA progress engine therefore
silently disabled a correctness-critical listener. Full mechanism, evidence, and **four** defects (A, B, C
and the newly-found **D**) in
[`dpu_scheduler_arm_execute_mismatch.md` §0 / §0b](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md).

### The three corrections that decided the order

1. **Fix A alone unwedges the listener.** Measured (C′ reverted on the DPU tree, A kept, arena imported):
   `pedrain_grants = 1,250,010 / 2,500,001` passes, listen backlog 0. So the wedge needed A **and** B
   together. C′ is *not* load-bearing — which frees us to replace it without passing a liveness cliff.
2. **`runnableMachineWork` is always true (defect D).** The heartbeat machine
   builder `HomerServiceBuildPeerAndMaintenanceMachineCandidates` (`:41075`) appends maintenance
   unconditionally when its always-present collector is a candidate (`:41094`-`:41102`), so blind-poll
   backoff is permanently armed in the collector phase. Measured on an idle service:
   `phase=2 runnable=1 machines=1`. A listener's liveness
   therefore must not depend on backoff behaviour **at all**.
3. **`knownExpectedWork = true` does not guarantee execution.** Budget reservation happens *after* the
   backoff gate in `HomerServiceMachineBaselineAppendCollectorAction`
   (`tuple_sink_service_process.c:10007`, gate/reservation at `:10037`-`:10049`), so a non-blind
   collector still loses to the total collector quota and to fixed collector ordering. **Neither C′ nor
   a fixed B guarantees the listener runs.** Only a **reserved
   admission** does. *(This came from the Codex review; neither the diagnosis nor the first fix caught it.)*

### The decision

> **A listener advertises a durable `pollDue` obligation on an interval — cleared and rearmed only when
> the poll actually executes — carried by its own collector, action and progress source, and admitted
> through a reserved budget line that generic collectors cannot consume.**

`knownExpectedWork` then means *"a poll is due"*, which is true and maintained, rather than *"an item is
ready"*, which a listener cannot know without a syscall. The implementation is
`HomerServiceDpuSetupTcpListenerPollInterval` → `HomerServiceDpuSetupTcpArmPoll` →
`HomerServiceDpuSetupTcpServerBeginSchedulerPass` (`homer_service_dpu_setup_tcp.c:240`, `:256`, `:282`),
with the obligation retired only at the actual poll boundary (`:314`, `:401`). **The pattern already
existed in-repo** for the
RDMA peer listener — `TupleSinkServiceArmListenerLiveness` / `listenerLivenessDue` /
`peerSchedulerFacts.listenerPollDue` (`remote_execution_peer_transport_rdma.c:1939`, `:1954`, `:1987`,
`:10803`; consumed at `tuple_sink_service_process.c:41081`). S3.1b copies it; S3.2 reuses it.

Seen this way, **C′ (`knownExpectedWork = started`) is the degenerate case with `interval = 0`.** It works,
for a reason it does not state, at the cost of one `accept()` + one empty `doca_pe_progress()` + up to two
O(1024) import-table scans on every idle pass. **S3.1b reverted it.** The candidate path still performs
four O(1024) `HomerDpuDmaGetSchedulerFacts` scans per pass — one per plan phase — so the landed saving is
one idle `accept()`, one empty `doca_pe_progress()`, and two scans, not the elimination of all scans.

### What validation taught us

- **A due fact and reserved admission are separate obligations.** Backoff exemption is weaker: it stops
  feedback suppression, but does not bypass the total/collector caps or fixed ordering. The two named sets
  are deliberately different: `HomerServiceProgressCollectorBypassesBlindBackoff`
  (`tuple_sink_service_process.c:9691`) contains three collectors, while
  `HomerServiceProgressCollectorReservedLifecycleAdmission` (`:9709`) contains only the setup listener.
- **A listener needs private feedback, but that is not the complete fix B.** The setup listener now maps
  1:1 to `ProgressRegistry.dpuSetupListenerSource` / `.dpuSetupListenerFeedback`
  (`tuple_sink_service_process.c:3464`-`:3473`, `:38425`-`:38443`); eleven DPU data-path collectors still
  share `dpuDmaFeedback`.
- **Proxy arming can hide real demand.** Reverting C′ exposed that `DPU_PE_DRAIN` had accidentally kept
  detached-import reclaim alive merely because the socket listener was started. The safe revert required
  the explicit `HomerDpuDmaSchedulerFacts.detachedImportCount` fact
  (`homer_service_dpu_dma.h:147`-`:164`; populated by `HomerDpuDmaGetSchedulerFacts` at
  `homer_service_dpu_dma.c:1024`-`:1089`).
- **Enum exhaustiveness is manual here.** Nearly every source/collector/action switch has `default:`.
  `HomerServiceDefaultCpuClassForProgressSource` is the only assignment to `sourceCore->cpuClass`; omitting
  the new source there would silently leave it `HOMER_PROGRESS_CPU_CLASS_INVALID`
  (`tuple_sink_service_process.c:15014`-`:15043`, `:15119`-`:15126`). `-Wswitch` did not protect the edit.

### The agreed order

| # | Step | Why here |
|---|---|---|
| 1 | ✅ **S3.1b — listener lifecycle admission** on the *existing* setup listener | **Landed and validated in `84ac374ef`** against the live agent-on regression net. |
| 2 | **S3.2 — the doorbell** reuses that machinery | Copy the validated two-predicate + poll-due + reserved-line implementation; inherits none of A/B/C/D. |
| 3 | **B — per-collector feedback redesign**, and decide what `runnableMachineWork` should mean (D) | Not needed for listener liveness once (1) lands. **But it must land before any S6 number**: B aliases the empty/productive streaks of all eleven DPU collectors, including the data-path ones, so cadence measured on it cannot be explained. |
| 4 | **A refinement** — an enrolled ring **index vector / bitset**, not a count | Makes enrolled membership the single source of truth instead of a predicate evaluated in two places. Also rename: it is an *enrolment* count, not an *immediately-submittable* one. |

### What B's fix actually requires (do not under-scope it)

Not one field. `HomerServicePopulateCollectorFeedbackFacts` (`tuple_sink_service_process.c:40431`) copies
**all three** backoff inputs — `recentEmptyPolls`, `recentProductivePolls`, `lastCollectedTick`
(`:40446`-`:40448`) — from a struct that **eleven** DPU collectors share (`:38425`-`:38436`). The write
path is keyed on the source: `HomerServiceUpdateProgressFeedbackAtTick` (`:13429`-`:13445`) resolves through
`HomerServiceProgressFeedbackForSource` (`:13377`), then updates the shared streaks at `:13617`-`:13626`.
Every other collector family is 1:1 collector→source. Moving `lastGrantedTick` alone leaves the empty and
productive streaks corrupted.

A complete fix must:
- move `lastGrantedTick`, `consecutiveEmptyGrants`, `consecutiveProductiveGrants` to per-collector storage,
  keeping source-level feedback only for source-wide frontiers, aggregate readiness and statistics;
- carry collector identity into the compiled plan — `HomerProgressGrant` / `HomerProgressCompiledActionGrant`
  currently lose it, and `HomerServiceMachineBaselineAppendCollectorAction` (`:10007`) is the last place it
  exists. It may need a collector **mask**, since one action can stand for several collector causes;
- advance the per-collector clock at **dispatch** (`HomerServiceExecuteProgressExecutionPlan`, `:43117`,
  dispatch loop at `:43129`), not at candidate construction, or a budget-dropped candidate will look
  granted;
- handle peer **bundled** execution: `HomerServiceFinishPeerControlProgressGrant` (`:13762`) has
  phase-specific results, and updating every collector in the bundle from one aggregate result would
  recreate the aliasing in a new place.

**Deferred with fix B — two pre-existing accounting defects, neither fixed by S3.1b:**

1. `peDrainReadyCount` **double-counts**. A grouped-control-read submit increments both
   `inflightTaskCount` and `controlReadInFlight` (`homer_service_dpu_dma.c:8803`-`:8804`), while backend
   publish/completion-pull counters are subsets of `inflightTaskCount` too. The sum at
   `tuple_sink_service_process.c:40293`-`:40307` therefore inflates only `maxPolls` /
   `readyItemCountHint`; liveness is unaffected. Fold this into the fix-B collector-facts rework.
2. Every `lastPlan*` field in `HomerMachineBaselinePolicyState` is really **last-PHASE**, not last-pass.
   `HomerMachineBaselinePlanBudgetFinish` overwrites them after each of four phases, so readers normally
   observe SEMANTIC; `lastPlanLifecycleGrantCount` consequently reads 0 outside COLLECTOR
   (`tuple_sink_service_process.c:3144`-`:3158`, writes at `:9565`-`:9570`). Use the
   `HOMER_DPU_SETUP_STARVE_DIAG` counters for cadence questions until the accounting is redesigned.

Re-validate: alternating productive/empty siblings on one source; bounded escape under multiple grants per
pass; peer bundled setup/send/close feedback; tight total and blind collector budgets; idle listener
syscall + import-scan frequency; setup and doorbell backlog latency under sustained DMA; then agent-on
basebackup, `pgbench --homer`, backend-to-backend COPY, and all seven smokes.

### Rejected

*Reject: make "a connection is pending" an observable maintained fact (level-triggered readiness).*
The service cannot learn the kernel's listen backlog state without a syscall. `io_uring` multishot accept
would expose it through a mapped completion queue, but that is a whole new subsystem for a cold control
path; a helper thread or `SIGIO` is equally disproportionate on a single-threaded busy-poll service.
**A poll-due obligation is the correct abstraction precisely because readiness is unknowable here.**

*Reject: add `DPU_PE_DRAIN` to `HomerServiceProgressCollectorBypassesBlindBackoff`
(`tuple_sink_service_process.c:9691`-`:9705`) and stop there.*
The exemption bypasses **backoff only**. The collector stays blind for budget purposes and can still lose
the blind quota, the total collector quota, or the ordering. And it would exempt the DOCA PE drain too,
which legitimately *should* back off when nothing is in flight. Exemption is step 5 of S3.1b, not a fix on
its own.

*Reject: keep C′ as the resting state.*
It is dishonest (`started` ≠ "work is due"), it does not guarantee execution, and it pays a syscall plus two
1024-slot scans on every idle pass. It was only a temporary pre-S3.1b bridge; S3.1b removed it in
`84ac374ef`.

---

> ### D6, restated: it is continuation-graph edge #1
>
> D6 is not a local optimisation. The continuation runtime needs two edges, in opposite directions, and
> **neither exists**:
>
> | Edge | Direction | Needed by | Today |
> |---|---|---|---|
> | forward index | waiter → resource | *pushers* — "I have a session; which ring do I DMA into?" | `FindDescriptorRef` scans, **per transaction** |
> | reverse cookie | resource → waiter | *collectors* — "ring 37 advanced; who was waiting?" | `FindSelectedSession` scans 64 sessions |
>
> D6 builds the forward edge. The reverse edge is where `HomerDependencyResolve()` will land, on
> `HomerDpuDmaRingRuntime`, which is already the dependency owner the continuation plan prescribes and which
> already carries every frontier field and no waiter.

---

## D11 — the peer responder DEFERS its response; it does not reply early (decided July 10, 2026)

D9 folded S3.7's async responder into S3.3 with one sentence: *"the responder
(`HandlePeerOpenCommandSessionRequest`, which handles and replies in one call) needs the equivalent
built."* This section is that design, plus the read of the transport that justifies it.

### The constraint, established by reading the code

`TupleSinkServiceProcessIncomingMailboxRequest`
(`remote_execution_peer_transport_rdma.c:7816`-`:7862`) does exactly four things:

1. calls `requestHandler(...)` with a **stack-local** `CitusRemoteExecPeerResponseUnion response`;
2. reserves a pooled, connection-owned `TupleSinkServicePeerControlResponsePublishSlot`;
3. `TupleSinkServicePrepareResponseMessage(messageSequence, requestKind, opIndex, opGeneration, &response, …)`;
4. RDMA-publishes it.

**There is no deferral today.** Step 2 happens *after* the handler returns, so a handler cannot hold
the slot; and the handler receives no identity it could save. The only deferral-shaped thing that
exists is `HomerPeerResponsePostTicket` (`:7855`), which schedules work **after** the response is
already sent (payload-close only). Confirmed by reading the drain, not by grep.

**But nothing about the transport forbids deferral.** The response identity is four scalars taken
from the inbound message — `messageSequence`, `requestKind`, `opIndex`, `opGeneration` — the response
body is a POD union, and the publish-slot pool is already connection-owned with a send-CQ retirement
path. Deferral is therefore *additive*, not surgery.

### The two options, and why we are not taking the cheap one

| | **A — defer the response** (chosen) | **B — reply OK immediately, spawn asynchronously** |
|---|---|---|
| Peer OPEN response means | "your backend is forked, pid=N" (**today's meaning**) | "a session object exists" |
| Spawn failure surfaces | in the OPEN response, at `WAIT_PEER_OPEN` | one round later, as a synthesized command failure |
| New session states | none | `spawnState ∈ {NONE, IN_FLIGHT, DONE, FAILED}`, checked by every command publisher |
| New window | none | session open, backend absent, commands already in the mailbox |
| Code | ~150 lines transport + ~200 service, mechanical, isolated | ~200 lines spread across the session state machine |
| Kills the `WaitForCommandStartup` nested pump later | yes — it is the mechanism | no |

**B looked smaller and is not.** `CitusRemoteExecPeerOpenCommandSessionResponse`
(`remote_execution_peer_control_protocol.h:237`-`:255`) carries `peerServiceSessionId`,
`peerCommandDoorbellToken` and `peerCommandMailboxDescriptor` — *nothing* derived from the spawn — so
B **can** truthfully reply early. But then a spawn failure has no reader: the requester has already
RDMA-written its command into the peer command mailbox, and a backend that never forks means nobody
reads it. B must therefore build a *second* failure channel (`forcedCommandFailure`, `:20557`) and
teach every `backendLoopActive` consumer about a session whose backend does not exist. It trades 350
lines of isolated, mechanical machinery for 200 lines of semantic change in the part of the service
we understand least — and it gives up a property D9 itself valued when it rejected the
`SPAWN_RESPONSE`-over-doorbell alternative on *failure semantics*.

### The design

Additive, three pieces, no behaviour change for any handler that does not opt in.

1. **Transport (`remote_execution_peer_transport_rdma.{c,h}`).**
   `TupleSinkServicePeerRequestHandler` gains an out-parameter `HomerPeerDeferredResponseTicket *`.
   `ProcessIncomingMailboxRequest` **pre-fills** it with the four identity scalars plus the
   connection handle and its generation, then calls the handler. If the handler sets
   `ticket->armed`, steps 2-4 are skipped. New entry point
   `TupleSinkServicePostDeferredPeerResponseRdma(peerTransportState, ticket, response, &posted, err)`
   re-resolves the connection by handle+generation (the same revalidation
   `TupleSinkServiceResolveControlMailboxAction` already does, `:7864`), then runs steps 2-4.
   A connection that died in between yields `posted=false` and a **loud log** — not an error.

2. **Service: a deferred-peer-response queue**, small fixed array, each entry
   `{ticket, requestKind, serviceSessionId, response}` plus a readiness predicate. Drained by a new
   demand-armed collector `HOMER_PROGRESS_COLLECTOR_PEER_DEFERRED_RESPONSE` (exact work — **not** a
   reserved lifecycle grant; it is armed by a count, never by a poll obligation, so D10's machinery
   does not apply).

3. **Service: the spawn phase machine** owns the readiness. It never learns about peers.

**Why the queue is service-level and not a field on the session:** a spawn that fails must still post
a response, and the failure path resets the session (`TupleSinkServiceResetSession`). Hanging the
ticket off the session would make the reset silently drop the peer's reply — the requester would hang
in `WAIT_PEER_OPEN` forever, which is precisely the silent-hang class this project keeps re-finding.

### ⚠ A latent hazard this makes visible (do not "fix" it in S3.3)

`TupleSinkServiceHandlePeerStartCommandRequest` already calls `TupleSinkServiceWaitForCommandStartup`
(`tuple_sink_service_process.c:20485`), a **nested full `TupleSinkServicePumpOnce(...,
PUMP_STARTUP_WAIT)` loop, with no timeout, from inside the granted `DRAIN_PEER_CONTROL_MAILBOX`
action.** And the DPU collectors are appended **unconditionally** in the COLLECTOR phase
(`:41661`-`:41663`) — they are *not* `pumpFlags`-gated. So on a DPU-resident service that nested pump
would drive `doca_pe_progress()` and mutate engine state (staged command slots, `discoveredReady`,
ready-queue entries) underneath an outer pass whose facts snapshot was already taken.

That is **exactly the "cooperative spin that pumps progress" alternative D9 rejected**, already live
in the tree on the peer START path. It is why D9's rejection is right, and it is a pre-existing
defect, not one S3.3 introduces. S3.3 must not rely on it. Retiring it is the natural S4/S5 payoff of
the deferral machinery — record it, do not widen it.

---

### S3 — spawn trigger (D2′)
- **S3.0** ✅ **DONE — citus `36a61a5a1`.** **D8** + **D8b**: reserved `hostPublishLines[48]` and
  `dpuCreditLines[48]` in the arena, pointed `bridgeHeader.hostPublishOffset` at the former, left
  `dpuCreditOffset = 0` deliberately (see D8b). Arena ABI `v1 → v2`, host-only. `controlCount` and
  `boundServiceSessionId` were already landed.

  > #### 🔥 S3.0 was supposed to be a no-op. It uncovered a permanent, silent DPU wedge.
  >
  > The gate ("arena import still accepted, basebackup + smoke green") **failed** — not because of the
  > reservation, but because **basebackup had never once been run against a DPU holding a live arena
  > import.** S2's gate proved the import was *accepted*; nothing proved the service still *worked*
  > afterwards. Trust-tier discipline says exactly this: "a written artifact describing a path was trusted
  > as evidence the path works."
  >
  > With the arena imported, the DPU stopped accepting **any** further host setup connection, forever.
  > `pg_basebackup` failed with `DPU setup socket exchange failed`; the DPU logged nothing and spun at
  > 100% CPU with a connection stuck in its listen backlog. Deterministic: agent off → 19.42 s / 23.2 GB /
  > `CLOSE_ACK`; agent on → setup failure. Root cause, evidence, and the two defects still unfixed:
  > [`dpu_scheduler_arm_execute_mismatch.md` §0](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md).
  >
  > Short version: `DPU_GROUPED_CONTROL_READ` was armed from `importedRingCount`, but its executor skips
  > every ring outside the role-{1,4,7} whitelist — and the arena's 49 rings contain **zero** enrolled.
  > That permanently-armed, permanently-empty sibling kept resetting the *shared-per-source* starvation
  > clock of the blind `DPU_PE_DRAIN` collector, which is the **only caller of the setup listener's
  > `accept()`**. Landed: (A) arm discovery from a new `groupedControlRingCount` fact computed at import,
  > and (C′) treat a **started** listener as known work rather than blind maintenance.
  >
  > ⚠ **Later measurement corrected this account.** Fix **A alone unwedges the listener**
  > (`pedrain_grants = 1,250,010 / 2,500,001` passes with C′ reverted, backlog 0). **C′ is a tactical
  > hotfix, not the resting state**, and it does not even guarantee the listener runs. A **fourth** defect
  > (D) was also found: `runnableMachineWork` is always true, so backoff's idle-service guard is dead.
  > See **D10** above and `dpu_scheduler_arm_execute_mismatch.md` §0b. ✅ **SUPERSEDED by S3.1b:** C′ was
  > removed in citus `84ac374ef`, and the listener no longer runs inside `DPU_PE_DRAIN`. This paragraph is
  > retained as the history of the S3.0 diagnosis, not as current scheduler behaviour.
  >
  > Also uncovered: the **S3.1 comch bump `3 → 4` had never been deployed to either DPU.** Both DPU trees
  > were still at v3, so no `--homer-dpu` path could have worked. Resynced and rebuilt both.

  Regression on the final binaries: arena import accepted (48 + 1 descriptors); 4-role DPU-relay basebackup
  **with the agent on** — 23.236 GB, `CLOSE_ACK`, rc=0, warm `19.55 / 23.00 s`; `pgbench --homer` 1000/1000,
  0 failed, 2983 TPS; six-leg TCP transport smoke `ok` on both ends with `distinct_va=YES`.
- **S3.1** Doorbell protocol: add `DOORBELL_ATTACH` / `DOORBELL_ATTACH_ACK` / `SPAWN_DOORBELL` message
  kinds to `homer_dpu_comch_abi.h` (`:33`-`:36`) and bump `HOMER_DPU_COMCH_PROTOCOL_VERSION`. Default
  doorbell port **9728**, its own listener — *never* the setup listener, whose single `clientFd` serial
  accept loop a persistent connection would starve (`homer_service_dpu_setup_tcp.c:48`, `:345`).
  ✅ **DONE — citus `c2ec17852`** (ABI only). ⚠ The bump was **not deployed to the DPUs until S3.0**; a
  comch bump means *both* DPU trees must be resynced and rebuilt or setup fails `BAD_PROTOCOL`.
- **S3.1b — LISTENER LIFECYCLE ADMISSION. ✅ DONE — citus `84ac374ef` (July 10, 2026).**
  See **D10** above and
  [`dpu_scheduler_arm_execute_mismatch.md` §0c](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
  for the decision, implementation, and evidence. The existing setup listener now supplies the working
  reference implementation that S3.2 must copy.

  Sub-steps, in order:
  1. ✅ **Split the accept loop out of `DPU_PE_DRAIN`.** New `HOMER_PROGRESS_COLLECTOR_DPU_SETUP_LISTENER`,
     new `HOMER_PROGRESS_ACTION_DPU_SETUP_LISTENER`, new `HOMER_PROGRESS_SOURCE_DPU_SETUP_LISTENER` with
     its own `ProgressRegistry.dpuSetupListenerSource` / `.dpuSetupListenerFeedback`
     (`tuple_sink_service_process.c:3464`-`:3473`).
     `HomerServiceExecuteDpuSetupListenerAction` (`:41821`) is now the only scheduler executor of
     `HomerServiceDpuSetupTcpServerProgress`; `DPU_PE_DRAIN` contains no socket progress (`:41955`-`:41985`).
     This is a **partial fix for defect B**: the listener is 1:1 collector→source; the eleven collectors at
     `:38425`-`:38436` still alias `dpuDmaFeedback`.
  2. ✅ **Durable poll-due obligation.** Listener-owned
     `HomerServiceDpuSetupTcpListenerPollInterval` → `HomerServiceDpuSetupTcpArmPoll` →
     `HomerServiceDpuSetupTcpServerBeginSchedulerPass` (`homer_service_dpu_setup_tcp.c:240`, `:256`,
     `:282`) raises a sticky `pollDue`. It is cleared and rearmed **only** inside
     `HomerServiceDpuSetupTcpServerProgress` (`:314`, `:401`), from the post-poll `clientFd` state.
     The interval is 1 pass for an active exchange, otherwise
     `HOMER_SERVICE_DPU_SETUP_TCP_IDLE_POLL_INTERVAL_PASSES = 512`
     (`homer_service_dpu_setup_tcp.h:29`-`:48`). Candidate arming is
     `pollDue || activeConnection`; both are `knownExpectedWork`
     (`tuple_sink_service_process.c:40142`-`:40178`).
  3. ✅ **Reserved admission.** `maxLifecycleCollectorGrants` defaults to 2 and is controlled by
     `HOMER_MACHINE_BASELINE_MAX_LIFECYCLE_COLLECTOR_GRANTS`
     (`tuple_sink_service_process.c:11703`-`:11721`). `HomerMachineBaselineBudgetTryReserve` bypasses the
     total/collector/blind caps for a lifecycle grant (`:9778`-`:9816`), gated by
     `HomerServiceProgressCollectorReservedLifecycleAdmission` (`:9709`-`:9727`). The listener is appended
     first in the COLLECTOR phase so the fixed `actionGrants[64]` array cannot already be full
     (`:10936`-`:10957`).
  4. ✅ **Revert C′ without hiding reclaim demand.** `DPU_PE_DRAIN` is now armed from
     `peDrainReadyCount > 0 || facts.detachedImportCount > 0`
     (`tuple_sink_service_process.c:40272`-`:40307`). The listener is no longer an arming proxy.
  5. ✅ **Name the backoff exemption.** `HomerServiceProgressCollectorBypassesBlindBackoff`
     (`tuple_sink_service_process.c:9691`-`:9705`) contains `{PEER_CM_SETUP, PEER_CLOSE_LIFETIME,
     DPU_SETUP_LISTENER}`. That set is deliberately broader than reserved admission: bypassing backoff
     prevents feedback suppression; it does not guarantee a quota slot.

  > ### ✅ What it actually took — direct gate evidence
  >
  > - **512.0 passes/grant idle** across 127 windows; **510.2 passes/grant under load** while the same
  >   windows contained 182k–246k `PE_DRAIN` grants. The listener held cadence while its former host
  >   action was saturated.
  > - `listener_grants == listener polls` exactly: `125987 == 125987`. No grant without a due poll, and
  >   no obligation retired without polling.
  > - **Zero `SETUP_LISTENER dropped` lines across 63M passes**: reserved admission never lost a grant.
  > - Idle `accept()` fell from about 150k/s to **586/s (256×)**. At 300k passes/s, 512 passes add
  >   **1.71 ms** to accept latency only; the kernel still completes `connect()` into the listen backlog.

  **Gate passed, agent ON.** Probe: `pedrain_grants` stayed frozen at `428773` while passes advanced
  45.0M → 46.5M (correct idle arming), and a bare mid-stream `connect()` was accepted (`accepted 1 → 4`,
  `errs 0 → 1`). Final binary: 4-role DPU-relay basebackup delivered
  `23,236,731,651 / 23,236,732,163 / 23,236,732,675` bytes with `CLOSE_ACK`, rc=0, wall
  `13.89` warmup / `8.18` / `10.41` s; **do not compare this cache state to S3.0's 19.55/23.00 s band.**
  `pgbench --homer`: 1000/1000, 0 failed, 5082 TPS, p99 0.224 ms, with a socketless backend observed under
  the agent-enabled postmaster. Six-leg TCP transport smoke: `ok` on both ends, `distinct_va=YES`. All ten
  citus make targets built; both DPUs were resynced/rebuilt (comch 4U, bridge 3U on all four nodes).

  > ### ⚠ First-class caveat — the invisible reclaim demand
  >
  > `HomerDpuDmaReclaimDetachedImports` (`homer_service_dpu_dma.c:7439`) has two callers: the payload
  > close/reclaim pump (`tuple_sink_service_process.c:33428`-`:33443`) and `DPU_PE_DRAIN`
  > (`:42007`-`:42028`). The pump dies with its owning stream, so PE_DRAIN is the backstop. Before S3.1b,
  > PE_DRAIN stayed armed whenever the setup listener was merely *started*; reclaim liveness was therefore
  > an accidental side effect of an unrelated socket fact. A naive C′ revert would leak a detached import
  > with zero in-flight tasks after its stream disappeared, with no log line. S3.1b exports
  > `HomerDpuDmaSchedulerFacts.detachedImportCount` (`homer_service_dpu_dma.h:147`-`:164`), computed for
  > free during the existing 1024-slot scan (`homer_service_dpu_dma.c:1024`-`:1089`), and arms PE_DRAIN
  > from the actual demand. **General rule:** arming on a proxy hides the real obligation, and the proxy
  > silently becomes load-bearing for work it has nothing to do with — the arm/execute mismatch in reverse.

  > 🆕 **Post-S3.1b follow-up — citus `63b0df0a7` (comment-only): a second invisible demand.**
  > `HOMER_SERVICE_DPU_SETUP_TCP_CLIENT_WAITING_CLOSE_DRAIN` is reachable: the current ternary is at
  > `homer_service_dpu_setup_tcp.c:604` (`:586` was its pre-`63b0df0a7` line). While in that phase,
  > `clientFd >= 0 ⇒ interval 1` is **load-bearing**: every listener poll enters
  > `HomerServiceDpuSetupTcpProgressCloseDrain` and calls `HomerDpuDmaDrainPeForClose`
  > (`:610`-`:646`). Before S3.1b, that progress rode on `DPU_PE_DRAIN` being armed unconditionally by
  > the unrelated `started` listener fact. It now has the listener's private `pollDue` plus reserved
  > admission — a strictly stronger guarantee.
  >
  > The active setup socket is otherwise short-lived. `HomerServiceDpuSetupTcpWriteAck` closes it as soon
  > as the full ack is written (`:739`-`:776`), so `activeConnection=false` throughout an established
  > bridge's steady state and probe output showing `tcp{active=0}` mid-stream is expected. Detached-import
  > reclaim and close drain are **two invisible demands in one formerly fused action**: direct evidence
  > for the generalized rule that a proxy arming fact can silently become load-bearing for unrelated work.

- **S3.2 — THE SPAWN DOORBELL. ✅ DONE — citus `52fd1ab3d` (July 10, 2026).**
  Agent connects the doorbell lazily with bounded backoff (the local DPU service may not be up at
  `_PG_init`), sends `DOORBELL_ATTACH`, then waits on the socket. On readable: drain to `EAGAIN`
  **without parsing**, then `kill(PostmasterPid, SIGUSR1)`. **No postgres core change** — the hook already
  exists (`postmaster.c:3977`, run from `process_pm_pmsignal()` in the postmaster MAIN LOOP, not the async
  signal handler, so forking a backend from `CitusRemoteExecPostmasterSigusr1Hook` is safe).

  New TU `homer_service_dpu_doorbell.{c,h}` — its own listener on **9728**, never the setup listener's port.

  Three places the brief above was wrong, all corrected in the implementation:

  1. **"Block in `recv()`" is wrong for a bgworker.** A raw blocking `recv()` deafens
     `HomerFrontendAgentMain` to `SIGTERM`, `SIGHUP`, and postmaster death. It uses `WaitLatchOrSocket` —
     same edge, latch and `WL_EXIT_ON_PM_DEATH` preserved.
  2. **An `ATTACH` must name a live ACTIVE import.** New `HomerDpuDmaHasActiveHostMmapImport`
     (`homer_service_dpu_dma.c:7788`). A crashed agent restarts with a NEW `bridgeGeneration`, so a stale
     doorbell must not be able to wake the postmaster for an import that no longer exists.
  3. **`PostSpawn` coalesces**, and that is correct by construction rather than an optimization: the
     postmaster's rescan is idempotent, so a wake carries no information beyond "look again".

  > ### ✅ Gate evidence (agent ON)
  >
  > - **Chain proven end to end** with the env-gated scaffolding
  >   `HOMER_SERVICE_DPU_DOORBELL_TEST_FIRE_GRANTS=200`: DPU frame → agent `WaitLatchOrSocket` wake →
  >   drain-without-parse → `SIGUSR1` → postmaster hook. Nothing rings the bell until S3.3, so without
  >   this knob S3.2 would have shipped **TIER 2** ("exists, never exercised"). **Delete it with S3.3.**
  > - Observed wake period **3.04 s** vs **2.7 s** predicted from `200 grants × 4096 passes ÷ 300k
  >   passes/s`. That agreement independently confirms the ATTACHED interval really is 4096 and not 1 —
  >   the exact regression the ⚠⚠ box below warns about.
  > - Probe: doorbell granted once per **4098 passes** attached (designed 4096); `bell polls ==
  >   doorbell_grants` (`3022 == 3022`); `attached=1 accepted=1 rejected=0 errs=0`; **zero** collector
  >   drops. Setup listener unchanged at **511.8** passes/grant; `pedrain_grants` frozen at 0 while idle.
  > - `pgbench --homer` 1000/1000, 0 failed, 5171 TPS, p99 0.213 ms; doorbell never dropped.
  > - 4-role DPU-relay basebackup ×2: 23.24 GB, `CLOSE_ACK`, rc=0, `10.42 / 8.22 s`. Its sender opens a
  >   **second setup connection while the doorbell holds a persistent one on the other port** — that is
  >   the whole reason the two listeners are separate.
  > - Six-leg TCP transport smoke `ok` on both ends, `distinct_va=YES`.

  > ### ⚠⚠ THE DOORBELL LISTENER REUSES S3.1b. It gets its own collector/action/source + pollDue + reserved admission.
  >
  > It is a second listener with the same unknown-arrival property that wedged the first one. **Do not fuse it
  > into `DPU_PE_DRAIN` or into the setup-listener action**. Copy the working obligation mechanics — a
  > doorbell-specific `BeginSchedulerPass` + `ArmPoll` pair — and the listener's reserved lifecycle line.
  > Add the doorbell collector to **both**
  > `HomerServiceProgressCollectorBypassesBlindBackoff` and
  > `HomerServiceProgressCollectorReservedLifecycleAdmission` (`tuple_sink_service_process.c:9691`-`:9727`):
  > the first prevents feedback backoff; the second guarantees admission, and neither substitutes for the
  > other. **Do not copy C′ (`knownExpectedWork = started`)**, and do not copy the setup listener's exact
  > interval rule. The doorbell needs three distinct facts/cadences:
  >
  > 1. **Attach handshake:** interval 1 while the attach exchange is in flight.
  > 2. **Attached and idle:** bounded read-side `pollDue` cadence to discover EOF, which drives S3.4's
  >    postmaster-death/import-teardown path.
  > 3. **Queued outbound `SPAWN_DOORBELL`:** an exact, maintained write-side queue-depth fact, separate
  >    from the read-side EOF poll obligation.
  >
  > A persistent connection under setup's `clientFd >= 0 ⇒ interval 1` rule does **not** hang. It silently
  > spends one reserved grant plus one `recv()/EAGAIN` syscall on every pass forever and permanently
  > consumes one of the two lifecycle grants (`homer_service_dpu_setup_tcp.c:219`-`:238`, post-S3.1b
  > audit `63b0df0a7`). Omitting the reserved poll-due machinery is the silent-hang failure. See
  > `dpu_scheduler_arm_execute_mismatch.md` §0c and revised rules 2/4.
> ### 🔀 ORDER REVISED after S3.3 landed (July 10, 2026). Read this before S3.2b/S3.2c/S3.3b below.
>
> The plan wrote these as **S3.2b → S3.2c → S3.3 → S3.3b**, on the pre-S3.3 mental model that the arena
> binding (S3.2c) came before the spawn trigger. S3.3 inverted that: **the DPU is the arena-slot
> allocator.** It must choose the slot index *before* the backend exists — because it is what DMAs into
> that slot's mailboxes and what stamps `arenaSlotIndex` into the spawn request. So the true dependency is:
>
>   **S3.3b (DPU allocates + stamps) must precede S3.2c (backend claims + checks).**
>
> And S3.2c's `backendChannelMode` flip to `SELECTED_DPU_DMA` is only safe *once* a slot is stamped, so the
> stamp and the flip are the **same commit** or the backend boots into a mode with no slot — a silent
> failure strictly worse than today's loud `ERROR` (see the gate box's ⚠ caveat). Hence:
>
> **Revised order:  S3.3b + S3.2c (one commit)  →  S3.2b (reaper)  →  S3.4 (doorbell-EOF teardown).**
>
> - **S3.3b+S3.2c together** is the commit that makes a DPU-spawned backend actually *survive*, and it is
>   the one that retires the gate's `ERROR`. It is the immediate next step.
> - **S3.2b** (the `ownerPid` reaper) is a robustness backstop, not on the critical path to a working
>   session; it only matters once real backends bind slots, i.e. after S3.2c. Moved after.
> - **S3.4** (doorbell EOF → import teardown) is diagnostics/clean-shutdown; it can follow.
>
> The two entries below keep their S3.2b/S3.2c numbers for traceability, but their *scheduling* is the
> revised order above. S3.2c's body is folded into S3.3b.

- **S3.2b** (now scheduled AFTER S3.2c — see the revised order above) Agent idle loop: an
  **`ownerPid` reaper**. Walk the arena's `BOUND` slots and release any whose
  `ownerPid` is gone (`kill(pid, 0)` → `ESRCH`). Covers a hard-crashed backend and the crash-restart leak
  (`_PG_init` does not re-run, so `CreateArena`'s whole-arena `memset` does not either). A recycled pid can
  give a false positive; acceptable for a prototype, and the checked claim still catches disagreement.

  > ### ✅ S3.2b DONE + VALIDATED — citus `f04f87715` (July 10, 2026)
  > Implemented as `HomerFrontendAgentReapDeadArenaSlots(arena)` in `homer_frontend_agent.c`, called each
  > iteration of the agent's steady-state loop (the 60 s idle tick, or sooner on a doorbell wake). It reads
  > each `BOUND` slot's `ownerPid`, and only on `kill(pid,0) == ESRCH` releases it through the SAME checked
  > `HomerFrontendAgentReleaseArenaSlot` the owner would have used (passing the slot's own recorded
  > `ownerSessionId`), so a concurrent release+rebind makes the release a no-op. `EPERM` is NOT reaped.
  >
  > **Validated by fault injection, and the validation revealed the reaper's PRIMARY trigger.** `kill -9` on
  > a socketless backend does NOT leave the agent alive to idle-tick: vanilla Postgres treats ANY
  > signal-killed backend as a crash, terminates all server processes, and reinitializes. The arena is POSIX
  > shm, so it SURVIVES the crash-restart with the dead backend's slot still `BOUND` — exactly the
  > "crash-restart leak" this reaper exists for. The fresh agent instance re-attaches and reaps it:
  > `LOG: homer frontend agent: reaped leaked arena slot 0 -- owner backend pid 710218 is gone` (pid an exact
  > match to the killed backend), zero collateral errors. So the reaper's dominant real-world path is
  > restart→re-attach→reap, not the quiet idle tick (which `kill -9` cannot isolate, since it always
  > crash-restarts). Both paths are in the code; the 60 s tick remains a backstop for a dead owner that did
  > NOT crash-restart the postmaster (e.g. recycled-pid cleanup).
- **S3.2c** (now the SAME commit as S3.3b — see the revised order above) Backend: call
  `HomerFrontendAgentBindArenaSlot(arena, request.arenaSlotIndex, sessionId, ...)`
  in the `SELECTED_DPU_DMA` branch of the socketless backend bootstrap, and register
  `HomerFrontendAgentReleaseArenaSlot` with `on_proc_exit`. Fatal on a failed claim — a collision means the
  DPU and the host disagree about allocation. **This is also where `backendChannelMode` flips from
  `HOST_SERVICE_SHM` (S3.3's deliberate placeholder) to `SELECTED_DPU_DMA`** — and that flip must not
  precede the stamp, or the backend boots looking for a slot no one gave it.
- **S3.3** DPU service: replace `TupleSinkServiceSubmitBackendSpawnRequest`'s `shm_open`
  (now `tuple_sink_service_process.c:19160`) with a DMA write into the imported spawn region — **fields
  first, then `REQUEST_READY`, each awaited** — then one `SPAWN_DOORBELL` frame.
  Body in a **new TU `homer_service_dpu_spawn.{c,h}`** (not `homer_service_dpu_doorbell.c`: the doorbell
  owns a socket, the spawn owns a DMA phase machine — one call links them); the 43k-line hub keeps only
  the call. Per **D7** the DPU reserves from its own range `slots[16..31]` using a DPU-local free list;
  it never CASes host memory (it cannot).

  #### Facts settled by reading the code before implementing (July 10, 2026)

  - **`HomerDpuDmaFindDescriptorRef` cannot find the spawn region.** It requires `serviceSessionId != 0`
    and matches on `HomerDpuDmaRingBoundSessionId` (`homer_service_dpu_dma.c:1121`-`:1131`). Role 8 is a
    per-node singleton and is never bound to a session. S3.3 needs a session-agnostic
    `HomerDpuDmaFindSpawnRegionDescriptorRef(engine, bridgeGeneration, …)`.
  - **The arena and the spawn region are ONE import, 49 descriptors.** `homer_frontend_agent.c:708`-`:724`
    puts the spawn descriptor at ring index `HOMER_FRONTEND_ARENA_RING_COUNT` (48), export id 2. The DPU
    log's `export_index=1 descriptor_first=48 descriptor_count=1` is that descriptor.
  - **Spawn is gated on an ATTACHED doorbell**, and uses the doorbell's `bridgeGeneration`. The DPU cannot
    `kill(spawnRegion->postmasterPid)`; the *only* way the postmaster learns of a `REQUEST_READY` is the
    agent's `SIGUSR1`. A spawn into a generation with no attached doorbell is a silent hang by
    construction, so it must be a loud refusal.
  - The DPU's local DMA buffers live in dedicated registered mmaps (`localControlMmap`,
    `localCommandMmap`, `localResponseMmap`, `localBackendCommandMmap`, `homer_service_dpu_dma.c:10852`+).
    S3.3 adds `localSpawnMmap`.
  - Task chaining is **completion-callback driven**: only the first task is `doca_task_submit`ed; the next
    is left `PREPARED` and submitted from the previous task's completion callback (see
    `publishTaskSlotIndex`, `:1868`-`:1871`). That is how "await, then next" is spelled in this engine.

  ### ✅ S3.3 DONE — citus `455c7a556` (July 10, 2026). GATE PASSED.

  > **The one line that is the gate.** On farnet1's DPU, with the frontend agent attached:
  > ```
  > tuple-sink service: DPU backend spawn begin handle=1 slot=16 session=1 db=5 user=10 bridge_generation=2558430997326443
  > tuple-sink service: DEBUG deferred peer response requestKind=5 messageSequence=1
  > tuple-sink service: DPU backend spawn COMPLETED handle=1 slot=16 session=1 launched_pid=616897 state_reads=4
  > ```
  > and, on farnet1's **host**, `postgres.log`:
  > ```
  > 2026-07-10 11:06:32.818 EDT [616897] ERROR: could not open Homer control region "/citus_remote_execution_control_v27"
  > ```
  > **The pids match.** The DPU asked, the postmaster forked, and the DPU read the child's pid back
  > across PCIe with a two-phase DMA read. `slot=16` is the first slot of the DPU's D7 range.
  > `state_reads=4` is the two-phase read polling `state` on its 256-pass cadence before
  > `RESPONSE_READY` appeared.
  >
  > The client also printed `pgbench: client 0 opened selected-DPU Homer SQL command session id=1 index=0`
  > — so the **deferred peer response was posted and consumed** (D11 end to end). pgbench then failed at
  > `client_sql_tx_begin`, which is S3.2c/S4 work, not a regression.
  >
  > ⚠ **The spawned backend dies with an `ERROR`, and that `ERROR` is a rough edge, not steady state.**
  > It is *not* a bug in S3.3's code and *not* a regression — the fork is the feature, and it completed;
  > the `ERROR` is downstream, in the pre-existing backend bootstrap. The backend was spawned with
  > `backendChannelMode = HOST_SERVICE_SHM` (see the comment in `TupleSinkServiceFillBackendSpawnRequest`),
  > so on boot it opens the host service's `/citus_remote_execution_control_v27` — which did not exist on
  > farnet1 during the gate, and which is the *wrong* attachment for a DPU-spawned backend anyway (its
  > session lives on the DPU). So the `ERROR` is the visible symptom of a **half-built path**: a missing
  > control region is normally a genuine fault worth screaming about, and we deliberately spawned a backend
  > into a config where that scream is guaranteed.
  >
  > It causes **no state corruption**: the DPU had already read `RESPONSE_READY`, written `FREE`, and
  > released the spawn slot before the child booted; the child just exits and the postmaster reaps it. The
  > only consequence is "the session has no working backend," which is exactly the S3.2c gap.
  >
  > **It vanishes with S3.3b+S3.2c** — the backend then boots `SELECTED_DPU_DMA`, binds its arena slot, and
  > never touches a host control region. Do **not** paper over it with an interim clean-exit/LOG: that is
  > throwaway code, and S3.3b+S3.2c is the very next commit. `HOST_SERVICE_SHM` is a placeholder chosen on
  > purpose because an *unbound arena slot* would be a worse, because silent, failure than a loud missing
  > region.

  > ### 🔥 The gate failed twice first, and both failures are worth more than the pass
  >
  > **1. The collector was armed and never granted — the arm/execute mismatch, committed by the
  > author of the document about the arm/execute mismatch.** `HomerServiceAppendDpuSpawnCollectorCandidate()`
  > added a candidate on every pass; `HomerMachineBaselineCompileExecutionPlan`'s COLLECTOR phase is a
  > **hand-enumerated list** of `FindCollector` + `AppendCollectorAction` calls, and nothing named the new
  > collector. Symptom: `spawn begin` and `deferred peer response` printed, then **silence forever** — not
  > even the 60 s timeout, because the timeout lives inside the action that never ran. Nothing warns you:
  > the candidate append compiles, all four enum switches compile, `-Wswitch` is useless (every one has a
  > `default:`), and an idle service looks exactly like a wedged one.
  > **Rule:** adding a collector means editing FOUR places, and the fourth is not a switch — it is
  > `HomerMachineBaselineCompileExecutionPlan`'s phase body.
  >
  > **2. `readlink -f /proc/<pid>/exe` returns `"<path> (deleted)"` after you rebuild the binary.**
  > Our `case "$exe" in */citus_tuple_sink_service)` stopped matching, so the "clean baseline" silently
  > left the OLD service running; the new instance died on `bind()` — but only after its `>` redirect had
  > **truncated the log**, so the old process kept appending at its old offset and the log filled with NULs
  > (`grep: binary file matches`). The tell was `handle=2 slot=17 session=2` on a supposedly fresh service.
  > This is the same family as the `pkill -f` self-match and the `rsync -a` mtime trap: **an identity check
  > that is exactly right for the case you thought about, and silently wrong for the one you didn't.**
  > Match `*/citus_tuple_sink_service*`.

  > ### Regression sweep on the final binaries (all green)
  >
  > - **`pgbench --homer`** (host arm, frontend agent **ON**): 1000/1000, 0 failed, **5070 TPS**,
  >   p50 0.173 / p95 0.206 / p99 0.223 ms. The only working spawn regression net, and it exercises both
  >   D7 bug fixes on the host arm.
  > - **4-role DPU-relay basebackup**: `23,240,464,292` bytes, `CLOSE_ACK`, rc=0, **9.21 s**. Its sender
  >   opens a **second** DPU setup connection while the doorbell holds a persistent one on the other port.
  > - **Six-leg TCP transport smoke**: `ok` on both ends (`--export-posix-shm --fork-reader` + all six legs).
  >   This is the only regression net for the ~1150 lines added to `homer_service_dpu_dma.c`.
  > - All ten citus make targets build clean; installed service has **zero** stats-macro taint.

  #### Sub-steps, in order (one commit at the gate — no TIER-2 landing)

  1. **Engine primitives** (`homer_service_dpu_dma.{c,h}`): `…FindSpawnRegionDescriptorRef`;
     `…SubmitSpawnRequestPublication` (two chained tasks: `request` body, then the offset-0
     `state = REQUEST_READY` word); `…SubmitSpawnStateRead` (4 B @ offset 0); `…SubmitSpawnResponseRead`
     (`sizeof(response)` @ `offsetof(slot, response)`); `…SubmitSpawnSlotRelease` (`state = FREE`);
     `…GetSpawnSlotFacts`. Five new `HomerDpuDmaTaskKind`s. One `localSpawnMmap` + 16 buffers.
  2. **`homer_service_dpu_spawn.{c,h}`** — the phase machine and the DPU-local free list over
     `slots[16..31]`:
     `WRITE_REQUEST → WRITE_READY → RING_DOORBELL → READ_STATE → (not ready ⇒ re-arm READ_STATE) →
      READ_RESPONSE → WRITE_FREE → DONE|FAILED`. ⚠ **DMA-BEFORE-DOORBELL**: `RING_DOORBELL` is entered
     only from the *completion* of `WRITE_READY`. Bug 3's generous timeout with a loud error attaches to
     `READ_STATE`.
  3. ✅ **Scheduler wiring**: `HOMER_PROGRESS_{SOURCE,COLLECTOR,ACTION}_DPU_SPAWN`, armed on
     `pendingCount + completedCount + deferredPeerResponseCount > 0` as `knownExpectedWork`. **Not** a
     reserved lifecycle grant — it is exact work, not a poll obligation (D10); it *does* bypass blind
     backoff. Add the source to `HomerServiceDefaultCpuClassForProgressSource` (its `default:` arm is
     `CPU_CLASS_INVALID` and `-Wswitch` will not tell you).
     **⚠ AND name the collector in `HomerMachineBaselineCompileExecutionPlan`'s COLLECTOR phase.**
     Arming a candidate that the phase body never enumerates grants it on no pass, ever. This was
     missed on the first attempt; see the failure box above.
     Placed **after** `DPU_PE_DRAIN` so the pass's DOCA completions are harvested before the spawn
     machine reads the slot facts they wrote — one pass per phase, not two.
  4. ✅ **Peer-responder deferral** (D11 above). Landed additively:
     `HomerPeerDeferredResponseTicket` + `TupleSinkServicePostDeferredPeerResponseRdma`
     (`remote_execution_peer_transport_rdma.c:7899`), a 16-entry service-level queue
     (`HomerServiceDeferredPeerResponse`), and the `HOMER_PROGRESS_ACTION_DPU_SPAWN` executor that posts
     it. A transient publish failure retries next pass (the 64-slot publish pool drains as send CQEs
     retire); a **dead connection** yields `posted=false, return true` and the entry is dropped with a
     loud log.
  5. ✅ **`TupleSinkServiceSubmitBackendSpawnRequest` gains a loud refusal on the DPU arm**, selected by
     `TupleSinkServiceDpuBackendSpawnArmActive()` = "a frontend agent is ATTACHED to our doorbell with a
     nonzero `bridgeGeneration`". Call site 3 (`HandlePeerOpenCommandSessionRequest`) takes the new
     `TupleSinkServiceBeginDpuBackendSpawn()` + deferral path instead. **Call sites 1, 2 and 4 fail
     loudly** on the DPU arm — they are S4/S5 work and must not silently fall back to `shm_open`, which
     on a DPU creates an empty region no postmaster ever reads.
     Note the arm test is *doorbell attached*, not *am I a DPU*: a host service never creates a doorbell
     server, so it correctly falls through to `shm_open` with no extra condition.
  6. ✅ **D7 bugs 1 and 2**: CAS `FREE → CLIENT_OWNED` over `slots[0..15]` only (lost CAS ⇒ `continue`,
     not `break`), and hoist the `int32 launchedPid` read above the release store.
  7. ✅ **Deleted `HOMER_SERVICE_DPU_DOORBELL_TEST_FIRE_GRANTS`** — `HomerServiceDpuSpawnAdvanceOne()`'s
     `PHASE_RING_DOORBELL` is its real producer.

  #### Two design choices made inside the plan's direction (recorded per the "note your reasoning" rule)

  1. **One collector, not two.** D11 sketched a separate `HOMER_PROGRESS_COLLECTOR_PEER_DEFERRED_RESPONSE`.
     The spawn is the *only* producer of deferred responses, so a second source/collector/action triple
     would have doubled the enum-switch surface (five hand-enumerated sites each, none protected by
     `-Wswitch`) to buy nothing. `HOMER_PROGRESS_ACTION_DPU_SPAWN` advances the machine, reaps
     completions, and posts. Split it the moment a second producer appears.
  2. **The spawn machine lives in a new TU, not in `homer_service_dpu_doorbell.c`.** The doorbell owns a
     socket; the spawn owns a DMA phase machine. One call (`HomerServiceDpuDoorbellPostSpawn`) links them.
     `homer_service_dpu_spawn.{c,h}` knows nothing about sessions, peers, or responses — the 43k-line hub
     binds a spawn handle to whatever it must answer.

  #### ⚠ Caveats that will bite the next stage

  - **A DPU-spawned backend dies immediately today** (`could not open Homer control region`). Intentional;
     see the gate box. Do not "fix" it by flipping `backendChannelMode` before S3.2c binds an arena slot.
  - **`HomerServiceDpuSpawnAbandon()` does not cancel the fork.** An abandoned spawn (queue-insert failure,
     i.e. 16 deferrals outstanding) still forks a backend that nobody owns. It cannot happen with 16 slots
     and one spawn per session open; if it ever can, the abandon path needs a real cancel.
  - **The DPU spawn slot is freed only after the engine reports `opInFlight == false`.** Freeing it earlier
     would let a DOCA completion callback write into a reused slot. `HomerDpuDmaClearSpawnSlot()` refuses
     and logs loudly rather than trusting its caller.
  - **`HomerDpuDmaFindDescriptorRef` still cannot find role 8** — it demands `serviceSessionId != 0`.
     `HomerDpuDmaFindSpawnRegionDescriptorRef` exists for exactly that reason. Do not "unify" them.

  **Gate driver:** `pgbench --homer --homer-dpu-command` from farnet0, which S1a already drove to the
  exact failure `could not open backend spawn region /citus_remote_exec_backend_spawn_v14` on the
  farnet1 DPU. That message is the S3.3 gate's before-picture; a forked `postgres: remote exec backend`
  on farnet1 is its after-picture.

  **It does NOT wait.** See D9 below — the spin cannot work on the DPU, so S3.7's async responder is folded
  into S3.3. The DPU records `pendingSpawn{slotIndex, sessionId}` and returns "in progress". A demand-armed
  progress action then does a **two-phase read** across scheduler passes:
  1. DMA-read `state` **alone** (4 bytes, offset 0, naturally aligned — cannot tear);
  2. only once that read has *completed* showing `RESPONSE_READY`, submit a **second** DMA read of
     `response`. The completion of (1) is the happens-before; the body is immutable between
     `RESPONSE_READY` and our `FREE`, so no atomicity or ordering assumption is needed;
  3. then DMA-write `state = FREE`. **Bug 2 dissolves** — we hold a copy, so there is nothing left in the
     slot to read after publishing.
- **S3.3b — 🎯 THE IMMEDIATE NEXT STEP, and the SAME commit as S3.2c.** DPU service: **allocate an arena
  slot and bind it** (this is where **D6**'s forward index is built). Before submitting the spawn request,
  pick a free arena slot `k` from a DPU-local table, stamp
  `arenaSlotIndex = k` into `CitusRemoteExecBackendSpawnRequest` (S3.3 currently stamps
  `CITUS_REMOTE_EXEC_BACKEND_ARENA_SLOT_INVALID` there — this replaces that), stamp
  `ringRuntime[3k+{0,1,2}].boundServiceSessionId = serviceSessionId` on the arena import, and cache the
  three resolved `HomerDpuDmaDescriptorRef`s on the selected session. Release both at session close.
  New engine API: `HomerDpuDmaBindRingSession()` / `...UnbindRingSession()`.
  **Consumers keep using the scans in S3** — the switch is S4.1, where a session exists to test it.

  > **Why this is one commit with S3.2c, and why it is next.** The DPU is the allocator (it stamps the
  > index); the backend is the claimant (it checks the index and binds). Split across commits, the middle
  > state is a backend that boots `SELECTED_DPU_DMA` looking for a slot the DPU has not yet been taught to
  > allocate — a silent failure. Together, they are the commit that makes a DPU-spawned backend *survive*
  > and that retires the gate's loud `ERROR`. The DPU-side allocation lives in
  > `homer_service_dpu_spawn.c` (it already owns the `slots[16..31]` free list and the per-entry
  > `descriptorRef`; the arena-ring binding is the natural neighbour). Set `arenaSlotIndex` in
  > `TupleSinkServiceFillBackendSpawnRequest` **only on the DPU arm** — the host arm keeps `INVALID`.

  ### ✅ S3.3b + S3.2c VALIDATED — citus `30dfc9e9a` (July 10, 2026). GATE PASSED, regression 3/3.

  > **The gate.** `pgbench --homer --homer-dpu-command` from farnet0 → farnet1 (4 roles, frontend agent
  > ON). farnet1 DPU log:
  > ```
  > DPU backend spawn COMPLETED handle=1 slot=16 session=1 launched_pid=687526 state_reads=4
  > ```
  > farnet1 `postgres.log` for the run (`[687024]`, 15:09): **`could not open Homer control region` is
  > ABSENT** (the only occurrence is the stale pre-fix `[616897]` at 11:06), and none of
  > `could not bind/attach frontend arena` / `invalid selected-DPU startup identity` /
  > `invalid result queue descriptor` appear. `ps` shows `postgres: remote exec backend` pid **687526 alive
  > and idling** long after the run. pgbench: `client 0 opened selected-DPU Homer SQL command session id=1`
  > then failed at `client_sql_tx_begin` — the exact S4 boundary, expected, not a gate failure.
  >
  > **Why "backend alive" is airtight, not a soft signal.** A DPU-spawned backend gets `SELECTED_DPU_DMA`
  > + a valid `arenaSlotIndex`, forcing `arenaBackend=true`, which MUST `AttachArena` + `BindArenaSlot`
  > (both FATAL-on-failure, on the bootstrap path *before* the command loop) or die. A live, idling
  > selected-DPU backend therefore cannot exist unless attach + checked bind + mailbox redirect + result
  > skip all succeeded. The absence of all four arena-error strings corroborates.
  >
  > **Regression 3/3:** multi-client `--homer` c4 (agent ON) = 20000/20000, **0 failed**, 11.7k TPS
  > (farnet1-LOCAL host-service path — no farnet0, no DPU, no RDMA; this is the host-arm no-regression
  > check, NOT the DPU path); 4-role DPU basebackup 23.24 GB + `CLOSE_ACK` in 23.7 s; 6-leg TCP smoke `ok`
  > both ends (S0b fork-reader proof intact).
  >
  > **Concurrent DPU-arena allocation — a SECOND, independent PASS** (`--homer-dpu-command -c 4` from
  > farnet0). Sessions 1/2/3 were concurrently in flight (all three `spawn begin` before any `COMPLETED`),
  > got **distinct spawn slots 16/17/18**, and — proven by **all 4 backends alive** (pids matching
  > `launched_pid`) — **distinct arena slots**: a duplicate arena slot would have FATAL'd the second
  > backend's `BindArenaSlot` CAS (`arena slot N is already bound`) and it would be dead. Zero
  > `already bound`/`partially bound`/collision lines. Notably **session 4 REUSED spawn slot 16** (freed
  > after session 1's spawn *completed*) yet got a distinct arena slot (session 1's arena binding still
  > held, its backend alive) — a live proof of the **spawn-slot-vs-arena-slot lifetime distinction** the
  > design rests on (spawn slot frees at spawn completion; arena slot frees only at SESSION close). Each of
  > the 4 still failed at `client_sql_tx_begin` (S4).
  >
  > **Two honest follow-ups (neither a gate blocker):**
  > - The positive `remote exec backend: bound frontend arena slot=<N>` line is compiled out at default
  >   `HOMER_REMOTE_EXEC_TRACE=0`, so the pass is *inferred* (live selected-DPU backend + DPU spawn-complete
  >   + no error strings) rather than read directly. A one-off `-DHOMER_REMOTE_EXEC_TRACE=1` farnet1-host
  >   build would print `bound frontend arena slot=0` for belt-and-suspenders proof.
  > - The idle socketless backend does not respond to `pg_ctl stop -m fast`/SIGTERM (had to be `kill -9`'d).
  >   PRE-EXISTING socketless-backend gap that S3.3b merely EXPOSED — before, the backend died instantly at
  >   the control region, so it never reached the idle loop. Normal shutdown is the `CLIENT_SQL_SESSION_CLOSE`
  >   command (S4) or S3.4's doorbell-EOF teardown; abnormal-kill robustness is a separate future item.

  ### (history) S3.3b + S3.2c IMPLEMENTED — built + farnet1-deployed

  **Where the allocation state actually lives — a deviation from the box above, decided from the code.**
  The box said "the DPU-side allocation lives in `homer_service_dpu_spawn.c`." On reading the code that is
  the wrong home, for two reasons, and the allocation was placed elsewhere:
  1. The arena-slot free/bound STATE is `HomerDpuDmaRingRuntime.boundServiceSessionId`, which is
     **engine-private** (`homer_service_dpu_dma.c`); `homer_service_dpu_spawn.c` cannot reach it. So the
     allocator MUST be an engine API. It is: **`HomerDpuDmaBindRingSession()` finds the lowest arena slot
     whose three rings are all UNBOUND (`boundServiceSessionId == 0`), stamps them, and returns the slot
     index + the three D6 refs**; `HomerDpuDmaUnbindRingSession()` clears them. A free list in spawn.c would
     be a second source of truth and would drift. There is no separate "DPU-local table" — the free list
     *is* `boundServiceSessionId`, exactly as the `homer_service_dpu_dma.c:427` comment always said.
  2. The binding's LIFETIME is the **session**, not the spawn: the plan itself says "cache the refs on the
     selected session… release at session close," but the spawn ENTRY is reaped at spawn completion, far
     earlier. So the DRIVING lives in the **session-owning hub**, `TupleSinkServiceBeginDpuBackendSpawn`
     (allocate + cache on `sessionState->dpuArena*` + stamp the request + undo on spawn-start failure) and
     the release lives in `TupleSinkServiceResetSession` (the single teardown funnel), beside the existing
     per-session peer-connection release, **before its final `memset`**. `homer_service_dpu_spawn.c` is
     untouched — it stays the pure spawn-slot phase machine.

  `FillBackendSpawnRequest` stays a pure builder: it takes `arenaSlotIndex` as a param (INVALID for the
  host arm) and is the ONE place mapping a valid slot ⟺ `SELECTED_DPU_DMA` channel mode. The fallible
  engine allocation + its release-on-failure sit in `BeginDpuBackendSpawn`, not in the builder.

  **⚠ S3.2c was materially LARGER than the one-liner above, and this is the load-bearing correction.**
  The one-liner assumed the selected-DPU backend bootstrap already worked and only needed a `BindArenaSlot`
  call. It did not: the selected-DPU backend in `remote_execution_backend_bridge.c` was still the **old
  Tier-2 per-session-shm shape** — it `shm_open`s named command/completion mailboxes AND a result queue by
  `queueShmName`. A DPU-spawned backend has none of those, so flipping `backendChannelMode` alone just
  moves the FATAL: control-region → **result-queue descriptor** (`resultServiceSinkId == 0`) → command
  mailbox. Real S3.2c had to **migrate the backend's command/completion mailboxes to the arena slot**:
  - New `arenaBackend = selectedDpuBackendChannel && arenaSlotIndex != INVALID` distinguishes the arena arm
    from the legacy Tier-2 selected-DPU arm (which keeps per-session shm) — the legacy arm is preserved,
    not gutted.
  - Arena arm: `HomerFrontendAgentAttachArena` + checked `HomerFrontendAgentBindArenaSlot` (FATAL on
    collision) + `on_proc_exit` release (via a file-scope wrapper — `ReleaseArenaSlot`'s 3-arg signature is
    not the `(int,Datum)` callback shape), then point `commandMailbox`/`completionMailbox` straight at
    `arena->slots[k].{commandMailbox,completionMailbox}`. The per-session `shm_open` becomes the `else`
    (host + legacy). Exit-path `munmap`/`close` is already `!= NULL`/`>= 0`-guarded, so leaving the arena
    arm's fds/addresses at their `-1`/`NULL` defaults makes teardown skip the agent-owned arena mapping;
    the kernel reclaims it at `proc_exit`.
  - **Result byte-ring (role 5) is DEFERRED to S4.0b** — the arena arm SKIPS the result-queue block
    (`if (selectedDpuBackendChannel && !arenaBackend)`). This matches the plan's own S4 split ("required
    before a backend can produce a single result byte through the arena"). No command executes until S4.2,
    so `resultQueueReady == false` is harmless until then. The gate is "the backend SURVIVES session-open,"
    proven by the backend binding its slot and then busy-waiting in the command loop
    (`RemoteExecBackendReadStableCommandRecord` spins on `publishedEpoch`); pgbench still fails at the
    transaction, which is S4, not a regression.

  **One more required fix the plan did not name:** for the DPU arm, `FillBackendSpawnRequest` must set
  `completionReadyBitmapEnabled = 0` and `serviceSessionIndex = INVALID`. The DPU discovers completions by
  DMA-polling the role-3 mailbox, NOT via the host-service completion-ready bitmap (which lives in the
  control region a selected-DPU backend never maps) — and the backend's own bootstrap FATALs
  ("invalid selected-DPU startup identity") if a selected-DPU spawn carries either. In S3.3 this check was
  dormant because the mode was `HOST_SERVICE_SHM`; the flip to `SELECTED_DPU_DMA` activates it.

  **Safety of binding a slot before the backend claims it** (the race S3.3b introduces): confirmed safe by
  the measured note at `homer_service_dpu_dma.c:1487` — a bound arena ring whose mailbox is still all-zeros
  reads `publishedEpoch(0) != consumedEpoch+1`, so the completion-pull scan stages nothing and raises no
  error; and the pull collector is not even granted until a selected session awaits a completion (S4). The
  "FREE slots are always zeroed" invariant (agent CreateArena + `ReleaseArenaSlot` re-zero) is what makes
  this hold across the whole window.

  **No ABI bump.** The spawn request / startup structs are unchanged (arenaSlotIndex already shipped at
  v15, S3.3c); only field VALUES change. The bridge protocol version is untouched, so the farnet0 DPU
  (session initiator/relay, never a spawner) stays protocol-compatible on its existing binary. New shared
  constant `CITUS_REMOTE_EXEC_BACKEND_ARENA_RINGS_PER_SLOT = 3` lives in the plain-C protocol header (the
  DPU allocator needs it without postgres.h); `homer_frontend_agent.h` now references it and static-asserts
  agreement — single source of truth for the 3-rings/{2,3,5}-roles layout.

  **Files touched (citus):** `homer_service_dpu_dma.{c,h}` (+`BindRingSession`/`UnbindRingSession`
  +`FindArenaImportIndex`), `tuple_sink_service_process.c` (`sessionState->dpuArena*` fields,
  `ReleaseDpuArenaBinding`, `BeginDpuBackendSpawn` allocate+cache+undo, `FillBackendSpawnRequest` arm knob,
  `ResetSession` release), `remote_execution_backend_bridge.c` (arena arm + on_proc_exit wrapper),
  `remote_execution_backend_protocol.h` + `homer_frontend_agent.h` (shared RINGS_PER_SLOT).
- **S3.3c** ABI: bump `CITUS_REMOTE_EXEC_BACKEND_PROTOCOL_VERSION` 14 → 15 (shm name `_v15`), add
  `uint32_t arenaSlotIndex` to `CitusRemoteExecBackendSpawnRequest` **and**
  `CitusRemoteExecBackendStartupData`, and copy it in `ProcessSpawnRequestSlot`. Host-service producers set
  it to `UINT32_MAX` (unused). Update `AGENTS.md`'s `/dev/shm` list for the `_v15` name.
  ✅ **DONE — citus `c2ec17852`**, together with the doorbell ABI and a postmaster-side bounds check on
  `arenaSlotIndex` (it crosses the trust boundary of open question 7 once the DPU DMA-writes it).
  **No further slot-layout change is needed** (D9b): `state` is already at offset 0 and 4 bytes, which is
  exactly what the two-phase read requires. Two `_Static_assert`s now pin that, with the immutability
  invariant stated at the struct.

#### ⚠ The spawn wait — a decision, not an oversight

`TupleSinkServiceSubmitBackendSpawnRequest` **spins** on `RESPONSE_READY` with `TupleSinkServiceCpuRelax()`
(`:18976`), and it is called **synchronously from inside a peer request handler**,
`TupleSinkServiceHandlePeerOpenCommandSessionRequest` (`:37489`, spawn at `:37659`).

On a host service that stalls one process, and it **works**: `reservedSlot` is a pointer into `mmap`'d
`/dev/shm`, the postmaster stores `RESPONSE_READY` into the same physical memory, and cache coherence
delivers it. No software is in the loop. The host arm can keep this forever.

> ## ⚠ D9 — ON THE DPU THE SPIN IS NOT A STALL. IT IS A SELF-DEADLOCK.
> ### (corrected July 10, 2026; the earlier "bounded, timed spin" decision is RETRACTED)
>
> After S3.3 the DPU has **no mapping** of the spawn region — only a DOCA import. `RESPONSE_READY` lives in
> host RAM across PCIe. There is no `reservedSlot` to load from. The only way to observe it is: submit a DMA
> read → wait for its **completion callback** → read the copy that landed in DPU memory. And:
>
> 1. DOCA delivers completions **only** through `doca_pe_progress()`. The engine says so itself:
>    *"`doca_pe_progress()` is the ONLY site that harvests DOCA completion/error callbacks"*
>    (`homer_service_dpu_dma.c:4894`).
> 2. `doca_pe_progress()` is called only by `HomerDpuDmaDrainPe`.
> 3. The service calls `HomerDpuDmaDrainPe` at **exactly one site** — `tuple_sink_service_process.c:41470` —
>    inside a *granted progress action*.
> 4. The service is **single-threaded**: zero `pthread_create` in the Homer service sources.
> 5. `SubmitBackendSpawnRequest` runs synchronously inside `HandlePeerOpenCommandSessionRequest` (`:37659`),
>    a *different* granted action, in the same pass, on that same thread.
>
> **While the spin runs, nothing calls `doca_pe_progress()`. The DMA read never completes.**
> A bounded spin times out every time, no matter how generous. An unbounded spin hangs forever.
> The waiter is the only agent that can cause the event it is waiting for.
>
> **Therefore S3.7 is not a follow-on; it is the mechanism.** Folded into S3.3. There is no interim.
>
> Scope: this is **not only the peer responder.** The other three call sites (`:33873`, `:37361`, `:37804`)
> also run inside the loop, so a DPU-resident service opening a command session *locally* — S4's single-node
> shape — hits the identical wall. The initiator already has a phase machine
> (`TupleSinkServiceProgressCommandOpenAsyncOp`: `COMMAND_CREATE → ENSURE_CONNECTION → REGISTER_MEMORY →
> START_PEER_OPEN → WAIT_PEER_OPEN`); it wants a `WAIT_BACKEND_SPAWN` phase. The responder
> (`HandlePeerOpenCommandSessionRequest`, which handles and replies in one call) needs the equivalent built.
>
> Bug 3's decision is unchanged and now attaches to the async wait: a **generous timeout with a loud error**,
> no liveness polling. A timeout that has never fired is the correct amount of machinery.
>
> **Hard prerequisite before any concurrent-workload measurement** (foreground pgbench + background
> basebackup) — which was already true of the stall, and is now true of a hang.

*Rejected: a cooperative spin that pumps DMA/RDMA progress while waiting.*
Rejected — but **not for the reason first written.** `HomerDpuDmaTaskMemcpyComplete` touches only *engine*
state (its task slot, `engine->fatalError`, counters) and never calls service code, so draining the PE from
inside a handler is **not** C-level re-entrancy into the scheduler. The real hazard is narrower: an
out-of-band drain mutates engine state — staged command slots, `discoveredReady`, ready-queue entries,
`backendCompletionStagedValid` — underneath an outer pass whose facts snapshot was already taken. Stale
facts, not recursion. Still reason enough to reject; a different reason.

*Rejected: send the spawn response back over the doorbell socket as a `SPAWN_RESPONSE` frame.*
It is genuinely attractive — self-announcing, no poll, and it would make bug 2 unrepresentable rather than
merely avoided. **It loses on failure semantics.** It inserts the agent between "the postmaster forked
successfully" and "the DPU knows", so an agent stall produces a **forked-but-orphaned backend from a session
the client saw fail** — a mode the DMA poll does not have, because the DPU reads the postmaster's word
directly and the postmaster must be alive anyway. It also costs a wire format, a comch bump, a
`kill(agentPid, SIGUSR1)` wake edge, and an `agentPid` arena field, to buy tens of microseconds on a path
about to `fork()` and attach a database.
> **And the reasoning that recommended it was a misapplication of our own KB.**
> [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md)
> argues against polling **hot, high-fan-in** readiness (16 mailboxes, per transaction, forever) when a
> channel exists. The spawn response is **one cold event, armed on demand** (`pendingSpawnCount` is 0 or 1).
> Polling it *is* the D6 discipline — poll the waiter, not the world. **Do not generalise that doc to cold,
> demand-armed events.**

#### ⚠ D9b — the two-phase read, and the platform assumption it avoids

`CitusRemoteExecBackendSpawnSlot` is **552 bytes across 9 cache lines**: `state` at offset 0 (written
**last**), `response` at offset 264 (written **first**).

PCIe guarantees **no atomicity for ordinary Memory Read TLPs** — it added dedicated AtomicOp TLPs precisely
because reads and writes are not atomic — and DOCA documents `doca_dma` as a memcpy with no atomicity or
ordering claim. Reliable single-copy atomicity extends only to **naturally-aligned ≤ 8-byte** accesses.
So a single DMA read of the whole slot gives **no snapshot**.

The slot has a property the publish line does not: **between `RESPONSE_READY` and our `FREE`, nothing writes
it.** So a two-phase read assumes nothing (see S3.3 above), at a cost of one extra round trip, once per
spawn, on a path immediately followed by `fork()`.

*Rejected: hoist `statusCode`/`launchedPid` into the slot's first cache line so one read gets a consistent
triple.* That trades a **guarantee** (aligned 4-byte atomicity) for a **platform assumption** (line-atomic
DMA reads), to save one round trip on a millisecond-scale cold path.

##### And the assumption the publish line *does* rest on — now stated, in code

`HomerDpuBridgeHostPublishLine` is read as **one 64-byte DMA transfer** whose `publishedEpoch`,
`publishedTail`, `generation` and `entryState` are then trusted *together*. That is sound only because:

- `publishedEpoch` sits at **offset 0**, the lowest address, so an **ascending-order source fetch** reads it
  before the body. A torn read can then only pair a **stale epoch with a fresh body** →
  `epoch == acceptedPublishedEpoch` → early return → re-read next pass. Missing a publication is free;
  discovery is a perpetual poll. *This is why "we'll pick it up later" is safe.*
- The opposite pairing — **fresh epoch with stale tail** — would be **fatal and silent**.
  `acceptedPublishedEpoch` is stored *unconditionally* (`homer_service_dpu_dma.c:9941`) before the tail is
  examined, and every later pass early-returns on epoch equality (`:9882`). The tail would be dropped and
  that ring would stall **forever**. And nothing catches it: `HomerDpuBridgeFrontierMonotonic` is
  `observed >= previous`, and a one-publication-stale tail **equals** the accepted tail.

**Per-field atomicity is necessary but not sufficient** — every field is an aligned ≤8-byte store, and that
is symmetric with respect to the hazard, which is *cross-field* consistency. The load-bearing assumption is
**ascending fetch order**, which is far weaker and far more plausible than line atomicity, and which has
~23 GB per validated basebackup run behind it with the frontier check never firing. A trailing epoch echo
would **not** remove it (under a descending fetch a matching head/tail pair can still bracket a body from the
next publication). So: state it, do not engineer around it.

Written down where it can be seen: the long note on `HomerDpuBridgeHostPublishLine`, the corrected
`_Static_assert` message ("must stay at the lowest address so torn DMA reads are conservative" — the old
message claimed "one-word polling", which the code does not do), and a warning at the unconditional epoch
store. **Where the body is immutable, do not rely on any of it — do the two-phase read.**
- **S3.4** DPU service: on doorbell **EOF**, log loudly that the frontend agent for that
  `bridgeGeneration` is gone, and begin the existing import teardown
  (`HomerDpuDmaBeginHostMmapImportTeardown`, `homer_service_dpu_dma.c:6964` — the same path a graceful
  setup `CLOSE` drives). **Diagnostics and clean shutdown, not a recovery path**: a subsequent DMA
  against a dead export is *supposed* to be fatal (see D2′ correction). Do **not** reclassify
  `DOCA_ERROR_IO_FAILED` as recoverable.

  > ### ✅ S3.4 DONE + VALIDATED — citus `81cb0ec45` (July 10, 2026)
  > Implemented in `HomerServiceExecuteDpuDoorbellAction`: `HomerServiceDpuDoorbellServerProgress` already
  > runs EOF detection (which closes an attached connection nonfatally); after refreshing the doorbell
  > facts, the action detects the attached→detached transition and calls
  > `HomerServiceDpuDoorbellTeardownGoneImport` → `HomerDpuDmaBeginHostMmapImportTeardown`. The attached
  > `(bridgeGeneration, clientInstanceId)` is tracked on `HomerServiceDpuDmaSchedulerState` because both are
  > zeroed on detach; the new-generation-attached-in-place case is also handled (the ~5 s bgworker restart
  > gap normally makes us see a plain detach first). Teardown is idempotent; the R3 fatal-on-DMA-fault guard
  > is untouched.
  >
  > **Validated** by the same crash-restart fault injection as S3.2b, now DPU-side: kill -9 a backend →
  > postmaster crash-restart → agent dies → doorbell EOF. The farnet1 DPU logged
  > `DPU spawn doorbell EOF -- frontend agent gone for bridge_generation=2959490104499559 …; tearing down
  > its host mmap import`, then `setup import closing` for BOTH imports (arena `mmap_export_id=1` and
  > spawn-region `=2`), the generation an exact match to the setup import, `inflight=0` on both (no fault
  > path taken), **zero `FATAL`/`DOCA_ERROR`** anywhere. The service then accepted the restarted agent's
  > new-generation import and kept running. At an idle gate there is no in-flight DMA to the dying import,
  > so the teardown completes cleanly rather than tripping the R3 fatal — exactly as intended.
- **S3.5** Keep the host-service `shm_open` arm intact and **functional** through S6. Add a one-shot
  `LOG` on first use — *"host-service backend spawn is deprecated; retires at S7.1"* — and nothing more.
  **Do not gut it early.** See the box below; this is not sentimentality about legacy code.
  ✅ **DONE — citus `89820eb36`** (July 10, 2026). One-shot `static bool` + `fprintf` in
  `TupleSinkServiceSubmitBackendSpawnRequest`, after the DPU-arm refusal and the request fill, right before
  the `shm_open`. Validated: a local `pgbench --homer` run (host arm) emitted the line EXACTLY once across
  5/5 txns. **S3 is now fully complete.**

> ### 🔑 DISCOVERY (July 9, 2026) — the spawn path has exactly ONE working regression net, and it is not basebackup
>
> **`pg_basebackup` never spawns a socketless backend.** The green 4-role DPU-relay basebackup regression —
> 23 GB, the thing we lean on for every transport change — gives **zero coverage of the backend-spawn path**.
> Verified: no `SubmitBackendSpawnRequest` reachable from `src/backend/backup/`.
>
> The only workload that exercises spawn end to end is **`pgbench --homer`** (the other one, backend-to-backend
> COPY, is broken at HEAD — Problem 2). Confirmed working on the S3.1 binaries at citus `c2ec17852`:
> ```
> number of transactions actually processed: 1000/1000
> number of failed transactions: 0 (0.000%)
> tps = 3057.5   latency p50 = 0.308 ms   p99 = 0.477 ms
> ```
> (functional check, single cold run — not a performance claim.)
>
> **Three consequences.**
> 1. Through S3–S5 the host arm is **the control for its own replacement.** If the DMA-then-doorbell
>    sequence misbehaves, it is the only known-good arm to A/B against.
> 2. There is no separate "host submitter" to delete. `TupleSinkServiceSubmitBackendSpawnRequest` is **one
>    function with four call sites**, one of which (`:37659`) is the DPU's own peer command-open handler.
>    S3.3 adds a DMA arm to that same function; the `shm_open` arm is an `else`. Keeping it costs nothing.
> 3. **Gutting the arm retires plain `pgbench --homer` wholesale**, not just a branch — every `--homer`
>    session needs a socketless backend. It finishes off backend-to-backend COPY too. Survivors would be:
>    basebackup (never spawns), the seven smokes, and — only once the S3 gate passes —
>    `pgbench --homer --homer-dpu-command`, which *becomes* the replacement spawn net.
>
> **So the retirement order is: S3 gate passes → the new net exists → only then gut.** That is S7.1, and it
> is one stage of patience for a full A/B.

> ### ⚠ CAPTURE THE BASELINE BEFORE S7.1 — it will not exist afterwards
>
> S6's gate is literally *"cross-node DPU vs today's cross-node host-service `--homer`"*. Retiring the host
> arm destroys the number we are trying to beat. Before S7.1, capture the warmed cross-node `--homer` c1 and
> c4 band and **stamp it with a commit SHA, not a date** (our own standing rule; the numbers currently in
> `AGENTS.md` are dated, predate several transport changes, and cannot be checked with
> `git merge-base --is-ancestor`).

**Gate:** a DPU service causes a socketless backend to be forked on its own host. Assert the ordering
(fields before state word) with a debug check.

#### Risks, named

| | Risk | Mitigation |
|---|---|---|
| R1 | ~~**exporter ≠ reader** aliasing (load-bearing)~~ | **CLEARED — S0b passed** (citus `37cc74b06`) |
| R2 | DOCA ~100 MB single-region ceiling | N = 16 ⇒ ≈ 57 MiB; growth path is `mmapExportCount > 1`, already in the ABI |
| R3 | agent death mid-DMA | **DOWNGRADED.** The agent must not crash; if it does that is a bug and a fatal DPU engine error is the correct outcome. Postmaster keeps the arena mapped, so pages cannot be recycled. Doorbell EOF is logged for attribution (S3.4), not for recovery. |
| R4 | DOCA state across `fork()` | agent is a leaf process; postmaster never touches DOCA |
| R5 | agent `shm_open` races postmaster creation | postmaster creates in `_PG_init`, before the agent starts; agent retries with backoff regardless |
| R6 | bridge-generation churn on agent restart | R3 closes the old import; new export mints a new generation |
| R7 | **trust boundary**: after S3 the DPU DMA-writes `dbOid`/`userOid` into a spawn slot | acceptable for a prototype; named, not discovered |

### S4 — SINGLE-NODE end-to-end (the de-risking gate)

> **⏺ RESTRUCTURED to CROSS-NODE-ONLY (user-confirmed, July 10, 2026).** Homer is going RDMA-only; the
> single-node "local fast path" is being deleted, so we do NOT build a single-node gate. S4 MERGES with the
> cross-node work: the gate becomes a cross-node real-pgbench SELECT (client node A, backend node B, both
> through their DPUs, RDMA between the DPUs). The single-node text below is kept for historical context on
> the shared mechanics, but the actual bring-up follows the **two-bridge design**:
> [dpu_crossnode_command_completion_bridges_design.md](./dpu_crossnode_command_completion_bridges_design.md).
> The net-new work is exactly two RDMA bridges — command ingress (node-A role-1 → node-B consumer A) and
> completion egress (node-B consumer B → node-A role-6); the result plane (role-5→role-7) is already built
> minus S4.0b, and the peer OPEN/spawn is already built. Order: S4.1+S4.0b → command bridge → completion
> bridge → results → cross-node SELECT gate → S4.0 (D6) last.

Client, PostgreSQL, and ONE DPU on the same host. **No peer leg, no RDMA, no second DPU.** The client
opens its session on the local DPU; the DPU triggers the spawn; the backend exports its mailboxes to the
same DPU; commands, completions and tuple results all flow host<->DPU by DMA.

**Smaller than written.** The single-node command/completion loop **already exists in the service**:
`START_COMMAND` off the role-1 control slot is DMA-published into the backend's role-2 mailbox by
`HomerServiceDpuStageOneBackendCommandForPublish`, and the backend's role-3 completion mailbox is
DMA-pulled and republished into the client's role-6 line by `...AcceptOneBackendCompletion` →
`...EnqueueSelectedCompletionEvent` → `...PublishOneSelectedCompletionEvent`. The TCP transport smoke
exercises both directions (`--expect-backend-command-publish`, `--expect-backend-completion-pull`),
now including into a tmpfs mapping (S0).

> **⚠ VALIDATION NET — the smoke does NOT cover the S4.0 hub refactor (traced July 10, 2026, codex).**
> The sentence above is true only of the ENGINE primitives. The `dpu-tcp-transport-smoke-bin` target
> links `homer_service_dpu_dma.c` but **not** `tuple_sink_service_process.c`; its
> `--expect-backend-command-publish` / `--expect-backend-completion-pull` legs call
> `HomerDpuDmaSubmitBackendCommandPublication` (`homer_dpu_tcp_transport_smoke.c:1456`) and
> `HomerDpuDmaSubmitBackendCompletionPulls` (`:1538/:1568`) **directly**, and never enter the two hub
> consumers S4.0 rewrites — `HomerServiceDpuStageOneBackendCommandForPublish`
> (`tuple_sink_service_process.c:40050`, the role-2 *stage*; DMA submit is the separate
> `DPU_BACKEND_COMMAND_STAGE`/publish action at `:43407`) or the
> `HOMER_PROGRESS_ACTION_DPU_BACKEND_COMPLETION_PULL` case (`:43295`). So the smoke is a good engine
> regression net but proves **nothing** about S4.0's consumer changes.
> **The ONLY live workload that drives consumers A/B is the DOCA SQL-UDF path**
> `citus_remote_exec_pgbench_transaction` + `citus.enable_experimental_homer_dpu_frontend=on` (recipe in
> `dpu_dma_backend_homer_service_current_scheduler_design.md:2487`). `pgbench --homer-dpu-command` does
> NOT reach them — `HomerClientOpenSqlSessionSelectedDpu` sets `session->control = NULL`
> (`homer_client.c:2977`), so its first `BEGIN` is rejected at `homer_client.c:5944` before any
> START_COMMAND is published. **Therefore every S4.0 sub-step must be validated by a real DPU DOCA run of
> the SQL-UDF path; there is no host-only shortcut. Establish that path is GREEN at HEAD before
> refactoring, or a regression cannot be attributed.**

So S4's own work is:
- **S4.0 — cash in D6.** Switch the consumers to the forward index S3.3b built, and delete the scans:
  role-2 publish reads `session->backendCommandMailboxRef` instead of calling `FindDescriptorRef` per
  `START_COMMAND`; role-3 pull iterates selected sessions awaiting a completion, via a new engine entry
  point `HomerDpuDmaSubmitBackendCompletionPullForRef(engine, ref)`, instead of scanning every ring of
  every import. `HomerDpuDmaSubmitBackendCompletionPulls` (the scan) is then deleted or reduced to the
  smoke's use. **Demote D5's `continue` to a one-shot loud diagnostic** — it is now unreachable, and if it
  ever fires, the grant and the iteration bookkeeping disagree.
  Also decide the role-5 teardown question D6 leaves open (`ByteRingFrontierForServiceSink` /
  `MarkImportSenderCloseReleased` search by `(serviceSessionId, serviceSinkId)`; the arena ring has
  `serviceSinkId = 0`).

  > **⏺ SETTLED S4.0 design (decided July 10, 2026, within-direction, per "note your reasoning").**
  > The plan's phrasing "role-2 publish reads `session->backendCommandMailboxRef`" hides a real
  > two-struct gap: S3.3b cached the D6 refs (`dpuArenaRingRefs[3]`, roles {2,3,5}) on the **frontend**
  > session `TupleSinkServiceSessionState` at bind time — but consumers A/B use a **different** struct,
  > the DPU-DMA scheduler's `HomerServiceDpuSelectedSessionState` (`:597`), created LAZILY at the first
  > START_COMMAND (`FindOrCreateSelectedSession`, `:39665`), and `dpuDmaState` holds **no** back-pointer
  > to the frontend session table (checked its struct body, `:650`–`:713`). Three ways to bridge that gap:
  > - **(A) copy `dpuArenaRingRefs` from the frontend session** at selected-session creation — needs a
  >   `serviceSessionId → TupleSinkServiceSessionState` lookup (`TupleSinkServiceFindSessionById`, `:22648`)
  >   plumbed into the scheduler, coupling the DPU-DMA scheduler to the frontend session table.
  > - **(B) prime the selected session at spawn time** from `dpuArenaRingRefs` — needs eager
  >   selected-session creation in `BeginDpuBackendSpawn` and couples spawn ↔ scheduler; changes
  >   `selectedSessionCount`/completion-wait arming semantics.
  > - **(C, CHOSEN) resolve role-2 and role-3 ONCE at selected-session creation via `FindDescriptorRef`,
  >   cache both on `HomerServiceDpuSelectedSessionState`, reuse per transaction.** This still "cashes in
  >   S3.3b": `FindDescriptorRef` resolves via the ring's `boundServiceSessionId` — the exact key S3.3b
  >   stamps — so it CANNOT diverge from `dpuArenaRingRefs`, and it also works unchanged for the OLD
  >   per-session export path (where the descriptor DECLARES a nonzero `serviceSessionId`). It keeps the
  >   scheduler self-contained (no frontend-table coupling), and it moves the scan off the per-transaction
  >   path (D6's actual goal: resolve once per session, O(1) per START_COMMAND). Cost: one `FindDescriptorRef`
  >   per session (at creation), vs. per transaction today. `dpuArenaRingRefs` on the frontend session stays
  >   live — it is still the home for role-5 (S4.0b) and teardown.
  >
  > **Role-5 teardown decision — DEFER the fix to S4.0b, do NOT touch it in S4.0.** Confirmed both helpers
  > REJECT `serviceSinkId == 0` up front (`ByteRingFrontierForServiceSink` `homer_service_dpu_dma.c:4668`;
  > `MarkImportSenderCloseReleased` `:8558`) and then require exact sink equality (`:4693`, `:8579`), while
  > the arena role-5 descriptor is built with `serviceSinkId = 0` (`homer_frontend_agent.c:750`) and binding
  > stamps only the session id (`:5654`), leaving the ref's sink at 0 (`:5663`). So both helpers MISS the
  > arena result ring. But **role 5 is not live until S4.0b** — no arena result ring exists to tear down in
  > S4.0 — so the fix belongs in S4.0b, alongside turning the ring on. The fix options (record in S4.0b):
  > give the arena result ring a nonzero synthetic `serviceSinkId`, OR add explicit "arena ring (sink 0)"
  > handling to both helpers keyed on `boundServiceSessionId` alone. S4.0 only records the decision.
  >
  > **⏺ RESOLVED (July 10, 2026): neither option — `boundServiceSinkId`, stamped at stream creation.**
  > Both recorded options are dead: the sink id is a JOIN KEY into the mirror pool
  > (`HomerDpuDmaResolveMirrorSlot` looks the pool up BY descriptor sink, `:4355`, rejects 0 at `:4333`;
  > pool slot bound by `(session, streamId)` at `tuple_sink_service_process.c:17241`), so a synthetic
  > constant can't match the dynamically allocated stream id and a session-only wildcard can't join at
  > all. The fix extends D5's rule to the sink: `boundServiceSinkId` on the ring runtime, stamped when
  > the service creates the backend's result stream (P3.1), honored at all FIVE sink-consuming sites
  > (the four match helpers + `ResolveMirrorSlot`). Spec in the bridges design doc §4 P0 part (c).
- **S4.0b — turn on the arena's credit lines (D8b).** *Required before a backend can produce a single
  result byte through the arena.* Rebase the **role-5** arena descriptors onto the arena base and set
  `bridgeHeader.dpuCreditOffset = offsetof(HomerFrontendArena, dpuCreditLines)`, as ONE edit — see D8b for
  the exact field values and for why a nonzero offset without the rebase is silent memory corruption.
  Until this lands, `HomerDpuDmaSubmitMirroredByteRingConsumedHeadPublications` (`:4007`) will reject the
  first consumed-head publish for an arena result ring with
  `"byte-ring credit descriptor is not a payload byte ring"` (`:3064`), which fails the whole sweep.
  Space is already reserved; nothing here needs an ABI bump.

  > **✅ DONE + VALIDATED — citus `ab850f892`, July 10, 2026.** Landed EXPANDED (5 parts, bridge v3→4 —
  > the "no ABI bump" line above predates the role-5 discovery correction): rebase+credit, flag-gated
  > role-5 grouped-control enrolment (PUBLISH_LINE), engine `boundServiceSinkId` (5 sites + sink gate +
  > deferred enqueue via `HomerDpuDmaBindArenaResultSink`), arena backend result wiring, producer
  > publish-line mirror. Validated on the full four-role deployment incl. the corrected DPU→DPU spawn
  > topology (arena backend `launched_pid=884091` survived with the new wiring; sink gate silent; 4-role
  > basebackup green). Full record: bridges design doc §4 P0.
- **S4.1** Export **role 6** from `HomerClientOpenSqlSessionSelectedDpu` (today the only role-6 exporter
  is the deprecated `homer_frontend_dma.c:1617`), alongside role 1.

  > **✅ DONE + VALIDATED — citus `68051ef88`, July 10, 2026.** 1→2 rings; role-6 line resolvable at
  > import (declared session id auto-binds); farnet0 DPU accepted the 2-ring import
  > (`descriptor_count=2`) in P0 validation.
- **S4.2** Drive `START_COMMAND` down the control slot from the client, and consume role-6 completion
  events instead of `HomerClientWaitCommandCompletion`'s shm mailbox.
- **S4.3** Depends on S2+S3 for the backend to exist at all.

**Gate:** a `SELECT` returns correct decoded values. Validates D1+D2+D3 together with the smallest
possible blast radius, and is the first proof the architecture works at all.

> The command/completion machinery it leans on is **TIER 2 → 1.5**: it exists, the smoke drives it, no
> production client does. Expect bugs on first real contact — that is what this gate is for.

### S5 — DPU<->DPU command peer-open — **RE-SCOPED: mostly already done; MERGED INTO S4**

> **⏺ MERGED with S4 (July 10, 2026).** With the single-node gate dropped, the cross-node command/completion
> relay IS the S4 bring-up. Peer OPEN/spawn (below) is done; the remaining net-new work — the command and
> completion RDMA relay halves — is specified in
> [dpu_crossnode_command_completion_bridges_design.md](./dpu_crossnode_command_completion_bridges_design.md).
> The S5.x items below stay as the peer-open record; the relay steps live in the design doc's §4.

> **Observed working during the S1a gate run**, before any S5 work was attempted. The farnet0 DPU
> established a `CRITICAL_CONTROL` RDMA connection to the farnet1 DPU and shipped an
> `OP_COMMAND_SESSION` peer-open; the farnet1 DPU received it, passed every gate, and reached
> `TupleSinkServiceSubmitBackendSpawnRequest` (`:37623`). Its error travelled back over RDMA and was
> DMA'd into pgbench's control slot.

- **S5.1** ~~Relax the peer command-open gates by allow-list (`:37385`, `:37395`, `:37406`)~~
  **NO-OP.** They already pass — the peer handler ran past them to the spawn call.
- **S5.2** ~~Thread `sessionUID` through the DPU<->DPU command open~~ **ALREADY DONE**, at
  `tuple_sink_service_process.c:35401` (`peerRequest.openCommandSessionRequest.sessionUID =
  asyncOp->request.sessionUID`). `clientSqlResultDpuRelay` is carried alongside at `:35398`.
- **S5.3** The receiving DPU triggers the spawn on ITS host. **This is S3, and it is the blocker.**

**Remaining gate:** after S3, a cross-node `OP_COMMAND_SESSION` spawns a backend on the peer's host
and a completion comes back. (The "reaches the remote DPU" half is already evidenced.)

### S6 — `pgbench --homer --homer-dpu -c 1`, cross-node
**Gate:** 200 transactions, zero failures, sane / non-constant decoded `abalance`, and no host Homer
service anywhere in the command path. Then `-c 2`, which unblocks byte-ring pool Stage 2
(`tupleSourceRing` per-session).

> ### ⚠ PREREQUISITE: fix B (per-collector feedback) BEFORE recording any S6 number.
>
> Step 3 of **D10**. The shared `dpuDmaFeedback` aliases `consecutiveEmptyGrants` /
> `consecutiveProductiveGrants` across **eleven** DPU collectors, including the data-path ones
> (grouped-control read, command pull, payload pull, completion push). Their poll cadence is therefore
> decided by whichever sibling was granted last. A throughput or latency number measured on that cannot be
> attributed, and re-measuring after the fix would invalidate it. Fix B, then measure.
>
> This is not a liveness requirement — S3.1b already makes the listeners safe. It is a
> *measurability* requirement. See `dpu_scheduler_arm_execute_mismatch.md` §0b, revised rule 3.

### S7 — retire the superseded paths
Only after S6.

> **📄 Detailed, code-verified map (authoritative): [`s7_host_service_retirement_plan.md`](./s7_host_service_retirement_plan.md)**
> — the three-bucket DELETE / KEEP / GUT-THE-ARM classification at citus `7e08343f2`, with per-sub-item
> checklists and the boundary-hazard list. It **supersedes the symbol lists below**, which predate several
> refactors; two stale claims are corrected there (§2) and annotated inline below.

> ### The retirement pattern (decided with the user, applies to every item below)
>
> **Gut the branch's body first; delete the branch later.** Replace the work with a loud, fatal assertion
> — `elog(FATAL, "host-service backend spawn is retired (S7.1); use the DPU spawn path")` — and keep the
> function, its signature, and its call sites for one cycle. Then delete.
>
> Why the two steps: a retired-but-present branch turns "some forgotten caller still relies on this" from a
> silent behaviour change into an immediate, attributable crash with a name in it. Deleting first turns it
> into a compile error at best and a subtly different code path at worst. The cost is one cycle.
>
> **Every gutting must be preceded by its replacement's gate passing**, never the other way round. See the
> S3.5 box: a retired path is frequently the only regression net for the thing replacing it.

- **S7.0 — CUT THE GATE'S OWN DEPENDENCY ON THE HOST SERVICE. This is the real precondition; do it first.**
  `pgbench` mapped the host-service control region under `if (homer_mode)`, which `--homer-dpu-command` also
  sets — yet the selected-DPU session open takes no control region. Gate the `HomerClientOpenControl` call on
  `!homer_dpu_command_mode`. **Until this lands, "delete the host service" breaks the gate at client boot.**
  Retires the gate's trap-2 asymmetry (client-side host service UP / backend-side DOWN) and the whole
  stale-control-region accident class. **Validate: the gate passes with BOTH host services down.**
- **S7.1** `HomerClientOpenSqlSession` (host-SHM command path) for pgbench, **and**
  `TupleSinkServiceSubmitBackendSpawnRequest`. ⚠ **CORRECTED (2026-07-17):** that function is now a
  **pure-host** claimant — it *refuses* the DPU arm (`tuple_sink_service_process.c:22906`), and DPU spawn is
  the separate `TupleSinkServiceBeginDpuBackendSpawn` — so **delete it wholesale and gut its four call-site
  arms**, not "an `else` arm" (S3.5's one-function framing is stale; see detailed doc §2). Together these
  retire plain `pgbench --homer` and backend-to-backend COPY. **Precondition:** the S3 gate has passed (so
  `--homer-dpu-command` is the spawn net), **and S7.0 has landed**.
  ⛔ **The old second precondition — "the cross-node `--homer` baseline has been captured and SHA-stamped" —
  is WAIVED (owner, July 13, 2026).** It was written when the host path still worked; that workload is broken
  at HEAD and is deliberately not being fixed. **You cannot baseline a corpse**, and the purpose the baseline
  served (never gut a path whose replacement has no net) is already served by the DPU gate.
  Then: **dissolve D7** — delete the range constants, delete the host-arm CAS with the arm it protected, and
  restate the contract as **D7′ (exactly one claimant)**.
- **S7.2** `citus_remote_exec_pgbench_transaction` + its UDF/extern/build refs (checkpoint "step 8").
- **S7.3** The Tier-2 `homer_frontend_dma*` path and its GUC, now genuinely superseded by S1b.
  Includes `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest` (`homer_frontend_dma_lifecycle.c:188`) —
  the *last* host-resident spawn claimant. **D7′ is only true once this is gone too.**
  Also **S2.5**, moved here: the Tier-2 `SELECTED_DPU_DMA` mailbox-open path
  (`remote_execution_backend_bridge.c:2572`-`:2669`). Read it before deleting it — see "Prior art" under S0b.
- **S7.4** Re-examine whether the `sessionUID` rendezvous is still needed. **Expectation: YES, keep it**
  — the role-7 ring is still exported by a host process and imported across PCIe, a boundary that does
  not disappear when the session table moves. Confirm in code; record the reasoning.

**MUST NOT touch, at any stage** (basebackup calls the first unconditionally; `--homer-dpu` result
delivery rides the third): `HomerClientOpenBaseBackupStreamSelectedDpu`,
`HomerClientOpenBaseBackupReceiveStreamSelectedDpu`, and the role-7 SQL-result receive **folded into
`HomerClientOpenSqlSessionSelectedDpu`** (`homer_client.c:3354`/`:3474` + `HomerClientPollSqlResultDpuReceive`
`:5052`). ⚠ **CORRECTED (2026-07-17):** the old `HomerClientOpenSqlResultReceiveStreamSelectedDpu` symbol
**no longer exists**, and the line numbers previously here predated the folding; current symbols/lines are in
the detailed doc §2/§4.

---

## ✅ FIXED — the staged-command wedge (citus `0efd12c50`, `584e01dcb`)

### The bug, and why it was worse than "a client hangs"

#### The staging pipeline (needed to understand the bug)

Not a ring of staged commands. The chain is **single-slot** most of the way:

```
host role-1 control slot (slotCount = 1)
  --DMA pull--> one of commandPullBufferCount (=64) engine pull buffers
  --accept-->   ONE staged slot per (import, ring):  ringRuntime->stagedCommandSlotValid (a bool)
  --stager-->   a SERVICE-owned queue (copy out, then release the engine pull buffer)
  --action-->   DMA the command / run the semantic dispatcher
```

While a ring's staged slot is set, the pull scheduler issues **no further pull for that ring**
(`homer_service_dpu_dma.c:1154`: skip if `commandPullInFlight || stagedCommandSlotValid`).

**Why 64 buffers when a session runs one command at a time?** Because *"one at a time" is per session,
and the pool is global.* The buffers are a shared free list (`commandPullBufferInUse[]`, linear
`HomerDpuDmaFindFreeCommandPullBuffer`), and a ring holds at most one. So
`commandPullBufferCount = 64` is **how many sessions can have a control command in flight at once** —
not a pipelining depth within a session. It is over-provisioned and harmlessly so: the service-side
queues are tighter (`stagedCommandDispatchSlots[]` and `pendingResponseSlots[]` are **16** each), while
`hostMmapImportCapacity` is **1024**. Nothing here is tuned; if 64 ever binds it is a one-constant
change, and it produces backpressure rather than error.

**Why copy out at all — this is the wedge, seen from the other side.** `CopyNextStagedCommand` peeks
the *first valid ring in index order*. If a stager dispatched in place, session A's `OPEN_SESSION`
— which sits in the async peer-open state machine across an RDMA CM connect and a peer
request/response — would **remain the head for that whole time**, and session B's staged command would
never be seen. **The copy-out-and-release is the act that advances the head.** A kind that no stager
copies out is therefore a *permanent* head: the same mechanism, read in the other direction.

Two further reasons, one of which the code states outright:
- **Durability.** The async op carries a response owner (`importIndex`, `ringIndex`, generations,
  `commandOrdinal`, `requestSequence`, `acceptedPublishedEpoch`). A client disconnect recycles engine
  staging mid-async. Per the code: *"so later stages cannot accidentally depend on engine staging
  storage for response-owner metadata."*
- **Returning the scarce buffer** to the shared pool for other sessions.

**Two stagers, two destination queues** — both copy out and release, they differ in downstream work:

| Kind | Staged-slot owner | Service queue | Then |
|---|---|---|---|
| `START_COMMAND` | `...StageOneBackendCommandForPublish` (releases at `:39264`) | `backendCommandPublishSlots[]` | DMA the command record into the backend's role-2 mailbox |
| everything else | `...StageOneCommandForDispatch` (releases at `:39623`) | `stagedCommandDispatchSlots[]` | run `TupleSinkServiceDispatchLocalControlSlot` |

#### The bug

`HomerDpuDmaCopyNextStagedCommand` takes a **`const` engine**: it **PEEKS**, scanning
(`importIndex`, then `ringIndex`) in **index order** and returning the first ring whose
`stagedCommandSlotValid` is set. Only `HomerDpuDmaReleaseStagedCommandSlot` pops, and the two release
sites above are the only ones.

The DMA layer accepts five kinds (`homer_service_dpu_dma.c:9825`): `OPEN_SESSION`, `CLOSE_SESSION`,
`REPORT_POST_COMMAND_STATE`, `START_COMMAND`, `POLL_COMMAND_COMPLETION`. The dispatch stager excluded
**both** `START` and `POLL`. **Two exclusions, one alternate owner.**

The stagers normally unwedge each other — publish leaves the head if it is not START, dispatch leaves
it if it is — *because every kind has exactly one staged-slot owner*. A staged `POLL` had none. It sat
at the head forever, **starving every ring that sorts after it, permanently**. (Rings *before* it in
scan order drain normally; a wedge on the first ring stalls the whole service, which is what the
validation run saw — its only client was import 0, ring 0.)

> **"No consumer" conflated two roles, and only one was missing.**
> - **Staged-slot owner** — who releases the engine's pull buffer. `POLL` had **none**. *That was the bug.*
> - **Semantic handler** — who answers the request. `POLL` **always had one**:
>   `TupleSinkServiceHandlePollCommandCompletion` (`:37407`), reachable from the dispatcher the whole
>   time. It looks the session up by id and, if `currentCommandSequence` matches and the state is
>   `COMPLETED`/`FAILED`, fills the completion into the response.
>
> The request simply never arrived at its handler.

The old comment claimed START/POLL were "owned by the explicit backend-command and backend-completion
actions". The START half was true. **The POLL half described an action that was never written.**

Latent, not live — but **closer than "latent" suggests.** The deprecated
`homer_frontend_control.c:1255` already branches on `useDpuControlChannel` before building its POLL
request, and the Tier-2 `homer_frontend_dma.c:899` exports a role-1 `FRONTEND_CONTROL_SLOT`. **The
wedge was one GUC away.** And S4/S6 make the DPU control slot the primary command channel.

### The fix: close the partition, don't special-case POLL

The dispatch stager now skips **only `START`** and becomes the **catch-all**. Everything else falls
through to `TupleSinkServiceDispatchLocalControlSlot`, which already handles `POLL`
(`TupleSinkServiceHandlePollCommandCompletion`, `:37942`) and whose `default:` arm turns any *future*
accepted-but-unhandled kind into an error response rather than a wedge.

*Rejected:* rejecting `POLL` at the DMA accept sets `engine->fatalError` — kills the service for a
request that is legal on the host-SHM path. *Rejected:* synthesizing an error response hard-codes
"the DPU cannot POLL" when the dispatcher plainly can.

> **⚠ STAGED-COMMAND OWNERSHIP INVARIANT.** The set of request kinds the DMA layer accepts must be
> PARTITIONED, exactly one owner each: `START_COMMAND` → the backend-command publish action;
> **everything else → the dispatch stager (catch-all)**. A kind skipped by both is never released and
> stalls the engine-wide head. **If you ever add an explicit `POLL` consumer** (the "backend-completion
> action" the old comment imagined), you MUST re-exclude `POLL` from the dispatch stager *in the same
> commit*, or it will be dispatched twice. This is stated at both stagers in code.

### Two clarifications worth keeping

- **The dispatcher's `default:` is NOT an assertion.** `TupleSinkServicePumpControlSlots` CASes a
  `REQUEST_READY` host-SHM slot and dispatches it **without validating `requestKind` first**, so a host
  client writing garbage into its control slot reaches `default:` today. The layering is: the DMA
  accept's own `default:` is the **hard** guard (`engine->fatalError`, an unrecognised kind never
  stages); the dispatcher's `default:` is the **soft net** (error response, not a wedge). In a service
  that must not die on bad input, "assert it never happens" would be the wrong shape.
- **Why a stager AND a dispatcher.** The DMA engine pulls into a small fixed pool of command-pull
  staging buffers (`commandPullBufferCount`) — scarce, engine-owned, transient. The **stager** copies
  the request into the service's own `stagedCommandDispatchSlots[]` and **releases the engine buffer
  immediately**, so a slow async dispatch (an `OPEN_SESSION` can sit in the peer-open state machine for
  many scheduler passes) cannot hold a pull buffer hostage and stall all command pulls. The
  **dispatcher** then runs the semantic handler against the durable copy. They are also separate
  grantable scheduler actions with independent capacity checks.

### Does this constrain the eventual Citus-backend COPY overhaul? No — it helps.

That path (`homer_frontend_control.c`) talks to the **host-SHM** control region and reaches the
dispatcher via `TupleSinkServicePumpControlSlots`. This change touches only what the *DPU-staged* path
forwards. If that backend is later moved onto a DPU control slot, its `POLL` will now be **dispatched
instead of wedging the service**. The only cost is the one-line re-exclusion above, if a dedicated POLL
staged-slot owner is ever written.

**Will COPY want a dedicated POLL action, the way START has one? Almost certainly not.**

The asymmetry is about *data movement*, not about importance:
- `START_COMMAND` needs its own action because it must **DMA a command record into the backend's
  role-2 mailbox** — it consumes a `backendCommandPublishSlots[]` entry, a DMA task, and a
  `pendingResponseSlots[]` entry for the ack.
- `POLL_COMMAND_COMPLETION` moves nothing. It reads local session state and fills a response; the
  generic dispatch path already publishes that response. A dedicated action would buy nothing.

The only justification would be POLL becoming **hot**, and the direction of travel is the opposite:
pushed completions (the peer completion ring) *replaced* polling precisely because each poll costs a
dispatch slot, a pending-response slot, and a response DMA. `TupleSinkServiceHandlePollCommandCompletion`
says so itself — *"Normal pgbench client SQL waits on the pushed completion ring directly; this handler
is retained for debug/legacy local-control callers."*

**Recommendation for the COPY overhaul:** leave `POLL` on the generic dispatch path, or let it wither
in favour of pushed completions. If it ever does go hot, write the dedicated action **and re-exclude
`POLL` from the dispatch stager in the same commit**, per the invariant above.

### A liveness guard for the CLASS of bug (citus `584e01dcb`)

Nothing would have caught the *next* partition hole: a staged command with no consumer is accepted,
valid, and immortal. So the stager now tracks the head's identity
(`bridgeGeneration`, `importIndex`, `ringIndex`, `commandOrdinal`) across consecutive observations and
**warns once**, naming the `requestKind`, if it survives
`HOMER_SERVICE_DPU_STAGED_HEAD_STALL_WARN_OBSERVATIONS` (default 1,000,000). Diagnostic only — it never
fails the service — and it counts only after a free dispatch slot is secured, i.e. only when it *could*
have consumed the head.

**Validated by manufacturing a real wedge, not by inspection.** A farnet0-DPU-only build whose dispatch
stager also skipped `OPEN_SESSION` (a kind the S1a gate really sends), threshold lowered to 1000:

```
tuple-sink service: WARNING staged-command head has not been released after 1000 observations:
  requestKind=1 import=0 ring=0 ordinal=0 bridge_generation=11224976018495102. ...
```
`requestKind=1` is `OPEN_SESSION` — the sabotaged kind. It warned once, and **before** the client's own
timeout. Then the DPU source was restored and both regressions confirmed **zero** warnings (S1a gate;
4-role basebackup `delivered_bytes=23233738650`, `CLOSE_ACK`, rc=0).

**Evidence for the original bug**, from an A/B with a temporary client probe that emitted one `POLL`
over a DPU control slot before the session open (probe reverted, not committed):

```
PRE-FIX   POLL-PROBE: no usable response: timed out waiting for ... probe response
          pgbench: could not open ...: timed out waiting for ... open ... response
          farnet0 DPU log: ZERO "dpu control slot" lines
          -> the POLL never dispatched AND the OPEN behind it never dispatched.

POST-FIX  POLL-PROBE: ... poll completion referenced an unknown compatibility session id
          [homer-service] dpu control slot: POLL_COMMAND_COMPLETION session=999999 sequence=1
          [homer-service] dpu control slot: OPEN_SESSION opKind=2 ...
          -> a real semantic response, DMA'd back; the OPEN behind it proceeded.
```

## Open questions

1. ~~**`backendCpu`** (D3): carry it through the DPU control slot, or use service policy?~~
   **CLOSED (S1a): service policy.** `TupleSinkServiceChooseBackendCpu()`
   (`tuple_sink_service_process.c:871`) is called service-side at `:18907`; `backendCpu` is not a field
   of `CitusRemoteExecOpenSessionRequest` at all. No client-side change.
2. ~~Does the socketless backend need DOCA at startup?~~ **Dissolved by D4** — no. Only the postmaster
   does DOCA.
3. ~~Result byte-ring ownership.~~ **Dissolved by D4** — the arena owns it; the backend binds a slot.
   Still audit what reads `stream.sendQueue` on the service side.
4. **Arena sizing.** `ringCount` per import (`homer_dpu_bridge_abi.h:119`) and
   `hostMmapImportCapacity` (`homer_service_dpu_dma.h:57`) bound how many sessions one export can
   declare. Size for the target concurrency; fatal on exhaustion.
   **Partly answered:** `hostMmapImportCapacity` defaults to **1024** (`homer_service_dpu_dma.c:785`),
   so the *import* count is a non-issue at our concurrency — two exports per pgbench client is nothing.
   What still needs sizing is `ringCount` per export and the arena's per-session descriptor sets.
5. ~~**Postmaster wait-set surgery** (S3.2): confirm where `ServerLoop` builds its fd set.~~
   **CLOSED by D2′ — we do not touch the wait set at all.** For the record, the seam was located and
   sized: `ServerLoop` (`postmaster.c:1628`) waits on `pm_wait_set`, built by
   `ConfigurePostmasterWaitSet` (`:1605`) with an exactly-sized `CreateWaitEventSet(NULL,
   1 + NumListenSockets)`; `ServerLoop` handles only `WL_LATCH_SET` and `WL_SOCKET_ACCEPT` (`:1673`)
   into a `WaitEvent events[MAXLISTEN]` (`:1632`). Adding a doorbell fd would have meant **four** new
   core touch points. The agent bgworker needs **none**.

6. ~~**fd hygiene.** The postmaster's doorbell fd must be closed in every forked child.~~
   **DISSOLVED by D2′** — the postmaster holds no doorbell fd. The agent is a sibling of the backends;
   they inherit nothing from it.

7. **Trust boundary.** After S3 the DPU DMA-writes `dbOid`/`userOid` into a spawn slot, so the DPU is
   now inside the trust boundary for backend spawn (previously a local host process was). Acceptable
   for a prototype; name it rather than discover it.
8. ~~Can DOCA export a POSIX-shm (tmpfs) mapping?~~ **Answered by S0 — YES**, both DMA directions.

## Follow-ups opened by S0

- **Repair the smoke's DPU→host write leg** (S0 side-finding 3): bind a `LANDING` pool slot for each
  role-7 descriptor ref instead of calling `HomerDpuDmaGetLandingRegionMemory`, and thread the handle
  into `HomerDpuDmaSubmitByteRingWrite`. Both write legs (`--expect-dpu-to-host-payload`,
  `--expect-loop2-backpressure`) use `SOURCE_LANDING`, so **this does NOT depend on byte-ring pool
  Stage 2** (only `SOURCE_TUPLE_SOURCE` still reads `engine->tupleSourceRing`, `:3184`).
- **`engine->landingRegion` singleton survived the pool migration** (`:3976` still returns it). Audit
  whether anything besides the smoke reads it; if not, retire it with pool Stage 2.
- The smoke's client exits 0 on legs it wasn't told to expect, so a **server-side** failure surfaces
  only as `client TCP close exchange failed`. That is how this rotted unnoticed. Consider having the
  server's failure reason travel back in the CLOSE_ACK.

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

### The scheduler / async-continuation thread (read these before touching D5, D6, or S4.0)

Several decisions in this plan turned out to be the first hand-built pieces of a much larger scheduler
overhaul. They are recorded here as decisions, and *reasoned* there. If you change D5, D6, D8 or S4.0, read
these first — the constraints live in them.

- [`dpu_readiness_collectors_and_continuation_edges.md`](../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md)
  — **the root cause.** Everything is polled; a completion queue buys only *aggregation* and *a cookie slot*;
  the ready queue is a hand-rolled CQ missing the cookie. Defines the **forward index** (= D6) and the
  **reverse cookie** (= D5's `boundServiceSessionId`), and where `HomerDependencyResolve()` lands. Also
  corrects this plan's fact **F2**, and is the source of **D8 / S3.0**.
- [`dpu_scheduler_arm_execute_mismatch.md`](../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md)
  — **the symptom map.** Ten sites where the engine arms work from exact per-entity demand and then executes
  it with a linear scan on a type tag, ranked, with false positives named. Findings 2/6/7 *are* D6. Finding 1
  is on the basebackup egress path and is **unscheduled**.
- [`homer_continuation_graph_scheduler_plan.md`](../../../future-directions/citus/transport/homer_continuation_graph_scheduler_plan.md)
  — **the destination.** Its `HomerReadyCatalog` is this engine's ready queue keyed on a continuation rather
  than a ring; its `HomerWaitRegistration` is the field `HomerDpuDmaRingRuntime` does not yet have.

### The data-plane thread

- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) — the descriptor split,
  and the three `--homer-dpu` bring-up runs that exposed the wall.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) — pool Stage 2 is
  blocked behind S6.
- [session_identity_and_pairing.md](../../../future-directions/citus/transport/session_identity_and_pairing.md)
  — `sessionUID` as the cross-boundary binding key; survives this migration.
