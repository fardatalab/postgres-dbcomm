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
- [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) - DPU payload byte-ring: wrap-gap protocol, the mirror-must-be-1:1-with-source invariant, the reframing record-header-parse egress, and the mirror-ring stall bug + decided constant-size fix.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) - Design + staged plan (with concrete API) for the per-session DPU byte-ring resource pool that fixes the concurrent-session ring aliasing (engine-singleton mirror/landing/tuple-source rings) blocking concurrent cross-node DPU basebackup.
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) - Two OPEN regressions found during `pgbench --homer-dpu` bring-up: `slotCapacityBytes` now names two different quantities (synthetic ring geometry vs negotiated per-record capacity), aborting every byte-ring tuple-view stream; and backend-to-backend COPY hangs at HEAD from a *different*, unbisected cause. Explains why the tempting fix breaks the frontend attach check.
- [dpu_command_plane_migration_plan.md](dpu_command_plane_migration_plan.md) - Planned "Stage B": move the SQL command/completion plane onto the DPU so a session's command spine and its result relay live in one process. Decomposes the spine into three legs (host→local-DPU exists; DPU↔DPU is net-new; DPU→remote-host backend spawn is unknown and gates the size of the work), and records why the pgbench-transaction UDF retires last.
- [dpu_dma_backend_homer_service_implementation_checkpoint.md](dpu_dma_backend_homer_service_implementation_checkpoint.md) - Stage-by-stage implementation checkpoint for the DPU-pull Homer service migration.
- [homer_current_implementation_checkpoint.md](homer_current_implementation_checkpoint.md) - Current canonical implementation map for the landed Homer frontend, service, shared ABI, RDMA transport, tuple COPY, pgbench, and basebackup paths after the frontend/service separation refactor.
- [payload_doorbell_token_v15_checkpoint.md](payload_doorbell_token_v15_checkpoint.md) - Landed checkpoint for peer protocol v15 payload doorbell tokens and fixed service-owned payload ready queues.
- [peer_client_completion_v27_checkpoint.md](peer_client_completion_v27_checkpoint.md) - Landed checkpoint for peer-client completion mailbox v27, full-slot WRITE_WITH_IMM publication, and receiver-service CPU publication.
- [peer_recv_dispatcher_v14_checkpoint.md](peer_recv_dispatcher_v14_checkpoint.md) - Landed checkpoint for peer protocol v14 immediate-data ABI and permanent recv-CQ dispatcher Slice 2A.
<!-- kb-docs:end -->

## Related

- `../../../future-directions/citus/transport/`: Transport scheduler, payload
  stream, and RDMA publication design notes.
- `../../../implementations/postgres/replication/`: PostgreSQL basebackup target
  integration notes that feed this transport path.
- `homer_current_implementation_checkpoint.md`: Current top-level implementation
  map for the Homer stack; older checkpoint docs remain detailed milestone
  history.
