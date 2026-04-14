# Ordered Latency CSV Parser

[`parse_latency_trace_csv.py`](./parse_latency_trace_csv.py) converts the ordered latency trace blocks emitted by [`latency_instr.c`](../src/common/latency_instr.c) into one CSV row per coordinator transaction/query-cycle trace. When worker logs are also provided, it joins worker timing reports and worker ordered traces back into each coordinator row using the propagated `rsid` / `rcid` / `kind` tags plus timestamp proximity.

The parser expects collector-backed PostgreSQL logs. In this cluster, the benchmark runbook archives every collector segment produced during one benchmark row with [`scripts/archive_server_logs.py`](../scripts/archive_server_logs.py), stitches those segments into one logical `node-X.log` per server, and uses [`scripts/reset_server_logs.py`](../scripts/reset_server_logs.py) plus explicit row-start / row-end LOG markers to bracket each row. We intentionally no longer archive only the newest segment, because the earliest traced transactions in the row can otherwise lose worker `tx_begin` / `task_query` records when the collector rotates mid-row. The current runners also force one `pg_rotate_logfile()` after the row-end marker and before the archive copy so the logging collector has materialized the row tail on disk before parsing.

## Usage

Coordinator-only CSV:

```bash
python3 fdl_utils/parse_latency_trace_csv.py \
  bench-results-tuned/<run-name>/server-logs/node-0.log \
  -o /tmp/latency_trace.csv
```

Coordinator + one or more worker logs:

```bash
python3 fdl_utils/parse_latency_trace_csv.py \
  bench-results-tuned/<run-name>/server-logs/node-0.log \
  --worker-log bench-results-tuned/<run-name>/server-logs/node-1.log \
  --worker-log bench-results-tuned/<run-name>/server-logs/node-2.log \
  -o /tmp/latency_joined.csv
```

`--include-counts` adds `<stage>_count` columns so repeated stages inside one transaction row are visible.

## Row Semantics

Each row is one ordered trace block from the coordinator log, not one client benchmark summary line.

That means:

- For autocommit single-statement workloads, one row is usually one SQL statement / transaction.
- For explicit multi-statement transactions, one row is the whole traced transaction lifecycle that flushed together.
- If a transaction touches multiple worker nodes, all matched worker-side contributions from all supplied worker logs are summed into that same coordinator row.

## Important Interpretation

`trace_total_ns` is **not** a universal “full client-observed latency” number.

It is the span covered by the recorded ordered trace in that row:

- start: the earliest recorded stage in the trace
- end: the latest recorded stage in the trace

So it includes the stages that are instrumented, but it does **not** automatically include:

- client-side network/setup outside the server
- coordinator-local work that is intentionally not part of the ordered communication trace
- any server work that happened before the earliest recorded stage or after the latest recorded stage

In the current setup, `trace_total_ns` is best interpreted as:

- “ordered traced communication/control-path span for this transaction/query-cycle”

not:

- “ground-truth end-to-end latency seen by the client in all respects”

## Column Groups

### Metadata

- `row_index`: row number assigned by the parser.
- `transaction_key`: stable key for the row. Prefers logged coordinator identity; otherwise uses timestamp + pid + row index.
- `source_file`: coordinator logfile path that produced the row.
- `timestamp`: timestamp on the coordinator `LatencyTraceIdentity` header line.
- `pid`: coordinator backend PID that emitted the row.
- `identity`: logged distributed transaction identity, if present.
- `command_tag`: coordinator trace command tag. Often blank at coordinator row scope because the row can cover multiple remote commands.
- `preceding_query`: latest `Query executed:` text seen on that backend before the trace flushed. For explicit transactions this is often `COMMIT;`.
- `dropped_entries`: count of ordered trace entries dropped because the fixed trace buffer filled.
- `trace_total_ns`: covered span from earliest recorded trace start to latest recorded trace end.
  This is the traced server-side transaction span, not a perfect client-side
  stopwatch, because `client_command_receive_ns` starts after the message type
  byte is already available on the backend.

### Raw Stage Totals

These columns come directly from the coordinator ordered trace and are always in nanoseconds.

- `client_session_establish_ns`: traced client-to-coordinator backend startup/auth cost recorded on that backend. Present when the row includes session establishment.
- `backend_spawn_ns`: traced postmaster accept/fork/backend-handoff span before `BackendInitialize()` starts on that backend.
- `client_command_receive_ns`: traced backend-side frontend message ingress after the message type byte is available. This captures body framing and protocol receive work for Query / Parse / Bind / Execute / Describe / Sync / FunctionCall messages. It intentionally does not include arbitrary client-idle time before the first byte arrives.
- `backend_parse_plan_ns`: traced coarse coordinator-local parse / analyze / rewrite / planning work. This is backend execution, not communication stack, but it is now counted toward the covered trace span.
- `backend_command_turnaround_ns`: traced coordinator-local post-`ReadyForQuery` handoff before the next frontend wait begins. This covers timing-report emission, timeout/bookkeeping resets, and related outer-loop control work; it is intentionally not part of the communication stack buckets. Newer instrumentation keeps this stage only when the current ordered trace row intentionally stays open across commands, so it should not spill into the next transaction row.
- `worker_session_acquire_ns`: coordinator-side span for acquiring a worker SQL session. This may include cache lookup, connection establishment, startup/auth wait, and related control-path bookkeeping.
- `placement_bind_ns`: coordinator control-plane bookkeeping that pins the placement to a worker session.
- `remote_tx_attach_ns`: coordinator span for worker transaction attach/bootstrap (`BEGIN` + tx-state replay + distributed tx id assignment).
- `remote_command_dispatch_ns`: coordinator send/control span for dispatching the remote work command.
- `remote_command_flush_ns`: coordinator post-dispatch socket-flush span for task-query bytes that were queued in libpq but not yet fully flushed to the worker socket.
- `remote_command_wait_ns`: coordinator-observed remote work round before result drain. This is the main composite remote span.
- `remote_result_drain_ns`: coordinator drain/materialization tail after the remote work becomes ready.
- `remote_tx_commit_ns`: coordinator span for remote commit.
- `remote_tx_abort_ns`: coordinator span for remote abort.
- `remote_tx_prepare_ns`: coordinator span for remote prepare.
- `worker_session_release_ns`: coordinator end-of-transaction worker-session reset/release/close bookkeeping after remote commit/abort/prepare has finished.
- `client_command_complete_ns`: coordinator tail for `CommandComplete`.
- `client_ready_for_query_ns`: coordinator tail for `ReadyForQuery`.

### Count Columns

Only present with `--include-counts`.

- `<stage>_count`: how many times that stage occurred inside the row.

This matters because explicit transactions often have repeated client tail stages, and multi-statement transactions can have repeated remote command stages.

### Joined Decomposition Columns

These appear even when no worker logs are provided, but they will be zero unless the join found matching worker-side evidence.

- `worker_session_acquire_actual_ns`: worker-side actual startup/auth work attributed to the coordinator `worker_session_acquire_ns`, currently `backend_spawn + client_session_establish` from the matched worker ordered trace.
- `worker_session_acquire_comm_ns`: `worker_session_acquire_ns - worker_session_acquire_actual_ns`.
- `remote_tx_attach_actual_ns`: worker-side actual work for remote tx attach, derived from worker timing reports for `tx_begin`.
- `remote_tx_attach_comm_ns`: `remote_tx_attach_ns - remote_tx_attach_actual_ns`.
- `remote_command_wait_actual_ns`: worker-side actual work for remote task execution, currently chosen from worker `QUERY_ACTIVE_WALL` first, falling back to `ExecSimpleQuery`.
- `remote_command_wait_comm_ns`: `remote_command_wait_ns - remote_command_wait_actual_ns`.
- `remote_result_drain_actual_ns`: worker-side result production work, currently taken from worker `Printtup`.
- `remote_result_drain_comm_ns`: `remote_result_drain_ns - remote_result_drain_actual_ns`.
- `remote_tx_commit_actual_ns`: worker-side actual work for `tx_commit`.
- `remote_tx_commit_comm_ns`: `remote_tx_commit_ns - remote_tx_commit_actual_ns`.
- `remote_tx_abort_actual_ns`: worker-side actual work for `tx_abort`.
- `remote_tx_abort_comm_ns`: `remote_tx_abort_ns - remote_tx_abort_actual_ns`.
- `remote_tx_prepare_actual_ns`: worker-side actual work for `tx_prepare`.
- `remote_tx_prepare_comm_ns`: `remote_tx_prepare_ns - remote_tx_prepare_actual_ns`.

### Join Diagnostics

- `matched_worker_timing_blocks`: number of worker timing-report blocks that were matched into this row.
- `matched_worker_trace_blocks`: number of worker ordered-trace blocks used for worker session startup attribution in this row.
- `unmatched_worker_timing_blocks`: reserved diagnostic field. It is currently always `0`; unmatched worker timing blocks are not yet emitted per coordinator row.

## How To Interpret The Split

For every joined stage:

- `*_actual_ns` means backend work on the worker.
- `*_comm_ns` means the remainder of the coordinator-side observed stage after subtracting the worker-side actual work.

`*_comm_ns` is therefore a residual bucket, not pure wire time. It can include:

- network propagation
- socket/protocol wait
- queueing and scheduler delay
- local communication/control-path bookkeeping on the coordinator side

This is intentional. When `*_actual_ns` stays flat but `*_comm_ns` grows, the bottleneck is more likely in the communication/control path. When `*_actual_ns` itself grows, the worker backend is doing more real work.

## Current Join Heuristic

The join currently uses:

- `rsid` / `rcid` / `kind` propagated in the remote command tag
- stage-kind compatibility
- timestamp proximity between coordinator and worker events

This is enough for the current validation scenarios, including multiple worker logs. It is not yet a perfect globally unique correlation key under arbitrary concurrency and id reuse.

## Plotting

[`plot_latency_trace_scenario.py`](./plot_latency_trace_scenario.py) plots the mean of all rows in one scenario CSV as a stacked horizontal bar and saves the result as PDF.

The plot intentionally uses a smaller set of consolidated buckets instead of the
full raw stage list:

- `Client Session Setup`
  Uses `backend_spawn_ns + client_session_establish_ns`.
- `Client Protocol`
  Uses `client_command_receive_ns + client_command_complete_ns +
  client_ready_for_query_ns`.
- `Coordinator Backend Work`
  Uses `backend_parse_plan_ns`.
- `Worker Session Control`
  Uses `worker_session_release_ns` plus only
  `worker_session_acquire_comm_ns` when worker joins are available.
  This is the communication/control residual of worker session acquisition plus
  the coordinator-side end-of-transaction worker-session cleanup.
  If worker joins are unavailable, it falls back to
  `worker_session_acquire_ns + worker_session_release_ns`.
- `Worker Lifecycle Work`
  Uses `worker_session_acquire_actual_ns + remote_tx_attach_actual_ns +
  remote_tx_commit_actual_ns + remote_tx_abort_actual_ns +
  remote_tx_prepare_actual_ns`.
  This is worker-side backend work for session startup/auth and distributed
  transaction lifecycle, distinct from the later query execution bucket.
- `Distributed TX Management`
  Uses only the residual/control portions of attach / commit / abort / prepare
  when worker joins are available, and otherwise falls back to the raw
  coordinator stage totals.
- `Remote Execution Control`
  Uses placement bind, remote dispatch, remote flush, and the
  residual/control parts of remote wait and drain. Without worker joins it
  falls back to the raw coordinator `remote_command_flush_ns +
  remote_command_wait_ns + remote_result_drain_ns`.
- `Worker Backend Work`
  Uses `remote_command_wait_actual_ns + remote_result_drain_actual_ns`.
  This is worker-side actual query execution and result production work, and is
  only nonzero when worker joins are available.

The x-axis is now percentage share of the traced mean transaction latency, not
absolute milliseconds. That makes scenario-level composition easier to compare
when the absolute mean latency differs significantly across scenarios.

The main diagnostic split is now:

- `Client Protocol`, `Worker Session Control`, `Distributed TX Management`,
  and `Remote Execution Control`
  are communication/control-path heavy buckets.
- `Worker Lifecycle Work` and `Worker Backend Work`
  are worker-side backend work buckets.

When the first set grows under concurrency while the second stays relatively
flat, the latency increase is more likely caused by communication stack
contention, queueing, or protocol progress. When the worker-work buckets grow,
the worker backends themselves are taking longer to do real work.

## Waterfall Plot

[`plot_latency_trace_waterfall.py`](./plot_latency_trace_waterfall.py) plots one traced transaction as a swimlane/waterfall timeline:

- one coordinator lane for local/backend/control stages plus synthetic command
  envelopes, coordinator-side remote round-trip parent envelopes, and derived
  local-processing spans for uncovered coordinator command time
- one lane per matched worker session (`rsid=...`) for the coordinator-observed
  remote envelopes
- darker nested bars on each worker lane for the matched worker-side leaf work
- a synthetic coordinator session-setup envelope from `backend_spawn` through
  the first pre-query `ReadyForQuery`, so the post-auth startup tail does not
  appear as unexplained blank space before the first real query parse/plan
  stage

This plot is meant to make communication/control-path "bubbles" visually
obvious. The lighter envelope bars are what the coordinator observes. The
darker nested bars are worker-side leaf work derived from matched worker
traces/timing reports. The coordinator lane also includes a synthetic command
envelope from the first `client_command_wait` / `client_command_receive` of one
statement through the matching `ReadyForQuery`, coordinator-side remote query
parent envelopes grouped by `(rsid, rcid)`, plus derived local-processing bars
for uncovered time inside that command envelope.

For the current `pgbench` benchmark family, there are now three different
pgbench-specific selectors in the waterfall script:

- `--first-pgbench-transaction`
  selects the first real pgbench client transaction after the explicit
  row-start marker. This is the earliest benchmark transaction, even when the
  first session transaction still carries one-time prepared-statement setup.
  For the cold first prepared-session transaction, the selector also stitches
  the detached one-time `BEGIN` prepare prelude from the immediately preceding
  empty-identity block when that prelude was emitted separately from the later
  non-empty transaction row.
- `--first-pgbench-tpcb`
  selects the first clean steady-state transaction in the archived row
  snapshot. The helper finds the explicit row-start marker and then picks the
  first later coordinator row with a real pgbench client transaction identity
  and exactly 7 `client_ready_for_query` boundaries. This deliberately skips
  the earlier first session transaction when `pgbench` is still paying its
  one-time lazy `PQprepare()` setup cost.
- `--median-pgbench-session-transaction`
  selects a representative real pgbench transaction after the row-start
  marker by grouping real `conn_txn=...` rows into coarse transaction shapes
  and then taking the median traced span within the dominant shape class. The
  helper automatically stitches adjacent startup/auth and session-teardown
  rows around the chosen transaction. This is the selector to use for
  `pgbench --connect`, where the raw file-wide median can easily land on a
  startup-only or teardown-only row instead of a meaningful session-scoped
  transaction lifecycle.

Use `--stitch-session-startup` when you want the waterfall to merge adjacent
session-lifecycle rows around the selected transaction row. Today that means
the immediately preceding coordinator startup/auth row and the immediately
following `client_session_teardown` row when present. This is useful for cold
session + first transaction analysis and for future `pgbench -C`
per-transaction connection views.

### Waterfall Legend Semantics

- `Coordinator Session Setup Envelope`
  Synthetic parent envelope from `backend_spawn` through the pre-query startup
  `ReadyForQuery`. This appears only when a stitched or raw connection-start
  row is present.
- `Coordinator Session Setup Actual`
  The measured coordinator-side `backend_spawn` and `client_session_establish`
  stages nested inside that envelope.
- `Coordinator Session Teardown`
  The measured coarse `client_session_teardown` stage emitted on backend exit
  and optionally stitched onto the selected transaction row.
- `Coordinator Command Envelope`
  One full SQL command lifecycle on the coordinator, from the first
  `client_command_wait` / `client_command_receive` until the matching
  `client_ready_for_query`.
- `Coordinator Remote Round Envelope`
  One coordinator-observed worker round trip for a specific `(rsid, rcid)`
  across `placement_bind`, `remote_command_dispatch`, `remote_command_flush`,
  `remote_command_wait`, and `remote_result_drain`.
- `Coordinator Local DB / Control (Derived)`
  Coordinator-local time inside one command envelope that is not already
  covered by explicit measured ordered stages. In current plots this is mostly
  backend execution / Citus control work, but it remains a derived complement
  until coordinator timing leaves are aggregated into the waterfall.
- `Coordinator Post-Command Handoff`
  The post-`ReadyForQuery` local handoff before the backend blocks on the next
  frontend message. This is mostly
  [`FinishQueryTimingCycle()`](../src/backend/tcop/postgres.c#L649),
  [`latency_trace_finish_query_cycle()`](../src/backend/tcop/postgres.c#L661),
  [`logger_print_timings()`](../src/backend/tcop/postgres.c#L669), and the tiny
  outer-loop return to the next
  [`SocketBackend()`](../src/backend/tcop/postgres.c#L419) read boundary. It is
  intentionally not communication-stack work.
- `Client Wait`
  Wait from entering the frontend read boundary until the first message byte is
  readable on the coordinator.
- `Coordinator Comm / Control`
  Explicit coordinator child stages such as `placement_bind` and
  `remote_command_dispatch`.
- `Frontend Protocol / Replies`
  Coordinator/frontend protocol receive and reply stages such as
  `client_command_receive`, bind/describe response send, row send,
  `client_command_complete`, and `client_ready_for_query`.
- `Worker Envelope`
  The coordinator-observed parent window on a worker lane, such as worker
  transaction attach, query round, prepare, commit, or session release.
- `Worker Session Setup Actual`
  Worker backend spawn / startup/auth work nested inside `worker_session_acquire`
  when a matched worker ordered trace is available.
- `Worker Wait`
  Worker-side wait time inside the worker envelope from matched worker timing
  reports.
- `Worker Protocol / Transport`
  Worker-side socket/protocol CPU and transport-adjacent work from matched
  worker timing reports.
- `Worker Row Serde`
  Worker-side row/result serialization work before sending rows back.
- `Worker Local Processing`
  Worker-local DB execution / transaction work after subtracting the measured
  protocol / transport / serde leaves.

### Prepared `pgbench` Command Semantics

The benchmark runner uses `pgbench -M prepared`, not one single multi-statement
SQL string. The built-in `tpcb-like` script in
[`pgbench.c`](../src/bin/pgbench/pgbench.c#L780) contains 7 separate commands:

1. `BEGIN`
2. `UPDATE pgbench_accounts`
3. `SELECT pgbench_accounts`
4. `UPDATE pgbench_tellers`
5. `UPDATE pgbench_branches`
6. `INSERT pgbench_history`
7. `END`

[`sendCommand()`](../src/bin/pgbench/pgbench.c#L3155) sends each command
separately, and in prepared mode
[`PQsendQueryPrepared()`](../src/interfaces/libpq/fe-exec.c#L1663) uses the
extended protocol path in
[`PQsendQueryGuts()`](../src/interfaces/libpq/fe-exec.c#L1787), which emits
`Bind`, `Describe`, `Execute`, and `Sync` for each command. That is why one
logical `pgbench` transaction appears as multiple coordinator command envelopes
and why the same worker can show multiple separate remote rounds within one
transaction row.

The waterfall x-axis is elapsed traced transaction time in milliseconds. In the
current implementation, the right edge is therefore the selected row's
`trace_total_ns` converted to milliseconds, because the plot includes all traced
stage envelopes for that transaction.

By default, the script now chooses the coordinator row whose `trace_total_ns`
is the median traced span among all parsed coordinator rows. That keeps the
figure tied to one real transaction while using the larger sample to select a
representative row. Use `--row-index` to override that default and inspect a
specific transaction directly.

Current limitations:

- The backend now emits ordered stages for
  `client_command_wait`, `client_bind_complete_send`,
  `client_describe_response_send`, `client_result_send`, and
  `backend_command_turnaround`. The current waterfall path expects those stages
  to exist and treats pre-upgrade traces as unsupported.
- The coordinator local-processing bars are inferred from the gaps between
  measured stages inside one command envelope; they are not direct
  ordered-latency stage records.
- Exact left/right placement of the darker worker bars is still approximate.
  Session-start actual bars preserve worker-local relative order but are
  centered inside the matched `worker_session_acquire` envelope.
  Query / result / tx-lifecycle worker leaves are laid out sequentially inside
  the matched coordinator envelope stage so the plot shows wait, protocol CPU,
  worker-local processing, and row-send work separately.
- As a result, any leftover slack inside an envelope should still be read as one
  total residual for the enclosing envelope stage, not as exact sub-interval
  placement of wire time on both sides of a darker nested bar.
- This approximation exists because the current instrumentation does not yet
  emit explicit worker-side receive/send boundary markers for every remote
  command, and cross-node wall clocks are not explicitly synchronized by the
  parser.
- Worker timing/trace blocks are matched back to coordinator rows by shared
  distributed transaction id first, then by the older rcid/timestamp heuristic
  only as a fallback when no dtxid is present.
- Coordinator timing reports are now generically parseable, but they are not
  yet aggregated into dedicated coordinator leaf bars inside the waterfall.

Even with that limitation, the plot is still useful for seeing whether a
transaction's latency is dominated by large envelope slack, coordinator-local
processing spans, or large worker wait/protocol/local segments.

If you want the numbered/bracketed command overlay for the built-in prepared
`pgbench` tpcb-like transaction, pass `--annotate-pgbench-tpcb` to
[`plot_latency_trace_waterfall.py`](./plot_latency_trace_waterfall.py). That
mode expects exactly 7 coordinator command envelopes and labels them as
`BEGIN`, account update, account select, teller update, branch update, history
insert, and `COMMIT / 2PC`.
