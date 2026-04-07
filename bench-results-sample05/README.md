# Benchmark Results

This directory holds the `pgbench` synchronous-commit sweep using a 5% log
sampling rate.

- Benchmark family: Citus distributed tables
- Foreground workload: standard `pgbench`
- Sampling: `--sampling-rate 0.05`

The actual run is in [pgbench-sync-commit-20260402-125212](./pgbench-sync-commit-20260402-125212/).
