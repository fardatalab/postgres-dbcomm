# Backend message loop and query-cycle timing

## Scope

This note explains how the top-level query-cycle denominator is now formed in
`PostgresMain()` (`src/backend/tcop/postgres.c:4832`), how it differs from the
older `ExecSimpleQuery` spot, and why `PG_WAIT_DONT_COUNT` is now legacy-only.

Key spots in `src/include/timing_spots.h`:

- `QUERY_ACTIVE_WALL`
- `QUERY_WAIT_WALL`
- `ExecSimpleQuery`
- `PG_WAIT_DONT_COUNT`
- external perf FIFO control in `src/backend/tcop/postgres.c`

## Main loop boundary in `PostgresMain()`

The important shared end point is the `send_ready_for_query` block in
`PostgresMain()` (`src/backend/tcop/postgres.c:4881`).

- `ReadyForQuery(whereToSendOutput)` runs at `src/backend/tcop/postgres.c:4972`.
- `FinishQueryTimingCycle()` now runs immediately afterwards at
  `src/backend/tcop/postgres.c:4986`.
- That helper stops `QUERY_ACTIVE_WALL`, then either calls
  `logger_print_timings()` or `logger_reset()` in
  `src/backend/tcop/postgres.c:771` and `src/backend/tcop/postgres.c:767`.

This replaces the older behavior where `logger_print_timings()` ran at the end
of the command switch in `PostgresMain()` (`src/backend/tcop/postgres.c:5262`
before this change). The printed timing block is now aligned to the actual
query-cycle end instead of ending before the later `ReadyForQuery()` flush.

## Query-cycle start points

`BeginQueryTimingCycle()` in `PostgresMain()` (`src/backend/tcop/postgres.c:721`)
opens `QUERY_ACTIVE_WALL` only once per logical query cycle.

It is called from:

- `PqMsg_Query` at `src/backend/tcop/postgres.c:5069`
- `PqMsg_Parse` at `src/backend/tcop/postgres.c:5132`
- `PqMsg_Bind` at `src/backend/tcop/postgres.c:5162`
- `PqMsg_Execute` at `src/backend/tcop/postgres.c:5183`
- `PqMsg_FunctionCall` at `src/backend/tcop/postgres.c:5208`
- `PqMsg_Describe` at `src/backend/tcop/postgres.c:5297`

This matters for worker backends and any extended-protocol path because one
logical query can span multiple protocol messages and only ends later when
`PqMsg_Sync` sets `send_ready_for_query = true` at
`src/backend/tcop/postgres.c:5333`.

## FIFO perf control

The external perf FIFO control now follows the same logical query-cycle
boundary instead of the older simple-query-only bracket.

- `PerfControlEnableIfNeeded()` lives in `src/backend/tcop/postgres.c:623`.
- `BeginQueryTimingCycle()` now calls it at
  `src/backend/tcop/postgres.c:729`.
- `PerfControlDisableIfNeeded()` lives in
  `src/backend/tcop/postgres.c:655`.
- `FinishQueryTimingCycle()` now calls it at
  `src/backend/tcop/postgres.c:764`.
- The actual FIFO path is the hardcoded `PERF_CONTROL_FIFO_PATH` at
  `src/backend/tcop/postgres.c:88`.

### Concurrency semantics

Because the FIFO controls a node-wide `perf record -a` session, a backend-local
flag is not sufficient once multiple backends overlap.

- Backends now coordinate through the shared logger state in
  `src/common/time_instr.c:96`.
- `logger_perf_query_cycle_enter()` /
  `logger_perf_query_cycle_exit()` maintain the shared
  `perf_active_query_cycles` refcount in
  `src/common/time_instr.c:385` and `src/common/time_instr.c:412`.
- The first backend entering a logical query cycle enables perf, and the last
  backend leaving disables it.

That makes the perf window faithful to the union of active logical query cycles
on the node. It does **not** provide per-query attribution once multiple
queries overlap: under concurrency, the perf data is node-aggregate, not tied
to one backend's `QUERY_ACTIVE_WALL`.

## Denominator timers

### `QUERY_ACTIVE_WALL`

`QUERY_ACTIVE_WALL` is the primary denominator for communication-stack
percentages.

- It starts in `logger_query_active_wall_start()` at
  `src/common/time_instr.c:293`.
- It stops in `logger_query_active_wall_stop()` at
  `src/common/time_instr.c:307`.
- It spans the logical query cycle on this backend and is paused around waits.
- It intentionally includes the end-of-cycle tail up to the shared
  `ReadyForQuery()` boundary, not just executor-core work.

### `QUERY_WAIT_WALL`

`QUERY_WAIT_WALL` is the matching excluded-wait bucket.

- Wait exclusion is driven centrally by
  `pgstat_report_wait_start()` / `pgstat_report_wait_end()` in
  `src/include/utils/wait_event.h:87` and `src/include/utils/wait_event.h:111`.
- The active-wall pause/resume helpers live in
  `logger_query_active_wall_pause_wait()` /
  `logger_query_active_wall_resume_wait()` in
  `src/common/time_instr.c:333` and `src/common/time_instr.c:349`.
- FE-libpq waits are the one missing class from PostgreSQL's backend wait
  backbone, so `PQsocketPoll()` explicitly pauses/resumes the active wall at
  `src/interfaces/libpq/fe-misc.c:1222`, `src/interfaces/libpq/fe-misc.c:1252`,
  `src/interfaces/libpq/fe-misc.c:1270`, and `src/interfaces/libpq/fe-misc.c:1311`.

## Legacy spots

### `ExecSimpleQuery`

`ExecSimpleQuery` is still emitted in `PostgresMain()`:

- simple protocol around `exec_simple_query(query_string)` at
  `src/backend/tcop/postgres.c:5099` … `src/backend/tcop/postgres.c:5110`
- execute protocol around `exec_execute_message(portal_name, max_rows)` at
  `src/backend/tcop/postgres.c:5194` … `src/backend/tcop/postgres.c:5196`

Use it only as a legacy statement-core context timer. It is narrower than the
new query-cycle denominator because it does not include the full protocol cycle.

### `PG_WAIT_DONT_COUNT`

`PG_WAIT_DONT_COUNT` is now legacy-only and should be ignored by the aggregation
scripts for primary analysis.

- The only remaining instrumentation sites are still nested under `PG_WAIT` in
  `secure_read()` / `secure_write()` at
  `src/backend/libpq/be-secure.c:224`, `src/backend/libpq/be-secure.c:230`,
  `src/backend/libpq/be-secure.c:368`, and `src/backend/libpq/be-secure.c:374`.
- But `PostgresMain()` no longer raises `pg_wait_dont_count_active`; the old
  toggles around `ReadCommand()` and `ReadyForQuery()` were removed in favor of
  the active-wall denominator.

So `PG_WAIT_DONT_COUNT` remains for compatibility with older logs, not as an
active part of the new analysis model.

## Analysis guidance

Use the scripts as follows:

- `aggregate_timing.py` sums timing blocks for one isolated query window and
  reports `query_active_wall_ns` plus communication buckets.
- `aggregate_transaction_timing.py` sums `QUERY_ACTIVE_WALL` across all
  statements in the explicit transaction and uses that as the active-execution
  denominator for transaction-level percentages.

Do not use `ExecSimpleQuery` or `PG_WAIT_DONT_COUNT` as the main denominator
anymore.

For perf-derived kernel-network measurements:

- Use isolated one-query/one-transaction-at-a-time runs on a node when you want
  to divide by one backend's `query_active_wall_ns`.
- If you intentionally run overlapping work, treat the perf result as a
  node-level aggregate over the union window instead.
- `fdl_utils/sum_net.py` now uses `query_active_wall_ns` as the optional
  denominator input and reports `kernel_net_avg_cores` plus
  `kernel_net_pct_of_query_active_wall`; the older sampled-kernel fraction is
  now only an explicit debug output.
- The denominator must come from the same perf-enabled run on the same node.
  Do not mix `perf.txt` from one run with `query_active_wall_ns` aggregated from
  a different baseline run.
