# Benchmark Results

This directory holds the Citus network-interference experiments.

- Benchmark family: Citus distributed tables
- Foreground workload: fixed `pgbench` run at `-c 16`
- Background workload: large distributed reader and writer loops running in
  parallel
- Goal: measure how concurrent SQL-driven network traffic affects foreground
  transaction latency and throughput

Runs currently present:

- [citus-network-interference-20260402-202754](./citus-network-interference-20260402-202754/) for the preliminary `synchronous_commit = local` pass
- [citus-network-interference-20260402-210021](./citus-network-interference-20260402-210021/) for the full `off/local/on/remote_write/remote_apply` sweep
- [citus-network-interference-20260402-214659](./citus-network-interference-20260402-214659/) for the baseline-inclusive sweep with `background_level = 0, 1, 2, 4, 8`
- [vanilla-network-interference-20260403-101046](./vanilla-network-interference-20260403-101046/) for the vanilla primary/standby interference sweep driven from node-1

Comparison artifact:

- [network_interference_comparison.png](./citus-network-interference-20260402-214659/comparison-plots/network_interference_comparison.png) for the side-by-side Citus vs vanilla view of TPS, p50, and p99 under background load
