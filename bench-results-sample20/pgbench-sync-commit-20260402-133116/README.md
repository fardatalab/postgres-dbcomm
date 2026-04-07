# Citus Sync-Commit Sweep, 20% Sampling

This run repeated the Citus synchronous-commit sweep with a higher sampling
rate to stabilize percentile estimates.

## Setup

- Cluster: current Citus coordinator/workers on the `10.10.1.x` fabric
- Tables: distributed `pgbench` tables
- Foreground workload: standard `pgbench`

## Benchmark

- Script: `scripts/run_pgbench_sync_commit_bench.py`
- `pgbench` mode: `-M prepared`
- Foreground concurrency: `-c 1..32`
- Client-side worker cap: capped independently from `-c`
- Sampling rate: `0.20`
- Duration: fixed-duration sweep

## Outputs

- `results.csv`
- `plots/`

