# Client SQL Session Future Directions

## Purpose

Index future-direction notes for replacing PostgreSQL frontend/libpq transaction traffic with the external Homer service and typed database-semantic command objects.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [client_sql_session_offload_plan.md](client_sql_session_offload_plan.md) - Plan and status for `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`: the single-client persistent-session `homer_pgbench` checkpoint is complete, while sink-backed result delivery, dynamic session state, prepared mode, and concurrent clients remain future work.
- [concurrent_pgbench_client_sessions.md](concurrent_pgbench_client_sessions.md) - Root-cause note and required design work for lifting the current one-client Homer pgbench guard: dynamic session ownership plus concurrency-safe peer control.
- [pgbench_homer_integration_plan.md](pgbench_homer_integration_plan.md) - Opt-in pgbench integration plan for Homer persistent single-client simple mode, preserving existing libpq modes for baseline comparison.
<!-- kb-docs:end -->

## Related

- `../../../postgres/query-lifecycle/`: Grounded PostgreSQL backend command-loop behavior.
- `../../../postgres/libpq-io/`: Grounded frontend/backend socket and libpq IO behavior being bypassed for this prototype.
- `../../../implementations/postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md`: Implementation checkpoint for the completed single-client `homer_pgbench` path.
- `../../citus/connection-management/remote_execution_session_control_plane.md`: Existing remote-execution session abstraction that this client-session design extends.
- `../../citus/data-movement/command_dispatch_completion_plane.md`: Existing command dispatch/completion design that already carries typed lifecycle and SQL command commands.
