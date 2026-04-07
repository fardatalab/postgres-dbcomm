# Benchmark Results

This directory holds the vanilla PostgreSQL synchronous-commit comparison
sweep.

- Benchmark family: single-node PostgreSQL primary with one physical standby
- Foreground workload: standard `pgbench`
- Sampling: `--sampling-rate 0.05`

The actual run is in [pgbench-sync-commit-20260402-180902](./pgbench-sync-commit-20260402-180902/).
