# Vanilla PostgreSQL Sync-Commit Sweep

This run compares the same synchronous-commit matrix on vanilla PostgreSQL
with one physical standby.

## Setup

- Primary: `node-0`
- Standby: `node-3`
- `node-0` was temporarily repurposed away from Citus by removing the Citus
  preload, then restarted as a plain PostgreSQL primary
- Replication: physical WAL streaming from `node-0` to `node-3`
- Benchmark database: `pgbench_vanilla`

## Benchmark

- Script: `scripts/run_pgbench_sync_commit_bench_vanilla.py`
- `pgbench` mode: `-M prepared`
- Foreground concurrency: `-c 1..32`
- Client-side worker cap: capped independently from `-c`
- Sampling rate: `0.05`
- Duration: fixed-duration sweep

## Outputs

- `results.csv`
- `plots/`

