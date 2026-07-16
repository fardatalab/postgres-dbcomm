# Client SQL Session — Contracts And Invariants

<!-- kb-summary: Current result-sink ownership and ordered completion-mailbox contracts for Homer client SQL sessions. -->

> These entries describe the current session-owned result path, not the older command-local sink lifecycle retained in historical parts of the checkpoint note.

## Symbol entries

### `RemoteExecBackendSessionState.resultSinkHandle` — persistent session-owned result resources

- **MEANS:** The socketless backend session owns and may reuse the result send handle, queue mapping, copy slot, and copied `TupleDesc` across compatible commands.
- **DOES NOT MEAN:** A command-local `DestReceiver` owns or closes those resources at `rShutdown` or `rDestroy`.
- **CONTRACT / INVARIANT:** `rShutdown` flushes the command's terminal result and drops only the receiver's borrowed pointer; `rDestroy` drops receiver-local references. Session cleanup closes the handle/mapping and destroys the copy slot and descriptor.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** session result-resource cleanup in `RemoteExecCleanupSessionResultQueue()` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1186`; receiver shutdown and destroy in `RemoteExecSqlDestShutdown()` at `:1635` and `RemoteExecSqlDestDestroy()` at `:1660`.

### Eight-slot completion mailboxes — ordered SPSC state publication, not command concurrency

- **MEANS:** One command may expose multiple ordered completion states; the eight-slot rings prevent a later state from overwriting an unread earlier state.
- **DOES NOT MEAN:** Eight slots authorize eight pipelined commands. The current frontend still has one command in flight per session, and frontend, backend-to-service, and peer completion rings are distinct objects.
- **CONTRACT / INVARIANT:** Producers fill the completion image before publishing its epoch/ready word with release ordering; consumers gate visibility on that word. A full ring waits rather than overwriting unread state.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** frontend completion ABI in `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:412`; backend mailbox ABI in `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:128` and `:385`; backend publication in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1810`; client consumption in `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1429`.

## Related

- [client_sql_session_pgbench_checkpoint.md](client_sql_session_pgbench_checkpoint.md)
- [../CONTRACTS.md](../CONTRACTS.md)
