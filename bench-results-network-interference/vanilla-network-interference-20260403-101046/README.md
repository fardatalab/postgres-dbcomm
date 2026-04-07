# Vanilla Network-Interference Sweep

This run measures the foreground `pgbench` impact of concurrent background SQL
traffic on a vanilla PostgreSQL primary/standby pair.

## Setup

- Primary: node-0 on the `10.10.1.x` fabric
- Standby: node-3, streaming from node-0
- Generator host: node-1
- Foreground tables: standard `pgbench` schema in `pgbench_vanilla`
- Background read source: local `pgbench_accounts`
- Background write sink: local `bg_sink`

## Traffic pattern

- Foreground workload: `pgbench` at `-c 16`, `-j 8`, `-M prepared`
- Foreground modes: `off`, `local`, `on`, `remote_write`, `remote_apply`
- Standby waiting: `synchronous_standby_names = FIRST 1 (vanilla_stby)`
- Background readers: repeated `COPY (SELECT * FROM pgbench_accounts) TO STDOUT`
- Background writers: repeated `COPY bg_sink (id, payload) FROM STDIN`
- Background intensity sweep: 0, 1, 2, 4, and 8 reader/writer sessions per
  direction

## Outputs

- `results.csv`

