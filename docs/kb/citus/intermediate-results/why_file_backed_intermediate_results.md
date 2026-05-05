# Why Citus intermediate results are file-backed

## Scope

- **Question**: why does Citus write intermediate results to files for broadcast / repartition / shuffle instead of just keeping them in memory?
- **Short answer**: the file is not just an implementation detail for buffering. In current Citus, it is the materialization boundary and naming contract between separate executor phases, separate backends, and separate nodes.

## Short answer

Citus does not treat intermediate results as a transient in-process buffer. It treats them as **named, transaction-scoped, COPY-formatted objects** that can be:

- produced by one query/executor phase,
- moved to other nodes later,
- reopened by later SQL execution through `read_intermediate_result(...)`,
- costed by the planner using file size,
- cleaned up with distributed-transaction lifetime rules.

That contract is implemented with regular files under the per-transaction intermediate-results directory returned by [`IntermediateResultsDirectory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L720).

## What the code actually does today

### 1. Producers serialize tuples into COPY bytes, not tuple-memory images

[`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405) serializes each slot with [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467). The resulting bytes are either:

- broadcast to nodes with [`BroadcastCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L552), or
- appended locally with [`WriteToLocalFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L488).

For repartitioning, [`worker_partition_query_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/partitioned_intermediate_results.c#L116) creates one file-backed dest receiver per target partition via [`CreateFileDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/worker_sql_task_protocol.c#L92), and those receivers append COPY rows to per-partition files in [`TaskFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/worker_sql_task_protocol.c#L168).

### 2. Receivers often forward bytes opaquely instead of rebuilding tuples immediately

On the push side, a worker receiving `COPY "<resultId>" FROM STDIN WITH (format result)` lands in [`ReceiveQueryResultViaCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L623), which calls [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51).

That receive path does **not** deserialize tuples. [`ReceiveCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L298) extracts `CopyData` payloads and [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51) appends the opaque bytes to disk.

On the pull side, [`fetch_intermediate_results()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L926) issues `COPY "<resultId>" TO STDOUT WITH (format result)` in [`FetchRemoteIntermediateResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1003). [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100) repeatedly calls `PQgetCopyData()` and writes each returned chunk to the destination file.

### 3. Consumers reopen the named result later through SQL

Later execution reads the result through [`read_intermediate_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L825) or [`read_intermediate_result_array()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L848). These call [`ReadIntermediateResultsIntoFuncOutput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L875), which locates the file by result id and calls [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447).

[`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447) re-parses the file with PostgreSQL COPY APIs [`BeginCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L471) and [`NextCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L480), then stores rows into a tuplestore with [`tuplestore_putvalues()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L488).

### 4. The per-transaction namespace is shared across backend processes

This is a key grounded fact for any redesign: the storage scope is not just "this executor invocation" and not even just "this backend process".

- The transaction namespace is derived from the distributed transaction identity in [`IntermediateResultsDirectory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L720), using `initiatorNodeIdentifier` and `transactionNumber`, rather than always using `MyProcPid`.
- The coordinator assigns a distributed transaction identity locally in [`AssignDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L855).
- Remote worker backends join that same identity through [`BeginAndSetDistributedTransactionIdCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L321), [`AssignDistributedTransactionIdCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L407), and [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L145).
- The Citus README explicitly describes distributed transaction ids as applying to **all backends running distributed transactions** in [`README.md`](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md#L2147).
- Parallel access is defined in terms of multiple connections to a worker node in [`adaptive_executor.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1368), and placement handling explicitly discusses a placement being read over multiple connections in [`placement_connection.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L568).

So the current file-backed namespace is genuinely a **cross-backend, per-node, distributed-transaction-scoped** storage area.

### 5. Missing-file and cleanup behavior already assume cross-backend races

The current code does not merely tolerate cross-backend participation; it has explicit behavior for it:

- [`ReadIntermediateResultsIntoFuncOutput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L875) warns that a file may be missing because another backend in the same distributed transaction already rolled back and removed it.
- [`RemoveIntermediateResultsDirectories()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L748) renames the shared directory before deleting it precisely because another backend might still be writing into it.
- [`worker_log_messages.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/worker_log_messages.c#L41) documents warnings that arrive from one session because another session in the same distributed transaction threw an error.

These are important invariants to preserve if storage stops being regular files.

### 6. Distributed deadlock detection relies on the same multi-backend transaction identity

Intermediate results themselves are not what deadlock detection is about, but the same distributed-transaction identity model is part of the reason cross-backend transaction scoping matters.

- Distributed deadlock detection is run periodically by the maintenance daemon in [`maintenanced.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c#L685) by calling [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L105).
- [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L105) builds a global wait graph through [`BuildGlobalWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L141).
- Local wait edges come from PostgreSQL lock manager state through [`AddEdgesForLockWaits()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L826) and [`AddEdgesForWaitQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L861).
- [`AddWaitEdge()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L911) uses `BackendData` to attach distributed-transaction identity to waiting and blocking processes.
- [`BuildAdjacencyListsForWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L446) collapses those backend-level edges into a graph keyed by `DistributedTransactionId`.

The relevant point for intermediate-result redesign is that the distributed transaction identity is not just bookkeeping for storage. It is an active coordination primitive used by other Citus subsystems too.

## Why files are the contract boundary instead of memory

### 1. Producer and consumer are deliberately decoupled in time and execution phase

The strongest reason is architectural: production and consumption are not a single streaming operator pipeline.

- Recursive subplans are executed first in [`ExecuteSubPlans()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/subplan_execution.c#L38), broadcast via [`CreateRemoteFileDestReceiver()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L219), and only later read by rewritten queries that call `read_intermediate_result(...)`.
- Repartitioning first materializes partitioned fragments on source nodes in [`worker_partition_query_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/partitioned_intermediate_results.c#L116), then later creates node-to-node fetch tasks in [`FragmentTransferTaskList()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c#L575) using [`QueryStringForFragmentsTransfer()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c#L609), and only after that does the final shard query read the colocated results.
- Repartitioned `INSERT .. SELECT` / `MERGE` explicitly build a later SQL phase over those named results in [`GenerateTaskListWithColocatedIntermediateResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/repartition_executor.c#L145), which swaps in [`BuildSubPlanResultQuery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/recursive_planning.c#L2311).

An in-memory buffer local to the producing backend would not survive this separation naturally. The later consumer is often a different SQL task, possibly on a different node, and conceptually a different executor phase.

### 2. The current API is by `resultId` and file lookup, not by in-memory object handle

The public/internal contract is “a named result exists for this distributed transaction”:

- filename resolution is done by [`QueryResultFileName()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L675),
- directories are transaction-scoped via [`IntermediateResultsDirectory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L720),
- transaction cleanup is handled by [`RemoveIntermediateResultsDirectories()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L748).

Many code paths assume this file identity directly:

- [`FetchRemoteIntermediateResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1003) checks `stat()` on the local file and skips work if the file already exists on the target node.
- [`ReadIntermediateResultsIntoFuncOutput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L875) warns if the file is missing, because missing-file behavior is part of transaction/failure handling.
- empty result files are intentionally created in [`RemoteFileDestReceiverShutdown()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L507) because downstream execution expects the named result to exist even when it has zero rows.

So replacing files with memory would not be a small storage optimization. It would require a different cross-phase object model and new lookup/lifetime/refcount semantics.

### 3. Files give bounded memory usage on paths that can be very large

This is not just about convenience. Several of these paths can materialize large result sets:

- the distributed README explicitly notes that re-partitioned `INSERT .. SELECT` is a high-throughput path and uses intermediate files as the staging object ([`README.md`](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md#L1718));
- subplans have a dedicated size guard via [`EnsureIntermediateSizeLimitNotExceeded()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/tuple_destination.c#L139) and `citus.max_intermediate_result_size`.

The implementation already uses bounded buffers around the file boundary:

- push-side broadcast serializes one row into a reusable `StringInfo` in [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405),
- push-side receiver writes each incoming `CopyData` chunk to disk in [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51),
- pull-side fetch writes each `PQgetCopyData()` chunk to disk in [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100),
- repartition file receivers flush their `StringInfo` after it crosses `COPY_BUFFER_SIZE` in [`TaskFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/worker_sql_task_protocol.c#L191).

A purely memory-backed design would need some other spill mechanism anyway, or it would make these paths vulnerable to unbounded resident memory growth.

### 4. Files are the simplest cross-node relocation unit for repartitioning

For repartitioning, the system does not keep one giant in-memory hash table and directly route every tuple to its final consumer query. Instead it:

1. partitions rows into named per-target files on the source node in [`worker_partition_query_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/partitioned_intermediate_results.c#L116),
2. groups transfers by source/target node pair in [`ColocationTransfers()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c#L504),
3. fetches batches of fragment files with one `fetch_intermediate_results(...)` task built by [`QueryStringForFragmentsTransfer()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c#L609),
4. executes the final shard-local query over `read_intermediate_result(...)`.

That is operationally much simpler with files because the transfer unit is already a named byte stream on the source and destination nodes. A memory-only design would need a distributed memory exchange protocol plus late consumer attachment semantics.

### 5. Planner and executor already rely on file metadata

The planner estimates `read_intermediate_result(...)` cost from file size:

- [`AdjustReadIntermediateResultCost()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/distributed_planner.c#L2137)
- [`AdjustReadIntermediateResultsCostInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/planner/distributed_planner.c#L2244)
- [`IntermediateResultSize()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L794)

That is another sign the file is part of the real execution model, not merely a temporary buffer chosen by one implementation.

## Important nuance: file-backed does not mean “never uses memory”

The current system still uses memory buffers on both sides:

- `StringInfo` serialization buffers in [`RemoteFileDestReceiverStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L260) and [`TaskFileDestReceiverStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/worker_sql_task_protocol.c#L120),
- libpq buffers and `PQgetCopyData()` allocations in [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100),
- tuplestores when reading results in [`ReadIntermediateResultsIntoFuncOutput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L875) and [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447).

The file layer sits between these memory stages as the durable materialization point.

## Why “just keep it in memory” is not a drop-in alternative

A true in-memory redesign would need at least:

- a distributed, transaction-scoped registry keyed by `resultId`,
- cross-backend and cross-node access to those objects,
- refcounting / cleanup rules matching the current directory lifecycle,
- explicit spill behavior once results exceed memory budgets,
- a new way for `read_intermediate_result(...)` and planner costing to reason about size and existence.

Without that, “keep it in memory” would only work for the narrow case where the producer and consumer are in the same backend, same phase, and small enough to fit comfortably in RAM. That is not the contract Citus currently implements.

## Related future directions

Substantial exploratory design notes now live in the top-level future-direction tree at [`intermediate_results_service_future_directions.md`](../../future-directions/citus/intermediate-results/intermediate_results_service_future_directions.md), so the factual intermediate-results index does not mix grounded behavior with open design discussion.

## Current downside in the code

There is a real cost to this design: current reads materialize through a tuplestore. The distributed README explicitly calls out that [`read_intermediate_result(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L825) first copies tuples into a tuplestore and that this may itself spill to disk ([`README.md`](/data/dbcomm/citus-dbcomm/src/backend/distributed/README.md#L1665)). So the current design favors robust decoupling and bounded memory over an all-streaming path.

## Bottom line

The main reason is **not** merely “shuffle needs files”. The stronger reason is that Citus currently models intermediate results as **named, transaction-scoped materialized COPY streams** that can be produced now, moved later, and read again by later SQL tasks. Files are the simplest mechanism that satisfies those semantics while keeping memory bounded.
