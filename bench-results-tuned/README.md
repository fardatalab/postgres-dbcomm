# Benchmark Results

This directory holds the tuned Citus synchronous-commit sweep.

- Benchmark family: Citus distributed tables
- Foreground workload: standard `pgbench`
- Tuning: larger `shared_buffers`, `max_wal_size`, `max_connections`, and
  other dedicated-benchmark settings on every node

The actual run is in [pgbench-sync-commit-20260402-145311](./pgbench-sync-commit-20260402-145311/).
