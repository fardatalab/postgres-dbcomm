# Connection Management

## Purpose

Capture grounded notes about the original Citus control plane: session transport caching, placement/co-location routing, remote transaction/command state, and operation-specific sink state above raw connections.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [connection_management_control_plane.md](connection_management_control_plane.md) - Canonical factual note for the four semantic control-plane facets in original Citus, the shared libpq execution substrate, and the workflow/API layering above them.
- [multi_statement_two_worker_transaction_lifecycle.md](multi_statement_two_worker_transaction_lifecycle.md) - Chronological lifecycle for a cold explicit multi-statement transaction that updates data on worker A and worker B, including when worker sessions join, when 2PC is activated, and which parts are sequential vs parallel.
- [update_returning_single_shard_network_lifecycle.md](update_returning_single_shard_network_lifecycle.md) - End-to-end client/coordinator/worker lifecycle for a single-shard `UPDATE ... RETURNING`, including when worker `BEGIN`/`COMMIT` actually happen and how latency accumulates on cold vs warm paths.
<!-- kb-docs:end -->

## Related

- `../data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md`: COPY-specific buffering and send/receive path built on top of the connection layer.
- `../../future-directions/citus/transport/rdma_transport_control_plane_abstraction.md`: Forward-looking RDMA/service adaptation ideas grounded in this transport/control-plane behavior.
