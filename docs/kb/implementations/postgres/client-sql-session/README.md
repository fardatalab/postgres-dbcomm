# Client SQL Session Implementations

<!-- kb-summary: Current Homer client-SQL session implementation checkpoints and result/completion contracts. -->

## Purpose

Track the implemented client SQL session prototype that runs a pgbench-like foreground transaction workload through Homer typed commands instead of libpq workload traffic.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Current result-sink ownership and ordered completion-mailbox contracts for Homer client SQL sessions.
- [client_sql_session_pgbench_checkpoint.md](client_sql_session_pgbench_checkpoint.md) - Current implementation state, validation evidence, and milestone history for integrated pgbench Homer client-SQL sessions.
<!-- kb-docs:end -->

## Related

- `../../../future-directions/postgres/client-sql-session/client_sql_session_offload_plan.md`: Target plan that this checkpoint advances.
- `../../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`: Shared command/completion plane design reused by the client SQL session.
