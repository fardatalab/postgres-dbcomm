# Citus Network-Interference Sweep, Preliminary Local-Only Pass

This run was an initial Citus interference pass while the experiment harness
was still being brought up. It only used `synchronous_commit = local`, so it is
useful as a sanity check but not the final cross-mode comparison.

## Setup

- Cluster: node-0 restored as a Citus coordinator on the `10.10.1.x` fabric
- Foreground tables: distributed `pgbench` schema
- Background read source: distributed `pgbench_accounts`
- Background write sink: distributed `bg_sink`

## Traffic pattern

- Foreground workload: `pgbench` at `-c 16`
- Foreground mode: `synchronous_commit = local`
- Background readers: repeated `COPY (SELECT * FROM pgbench_accounts) TO STDOUT`
- Background writers: repeated `COPY bg_sink (id, payload) FROM STDIN`
- Background intensity sweep: 1, 2, 4, and 8 reader/writer sessions per direction

## Outputs

- `results.csv`
- `plots/`
