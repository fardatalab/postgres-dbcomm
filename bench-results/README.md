# Benchmark Results

This directory holds the first baseline `pgbench` synchronous-commit sweep on Citus.

- Benchmark family: Citus distributed tables
- Foreground workload: standard `pgbench` TPC-B-like mix
- Sweep shape: `synchronous_commit = off, local, on, remote_write, remote_apply`
- Foreground concurrency: `-c 1..32`
- Client-side worker cap: `-j` capped independently from `-c`
- Sampling: full logs for this first pass

The actual run is in [pgbench-sync-commit-20260402-121955](./pgbench-sync-commit-20260402-121955/).
