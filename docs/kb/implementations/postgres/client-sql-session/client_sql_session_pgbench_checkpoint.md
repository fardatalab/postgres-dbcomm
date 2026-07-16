# Client SQL Session Pgbench Checkpoint

<!-- kb-summary: Current implementation state, validation evidence, and milestone history for integrated pgbench Homer client-SQL sessions. -->

## Scope

- **What this doc explains**: the current implementation state for the simple-mode pgbench transaction workload that uses Homer typed client SQL sessions instead of libpq workload traffic.
- **What this doc does NOT cover**: prepared-mode pgbench, broad script compatibility, basebackup/replication offload, or the full network-interference benchmark.
- **Primary directory**: `docs/kb/implementations/postgres/client-sql-session/`
- **Doc type**: `implementation-checkpoint`

## Summary

Current state as of May 31, 2026: the client-to-PostgreSQL transaction milestone
is implemented and benchmarked through the integrated `pgbench --homer` path.
That path runs the built-in TPC-B-like transaction through Homer, including
service-owned sink-backed materialization/draining for the `SELECT abalance`
tuple result, byte-ring result queues, code-level CPU placement, async local
command submission, pushed completion rings, in-band tuple-result EOS, and
optional in-process tail-latency reporting. The older standalone
`homer_pgbench` proof-of-concept runner has been removed from the Citus/dbcomm
tree; it is no longer a benchmark entry point.

Important scope update: `pgbench --homer` now supports both the original
farnet1-local client SQL path and the farnet0-client-to-farnet1-PostgreSQL RDMA
path. In remote mode, pgbench maps the farnet0 local Homer service, the farnet0
service opens a peer `CLIENT_SQL_SESSION` on the farnet1 service, commands and
pushed completions move over service-to-service RDMA, and tuple-result sinks are
translated back to frontend-visible receive descriptors on farnet0. The
remaining cross-node gap is closing the gap with the farnet1-local
shared-memory path and making transport scheduling policy explicit; basic
single-client and multi-client remote correctness are both working.

The shared Homer client/session pieces are:

- [`remote_execution_client.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:1) exposes the frontend-safe Homer client API: control-region open/close, client SQL session open/close, command start, command poll, and start-and-wait helpers.
- [`homer_client.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1) implements that API without `PGconn`, backend memory contexts, or `ereport`.
- [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:408) opens a `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` using `CITUS_REMOTE_EXEC_CONTROL_OP_COMMAND_SESSION`.
- [`HomerClientStartAndWaitCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:737) sends one typed command and waits on the pushed completion ring; the older explicit completion-poll control request remains compatibility scaffolding.
- [`pgbench.c`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1) now has an opt-in Homer mode. [`sendHomerCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3573) maps exact transaction lifecycle SQL to typed Homer lifecycle commands and sends ordinary SQL through `SQL_EXECUTE`.
- [`threadRun()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8215) opens one persistent Homer control/session pair before measured work in `--homer` mode, preserving the normal libpq path otherwise.
- [`printResults()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7107) prints the active transport so Homer and libpq results from the same binary are distinguishable.

## Current State Snapshot

Complete for the current simple-mode milestone:

- `pgbench --homer` runs the built-in TPC-B-like script over the Homer external-service command path instead of libpq workload traffic.
- `pgbench --homer --homer-peer-host ...` runs the same simple-mode workload
  from farnet0 against the farnet1 PostgreSQL/socketless backend over the Homer
  RDMA service path.
- The default pgbench libpq modes remain available in the same binary. `--homer` is explicit opt-in, and `transport: homer` / `transport: libpq` is printed in the summary.
- The Homer path opens one frontend control mapping and one persistent client SQL session for the run, then reuses one socketless backend across transactions.
- Commands are semantic Homer commands: exact `BEGIN`, `COMMIT` / `END`, and `ROLLBACK` become typed lifecycle commands; ordinary SQL is a typed `SQL_EXECUTE` command carrying statement text.
- Row-producing SQL uses generic tuple-result mode. Pgbench's `SELECT abalance` is delivered through a tuple sink derived from the executor `TupleDesc`; it is not a special `(int8)` scalar shortcut.
- Client SQL result sinks are service-owned and parented under the persistent
  client SQL service session. The socketless backend requests the sink from the
  service after the executor provides the `TupleDesc`, then opens the returned
  send descriptor; the frontend maps the receive descriptor named by command
  completion metadata.
- The frontend opens/drains/closes the result sink named by command completion
  metadata, but it does not unlink the queue. Sink lifetime belongs to the
  backend/service session path.
- Client SQL result queues are byte-ring backed. [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:946)
  maps the byte-ring descriptor, and
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1054)
  parses complete tuple-view records and advances byte credit.
- Finite tuple results no longer close through a peer-control close request.
  [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
  marks the final tuple-view transport record with
  [`CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:37),
  or publishes a zero-row EOS record through
  [`SubmitCitusTupleSinkEmptyEosRecord()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1720)
  when no payload record is available.
- Terminal command completion for row-producing client SQL is gated on payload
  EOS through
  [`HomerServicePayloadStreamResultEosPosted()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3568).
  If the backend marks EOS after the service already copied the last record,
  [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8950)
  appends a synthetic zero-row EOS record. The frontend tracks this with
  [`HomerClientResultSink.eosSeen`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_client.h:68)
  while draining in
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2806).
- Code-level benchmark placement exists: `--client-cpu` for pgbench, `HOMER_SERVICE_CPU` for the service, and `HOMER_REMOTE_EXEC_BACKEND_CPU` carried through the backend spawn protocol.
- `--latency-percentiles` prints exact p50/p95/p99/max from in-process pgbench samples. For `-t` runs, sample vectors are preallocated before benchmark timing, so the measured path is a direct append.
- `src/tools/homer_pgbench_single_client_compare.sh` runs paired Homer/libpq single-client comparisons with the same seeds and records process-placement snapshots.
  The script remains useful for c1 comparisons, but integrated `pgbench
  --homer` is the canonical workload path.

Current measured status on farnet1:

- May 31, 2026 no-peer-close/EOS checkpoint: the earlier close-specific
  blocking peer-control exception is gone. Tuple-result EOS is an in-band
  payload-stream fact, and terminal completion is pushed only after the EOS
  record is posted and its source bytes are retired. Final validation after the
  rebuild/reinstall/sync cycle: warmed remote c1/j1 `-t 20000` completed
  `20000/20000` with zero failures at `4820.286 TPS`, p50 `0.205 ms`, p99
  `0.228 ms`; warmed remote c4/j4 `-t 10000` completed `40000/40000` with zero
  failures at `11144.880 TPS`, p50 `0.350 ms`, p99 `0.605 ms`; warmed remote
  RDMA blackhole basebackup completed in `4.35s`. A final short smoke after
  reinstall/sync completed `100/100` with zero failures at `3111 TPS`; use the
  warmed longer runs for performance comparisons.
- May 30, 2026 async peer-control cleanup checkpoint: the service-to-service
  peer-control path now has explicit async start/poll operations and async
  control publication underneath the remaining blocking adapters. The
  key code points are
  [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456),
  [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623),
  [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3786),
  and
  [`TupleSinkServiceDrainTaggedSendCompletions()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1497).
  Request publishes borrow op-owned registered source storage; response
  publishes borrow preallocated response publish slots; tagged send CQEs retire
  those sources later. The blocking adapter remains for some cold/setup and
  legacy call sites, but it now waits on an explicit op handle rather than on an ambient
  connection-wide response.
  Final validation after rebuilding, installing, syncing the farnet0 service
  binary, and restarting PostgreSQL plus both Homer services: warmed remote
  c1/j1 `-t 20000` completed `20000/20000` with zero failures at
  `4869.43 TPS`, p50 `0.203 ms`, p99 `0.224 ms`; warmed remote c4/j4
  `-t 10000` completed `40000/40000` with zero failures at `11457.72 TPS`,
  p50 `0.337 ms`, p99 `0.594 ms`; warmed remote RDMA blackhole basebackup
  completed in `4.32s`; concurrent remote c4 pgbench plus remote RDMA
  basebackup completed with both rc=0, pgbench `9486.10 TPS`, p50 `0.383 ms`,
  p99 `0.772 ms`, and basebackup `4.45s`. The first post-restart c1 run had a
  cold/warmup outlier (`1173 ms` max latency), so use the warmed repeat for
  steady-state comparison.
- May 16, 2026 real two-node RDMA client-SQL checkpoint: after adding remote
  Homer pgbench options, peer `CLIENT_SQL_SESSION` open/bind support, direct
  RDMA command-mailbox forwarding, peer pushed completions, result-sink
  descriptor translation, explicit peer bind hosts, and receive-side byte-ring
  EOS propagation, farnet0 `pgbench --homer` completed remote c1/j1
  `20000/20000` transactions with zero failures. Warmed c1 remote RDMA measured
  `4246 TPS`, average `0.235 ms`, p50 `0.233 ms`, p95 `0.243 ms`, p99
  `0.254 ms`, max `4.691 ms`. Same-build local farnet1 Homer c1 measured
  `5948 TPS`, average `0.168 ms`, p99 `0.185 ms`. The remote c1 path is
  correct and in the same broad latency band as the earlier remote checkpoint,
  but not on par with the local shared-memory path.
- The same real RDMA build does **not** yet scale for multi-client pgbench:
  farnet0 c4/j4 `-t 10000` repeated at about `4811` and `4838 TPS`, with p95
  around `4.1-4.5 ms` and p99 around `7.9 ms`. This is a structural
  multi-session transport bottleneck, not a correctness failure. The likely
  causes are still the single service loop plus shared per-peer RDMA command and
  completion publication machinery; this should be handled with the planned
  traffic-class/multiple-QP/scheduler work rather than hidden as a benchmark
  variance issue.
  Follow-up performance investigation recorded three reverted/non-useful
  optimization attempts in the future-direction note: fully unsignaled command
  epoch publication filled the send queue because no later signaled WR retired SQ
  credits; periodic command epoch checkpoints did not improve warmed c1; and a
  completion scratch ring plus periodic completion checkpoint kept correctness but
  worsened c1. The current evidence points to service-thread/QP serialization and
  per-command/per-completion signaled publication waits, not raw RDMA bandwidth,
  as the next bottleneck to measure and attack.
  A diagnostic `HOMER_SERVICE_CLIENT_SQL_STATS=1` build then measured that the
  built-in transaction performs about `7` remote command publications and `8`
  remote completion publications per transaction, with signaled epoch publish
  averages around `7-8 us` and per-command/per-completion record writes of about
  `54184` and `33776` bytes respectively. A c4 no-`SELECT` script improved from
  `~4.1k TPS` to `~5.8k TPS` and dropped p95 from `~4.2 ms` to `~0.58 ms`, but
  p99 remained around `9 ms`; this implicates both result-sink interaction and
  command/completion control-plane serialization.

- May 17, 2026 command-publication A/B: command record
  `RDMA_WRITE_WITH_IMM` plus receiver-service local epoch publish was correct
  but slower than direct RDMA epoch publish. With the immediate path enabled,
  warmed farnet0 -> farnet1 pgbench measured about `4664 TPS` c1 and
  `9962 TPS` c4. With the direct epoch path restored as the default, the same
  run shape measured about `4659-4691 TPS` c1, p99 `0.233-0.239 ms`, and
  `10433-10498 TPS` c4, p99 `0.653-0.654 ms`. The switch is
  `HOMER_SERVICE_CLIENT_SQL_COMMAND_IMM_PUBLISH=0`; keep it off for performance
  runs unless explicitly re-testing the one-WR publication hypothesis. The
  result shows that removing a tiny RDMA epoch write can lose if the backend
  command wakeup must now pass through the receiver service loop.
  Follow-up optimization recovered the immediate c1 path but did not make it win:
  after replacing service-session-id matching with a receiver-local command
  doorbell token, O(1) pending-doorbell pop, single-CQE repost fast path, command
  sequence validation, pending-token cleanup on reset, and active-session scan
  shrink, warmed immediate measured `4647 TPS` c1 / p99 `0.236 ms` and
  `10084 TPS` c4 / p99 `0.656 ms`. The restored default direct path measured
  `4735 TPS` c1 / p99 `0.231 ms` and `10632 TPS` c4 / p99 `0.668 ms` in the
  same follow-up run shape. Keep `HOMER_SERVICE_CLIENT_SQL_COMMAND_IMM_PUBLISH`
  off unless the next experiment changes the mailbox/ring layout enough to avoid
  the receiver-service publish hop.
  The current immediate experiment was then reordered so command CQEs can publish
  the backend-visible epoch before receive-WQE reposting via
  [`TupleSinkServiceDrainPeerConnectionCommandDoorbellsRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2583).
  This is the right ordering for any receiver-service fallback, but warmed remote
  pgbench was still essentially unchanged (`4636 TPS` c1, p99 `0.236 ms`;
  `10100 TPS` c4, p99 `0.657 ms`). The next step is therefore not more
  immediate-token routing; it is a real multi-slot command ring with backend
  polling of `ready_seq == expected_seq`, followed by a one-RDMA-write
  self-publishing slot fast path gated by `ibv_query_qp_data_in_order()`.
  A follow-up direct-source command attempt, where the service registered the
  frontend command mailbox as the RDMA source to avoid the scratch copy, was
  reverted after a correctness failure: pgbench received a completion and then
  found the next command mailbox still busy (`published=248 consumed=247`).
  That shows remote completion can race ahead of sender-side source retirement;
  avoiding the scratch copy needs a real multi-slot command source ring, not the
  current single-slot frontend mailbox as a borrowed RDMA source.

- May 16, 2026 service-owned result sink and byte-ring-only tuple/result
  checkpoint: after forcing the installed Postgres backend, pgbench, and
  pg_basebackup binaries to relink against the Citus/dbcomm `v19` control
  protocol and restarting Postgres plus `citus_tuple_sink_service`, correctness
  checks passed with no failed transactions. Local blackhole basebackup
  completed in `4.19s`. Pinned c1/j1 `pgbench --homer -t 10000
  --latency-percentiles --client-cpu=8` completed `10000/10000` at
  `5838 TPS`, average `0.171 ms`, p99 `0.188 ms`, and
  `pgbench_history` grew by exactly `10000`. Pinned c4/j4
  `--client-cpus=8,9,10,11 -t 20000` repeated at `14983` and `14851 TPS`,
  with p99 `0.470` and `0.465 ms`; each run completed `80000/80000` with zero
  failures. Unpinned c4/j4 runs were around the `14k TPS` band and are useful
  as scheduler-placement evidence, not as canonical prototype numbers.
- May 16, 2026 full byte-ring result migration checkpoint: after moving
  tuple/result payload streams to the byte-ring substrate, removing per-batch
  tuple-sink allocation/free, replacing fixed 1 ms Homer backend/worker sleeps
  with CPU-relax polling, and removing the obsolete standalone `homer_pgbench`
  scaffold, integrated `pgbench --homer -t 3000 --latency-percentiles` completed
  all transactions with zero failures. Homer c1/j1 repeats were `5654`, `5679`,
  and `5784 TPS` with p99 about `0.196-0.201 ms`; libpq c1/j1 repeats from the
  same installed binary were `4398`, `4209`, and `4344 TPS` with p99 about
  `0.253-0.286 ms`. Homer c4/j4 repeats were `14656`, `15921`, and `15229 TPS`;
  libpq c4/j4 repeats were `10966`, `10852`, and `10003 TPS`.
- May 14, 2026 regression follow-up: a low single-client run was reproduced at
  `4900 TPS`, but `pgbench_history` had grown to about `5.5M` rows. After
  `truncate pgbench_history` plus `vacuum analyze` on the active pgbench tables,
  clean-state c1 Homer returned to `6033`, `7329`, and `7333 TPS`; clean-state
  c4/j4 Homer returned to `15122` and `15007 TPS`. This means future pgbench
  performance comparisons must reset or explicitly record table state before
  attributing low TPS to the Homer transport. A semantics-preserving cleanup also
  moved the backend result-copy slot from per-SELECT receiver lifetime to
  persistent socketless-backend session lifetime: the session fields are
  [`resultCopySlot`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:136)
  and
  [`resultCopyTupleDesc`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:137),
  [`RemoteExecEnsureSessionResultCopySlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:580)
  reuses them across compatible `SELECT abalance` commands, and
  [`RemoteExecSqlDestDestroy()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:919)
  now only drops borrowed receiver pointers. After rebuilding/installing and
  restarting, warmed c1 Homer repeats were `6675`, `5604`, `5589`, `6750`, and
  `6413 TPS`; a clean-state c4/j4 run completed `80000/80000` at `15104 TPS`
  with p99 `0.460 ms`.
- After the direct session-local command/completion channel patch on May 14,
  2026, `pgbench --homer -c 4 -j 4 -t 50000` completed `200000/200000`
  transactions with zero failures at `15783 TPS` in the first long run and
  `15572 TPS` after a small hot-path consumed-epoch store cleanup. The same
  binary/placement over libpq completed `200000/200000` at `8477 TPS`. Raw
  artifacts are under
  `/data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_direct_channels_compare_20260514_064556`
  and
  `/data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_direct_channels_after_opt_20260514_064742`.
- The same direct-channel run shape reported single-client Homer `5991 TPS`
  and libpq `3994 TPS` for `50000/50000` transactions. This is below the best
  warm `~6.6k TPS` single-client run from the earlier command-copy pruning pass,
  but still above the old `~5.1k TPS` milestone and verifies that removing the
  service relay did not reintroduce the single-client regression.
- After async local `START_COMMAND` and pushed completion rings landed, 50k-transaction paired runs on May 13, 2026 reported Homer `5131` and `5068 TPS` with p99 `0.212` / `0.215 ms`; libpq reported `3264` and `3435 TPS` with p99 `0.413` / `0.333 ms`. All four runs completed `50000/50000` transactions with zero failures. Raw artifacts are under `/data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_async_start_ring_final_20260513_130349`.
- Three 10k-transaction paired runs with code-level pinning averaged about `5076 TPS` for Homer and `3622 TPS` for libpq. Median TPS was about `5101` for Homer and `3791` for libpq.
- A logged 10k-transaction tail-latency check showed Homer p99 `0.218 ms` and libpq p99 `0.299 ms`; Homer had a worse single max outlier in that run.
- A 1000-transaction integrated `--latency-percentiles` smoke showed Homer p99 `0.227 ms` and libpq p99 `0.286 ms`.

Still incomplete or intentionally scoped out:

- prepared-mode pgbench and typed prepare/bind/execute commands
- reconnect mode and retry mode in Homer
- `\gset`, `\aset`, pipeline meta-commands, and broad pgbench script compatibility
- normal frontend authentication and name-to-OID mapping; current prototype still takes database/user OIDs
- SQL command tags, SQLSTATE, and authoritative dynamic session state in command completion
- cancellation and robust error propagation
- multi-client real-RDMA scaling is correct, but still below the best farnet1
  local shared-memory path; future work should separate traffic-class resources
  and scheduler policy rather than treating the current fixed-priority service
  loop as final
- WAL/replication-path Homer support needed for the full basebackup /
  network-interference benchmark
- DPU-oriented service/control transport; current service still polls host shared memory and uses fixed control slots

The earliest installed smoke test on farnet1 used normal `pgbench -i` only for
schema/data initialization. The workload loop itself used the now-removed
standalone runner:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/homer_pgbench \
  --database-oid 5 --user-oid 10 --scale 1 --transactions 3 --seed 43
```

Observed result:

```text
homer_pgbench: transactions=3 completed=3 failed=0 scale=1 elapsed_ms=80.407 tps=37.310
```

After the earlier one-transaction smoke plus this three-transaction repeat, `select count(*) from pgbench_history;` returned `4`. After factoring the frontend API into `libhomer_client.a`, follow-up one-transaction reruns with seeds `44` and `45` succeeded and `pgbench_history` reached `6`. After the persistent-session patch, pinned 3-, 1000-, and 5000-transaction standalone `homer_pgbench` runs also completed with one service session/backend per run. After the pgbench integration, pinned `pgbench --homer` 2-, 1000-, and 5000-transaction runs completed through the same external service path. After sink-backed result materialization landed, the unpinned full built-in transaction completed a 30 second `pgbench --homer` run with `SELECT abalance` payloads delivered through tuple sinks.

## Implemented Pieces

### Protocol and session identity

- [`REMOTE_EXEC_OP_CLIENT_SQL_SESSION`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:30) now exists as a distinct operation kind.
- [`CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:93) mirrors that kind in the fixed-width service protocol.
- [`CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_TX_BEGIN`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:138) adds the typed frontend-owned begin command. The existing typed `TX_COMMIT` and `TX_ABORT` commands are reused for transaction lifecycle, but no longer retire client SQL sessions.
- [`CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:139) is the internal typed backend shutdown command used behind `HomerClientCloseSession()`.
- [`CitusRemoteExecBackendSpawnRequest.sessionOpKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:69) and [`CitusRemoteExecBackendStartupData.sessionOpKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:59) let the socketless backend distinguish persistent client SQL sessions from older backend-to-backend SQL command sessions.
- [`Makefile`](/data/dbcomm/citus-dbcomm-separate-comm-stack/Makefile:47) builds `homer_client.o` and archives it as `libhomer_client.a`. [`install-service-bin`](/data/dbcomm/citus-dbcomm-separate-comm-stack/Makefile:93) installs the service binary and `libhomer_client.a`; `install-headers` installs `remote_execution_client.h`. The old `homer_pgbench` binary target has been removed.

### Frontend client API library

The frontend API has been split out of the standalone runner:

- [`HomerClientOpenControl()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:136) maps the service control shared memory without `PGconn`, backend memory contexts, or `ereport`.
- [`HomerClientReserveControlSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:248) reserves one fixed-width control slot with atomics.
- [`HomerClientSubmitControlRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:374) publishes the request and validates the fixed-width response.
- [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1485) consumes pushed command completion metadata from the per-session frontend completion ring. [`HomerClientPollCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1612) remains as compatibility scaffolding, but the local frontend path no longer submits a completion-poll control request.
- [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:946), [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1054), and [`HomerClientCloseResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1031) map, drain, and close the backend-produced tuple-result sink named by command completion metadata.
- [`HomerClientCloseSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:496) remains a public control-plane close API; the service translates it into the internal typed `CLIENT_SQL_SESSION_CLOSE` command when a persistent socketless backend is active.

The API still exposes one-command-at-a-time semantics. That is intentional for
the simple-mode pgbench milestone; command pipelining remains separate design
work.

Update: multi-client pgbench now works for the local client-SQL path with one
Homer session per pgbench client. It is still one command in flight per session;
command pipelining remains future work.

### Pgbench integration

- [`homer_mode`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:274) is an opt-in transport switch. The default `PGconn` / libpq path remains unchanged unless `--homer` is present.
- [`CState.homer_session`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:609) and [`TState.homer_control`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:676) add Homer state beside pgbench's original libpq connection state.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3737) is the blocking adapter retained for setup/cleanup paths outside the measured command loop. The measured Homer path uses [`HomerStartCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3612) plus [`CSTATE_WAIT_RESULT`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:4753), so one pgbench thread can keep progressing multiple client sessions.
- [`sendHomerCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3390) maps exact `BEGIN`, `COMMIT` / `END`, and `ROLLBACK` SQL text to typed Homer lifecycle commands. Ordinary SQL is sent as `CITUS_REMOTE_EXEC_COMMAND_SQL_EXECUTE`.
- [`HomerSqlLooksRowProducing()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3230) requests tuple-result mode for `SELECT`, `WITH`, and `VALUES` statements. This covers the built-in pgbench `SELECT abalance` command without hard-coding the `(abalance int8)` shape.
- [`sendCommand()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3507) branches to Homer only in `--homer` mode; the existing `PQsendQuery()`, `PQsendQueryParams()`, and `PQsendQueryPrepared()` branches remain the normal behavior.
- [`CSTATE_START_COMMAND`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:4138) moves directly to `CSTATE_END_COMMAND` after a completed Homer command, avoiding libpq socket/result waiting while preserving pgbench's command and transaction timing.
- [`CSTATE_END_TX`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:4540) checks `homer_transaction_attached` in Homer mode instead of calling `PQtransactionStatus()` on a nonexistent `PGconn`.
- [`threadRun()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7842) opens the persistent Homer control/session before the benchmark start barrier, matching pgbench's existing persistent-connection measurement boundary.
- [`finishHomerSession()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8137) closes the persistent Homer session and defensively aborts any still-attached transaction first.
- [`src/bin/pgbench/Makefile`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/Makefile:10) and [`src/bin/pgbench/meson.build`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/meson.build:31) link the Homer frontend client library into pgbench for this prototype.

The pgbench integration intentionally rejects unsupported paths at startup:
non-simple query mode, reconnect mode, retry mode, missing Homer OID options,
`\gset`, `\aset`, pipeline meta-commands, and scripts without explicit
transaction lifecycle SQL. Multiple clients/threads are supported for the local
client-SQL path with one Homer session per pgbench client and one command in
flight per session.

### Service-side local client session path

- [`TupleSinkServiceHandleOpenCommandSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6568) accepts `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION` as a command-session operation without requiring a peer RDMA endpoint.
- [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7539) routes client SQL sessions locally through session-owned command/completion mailboxes instead of peer-control RDMA.
- Local client SQL `START_COMMAND` is now submit-only. [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7539) publishes the backend command, returns accepted/PENDING state, and relies on the normal service loop to push later `STARTED` / terminal completion events. [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2485) remains only for compatibility close/debug-style blocking paths, not the measured pgbench command path.
- [`TupleSinkServiceSessionShouldRetireWhenIdle()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3957) treats `FORCE_FRESH` as command-session open-time allocation policy. For `CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION`, it no longer retires on `TX_COMMIT` / `TX_ABORT`; it retires on explicit close, failure, or backend-reported non-reuse.
- [`TupleSinkServiceHandleCloseSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7152) sends `CLIENT_SQL_SESSION_CLOSE` to the socketless backend before unlinking the session mailboxes.

The startup-wait and retirement fixes are important implementation corrections. Without them, the service could either re-enter the active request slot or retire the fresh session immediately after `CLIENT_SQL_TX_BEGIN`.

### Socketless backend dispatch

- [`RemoteExecBackendExecuteClientSqlTxBeginCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:714) starts a frontend-owned SQL transaction without attaching Citus distributed transaction identity.
- [`RemoteExecBackendExecuteSqlCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:969) executes one non-utility SQL string inside the attached transaction and returns processed row count.
- [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:813) and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:858) keep the backend loop alive for `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`, while preserving terminal behavior for older backend-to-backend command sessions.
- [`RemoteExecBackendExecuteClientSqlSessionCloseCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:904) explicitly shuts down the persistent client SQL backend on session close.
- The command loop dispatches `CLIENT_SQL_TX_BEGIN`, `SQL_EXECUTE`, `TX_COMMIT`, `TX_ABORT`, and `CLIENT_SQL_SESSION_CLOSE` through the typed command switch in [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1068).

SQL execution intentionally reuses the existing non-SPI executor path:

- [`ExecuteQueryStringIntoDestReceiverWithProcessedCount()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/executor/multi_executor.c:632)
- [`ExecuteQueryIntoDestReceiverWithProcessedCount()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/executor/multi_executor.c:698)
- [`ExecutePlanIntoDestReceiver()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/executor/multi_executor.c:721)

### Result materialization status

Sink-backed tuple results are implemented for the client SQL path. This
replaces the earlier scalar/discard shortcut for row-producing pgbench commands,
and the result sink is now allocated/bound by the Homer service rather than by a
backend-created POSIX shared-memory shortcut:

- [`RemoteExecOpenServiceOwnedResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:597) submits a service `OPEN_SESSION` request for a tuple-sink stream whose `parentServiceSessionId` is the active client SQL service session.
- [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:740) binds or reuses one service-owned result sink when the executor `TupleDesc` is compatible, and reopens it when the tuple contract changes.
- [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9920) recognizes this client-SQL result-open shape through `parentServiceSessionId` and returns a byte-ring descriptor for the exact service-owned stream.
- [`RemoteExecSqlDestStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:972) receives the executor `TupleDesc`, builds a tuple-view contract, opens the service-owned send-side tuple sink, and publishes a `STARTED` completion so the frontend can open the sink while the command is still active.
- [`RemoteExecSqlDestReceiveSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:585) copies executor tuples into a receiver-owned virtual slot, appends them to tuple-sink batches, and flushes/reserves batches on normal batch-full backpressure.
- [`RemoteExecSqlDestShutdown()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1635) flushes the final batch and marks peer-closed/EOS, then drops only the receiver's borrowed handle pointer. The persistent backend session retains the actual handle and mapping for compatible later commands.
- [`RemoteExecSqlDestDestroy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1660) drops receiver-local references only. [`RemoteExecCleanupSessionResultQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1186) closes the handle/mapping, service-owned sink, copy slot, and copied descriptor when the socketless backend session ends.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3249) opens and drains the result sink on `STARTED` or `COMPLETED` command completions, then closes/unlinks the frontend mapping.

Two lifetime corrections are important:

- The receiver-owned copy slot uses a copied `TupleDesc` in `TopMemoryContext`. Keeping the executor-provided `TupleDesc` inside a longer-lived slot after `PortalDrop()` produced stale executor slot metadata on the next command.
- The send-side tuple sink handle is session-owned and deliberately survives `rShutdown`/`rDestroy`. Those callbacks own one command's receiver state, while `RemoteExecBackendSessionState` owns the reusable mapping, copy slot, and copied descriptor until session cleanup. The earlier command-local close avoided one stale-context failure but also prevented the current cross-command reuse; it is historical rationale, not the current ownership rule.

The current implementation deliberately keeps a local shared-memory mapping for
the local-host prototype, but ownership and binding are now service-driven. That
distinction matters for later DPU work: the backend writes through a descriptor
that the Homer service allocated and named; it no longer invents and unlinks a
private result queue on the row-producing command path.

## Verification

Build verification:

- `make -j8 service-bin` completed.
- `make -j8 client-bin` completed.
- `make -j8 extension` completed.
- `sudo -n make install` installed `citus.so`, headers, and `citus_tuple_sink_service` into `/data/dbcomm/pg-citus`.
- After the client-library split, `make -j8 client-bin` built `build/homer/libhomer_client.a`; `sudo -n make install-headers install-service-bin` installed the header, static library, and service binary. The removed `homer_pgbench` binary was also deleted from `/data/dbcomm/pg-citus/bin`.

Performance build flags to preserve:

- [`HOMER_SERVICE_VERBOSE_LOGGING`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:66) defaults to `0`. Keep it off for performance runs; `-DHOMER_SERVICE_VERBOSE_LOGGING=1` enables service command/session tracing.
- [`HOMER_REMOTE_EXEC_TRACE`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:74) defaults to `0`. Keep it off for socketless-backend performance runs.
- [`HOMER_POSTGRES_PERF_DIAGNOSTICS`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/tcop/postgres.c:29) defaults to `0`. Keep it off unless per-query timing / perf FIFO diagnostics are needed.
- [`HOMER_REMOTE_EXEC_STARTUP_TRACE`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/tcop/backend_startup.c:50) and [`HOMER_REMOTE_EXEC_STARTUP_TRACE`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/utils/init/postinit.c:1510) default to `0`. Keep them off unless debugging backend launch.
- [`HOMER_SERVICE_IDLE_PEER_PUMP_INTERVAL`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:77) defaults to `1024`. This keeps the RDMA peer accept pump periodic when no peer session/sink exists, so local client SQL runs do not enter the RDMA CM event path on every service loop.
- [`HOMER_SERVICE_BACKEND_EXIT_CHECK_INTERVAL`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:90) defaults to `1024`. This keeps the local backend-exit probe periodic rather than calling `kill(pid, 0)` on every command-wait spin.
- [`HOMER_SERVICE_LOCAL_WAIT_BACKGROUND_PUMP_INTERVAL`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:114) defaults to `64`. The local client-SQL wait checks the target completion mailbox every spin, but only runs the broader all-session completion and sink/payload pump on this cadence.

Runtime placement knobs:

- [`--client-cpu=CPU`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1114) pins every pgbench worker thread to the same CPU for both libpq and `--homer` runs. [`--homer-client-cpu=CPU`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1118) is kept as a compatibility alias. The parser is [`parseClientCpuOption()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:967); [`applyClientCpuAffinity()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1027) runs after the READY barrier and before measured connection/session setup at [`threadRun()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8477).
- [`--client-cpus=LIST`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1115) is the multi-worker placement knob. [`parseClientCpuListOption()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:987) parses a comma-separated list, and [`applyClientCpuAffinity()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1031) maps worker thread `tid` to `client_cpus[tid % client_cpu_count]`. This avoids ad hoc per-thread `taskset` while keeping default pgbench scheduling untouched when no placement option is supplied.
- Multi-client warning: `--client-cpu=CPU` is still an artificial frontend
  bottleneck for c4/j4 Homer runs because all busy-waiting frontend workers
  share one core. Use no frontend pinning, or use `--client-cpus=LIST` with
  enough CPUs for the worker-thread count. Fully automatic CPU-list selection
  is not implemented yet; a future `auto` mode should exclude the service and
  socketless-backend CPUs before assigning pgbench workers.
- `HOMER_SERVICE_CPU` pins the standalone service process. The service parses it with [`TupleSinkServiceReadCpuEnv()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:233), applies it with [`TupleSinkServiceApplyCpuAffinity()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:362), and reads it before entering the main service loop at [`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8898).
- `HOMER_REMOTE_EXEC_BACKEND_CPU` pins every socketless backend to one CPU. `HOMER_REMOTE_EXEC_BACKEND_CPUS` is the multi-backend placement knob: the service parses the comma-separated list with [`TupleSinkServiceReadCpuListEnv()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:268), chooses a CPU round-robin at backend spawn time with [`TupleSinkServiceChooseBackendCpu()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:343), and writes it into `request.backendCpu` in [`TupleSinkServiceSubmitBackendSpawnRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2201). The postmaster copies that field into startup data in [`ProcessSpawnRequestSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2156), and the socketless backend applies it once during startup in [`RemoteExecBackendApplyCpuAffinity()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:230) before [`RemoteExecBackendInitializeConnectionByOid()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1820).
- The backend spawn protocol carries `backendCpu` in both [`CitusRemoteExecBackendSpawnRequest`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:73) and [`CitusRemoteExecBackendStartupData`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:61). The current implementation trusts benchmark configuration; it does not yet reject overlapping `HOMER_SERVICE_CPU`, `HOMER_REMOTE_EXEC_BACKEND_CPUS`, and pgbench `--client-cpus` sets.
- [`src/tools/homer_pgbench_single_client_compare.sh`](/data/dbcomm/postgres-citus-separate-comm-stack/src/tools/homer_pgbench_single_client_compare.sh:1) is the current repeatable comparison wrapper. It uses pgbench's code-level `--client-cpu`, runs paired Homer/libpq single-client runs with the same seeds, and records `/proc` CPU affinity/PSR snapshots before and after each run. It intentionally does **not** restart the service, because `HOMER_SERVICE_CPU` and `HOMER_REMOTE_EXEC_BACKEND_CPU` must be set before launching `citus_tuple_sink_service`.

Runtime verification:

- restarted Postgres with `/data/dbcomm/pg-citus/bin/pg_ctl -D /data/dbcomm/pg-citus/data restart -m fast -w`
- restarted `/data/dbcomm/pg-citus/bin/citus_tuple_sink_service`
- initialized scale-1 pgbench tables with ordinary `pgbench -i`
- historical early verification used the now-removed standalone `homer_pgbench`
  runner for one-transaction and three-transaction smokes, then 3-, 1000-, and
  5000-transaction persistent-session runs
- `pgrep -af 'remote exec|citus_tuple_sink_service|postgres -D /data/dbcomm/pg-citus/data|Citus Maintenance'` showed no leftover remote-exec backend after session close

Pgbench integration verification:

- `ninja -C build src/bin/pgbench/pgbench` built `/data/dbcomm/postgres-citus-separate-comm-stack/build/src/bin/pgbench/pgbench`.
- `pgbench --help` shows `--homer`, `--homer-database-oid`, and `--homer-user-oid`.
- `pgbench --homer -S --homer-database-oid 5 --homer-user-oid 10 -t 1 postgres` rejects the select-only builtin before benchmarking because the script lacks explicit `BEGIN` / `COMMIT` lifecycle commands.
- A debug 2-transaction run executed the default TPC-B-like script through Homer and increased `pgbench_history` from `17012` to `17014`.
- A pinned 1000-transaction Homer pgbench run increased `pgbench_history` from `17014` to `18014`.
- A pinned 5000-transaction Homer pgbench run increased `pgbench_history` from `19014` to `24014`.
- A final process check showed the service and Citus maintenance daemon only; no leftover remote-exec backend remained after session close.
- After sink-backed result materialization landed, the built-in TPC-B-like script passed `pgbench --homer --homer-database-oid 5 --homer-user-oid 10 -n -c 1 -j 1 -t 2 postgres` with two processed transactions and zero failures.
- A 30 second full built-in `pgbench --homer` run completed `27217` transactions with zero failures and the service log showed the expected result-sink `STARTED`/`COMPLETED` pair for each `SELECT abalance`.
- After adding code-level affinity controls, `ninja -C build src/bin/pgbench/pgbench` rebuilt pgbench successfully, `make -j8 service-bin` rebuilt the service successfully, and `make -j8` rebuilt `citus.so` successfully. `bash -n src/tools/homer_pgbench_single_client_compare.sh` verified the comparison wrapper syntax.
- After the May 16 byte-ring result migration, `make -C /data/dbcomm/citus-dbcomm-separate-comm-stack -j$(nproc)`, `sudo -n make install-headers install-service-bin install`, `ninja -C /data/dbcomm/postgres-citus-separate-comm-stack/build src/backend/postgres src/bin/pgbench/pgbench src/bin/pg_basebackup/pg_basebackup`, and `sudo -n meson install -C /data/dbcomm/postgres-citus-separate-comm-stack/build --no-rebuild` all completed successfully.

Service log evidence before result sinks showed the expected seven-command sequence per transaction:

```text
client_sql_tx_begin sequence=1 state=completed rows=0
sql_execute sequence=2 state=completed rows=1
sql_execute sequence=3 state=completed rows=1
sql_execute sequence=4 state=completed rows=1
sql_execute sequence=5 state=completed rows=1
sql_execute sequence=6 state=completed rows=1
tx_commit sequence=7 state=completed rows=0
```

After persistent-session reuse, service log evidence changed to one service session and one backend spawn per run, followed by many transaction command sequences and one explicit close. For the 3-transaction smoke:

```text
opened command session=1 peer_session=0 op=5
spawn request completed session=1 op=5 command=client_sql_tx_begin sequence=1 launched_pid=344819
...
tx_commit sequence=21 state=completed rows=0
client_sql_session_close sequence=22 state=completed rows=0
closing compatibility session=1 without an exact sink handle
```

After result sinks, each row-producing command emits a `STARTED` completion before terminal completion so the frontend can open and drain the sink:

```text
sql_execute sequence=N state=started rows=0 detail=result sink ready
sql_execute sequence=N state=completed rows=1 detail=
```

## Historical Standalone Runner Measurements

This section is historical context for the removed standalone runner. It should
not be used for current performance claims; use integrated `pgbench --homer`
instead. After adding online transaction-latency accounting to the old
`homer_pgbench.c`, a longer run on farnet1 completed successfully:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/homer_pgbench \
  --database-oid 5 --user-oid 10 --scale 1 --transactions 1000 --seed 1001
```

Observed result:

```text
homer_pgbench: transactions=1000 completed=1000 failed=0 scale=1 elapsed_ms=27580.137 tps=36.258 latency_avg_ms=27.580 latency_min_ms=26.314 latency_max_ms=32.808 latency_stddev_ms=0.985
```

For local context, stock single-client simple-query `pgbench` on the same initialized scale-1 tables reported:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -n -c 1 -j 1 -t 1000 postgres
```

```text
latency average = 0.327 ms
tps = 3053.453762 (without initial connection time)
```

A closer, but still imperfect, historical sanity check was stock `pgbench -C`,
because the then-current standalone Homer runner paid a session open/close cost
per transaction:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -n -C -c 1 -j 1 -t 1000 postgres
```

```text
latency average = 3.833 ms
average connection time = 2.031 ms
tps = 260.925608 (including reconnection times)
```

This was not an apples-to-apples final benchmark. That early Homer runner opened
and closed one `REMOTE_EXEC_OP_CLIENT_SQL_SESSION` per transaction, while normal
stock pgbench keeps one connection/backend context for the client. Even stock
reconnect-per-transaction pgbench was substantially faster than the initial
Homer path, so the gap was real, but the persistent-connection pgbench number
overstated the direct comparison. The number is useful as historical evidence
for why the persistent-session pgbench integration became the benchmark path,
not as a current performance claim.

Follow-up performance sanity checks:

- Replacing fixed 1 ms sleeps in the Homer frontend client, service control waits, service main loop, and socketless backend mailbox wait with `sched_yield()` removed the artificial per-command millisecond wakeup cost.
- After warming normal Citus backend state once with `psql`, the no-sleep/yield Homer run completed:

```text
homer_pgbench: transactions=1000 completed=1000 failed=0 scale=1 elapsed_ms=3733.753 tps=267.827 latency_avg_ms=3.734 latency_min_ms=3.134 latency_max_ms=5.134 latency_stddev_ms=0.269
```

That put the early Homer prototype in the same rough range as stock
`pgbench -C`, confirming that the original ~27 ms result was dominated by
sleep-based polling. It also confirmed that the remaining order-of-magnitude gap
versus persistent stock pgbench was primarily lifecycle/model mismatch: the old
standalone Homer runner still created a service session and socketless backend
per transaction instead of keeping one client SQL session/backend alive across
many transactions.

Later correction after explicit CPU pinning:

- Postgres/postmaster and newly spawned backend children were pinned to CPU 4.
- The external service was pinned to CPU 2.
- The frontend `homer_pgbench` runner was launched on CPU 3.
- The Citus maintenance daemon was pinned to CPU 5 after warmup.
- Under that layout, a `sched_yield()` run completed 1000 transactions at `tps=242.942`, `latency_avg_ms=4.116`, with `/usr/bin/time` reporting `user=0.55 sys=3.56`.
- Rebuilding the three wait helpers as raw `pause` busy waits completed 1000 transactions at `tps=246.288`, `latency_avg_ms=4.060`, with `user=4.05 sys=0.01`.
- A longer raw `pause` run completed 5000 transactions at `tps=246.023`, `latency_avg_ms=4.065`, with `user=20.30 sys=0.01`.

So raw `pause` spinning is **not** itself worse than `sched_yield()` once the producer/consumer processes are explicitly placed on separate CPUs. The earlier pure-spin hang should be treated as an unpinned/cold-start progress problem that still needs diagnosis if it reappears, not as proof that `pause` is inherently unsafe for this benchmark.

The current code-level equivalent is:

```sh
HOMER_SERVICE_CPU=2 HOMER_REMOTE_EXEC_BACKEND_CPU=4 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service

CLIENT_CPU=3 RUN_AS=dbcomm TRANSACTIONS=10000 RUNS=3 \
  /data/dbcomm/postgres-citus-separate-comm-stack/src/tools/homer_pgbench_single_client_compare.sh
```

This replaces `taskset` as the canonical benchmark setup. `taskset -pc` remains useful as an observation/debugging command, but benchmark placement should come from the code/env knobs above.

Fresh code-level affinity comparison on farnet1, May 12 2026:

```text
Setup:
service: HOMER_SERVICE_CPU=2
socketless backend children: HOMER_REMOTE_EXEC_BACKEND_CPU=4
pgbench worker: --client-cpu=3
workload: built-in TPC-B-like pgbench, scale 1, -n -c 1 -j 1 -t 10000
artifacts: /data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_pgbench_affinity_20260512_020714

Homer run 1: latency average = 0.200 ms, tps = 5010.833422, failed = 0
Homer run 2: latency average = 0.195 ms, tps = 5116.052536, failed = 0
Homer run 3: latency average = 0.196 ms, tps = 5101.140309, failed = 0
Homer average: latency ~= 0.197 ms, tps ~= 5076.009

libpq run 1: latency average = 0.264 ms, tps = 3790.944571, failed = 0
libpq run 2: latency average = 0.305 ms, tps = 3275.727482, failed = 0
libpq run 3: latency average = 0.263 ms, tps = 3797.936126, failed = 0
libpq average: latency ~= 0.277 ms, tps ~= 3621.536
```

This makes the current full `pgbench --homer` path about `1.40x` faster by average TPS for these three short 10k-transaction single-client runs. By medians, Homer is `5101` TPS versus libpq `3791` TPS, about `1.35x`. The long-lived service affinity was confirmed in the wrapper's `/proc` snapshots (`cpus_allowed_list=2`). The pgbench worker and socketless backend are short-lived enough that the before/after process snapshots usually do not catch them; their placement comes from the in-code `--client-cpu` call and the v9 backend spawn protocol.

Tail-latency note:

Older `pgbench` summaries in this branch did not print percentiles; [`printResults()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:7088) printed average latency/TPS, and [`printSimpleStats()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:6929) added stddev only in modes that collect full stats. The first p99 numbers above were computed from the normal `-l` per-transaction log path: [`doLog()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:5160) writes one raw transaction latency in microseconds at field 3 when `--aggregate-interval` is not used.

Current integrated tail-latency reporting:

- [`--latency-percentiles`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1041) enables exact in-process p50/p95/p99/max reporting for both libpq and `--homer`.
- [`LatencySamples`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:388) is intentionally separate from `SimpleStats`, so default runs do not retain one value per transaction.
- [`reserveLatencySamples()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1539) preallocates the exact per-thread sample budget before benchmark timing for transaction-count runs (`-t`). The setup loop reserves `thread->nstate * nxacts` samples at [`main()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:8105), so the measured `-t` path should only append into an existing vector. Duration runs (`-T`) cannot know their final sample count, so they keep the geometric growth fallback.
- [`processXactStats()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:5280) computes real transaction latency when `--latency-percentiles` is enabled and appends successful, non-skipped transaction samples with [`addLatencySample()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:1559).
- [`printLatencyPercentiles()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:6997) merges per-thread sample buffers, sorts once after the run, and prints p50/p95/p99/max. This is exact, not histogram-estimated.
- [`src/tools/homer_pgbench_single_client_compare.sh`](/data/dbcomm/postgres-citus-separate-comm-stack/src/tools/homer_pgbench_single_client_compare.sh:27) now enables percentile reporting by default for paired Homer/libpq runs; set `LATENCY_PERCENTILES=0` to turn it off.

One logged 10k-transaction run with the same placement (`--client-cpu=3`, service CPU 2, backend CPU 4, seed 9301) produced:

```text
Homer: samples=10000 avg_ms=0.200 p50_ms=0.198 p95_ms=0.208 p99_ms=0.218 max_ms=5.685
libpq: samples=10000 avg_ms=0.269 p50_ms=0.267 p95_ms=0.280 p99_ms=0.299 max_ms=3.025
artifacts: /tmp/pgbench_tail_20260512_021027
```

So in that run Homer had better p50/p95/p99 than libpq, while its maximum was higher because of one outlier.

Integrated percentile smoke after adding `--latency-percentiles`:

```text
Homer, 1000 tx, seed 9401:
latency average = 0.210 ms
tps = 4756.310435
latency p50 = 0.205 ms
latency p95 = 0.216 ms
latency p99 = 0.227 ms
latency max = 4.951 ms

libpq, 1000 tx, seed 9401:
latency average = 0.266 ms
tps = 3756.503447
latency p50 = 0.263 ms
latency p95 = 0.276 ms
latency p99 = 0.286 ms
latency max = 1.959 ms
```

Persistent-session performance:

```text
pinned persistent pause, 1000 tx:
homer_pgbench: transactions=1000 completed=1000 failed=0 scale=1 elapsed_ms=192.423 tps=5196.887 latency_avg_ms=0.192 latency_min_ms=0.180 latency_max_ms=3.157 latency_stddev_ms=0.095
/usr/bin/time: wall=0.20 user=0.19 sys=0.00

pinned persistent pause, 5000 tx:
homer_pgbench: transactions=5000 completed=5000 failed=0 scale=1 elapsed_ms=950.622 tps=5259.714 latency_avg_ms=0.190 latency_min_ms=0.179 latency_max_ms=3.371 latency_stddev_ms=0.046
/usr/bin/time: wall=0.96 user=0.94 sys=0.01
```

This validates that the previous ~4 ms/transaction result was dominated by per-transaction service session and socketless backend lifecycle. It was still not a final apples-to-apples comparison with stock pgbench because `SELECT abalance` was only checked by processed row count and row payloads were not yet materialized through result sinks at that point.

Pgbench-integrated persistent-session comparison from the same Homer-enabled pgbench binary:

```text
pinned pgbench --homer, 1000 tx, random seed 6001:
number of transactions actually processed: 1000/1000
number of failed transactions: 0 (0.000%)
latency average = 0.203 ms
initial connection time = 0.093 ms
tps = 4934.494584 (without initial connection time)
/usr/bin/time: wall=0.21 user=0.19 sys=0.01
pgbench_history delta: 1000

pinned pgbench -M simple over libpq, 1000 tx, random seed 6001:
number of transactions actually processed: 1000/1000
number of failed transactions: 0 (0.000%)
latency average = 0.314 ms
initial connection time = 2.984 ms
tps = 3184.074533 (without initial connection time)
/usr/bin/time: wall=0.33 user=0.00 sys=0.04
pgbench_history delta: 1000

pinned pgbench --homer, 5000 tx, random seed 6002:
number of transactions actually processed: 5000/5000
number of failed transactions: 0 (0.000%)
latency average = 0.192 ms
initial connection time = 0.094 ms
tps = 5213.709972 (without initial connection time)
/usr/bin/time: wall=0.97 user=0.91 sys=0.05
pgbench_history delta: 5000

pinned pgbench -M simple over libpq, 5000 tx, random seed 6002:
number of transactions actually processed: 5000/5000
number of failed transactions: 0 (0.000%)
latency average = 0.313 ms
initial connection time = 3.266 ms
tps = 3197.392974 (without initial connection time)
/usr/bin/time: wall=1.58 user=0.03 sys=0.17
pgbench_history delta: 5000
```

This was the first apples-to-apples-ish result because both paths used pgbench's own script and reporting. It predated sink-backed result delivery, so it was useful only as a control-plane/lifecycle comparison.

After sink-backed result delivery landed, the first unpinned 30 second comparison from the same Homer-enabled pgbench binary was:

```text
pgbench --homer, 30s, 1 client:
number of transactions actually processed: 27217
number of failed transactions: 0 (0.000%)
latency average = 1.102 ms
initial connection time = 0.216 ms
tps = 907.215643 (without initial connection time)

pgbench over libpq, 30s, 1 client:
number of transactions actually processed: 79145
number of failed transactions: 0 (0.000%)
latency average = 0.379 ms
initial connection time = 2.474 ms
tps = 2638.376593 (without initial connection time)
```

This was the first full built-in pgbench comparison with row payloads flowing
through Homer tuple sinks. The gap was about 2.9x in favor of libpq for that
single-client local-host run. The number is historical, not a current
performance claim: at that point the result sink was still backend-created local
POSIX shm rather than service-owned, command completion was still very chatty
for one-row results (`STARTED` plus `COMPLETED`), and only the single-client
one-command-at-a-time path was supported.

Follow-up pinned checks after compile-time logging gates were left off:

```text
pgbench --homer with comment-prefixed SELECT, no result sink, 10000 tx,
before skipping unused tuple-result metadata:
latency average = 0.254 ms
tps = 3943.408927

pgbench --homer with comment-prefixed SELECT, no result sink, 10000 tx,
after skipping unused tuple-result metadata:
latency average = 0.200 ms
tps = 5010.213320

pgbench --homer full built-in TPC-B-like script, sink-backed SELECT, 10000 tx,
after skipping unused tuple-result metadata:
latency average = 0.510 ms
tps = 2156.347716

pgbench over libpq, same binary, 10000 tx:
latency average = 0.224 ms
tps = 4470.712363
```

The no-sink regression is now explained: result-sink support embedded a full `CitusTupleViewContract` in every command completion. `sizeof(CitusRemoteExecCommandCompletion)` is `33776` bytes, mostly from the `33296` byte tuple-view contract, so the no-result path was still paying large memset/copy costs for metadata it did not use. Skipping the contract unless `resultFlags` includes `CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_READY` brought the no-sink path back to the earlier `~5k` range.

Confirmed and fixed performance issues:

- [`HomerClientStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:720) used to discard command completion metadata already returned by the service's `START_COMMAND` response. [`HomerClientStartCommandWithCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:581) now exposes that completion, and [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3259) uses it, avoiding a redundant `POLL_COMMAND_COMPLETION` request for short terminal commands.
- The service now clears `terminalCompletionPendingPeerPoll` when a local `START_COMMAND` response itself carries terminal completion in [`TupleSinkServiceHandleStartCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6655), so the no-result path does not pay a follow-up poll purely for bookkeeping.
- [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:441) is now the local frontend pushed-completion record. [`TupleSinkServiceEnsureClientCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1576) creates one per `CLIENT_SQL_SESSION`, and [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1378) publishes terminal backend completion into it after [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1777) consumes backend state.
- [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:442) is now an 8-slot SPSC ring, not a single-slot or two-slot record. The implementation originally tried both; long pgbench runs exposed `commandSequence=0` mismatches because row-producing SQL can publish more than two visible states before the frontend has copied them.
- [`CitusRemoteExecLocalCompletionMailbox`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:141) is also an 8-slot SPSC ring. This second ring is required for the same reason on the backend-to-service hop: the socketless backend can publish sink-ready `STARTED` and terminal `COMPLETED` close together, and the service must consume those images in order before forwarding them to the frontend ring.
- [`PublishCompletionToMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1012) and [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1439) both reserve the next ring epoch, fill the fixed completion image, and only then publish the epoch with release ordering. The normal path is still allocation-free; a full ring spins instead of overwriting unread state.
- [`HomerClientOpenCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:397) maps that frontend completion record during session open. [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1473) and [`HomerClientWaitCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1547) observe completion epochs directly; [`HomerClientPollCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1612) remains only as a compatibility wrapper and no longer submits a `POLL_COMMAND_COMPLETION` control request for local frontend sessions.
- [`HomerRunCommandAndWait()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3404) now calls [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1473) at [`pgbench.c:3555`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pgbench/pgbench.c:3555), so pgbench can keep draining a result sink while waiting for terminal completion without issuing a completion poll request.
- [`TupleSinkServicePollForCmEvent()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:460) now trusts the nonblocking RDMA CM event channel and does not add a separate `poll()` syscall. Earlier sampling showed lack of local-control progress while inside this path, but that is not evidence that `rdma_get_cm_event()` itself blocks when configured nonblocking.
- [`TupleSinkServiceHasActivePeerWork()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4827) and [`TupleSinkServicePumpPeerConnections()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4873) make idle peer pumping periodic when there is no peer command endpoint or peer-bound sink.
- [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2485) no longer calls `kill(pid, 0)` on every mailbox wait spin; it samples the early-backend-exit check using `HOMER_SERVICE_BACKEND_EXIT_CHECK_INTERVAL`.
- [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8349) is now the common service-progress primitive. [`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8512) uses the full local-control/peer-control/completion/sink/heartbeat mask, while [`TupleSinkServiceWaitForLocalCommandStartup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2485) uses a restricted local-wait shape: target completion every spin and the [`TUPLE_SINK_SERVICE_PUMP_LOCAL_WAIT_BACKGROUND`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:570) all-completion/sink pump every `64` spins.
- Peer-control acceptance remains intentionally excluded from the local client-SQL wait. A peer `START_COMMAND` request can require local control-slot progress during its own startup wait, and the current local request slot is still `REQUEST_READY`; accepting such a peer command here would risk a nested wait that cannot safely pump local control. The remaining future work is a nonblocking/deferred peer-control dispatch mode, not simply adding peer control to the local wait mask.
- [`HomerClientReserveControlSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:248) no longer clears the large response union on the frontend. The service already clears and owns response publication in [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7219).
- [`HomerClientCopyCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:425), [`PublishCompletionToMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:837), [`TupleSinkServiceFillCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1093), and [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1550) now avoid clearing/copying the embedded result queue descriptor and tuple-view contract when `resultFlags == NONE`.

After replacing local frontend completion polling with pushed completion, the
merged client-SQL/basebackup branch was rebuilt and run with the installed
Homer service restarted from the new binary:

```text
artifacts: /data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_push_completion_20260513_111923

pgbench --homer, 10000 tx, run 1:
latency average = 0.212 ms
tps = 4718.890947
latency p50 = 0.198 ms
latency p95 = 0.307 ms
latency p99 = 0.376 ms
latency max = 5.724 ms

pgbench --homer, 10000 tx, run 2:
latency average = 0.199 ms
tps = 5014.605037
latency p50 = 0.197 ms
latency p95 = 0.209 ms
latency p99 = 0.221 ms
latency max = 5.310 ms

pgbench --homer, 10000 tx, run 3:
latency average = 0.201 ms
tps = 4985.691067
latency p50 = 0.198 ms
latency p95 = 0.210 ms
latency p99 = 0.221 ms
latency max = 5.072 ms

pgbench over libpq, same binary/seeds, 10000 tx:
run 1: latency average = 0.302 ms, tps = 3312.639276, p99 = 0.326 ms
run 2: latency average = 0.304 ms, tps = 3292.044511, p99 = 0.325 ms
run 3: latency average = 0.304 ms, tps = 3294.390345, p99 = 0.329 ms
```

All six runs processed `10000/10000` transactions with zero failures. The
steady Homer runs are again near the earlier no-sink `~5k TPS` range even with
sink-backed `SELECT abalance`; the remaining gap from ideal local command cost
is now more likely result-sink setup/teardown and per-command control-slot work
than completion polling.

The first pushed-completion implementation still published a frontend completion
mailbox record even when the service was still building a `START_COMMAND`
response that already carried the same terminal completion. That was redundant
for the one-command-at-a-time pgbench path and added an extra fixed completion
copy plus epoch store on common commands. The service now suppresses that
mailbox publish while `peerCommandResponsePending` is true in
[`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1378);
delayed completions that finish after a `STARTED` response still use the pushed
completion mailbox.

After that hot-path pruning:

```text
artifacts: /data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_push_completion_optimized_20260513_120609

pgbench --homer, 10000 tx:
run 1: latency average = 0.226 ms, tps = 4423.226717, p99 = 0.288 ms
run 2: latency average = 0.197 ms, tps = 5072.323728, p99 = 0.214 ms
run 3: latency average = 0.198 ms, tps = 5058.360838, p99 = 0.214 ms

pgbench over libpq, same binary/seeds, 10000 tx:
run 1: latency average = 0.306 ms, tps = 3272.291998, p99 = 0.333 ms
run 2: latency average = 0.305 ms, tps = 3280.641851, p99 = 0.329 ms
run 3: latency average = 0.303 ms, tps = 3296.053865, p99 = 0.329 ms

extra warm Homer-only run, 10000 tx:
latency average = 0.200 ms
tps = 4990.891623
latency p99 = 0.215 ms
```

The first optimized Homer run appears to be an outlier after service restart.
The following three Homer samples are `5072`, `5058`, and `4991` TPS, which is
back in line with the earlier `~5.1k TPS` no-poll/no-sink control-path result.

After the shared service-progress helper and local client-SQL wait fix landed,
the first implementation pass was correct but slightly below the best prior
steady-state samples because the local hot wait called the generic helper for
the target-mailbox check and could run the periodic background scan before
returning on an already-arrived completion. The final patch restored the direct
target-mailbox check, moved the terminal-state check ahead of the background
scan, and marked the common pump helper inline for the main-loop constant mask.

```text
artifacts: /data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_wait_pump_opt_20260513_122031

pgbench --homer, 10000 tx:
run 1: latency average = 0.198 ms, tps = 5056.843983, p99 = 0.217 ms
run 2: latency average = 0.197 ms, tps = 5063.473168, p99 = 0.214 ms
run 3: latency average = 0.197 ms, tps = 5083.018398, p99 = 0.214 ms

pgbench over libpq, same binary/seeds, 10000 tx:
run 1: latency average = 0.303 ms, tps = 3296.001719, p99 = 0.333 ms
run 2: latency average = 0.304 ms, tps = 3286.075191, p99 = 0.334 ms
run 3: latency average = 0.305 ms, tps = 3275.858398, p99 = 0.335 ms
```

All six wait-pump verification runs processed `10000/10000` transactions with
zero failures. The optimized wait-loop implementation is therefore on par with
the prior pushed-completion checkpoint while making service-side payload/sink
progress possible during local client-SQL waits.

Design status note: this wait-loop fix is a transitional progress fix, not the
target architecture. The better next design is nonblocking command submission:
local and peer `START_COMMAND` handlers should publish backend work and return
immediately, while `STARTED`, result-sink readiness, `COMPLETED`, and `FAILED`
arrive later through pushed completion channels. That removes the nested wait
that forced the current mask/cadence logic and also addresses the deferred
peer-control acceptance problem documented in
[`../../../future-directions/citus/transport/homer_transport_scheduler_and_payload_streams.md`](../../../future-directions/citus/transport/homer_transport_scheduler_and_payload_streams.md).

After local `START_COMMAND` became submit-only and both completion handoffs were
converted to small rings, the longer validation that exposed the single-slot and
two-slot races passed:

```text
artifacts: /data/dbcomm/postgres-citus-separate-comm-stack/tmp/homer_async_start_ring_final_20260513_130349

pgbench --homer, 50000 tx:
run 1: latency average = 0.195 ms, tps = 5131.106440, p99 = 0.212 ms
run 2: latency average = 0.197 ms, tps = 5068.486402, p99 = 0.215 ms

pgbench over libpq, same binary/seeds shape, 50000 tx:
run 1: latency average = 0.306 ms, tps = 3263.546099, p99 = 0.413 ms
run 2: latency average = 0.291 ms, tps = 3434.869920, p99 = 0.333 ms
```

All four final runs processed `50000/50000` transactions with zero failures.
One intermediate 50k run failed before the backend-to-service ring landed; that
failure showed that fixing only the frontend completion mailbox was insufficient.
Operationally, backend-bridge changes require a full extension install and a
postmaster restart, not just `install-service-bin`, because the socketless
backend runs code from installed `citus.so`.

Remaining likely bottlenecks:

- The frontend still uses the fixed control-slot path in
  [`HomerClientSubmitControlRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:415)
  for every command submission. In the full pgbench script that is seven command
  submissions per transaction. Completion is pushed, but command submission is
  not yet a DPU-oriented queue/doorbell design.
- The service still scans all fixed control slots in [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7198) and spins through [`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7314). That is acceptable for a prototype but not a final DPU-oriented queue/doorbell design.
- Sink-backed `SELECT abalance` now drains tuple-view records from a
  service-owned byte-ring result queue. The backend no longer creates/unlinks a
  private POSIX shm result queue, and pgbench closes frontend mappings with
  `unlinkQueue=false`. Remaining result-path overhead is mapping/lifetime churn:
  [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2552),
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2703),
  and
  [`HomerClientCloseResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2827)
  still map/drain/unmap around row-producing commands, while
  [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:740)
  binds/reuses the service-owned send descriptor on the backend side.
- The old `strace -c` evidence showing one frontend `unlink` per transaction
  belongs to the pre-service-owned-sink implementation. It remains useful only
  as evidence for why backend-created result queues were the wrong ownership
  model; it is not the current hot path.
- The backend still uses the normal parse/plan/execute path per SQL string in [`RemoteExecBackendExecuteSqlCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1437). Prepared-mode pgbench is the right future milestone for separating communication-stack overhead from SQL parse/plan overhead.

## May 17, 2026 Remote RDMA Command-Ring Checkpoint

The real two-node pgbench path now uses a multi-slot command mailbox instead of
one frontend/backend command record per session. The shared ABI is
`CitusRemoteExecLocalCommandMailbox` with
`CITUS_REMOTE_EXEC_LOCAL_COMMAND_MAILBOX_SLOTS = 64` and
`commandSlots[]` in
[`remote_execution_backend_protocol.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_backend_protocol.h:34),
with the protocol bumped to `v10` to avoid stale shared-memory reuse.

Current command publication behavior:

- [`HomerClientStartDirectCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1982)
  writes the next frontend command into the ring slot selected by
  `CitusRemoteExecLocalCommandSlotIndex()`, then publishes slot-local
  `readySeq` and the aggregate `publishedEpoch`.
- [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4870)
  forwards remote client-SQL commands directly from that registered frontend
  slot, instead of copying the full command record into
  `clientSqlCommandScratch`.
- On devices/QPs that report whole-message in-order RDMA write placement,
  [`TupleSinkServicePeerConnectionSupportsWholeMessageWriteRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2751)
  allows one fixed-slot self-publishing RDMA write of the existing
  `[CitusRemoteExecLocalCommandRecord][readySeq]` slot. The capability is
  queried during QP setup in
  [`TupleSinkServiceInitConnectionResources()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1311).
- On the current farnet setup, `ibv_query_qp_data_in_order()` reports no
  whole-message support, likely because PCI write ordering is configured as
  relaxed. The active measured path therefore falls back to two RDMA writes:
  an unsignaled command-record write with
  [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4133),
  followed by a tagged `readySeq` write through
  [`TupleSinkServicePostPeerRegisteredCommandBytesRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4191).
- [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2131)
  polls `readySeq == expectedCommandSequence` and delays receiver credit
  (`consumedEpoch`) until after terminal completion publication. This keeps a
  borrowed command-slot record valid while the backend dispatches it.
- Receiver-side lifecycle checks in
  [`TupleSinkServiceCommandMailboxBusy()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5128)
  use the same `readySeq` predicate for cross-node client-SQL receiver sessions,
  so the backend no longer writes the mailbox `publishedEpoch` header on every
  command.

The old `HOMER_SERVICE_CLIENT_SQL_COMMAND_IMM_PUBLISH` experiment is compiled
out for this milestone because its immediate value carried only a session token,
not the slot/sequence information needed for a multi-slot command ring:
[`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:222).

Correctness and warmed remote RDMA performance after the first command-ring
checkpoint:

```text
remote farnet0 -> farnet1, c1/j1, 20000 tx, pgbench_history truncated before each run:
run 1: cold/session outlier, 3552 TPS, p99 0.229 ms, max 1460.511 ms
run 2: 4872 TPS, average 0.205 ms, p99 0.224 ms
run 3: 4296 TPS, average 0.233 ms, p99 0.254 ms

remote farnet0 -> farnet1, c4/j4, 10000 tx/client, pgbench_history truncated before each run:
run 1: 10763 TPS, average 0.372 ms, p99 0.629 ms
run 2: 10711 TPS, average 0.373 ms, p99 0.642 ms
run 3: 10767 TPS, average 0.372 ms, p99 0.632 ms
```

All six final runs completed with zero failed transactions. The first c1 run
after service/Postgres restart still includes a setup-sized transaction outlier;
use warmed repeats for steady-state comparison. The prepared readySeq helper and
backend header-store removal improved the best c1 warmed run slightly and kept
c4 stable, but did not remove the broader c1 run-to-run variance.

The follow-up command-send optimization pass implemented asynchronous
source-slot retirement. Source `consumedEpoch` now advances immediately after a
successful post in
[`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5148),
and local RNIC send CQEs retire outstanding command writes later through
[`TupleSinkServicePollRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1866).
The default build still signals every command write with
`HOMER_SERVICE_CLIENT_SQL_COMMAND_SIGNAL_INTERVAL=1` at
[`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:185),
because deferred signaled checkpoints did not improve this workload.

Deferred CQE experiments:

```text
remote farnet0 -> farnet1, default interval=1 after deferred-CQE code:
c1/j1, 50000 tx/client:
run 1: 4845 TPS, average 0.206 ms, p50 0.204 ms, p99 0.226 ms
run 2: 4294 TPS, average 0.233 ms, p50 0.230 ms, p99 0.258 ms
run 3: 4302 TPS, average 0.232 ms, p50 0.230 ms, p99 0.254 ms

Earlier interval=8/32 tests were correct but did not improve throughput or
tail latency. Reducing CQE count is therefore not the current dominant cost.
```

The current deferred-CQE implementation is intentionally conservative and should
not be treated as the final transport-resource design. It uses a global
outstanding table,
[`TupleSinkServiceClientSqlCommandWriteCompletion`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:687),
and batched retire-through currently scans that table in
[`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1684).
A cleaner successor should move this state into each transport resource/QP:
`nextPostOrdinal`, `completedPostOrdinal`, and an ordered FIFO of minimal pending
command descriptors. A signaled CQE then advances the per-resource frontier and
pops FIFO entries in order, with no global table scan.

There is also a temporary CQ ownership shim. Command, payload, and control
writes currently share some peer send-CQ plumbing. Since `ibv_poll_cq()` consumes
CQEs, a subsystem-specific poller can see another subsystem's completion first.
The RDMA layer therefore stashes cross-subsystem CQEs in
[`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4831)
and
[`TupleSinkServicePollPeerCommandSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4983).
This is correct but not the desired architecture. The next transport-resource
split should give each traffic class/QP ownership of its own post/completion
frontiers; a near-term cleanup would be one central CQ demux per connection
instead of multiple pollers plus stashing.

May 17, 2026 update: the first traffic-class peer-transport split is now
implemented. The critical-control, foreground-payload, and bulk-payload classes
resolve to separate RDMA-CM connections/QPs/CQs in
[`TupleSinkServiceFindOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2599),
and payload open/close uses the stream's lane via
[`TupleSinkServiceSendPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5787).
This removes command/control and bulk payload from the same posted-WR queue, but
does not yet shard client-SQL commands across multiple critical-control QPs.
Validation after a warmed repeat showed c1/j1/t50000 at `4835.21 TPS`, p50
`0.205 ms`, p99 `0.225 ms`, zero failures. A c4/j4/t10000 run was correct
(`40000/40000`, zero failures) but only `7598.59 TPS` with p99 `8.136 ms`; treat
that as remaining multi-client scaling work, not as a solved performance
result.

May 30, 2026 update: peer-control request/response matching now uses explicit
op slots, and the transport exposes async start/poll helpers:
[`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6456)
and
[`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6623).
At this checkpoint, some service call sites still used the blocking compatibility
adapter
[`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6731),
and outgoing RDMA-CM connection setup still waits inside
[`TupleSinkServiceConnectOutgoingPeerConnection()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2780).
The future-direction note now records the full nonblocking plan: convert
RDMA-CM connect/accept into lane phases, bind peer-control ops to
session/stream/control-slot continuations, and keep the warmed transaction hot
path free of new allocations, broad scans, or idle-CQ polling.

May 31, 2026 update: the close/EOS exception has been removed rather than made
into another peer-control operation. [`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8076)
now performs local sender-side cleanup after payload EOS has been represented in
the stream. Normal remote client SQL commands and completions use the direct
command/completion rings; the remaining blocking adapters are setup/cleanup or
legacy backend/backend scaffolding, not the measured pgbench command path.

Validation after the op-slot checkpoint separated standalone and concurrent
measurements:

```text
standalone remote c4/j4 Homer pgbench:
  4000/4000 transactions, zero failures on the cold smoke
  warmed run: 11410.910599 TPS, p50 0.338 ms, p99 0.575 ms

standalone remote RDMA basebackup:
  cold run: 8.62s
  warmed run: 4.42s

concurrent remote c4/j4 Homer pgbench plus remote RDMA basebackup:
  both processes returned exit code 0
  pgbench: 4682.272133 TPS, p50 0.832 ms, p99 1.274 ms
  basebackup: 15.38s
```

The concurrent result was lower than the earlier May 29 concurrent checkpoint
recorded in the transport note (`~9.36k TPS` pgbench and `~4.45s` basebackup),
while standalone c4 pgbench and standalone basebackup remained in the expected
warm bands. A May 30 rerun did not reproduce the drop. With no stale
`remote exec backend` processes visible before the run, standalone c4/t10000
measured `11429.95 TPS`, p50 `0.337 ms`, p99 `0.591 ms`; concurrent c4/t2500
repeats measured `9545.20`, `9636.47`, and `9473.30 TPS` while basebackup
completed in `4.33`, `4.45`, and `4.31s`; and a concurrent c4/t10000 run
measured `9733.60 TPS`, p50 `0.379 ms`, p99 `0.776 ms`, with basebackup
`4.65s`. Treat the earlier `4.68k / 15.38s` artifact as stale/transient unless
it can be reproduced under the same warm-run and cleanup procedure.

Remaining caveat: the command ring is multi-slot, but the current correctness
target is still pgbench's strict one-command-at-a-time session behavior. The
async source-retirement work removes the inline wait on a single source slot,
but it is not SQL command pipelining. True command pipelining still needs an
explicit remote receiver-credit frontier before the sender may safely wrap
remote command slots, and that belongs with the multi-resource transport work.

## Deliberate Limitations

- Homer workload clients must currently run as the same OS user that owns the
  control shared memory (`dbcomm`) because the service creates the control
  region with `0600`.
- The prototype Homer pgbench mode takes database/user OIDs
  (`--homer-database-oid 5 --homer-user-oid 10`) instead of doing frontend
  authentication or catalog lookup.
- The pgbench integration takes prototype OID flags (`--homer-database-oid 5 --homer-user-oid 10`) and still uses libpq for setup/version/table-info/vacuum work. The measured workload traffic goes through Homer only in `--homer` mode.
- `--homer` currently supports simple query mode, explicit transaction lifecycle
  SQL, multiple local clients/threads with one session per client, no reconnect
  mode, and no retry mode. Unsupported combinations are rejected at startup.
- `SELECT abalance` now uses sink-backed byte-ring tuple-result delivery in
  `pgbench --homer`. The standalone `homer_pgbench` runner has been removed, so
  current performance claims should use the integrated pgbench path.
- Row-producing result shape is currently validated and carried as a generic
  tuple-view contract, but frontend mapping reuse is only complete for the
  stable pgbench result shape. Backend-side
  [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:740)
  can reopen the service-owned result sink when a later `SELECT` has a different
  `TupleDesc`; frontend-side
  [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:2552)
  still errors if an already-open result mapping points at a different queue
  descriptor instead of closing/remapping. Supporting different `SELECT` shapes
  in one session therefore remains future work.
- `scalarInt64` fields still exist in shared completion structs for older/backend wrapper compatibility. They are no longer the client SQL result path and should be removed once the older shortcut is retired.
- Command completion metadata still lacks SQL command tags, SQLSTATE, and authoritative dynamic session state.
- The implementation now supports multiple local pgbench client sessions over
  real RDMA, with
  one command in flight per session. It does not solve command pipelining,
  remote receiver-credit tracking for pipelined command rings, transport-class
  QP separation, peer-control pipelining, cancellation, or prepared statements.
- `START_COMMAND` completion is asynchronous/pushed for the normal local
  client-SQL path. Command submission still uses fixed control slots and remains
  a future DPU/control-queue design target.

## Plan Coverage

Completed from the original client SQL plan:

- distinct `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`
- local service open/start path for client SQL sessions, with local frontend
  terminal completion now pushed through a per-session completion mailbox
- typed `CLIENT_SQL_TX_BEGIN` plus typed commit/abort reuse
- reusable frontend-safe Homer client API in `remote_execution_client.h` / `homer_client.c`
- fixed-width SQL command execution through the socketless backend
- frontend no-libpq runner for the simple pgbench transaction shape
- single-client smoke verification with initialized pgbench tables
- persistent client SQL session reuse across many transactions
- persistent socketless backend reuse across many transactions
- explicit typed client SQL session close command
- opt-in upstream `pgbench --homer` mode for the single-client persistent-session benchmark
- code-level pgbench frontend pinning for both Homer and libpq comparison runs
- code-level Homer service and socketless-backend pinning through service environment variables and the backend spawn protocol
- repeatable single-client comparison wrapper that records process placement artifacts
- same-binary baseline comparison against libpq simple mode
- generic tuple-result sink materialization for row-producing simple SQL, including pgbench's `SELECT abalance`
- shared service-progress helper plus local client-SQL wait fix for all-completion and sink/payload background progress

Partially completed:

- command completion metadata: processed row count, bounded detail string, result sink readiness, and result EOS ordering work; SQL command tags, SQLSTATE, and authoritative dynamic session state remain missing
- result modes: `TUPLE` is now backed by a service-owned tuple sink for client SQL, but richer frontend result accessors and frontend remap support for changed tuple shapes remain future work
- service-progress fairness: the measured local and remote client-SQL paths no
  longer depend on nested startup/completion waits, but older backend/backend
  peer command paths still retain wait/poll scaffolding.
- async command submission: done for the normal client-SQL pgbench path through
  pushed command completions and direct remote command rings; still future for
  the older backend/backend peer `START_COMMAND` path.

Not completed:

- prepared-mode pgbench
- concurrent client/session correctness is implemented for the current
  one-command-at-a-time pgbench model; scaling and transport-resource isolation
  remain future work
- frontend auth/user-name mapping
- basebackup or network-interference benchmark integration

## Related

- [`../../../future-directions/postgres/client-sql-session/client_sql_session_offload_plan.md`](../../../future-directions/postgres/client-sql-session/client_sql_session_offload_plan.md): motivating design and remaining future work.
- [`../../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`](../../../future-directions/citus/data-movement/command_dispatch_completion_plane.md): shared command/completion plane design.
