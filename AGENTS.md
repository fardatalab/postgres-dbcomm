# Project setup background

This project uses two source trees:

- PostgreSQL fork: `/data/dbcomm/postgres-citus-separate-comm-stack`
- Citus/Homer fork: `/data/dbcomm/citus-dbcomm-separate-comm-stack`

The installed runtime prefix is `/data/dbcomm/pg-citus`. The runtime user is
`dbcomm`. Build or install commands may need to run as `dbcomm`, or with
`sudo -n`, depending on file ownership in the current checkout.

The project is a database systems research prototype for communication-stack
efficiency and offload. New Homer code should live under an appropriate
`homer/` subdirectory in the Postgres or Citus tree.

## Farnet topology

`farnet1` is the primary development and benchmark driver host. `farnet0` is the
peer host. Both machines have the same broad `/data/dbcomm` layout and the same
`dbcomm` runtime user. Access the peer with:

```sh
ssh farnet0
```

Current filesystem facts:

- `/data` on `farnet1` is local ext4.
- `/data` on `farnet0` is local ext4.
- Do not rely on NFS. `farnet1` may still advertise an old `/data` export, but
  normal workflow should use explicit `rsync`.
- Avoid syncing live database data directories unless PostgreSQL is stopped and
  replacing that data directory is the explicit goal.

Fast-link addresses used by the current runbooks:

- `farnet0`: `10.10.1.100`
- `farnet1`: `10.10.1.101`

Confirm with `ip -br addr` before a benchmark if the machines were rebooted or
renumbered.

## Build and install

When source changes are involved, assume both Postgres and Citus/Homer must be
rebuilt and reinstalled before validation unless the user explicitly asks for a
lighter experiment.

On `farnet1`:

```sh
cd /data/dbcomm/postgres-citus-separate-comm-stack
ninja -C build install

cd /data/dbcomm/citus-dbcomm-separate-comm-stack
make -j8
sudo -n make install-headers install-service-bin install
```

For Homer performance measurements, build the service/client library without
the stats macros:

```sh
cd /data/dbcomm/citus-dbcomm-separate-comm-stack
make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
sudo -n make install-headers install-service-bin install

cd /data/dbcomm/postgres-citus-separate-comm-stack
env CCACHE_DISABLE=1 ninja -C build src/backend/postgres src/bin/pg_basebackup/pg_basebackup
sudo -n meson install -C build --no-rebuild
```

For counter-based diagnosis, rebuild Citus/Homer with:

```sh
CPPFLAGS='-D_GNU_SOURCE -DHOMER_SERVICE_PAYLOAD_STATS=1 -DHOMER_CLIENT_BASEBACKUP_STATS=1'
```

If the install prefix is not writable by the current user, run the Postgres
install command with the same privilege style used for Citus:

```sh
sudo -n ninja -C /data/dbcomm/postgres-citus-separate-comm-stack/build install
```

Important installed artifacts include:

- `/data/dbcomm/pg-citus/bin/postgres`
- `/data/dbcomm/pg-citus/bin/pgbench`
- `/data/dbcomm/pg-citus/bin/pg_basebackup`
- `/data/dbcomm/pg-citus/bin/citus_tuple_sink_service`
- `/data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so`

## Sync farnet1 artifacts to farnet0

Use dry-run `rsync` first:

```sh
rsync -azn --delete --itemize-changes \
  /data/dbcomm/postgres-citus-separate-comm-stack/ \
  farnet0:/data/dbcomm/postgres-citus-separate-comm-stack/

rsync -azn --delete --itemize-changes \
  /data/dbcomm/citus-dbcomm-separate-comm-stack/ \
  farnet0:/data/dbcomm/citus-dbcomm-separate-comm-stack/

rsync -azn --delete --itemize-changes --exclude data/ \
  /data/dbcomm/pg-citus/ \
  farnet0:/data/dbcomm/pg-citus/
```

Remove `-n` only after the dry-run looks correct.

## Clean runtime baseline

Unless the goal is specifically to study warm reuse, start validation from a
clean baseline on both hosts.

On `farnet1`:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data stop -m fast || true

sudo -n pkill -f citus_tuple_sink_service || true
sudo -n pkill -f 'postgres: remote exec backend' || true
```

On `farnet0`:

```sh
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data stop -m fast || true"

ssh farnet0 "sudo -n pkill -f citus_tuple_sink_service || true"
ssh farnet0 "sudo -n pkill -f 'postgres: remote exec backend' || true"
```

If PostgreSQL later reports that another server might be running, inspect stale
IPC and remove only objects clearly owned by this prototype run:

```sh
ipcs -m
ls -lh /dev/shm | grep -E 'citus|homer|remote_exec'
```

Do not remove unrelated shared memory from other users' experiments.

## Process preflight for experiments

Before every performance, diagnostic, or rejection/acceptance run, check that the
runtime process set is clean on both hosts. Do this even after a successful
restart: aborted pgbench/basebackup runs and timed-out peer-control experiments
can leave client or socketless backend processes behind, and any result collected
on top of that state is contaminated.

On `farnet1`:

```sh
ps -eo pid,ppid,psr,comm,args | \
  grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' | \
  grep -v grep || true
```

On `farnet0`:

```sh
ssh farnet0 "ps -eo pid,ppid,psr,comm,args | \
  grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' | \
  grep -v grep || true"
```

For a clean experiment start, the only Homer-specific long-lived process should
be the intended `citus_tuple_sink_service` on each host, plus PostgreSQL
postmaster/background processes on hosts where PostgreSQL is intentionally
running. Do not start the measurement if an old `pgbench`, `pg_basebackup`,
`walsender`, or `postgres: remote exec backend` from a previous run is present.
Clean or restart first, then rerun the preflight and record that it was clean.

If a candidate times out, is interrupted, or needs manual process cleanup, discard
that run as diagnostic-only. Do not use it for acceptance/rejection performance
claims. Return to the clean runtime baseline, rerun the process preflight, and
then collect fresh warmed measurements.

## Start PostgreSQL

For the pgbench foreground workload and the basebackup sender, PostgreSQL is
needed on `farnet1`.

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log \
  start -w
```

For current `TARGET 'homer:mode=rdma,...'` basebackup blackhole tests,
`farnet0` only needs the Homer service. Start PostgreSQL on `farnet0` only if
the experiment also needs a database instance there.

## Start Homer services

Use code-level pinning instead of `taskset`. Keep CPU sets disjoint across:

- the standalone Homer service
- socketless Homer backends spawned by the service
- pgbench worker threads

Example `farnet1` service for pgbench and local basebackup sender:

```sh
sudo -n -u dbcomm env \
  HOMER_SERVICE_CPU=2 \
  HOMER_REMOTE_EXEC_BACKEND_CPUS=4,5,6,7 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.101 \
  HOMER_SERVICE_PEER_PORT=9717 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service \
  > /data/dbcomm/pg-citus/data/homer_service_farnet1.log 2>&1 &
```

Example `farnet0` receiver service for RDMA basebackup:

```sh
ssh farnet0 "sudo -n -u dbcomm env \
  HOMER_SERVICE_CPU=2 \
  HOMER_REMOTE_EXEC_BACKEND_CPUS=4,5,6,7 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.100 \
  HOMER_SERVICE_PEER_PORT=9717 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service \
  > /data/dbcomm/pg-citus/data/homer_service_farnet0.log 2>&1 &"
```

Two hosts can both listen on port `9717`. If two services are started on the
same host, give the second service a different `HOMER_SERVICE_PEER_PORT`,
`HOMER_SERVICE_CONTROL_SHM_NAME`, and `HOMER_SERVICE_SHM_TAG`.

## Prepare pgbench schema

Run this on `farnet1`. Citus is not required for the regular single-node
pgbench foreground workload.

```sh
SCALE=${SCALE:-1}
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm -i -s "$SCALE" postgres
```

Get the OIDs required by the current Homer pgbench prototype:

```sh
DBOID=$(
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
    -h /tmp -p 5432 -U dbcomm -AtX postgres \
    -c "select oid from pg_database where datname = 'postgres'"
)

USEROID=$(
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
    -h /tmp -p 5432 -U dbcomm -AtX postgres \
    -c "select oid from pg_roles where rolname = 'dbcomm'"
)

printf 'DBOID=%s USEROID=%s\n' "$DBOID" "$USEROID"
```

For performance comparisons, reset the insert-only history table and refresh
stats before each paired run group. A stale `pgbench_history` table with
millions of rows has already produced false Homer regression signals.

```sh
for sql in \
  "truncate pgbench_history" \
  "vacuum analyze pgbench_accounts" \
  "vacuum analyze pgbench_branches" \
  "vacuum analyze pgbench_tellers"; do
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
    -h /tmp -p 5432 -U dbcomm -v ON_ERROR_STOP=1 postgres \
    -c "$sql"
done
```

## Run pgbench through Homer

`pgbench --homer` supports two modes:

- farnet1-local: pgbench maps the farnet1 Homer service control shared memory,
  opens a local `CLIENT_SQL_SESSION`, and drives a socketless backend on
  farnet1.
- farnet0-to-farnet1 RDMA: pgbench maps the farnet0 Homer service, passes
  `--homer-peer-host 10.10.1.101`, and the two Homer services carry commands,
  completions, and tuple-result payloads over RDMA to the farnet1 PostgreSQL
  backend.

CPU placement matters for the current farnet1-local path because frontend,
service, and socketless backend busy-poll shared cache lines. On the current
farnet1 topology, CPUs `2`, `3`, and `4` share one L3 domain
(`/sys/devices/system/cpu/cpu*/cache/index3/shared_cpu_list` reports
`0-5,48-53`), while CPUs `8` and `9` are in another. For single-client local
microbenchmarks, use service CPU `2`, backend CPU `4`, and client CPU `3` if the
goal is the best local-control number. Use the multi-client CPU list only for
the multi-session workload and record it with the result.

Single-client smoke:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid "$DBOID" \
  --homer-user-oid "$USEROID" \
  --latency-percentiles \
  --client-cpu=3 \
  -n -M simple -c 1 -j 1 -t 1000 postgres
```

Remote RDMA single-client smoke from `farnet0` to farnet1:

```sh
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid '$DBOID' \
  --homer-user-oid '$USEROID' \
  --homer-peer-host 10.10.1.101 \
  --homer-peer-port 9717 \
  --homer-peer-node 1 \
  --latency-percentiles \
  --client-cpu=3 \
  -n -M simple -c 1 -j 1 -t 20000 postgres"
```

Multi-client foreground run:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid "$DBOID" \
  --homer-user-oid "$USEROID" \
  --latency-percentiles \
  --client-cpus=8,9,10,11 \
  -n -M simple -c 4 -j 4 -t 20000 postgres
```

Remote RDMA multi-client run from `farnet0` to farnet1:

```sh
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid '$DBOID' \
  --homer-user-oid '$USEROID' \
  --homer-peer-host 10.10.1.101 \
  --homer-peer-port 9717 \
  --homer-peer-node 1 \
  --latency-percentiles \
  --client-cpus=8,9,10,11 \
  -n -M simple -c 4 -j 4 -t 10000 postgres"
```

Use `--client-cpus=LIST` for multi-worker runs. `--client-cpu=CPU` pins every
pgbench worker to the same CPU and is only appropriate for single-client
reproducibility.

Current measurement caveat: warmed farnet0-to-farnet1 c1 is correct and has
been around `4.2k TPS` with p99 about `0.25 ms`. Real-RDMA c4 correctness holds
but scaling is poor, around `4.8k TPS` with p95 around `4 ms`; treat that as a
known shared RDMA command/completion transport bottleneck, not as the final
multi-client result.

Libpq baseline with the same pgbench worker placement:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --latency-percentiles \
  --client-cpus=8,9,10,11 \
  -n -M simple -c 4 -j 4 -t 20000 postgres
```

## Run basebackup through Homer

Current basebackup constraints:

- Use `-X none`; WAL streaming/fetch is not yet Homer.
- `TARGET 'homer:mode=blackhole,...'` exercises Homer locally and discards in
  the service.
- PostgreSQL's built-in `TARGET 'blackhole'` is not a Homer path.
- For remote RDMA blackhole, use `TARGET 'homer:mode=rdma,host=...,port=...'`.

Performance measurement rule:

- Treat the first run after a PostgreSQL/service restart, binary sync, queue
  geometry change, or peer-connection setup as a warmup run. Do not use that
  cold first run as the regression/performance number.
- The number we care about is warmed steady state: run the same command
  repeatedly without restarting PostgreSQL or either Homer service, and compare
  the stable repeat band.
- Prefer a workload that lasts at least about 5 seconds, or aggregate several
  warmed repeats if the individual run is shorter. Record the full repeat set,
  not only the best run.
- When comparing local Homer blackhole and remote RDMA blackhole, warm both
  paths separately before comparing. Local blackhole is the producer/service
  baseline; remote RDMA adds the peer transport path.

Local Homer blackhole smoke on `farnet1`:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=blackhole,node=1' \
  -v
```

Remote RDMA blackhole from `farnet1` to the `farnet0` service:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2' \
  -v
```

Optional explicit queue geometry for sensitivity runs:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
  -v
```

Producer-publication note:

- basebackup now uses a producer-owned byte ring and publishes each committed
  record immediately to preserve pipeline overlap.
- the old `publish=N` target-detail knob is retained for command-line
  compatibility, but it no longer controls producer batching on the byte-ring
  basebackup path.

Compatibility example:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608,publish=1' \
  -v
```

Warm-run pattern for the remote RDMA path:

```sh
for run in 1 2 3 4; do
  echo "remote-rdma-basebackup run=$run"
  /usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
    -h /tmp -p 5432 -U dbcomm \
    -X none -c fast \
    -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
    -v
done
```

Use `run=1` as warmup unless the experiment explicitly studies cold setup.
Compare `run=2..4` against local blackhole warm repeats.

## Run foreground pgbench with background basebackup

This reproduces the intended network-interference shape: transaction traffic is
the foreground workload and one or more basebackup streams are background
traffic. In the current milestone, foreground pgbench can use Homer end to end
for its measured transaction commands, while basebackup uses Homer for the
archive/manifest data plane with `-X none`.

Current caveat: the first post-restart run establishes peer lanes, registers
memory, and warms PostgreSQL/service state. Use it as warmup. For performance,
compare repeat runs without restarting PostgreSQL or either Homer service.

Single-client foreground pgbench from `farnet0` while one remote RDMA
basebackup runs from `farnet1` to the `farnet0` Homer service:

```sh
OUT=/tmp/homer_concurrent_$(date +%s)
mkdir -p "$OUT"

(
  /usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
    -h /tmp -p 5432 -U dbcomm \
    -X none -c fast \
    -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
    -v
) > "$OUT/basebackup.log" 2>&1 &
BB_PID=$!

sleep 0.5

ssh farnet0 \
  "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
     -h /tmp -p 5432 -U dbcomm \
     --homer \
     --homer-database-oid 5 \
     --homer-user-oid 10 \
     --homer-peer-host 10.10.1.101 \
     --homer-peer-port 9717 \
     --homer-peer-node 1 \
     --latency-percentiles \
     --client-cpu=3 \
     -n -M simple -c 1 -j 1 -t 10000 postgres" \
  > "$OUT/pgbench.log" 2>&1
PG_RC=$?

wait "$BB_PID"
BB_RC=$?

printf 'pgbench_rc=%s basebackup_rc=%s artifacts=%s\n' "$PG_RC" "$BB_RC" "$OUT"
tail -40 "$OUT/pgbench.log"
tail -40 "$OUT/basebackup.log"
```

Four-client foreground pgbench uses the same background basebackup command and
changes only the pgbench client/thread and CPU placement. This is a correctness
shape for the current milestone; do not treat its throughput as a solved
scaling result until the command/control scaling issue is addressed.

```sh
OUT=/tmp/homer_concurrent_c4_$(date +%s)
mkdir -p "$OUT"

(
  /usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
    -h /tmp -p 5432 -U dbcomm \
    -X none -c fast \
    -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
    -v
) > "$OUT/basebackup.log" 2>&1 &
BB_PID=$!

sleep 0.5

ssh farnet0 \
  "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
     -h /tmp -p 5432 -U dbcomm \
     --homer \
     --homer-database-oid 5 \
     --homer-user-oid 10 \
     --homer-peer-host 10.10.1.101 \
     --homer-peer-port 9717 \
     --homer-peer-node 1 \
     --latency-percentiles \
     --client-cpus=3,4,5,6 \
     -n -M simple -c 4 -j 4 -t 2500 postgres" \
  > "$OUT/pgbench.log" 2>&1
PG_RC=$?

wait "$BB_PID"
BB_RC=$?

printf 'pgbench_rc=%s basebackup_rc=%s artifacts=%s\n' "$PG_RC" "$BB_RC" "$OUT"
tail -40 "$OUT/pgbench.log"
tail -40 "$OUT/basebackup.log"
```

For a libpq foreground comparison under the same background basebackup load,
rerun the same background loop and replace the pgbench command with the libpq
baseline from the previous section.

## Run Citus backend-to-backend COPY through Homer

This workload exercises the Citus coordinator backend on `farnet1`, the
standalone Homer services on both hosts, and a socketless Citus worker backend on
`farnet0`. It is the current service-to-service tuple payload path. The normal
terminal command-completion path now uses the peer completion ring; the old peer
`POLL_COMMAND_COMPLETION` path is retained as fallback/debug behavior.

Prerequisites:

- PostgreSQL must be running on both `farnet1` and `farnet0`.
- Homer services must be running on both hosts with the normal peer bind
  addresses from the "Start Homer services" section.
- The process preflight must show no stale `remote exec backend`, `psql`,
  `pgbench`, or old Homer service process from an earlier run.
- Keep verbose Homer logging and diagnostic stats macros off for performance
  runs.

Prepare the input file on `farnet1`:

```sh
awk 'BEGIN {
  for (i = 1; i <= 10000000; i++)
    printf "%d,%d,payload_%08d\n", i, i * 10, i
}' > /tmp/homer_tuple_sink_copy_10m.csv

wc -l /tmp/homer_tuple_sink_copy_10m.csv
ls -lh /tmp/homer_tuple_sink_copy_10m.csv
```

Create the distributed table and verify that its single placement lands on
`10.10.1.100`:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
  -h /tmp -p 5432 -U dbcomm -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SET client_min_messages=notice;
SET citus.shard_count=1;
DROP TABLE IF EXISTS homer_tuple_sink_copy_bench;
CREATE TABLE homer_tuple_sink_copy_bench (id int, v bigint, payload text);
SELECT create_distributed_table('homer_tuple_sink_copy_bench', 'id');
SELECT p.shardid, n.nodename, n.nodeport
FROM pg_dist_placement p JOIN pg_dist_node n ON p.groupid = n.groupid
WHERE p.shardid IN (
  SELECT shardid
  FROM pg_dist_shard
  WHERE logicalrelid = 'homer_tuple_sink_copy_bench'::regclass
)
ORDER BY p.shardid, n.nodename;
SQL
```

Warm once, then use repeat runs as the baseline:

```sh
OUT=/tmp/homer_b2b_copy_10m_baseline_$(date +%s)
mkdir -p "$OUT"

for run in 1 2 3 4; do
  {
    echo "backend-to-backend-homer-copy-10m run=$run"
    /usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
      -h /tmp -p 5432 -U dbcomm -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SET client_min_messages=notice;
SET citus.enable_experimental_tuple_sink_routing=on;
SET citus.enable_experimental_tuple_sink_loopback_validation=off;
TRUNCATE homer_tuple_sink_copy_bench;
\copy homer_tuple_sink_copy_bench FROM '/tmp/homer_tuple_sink_copy_10m.csv' WITH (FORMAT csv);
SELECT count(*), min(id), max(id), sum(v) FROM homer_tuple_sink_copy_bench;
SQL
  } > "$OUT/run_${run}.log" 2>&1
  echo "run=$run rc=$? log=$OUT/run_${run}.log"
done

printf 'artifacts=%s\n' "$OUT"
for f in "$OUT"/run_*.log; do
  echo "--- $f"
  tail -20 "$f"
done
```

Current post-peer-push baseline captured on June 6, 2026:

- Input: `/tmp/homer_tuple_sink_copy_10m.csv`, 10,000,000 rows, about 323 MB.
- Correctness check: `count=10000000`, `min=1`, `max=10000000`,
  `sum=500000050000000` on every repeat.
- Default Homer tuple-sink geometry is now `8192` tuples and `524288` bytes.
- Warmup after rebuild/sync: `real 11.47`.
- Measured repeats: `real 7.46`, `7.62`, `7.32`; average repeat time about
  `7.47 s`, about `1.34M rows/s`.
- Raw artifacts: `/tmp/homer_b2b_copy_10m_default_after_batch_default_1780784020`.
- Vanilla Citus on the same 10M-row input measured `5.18`, `5.11`, and `5.11 s`
  in `/tmp/citus_vanilla_copy_10m_baseline_1780783569`. Treat the remaining
  Homer gap as payload-loop/tuple-record overhead, not as terminal peer
  completion polling.

Discard the run if a timeout, interrupted client, or failed COPY leaves a stale
`postgres: remote exec backend`. Return to the clean runtime baseline and rerun
the process preflight before measuring again.

## Quick validation checklist

After each run, capture:

```sh
ps -eo pid,ppid,psr,comm,args | \
  grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup' | \
  grep -v grep || true

grep -E 'transport:|tps|latency|p50|p95|p99|number of failed transactions' \
  /data/dbcomm/pg-citus/homer_runs/*/*.log 2>/dev/null || true

tail -n 80 /data/dbcomm/pg-citus/data/homer_service_farnet1.log
ssh farnet0 "tail -n 80 /data/dbcomm/pg-citus/data/homer_service_farnet0.log"
```

For performance runs, keep verbose Homer logging compile-time switches off.
Control-path diagnostics are useful for debugging, but logging on the measured
path has caused misleading throughput regressions in this prototype.
