# Data movement

## Purpose

Capture the conceptual modes of data movement (off-path bulk movement vs in-query tuple movement) and map them to timing spot groups.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [data_movement_modes.md](data_movement_modes.md) - Off-path COPY-based movement vs in-query push/pull tuple movement, with intended timing spots.
- [in_query_pull_intermediate_results_workflow.md](in_query_pull_intermediate_results_workflow.md) - Worker pulls an intermediate result file from another node via `fetch_intermediate_results()` + `COPY ... TO STDOUT (format result)`.
- [in_query_push_intermediate_results_workflow.md](in_query_push_intermediate_results_workflow.md) - Coordinator broadcasts intermediate results via `RemoteFileDestReceiver` + `COPY ... FROM STDIN (format result)`.
- [mechanism_matrix.md](mechanism_matrix.md) - Scenario-by-scenario mapping from Citus workflows to `DataRow`, PostgreSQL COPY, or other transfer mechanisms.
- [off_path_bulk_copy_workflow.md](off_path_bulk_copy_workflow.md) - Distribute local table data into shard placements: heap scan feeding a Citus DestReceiver that speaks COPY to workers.
- [off_path_local_table_distribution_buffering_and_copy_costs.md](off_path_local_table_distribution_buffering_and_copy_costs.md) - Sender/receiver buffer chain for local-table distribution into shards, plus concrete copy-reduction directions.
- [off_path_reference_replication_and_rebalance.md](off_path_reference_replication_and_rebalance.md) - Reference-table replication and shard move/copy/rebalance flows (`TransferShards`) including COPY and logical-replication data planes.
- [portal_result_streaming_workflow.md](portal_result_streaming_workflow.md) - Worker streams query results to coordinator as libpq `PGresult` (no intermediate files), handled by adaptive executor `ReceiveResults_*`.
<!-- kb-docs:end -->

## Related

- `../../instrumentation/timing-spots/timing_spots.md`: Canonical spot catalog.
- `../connection-management/connection_management_control_plane.md`: Original Citus control-plane facets and API layering that sit under the data-movement workflows.
- `../../postgres/copy/`: PostgreSQL COPY implementation details (used as a building block for Citus data movement).
