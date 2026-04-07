# Citus Test Sync-Commit Sweep

This is a small validation run of the synchronous-commit sweep pipeline.

## Setup

- Cluster: current Citus node-0..node-4 layout
- Tables: distributed `pgbench` schema

## Benchmark

- Script: `scripts/run_pgbench_sync_commit_bench.py`
- Purpose: smoke-test the CSV/report/plot pipeline
- Scope: short run used before the main tuned benchmark

