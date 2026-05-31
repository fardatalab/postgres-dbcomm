# Byte-Ring Payload Stream Checkpoint

## Summary

The current implementation has a common byte-ring Homer payload substrate for
basebackup streams and tuple/result payload streams. PostgreSQL no longer writes
basebackup objects or client-SQL tuple results into fixed producer slots on the
hot path. Producers reserve byte-ring record space, write payload bytes directly
into the reserved record, and publish an absolute producer byte tail. The sender
packs complete byte-ring records into the receiver-owned RDMA byte ring and
publishes an absolute receiver byte tail with a final `RDMA_WRITE_WITH_IMM`.

The two current record shapes are:

- basebackup: `CitusTupleSinkTransportHeader +
  CitusRemoteBaseBackupMessageHeader + payload`
- tuple/result: `CitusTupleSinkTransportHeader + CitusTupleSinkBatchHeader +
  tuple-view row bytes`

Terminology note: this current implementation uses **byte-ring records**. It
does not implement the deferred one-RDMA-op grant-boundary record design. If we
later introduce `HomerPayloadRecordHeader` or a grant-end marker record, that
will be a separate follow-up to this checkpoint.

This checkpoint supersedes the earlier partial basebackup-only byte-ring
migration. The byte ring is enabled by object family in
[`HomerServicePayloadStreamUsesByteRing()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1284), currently for
`HOMER_PAYLOAD_OBJECT_FAMILY_BASE_BACKUP_STREAM` and
`HOMER_PAYLOAD_OBJECT_FAMILY_TUPLE_VIEW_BATCH`.

## Protocol Objects

[`CitusHomerPayloadByteRingControl`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:251)
is the byte-ring control block. Its hot fields are `publishedTail` and
`consumedHead`, both absolute byte offsets. The same control block now covers
receiver-owned RDMA rings and producer-owned local shared-memory rings. The
ring storage begins after the control block, at `ringAddress + controlBytes` for
RDMA descriptors.

[`CitusHomerPayloadByteRingDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:260)
carries the receiver-local RDMA address, rkey, control size, ring bytes, and
flags. The peer-open response includes it as
[`peerReceiveByteRingDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_peer_control_protocol.h:117).

[`CitusTupleSinkQueueDescriptor.descriptorFlags`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:294)
now carries `CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING` for backend- and
frontend-visible tuple/result queues. This keeps tuple-view contracts as the DB
semantic object description while changing only the queue substrate underneath.

The peer protocol version was bumped because the peer-open response layout
changed. A sender and receiver service must therefore be rebuilt and restarted
together.

The local producer byte ring has a distinct terminal bit,
`CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_CLOSED`, at
[`tuple_sink_protocol.h:36`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:36).
Do not reuse `CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_CLOSED` for byte-ring flags: bit
0 already means receiver-owned in the byte-ring flag space.

## Open and Binding Flow

On local stream creation,
[`HomerServiceCreatePayloadStreamEntry()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5361)
allocates a producer-owned byte ring for byte-ring payload streams through
[`HomerServiceMapProducerByteRingQueue()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4736).
`slots=` remains a capacity multiplier in the target detail, but for byte-ring
streams it no longer describes fixed producer slots on the hot path.

On the sender service, [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5994)
checks whether the stream uses the byte ring. For byte-ring streams it validates
the peer's byte-ring descriptor and stores it with
[`HomerServiceRecordPeerReceiveByteRing()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5612).

On the receiver service, [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6229)
allocates and registers the receiver-owned byte ring through
`HomerServiceEnsureLocalReceiveByteRing()`. Basebackup uses a service-owned
blackhole receive byte ring; tuple/result streams alias the local receive byte
ring to the backend-visible receive queue so the peer can RDMA write directly
into bytes drained by the worker/frontend tuple sink consumer.
Close/drain checks use [`HomerServiceReceivePayloadTransportIsDrained()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5809),
which compares byte-ring `publishedTail` and `consumedHead` for byte streams.

## Send Path

[`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7649)
performs common producer-ring registration and dispatches byte-ring streams to
[`HomerServicePumpOutgoingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7045).
The function name is now stale: the code path is generic for basebackup and
tuple-view batch streams, with object-family validation inside the byte-ring
sender.

The byte-ring sender keeps two frontier domains:

- object sequence frontiers: `payloadSenderPostedTail`,
  `payloadSenderCompletedHead`, and `payloadSenderLastSignaledTail`
- source byte-ring frontiers: `payloadSourceBytePostedTail`,
  `payloadSourceByteCompletedHead`, and `payloadSourceByteLastSignaledTail`
- remote byte-ring frontiers: `payloadByteSenderPostedTail` and
  `senderVisibleRemoteConsumedHead`

The object frontier preserves semantic ordering and stats. The source byte
frontier controls when PostgreSQL producer bytes can be released. The remote
byte frontier controls receiver-ring credit.

Each batch parses already-published producer byte-ring records, validates ready
backend objects in place, computes the remote byte-ring offset, skips the unused
trailer at wrap if one whole record will not fit, coalesces adjacent local and
remote byte ranges when possible, and fills a local array of
`TupleSinkServicePeerPayloadWriteDescriptor`s. It then posts the batch with
[`TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3826).

That RDMA helper posts payload `IBV_WR_RDMA_WRITE` WRs followed by one signaled
`IBV_WR_RDMA_WRITE_WITH_IMM` to the receiver's `publishedTail` word. The
immediate value identifies the stream; the small write payload is the absolute
byte tail. The completion id now carries the source byte frontier. The service
maps that byte completion back to the semantic object frontier through
[`HomerServiceTrackPayloadCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1333)
and
[`HomerServiceCompleteTrackedPayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1373).

One subtle correction: `TupleSinkServicePollPeerPayloadSendCompletionRdma()` may
drain multiple send CQEs for the same stream in one `ibv_poll_cq()` batch and
return only the newest frontier. The completion tracker therefore treats a
completed byte tail as cumulative and retires all tracked batches up to that
frontier. Exact one-CQE-per-service-poll matching was wrong and caused false
`byte-ring-completion-tracking-failed` terminals.

One subtle implementation fix: the connection now reserves publish-tail staging
buffers with a monotonic connection-local cursor in
[`TupleSinkServiceReservePayloadPublishTailSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:217).
Indexing the staging word by `tailValue % N` is unsafe once `tailValue` can be a
byte offset, because byte offsets can alias a small staging ring while earlier
WRs are still outstanding.

## Receive Path

[`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9013)
dispatches byte-ring streams to
[`HomerServicePumpIncomingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8734).
This function name is also stale after the tuple/result migration.

For basebackup, the receiver consumes data doorbells, reads the byte-ring
`publishedTail`, and parses complete byte-ring records from its local
`consumedHead` up to that byte frontier.
When a wrap trailer does not contain the expected object sequence and the
published tail has crossed the ring boundary, the receiver treats the trailer as
skipped space and advances to offset zero.

For tuple/result streams, the service is intentionally passive on payload bytes:
the peer writes directly into the backend-visible receive byte ring, and
[`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1393)
parses the tuple-view record for the worker/frontend consumer. The service only
drains data doorbells and returns receiver-head ACKs based on the consumer's byte
`consumedHead`.

The basebackup receive cursor is kept in registers while parsing one doorbelled
byte range.
`consumedHead` is stored back to the receiver-local control block once per
drain pass in
[`HomerServicePumpIncomingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8734).
Remote credit is returned by
[`HomerServicePostReceiverHeadAck()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8520),
so per-record stores to receiver-local `consumedHead` are not needed on the hot
path.

## Current Limitations

- The first implementation still uses two RDMA operations for publication:
  payload WRs plus a final tail `RDMA_WRITE_WITH_IMM`. The one-RDMA-op in-band
  grant-boundary record publication design remains future work.
- The byte ring grants whole records only. There is no fragment/reassembly layer
  yet for very large semantic objects.
- The producer-side reservation API is record-capacity based. A tiny lifecycle
  record near the end of the ring may still skip the trailing bytes if the
  maximum record capacity would not fit there. This preserves simple no-wrap
  record parsing; finer packing would need a reservation API that knows the
  record size before returning payload memory.
- Some service helper names still say `BaseBackup` even though the functions are
  now the generic byte-ring path for basebackup and tuple-view batch streams.
  This is naming debt, not a data-path limitation.
- Tuple/result queues are byte-ring-only on the active backend/client API path.
  Descriptors without `CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING` are
  rejected at open/validation time. The protocol still contains some
  fixed-slot-era structs and names for historical/control compatibility, but the
  measured tuple/result payload hot path no longer dispatches between slot and
  byte-ring queue implementations.
- Tuple/result byte-ring receive currently uses whole-record visibility and
  direct receive-ring aliasing. Fragment/reassembly and the one-RDMA-op
  grant-boundary publication design remain future work.

## Verification

Build/installation used the no-stats performance shape:

```sh
cd /data/dbcomm/citus-dbcomm-separate-comm-stack
make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
sudo -n make install-headers install-service-bin install

cd /data/dbcomm/postgres-citus-separate-comm-stack
env CCACHE_DISABLE=1 ninja -C build src/backend/postgres src/bin/pg_basebackup/pg_basebackup
sudo -n meson install -C build --no-rebuild
```

Because the peer protocol changed, `/data/dbcomm/pg-citus/` was synced to
`farnet0` and both Homer services were restarted with explicit RDMA bind hosts:

```sh
HOMER_SERVICE_PEER_BIND_HOST=10.10.1.101 HOMER_SERVICE_PEER_PORT=9717
HOMER_SERVICE_PEER_BIND_HOST=10.10.1.100 HOMER_SERVICE_PEER_PORT=9717
```

Remote RDMA basebackup correctness smoke succeeded with:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
  -v
```

Measured remote RDMA warm-run results on May 16, 2026 after the producer-side
byte-ring conversion and hot-path cleanup:

- first run after restart: `7.83s`, treated as cold setup/warmup
- warmed remote RDMA repeats: `4.17`, `4.24`, `4.22`, `4.25s`
- warmed local blackhole repeats: `3.95`, `3.95`, `4.08s`

Treat remote RDMA warmed steady state as roughly `4.17-4.25s` in this
checkpoint, an improvement over the previous `4.5-4.7s` service-to-service
byte-ring checkpoint. The remaining gap to local blackhole is now smaller, but
still points at transport-side overhead rather than producer-slot layout.

The final optimization pass removed full `CITUS_REMOTE_EXEC_PEER_ERROR_BYTES`
buffer clears from the active service pump paths and replaced them with
single-byte terminators before error-producing calls. The old clears happened on
the service loop even on successful iterations, so they were unnecessary repeated
memory traffic rather than useful diagnostics.

After the full tuple/result byte-ring migration and tuple-sink allocation
cleanup, correctness/performance verification on May 16, 2026 used the
integrated `pgbench --homer` path and the same installed binary's libpq path:

- Homer c1/j1, `-t 3000`: `5654`, `5679`, `5784 TPS`; p99 about
  `0.196-0.201 ms`
- libpq c1/j1, `-t 3000`: `4398`, `4209`, `4344 TPS`; p99 about
  `0.253-0.286 ms`
- Homer c4/j4, `-t 3000`: `14656`, `15921`, `15229 TPS`; p99 about
  `0.405-0.461 ms`
- libpq c4/j4, `-t 3000`: `10966`, `10852`, `10003 TPS`; p99 about
  `0.652-0.687 ms`

Basebackup was also rechecked after the tuple/result migration. Local blackhole
ran at `4.17s` then `3.96s`; remote RDMA ran at `8.25s` for the first cold
setup/warmup run and `4.21s` for the warmed run.

After the follow-up cleanup that made tuple/result queues byte-ring-only and
moved client-SQL result sink ownership into the Homer service, farnet1 local
validation on May 16, 2026 showed:

- local blackhole basebackup: `4.19s`
- pinned c1/j1 `pgbench --homer -t 10000 --latency-percentiles
  --client-cpu=8`: `5838 TPS`, p99 `0.188 ms`, zero failures
- pinned c4/j4 `pgbench --homer -t 20000 --latency-percentiles
  --client-cpus=8,9,10,11`: `14983` and `14851 TPS` repeats, p99 `0.470` and
  `0.465 ms`, zero failures

One important rebuild pitfall: the Postgres backend statically links the Homer
client/control protocol pieces. When `libhomer_client.a`,
`remote_execution_control_protocol.h`, or shared protocol versions change, force
a relink of at least `src/backend/postgres`, `src/bin/pgbench/pgbench`, and
`src/bin/pg_basebackup/pg_basebackup` before installing. A stale backend can map
an older control shared-memory name while the service uses the newer one; the
observed failure looked like a timeout waiting for Homer service progress. A
quick sanity check is:

```sh
strings /data/dbcomm/pg-citus/bin/postgres \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service |
  rg 'citus_remote_execution_control_v' | sort -u
```

The installed binaries should agree on one protocol/control-region version.

## Future Work

The next performance-relevant substrate step is not another fixed-slot
conversion for basebackup or tuple/result streams. Remaining work is
transport-side: traffic-class transports, multiple QPs, real RDMA egress
scheduling, possible one-RDMA-op in-band publication, naming cleanup, and
optional fragment/reassembly for very large semantic objects.

May 17, 2026 update: the first traffic-class transport baseline is now landed in
the Citus/dbcomm worktree. Payload peer-open now uses the same foreground/bulk
traffic-class lane that later carries payload writes, so responder-side
byte-ring MRs are registered under the correct lane-local QP/PD. During
validation we also fixed a basebackup-specific descriptor bug:
[`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8597)
now leaves `peerReceiveQueueDescriptor` zeroed for remote basebackup blackhole,
because the responder has no frontend-visible POSIX receive queue there and the
sender only needs the byte-ring RDMA descriptor. Warm remote RDMA basebackup
after the fix measured `4.37s`, `4.34s`, and `4.29s` after a cold `5.35s` first
run.

This checkpoint advances the future design in
[homer_transport_scheduler_and_payload_streams.md](../../../future-directions/citus/transport/homer_transport_scheduler_and_payload_streams.md).
