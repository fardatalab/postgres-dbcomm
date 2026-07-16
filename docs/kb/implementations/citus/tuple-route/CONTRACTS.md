# Tuple Route — Contracts And Invariants

<!-- kb-summary: Current borrowed-tuple and receive-record lifetime contracts for the tuple-view queue substrate. -->

> The neighboring checkpoint is historical; this index records only current queue/frontend behavior verified from the renamed Homer tuple-view implementation.

## Symbol entries

### Borrowed tuple view — Datums may point into the current receive record

- **MEANS:** Caller-owned `Datum` and null arrays may contain pass-by-reference Datums pointing into the currently borrowed receive record.
- **DOES NOT MEAN:** Borrowing materializes independently owned Datums, permits a second borrow from the same batch, or extends record lifetime beyond explicit release.
- **CONTRACT / INVARIANT:** At most one tuple is borrowed per batch. Keep the borrow live through every executor use, then explicitly release it before borrowing another tuple or releasing the batch.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** decode/borrow construction in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_view_codec.c:437`; frontend borrow/release state in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:785` and `:2437`; worker insert use/release order in `/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:370`.

## Control-flow and lifecycle contracts

### Receive records are released in byte-ring FIFO order

- **ORDER:** Release the active tuple borrow, then release the batch only when the shared `consumedHead` equals that batch's `recordStart`; advance it exactly to `recordTail` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:2377`).
- **WHAT BREAKS IF REORDERED:** Releasing a later record first makes still-borrowed or earlier record bytes reusable and permits producer overwrite while consumers retain pointers into them.
- **OWNERS:** The receive batch owns the record interval; the byte ring owns the monotone FIFO `consumedHead`; cleanup may clear a leaked borrow but may not reorder record retirement.
- **TEMPTING WRONG MOVE:** A receive batch is not an independently releasable heap wrapper—the release publishes shared storage reuse.

## Related

- [local_batch_materialization_checkpoint.md](local_batch_materialization_checkpoint.md)
- [../connection-management/CONTRACTS.md](../connection-management/CONTRACTS.md)
