# Query lifecycle

## Purpose

Document the backend “message loop” and how query execution is timed, including end-of-command protocol messages.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [execsimplequery_and_message_loop.md](execsimplequery_and_message_loop.md) - `PostgresMain` query-cycle boundary, `QUERY_ACTIVE_WALL`, and legacy `ExecSimpleQuery` context.
<!-- kb-docs:end -->

## Related

- `../../instrumentation/timing-spots/timing_spots.md`: Full spot catalog.
- `../libpq-io/`: IO wait/read/write timing that often dominates end-to-end latency.
