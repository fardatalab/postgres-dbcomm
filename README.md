# DPU Repartition-Join Measurement Runbook

This repository is a PostgreSQL 17 tree plus a copied Citus tree for the DPU
repartition-join communication-stack measurements. It is not the runbook for the
older pgbench transaction-latency benchmark result trees.

The current branch layout is:

- PostgreSQL: `pg-17-timings-latency-dpu`
- Citus: copied or paired branch `timings-latency-dpu`

## Cluster Layout

The intended cluster has one coordinator and two active Citus workers. The
runbook should not use physical replicas, `citus_add_secondary_node()`,
`pg_basebackup`, or synchronous-replica settings for the repartition-join
measurements.

| Role | Machine | PostgreSQL address | Notes |
| --- | --- | --- | --- |
| Coordinator | `farnet1` host OS | `10.10.1.101` on `enp33s0f0np0` | Use this address in Citus metadata. |
| Worker 1 | `farnet1-bf3-a` DPU OS | `10.10.1.201` on `enp3s0f0s0` | Reachable from `farnet1` as `ubuntu@192.168.100.2` over `tmfifo_net0`. |
| Worker 2 | `farnet0-bf3-a` DPU OS | `10.10.1.200` on `enp3s0f0s0` | Reachable from `farnet0` as `ubuntu@192.168.100.2` over `tmfifo_net0`. |

The host-side fast fabric is `10.10.1.0/24`; the coordinator address is the
`farnet1` host address on that fabric. `farnet0` itself is not a PostgreSQL node
for this measurement; it is only the host used to manage the `farnet0` DPU.

## Build And Install

Install PostgreSQL and Citus under `~/pg` on all three PostgreSQL nodes:

- `farnet1`
- `farnet1-bf3-a`
- `farnet0-bf3-a`

For PostgreSQL, work from `pg-17-timings-latency-dpu`, reconfigure the Meson
build tree under `build/` for the `~/pg` prefix, and install from that build
tree.

For Citus, work from `timings-latency-dpu`, then rebuild and install against the
matching PostgreSQL install:

```bash
./configure PG_CONFIG=$HOME/pg/bin/pg_config
make -j"$(nproc)"
make install
```

If a build tree was copied from another machine, reconfigure and rebuild before
installing. The generated Citus makefiles and cached PostgreSQL paths must point
at the local `~/pg` installation on the node where Citus is being installed.

## PostgreSQL Configuration

Use the data directory for the local `~/pg` installation on each node. In the
usual local layout this is `~/pg/data`; if a node uses a different `PGDATA`,
apply the same edits in that directory instead.

Set the following in `postgresql.conf` on all three PostgreSQL nodes:

```conf
listen_addresses = '*'
shared_preload_libraries = 'citus'
logging_collector = on
log_destination = 'stderr'
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
citus.enable_repartition_joins = on
```

`shared_preload_libraries` and `logging_collector` require a PostgreSQL restart.
The collector setting is important because the timing scripts read PostgreSQL
server log files; stdout-only postmaster output is not enough for a reproducible
artifact pipeline.

Allow normal PostgreSQL connections among the three `10.10.1.0/24` addresses in
`pg_hba.conf` on all three nodes. A broad lab-only entry is:

```conf
host    all     all     10.10.1.0/24     trust
```

Use a stricter authentication method if the cluster is exposed beyond the lab
fabric. Physical-replica-specific `pg_hba.conf` entries are not needed for this
three-node repartition-join setup.

## Citus Metadata

Start PostgreSQL on all three nodes, then initialize Citus metadata from the
coordinator on `farnet1`:

```sql
CREATE EXTENSION IF NOT EXISTS citus;
SELECT citus_set_coordinator_host('10.10.1.101', 5432);
SELECT citus_add_node('10.10.1.201', 5432);
SELECT citus_add_node('10.10.1.200', 5432);
```

If metadata already exists from an older setup, update or remove stale rows
instead of adding duplicates:

```sql
SELECT citus_update_node(nodeid, '10.10.1.201', 5432)
FROM pg_dist_node
WHERE nodename IN ('10.10.1.201', 'old-worker-1-address');

SELECT citus_update_node(nodeid, '10.10.1.200', 5432)
FROM pg_dist_node
WHERE nodename IN ('10.10.1.200', 'old-worker-2-address');
```

After metadata changes, verify that only the two DPU workers are active:

```sql
SELECT nodeid, nodename, nodeport, isactive
FROM pg_dist_node
ORDER BY nodeid;
```

## TPC-H Data Preparation

The measured repartition-join runs use TPC-H scale factor 10. The local evidence
for that is the existing database/load-script name `tpch_sf10`, the generated
query directory `/data/dbcomm/tpch-kit/dbgen/queries-sf10`, and the SF10 row
counts already present under `/data/dbcomm/tpch-kit/dbgen/dss-output`.

Prepare the database on the coordinator:

```bash
createdb -h 10.10.1.101 -p 5432 tpch_sf10
psql -h 10.10.1.101 -p 5432 -d tpch_sf10 -v ON_ERROR_STOP=1 \
  -f /data/dbcomm/tpch-kit/dbgen/dss.ddl
```

Then create the Citus metadata before loading data so `COPY` routes rows to the
two DPU workers. Keep `orders` and `lineitem` colocated on order key, and keep
`customer` distributed on customer key; that distribution is what makes Q3-style
`customer`/`orders` joins exercise repartitioning instead of turning into a
fully colocated join.

```sql
CREATE EXTENSION IF NOT EXISTS citus;
SET citus.shard_count = 32;

SELECT create_reference_table('region');
SELECT create_reference_table('nation');

SELECT create_distributed_table('customer', 'c_custkey', shard_count := 32);
SELECT create_distributed_table('orders', 'o_orderkey', shard_count := 32);
SELECT create_distributed_table('lineitem', 'l_orderkey', shard_count := 32);
SELECT create_distributed_table('part', 'p_partkey', shard_count := 32);
SELECT create_distributed_table('supplier', 's_suppkey', shard_count := 32);
SELECT create_distributed_table('partsupp', 'ps_partkey', shard_count := 32);
```

Load the SF10 data from the coordinator or from another machine that can connect
to the coordinator and read the data files:

```bash
TBL_DIR=/data/dbcomm/tpch-kit/dbgen/dss-output
for table_file in "$TBL_DIR"/*.tbl; do
  table_name=$(basename "$table_file" .tbl)
  echo "loading $table_name"
  sed 's/|$//' "$table_file" |
    psql -h 10.10.1.101 -p 5432 -d tpch_sf10 -v ON_ERROR_STOP=1 \
      -c "\\copy $table_name FROM STDIN WITH (FORMAT csv, DELIMITER '|')"
done
```

Do not apply `/data/dbcomm/tpch-kit/dbgen/dss.ri` for this measurement path. It
uses `TPCD.` schema-qualified constraint statements and the constraints are not
needed for the repartition-join timing runs.

After loading, gather statistics and verify the SF10 row counts:

```sql
ANALYZE;

SELECT 'region' AS table_name, count(*) FROM region
UNION ALL SELECT 'nation', count(*) FROM nation
UNION ALL SELECT 'supplier', count(*) FROM supplier
UNION ALL SELECT 'part', count(*) FROM part
UNION ALL SELECT 'partsupp', count(*) FROM partsupp
UNION ALL SELECT 'customer', count(*) FROM customer
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'lineitem', count(*) FROM lineitem
ORDER BY table_name;
```

Expected SF10 counts are:

| Table | Rows |
| --- | ---: |
| `region` | 5 |
| `nation` | 25 |
| `supplier` | 100000 |
| `part` | 2000000 |
| `partsupp` | 8000000 |
| `customer` | 1500000 |
| `orders` | 15000000 |
| `lineitem` | 59986052 |

Use the fixed query files under `/data/dbcomm/tpch-queries` for measurement
runs, for example:

```bash
psql -h 10.10.1.101 -p 5432 -d tpch_sf10 \
  -f /data/dbcomm/tpch-queries/tpch-q3.sql
```

The raw generated files under `/data/dbcomm/tpch-kit/dbgen/queries-sf10` may
still contain standalone `; LIMIT ...` fragments, so do not use that directory
directly unless you regenerate and fix the SQL first.

## Measurement Artifacts

The spreadsheet-style communication breakdown is assembled manually from script
outputs. There is no single script that writes the spreadsheet.

For each measured PostgreSQL node, collect the server log for the same workload
window and run:

```bash
python3 fdl_utils/aggregate_timing.py \
  logs/farnet1.log \
  --view summary \
  > farnet1-timing-summary.csv
```

Repeat that for the two DPU worker logs, for example:

```bash
python3 fdl_utils/aggregate_timing.py logs/farnet1-bf3-a.log --view summary > farnet1-bf3-a-timing-summary.csv
python3 fdl_utils/aggregate_timing.py logs/farnet0-bf3-a.log --view summary > farnet0-bf3-a-timing-summary.csv
```

The rows copied into `repartition join.xlsx` come from these timing summaries
plus the optional kernel-network summaries below. Keep the log names and output
locations run-specific; the scripts do not require a fixed result-directory
layout.

## Kernel TCP/IP And Socket-Stack CPU

Run `perf record` manually in a separate terminal on the node being measured:

```bash
sudo perf record -a -F 200 -e cpu-clock:k -g --kernel-callchains -P -o perf.data
```

Start it immediately before the measured query or workload window, stop it with
`Ctrl-C` after the window ends, then dump and parse the samples:

```bash
sudo perf script -i perf.data -F comm,period,event,ip,sym,dso > perf.txt
python3 fdl_utils/sum_net.py \
  perf.txt \
  --aggregate-csv farnet1-timing-summary.csv \
  > farnet1-kernel-net-summary.txt
```

The `perf.txt` file and timing summary must come from the same node and the same
measurement window. `sum_net.py` reports `sampled_kernel_net_ns`,
`kernel_net_avg_cores`, and `kernel_net_pct_of_query_active_wall`; those values
are the perf-derived inputs for the spreadsheet.

## Reproduction Checklist

1. Check out `pg-17-timings-latency-dpu` in this PostgreSQL tree.
2. Check out or create the paired Citus worktree on `timings-latency-dpu`.
3. Build and install PostgreSQL and Citus under `~/pg` on `farnet1`,
   `farnet1-bf3-a`, and `farnet0-bf3-a`.
4. Edit `~/pg/data/postgresql.conf` and `~/pg/data/pg_hba.conf` on all three
   PostgreSQL nodes as described above, then restart PostgreSQL.
5. Initialize or repair Citus metadata so the coordinator is `10.10.1.101` and
   the only active workers are `10.10.1.201` and `10.10.1.200`.
6. Prepare `tpch_sf10` from the SF10 TPC-H data and fixed query files.
7. Run the repartition-join workload on the coordinator.
8. For each node that contributes to the spreadsheet, keep the matching
   collector log, run `aggregate_timing.py`, and optionally run the manual
   `perf` plus `sum_net.py` workflow.
9. Copy the relevant timing and kernel-network summary values into the
   spreadsheet by hand.
