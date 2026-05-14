# Client SQL Session Implementations

## Purpose

Track the implemented client SQL session prototype that runs a pgbench-like foreground transaction workload through Homer typed commands instead of libpq workload traffic.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [client_sql_session_pgbench_checkpoint.md](client_sql_session_pgbench_checkpoint.md) - TODO
<!-- kb-docs:end -->

## Related

- `../../../future-directions/postgres/client-sql-session/client_sql_session_offload_plan.md`: Target plan that this checkpoint advances.
- `../../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`: Shared command/completion plane design reused by the client SQL session.
