# Client SQL Session Implementations

## Purpose

Track the implemented client SQL session prototype that runs a pgbench-like foreground transaction workload through Homer typed commands instead of libpq workload traffic.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [client_sql_session_pgbench_checkpoint.md](client_sql_session_pgbench_checkpoint.md): Current implementation checkpoint for the single-client `pgbench --homer` path, sink-backed results, persistent `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`, and benchmark placement knobs.
<!-- kb-docs:end -->

## Related

- `../../../future-directions/postgres/client-sql-session/client_sql_session_offload_plan.md`: Target plan that this checkpoint advances.
- `../../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`: Shared command/completion plane design reused by the client SQL session.
