# Data Movement

## Purpose

Index concrete future-direction designs for replacing or restructuring Citus data-movement paths.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [command_dispatch_completion_plane.md](command_dispatch_completion_plane.md) - Command dispatch/completion design and status: typed single-client client-SQL pgbench transactions now run through Homer, with tuple-result sinks and concurrent sessions still future work.
- [off_path_local_table_rdma_tuple_service_first_prototype.md](off_path_local_table_rdma_tuple_service_first_prototype.md) - TODO
- [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md) - Long-term PostgreSQL-side dispatcher and executor-pool design for receiving typed command dispatches from the external service without SQL UDF or libpq start-wait.
- [tuple_sink_data_plane_completion_plan.md](tuple_sink_data_plane_completion_plan.md) - Remaining future work after the first tuple-sink vertical slice: speculative startup, richer backpressure/wakeups, failure surfacing, cross-transaction reuse, and later wire-format redesign.
<!-- kb-docs:end -->

## Related

- `../../../citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md`: Grounded current path and copy/allocation costs.
- `../transport/rdma_transport_control_plane_abstraction.md`: Transport/control-plane design space that this prototype instantiates.
