# Transport

## Purpose

Index forward-looking transport and control-plane designs for Citus, especially where current libpq-based connection management may need a new abstraction for RDMA or external data-plane services.

## Subdirectories

<!-- kb-subdirs:start -->
- [dpu_dma_backend_homer_service_plan_doca_headers](dpu_dma_backend_homer_service_plan_doca_headers/) - Local snapshot of DOCA headers referenced by the DPU DMA backend Homer service plan.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md) - Standalone host-DPU validation plan for DMA ordering, visibility, sync-event publication, COMCH wakeups, relaxed ordering, and cache-coherency assumptions before Homer integration.
- [doca_host_dpu_homer_boundary.md](doca_host_dpu_homer_boundary.md) - DOCA-grounded host-DPU boundary plan for moving the current backend-to-Homer process edge onto BlueField, including DMA, PE batching, COMCH/sync-event notifications, and publication caveats.
- [dpu_dma_backend_homer_service_current_scheduler_design.md](dpu_dma_backend_homer_service_current_scheduler_design.md) - Current-scheduler implementation companion for the DPU DMA Homer migration, including grouped-control ABI, callback retirement, TCP setup lifecycle, and bounded machine-baseline scheduler integration.
- [dpu_dma_backend_homer_service_plan.md](dpu_dma_backend_homer_service_plan.md) - Implementation-facing plan for a DPU-resident Homer backend service that uses DOCA DMA to pull backend-produced host rings, push completions, and preserve grouped publication frontiers.
- [homer_completion_publication_v27_recovery_plan.md](homer_completion_publication_v27_recovery_plan.md) - Audited recovery plan for peer-client completion publication: one full-slot WRITE_WITH_IMM, receiver CPU publication, descriptor seals, recv-CQ ownership, Stage 6 readiness, and follow-on hot-path cleanup.
- [homer_continuation_graph_scheduler_plan.md](homer_continuation_graph_scheduler_plan.md) - TODO
- [homer_frontend_service_separation_plan.md](homer_frontend_service_separation_plan.md) - Mechanical refactor plan for making the PostgreSQL/Citus-facing Homer frontend a separately identifiable component, splitting shared ABI headers from SHM channel details, and preparing the current SHM path for later DPU/DMA replacement.
- [homer_payload_control_unification_plan.md](homer_payload_control_unification_plan.md) - Planned unification milestone for Homer byte-ring payload lifecycle, peer command/control lifecycle, and neutral bidirectional payload-stream scheduler facts without rewriting DB-semantic adapters.
- [homer_payload_publication_owner_frontier_plan.md](homer_payload_publication_owner_frontier_plan.md) - Focused Homer payload-publication design note for send-owner reservation, byte-ring tail derivation from in-band records, and eventual removal of the extra byte-ring tail WIMM.
- [homer_transport_hot_path_optimization_plan.md](homer_transport_hot_path_optimization_plan.md) - Audited Homer transport optimization plan: several stages are implemented or superseded by the v27 recovery work, while remaining items are lower-priority caveats and future design constraints.
- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md) - Homer scheduler-ready design and checkpoint, including the completed service-progress/egress scheduler substrate plus remaining policy, traffic-class, multi-QP, peer-poll fallback cleanup, and fragmentation directions.
- [homer_unified_aggregate_action_scheduler_plan.md](homer_unified_aggregate_action_scheduler_plan.md) - Merged guarded-action scheduler redesign plan: bounded aggregate candidates, unified grant vectors, explicit peer-control/CQ ownership prerequisites, and staged migration from the current machine/collector plus egress split.
- [peer_client_completion_publication_pipeline_plan.md](peer_client_completion_publication_pipeline_plan.md) - Historical peer-client completion publication plan, now superseded by `homer_completion_publication_v27_recovery_plan.md`; retained for rejected intermediate designs and pitfalls.
- [peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md) - Implementation status and cleanup plan for replacing normal service-to-service peer completion polling with a requester-owned peer command completion ring.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md) - RDMA publication/visibility rules, including why the tuple-sink prototype now uses WRITE_WITH_IMM doorbells rather than trusting memory polling alone as the responder-visible publish event.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md) - TODO
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md) - Current state-machine service-progress scheduler design, default `machine-baseline` policy, validation gate, known caveats, and future adaptive-policy directions.
- [session_identity_and_pairing.md](session_identity_and_pairing.md) - Cross-node session identity model (sessionKey vs node-local serviceSessionId vs cross-node sessionUID), the basebackup throwaway-session/registry warts, the DPU-instance spike that killed the handshake plan, and the adopted receiver-session base-compat+tag scan (no handshake).
- [dpu_readiness_collectors_and_continuation_edges.md](dpu_readiness_collectors_and_continuation_edges.md) - Why everything is polled (recv-CQ, doca_pe, and a memory word are all polls), what a completion queue actually buys (aggregation + a cookie slot), the DPU ready queue as a hand-rolled cookie-less CQ, and the two missing continuation-graph edges: the forward index (waiter->resource) and the reverse cookie (resource->waiter, where HomerDependencyResolve lands). Grouped-control is parameterized and the parameter is 1. READ FIRST when resuming the async-continuation work.
- [dpu_scheduler_arm_execute_mismatch.md](dpu_scheduler_arm_execute_mismatch.md) - Symptom map for the above: ten sites where the DPU scheduler arms work from exact per-entity demand and then executes it with a linear scan on a type tag, ranked by hot-path x cost gap, plus the ready-queue audit (5 of 7 kinds dead) and named false positives.
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current behavior that motivates these transport abstractions.
- `../../../implementations/citus/transport/homer_current_implementation_checkpoint.md`: Current landed Homer implementation map that consolidates the frontend/service refactor, RDMA transport checkpoints, tuple COPY, pgbench, and basebackup paths.
- `../connection-management/remote_execution_session_control_plane.md`: Chosen higher-level control-plane abstraction that transport designs must serve.
- `../data-movement/command_dispatch_completion_plane.md`: Typed command dispatch/completion plane that the unification plan keeps semantically separate from payload-stream open.
- `../intermediate-results/intermediate_results_service_future_directions.md`: Broader tuple-service design space that depends on the transport/control-plane choice.
- `../../../implementations/citus/transport/dpu_command_plane_migration_plan.md`: The plan actively consuming these designs. D5 (reverse cookie), D6 (forward index / continuation-graph edge #1) and D8 (arena publish-line reservation) are decided there and reasoned in `dpu_readiness_collectors_and_continuation_edges.md`.
