# Citus Transport Implementations

## Purpose

Track implementation checkpoints for Homer transport-layer prototype work in
Citus, including RDMA substrate changes that are separate from the higher-level
remote execution session semantics.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - ⭐ **READ BEFORE WRITING CODE that touches session / ring / sink ownership.** The area's INVERTED INDEX: symbol-keyed statements of what a thing MEANS, what it does **NOT** mean (and the near-neighbour it will be confused with), the contract it carries, and EVERY site that must obey it. Covers: `descriptor->serviceSessionId` (0 for every ring — the wrong way to ask who owns a ring); `completion.commandState` (MANUFACTURED — key on `commandKind`); `activeSinkCount` (one decrement funnel; one miss makes every retirement path refuse); `backendLoopActive` (a scheduler input, not just a backend flag); the selected-session table (BOTH DPUs have one); plus why SEVEN resources are all sized 64 and each exhaustion masks the next.
- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md) - Current checkpoint for the service-to-service byte-ring RDMA substrate used by Homer basebackup streams.
- [copy_path_revival_contract.md](copy_path_revival_contract.md) - **Read before touching backend-to-backend COPY, and before assuming a P0 substrate change is COPY-safe.** What is actually broken (COPY's CONTROL plane, bounded to before backend-spawn submission) versus what is NOT (the payload/retirement substrate, which other workloads exercise every run); why the `a3cdd5f3c` suspect is REFUTED and the bisect still has to be run; the full coverage ledger (what only COPY reaches, what NOTHING reaches); the invariants P0 has newly imposed that a revival must uphold; and the latent bugs that will look like new ones.
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) - Two OPEN regressions found during `pgbench --homer-dpu` bring-up: `slotCapacityBytes` now names two different quantities (synthetic ring geometry vs negotiated per-record capacity), aborting every byte-ring tuple-view stream; and backend-to-backend COPY hangs at HEAD from a *different*, unbisected cause. Explains why the tempting fix breaks the frontend attach check.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md) - TODO
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) - Design + staged plan (with concrete API) for the per-session DPU byte-ring resource pool that fixes the concurrent-session ring aliasing (engine-singleton mirror/landing/tuple-source rings) blocking concurrent cross-node DPU basebackup.
- [dpu_command_plane_migration_plan.md](dpu_command_plane_migration_plan.md) - Detailed, agreed plan (S0-S7) to move the SQL command plane onto the DPU. A PREREQUISITE for `pgbench --homer-dpu`, not a follow-on: the DPU engine only ever imports HOST memory, so a host-resident service has nothing to import and the result stream can never egress. Target is symmetric Homer frontends - client and PG backend each export their rings to their own local DPU. Includes the trust-tier rule ("compiles" is not "works"), decision D10 (a listener is admitted by a poll-due obligation, not a readiness claim), and the postmaster doorbell design for DPU-triggered backend spawn. S3.1b validated at citus `84ac374ef`; S3.2 doorbell + S3.3 spawn DMA are the critical path.
- [dpu_crossnode_command_completion_bridges_design.md](dpu_crossnode_command_completion_bridges_design.md) - Design + implementation steps for the cross-node selected-DPU command/completion bridges (the merged **S4+S5** of the command-plane migration). Homer is RDMA-only here; the single-node "local fast path" is deleted. Also carries the §10k investigation and its resolution: the ~1.5 s per-connection stall was an **RNR-NAK race** -- the accepting side posted its bootstrap RECV *after* `rdma_accept()`, and `rnr_retry_count=7` (infinite) turned a hard error into a silent ~655 ms-per-retry latency tax. Fixed in P7 (citus `d33f6ded3`); peer connect 1450 ms -> 10 ms.
- [dpu_dma_backend_homer_service_implementation_checkpoint.md](dpu_dma_backend_homer_service_implementation_checkpoint.md) - Stage-by-stage implementation checkpoint for the DPU-pull Homer service migration.
- [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) - DPU payload byte-ring: wrap-gap protocol, the mirror-must-be-1:1-with-source invariant, the reframing record-header-parse egress, and the mirror-ring stall bug + decided constant-size fix.
- [homer_current_implementation_checkpoint.md](homer_current_implementation_checkpoint.md) - **STALE (July 3, 2026) -- milestone history only.** Predates the command-plane migration and P1-P6; its model of a session is wrong. See `selected_dpu_session_rings_and_lifecycle.md` instead.
- [payload_doorbell_token_v15_checkpoint.md](payload_doorbell_token_v15_checkpoint.md) - Landed checkpoint for peer protocol v15 payload doorbell tokens and fixed service-owned payload ready queues.
- [peer_client_completion_v27_checkpoint.md](peer_client_completion_v27_checkpoint.md) - Landed checkpoint for peer-client completion mailbox v27, full-slot WRITE_WITH_IMM publication, and receiver-service CPU publication.
- [peer_recv_dispatcher_v14_checkpoint.md](peer_recv_dispatcher_v14_checkpoint.md) - Landed checkpoint for peer protocol v14 immediate-data ABI and permanent recv-CQ dispatcher Slice 2A.
- [dpu_collector_feedback_aliasing_defect_b.md](dpu_collector_feedback_aliasing_defect_b.md) - **PLAN, ACTIVE. Blocks command-plane S6.** Twelve DPU collectors share ONE `HomerProgressSourceFeedback`, so an empty grant by ANY sibling both drives the discovery collector toward blind-backoff suppression AND refreshes the starvation clock that is supposed to release it -- the escape hatch can never open. This is the SAME mechanism that silently wedged the DPU setup listener for four days; it was diagnosed correctly and fixed for **one** collector, and the other twelve were left aliased (the code's own comment at `tuple_sink_service_process.c:2572` is the post-mortem). Fix is a ROUTING change, not a redesign: the per-action result already exists and is discarded at the finish site. Also records an OVERCLAIM made and retracted -- this is a candidate cause of the measured ~223us discovery latency, **not a proven one**; probe (FB-0) before fixing.
- [resource_retirement_contract_audit.md](resource_retirement_contract_audit.md) - **P0 SET COMPLETE** (P0-a/b/c/d/f/i + S24/F7/S29 all landed; P0-e DROPPED, its bug REFUTED). **P7b is unblocked.** THE RESOURCE CONTRACT ("retire before you release") plus a full audit of where it is violated across RDMA and DOCA DMA, and the remediation plan. Central finding: the contract is upheld EXACTLY where someone was previously burned and nowhere else -- scar tissue, not design. Worst item: the dead-owner arena reaper frees and ZEROES a host arena slot with no knowledge of the DPU's in-flight DMA, and its safety argument silently does not cover the DPU (a separate machine that is not restarted). Read before landing connection pooling -- pooling invalidates every "cannot happen, the object is destroyed anyway" argument.
- [selected_dpu_session_rings_and_lifecycle.md](selected_dpu_session_rings_and_lifecycle.md) - **ARCHITECTURE OF RECORD for the selected-DPU path.** THE CONTRACT (a session binds every ring it needs in ONE export, because the session kind determines the ring set); the FOUR identities and which question each answers (sessionKey is a REUSE key and is NOT unique -- ownership is IDENTITY, never policy); the ring roles; the open/command/close/teardown lifecycle across both nodes; and the ONE bug family behind everything expensive here -- a missing thing read as a definite negative.
- [tuple_deform_two_ring_relay_overview.md](tuple_deform_two_ring_relay_overview.md) - Follows ONE SQL result row end to end: the socketless backend writes PostgreSQL-aware **tuple-view** batches, the services move those bytes between hosts, the receiving DPU **deforms** each packed row into an O(1)-addressable decoded image, and the client drains that image from its own role-7 ring. No result row takes libpq's normal path. Start here to understand the result plane (the command plane is a different axis).
- [send_cqe_coalescing_implementation_plan.md](send_cqe_coalescing_implementation_plan.md) - **PLAN OF RECORD for re-arming send-CQE coalescing** (the arc's original goal; owner decision 2026-07-15: do it before S6). Three stages: (1) terminal flush = the close checkpoint (already signalled) + the MISSING abort notification (also fixes the basebackup abort-hang); (2) re-arm the interval (per-QP `SQ/2` + pool-pressure + terminal decision, QP-wide frontier stamping, unit-level reservation, flip the P0-i tripwires); (3) the uniform coalesced CQ handling (de-schedule the send-CQ drain to a post-clocked poll) + FB-2 feedback de-alias + cost instrumentation. Carries the 2026-07-15 site-enumeration **CORRECTIONS** to the design doc -- most importantly ⛔ **C5 is dead** (its depth-1 "~450µs fence" target is dead code; every live pool is already deep), so pool-deepening is dropped and the 235.4µs latency cause is unattributed + tracked separately.
<!-- kb-docs:end -->

## Related

- `../../../operations/`: **How to actually run and validate any of this.** The farnet runbook's backing
  store: every check that lies (`farnet_operational_hazards.md`) and the diagnostic instruments +
  measurement history (`farnet_diagnostics_and_baselines.md`). Most hazards there were discovered while
  validating the docs in *this* directory.
- `../../../future-directions/citus/transport/`: Transport scheduler, payload
  stream, and RDMA publication design notes.
- `../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md`:
  Root cause behind several decisions in `dpu_command_plane_migration_plan.md` (D5 reverse cookie,
  D6 forward index, D8 arena publish-line reservation). Read before changing them.
- `../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`: Ranked map of the
  ten sites where the DPU scheduler arms from exact demand and executes by linear scan. Finding 1 is on
  the basebackup egress path and is unscheduled.
- `../../../implementations/postgres/replication/`: PostgreSQL basebackup target
  integration notes that feed this transport path.
- `selected_dpu_session_rings_and_lifecycle.md`: **Start here** for the selected-DPU
  path. `homer_current_implementation_checkpoint.md` and the other checkpoint docs are
  milestone history; several of their code pointers and premises predate the
  command-plane migration.
