# Tuple-Sink End-to-End Onboarding

## Scope

This document explains the current landed tuple-sink tuple flow, from
coordinator-side session open through tuple-view batching, service-to-service
RDMA payload movement, receive-side semantic decode, and worker-side executor
insertion.

It does not cover speculative startup, cross-transaction backend reuse, non
tuple-sink `RemoteExecutionSession` modes, or a future canonical tuple
serialization format.

## Current status note

This is an onboarding map for the active implementation described in
[`cross_node_tuple_sink_peer_control_checkpoint.md`](kb/implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md).

The important current correction is:

- the first tuple-sink vertical slice is already landed
- speculative startup/overlap is optional follow-up latency work, not required for the data path to work
- wakeups are still future work to replace current sleep/poll loops
- service-side semantic decode is landed
- canonical portable tuple serialization is not landed

## Short mental model

`TupleDesc` metadata becomes a once-per-exact-sink
[`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:124).
Each PostgreSQL `TupleTableSlot` is encoded into compact tuple-view rows inside
one or more local send queue batches. The external service RDMA-writes those
batch bytes to the peer service. The peer service semantically parses and
rebuilds the incoming rows into the backend-visible receive sink. The worker
backend borrows rows from that receive sink into executor slot arrays and calls
PostgreSQL's insert path.

The tuple view is not a standalone heap tuple object. In the hot path, a tuple
view is a row layout inside the payload area of a
[`CitusTupleSinkBatchHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:274)
queue slot.

## Open and contract flow

The coordinator-side COPY path lazily opens a tuple-sink session in
[`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/commands/multi_copy.c:2881).
That eventually reaches
[`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2034).

During open, the backend builds the tuple-shape metadata in
[`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:583).
The resulting
[`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:124)
contains relation id, attribute count, null-bitmap width, and one
[`CitusTupleContractAttribute`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:98)
per attribute.

That contract is sent to the local service in
[`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:276).
The local service handles the request in
[`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5158),
where it validates the contract, finds or creates a compatibility session, and
finds or creates the exact tuple sink.

For a send-side open, the local service then peer-opens the remote receive side
through
[`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3831).
The peer-open request is
[`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75),
and it also carries the full tuple-view contract. The remote service handles it
in
[`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4044),
creating the peer receive sink and its receiver-owned RDMA payload ring through
[`TupleSinkServiceEnsureLocalReceivePayloadRing()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:682).

So the contract is sent at exact-sink open time, not once per large tuple batch.
Many queue-slot batches can flow under the same opened sink.

## Sender-side tuple encoding

The coordinator publishes tuples from
[`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/commands/multi_copy.c:3129).
For the normal worker-owned path it calls
[`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3109),
which hides reserve/append/flush policy under the session.

The reserve path is:

- [`AppendTupleViewToRemoteSendStreamInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2858) keeps one current send batch under the session.
- [`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2744) waits until a writable slot exists or the data plane/command becomes terminal.
- [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2917) delegates to the local tuple-sink queue substrate.
- [`TryReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1038) claims one local send queue slot.

The encode/append path is:

- [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1180) calls `slot_getallattrs()`, normalizes each attribute, writes the row null bitmap, then writes aligned fixed-width or length-prefixed attribute payload bytes.
- [`NormalizeTupleSinkAttribute()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:603) prepares one attribute's scratch value, including omitted/dropped/generated/null handling.
- [`TupleSinkTupleBytes()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:682) computes the compact row size for the current normalized tuple.
- [`TupleSinkAttributeUsesLengthPrefix()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:589) decides whether an attribute needs an inline length prefix.

Publication is still queue-based. [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1353)
publishes one completed queue slot by advancing the queue's `publishedTail`.
The producer has already written the slot payload in place.

At the end of the COPY stream,
[`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/commands/multi_copy.c:2815)
flushes any partial session-managed batch through
[`FlushRemoteSendStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3120)
before closing the data plane, so the worker sees every row before EOS.

## Service-to-service payload movement

PostgreSQL/Citus backends do not directly perform the RDMA writes for tuple
payloads. They write local shared-memory tuple queues. The external service owns
the service-to-service payload movement.

On the sender service, [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4497)
observes published local send queue slots. It validates the
[`CitusTupleSinkBatchHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:274),
writes a
[`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:300)
in front of the backend batch image, then posts the registered bytes into the
peer receive payload ring at
[tuple_sink_service_process.c:4669](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4669).

The service-to-service ring metadata is separate from the backend-visible tuple
queues:

- [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:159) tracks payload-ring producer/consumer positions.
- [`CitusTupleSinkPayloadRingDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:196) carries RDMA address, rkey, slot count, and slot byte geometry.
- [`TupleSinkServicePayloadRingState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:71) is the service-side runtime wrapper around the local payload ring.

The sender also consumes remote doorbells in
[`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4497)
to learn the receiver's consumed-head progress. That is current backpressure
plumbing; richer backend wakeups are still future work.

## Receive-side service decode

The remote service consumes the peer-written payload ring in
[`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4720).
It waits for a data doorbell, validates the transport header and batch header,
then rebuilds the local backend-visible receive queue slot.

The old blind-copy path is intentionally left as commented-out historical code
at [tuple_sink_service_process.c:4836](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4836).
The active path calls
[`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415),
which walks each incoming row through
[`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2217).

That decode step:

- reads the incoming row null bitmap
- walks attributes from the sink's
  [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:124)
- rejects impossible omitted/generated/dropped attribute states
- handles fixed-width attributes and length-prefixed attributes
- checks bounds and alignment
- writes a rebuilt row into the local receive queue slot

This is semantic decode/rebuild, not final portable serialization. Today the
wire row layout and receive-sink row layout intentionally remain the same, and
[`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2217)
still requires the rebuilt row width to match the consumed wire width. The
architectural value is that the external service now owns the parse/rebuild
boundary instead of making the worker backend understand incoming transport
bytes directly.

After decode succeeds,
[`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4720)
publishes the receive queue slot by advancing the local receive queue's
`publishedTail`.

## Worker-side receive and insert

The worker insert path is launched by
[`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/commands/multi_copy.c:2651)
and executed by the socketless backend command loop
[`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:646).

The worker's tuple drain happens in
[`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202):

- [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3132) polls the backend-visible receive queue.
- [`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3178) detects receive-side EOS when no more batches are available.
- [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3223) borrows one row from the current receive batch into caller-provided arrays.
- [`DecodeTupleViewFromTupleSinkRow()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service.c:1592) performs the backend-local row-to-`Datum[]` decode.
- [`ExecStoreVirtualTuple()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/executor/execTuples.c:1641) marks the executor slot valid.
- [`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/executor/execReplication.c:490) performs the actual insert.
- [`ReleaseBorrowedRemoteTupleView()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3254) ends the borrowed tuple lifetime after the insert call.
- [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3294) releases the whole receive batch after all rows in it are consumed.

The borrowed wrapper is
[`RemoteTupleView`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:200).
It does not own tuple storage. Callers provide the `Datum *values` and
`bool *isnull` arrays, typically the target tuple slot's arrays. By-reference
Datums may point directly into the currently borrowed receive batch, which is
why
[`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202)
keeps the borrow live across
[`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/executor/execReplication.c:490).

## Key objects to remember

- [`RemoteExecutionSession`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:179): backend-visible session abstraction.
- [`RemoteTupleView`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:200): borrowed row wrapper over caller-owned `Datum[]` / `isnull[]` arrays.
- [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:276): local backend-to-service open request.
- [`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75): service-to-service peer-open request.
- [`TupleSinkServiceSessionState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:123): service-owned compatibility session state.
- [`TupleSinkServiceSinkState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:201): service-owned exact sink state.
- [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:124): per-sink tuple-shape contract.
- [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:174): backend local queue attachment descriptor.
- [`CitusTupleSinkQueueControl`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:150): backend-visible SPSC queue control block.
- [`CitusTupleSinkBatchHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:274): per queue-slot batch header.
- [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:300): service-to-service payload transport prefix.
- [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:159): service-to-service payload ring control block.
- [`CitusTupleSinkPayloadRingDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:196): RDMA payload ring descriptor exchanged during peer open.

## Wakeup follow-up

Wakeups are not speculative startup. They are the future replacement for current
poll/sleep waiting around queue progress.

Today the sender waits for capacity in
[`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2744)
using `CHECK_FOR_INTERRUPTS()` plus `pg_usleep(1000L)`. The worker receive loop
does the same when
[`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3132)
returns no batch and
[`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:3178)
is still false.

The likely future shape is service-owned event publication plus backend-owned
wait consumption:

- service signals when send capacity becomes available
- service signals when receive data is published
- service signals peer close / EOS
- service signals terminal transport or command failure
- backend/session wait helpers consume those signals instead of pure polling

That makes wakeups a latency and CPU-efficiency improvement around the existing
data path. It is not required to keep the tuple view encode/RDMA/decode/insert
flow logically correct.

## Current caveats

- The current decode boundary is real, but the current wire row format is still
  intentionally same-layout with the receive sink row format. Canonical portable
  serialization remains future work.
- Receive-side transport failure still lacks a distinct backend-visible
  terminal condition beyond peer-close/EOS in the current worker loop.
- Cross-transaction backend/session reuse is not implemented; the active worker
  path is transaction-scoped.
- Non tuple-sink `RemoteExecutionSession` modes are still future work.

## Related

- [`cross_node_tuple_sink_peer_control_checkpoint.md`](kb/implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md): canonical current implementation checkpoint for the landed tuple-sink vertical slice.
- [`tuple_sink_data_plane_completion_plan.md`](kb/future-directions/citus/data-movement/tuple_sink_data_plane_completion_plan.md): remaining future work after the landed vertical slice.
- [`local_batch_materialization_checkpoint.md`](kb/implementations/citus/tuple-route/local_batch_materialization_checkpoint.md): older tuple-route substrate checkpoint that explains the historical local queue/batch layer.
- [`remote_execution_session_wrapper_checkpoint.md`](kb/implementations/citus/connection-management/remote_execution_session_wrapper_checkpoint.md): earlier backend-facing session wrapper checkpoint.
