# Tuned Citus Sync-Commit Sweep

This run is the tuned Citus benchmark after increasing the server-side
resources for a more dedicated benchmark configuration.

## Setup

- Cluster: current Citus node-0..node-4 layout on the `10.10.1.x` fabric
- Tables: distributed `pgbench` schema
- Runtime tuning: larger `max_connections`, `shared_buffers`, `max_wal_size`,
  `checkpoint_timeout`, and `min_wal_size` on every node

## Benchmark

- Script: `scripts/run_pgbench_sync_commit_bench.py`
- `pgbench` mode: `-M prepared`
- Foreground concurrency: `-c 1..32`
- Client-side worker cap: capped independently from `-c`
- Sampling rate: `0.05`
- Duration: fixed-duration sweep

## Outputs

- `results.csv`
- `plots/`
- `comparison-plots/` for Citus-vs-vanilla plots

