# Ordered Latency CSV Parser

[`parse_latency_trace_csv.py`](./parse_latency_trace_csv.py) converts the ordered latency trace blocks emitted by [`latency_instr.c`](../src/common/latency_instr.c) into one CSV row per coordinator transaction/query-cycle trace. When worker logs are also provided, it joins worker timing reports and worker ordered traces back into each coordinator row using the propagated `rsid` / `rcid` / `kind` tags plus timestamp proximity.

The parser expects collector-backed PostgreSQL logs. In this cluster, the benchmark runbook archives those files with [`scripts/archive_server_logs.py`](../scripts/archive_server_logs.py) in `--current-only` mode after each benchmark row, and it uses [`scripts/reset_server_logs.py`](../scripts/reset_server_logs.py) to rotate/prune the collector directory before the next row starts.

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

- one coordinator lane for local/backend/control stages
- one lane per matched worker session (`rsid=...`) for the coordinator-observed
  remote envelopes
- darker nested bars on each worker lane for the matched worker-side actual work
- a synthetic coordinator startup envelope from `backend_spawn` through the
  first pre-query `ReadyForQuery`, so the post-auth startup tail does not appear
  as unexplained blank space before the first real query parse/plan stage

This plot is meant to make communication/control-path "bubbles" visually
obvious. The lighter envelope bars are what the coordinator observes. The
darker nested bars are worker-side actual work derived from matched worker
traces/timing reports. Slack around the darker bars is the approximate
communication/control residual.

The waterfall x-axis is elapsed traced transaction time in milliseconds. In the
current implementation, the right edge is therefore the selected row's
`trace_total_ns` converted to milliseconds, because the plot includes all traced
stage envelopes for that transaction.

Current limitation:

- Exact left/right placement of the darker worker bars is approximate.
  Session-start actual bars preserve worker-local relative order but are
  centered inside the matched `worker_session_acquire` envelope.
  Query / result / tx-lifecycle actual bars are centered inside the matched
  coordinator envelope stage.
- As a result, the visible slack to the left and right of a darker nested bar
  should be read as one total residual for the enclosing envelope stage, not as
  two separately measured wait intervals.
- This approximation exists because the current instrumentation does not yet
  emit explicit worker-side receive/send boundary markers for every remote
  command, and cross-node wall clocks are not explicitly synchronized by the
  parser.

Even with that limitation, the plot is still useful for seeing whether a
transaction's latency is dominated by large envelope slack or by large worker
actual-work segments.
