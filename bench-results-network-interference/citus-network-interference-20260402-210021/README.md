# Citus Network-Interference Sweep, Full Mode Matrix

This run measures the foreground `pgbench` impact of concurrent background SQL
traffic on the Citus cluster across the full synchronous-commit matrix.

## Setup

- Cluster: node-0 restored as a Citus coordinator on the `10.10.1.x` fabric
- Citus topology: node-1 and node-2 are worker primaries; node-3 and node-4
  are their physical standbys
- Foreground tables: distributed `pgbench` schema
- Background read source: distributed `pgbench_accounts`
- Background write sink: distributed `bg_sink`

## Traffic pattern

- Foreground workload: `pgbench` at `-c 16`
- Foreground modes: `off`, `local`, `on`, `remote_write`, `remote_apply`
- Worker primaries: configured with `synchronous_standby_names = FIRST 1 (...)`
  so the synchronous-commit modes actually wait for the physical standbys
- Background readers: repeated `COPY (SELECT * FROM pgbench_accounts) TO STDOUT`
- Background writers: repeated `COPY bg_sink (id, payload) FROM STDIN`
- Background intensity sweep: 1, 2, 4, and 8 reader/writer sessions per
  direction

## Outputs

- `results.csv`
- `plots/`
