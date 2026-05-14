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
  HOMER_SERVICE_PEER_PORT=9717 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service \
  > /data/dbcomm/pg-citus/data/homer_service_farnet1.log 2>&1 &
```

Example `farnet0` receiver service for RDMA basebackup:

```sh
ssh farnet0 "sudo -n -u dbcomm env \
  HOMER_SERVICE_CPU=2 \
  HOMER_REMOTE_EXEC_BACKEND_CPUS=4,5,6,7 \
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

Single-client smoke:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid "$DBOID" \
  --homer-user-oid "$USEROID" \
  --latency-percentiles \
  --client-cpu=8 \
  -n -M simple -c 1 -j 1 -t 1000 postgres
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

Use `--client-cpus=LIST` for multi-worker runs. `--client-cpu=CPU` pins every
pgbench worker to the same CPU and is only appropriate for single-client
reproducibility.

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

## Run foreground pgbench with background basebackup

This reproduces the intended network-interference shape: transaction traffic is
the foreground workload and one or more basebackup streams are background
traffic. In the current milestone, foreground pgbench can use Homer end to end
for its measured transaction commands, while basebackup uses Homer for the
archive/manifest data plane with `-X none`.

On `farnet1`:

```sh
OUT=/data/dbcomm/pg-citus/homer_runs/$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUT"

for stream in 1 2; do
  /usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
    -h /tmp -p 5432 -U dbcomm \
    -X none -c fast \
    -t "homer:mode=rdma,host=10.10.1.100,port=9717,node=$((20 + stream))" \
    -v > "$OUT/basebackup_${stream}.log" 2>&1 &
done

sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer \
  --homer-database-oid "$DBOID" \
  --homer-user-oid "$USEROID" \
  --latency-percentiles \
  --client-cpus=8,9,10,11 \
  -n -M simple -c 4 -j 4 -t 20000 postgres \
  > "$OUT/pgbench_homer.log" 2>&1

wait
printf 'artifacts: %s\n' "$OUT"
```

For a libpq foreground comparison under the same background basebackup load,
rerun the same background loop and replace the pgbench command with the libpq
baseline from the previous section.

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
