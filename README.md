# Benchmark Runbook

This repository is a PostgreSQL 17 tree plus a copied Citus tree that we use for benchmark experiments on the `node-0` to `node-4` cluster.

The canonical results directories to keep are:

- `bench-results-network-interference`
- `bench-results-tuned`
- `bench-results-vanilla`

The older `bench-results`, `bench-results-sample05`, `bench-results-sample20`, and `bench-results-test` trees are exploratory or intermediate runs.

## Cluster Assumptions

The scripts in this tree assume the following layout and install paths:

- `node-0`: coordinator for Citus, or vanilla primary when the vanilla setup scripts repurpose it
- `node-1`: Citus worker primary, and the generator host for the vanilla interference experiment
- `node-2`: Citus worker primary
- `node-3`: standby for `node-1`, or vanilla standby when the vanilla setup scripts repurpose it
- `node-4`: standby for `node-2`
- fast fabric: `enp23s0f0` on `10.10.2.0/24`
- install prefix on every node: `~/pg`
- SSH user: `JasonHu`

The current branch layout is also important:

- PostgreSQL: `pg-17-timings`
- Citus: copied `timings-jason`

## Setup Not Handled By The Scripts

Before running any benchmark, make sure these prerequisites are already true:

1. PostgreSQL and Citus are built and installed under `~/pg` on every node.
2. If a build tree was copied in with `scp`, it was reconfigured for this cluster before installation so generated paths point at `~/pg`.
3. The `10.10.2.0/24` fabric is allowed in `pg_hba.conf` for normal connections and replication connections.
4. Collector-backed logging is enabled on all nodes (`logging_collector = on`) so the latency-trace parser can read the server logs for each run.
5. The cluster nodes can SSH to each other with `BatchMode=yes`.
6. For Citus runs, node-0 must currently preload `citus` and node-3/node-4 must be the worker standbys. On a fresh cluster, bootstrap those standbys from node-1/node-2 and register them as secondaries in the Citus metadata before the first benchmark run.
7. For vanilla runs, node-0 must be a plain PostgreSQL primary and node-3 must be its standby.

### Build And Install Prerequisites

The setup scripts assume the binaries are already available under `~/pg`. The cluster-specific steps are:

- PostgreSQL: work from the `pg-17-timings` branch, reconfigure the Meson build tree under `build/` with the new `~/pg` prefix, then install that build tree.
- Citus: work from the copied `timings-jason` tree, clean out stale copied build artifacts if needed, rerun `./configure PG_CONFIG=$HOME/pg/bin/pg_config`, rebuild, and reinstall so the generated makefiles and cached metadata point at the current cluster.

If the tree came from another cluster, a clean rebuild is important for the Citus preload path to work reliably on this one.

For a fresh Citus bring-up, the manual metadata order matters:

- start node-0, node-1, and node-2 with Citus preloaded
- run `SELECT citus_set_coordinator_host('10.10.2.1', 5432);` on node-0
- add node-1 and node-2 with `citus_add_node(...)`
- bootstrap node-3 from node-1 and node-4 from node-2 with `pg_basebackup -R -X stream -C -S ...`
- register node-3 and node-4 with `citus_add_secondary_node(...)`

`citus_update_node()` is for later address moves or failover-style updates; it does not create the initial metadata rows.

## Recommended Reproduction Order

A full end-to-end reproduction usually goes in this order:

1. Build and install PostgreSQL and Citus under `~/pg` on all five nodes.
2. Make sure `pg_hba.conf` allows the `10.10.2.0/24` fabric on all nodes, for both normal connections and replication.
3. Restore the Citus topology and verify `CREATE EXTENSION citus;` succeeds on node-0.
4. Run the Citus synchronous-commit sweep.
5. Run the Citus network-interference sweep.
6. Switch node-0/node-3 to the vanilla primary/standby topology.
7. Run the vanilla synchronous-commit sweep.
8. Run the vanilla network-interference sweep.
9. Restore Citus again before leaving the cluster in a reusable state for later Citus runs.
10. Archive the collector logs from the nodes involved in each run and parse them with the latency-trace utilities if you want the trace-level measurements, not just the `pgbench` summary CSV.

## How The Scripts Fit Together

The repo has four benchmark families:

- Citus synchronous-commit sweep
- vanilla PostgreSQL synchronous-commit sweep
- Citus network-interference sweep
- vanilla PostgreSQL network-interference sweep

Each family has a setup script, a runner, and usually a plotter.

### Latency Trace Collection

The latency-instrumented branch writes ordered trace blocks into the collector-backed PostgreSQL logs, so after a benchmark row you should snapshot the collector logs from the participating nodes before parsing.

Use:

```bash
python3 scripts/archive_server_logs.py --output-dir bench-results-tuned/<run-name>/server-logs
```

The benchmark runners now use a row-by-row log workflow:

1. rotate/prune the collector logs on the participating nodes before the row starts
2. emit an explicit row-start marker so the fresh collector segment exists before the workload begins
3. run the benchmark row
4. emit a row-end marker
5. force one `pg_rotate_logfile()` on each archived node so the collector flushes the row tail into durable segments before copy
6. archive every collector segment created during that row, and stitch them into `node-X.log` snapshots in that row's `server-logs/` subdirectory

That keeps each CSV row paired with its own text logfile snapshot, avoids the huge multi-row collector directories we had earlier, prevents the earliest traced transactions in the row from losing worker `tx_begin` / `task_query` records when the collector rotates mid-row, and avoids racing the logging collector while it is still draining the active segment after the row-end marker.

For Citus benchmark families, archive node-0 plus the worker primaries `node-1` and `node-2`.
For vanilla benchmark families, archive node-0 plus the standby node-3 if it has useful trace output; the primary log is the most important one.

Then parse the archived logs with:

```bash
python3 fdl_utils/parse_latency_trace_csv.py \
  bench-results-tuned/<run-name>/server-logs/node-0.log \
  --worker-log bench-results-tuned/<run-name>/server-logs/node-1.log \
  --worker-log bench-results-tuned/<run-name>/server-logs/node-2.log \
  -o bench-results-tuned/<run-name>/latency-trace.csv
```

The scenario plotter and waterfall plotter then consume that latency CSV and the archived per-row log snapshots, respectively. The waterfall plotter now defaults to the coordinator row whose traced span (`trace_total_ns`) is the median of that archived sample, and `--row-index` still lets you inspect a specific traced transaction when you want a targeted debug view. The updated waterfall now does three important things:

- synthesizes coordinator command envelopes and coordinator-side remote round-trip parent envelopes so the coordinator lane shows the full worker round trip rather than only child dispatch/wait/drain slices spread across lanes
- requires the newer ordered stages such as `client_command_wait`, bind/describe response send, row send, and the post-`ReadyForQuery` coordinator turnaround/logging stage that regenerated traces now emit
- decomposes worker timing reports into wait, protocol/transport CPU, row serialization, and worker-local processing instead of one centered opaque worker "actual" bar

The waterfall is meant to separate:

- communication-stack-facing work:
  client/coordinator protocol receive and replies, coordinator remote dispatch control, coordinator-observed remote round envelopes, worker protocol / transport CPU, and worker-side wait inside those envelopes
- local backend / DB work:
  coordinator-local parse / execute / distributed-control work and worker-local execution / transaction work

`backend_command_turnaround` is intentionally not part of the communication stack buckets. It is the coordinator's post-`ReadyForQuery` handoff window after the command is already complete, covering [`FinishQueryTimingCycle()`](/users/JasonHu/postgres-dbcomm/src/backend/tcop/postgres.c#L649), the ordered-trace/timing report flush via [`latency_trace_finish_query_cycle()`](/users/JasonHu/postgres-dbcomm/src/backend/tcop/postgres.c#L661) and [`logger_print_timings()`](/users/JasonHu/postgres-dbcomm/src/backend/tcop/postgres.c#L669), plus the tiny outer-loop handoff back to the next frontend read. It should not include Citus distributed execution work, worker round trips, or frontend socket wait.

#### Waterfall Legend Semantics

- `Coordinator Command Envelope`
  One full SQL command lifecycle on the coordinator, from the first `client_command_wait` / `client_command_receive` until the matching `client_ready_for_query`.
- `Coordinator Remote Round Envelope`
  One coordinator-observed worker round trip for a specific `(rsid, rcid)` across `placement_bind`, `remote_command_dispatch`, `remote_command_flush`, `remote_command_wait`, and `remote_result_drain`.
- `Coordinator Local DB / Control (Derived)`
  Coordinator-local time inside one command envelope that is not already covered by explicit measured ordered stages. In current plots this is mostly backend execution / Citus control work, but it remains a derived complement until coordinator timing leaves are aggregated into the waterfall.
- `Coordinator Post-Command Handoff`
  The post-`ReadyForQuery` local handoff before the backend blocks on the next frontend message. This is mostly local timing/logging / loop bookkeeping, not communication-stack work.
- `Client Wait`
  Wait from entering the frontend read boundary until the first message byte is readable on the coordinator.
- `Coordinator Comm / Control`
  Explicit coordinator child stages such as `placement_bind` and `remote_command_dispatch`.
- `Frontend Protocol / Replies`
  Coordinator/frontend protocol receive and reply stages such as `client_command_receive`, bind/describe response send, row send, `client_command_complete`, and `client_ready_for_query`.
- `Coordinator Session Setup Envelope`
  Synthetic parent envelope from `backend_spawn` through the first startup `ReadyForQuery` when a stitched or raw connection-start row is present.
- `Coordinator Session Setup Actual`
  The measured `backend_spawn` and `client_session_establish` stages nested inside that envelope.
- `Coordinator Session Teardown`
  The measured coarse `client_session_teardown` stage emitted on backend exit and optionally stitched onto the selected transaction row.
- `Worker Envelope`
  The coordinator-observed parent window on a worker lane, such as worker transaction attach, query round, prepare, commit, or session release.
- `Worker Wait`
  Worker-side wait time inside the worker envelope from matched worker timing reports.
- `Worker Protocol / Transport`
  Worker-side socket/protocol CPU and transport-adjacent work from matched worker timing reports.
- `Worker Row Serde`
  Worker-side row/result serialization work before sending rows back.
- `Worker Session Setup Actual`
  Worker backend spawn / startup/auth work nested inside `worker_session_acquire` when a matched worker ordered trace is available.
- `Worker Local Processing`
  Worker-local DB execution / transaction work after subtracting the measured protocol / transport / serde leaves.

Use `--annotate-pgbench-tpcb` with [`plot_latency_trace_waterfall.py`](./fdl_utils/plot_latency_trace_waterfall.py) when you want the bracketed numbered overlay that labels the 7 built-in prepared `pgbench` commands directly on top of the representative transaction figure. The annotator supports both the steady-state 7-cycle row shape and the stitched 14-cycle first-transaction shape by collapsing each cold first-use prepare+execute pair back into one semantic pgbench command. There are now three pgbench-specific selectors:

- `--first-pgbench-transaction`
  Picks the first real pgbench client transaction after the explicit row-start marker. This is the right selector for the cold-ish first transaction. For the cold first prepared-session transaction, the helper also stitches in the detached one-time `BEGIN` prepare prelude when that cycle was logged in the immediately preceding startup/empty-identity block, so the plotted first transaction reflects the full first-use command semantic rather than only the later non-empty row.
- `--first-pgbench-tpcb`
  Picks the first clean steady-state 7-command prepared pgbench transaction after the row-start marker. This deliberately skips the earlier first session transaction when it is still paying the one-time lazy `PQprepare()` setup cost.
- `--median-pgbench-session-transaction`
  Picks a representative real pgbench transaction after the row-start marker by first grouping real `conn_txn=...` rows by transaction shape and then taking the median traced span within the dominant shape class. The helper also stitches adjacent startup/teardown rows automatically. This is the right selector for `pgbench --connect`, where a raw file-wide median can land on a startup-only or teardown-only row instead of a meaningful session-scoped transaction lifecycle.

Use `--stitch-session-startup` when you want to merge adjacent session-lifecycle rows around the selected transaction row. Today that means:

- the immediately preceding coordinator startup/auth row, and
- the immediately following `client_session_teardown` row when present

This is the right view for cold session + first transaction analysis, and it is also the shape we will want for future `pgbench -C` per-transaction connection plots.

#### Why One `pgbench` Transaction Shows Multiple Command Envelopes

The benchmark runner uses `pgbench -M prepared`, not one single multi-statement SQL string. The built-in `tpcb-like` script in [`pgbench.c`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L780) contains 7 separate commands:

1. `BEGIN`
2. `UPDATE pgbench_accounts`
3. `SELECT pgbench_accounts`
4. `UPDATE pgbench_tellers`
5. `UPDATE pgbench_branches`
6. `INSERT pgbench_history`
7. `END`

[`sendCommand()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3155) sends each command separately, and in prepared mode [`sendCommand()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3155) first calls [`prepareCommand()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3089). On the first use of each command in one client session, [`prepareCommand()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3089) issues [`PQprepare()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3105) before the actual execution path later calls [`PQsendQueryPrepared()`](/users/JasonHu/postgres-dbcomm/src/bin/pgbench/pgbench.c#L3189). The libpq execution path in [`PQsendQueryGuts()`](/users/JasonHu/postgres-dbcomm/src/interfaces/libpq/fe-exec.c#L1787) emits `Bind`, `Describe`, `Execute`, and `Sync` for each command. That is why a single logical `pgbench` transaction appears as multiple coordinator command envelopes, why worker 1 can show two separate remote rounds for the account `UPDATE` and later account `SELECT`, and why the very first real transaction can legitimately have more than the steady-state 7 command cycles.

### Citus Synchronous-Commit Sweep

If you just finished the vanilla experiments, restore the Citus cluster first with the Citus interference setup helper, then ensure the distributed pgbench tables exist. The setup helper can also verify the tables and recreate the background sink table if needed.

First initialize or verify the distributed pgbench tables:

```bash
bash scripts/setup_pgbench_citus.sh 32 postgres
```

That helper runs the standard `pgbench -i -s 32` initializer and then distributes the built-in tables with Citus.

Then run the sweep:

```bash
python3 scripts/run_pgbench_sync_commit_bench.py \
  --dbname postgres \
  --duration 5 \
  --max-clients 32 \
  --pgbench-workers 8 \
  --sampling-rate 1.0 \
  --results-dir bench-results-tuned
```

Useful notes:

- `-c` is the actual session concurrency; the runner sweeps it from 1 to `--max-clients`.
- `-j` is capped separately with `--pgbench-workers` so client-side worker threads do not overwhelm the benchmark driver.
- The runner uses `-M prepared`.
- The default modes are `local`, `on`, `remote_write`, and `remote_apply`.
- The default result directory is `bench-results`, but for the canonical tuned run we keep the output under `bench-results-tuned`.

Plot the CSV afterward with:

```bash
python3 scripts/plot_pgbench_sync_commit_bench.py \
  --csv bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv
```

If you want the trace-level instrumentation for this run, archive the node logs from the same result directory after the sweep and then parse them with `fdl_utils/parse_latency_trace_csv.py` as described above.

### Vanilla PostgreSQL Synchronous-Commit Sweep

Switch node-0/node-3 away from Citus and into the plain primary/standby topology, then initialize a separate `pgbench_vanilla` database:

```bash
python3 scripts/setup_pgbench_vanilla.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

Then run the vanilla sweep:

```bash
python3 scripts/run_pgbench_sync_commit_bench_vanilla.py \
  --dbname pgbench_vanilla \
  --duration 12 \
  --max-clients 32 \
  --pgbench-workers 8 \
  --sampling-rate 0.05 \
  --results-dir bench-results-vanilla
```

Useful notes:

- This run is the plain PostgreSQL counterpart to the Citus sweep.
- It also uses `-M prepared`.
- The default result directory is `bench-results-vanilla`.
- The helper checks that node-3 is streaming from node-0 before it runs.

To compare the tuned Citus and vanilla sweeps side by side:

```bash
python3 scripts/plot_pgbench_sync_commit_comparison.py \
  --citus-csv bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv \
  --vanilla-csv bench-results-vanilla/pgbench-sync-commit-<timestamp>/results.csv
```

Archive the collector logs from node-0 and node-3 after the vanilla run if you want the trace-level latency decomposition for that family as well.

### Citus Network-Interference Sweep

If you are coming back from vanilla experiments, restore the Citus topology first. Then prepare the background sink table and verify the distributed pgbench tables:

```bash
python3 scripts/setup_citus_network_interference.py \
  --pgbench-scale 32 \
  --dbname postgres \
  --private-client-count 16 \
  --client-private-rows 100000
```

Then run the interference sweep:

```bash
python3 scripts/run_citus_network_interference_bench.py \
  --dbname postgres \
  --clients 1 8 16 \
  --repeats 3 \
  --duration 12 \
  --pgbench-workers 8 \
  --sampling-rate 0.05 \
  --workload client-private-table \
  --background-levels 0 1 2 4 8 \
  --reset-pgbench-between-runs logical \
  --results-dir bench-results-network-interference
```

Useful notes:

- `--clients 1 8 16` gives low, medium, and high foreground transaction concurrency in one CSV.
- `--repeats 3` runs each exact mode/client/background row three times. The plotters aggregate repeats and show standard-deviation error bars.
- `--workload client-private-table` is the low-contention foreground workload. The setup helper creates one logged account table per logical client, and the runner starts one single-client `pgbench` process per table, then aggregates the per-client logs into one row.
- Use `--workload tpcb` only for the realistic built-in pgbench comparison; that path still has branch/teller hot-row contention.
- `background_level = 0` is the no-background baseline.
- For each measured row, the runner restores the foreground tables touched by the selected workload and truncates all background write-sink tables. Use `--reset-pgbench-between-runs none` only when you explicitly want the older cumulative-state behavior.
- Background readers stream `SELECT *`-style traffic from a large distributed source table.
- Background writers stream `COPY` batches into a distributed sink table.
- The background loops run until a shared stop time so the overlap with `pgbench` is guaranteed.
- The default result directory is `bench-results-network-interference`.
- The Citus helper currently refreshes the worker-1 standby on `node-3`; if `node-4` is missing or stale on a fresh cluster, reclone it from node-2 before running the Citus benchmark family.

To plot one Citus interference CSV:

```bash
python3 scripts/plot_citus_network_interference.py \
  --csv bench-results-network-interference/citus-network-interference-<timestamp>/results.csv
```

To compare the Citus and vanilla interference sweeps:

```bash
python3 scripts/plot_pgbench_network_interference_comparison.py \
  --citus-csv bench-results-network-interference/citus-network-interference-<timestamp>/results.csv \
  --vanilla-csv bench-results-network-interference/vanilla-network-interference-<timestamp>/results.csv
```

Archive node-0, node-1, and node-2 from the Citus interference run before parsing the trace logs with `fdl_utils/parse_latency_trace_csv.py`.

### Vanilla PostgreSQL Network-Interference Sweep

Switch node-0/node-3 into the vanilla primary/standby topology and create the local background source/sink tables:

```bash
python3 scripts/setup_pgbench_vanilla_interference.py \
  --scale 32 \
  --dbname pgbench_vanilla \
  --private-client-count 16 \
  --client-private-rows 100000
```

Then run the interference sweep:

```bash
python3 scripts/run_pgbench_network_interference_vanilla.py \
  --dbname pgbench_vanilla \
  --clients 1 8 16 \
  --repeats 3 \
  --duration 16 \
  --pgbench-workers 8 \
  --sampling-rate 0.2 \
  --workload client-private-table \
  --background-levels 0 1 2 4 8 \
  --reset-pgbench-between-runs logical \
  --results-dir bench-results-network-interference
```

Useful notes:

- `node-1` is used as the generator host for the background traffic.
- The foreground benchmark is still `pgbench` on node-0.
- `background_level = 0` is the no-background baseline.
- `--clients 1 8 16` gives low, medium, and high foreground transaction concurrency in one CSV.
- `--repeats 3` runs each exact mode/client/background row three times. The plotters aggregate repeats and show standard-deviation error bars.
- `--workload client-private-table` is the low-contention foreground workload. Use `--workload tpcb` only for the realistic built-in pgbench comparison.
- The setup helper creates background `COPY`/`SELECT` sink/source tables as `UNLOGGED`. That avoids turning the background writer into a WAL/replication benchmark.
- For each measured row, the runner restores the foreground tables touched by the selected workload and truncates the background sink tables. Use `--reset-pgbench-between-runs none` only when you explicitly want the older cumulative-state behavior.
- The default result directory is `bench-results-network-interference`.

Archive node-0 and node-3 after the vanilla interference run if you want to parse the collector logs for the trace-level view.

The important negative result from this family is that logged SQL sink tables
created an apparent replicated-commit latency signal, but the signal mostly
disappeared when the sinks were changed to `UNLOGGED`. That means the earlier
large SQL `COPY`/`SELECT` effect was mostly WAL/replication pressure from the
logged background sinks, not clean network interference. The comparison plot is
under:

```text
bench-results-network-interference/sql-unlogged-negative-vs-basebackup-20260505/
```

### Vanilla Physical-Basebackup Interference Sweep

For the current positive network-interference motivation, use the physical
backup harness instead of the SQL `COPY`/`SELECT` background generator:

```bash
python3 scripts/run_pgbench_basebackup_interference_vanilla.py \
  --dbname pgbench_vanilla \
  --duration 16 \
  --clients 1 8 16 \
  --repeats 5 \
  --modes local remote_write remote_apply \
  --basebackup-streams 0 1 4 8 \
  --sampling-rate 0.2 \
  --pgbench-workers 8 \
  --results-dir bench-results-network-interference
```

Useful notes:

- `node-0` is the vanilla primary and `node-3` is its synchronous standby.
- `node-1` runs looping `pg_basebackup` clients against node-0.
- Foreground `pgbench` still runs on node-0 against node-0.
- Replication traffic is node-0 to node-3; physical backup traffic is node-0 to node-1.
- `basebackup_streams = 0` is the no-background baseline.
- `local` at the same backup-stream count is the primary-side control: same foreground work and same backup traffic, but commits do not wait for standby acknowledgement.
- The local-normalized latency plots are unitless ratios: remote-mode slowdown divided by local-mode slowdown.

Best current result:

```text
bench-results-network-interference/vanilla-basebackup-interference-20260505-merged-5repeats/
```

At 8 `pg_basebackup` streams, the 5-repeat merged run measured roughly 65-85 Gbps
of node-0 TX and showed local-normalized extra average-latency slowdown of about
2.0-3.0x for `remote_write` and 2.9-3.9x for `remote_apply`. The p99 effect was
much larger, about 10-64x over local for `remote_write` and 25-82x over local
for `remote_apply`.

Plot it with:

```bash
python3 scripts/plot_basebackup_interference.py \
  bench-results-network-interference/vanilla-basebackup-interference-<timestamp>/results.csv
```


## Switching Between Citus And Vanilla

The scripts intentionally let node-0/node-3 move between the two topologies, but the switch should be explicit so the state is easy to reason about. The order below is the safe way to do it.

### From Citus To Vanilla

1. Finish or stop any Citus benchmark that is still running.
2. Run the vanilla setup script to repurpose node-0 as a primary and node-3 as its standby:

```bash
python3 scripts/setup_pgbench_vanilla.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

3. If you plan to run the vanilla interference benchmark, add the local sink table:

```bash
python3 scripts/setup_pgbench_vanilla_interference.py \
  --scale 32 \
  --dbname pgbench_vanilla
```

### From Vanilla Back To Citus

1. Finish or stop any vanilla benchmark that is still running.
2. Restore node-0 as a Citus coordinator and node-3 as the worker-1 standby:

```bash
python3 scripts/setup_citus_network_interference.py \
  --pgbench-scale 32 \
  --dbname postgres
```

3. If `node-4` is missing or stale on a fresh cluster, bootstrap it from node-2 and register it as the worker-2 standby before running Citus benchmarks.
4. If you only need the distributed pgbench tables and not the interference sink table, pass `--skip-pgbench-setup` to avoid reinitializing the tables.
5. If you are starting from a fresh Citus topology, run `scripts/setup_pgbench_citus.sh 32 postgres` afterward when you specifically want to refresh the distributed pgbench tables.

This explicit switch order matters because the vanilla setup disables the Citus preload on node-0, while the Citus setup expects that preload and the worker standbys to be in place.

## Common Output Layout

Each timestamped run directory contains a `results.csv` and the plots for that run.

Examples:

- `bench-results-tuned/pgbench-sync-commit-<timestamp>/results.csv`
- `bench-results-vanilla/pgbench-sync-commit-<timestamp>/results.csv`
- `bench-results-network-interference/citus-network-interference-<timestamp>/results.csv`
- `bench-results-network-interference/vanilla-network-interference-<timestamp>/results.csv`

The plotters write a sibling `plots/` or `comparison-plots/` directory next to the CSV unless you override `--output-dir`.

## Quick Reference

- Use the Citus setup helper before the Citus benchmarks.
- Use the vanilla setup helper before the vanilla benchmarks.
- Use the Citus interference setup helper before the Citus interference run.
- Use the vanilla interference setup helper before the vanilla interference run.
- Keep the three canonical result trees: `bench-results-network-interference`, `bench-results-tuned`, and `bench-results-vanilla`.
