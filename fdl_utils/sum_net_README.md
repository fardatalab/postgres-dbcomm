# kernel stack timing

1. use perf record (PG now enables/disables it at logical query-cycle boundaries)

```bash
mkfifo /tmp/perf_ctl
sudo perf record -a -F 200 -e cpu-clock:k -g --kernel-callchains -P --control=fifo:/tmp/perf_ctl --delay=-1 -o perf.data
```

this will keep running, and we should run this (in a separate terminal) before any Citus/Postgres start executing queries/transactions

1. Start running with Citus as usual, then once it’s done, use Ctrl-C to stop perf, which should generate a `perf.data`
2. Dump meaningful data from it:

```bash
sudo perf script -i perf.data -F comm,period,event,ip,sym,dso > perf.txt
```

4. Pair the perf run with the matching timing denominator from the same
   perf-enabled run on the same node:

- single-query or single log slice:

```bash
# timing_summary.csv must come from the same perf-enabled run and same node
python3 aggregate_timing.py coordinator.log --view summary > timing_summary.csv
python3 sum_net.py perf.txt --aggregate-csv timing_summary.csv
```

- explicit transaction aggregation:

```bash
# txn_summary.csv must come from the same perf-enabled run and same node
python3 aggregate_transaction_timing.py coordinator.log > txn_summary.csv
python3 sum_net.py perf.txt --aggregate-csv txn_summary.csv
```

## Current backend semantics

- PG aligns FIFO perf control with the same logical query-cycle boundary used by
  `QUERY_ACTIVE_WALL`, including extended protocol until the matching
  `ReadyForQuery()`.
- Perf is still node-wide because the command uses `perf record -a`, so backend
  code coordinates it with a shared first-enable/last-disable refcount:
  the recorder stays enabled while at least one backend on the node is inside a
  logical query cycle.

## How to interpret the result

- `sampled_kernel_net_ns` is an estimate of kernel-network CPU-clock time for the
  instrumented run, not a ground-truth "without perf" runtime. Use it mainly
  for ratios, not as an absolute standalone number.
- `sum_net.py` now prints:
  - `sampled_kernel_total_ns`
  - `sampled_kernel_net_ns`
  - `query_active_wall_ns` when a denominator is provided
  - `kernel_net_avg_cores`
  - `kernel_net_pct_of_query_active_wall`
- `kernel_net_avg_cores` is the main metric. It is:

  `kernel_net_avg_cores ~= sampled_kernel_net_ns / query_active_wall_ns`

- `query_active_wall_ns` must come from the same run with perf enabled. Do not
  reuse a denominator from a separate no-perf baseline run, because the perf
  run is the run whose sampled kernel CPU is being measured.

- `kernel_net_pct_of_query_active_wall` is the same ratio times 100. This is
  still a wall-normalized CPU ratio, so values above 100% are possible if the
  kernel networking work averaged more than one core during the active window.
- The old sampled-kernel ratio is still available only as an explicit debug
  output via `--show-sampled-kernel-fraction`.

## Concurrency caveat

- Under overlapping backends on the same node, perf captures the union of all
  active query cycles on that node. That is valid for node-aggregate
  measurements, but it is not per-query attribution anymore.
- If you intentionally run multiple clients concurrently and treat the whole run
  as one measurement, then sum `query_active_wall_ns` across all matching query
  or transaction rows on that node for that same perf-enabled run and use that as the
  denominator.
- Do not divide a concurrent node-wide perf run by one transaction's or one
  query's `query_active_wall_ns`.
