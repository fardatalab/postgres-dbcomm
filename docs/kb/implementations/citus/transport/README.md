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
- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md) - Current checkpoint for the service-to-service byte-ring RDMA substrate used by Homer basebackup streams.
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) - Two OPEN regressions found during `pgbench --homer-dpu` bring-up: `slotCapacityBytes` now names two different quantities (synthetic ring geometry vs negotiated per-record capacity), aborting every byte-ring tuple-view stream; and backend-to-backend COPY hangs at HEAD from a *different*, unbisected cause. Explains why the tempting fix breaks the frontend attach check.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md) - TODO
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) - Design + staged plan (with concrete API) for the per-session DPU byte-ring resource pool that fixes the concurrent-session ring aliasing (engine-singleton mirror/landing/tuple-source rings) blocking concurrent cross-node DPU basebackup.
- [dpu_command_plane_migration_plan.md](dpu_command_plane_migration_plan.md) - Detailed, agreed plan (S0-S7) to move the SQL command plane onto the DPU. A PREREQUISITE for `pgbench --homer-dpu`, not a follow-on: the DPU engine only ever imports HOST memory, so a host-resident service has nothing to import and the result stream can never egress. Target is symmetric Homer frontends - client and PG backend each export their rings to their own local DPU. Includes the trust-tier rule ("compiles" is not "works"), decision D10 (a listener is admitted by a poll-due obligation, not a readiness claim), and the postmaster doorbell design for DPU-triggered backend spawn. S3.1b validated at citus `84ac374ef`; S3.2 doorbell + S3.3 spawn DMA are the critical path.
- [dpu_dma_backend_homer_service_implementation_checkpoint.md](dpu_dma_backend_homer_service_implementation_checkpoint.md) - Stage-by-stage implementation checkpoint for the DPU-pull Homer service migration.
- [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) - DPU payload byte-ring: wrap-gap protocol, the mirror-must-be-1:1-with-source invariant, the reframing record-header-parse egress, and the mirror-ring stall bug + decided constant-size fix.
- [homer_current_implementation_checkpoint.md](homer_current_implementation_checkpoint.md) - **STALE (July 3, 2026) -- milestone history only.** Predates the command-plane migration and P1-P6; its model of a session is wrong. See `selected_dpu_session_rings_and_lifecycle.md` instead.
- [selected_dpu_session_rings_and_lifecycle.md](selected_dpu_session_rings_and_lifecycle.md) - **ARCHITECTURE OF RECORD for the selected-DPU path.** THE CONTRACT (a session binds every ring it needs in ONE export, because the session kind determines the ring set); the FOUR identities and which question each answers (sessionKey is a REUSE key and is NOT unique -- ownership is IDENTITY, never policy); the ring roles; the open/command/close/teardown lifecycle across both nodes; and the ONE bug family behind everything expensive here -- a missing thing read as a definite negative.
- [payload_doorbell_token_v15_checkpoint.md](payload_doorbell_token_v15_checkpoint.md) - Landed checkpoint for peer protocol v15 payload doorbell tokens and fixed service-owned payload ready queues.
- [peer_client_completion_v27_checkpoint.md](peer_client_completion_v27_checkpoint.md) - Landed checkpoint for peer-client completion mailbox v27, full-slot WRITE_WITH_IMM publication, and receiver-service CPU publication.
- [peer_recv_dispatcher_v14_checkpoint.md](peer_recv_dispatcher_v14_checkpoint.md) - Landed checkpoint for peer protocol v14 immediate-data ABI and permanent recv-CQ dispatcher Slice 2A.
<!-- kb-docs:end -->

## Related

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
