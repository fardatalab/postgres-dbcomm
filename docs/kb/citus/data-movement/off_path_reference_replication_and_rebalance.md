# Off-path movement: reference table replication and shard transfer/rebalance

## Scope

This doc covers the off-path paths that were not part of the "distribute local table into shards" workflow:

- Replicating reference tables to missing nodes
- Copying/moving shard placements (including rebalance paths)

These are off-path bulk movement operations, but their data plane can be either COPY-based or logical-replication-based.

## Reference table replication workflow

Entry points:

- `replicate_reference_tables()` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:72`
- `EnsureReferenceTablesExistOnAllNodesExtended(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:114`

Coordinator-side flow:

1. Detect missing reference-table placements (`WorkersWithoutReferenceTablePlacement(...)`) at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:186`.
2. Build copy command text using `CopyShardPlacementToWorkerNodeQuery(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:802`.
3. Open loopback connection and run the copy command:
   - `GetNodeUserDatabaseConnection(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:252`
   - `ExecuteCriticalRemoteCommand(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:271`, `:275`

Generated command:

- `SELECT pg_catalog.citus_copy_shard_placement(...)` from `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:814`

Parallel reference-table transfer scheduling (background jobs):

- `ScheduleTasksToParallelCopyReferenceTablesOnAllMissingNodes(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:327`
- Schedules internal single-shard copy tasks with `citus_internal_copy_single_shard_placement(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:513` ... `:520`

Worker-side data path for the block-writes transfer mode:

1. Coordinator-side transfer creates `worker_copy_table_to_node(...)` SQL text in `CreateShardCopyCommand(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:2078` ... `:2087`).
2. Source worker executes `worker_copy_table_to_node(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_copy_table_to_node_udf.c:39`), creates a shard-copy dest receiver (`:61`), and runs `SELECT ... FROM shard` into that receiver via `ExecuteQueryStringIntoDestReceiver(...)` (`:77`).
3. `CreateShardCopyDestReceiver(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c:143`) uses `ConnectToRemoteAndStartCopy(...)` (`:98`) to open a connection to the target worker and start `COPY ... FROM STDIN` (`SendRemoteCommand(...)` at `:122`, `GetRemoteCommandResult(...)` at `:127`).
4. As tuples are produced, `ShardCopyDestReceiverReceive(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c:174`) serializes rows and pushes COPY frames to the target via `PutRemoteCopyData(...)` (`:221`).
5. On completion, `ShardCopyDestReceiverShutdown(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c:290`) ends COPY (`PutRemoteCopyEnd(...)` at `:312`) and waits for target ACK/result (`GetRemoteCommandResult(...)` at `:330`).

## Shard copy/move/rebalance workflow

Public/internal transfer entry points in `shard_transfer.c`:

- `citus_copy_shard_placement(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:190`
- `citus_move_shard_placement(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:351`
- `citus_internal_copy_single_shard_placement(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:300`

All route to:

- `TransferShards(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:472`

Rebalancer orchestration:

- Foreground rebalance: `rebalance_table_shards(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:987` -> `RebalanceTableShards(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:1932`
- Background rebalance: `citus_rebalance_start(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:1042` -> `RebalanceTableShardsBackground(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:2181`
- Background tasks issue `citus_move_shard_placement(...)` commands at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:2309` and `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:2393`

## Data plane modes under `TransferShards(...)`

### A) Block-writes copy path

- `CopyShardTablesViaBlockWrites(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1832`
- Calls `CopyShardsToNode(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1997`
- `CopyShardsToNode(...)` builds `worker_copy_table_to_node(...)` commands via `CreateShardCopyCommand(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:2078`

This is the COPY-oriented transfer mode.

### B) Logical replication path

- `CopyShardTablesViaLogicalReplication(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1760`
- Performs relation setup then `LogicallyReplicateShards(...)` for data movement at `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1804`

This mode is not COPY-based for the data phase.

Worker-side implication:

- In this mode, workers participate through logical replication plumbing invoked by `LogicallyReplicateShards(...)` instead of `worker_copy_table_to_node(...)`. So the worker data path is different from the COPY sender/receiver path above.

## Timing-spot coverage status for these workflows

- `src/include/timing_spots.h` does **not** currently define dedicated spot families specifically for reference-table replication or shard transfer/rebalance workflows.
- These flows are mostly observed indirectly via generic timing spots (for example `PG_WAIT`, and transaction/connection spots like `XACT_TS_*`) plus shared COPY-related spots when block-writes COPY mode is used.
- There are no `timing_start/timing_end` call sites in:
  - `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c`
  - `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c`
  - `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_copy_table_to_node_udf.c`
  - `/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c`

## Accuracy notes for the earlier writeup

- Accurate: these are off-path, bulk data movement operations.
- Needs refinement: off-path is not always COPY transport; rebalance/transfer may use logical replication.
- Practical rule: "off-path uses COPY" is true for table distribution and block-writes shard transfer, but incomplete as a global statement for all off-path flows.

## Related

- `off_path_bulk_copy_workflow.md` (local table distribution into shards)
- `data_movement_modes.md` (cross-mode conceptual index)
- `../../citus/transactions/timing/distributed_transaction_timing.md` (transaction timers often overlap transfer orchestration)
