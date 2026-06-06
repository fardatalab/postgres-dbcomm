# Homer Backend-to-Backend COPY Baseline

Captured: June 6, 2026.

Purpose: preserve the current Citus backend-to-backend Homer COPY workload,
including the post-peer-push-completion baseline and the remaining performance
gap against vanilla Citus.

## Workload

- Coordinator: `farnet1`.
- Worker placement: `10.10.1.100:5432` on `farnet0`.
- Data path: Citus coordinator backend to Homer service on `farnet1`, RDMA to
  Homer service on `farnet0`, then socketless Citus remote exec backend ingest.
- Table: `homer_tuple_sink_copy_bench (id int, v bigint, payload text)`.
- Input file: `/tmp/homer_tuple_sink_copy_10m.csv`.
- Input size: 10,000,000 rows, about 323 MB.
- Homer GUCs:
  - `citus.enable_experimental_tuple_sink_routing=on`
  - `citus.enable_experimental_tuple_sink_loopback_validation=off`

Correctness check on every accepted run:

```text
count=10000000 min=1 max=10000000 sum=500000050000000
```

## Current Homer Result

This is the current default after peer service-to-service push command
completion and after changing the tuple-sink default batch geometry to
`8192` tuples / `524288` bytes.

Raw artifacts:

```text
/tmp/homer_b2b_copy_10m_default_after_batch_default_1780784020
```

| Run | Result | Real Time |
| --- | --- | --- |
| 1 | warmup, correct | 11.47 s |
| 2 | correct | 7.46 s |
| 3 | correct | 7.62 s |
| 4 | correct | 7.32 s |

Measured repeat average: about 7.47 s, about 1.34M rows/s.

Interpretation: the pathological small-batch default is fixed, but Homer is
still slower than vanilla Citus for this workload. Peer push completion removes
the normal terminal command-completion poll round trip, but the steady 10M-row
COPY gap is now dominated by payload-loop and per-record work, not by terminal
command completion.

## Vanilla Citus Baseline

The comparable vanilla Citus path uses the same distributed table and input file
with Homer tuple-sink routing disabled:

```sql
SET citus.enable_experimental_tuple_sink_routing=off;
```

Raw artifacts:

```text
/tmp/citus_vanilla_copy_10m_baseline_1780783569
```

| Run | Result | Real Time |
| --- | --- | --- |
| 1 | warmup, correct | 5.18 s |
| 2 | correct | 5.11 s |
| 3 | correct | 5.11 s |

Measured repeat average: about 5.11 s, about 1.96M rows/s.

## Post-Peer-Push Batch Sweep

Captured on June 6, 2026 after peer service-to-service push completion,
aggregate payload scheduling, close/lifecycle fixes, and clean rebuild/sync.
This sweep changes only Homer tuple-sink batching GUCs.

Raw artifacts:

```text
/tmp/homer_b2b_copy_10m_batch_sweep_1780783606
/tmp/homer_b2b_copy_10m_batch_sweep2_1780783711
```

| Tuple Target | Slot Capacity | Result |
| --- | --- | --- |
| default old (`16`) | default old (`1024` bytes) | correct; `62.93 s` |
| `64` | `4096` bytes | correct; warmed around `44.96 s` |
| `256` | `16384` bytes | correct; `21.42 s` |
| `1024` | `65536` bytes | correct; `11.46 s` |
| `4096` | `262144` bytes | correct; `8.00 s` |
| `8192` | `524288` bytes | correct; `7.64 s` |
| `16384` | `1048576` bytes | correct; `8.37 s` |

The default was raised to `8192` tuples and `524288` bytes because that is the
best measured band from this sweep. Larger records did not continue improving
throughput.

An opt-in `HOMER_PROGRESS_POLICY=cpu-liveness` check confirmed that scheduling
policy selection was not the main fix:

```text
/tmp/homer_b2b_copy_10m_policy_cpu_liveness_1780783859
64:4096      50.44 s correct
8192:524288   7.41 s correct
```

## Peer Push Completion And Lifecycle Fixes

The current code adds a requester-service-owned peer command completion ring for
service-to-service command sessions. The backend maps that ring directly and
`PollRemoteExecutionCommandCompletion()` checks it before using the old local
service `POLL_COMMAND_COMPLETION` fallback. The responder writes completion
events into the requester ring when it consumes the backend completion mailbox.

Important code anchors:

- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:352`:
  `CitusRemoteExecPeerCommandCompletionRingDescriptor`.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:501`:
  `CitusRemoteExecPeerCommandCompletionEvent`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1908`:
  `TryConsumeRemoteExecutionPeerCommandCompletion()`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2928`:
  `PollRemoteExecutionCommandCompletion()` prefers cached/ring completion before
  falling back to local-service polling.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8090`:
  `TupleSinkServicePublishPeerCommandCompletion()` publishes the peer event and
  epoch over RDMA.

The same implementation slice fixed two lifecycle issues observed during tuple
COPY profiling:

- byte-ring receive now preserves `receivedPeerClosePending` until the peer close
  is drained through the normal payload stream pump
  (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15866`);
- exact sink close is idempotent when a stream has already been reclaimed and
  the owning session has no active sinks left
  (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19302`).

## Regression Smokes

After the peer-push and tuple-COPY default changes, the shared Homer paths were
smoke checked:

- Remote Homer pgbench c1 after rebuilding/installing matching Postgres and
  Citus/Homer artifacts:
  `/tmp/homer_pgbench_remote_c1_warm_after_pg_rebuild_1780784173`, `20000/20000`
  transactions, `0` failures, `4303.6 TPS`, p50 `0.211 ms`, p95 `0.223 ms`,
  p99 `0.235 ms`.
- Remote RDMA basebackup blackhole:
  `/tmp/homer_basebackup_remote_regression_1780784191`, run 1 `6.73 s`, run 2
  `4.81 s`, both completed.
- Final process preflight showed only intended PostgreSQL and Homer service
  processes on both hosts, with no stale `pgbench`, `pg_basebackup`, or
  `postgres: remote exec backend`.

## Historical Pre-Peer-Push Notes

Earlier in the day, before the W3 peer-push changes, this workload was measured
around 5.3-5.6 s with smaller tuple records. That result did not reproduce after
the later scheduler/peer-completion work: rechecking the old commit in a
detached worktree timed out with ready-set overflow. Treat the old numbers as
historical bring-up evidence only, not as the current baseline.

Correctness issues fixed during the initial bring-up:

- Async local-control failures for non-open requests were reported with the
  wrong response kind.
- Socketless backend spawn originally rejected tuple-sink/copy-ingest session
  op kinds.
- COPY command payload publication copied inactive union members and overwrote
  the active copy-ingest payload.
- The byte-ring receiver did not skip wrap padding larger than a transport
  header.
- Sender-side close cleared the peer binding before publishing tuple byte-ring
  EOS.
- The backend receiver did not turn in-band tuple byte-ring EOS into the
  backend-visible peer-closed flag.
- Receiver consumed-head ACK bookkeeping conflated outstanding ACK WR count with
  byte-frontier distance.
- Late peer ACK WRs could target a deregistered sender-owned byte-ring head
  mirror.
