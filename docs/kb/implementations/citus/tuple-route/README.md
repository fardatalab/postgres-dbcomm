# Tuple Route

<!-- kb-summary: Historical tuple-route checkpoints and current tuple-view queue lifetime contracts. -->

## Purpose

Capture the actual code and behavior of the experimental tuple-route prototype as it is implemented step by step. Use this directory when the question is “what does our current tuple-route code do now?” rather than “how does existing Citus work?” or “what is the target RDMA/service design?”

Historical naming note:

- this directory keeps the older `tuple-route` name because it documents the historical checkpoint sequence
- the active substrate/control-plane code has since been renamed to `tuple_sink_*`

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Current borrowed-tuple and receive-record lifetime contracts for the tuple-view queue substrate.
- [local_batch_materialization_checkpoint.md](local_batch_materialization_checkpoint.md) - Historical tuple-route checkpoint for shared-memory batching beneath the later service-owned control path.
<!-- kb-docs:end -->

## Related

- `../../../citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md`: Grounded current COPY-based sender/receiver path.
- `../../../future-directions/citus/data-movement/off_path_local_table_rdma_tuple_service_first_prototype.md`: Target design for the off-path local-table tuple-service prototype.
- `../../../future-directions/citus/transport/rdma_transport_control_plane_abstraction.md`: Transport/control-plane design space that this prototype is intentionally not implementing yet.
- `../connection-management/remote_execution_session_wrapper_checkpoint.md`: Current implementation state of the session-centric wrapper that now fronts this tuple-route prototype.
