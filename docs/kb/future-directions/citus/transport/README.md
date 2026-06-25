# Transport

## Purpose

Index forward-looking transport and control-plane designs for Citus, especially where current libpq-based connection management may need a new abstraction for RDMA or external data-plane services.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md) - Standalone host-DPU validation plan for DMA ordering, visibility, sync-event publication, COMCH wakeups, relaxed ordering, and cache-coherency assumptions before Homer integration.
- [doca_host_dpu_homer_boundary.md](doca_host_dpu_homer_boundary.md) - DOCA-grounded host-DPU boundary plan for moving the current backend-to-Homer process edge onto BlueField, including DMA, PE batching, COMCH/sync-event notifications, and publication caveats.
- [homer_completion_publication_v27_recovery_plan.md](homer_completion_publication_v27_recovery_plan.md) - Audited recovery plan for peer-client completion publication: one full-slot WRITE_WITH_IMM, receiver CPU publication, descriptor seals, recv-CQ ownership, Stage 6 readiness, and follow-on hot-path cleanup.
- [homer_payload_publication_owner_frontier_plan.md](homer_payload_publication_owner_frontier_plan.md) - Focused Homer payload-publication design note for send-owner reservation, byte-ring tail derivation from in-band records, and eventual removal of the extra byte-ring tail WIMM.
- [homer_payload_control_unification_plan.md](homer_payload_control_unification_plan.md) - Planned unification milestone for Homer byte-ring payload lifecycle, peer command/control lifecycle, and neutral bidirectional payload-stream scheduler facts without rewriting DB-semantic adapters.
- [homer_transport_hot_path_optimization_plan.md](homer_transport_hot_path_optimization_plan.md) - Staged Homer transport optimization plan for CQ ownership, metadata hot/cold splits, payload/completion overlap, scheduler budgets, and basebackup bulk-path cleanup after the correctness fixes.
- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md) - Homer scheduler-ready design and checkpoint, including the completed service-progress/egress scheduler substrate plus remaining policy, traffic-class, multi-QP, peer-poll fallback cleanup, and fragmentation directions.
- [homer_unified_aggregate_action_scheduler_plan.md](homer_unified_aggregate_action_scheduler_plan.md) - Merged guarded-action scheduler redesign plan: bounded aggregate candidates, unified grant vectors, explicit peer-control/CQ ownership prerequisites, and staged migration from the current machine/collector plus egress split.
- [peer_client_completion_publication_pipeline_plan.md](peer_client_completion_publication_pipeline_plan.md) - Targeted plan for making cross-node client-SQL completion publication nonblocking: slot-local validation, async RDMA source slots, tagged send-CQ retirement, and scheduler-visible source-credit retry.
- [peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md) - Implementation status and cleanup plan for replacing normal service-to-service peer completion polling with a requester-owned peer command completion ring.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md) - RDMA publication/visibility rules, including why the tuple-sink prototype now uses WRITE_WITH_IMM doorbells rather than trusting memory polling alone as the responder-visible publish event.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md) - TODO
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md) - Current state-machine service-progress scheduler design, default `machine-baseline` policy, validation gate, known caveats, and future adaptive-policy directions.
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current behavior that motivates these transport abstractions.
- `../connection-management/remote_execution_session_control_plane.md`: Chosen higher-level control-plane abstraction that transport designs must serve.
- `../data-movement/command_dispatch_completion_plane.md`: Typed command dispatch/completion plane that the unification plan keeps semantically separate from payload-stream open.
- `../intermediate-results/intermediate_results_service_future_directions.md`: Broader tuple-service design space that depends on the transport/control-plane choice.
