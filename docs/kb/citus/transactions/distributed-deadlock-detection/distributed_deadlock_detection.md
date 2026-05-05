# Citus distributed deadlock detection

## Scope

- **What this doc explains**: How Citus detects distributed deadlocks, what data it relies on beyond `BackendData`, and how backend-level wait information is lifted to distributed transactions.
- **What this doc does NOT cover**: A proposal to redesign deadlock detection or a full walkthrough of PostgreSQL's local deadlock detector.
- **Primary directory**: `docs/kb/citus/transactions/distributed-deadlock-detection/`

## Why this exists (context)

Distributed deadlock detection is a concrete example of why Citus treats distributed transaction identity as a cross-backend coordination primitive rather than as storage-only metadata. The same per-backend distributed transaction tagging that shapes intermediate-result lifetime is also used to build cluster-wide wait graphs and cancel a victim transaction.

## Key code pointers

- [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L105) - entry point for detecting a distributed deadlock and cancelling a victim.
- [`BuildGlobalWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L141) - builds the combined local+remote wait graph.
- [`AddEdgesForLockWaits()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L826) - scans PostgreSQL lock holders that block a waiting backend.
- [`AddEdgesForWaitQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L861) - scans wait-queue ordering edges that can also matter for deadlocks.
- [`AddWaitEdge()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L911) - annotates backend waits with distributed transaction identity from `BackendData`.
- [`BuildAdjacencyListsForWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L446) - collapses backend-level wait edges into a graph keyed by `DistributedTransactionId`.
- [`AssociateDistributedTransactionWithBackendProc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L56) - maps a distributed transaction node back to a live backend process to cancel.
- PostgreSQL `PGPROC` wait state fields in [`proc.h`](/data/dbcomm/postgres-citus/src/include/storage/proc.h#L233) - local wait metadata that Citus reads indirectly through lock-graph construction.
- PostgreSQL local lock wait / wakeup path in [`ProcSleep()`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/proc.c#L1071) and [`CheckDeadLock()`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/proc.c#L1759) - local deadlock/background wait handling that coexists with Citus' distributed detector.

## Current behavior / grounded findings

- Citus runs distributed deadlock detection periodically in the maintenance daemon by calling [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c#L709).
- The detector only considers distributed transactions when invoked in its normal mode via `onlyDistributedTx = true` in [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L123) and [`BuildGlobalWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L149).
- `BackendData` is necessary but not sufficient. It supplies the distributed transaction identity attached to each backend, while the actual blocking relationships come from PostgreSQL lock-manager state and remote wait-edge RPCs.
- Victim selection is deterministic and timestamp-based: [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L170) chooses the youngest alive distributed transaction initiated by the local node and cancels its initiator process.

## Architecture / data flow

- **Entry point**: [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L105)
- **Local wait extraction**:
  - PostgreSQL lock-manager structures are walked in [`AddEdgesForLockWaits()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L826) and [`AddEdgesForWaitQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L861)
  - backend-level waits are converted to `WaitEdge`s in [`AddWaitEdge()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L911)
- **Remote wait extraction**:
  - [`BuildGlobalWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L141) opens internal connections to other nodes and queries `dump_local_wait_edges()`
- **Graph reduction**:
  - [`BuildAdjacencyListsForWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L446) groups wait edges by `DistributedTransactionId`
- **Cycle detection**:
  - DFS in [`CheckDeadlockForTransactionNode()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L235)
- **Victim selection and action**:
  - map back to a live proc with [`AssociateDistributedTransactionWithBackendProc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L56)
  - cancel with [`CancelTransactionDueToDeadlock()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L211)

## Behavior details

### What `BackendData` contributes

[`AddWaitEdge()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L911) reads `BackendData` for the waiting and blocking backends and attaches:

- `initiatorNodeIdentifier`
- `transactionNumber`
- `timestamp`

Without that mapping, Citus would only have a backend-to-backend wait graph, not a distributed-transaction-to-distributed-transaction graph.

### What other data the detector needs

The detector also depends on:

- PostgreSQL lock holder / waiter state via `PGPROC`, `LOCK`, and wait queues in [`lock_graph.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L826)
- remote wait-edge dumps collected by [`BuildGlobalWaitGraph()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/lock_graph.c#L175)
- transaction timestamps for youngest-victim choice in [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L199)
- `pgstat` current-query lookup for debug logging in [`LogTransactionNode()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L613)

### Relationship to PostgreSQL local deadlock handling

Citus does not replace PostgreSQL's local deadlock detector. PostgreSQL still handles local wait/deadlock behavior in the regular lock manager, for example through [`CheckDeadLock()`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/proc.c#L1759) and [`DeadLockCheck()`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/deadlock.c#L217). Citus adds a cluster-wide layer on top by combining local wait graphs from multiple nodes.

## Invariants and assumptions

- Only distributed transactions are included in the normal deadlock-detection path.
- The same distributed transaction identity can be attached to multiple backend processes across the cluster, and that is intentional.
- Victim cancellation is done from the initiator node's perspective; the detector skips candidate transaction nodes not initiated by the local node in [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L145).
- The distributed transaction identity is used as a coordination primitive across Citus, not just for intermediate-result namespacing.

## Interactions and cross-links

- **Distributed transaction identity**: [`AssignDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L855), [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L145)
- **Intermediate-result lifetime on the same distributed transaction scope**: [`why_file_backed_intermediate_results.md`](../../intermediate-results/why_file_backed_intermediate_results.md)
- **Maintenance daemon**: [`maintenanced.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/maintenanced.c#L685)
- **PostgreSQL lock-manager background**: [`proc.h`](/data/dbcomm/postgres-citus/src/include/storage/proc.h#L233), [`proc.c`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/proc.c#L1071), [`deadlock.c`](/data/dbcomm/postgres-citus/src/backend/storage/lmgr/deadlock.c#L217)

## Open questions / TODO

- Whether a future redesign should keep using distributed transaction id as the graph key, or move more of this logic toward global PID, remains an open Citus direction noted in the README.
