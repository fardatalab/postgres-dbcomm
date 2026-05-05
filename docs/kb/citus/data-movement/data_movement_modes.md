# Citus data movement modes (conceptual) and intended timing spots

## Scope

This doc incorporates earlier writeups about:

- Off-path bulk movement (table distribution / reference table replication / shard rebalancing)
- In-query tuple movement (repartition joins, gather, scatter)
- Push-based vs pull-based shuffling
- Portal execution / streaming results

Unlike the initial KB snapshot, the Citus extension source is available at `/data/dbcomm/citus-dbcomm`. This doc now ties the conceptual writeup to concrete Citus code paths and timing spots.

## Off-path data movement (bulk COPY-based)

Off-path data movement typically uses PostgreSQL COPY as the serialization + transport primitive.

### Distributing a local table into shards

The “copy local table into shards” path is implemented by scanning a local relation and forwarding each tuple into a Citus COPY DestReceiver that writes into shard placements.

Key code pointers:

- `CreateCitusTable(...)` starts `CreateCitusTable_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1107` … `:1114`) and ends it at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1384`.
- `CopyFromLocalTableIntoDistTable(...)` is now explicitly timed by `CopyFromLocalTableIntoDistTable_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2696` … `:2778`).
- `DoCopyFromLocalTableIntoShards(...)` times the per-tuple scan+send loop with `DoCopyFromLocalTableIntoShards_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2792` … `:2819`).
- The DestReceiver side that serializes + sends tuples to placements is timed by:
  - `CitusSendTupleToPlacements_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2358` … `:2456`)
  - `SerializeAndCopyRow_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2419` … `:2428`, and `:2439` … `:2445`)
  - `SendCopyDataToPlacement_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2379` … `:2385`, `:2391` … `:2408`, `:2448` … `:2452`)

This confirms the writeup’s key point: bulk/shard distribution piggybacks on the COPY protocol and COPY row formatting, even though the orchestration is Citus-specific (DestReceiver + multi-connection placement management).

Nuance vs the writeup:

- The *logical* operation is not `COPY table ...`; it is a heap scan (`table_scan_getnextslot`) that feeds tuples into a Citus DestReceiver which speaks COPY to the workers. So COPY is the **transport/format**, while the scan/orchestration is Citus-specific.

### Reference table replication and shard rebalance/transfer (also off-path)

The earlier writeup is directionally correct that these are off-path bulk movement operations, but the concrete transfer mechanism is not always "just COPY".

Reference table replication entry points:

- `replicate_reference_tables()` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:72`) calls `EnsureReferenceTablesExistOnAllNodesExtended(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:114`).
- That path builds a `citus_copy_shard_placement(...)` SQL command via `CopyShardPlacementToWorkerNodeQuery(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:802`) and executes it remotely (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/reference_table_utils.c:262`, `:275`).
- `citus_copy_shard_placement(...)` dispatches to `TransferShards(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:190`, `:207`).

Block-writes mode worker-side transfer path:

- `CreateShardCopyCommand(...)` emits `worker_copy_table_to_node(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:2078` ... `:2087`).
- On the source worker, `worker_copy_table_to_node(...)` drives query execution into `CreateShardCopyDestReceiver(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_copy_table_to_node_udf.c:39`, `:61`, `:77`).
- `ShardCopyDestReceiverReceive(...)` serializes/sends COPY data to the target worker (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c:174`, `:221`), and shutdown finalizes via `PutRemoteCopyEnd(...)` + result ACK (`:312`, `:330`).

Shard rebalance/move entry points:

- `rebalance_table_shards(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:987`) and `citus_rebalance_start(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:1042`) eventually run `RebalanceTableShards(...)` / `RebalanceTableShardsBackground(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:1932`, `:2181`).
- Those paths issue `citus_move_shard_placement(...)` or `citus_copy_shard_placement(...)` commands (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_rebalancer.c:2309`, `:2393`), which again route into `TransferShards(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:472`).

Transfer modes in `TransferShards(...)`:

- **Block-writes mode**: `CopyShardTablesViaBlockWrites(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1832`) runs `CopyShardsToNode(...)` (`:1997`) which issues `worker_copy_table_to_node(...)` via `CreateShardCopyCommand(...)` (`:2078` ... `:2087`).
- **Logical replication mode**: `CopyShardTablesViaLogicalReplication(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c:1760`) uses logical replication instead of COPY for the data phase (`:1804`).

So, for off-path movement:

- "uses COPY" is accurate for distribution and block-writes shard copy paths.
- It is incomplete for rebalance/repair flows, where logical replication may be selected.

### Intended timing spots for other off-path operations

The following off-path operations are described in the writeup, but do not currently have their own dedicated `timing_spots.h` spot families in this repo snapshot:

- Reference table replication (implemented through `citus_copy_shard_placement` / `TransferShards` paths).
- Shard rebalancing and move/copy operations (implemented through `rebalance_table_shards` + `TransferShards` paths).

If/when dedicated timing spots are added for those workflows, they should cross-link to this doc and to `docs/kb/citus/intermediate-results/push_pull_intermediate_results.md`.

Intended timing spots (bulk distribution/replication/rebalance) from `timing_spots.h`:

- `CreateCitusTable_` (`src/include/timing_spots.h:84`)
- `CopyFromLocalTableIntoDistTable_` (`src/include/timing_spots.h:85`)
- `DoCopyFromLocalTableIntoShards_` (`src/include/timing_spots.h:86`)
- `CitusSendTupleToPlacements_` (`src/include/timing_spots.h:87`)
- `SerializeAndCopyRow_` (`src/include/timing_spots.h:88`)
- `SendCopyDataToPlacement_` (`src/include/timing_spots.h:89`)
- `WriteTupleToLocal_` (`src/include/timing_spots.h:90`)
- `DoLocalCopy_` (`src/include/timing_spots.h:91`)

Cross-link (building block):

- PostgreSQL COPY instrumentation in this repo: `docs/kb/postgres/copy/copy_path_timing.md`
- Citus distribution/placement COPY receiver: `docs/kb/citus/intermediate-results/push_pull_intermediate_results.md`

## In-query tuple movement

In-query movement directly impacts end-to-end query latency because tuples are produced during execution.

### Push-based shuffle (gather -> scatter via coordinator)

Coordinator gathers tuples, decides placement, and sends tuples out to workers.

This characterization is directionally correct, but the **source** of the tuple stream can vary:

- Sometimes tuples are produced on the coordinator (local plan execution).
- Sometimes the coordinator first gathers tuples from workers, then re-broadcasts.
- The common mechanism is: **coordinator broadcasts an intermediate result stream** using `RemoteFileDestReceiver` and a special COPY “format result” protocol.

Concrete Citus code path for push-based intermediate results (DestReceiver broadcast):

- `RemoteFileDestReceiverReceive(...)` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:404` times serialize/send as:
  - `RemoteFileDestReceiver_SerAndSend` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:409` … `:475`)
  - Verbose split: `RemoteFileDestReceiver_Ser` (`:431` … `:447`) and `RemoteFileDestReceiver_Send` (`:450` … `:455`)

Concrete Citus code path for push-based repartitioned INSERT..SELECT intermediate results:

- `ExecutePlanIntoColocatedIntermediateResults(...)` times:
  - `ExecutePlanIntoColocatedIntermediateResults_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:347` … `:382`)
  - `ExecutePlanIntoDestReceiver_` around `ExecutePlanIntoDestReceiver(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:374` … `:376`)

Intended timing spots (push execution + receiver-side copy write):

- Push execution (produce intermediate results into a dest receiver):
  - `ExecutePlanIntoColocatedIntermediateResults_` (`src/include/timing_spots.h:104`)
  - `ExecutePlanIntoDestReceiver_` (`src/include/timing_spots.h:105`)
- Receiver-side COPY ingestion (worker receives tuples and writes intermediate file):
  - `ReceiveAndWriteCopyData_` (`src/include/timing_spots.h:97`)
  - `ReceiveAndWriteCopyData_Deser` (`src/include/timing_spots.h:98`)
  - `ReceiveAndWriteCopyData_WriteFile` (`src/include/timing_spots.h:99`)

### Pull-based shuffle (worker -> worker)

Workers fetch intermediate files directly from other workers (avoid coordinator data plane).

Concrete Citus code path:

- Distributed intermediate results are colocated by issuing SQL tasks that call `fetch_intermediate_results()` between nodes:
  - `ColocateFragmentsWithRelation(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:472` … `:488`)
  - Query is constructed by `QueryStringForFragmentsTransfer(...)` to run on the *target* node and pull from the *source* node (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:608` … `:659`)

Concrete timing spots for the pull path:

- `FetchIntermediate_` wraps `fetch_intermediate_results(PG_FUNCTION_ARGS)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926` … `:988`).
- `FetchIntermediate_CopyAndWrite` wraps one “COPY chunk” attempt (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1051` … `:1055`).
- `PG_WAIT` wraps `WaitLatchOrSocket(...)` while waiting for socket readability (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1070` … `:1074`).
- `FetchIntermediate_FileWrite` wraps `FileWriteCompat(...)` per received COPY chunk (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1123` … `:1135`).

Intended timing spots (fetch side + send side) from `timing_spots.h`:

- Fetch side:
  - `FetchIntermediate_` (`src/include/timing_spots.h:93`)
  - `FetchIntermediate_CopyAndWrite` (`src/include/timing_spots.h:94`)
  - `FetchIntermediate_FileWrite` (`src/include/timing_spots.h:95`)
- Send side (COPY-based transfer):
  - `SendViaCopy_` (`src/include/timing_spots.h:100`)
  - `SendViaCopy_Send` (`src/include/timing_spots.h:101`)
  - `SendViaCopy_FileRead` (`src/include/timing_spots.h:102`)

## Portal execution / result streaming

PostgreSQL portal execution streams output tuples to a `DestReceiver`.

In Citus, the worker’s `DestReceiver` is effectively the coordinator (which then returns to the SQL client).

Concrete Citus code path:

- Adaptive executor coordinator receive loop: `ReceiveResults(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3995` (see `docs/kb/citus/executor/receive_results_streaming.md`).
  - Definition line is currently `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3996`.

## Recommended workflow docs

This file is the conceptual index. For concrete step-by-step coordinator/worker workflows, see:

- `off_path_bulk_copy_workflow.md` (off-path distribution via heap scan + COPY transport)
- `in_query_push_intermediate_results_workflow.md` (push/broadcast intermediate results)
- `in_query_pull_intermediate_results_workflow.md` (pull/fetch intermediate results)
- `portal_result_streaming_workflow.md` (worker -> coordinator streaming results)
- `off_path_reference_replication_and_rebalance.md` (reference table replication + shard transfer/rebalance flows)

Timing spots used:

- Worker -> coordinator receive path:
  - `ReceiveResults_` (`src/include/timing_spots.h:107`)
  - `ReceiveResults_Net` (`src/include/timing_spots.h:108`)
  - `ReceiveResults_Deserialize` (`src/include/timing_spots.h:109`)
  - `ReceiveResults_BuildTuples` (`src/include/timing_spots.h:110`)
  - `ReceiveResults_HeapFormTuple` (`src/include/timing_spots.h:111`)

In this repo snapshot, `ReceiveResults_HeapFormTuple` is wired around `heap_form_tuple()` in `BuildTupleFromCStrings()` (`src/backend/executor/execTuples.c:2262`–`src/backend/executor/execTuples.c:2268`). This provides a baseline for tuple materialization cost even before the rest of the `ReceiveResults_*` stack is wired.

In Citus, `ReceiveResults_HeapFormTuple` is also used for the binary result path:

- `BuildTupleFromBytes(...)` wraps `heap_form_tuple(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:119` … `:125`).
