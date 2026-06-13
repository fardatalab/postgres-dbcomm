# Homer Current Reference Measurements - 2026-06-12

This records ballpark measurements for the current Homer code before adding the
unscheduled fixed-loop baselines. The goal is to catch grossly suspicious future
numbers, not to claim final tuned performance.

## Build and Runtime Identity

- Measurement host: `farnet1`, with peer `farnet0`.
- Raw artifacts: `/tmp/homer_reference_20260612_193009`.
- Postgres branch: `homer-unscheduled-baselines`.
- Postgres commit: `9a8d6afc7cdcb0b6c27710c247b2f98e170c0454`.
- Citus/Homer branch: `homer-unscheduled-baselines`.
- Citus/Homer commit: `04b70c2eca1e4bd6835405d7fda25191975cc9ac`.
- Installed runtime: `/data/dbcomm/pg-citus`.
- Installed versions: `postgres (PostgreSQL) 17.6`, `pgbench (PostgreSQL) 17.6`,
  `pg_basebackup (PostgreSQL) 17.6`.
- Pgbench identifiers: `DBOID=5`, `USEROID=10`.
- Services:
  - `farnet1`: `HOMER_SERVICE_CPU=2`, backend CPUs `4,5,6,7`, bind
    `10.10.1.102:9717`.
  - `farnet0`: `HOMER_SERVICE_CPU=2`, backend CPUs `4,5,6,7`, bind
    `10.10.1.100:9717`.

Important caveat: this build printed timing report blocks during `psql`
commands. Treat the numbers below as ballpark only; before final experiments,
rebuild with measurement-noise instrumentation disabled and rerun the reference
set.

## Pgbench Reference

`pgbench` used scale `1`, simple query mode, and latency percentiles. The local
libpq runs are same-host `farnet1` through `/tmp`; remote Homer runs execute
`pgbench` on `farnet0` and connect to the `farnet1` service over RDMA.

| Workload | Command shape | Result |
| --- | --- | --- |
| libpq c1 | `-c 1 -j 1 -t 5000`, `--client-cpu=3` | `3096 TPS`, avg `0.323 ms`, p95 `0.437 ms`, p99 `0.471 ms`, max `4.199 ms` |
| libpq c4 | `-c 4 -j 4 -t 2500`, `--client-cpus=8,9,10,11` | `6746 TPS`, avg `0.593 ms`, p95 `0.784 ms`, p99 `0.942 ms`, max `4.568 ms` |
| remote Homer c1 run 1 | `-c 1 -j 1 -t 5000`, `--client-cpu=3` | `3669 TPS`, avg `0.273 ms`, p95 `0.282 ms`, p99 `0.295 ms`, max `7.307 ms` |
| remote Homer c1 run 2 | same | `3694 TPS`, avg `0.271 ms`, p95 `0.278 ms`, p99 `0.290 ms`, max `6.490 ms` |
| remote Homer c1 run 3 | same | `3968 TPS`, avg `0.252 ms`, p95 `0.259 ms`, p99 `0.271 ms`, max `6.595 ms` |
| remote Homer c4 run 1 | `-c 4 -j 4 -t 2500`, `--client-cpus=8,9,10,11` | `8670 TPS`, avg `0.461 ms`, p95 `0.601 ms`, p99 `0.688 ms`, max `15.784 ms` |
| remote Homer c4 run 2 | same | `8698 TPS`, avg `0.460 ms`, p95 `0.598 ms`, p99 `0.684 ms`, max `14.267 ms` |
| remote Homer c4 run 3 | same | `8582 TPS`, avg `0.466 ms`, p95 `0.606 ms`, p99 `0.698 ms`, max `15.848 ms` |

Local Homer pgbench was accidentally probed and did not produce a valid number:

- `pgbench --homer` on `farnet1` failed with
  `client 0 Homer sql_execute failed: Homer control request failed`.
- This is not a baseline workload we plan to use. Ignore it for baseline
  comparisons unless we later decide to debug local-only Homer SQL behavior.
- The remote Homer pgbench path did work, so this was not global Homer SQL
  breakage.

## Basebackup Reference

All basebackup runs used `-X none -c fast`. Remote RDMA used
`TARGET 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608'`.

| Workload | Run 1 | Run 2 | Run 3 | Ballpark |
| --- | ---: | ---: | ---: | --- |
| local Homer blackhole | `10.41 s` | `4.84 s` | `4.17 s` | warmed around `4.2-4.8 s` |
| remote RDMA blackhole | `5.94 s` | `4.22 s` | `4.23 s` | warmed around `4.2 s` |

Use the first run only as warmup. The warmed remote RDMA path is in the same
band as local blackhole for this cluster size.

## Mixed Pgbench and Basebackup

Mixed runs started one remote RDMA basebackup from `farnet1` to `farnet0`, then
started remote Homer pgbench from `farnet0` to `farnet1` after a short delay.

| Workload | Pgbench result | Basebackup result |
| --- | --- | --- |
| remote Homer c1 plus basebackup | `3170 TPS`, avg `0.315 ms`, p95 `0.725 ms`, p99 `0.953 ms`, max `16.967 ms` | `4.18 s` |
| remote Homer c4 plus basebackup | `6374 TPS`, avg `0.628 ms`, p95 `1.134 ms`, p99 `1.499 ms`, max `19.522 ms` | `4.38 s` |

This is the most useful pre-baseline interference reference from this run:
basebackup throughput stayed stable, while foreground pgbench lost throughput
and tail latency worsened versus isolated remote Homer.

## Citus Backend-to-Backend COPY

Input: `/tmp/homer_tuple_sink_copy_10m.csv`, `10,000,000` rows, about `323 MB`.
The distributed table placement was verified on `10.10.1.100:5432`.

Homer tuple-sink COPY did not produce valid numbers:

- All three attempts failed at line 1 with
  `ERROR: remote execution control response kind mismatch`.
- A follow-up rebuild check corrected the install order by rebuilding and
  installing Citus/Homer against the current Postgres install, then relinking and
  reinstalling the Postgres binaries that statically link `libhomer_client.a`.
  The failure still reproduced in `/tmp/homer_rebuild_copy_check_20260612_194336`,
  so the original result should not be explained away as only stale install
  order.
- The error is raised by
  `RemoteExecutionControlCheckResponseHeader()` in
  `/data/dbcomm/citus-dbcomm-homer-unscheduled-baselines/src/backend/distributed/utils/homer/remote_execution_session.c`.
  The service logs for the corrected rebuild also showed peer receive sinks being
  provisioned with `payload_ring_slot_count=0` and `payload_ring_slot_bytes=0`,
  which points toward a COPY/peer-control contract or geometry propagation issue.
- This should be debugged before using backend-to-backend COPY in the fixed-loop
  baseline experiment matrix.

Vanilla Citus COPY with Homer tuple-sink routing disabled was healthy:

| Workload | Run 1 | Run 2 | Run 3 |
| --- | ---: | ---: | ---: |
| vanilla Citus COPY 10M | `5.17 s` | `5.45 s` | `6.18 s` |

Each vanilla run returned `count=10000000`, `min=1`, `max=10000000`, and
`sum=500000050000000`.

## Immediate Follow-Ups

- Rebuild without timing report instrumentation before final measurements.
- Debug backend-to-backend Homer COPY failure before treating COPY as part of the
  baseline performance matrix.
- Use the mixed remote Homer pgbench plus remote RDMA basebackup run as the
  current best scheduler-motivation reference shape.
