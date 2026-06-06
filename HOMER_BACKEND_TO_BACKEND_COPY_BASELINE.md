# Homer Backend-to-Backend COPY Baseline

Captured: June 6, 2026.

Purpose: preserve the current Citus backend-to-backend Homer COPY workload and
baseline before implementing peer push completions.

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

## Baseline Result

One warmup plus three measured Homer repeats were collected after clean process
preflight and matching installed artifacts on both hosts.

| Run | Result | Real Time |
| --- | --- | --- |
| 1 | warmup, correct | 5.09 s |
| 2 | correct | 5.34 s |
| 3 | correct | 5.58 s |
| 4 | correct | 5.75 s |

Measured repeat average: about 5.56 s, about 1.80M rows/s.

Correctness check on every run:

```text
count=10000000 min=1 max=10000000 sum=500000050000000
```

Raw artifacts:

```text
/tmp/homer_b2b_copy_10m_baseline_1780761813
```

## Vanilla Citus Baseline

The comparable vanilla Citus path uses the same distributed table and input file
with Homer tuple-sink routing disabled:

```sql
SET citus.enable_experimental_tuple_sink_routing=off;
```

| Run | Result | Real Time |
| --- | --- | --- |
| 1 | warmup, correct | 5.18 s |
| 2 | correct | 5.34 s |
| 3 | correct | 5.44 s |
| 4 | correct | 5.19 s |

Measured repeat average: about 5.33 s, about 1.88M rows/s.

Raw artifacts:

```text
/tmp/vanilla_citus_copy_10m_baseline_1780762245
```

## Batch-Size Sweep

Captured on June 6, 2026 after the baseline above, using the same 10M-row input
and table. This sweep changes only Homer tuple-sink batching GUCs.

Raw artifacts:

```text
/tmp/homer_b2b_copy_batch_sweep_1780764011
```

| Tuple Target | Slot Capacity | Run 1 | Run 2 | Run 3 | Result |
| --- | --- | --- | --- | --- | --- |
| 16 | 1024 bytes | 5.01 s | 5.48 s | 5.29 s | correct; measured avg of runs 2-3 about 5.39 s |
| 64 | 1024 bytes | 5.28 s | 5.42 s | 5.34 s | correct; measured avg of runs 2-3 about 5.38 s |
| 64 | 4096 bytes | 5.02 s | n/a | n/a | failed; not a performance datapoint |

Interpretation: raising `citus.experimental_tuple_sink_batch_tuple_target` from
`16` to `64` while leaving `citus.experimental_tuple_sink_slot_capacity_bytes`
at the default `1024` bytes did not improve throughput. The slot byte capacity
is likely limiting the effective tuple count per tuple-view record. Raising slot
capacity to `4096` exposed a transport correctness issue before a throughput
measurement could be collected.

The failed larger-slot run ended with:

```text
ERROR: experimental worker tuple-sink insert command did not complete successfully
DETAIL: shard=102638 placement=631 state=4 detail=remote execution control request timed out waiting for service progress
```

The receiver service log reported an RDMA payload/head publication failure:

```text
tuple-sink service: byte-ring tuple incoming head publish failed session=27 sink=25 head=514998464 detail=payload send completion failed: opcode=0 status=10
```

After that RDMA error, service-only restart was not enough for trustworthy
measurement; a full PostgreSQL plus Homer restart on both hosts was needed. A
post-clean-restart default-geometry smoke succeeded with the 10M-row correctness
check in:

```text
/tmp/homer_b2b_copy_post_clean_restart_smoke_1780764262
```

That smoke was a cold post-restart run (`real 9.09`) and should not be used as a
steady-state throughput datapoint.

Follow-up fix and rerun on June 6, 2026:

- fixed receiver consumed-head ACK bookkeeping to count outstanding ACK WRs
  separately from byte-frontier distance
- kept the sender-owned tuple byte-ring head mirror registered after exact-stream
  teardown so late peer ACK WRs do not hit a deregistered remote key
- suppressed terminal tuple byte-ring credit ACKs after the backend consumed
  in-band EOS and marked the receive ring peer-closed
- added byte-ring remote range validation before payload/tail RDMA posts

Raw artifacts:

```text
/tmp/homer_b2b_copy_slot4096_fix2_1780765554
```

| Tuple Target | Slot Capacity | Run 1 | Run 2 | Run 3 | Run 4 | Result |
| --- | --- | --- | --- | --- | --- | --- |
| 64 | 4096 bytes | 8.24 s | 5.37 s | 5.21 s | 5.30 s | correct; measured avg of runs 2-4 about 5.29 s |

The first run was a cold post-restart warmup. Runs 2-4 all produced:

```text
count=10000000 min=1 max=10000000 sum=500000050000000
```

## Regression Smokes

After the backend-to-backend COPY fixes, the other Homer workloads were smoke
checked without a PostgreSQL/service restart between warm repeats:

- Before the 4096-byte slot follow-up: remote RDMA pgbench c1, warmed:
  `10000/10000` transactions, `0` failures, `4730 TPS`, p50 `0.208 ms`, p95
  `0.223 ms`, p99 `0.233 ms`.
- Before the 4096-byte slot follow-up: remote RDMA basebackup blackhole: warmup
  `9.18 s`, repeats `4.59 s` and `4.65 s`, all completed.
- After the 4096-byte slot fix: remote RDMA pgbench c1, warmed:
  `10000/10000` transactions, `0` failures, `4609 TPS`, p50 `0.213 ms`, p95
  `0.228 ms`, p99 `0.239 ms`; artifact
  `/tmp/homer_pgbench_regression_after_copy_fix_warm_1780765620.log`.
- After the 4096-byte slot fix: remote RDMA basebackup blackhole completed:
  warmup `6.43 s`, warmed `4.68 s`; artifacts
  `/tmp/homer_basebackup_regression_after_copy_fix_1780765634`.

Basebackup artifacts:

```text
/tmp/homer_basebackup_regression_1780762311
```

Installed artifact hashes for this baseline:

```text
17014869e26946457fbb8c9ea77454cb4e86f70fffa58cfd60ce29266ccbf48d  /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so
53f0c9c1cb8735c2e42e4981545825731aa2d026d489c77af01537a6633f9160  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service
```

Installed artifact hashes after the 4096-byte slot fix:

```text
0b6ce8d0ed9dccfb93b6be5510d21d9d6a2c080b8c442c112355884981632943  /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so
58bdd0bca77f7dc3630b148971eadf49643379a00d7560ae3c446caf0df0cd4f  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service
```

## Notes From Bring-Up

The workload did not run cleanly before this baseline. Correctness issues fixed
along the way:

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
- A normal-path peer-control fallback print was gated behind the peer transport
  stats macro; it was too noisy for performance runs.
