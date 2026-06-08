# Homer Tuple COPY Batching and Copy-Reduction Options

## Purpose

Record the current backend-to-backend Homer COPY batching findings and the
future design options for reducing tuple payload copy/materialization costs. This
is future-direction material: it should not be read as a completed change to the
current tuple-sink substrate.

## Current Behavior

The Citus backend-to-backend COPY path does not send per-row libpq COPY payloads
over Homer. The coordinator-side backend opens a tuple-sink send stream, then
the current tuple-view substrate materializes each tuple into a Homer-owned
batch record.

Important current code anchors:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:684`:
  `NormalizeTupleSinkAttribute()` prepares per-attribute state and detoasts
  variable-width values into the sink scratch context when needed.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1405`:
  `TryAppendTupleViewToCitusTupleSinkBatch()` appends one normalized tuple into a
  reserved tuple-view batch.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1511`:
  by-reference attributes are copied into the Homer-owned tuple-view payload.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3630`:
  `BorrowNextRemoteTupleView()` borrows receiver-side tuple values from the
  receive batch into caller-owned slot arrays.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:389`:
  the worker insert loop keeps the borrowed tuple view live through
  `ExecSimpleRelationInsert()`.

The receiver-side borrow is already present. The sender-side path still has an
ownership boundary where PostgreSQL/Citus slot data is converted into a
Homer-owned tuple-view byte image.

## June 6, 2026 Batch-Size Sweeps

Baseline workload:

- input: `/tmp/homer_tuple_sink_copy_10m.csv`
- table: `homer_tuple_sink_copy_bench (id int, v bigint, payload text)`
- size: 10,000,000 rows, about 323 MB
- current raw artifacts:
  - `/tmp/homer_b2b_copy_10m_batch_sweep_1780783606`
  - `/tmp/homer_b2b_copy_10m_batch_sweep2_1780783711`

Current post-peer-push results:

| Tuple Target | Slot Capacity | Result |
| --- | --- | --- |
| default old (`16`) | default old (`1024` bytes) | correct; `62.93 s` |
| `64` | `4096` bytes | correct; warmed around `44.96 s` |
| `256` | `16384` bytes | correct; `21.42 s` |
| `1024` | `65536` bytes | correct; `11.46 s` |
| `4096` | `262144` bytes | correct; `8.00 s` |
| `8192` | `524288` bytes | correct; `7.64 s` |
| `16384` | `1048576` bytes | correct; `8.37 s` |

The current default is now `8192` tuples and `524288` bytes. This is a
pragmatic default, not a final transport result: it removes the pathological
small-record overhead exposed by the post-peer-push service loop, while avoiding
the slight regression seen at `16384` / `1048576`.

Earlier in the same bring-up, increasing slot capacity to 4096 bytes initially
exposed a correctness/transport issue. The SQL client reported:

```text
remote execution control request timed out waiting for service progress
```

The receiver service reported:

```text
byte-ring tuple incoming head publish failed ... detail=payload send completion failed: opcode=0 status=10
```

The follow-up fix separated receiver ACK WR-count bookkeeping from byte-frontier
distance, kept the sender-owned tuple byte-ring head mirror registered through
connection cleanup rather than exact-stream teardown, skipped terminal tuple
byte-ring credit ACKs after in-band EOS, and added local byte-ring target range
validation before RDMA posts. The fixed 4096-byte run completed one cold warmup
and three warmed repeats:

```text
/tmp/homer_b2b_copy_slot4096_fix2_1780765554
run=1 real=8.24 correct
run=2 real=5.37 correct
run=3 real=5.21 correct
run=4 real=5.30 correct
```

That pre-peer-push 4096-byte result was useful for bring-up, but it should not be
treated as the current baseline. After the later scheduler/peer-completion work,
small tuple records were much slower (`64` / `4096` around 45 s), while the
larger default initially recovered the path to about 7.3-7.6 s. Rechecking the
old `65207cd4e` baseline in a detached worktree timed out with ready-set
overflow, so the earlier near-vanilla number is historical evidence rather than
a stable current comparison.

The comparable vanilla Citus baseline on the same 10M-row input was:

```text
/tmp/citus_vanilla_copy_10m_baseline_1780783569
run=1 real=5.18 correct
run=2 real=5.11 correct
run=3 real=5.11 correct
```

Latest clean recheck after the shared-path SQL result-sink geometry fix and
process preflight no longer shows a Homer-vs-vanilla wall-time gap for the
default geometry:

```text
/tmp/homer_tuple_copy_investigate_baseline_1780796369
Homer: 5.39, 5.04, 5.20, 5.95 s

/tmp/homer_tuple_copy_investigate_recheck2_1780796468
Homer: 5.13, 5.23, 5.07, 5.17 s

/tmp/citus_vanilla_copy_recheck_1780796433
vanilla Citus: 5.20, 5.22, 5.27, 6.32 s
```

The older 7.3-8.0 s bands remain useful as runtime-state sensitivity evidence,
but they are not the current accepted default-geometry baseline. The
copy-reduction ideas below remain useful future work for lowering CPU cost,
reducing sensitivity to warmup/runtime state, and making the tuple-view path a
stronger transport substrate, not for fixing an active measured wall-time
regression against vanilla on this 10M-row workload.

Parity with vanilla Citus is only a no-regression checkpoint. The expected
research target is better-than-vanilla COPY throughput because Homer's
tuple-view path avoids full libpq COPY serialization/deserialization and should
spend less CPU per tuple once the remaining transport/materialization/scheduling
costs are under control. Future optimization work should treat persistent parity
as evidence that one of those expected savings is being lost.

The relevant code area is the tuple byte-ring RDMA path:

- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6666`:
  `HomerServiceEnsureLocalReceiveByteRing()` aliases tuple receive rings to the
  backend-visible byte ring and RDMA-registers that memory.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13242`:
  `HomerServicePumpOutgoingByteRingBaseBackup()` also drives tuple byte-ring
  outgoing publication despite the historical basebackup name.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13883`:
  `TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()` posts
  the data writes plus the tail/doorbell publication.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16584`:
  exact receive attach currently accepts the peer-provisioned sink geometry as
  authoritative even when the worker backend's local GUC defaults request a
  smaller geometry.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:28`:
  `CITUS_TUPLE_SINK_DEFAULT_SLOT_CAPACITY_BYTES` is now `524288`.
- `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:29`:
  `CITUS_TUPLE_SINK_DEFAULT_BATCH_TUPLE_TARGET` is now `8192`.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1224`:
  `HOMER_PROGRESS_PLAN_MAX_GRANTS` is now `64`, so a 32-shard COPY can have
  ready payload sources plus CQ/control/completion work without immediate
  ready-set overflow.
- `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4793`:
  `HomerServiceBuildCoarseProgressReadySet()` uses one aggregate payload source
  when the active payload stream scan limit is dense enough.

After the RDMA failure, a service-only restart was not sufficient for trustworthy
measurement. A full PostgreSQL plus Homer restart on both hosts recovered the
default path; the post-clean-restart default smoke succeeded in
`/tmp/homer_b2b_copy_post_clean_restart_smoke_1780764262`. That smoke was cold
and should not be used as a steady-state throughput datapoint.

## Future Work Options

### 1. Measure and reduce payload-loop per-record overhead

Larger tuple-view records are now reliable enough for the current 10M-row
workload, and the default uses the best measured band. The next question is why
the service loop still spends enough CPU per payload record to trail vanilla
Citus by about 2.3-2.5 s.

Useful counters for this step:

- tuple-view records produced
- average and histogram of tuples per record
- payload bytes per record
- RDMA write descriptors per COPY
- send completions and receiver head acknowledgements
- receiver empty polls and blocked-on-local-receive-queue events
- aggregate-source grants versus per-stream grants
- `HomerServicePumpPayloadStream()` and
  `HomerProgressResultMergePayloadDelta()` CPU profile share

### 2. Direct-fill Homer-owned tuple-view buffers

A realistic copy-reduction direction is to make the Homer-owned tuple-view batch
buffer the producer's destination earlier. This is not zero-copy: tuple bytes are
still written into memory. The intended gain is to avoid a second materialization
boundary where by-reference values first live in backend-local tuple/detoast
memory and are then copied into a Homer batch.

This direction is most plausible if the COPY producer can write the final
tuple-view representation directly into the reserved batch payload while still
preserving PostgreSQL error unwinding, memory-context cleanup, and tuple
semantics.

### 3. Borrow sender-side DB memory only with an explicit lifetime contract

Borrowing sender-side by-reference Datums directly as RDMA payload sources is a
larger design change. PostgreSQL slot values may point into per-tuple memory,
input buffers, detoast scratch memory, or other backend-local storage that can be
reset before the service/RDMA path has completed. RDMA also needs registered and
access-valid memory.

This option should require an explicit ownership contract, such as pinned
producer buffers, registered staging arenas, or scatter/gather descriptors whose
lifetime extends until transport completion. Without that contract, the current
copy into Homer-owned memory is the safer design.

### 4. Use scatter/gather after ownership is solved

If sender-side lifetime and registration are made explicit, RDMA scatter/gather
could reduce header/payload assembly copies. This is not a substitute for the
lifetime contract: SGEs still reference memory that must remain valid until the
RNIC completes the work request.

### 5. Keep peer push completion separate from COPY throughput tuning

Peer push command completion should still remove terminal command-completion
polling and simplify session retirement, but it is unlikely to fix the steady
10M-row COPY throughput gap by itself. COPY throughput tuning should focus first
on tuple-view record size, byte-ring RDMA reliability, and per-record service
overhead.

## Related

- `../transport/homer_transport_scheduler_and_payload_streams.md`: scheduler
  and payload-stream design context, including peer push completion and
  fragmentation directions.
- `../transport/rdma_publication_visibility_and_doorbells.md`: RDMA
  publication and doorbell rules that the tuple byte-ring path must preserve.
- `../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md`:
  current tuple-sink implementation checkpoint.
- `../../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md`:
  older tuple-view batch materialization checkpoint.
