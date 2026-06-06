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

## June 6, 2026 Batch-Size Sweep

Baseline workload:

- input: `/tmp/homer_tuple_sink_copy_10m.csv`
- table: `homer_tuple_sink_copy_bench (id int, v bigint, payload text)`
- size: 10,000,000 rows, about 323 MB
- raw artifacts: `/tmp/homer_b2b_copy_batch_sweep_1780764011`

Results:

| Tuple Target | Slot Capacity | Result |
| --- | --- | --- |
| 16 | 1024 bytes | correct; measured runs 5.48 s and 5.29 s after warmup |
| 64 | 1024 bytes | correct; measured runs 5.42 s and 5.34 s after warmup |
| 64 | 4096 bytes | initially failed; fixed follow-up measured 5.37 s, 5.21 s, and 5.30 s after warmup |

Raising `citus.experimental_tuple_sink_batch_tuple_target` from `16` to `64`
without increasing `citus.experimental_tuple_sink_slot_capacity_bytes` did not
move throughput. That supports the suspicion that the default 1024-byte slot
capacity caps the effective tuple count per record before the soft tuple target
matters.

Increasing slot capacity to 4096 bytes initially exposed a correctness/transport
issue. The SQL client reported:

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

The warmed average was about 5.29 s, which is only modestly better than the
1024-byte slot runs and still close to the vanilla Citus/libpq baseline. That
keeps copy-reduction and RDMA loop efficiency as the more interesting future
directions.

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

After the RDMA failure, a service-only restart was not sufficient for trustworthy
measurement. A full PostgreSQL plus Homer restart on both hosts recovered the
default path; the post-clean-restart default smoke succeeded in
`/tmp/homer_b2b_copy_post_clean_restart_smoke_1780764262`. That smoke was cold
and should not be used as a steady-state throughput datapoint.

## Future Work Options

### 1. Fix and measure larger tuple-view records first

Before copy-reduction work, make larger tuple byte-ring slots reliable. The
current 4096-byte slot failure means we cannot yet test whether fewer semantic
records, fewer RDMA writes, fewer completions, and fewer service iterations
recover the small Homer-vs-vanilla COPY gap.

Useful counters for this step:

- tuple-view records produced
- average and histogram of tuples per record
- payload bytes per record
- RDMA write descriptors per COPY
- send completions and receiver head acknowledgements
- receiver empty polls and blocked-on-local-receive-queue events

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
