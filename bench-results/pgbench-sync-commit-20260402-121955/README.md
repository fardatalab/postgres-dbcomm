# Citus Baseline Sync-Commit Sweep

This run is the baseline Citus benchmark used before the later tuning pass.

## Setup

- Cluster: node-0 to node-4 Citus layout on the `10.10.1.x` fabric
- Coordinator: `node-0`
- Workers: `node-1` and `node-2`
- Worker standbys: `node-3` and `node-4`
- Citus tables: standard `pgbench` tables distributed with `create_distributed_table(...)`

## Benchmark

- Script: `scripts/run_pgbench_sync_commit_bench.py`
- `pgbench` mode: `-M prepared`
- Foreground concurrency: `-c 1..32`
- Client-side worker cap: `-j` capped separately from `-c`
- Sampling: `--sampling-rate 1.0`
- Duration: fixed-duration sweep

## Outputs

- `results.csv`: run summary and percentile metrics
- `plots/`: seaborn plots for TPS, p50, and p99

