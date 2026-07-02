## Cluster Setup

- This branch documents the DPU repartition-join measurement setup, not the
  older pgbench transaction-latency benchmark setup.
- Use PostgreSQL branch `pg-17-timings-latency-dpu` with Citus branch
  `timings-latency-dpu`.
- The active PostgreSQL cluster has exactly three nodes:
  - coordinator: `farnet1` host OS, `10.10.1.101` on `enp33s0f0np0`
  - worker: `farnet1-bf3-a` DPU OS, `10.10.1.201` on `enp3s0f0s0`
  - worker: `farnet0-bf3-a` DPU OS, `10.10.1.200` on `enp3s0f0s0`
- `farnet0` host OS is only the management host for its DPU worker; it is not a
  PostgreSQL worker for this setup.
- The DPU management path is host-local `tmfifo_net0`: from `farnet1`, use
  `ubuntu@192.168.100.2` for `farnet1-bf3-a`; from `farnet0`, use
  `ubuntu@192.168.100.2` for `farnet0-bf3-a`.
- Do not configure physical replicas for this measurement. Avoid
  `citus_add_secondary_node()`, `pg_basebackup`, replication slots,
  synchronous replica-wait settings, and replica-specific `pg_hba.conf` entries
  unless a separate experiment explicitly reintroduces physical replication.

## Build And Install

- The install prefix for both PostgreSQL and Citus on every PostgreSQL node is
  `~/pg`.
- If a PostgreSQL or Citus build tree was copied from another machine,
  reconfigure it before installing so generated paths are rewritten for the
  current node.
- For PostgreSQL, reconfigure the Meson build under `build/` with the `~/pg`
  prefix, then install from that build tree.
- For Citus, rerun `./configure PG_CONFIG=$HOME/pg/bin/pg_config` from the
  Citus checkout, then rebuild and install so `Makefile.global`,
  `config.status`, and cached build metadata point at the node-local
  PostgreSQL installation.
- Before validating, remove stale copied install artifacts if they can shadow
  the current build. A clean rebuild is the reliable way to make the Citus
  preload match this PostgreSQL tree.

## PostgreSQL Configuration

- Apply configuration in the active data directory for the `~/pg` install on
  each node, usually `~/pg/data/postgresql.conf` and `~/pg/data/pg_hba.conf`.
- Set these values in `postgresql.conf` on all three PostgreSQL nodes:

```conf
listen_addresses = '*'
shared_preload_libraries = 'citus'
logging_collector = on
log_destination = 'stderr'
log_directory = 'log'
log_filename = 'postgresql-%Y-%m-%d_%H%M%S.log'
citus.enable_repartition_joins = on
```

- Restart PostgreSQL after changing `shared_preload_libraries` or
  `logging_collector`; a reload is not enough for those settings.
- Allow normal PostgreSQL traffic among the three `10.10.1.0/24` fabric
  addresses in `pg_hba.conf`, for example:

```conf
host    all     all     10.10.1.0/24     trust
```

- Use stricter authentication if the fabric is not isolated. The repartition-join
  measurement itself does not need physical-replica-specific `pg_hba.conf`
  entries.

## Citus Metadata

- Start PostgreSQL from `~/pg` on all three nodes and verify that
  `CREATE EXTENSION IF NOT EXISTS citus;` succeeds.
- On the coordinator, set the coordinator host and add the two DPU workers:

```sql
CREATE EXTENSION IF NOT EXISTS citus;
SELECT citus_set_coordinator_host('10.10.1.101', 5432);
SELECT citus_add_node('10.10.1.201', 5432);
SELECT citus_add_node('10.10.1.200', 5432);
```

- If the coordinator still has metadata from an older setup, remove or update
  stale node rows before running measurements. Verify the result with:

```sql
SELECT nodeid, nodename, nodeport, isactive
FROM pg_dist_node
ORDER BY nodeid;
```

Only `10.10.1.201` and `10.10.1.200` should appear as active Citus workers for
this setup.

## TPC-H Data Preparation

- The intended TPC-H scale factor for the measured repartition-join runs is
  SF10. The local data lives under `/data/dbcomm/tpch-kit/dbgen/dss-output`, and
  the database name used by the existing helper scripts is `tpch_sf10`.
- Create the schema from `/data/dbcomm/tpch-kit/dbgen/dss.ddl` on the
  coordinator, then distribute tables before loading data:

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

- Keep `orders`/`lineitem` colocated on order key and `customer` distributed on
  customer key for Q3-style repartition measurements. Changing those keys can
  turn the measured query into a different plan shape.
- Load `.tbl` files with `sed 's/|$//'` piped into psql `\copy`, because TPC-H
  data files have a trailing `|` field separator that PostgreSQL CSV input
  should not see.
- Run `ANALYZE` after loading and verify SF10 row counts: `region` 5, `nation`
  25, `supplier` 100000, `part` 2000000, `partsupp` 8000000, `customer`
  1500000, `orders` 15000000, and `lineitem` 59986052.
- Use fixed query files from `/data/dbcomm/tpch-queries`, not the raw
  `/data/dbcomm/tpch-kit/dbgen/queries-sf10` files, unless the raw generated SQL
  has been repaired first.

## Measurement Artifacts

- The spreadsheet rows are assembled manually from script outputs, not generated
  directly by one spreadsheet exporter.
- Use `fdl_utils/aggregate_timing.py --view summary` on each node's collector log
  from the same measured repartition-join run.
- For kernel TCP/IP and socket-stack CPU, start `perf record` manually in a
  separate terminal on the node being measured:

```bash
sudo perf record -a -F 200 -e cpu-clock:k -g --kernel-callchains -P -o perf.data
```

- Stop perf with `Ctrl-C` after the measured window, then run:

```bash
sudo perf script -i perf.data -F comm,period,event,ip,sym,dso > perf.txt
python3 fdl_utils/sum_net.py perf.txt --aggregate-csv <same-node-timing-summary.csv>
```

- Keep the perf capture and timing summary paired by node and measurement
  window. A coordinator perf run should not be divided by a worker timing
  summary, and a wide perf window should not be paired with a narrow query log.
