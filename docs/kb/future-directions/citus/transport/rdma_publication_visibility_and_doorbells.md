# RDMA publication visibility and doorbells

## Scope

- **What this doc explains**: the RDMA publication/visibility rules that the tuple-sink control and data channels should rely on, and the currently chosen responder-visible publication mechanism.
- **What this doc does NOT cover**: provider-specific tuning, persistent flush semantics, or durable receiver-side completion semantics above the RDMA transport layer.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The earlier tuple-sink prototype treated a changed memory-resident tail/head word as the publication boundary for both:

- the cross-node control mailbox in [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2115) and [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2066)
- the tuple payload ring in [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2230) and [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2440)

That was too weak as the long-term responder-visible publication rule.

The important correction is:

- coherent DMA does not mean large RDMA writes become visible atomically
- local CQEs do not by themselves create a safe responder-visible publish point
- with relaxed ordering enabled, separate incoming RDMA writes may become visible in an arbitrary order
- even without relaxed ordering, ordinary memory polling of a separately written publish word is not the portable responder-visible completion rule we want to rely on

So the design should not say:

- "payload/header/tail were posted in that order, therefore the responder may safely poll memory for tail"

It should instead say:

- "payload/header are written with ordinary RDMA writes, and a final responder-visible publish event tells the receiver it is now safe to read the already-written memory state"

## Chosen rule

For the tuple-sink prototype, the chosen responder-visible publish mechanism is:

1. ordinary RDMA writes for message payload, slot header, tuple payload bytes, and the 64-bit memory-resident head/tail words
2. a final `RDMA_WRITE_WITH_IMM` as the responder-visible publish/doorbell step
3. remote CQ polling for that doorbell instead of trusting memory polling alone as the correctness boundary
4. acquire-based local loads of the memory-resident 64-bit state only after the doorbell has arrived

Important clarifications:

- the 64-bit head/tail still live in memory
- the 32-bit `imm_data` is **not** the head/tail value itself
- `imm_data` is only the responder-visible doorbell token
- the current tuple-sink prototype uses `imm_data == 0` for control-doorbell events and the remote local `serviceSinkId` for data-doorbell events

## What “coherent DMA” does and does not mean

On the current x86-based prototype machines, coherent DMA means:

- CPU caches and device DMA writes stay mutually visible without explicit software cache flush/invalidate steps

It does **not** mean:

- an arbitrary RDMA write becomes visible atomically as one indivisible update
- a responder may safely read payload bytes while the NIC is still writing them
- a changed memory-resident publish word is automatically a sufficient responder-visible completion event

So the safe protocol is still publication-by-doorbell, not publication-by-hopeful-memory-polling.

## Why `WRITE_WITH_IMM` is enough here

For the current tuple-sink prototype we do **not** need RDMA atomics just to publish head/tail, because:

- each publish word still has only one remote writer
- the payload and control channels are both SPSC in the chosen first design

So the missing requirement is not multi-writer atomic arbitration. It is a responder-visible publish event. `RDMA_WRITE_WITH_IMM` gives us that event without changing the in-memory 64-bit head/tail representation.

Important distinction:

- `RDMA_WRITE_WITH_IMM` is a responder-visible notification/publication primitive
- it is **not** the same thing as the verbs flush opcode

If a later prototype explicitly enables relaxed-ordering MRs or needs stronger remote flush/persistence semantics, revisit that separately.

## Current implementation mapping

### Control channel

The current control channel still keeps one memory-resident mailbox slot and one 64-bit published tail:

- mailbox publication in [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2115)
- mailbox consumption in [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2066)

But the responder-visible publish rule is now:

- post the control-message slot unsignaled first in [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2115)
- post the 64-bit mailbox tail second with `RDMA_WRITE_WITH_IMM` in the same helper
- wait locally only for that final signaled publish WR through [`TupleSinkServiceWaitForCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:489)
- consume the mailbox only after the receive-side doorbell has been drained through [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1919)

So the mailbox still exists, but polling memory alone is no longer the correctness boundary.

### Data channel

The data ring keeps:

- a receiver-owned payload ring in [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:123)
- a sender-owned mirrored remote-head word in [`CitusTupleSinkPayloadHeadMirrorDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:181)

Sender-side payload publication in [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2230) is now:

1. post [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:289) unsignaled through [`TupleSinkServicePostPeerInlineBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2876)
2. post the exact local send-slot bytes unsignaled through [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2966)
3. post the receiver-owned payload-ring tail with a final signaled [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3046)

Receiver-side consumption in [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2440) is now gated by [`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3103), not by blind polling alone.

Reverse head publication back to the sender is also doorbelled:

- the receiver writes the new consumed head into sender-owned memory with a signaled [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3046)
- the sender consumes that doorbell through [`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3103) and then reads the mirrored head locally in [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2230)

## Current prototype conventions

### Immediate-value convention

The current first convention is intentionally simple:

- `0` means a control-doorbell event
- nonzero means a data-doorbell event whose value is the local `serviceSinkId` on the receiving side

Why this works for now:

- the service still supports only up to 64 active local sinks at once
- `serviceSinkId` is monotonically allocated and easily fits into 32 bits in the current prototype
- the local sink role already tells the receiver whether the doorbell means "new payload tail is ready" or "mirrored remote head is ready"

### Head/tail terminology

Use head/tail terminology for the mirrored remote head.

In the current tuple-sink prototype it is just:

- a memory-resident head word
- written by the consumer
- read by the producer after a responder-visible doorbell

The current code now reflects that in:

- [`TupleSinkServiceEnsureLocalSenderHeadMirror()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:580)
- [`TupleSinkServiceRecordPeerSenderHeadMirror()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1455)
- [`CitusTupleSinkPayloadHeadMirrorDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:181)

## Payload Publish Pipelining Plan

The basebackup prototype exposed a transport-level performance issue in the
shared payload path. With 1 MiB `BASE_BACKUP` payload slots, the two-service
RDMA blackhole run completed correctly but produced about 25k semantic objects
and took about 31s. Larger slots improved throughput mostly by reducing object
count, which points at the current per-object local completion policy rather
than raw RDMA bandwidth.

The important correction is that the final signaled
[`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3273)
wait is not a remote application ACK. It is a local send-completion checkpoint
for the signaled `RDMA_WRITE_WITH_IMM`. It is still expensive when paid once per
payload object. The current sender path in
[`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5153)
also uses the local send queue `consumedHead` as both "next object to send" and
"last object whose source slot may be reused." That is only correct because the
service waits synchronously before advancing `consumedHead`.

The full lazy-completion implementation should split those concepts:

- `publishedTail`: backend has filled local queue slots through this sequence
- `payloadSenderPostedTail`: service has posted RDMA payload/tail writes through this sequence
- `sendQueue.consumedHead`: local NIC completion frontier; backend may reuse slots through this sequence
- `senderVisibleRemoteConsumedHead`: remote receiver has released receive-ring slots through this sequence

The sender should then post multiple payload objects before waiting:

1. consume remote head doorbells to learn receive-ring capacity
2. post the payload slot RDMA write, usually unsignaled
3. post the tail `RDMA_WRITE_WITH_IMM`, usually unsignaled
4. request a signaled completion only every small batch or when the local
   publish-word ring/inflight window is full
5. poll send CQ completions opportunistically and advance
   `sendQueue.consumedHead` only to the latest completed signaled frontier
6. on close/error/drain, force a final signaled checkpoint before declaring the
   local send queue reusable or terminal

This keeps the responder-visible publication rule unchanged: the receiver still
waits for the `WRITE_WITH_IMM` doorbell before reading the payload ring. The
optimization is only on the sender's local completion policy.

One hidden lifetime issue must be handled explicitly. The current connection
state has a single 64-bit
[`outgoingPublishedTailBuffer`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:165)
registered by `outgoingTailMr`; it is safe only because the helper waits before
the field is reused. Pipelining needs a small registered ring of publish-word
buffers or an equivalent per-WR source lifetime rule. Otherwise the service
could overwrite the 64-bit source word before the NIC reads it.

This is a shared substrate improvement. Basebackup and tuple-copy payloads both
converge in
[`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5153),
so the implementation should not create a basebackup-only fast path.

### Implementation checkpoint

The first pipelined checkpoint implementation now batches payload publication in
the shared sender loop rather than adding basebackup-specific logic:

- [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5153)
  computes a bounded batch from backend-published slots, remote receive-ring
  capacity, and `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`.
- Each object still posts one registered payload write and one
  responder-visible tail `WRITE_WITH_IMM`, but only the last tail update in the
  batch asks for a local send CQE through
  [`TupleSinkServicePostPeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3182).
- The sender waits once per batch with
  [`TupleSinkServiceWaitForPeerSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3249)
  and advances the backend-visible `sendQueue.consumedHead` only after that
  completion.
- The connection owns a registered tail-source ring
  `payloadPublishTailBuffers`, so multiple outstanding tail WRs do not reuse the
  single synchronous `outgoingPublishedTailBuffer`.
- This first checkpoint does not persist a separate `payloadSenderPostedTail`
  across service-loop iterations. It posts a bounded batch, waits for the batch
  checkpoint, and then advances `sendQueue.consumedHead`. That is less aggressive
  than the full frontier split, but it removes most of the measured per-object
  CQ wait while keeping source-slot lifetime simple.

Measured on the basebackup two-service RDMA blackhole workload:

- 64 x 1 MiB slots improved from about `31.05s` to `7.20s` for about
  `23.2 GB` of payload, or about `3222 MB/s`.
- Default 8 x 8 MiB slots improved from `9.28s` before batching to `6.50s`,
  or about `3569 MB/s`.
- The same restarted-server libpq tar-to-stdout reference was `10.16s`, or
  about `2283 MB/s`.

This validates the diagnosis: the 1 MiB slowdown was caused by one local
signaled send-completion wait per semantic payload object, not by 1 MiB being an
inherently bad RDMA payload size.

### Fuller optimization plan: persistent sender frontier split

The next optimization should turn the current bounded synchronous batch into a
real sender pipeline. The goal is not to change the responder-visible
publication semantics. The receiver should still only read a payload slot after
its data `WRITE_WITH_IMM` doorbell arrives. The change is only on the sender:
posting work and learning local source-buffer reuse should become separate
activities.

The current implementation still has one important serialization point:
[`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5153)
posts at most one bounded batch, waits in
[`TupleSinkServiceWaitForPeerSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3249),
then advances `sendQueue.queueControl->consumedHead`. That is much better than
waiting per object, but the service still stops producing new WRs while it waits
for the batch checkpoint.

The fuller design should add explicit sender-side frontiers to
`TupleSinkServiceSinkState` in
[`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:422):

- `payloadSenderPostedTail`: highest semantic object sequence for which the
  service has posted the payload write and tail doorbell.
- `payloadSenderCompletedHead`: highest semantic object sequence whose local
  send completion has been observed. This is the value that may be copied into
  the backend-visible `sendQueue.queueControl->consumedHead`.
- `payloadSenderSignaledTail`: highest semantic object sequence for which the
  service has requested a signaled publish checkpoint.
- `payloadSenderCompletionPending`: whether a signaled checkpoint is currently
  outstanding.
- optional debug counters for posted objects, completed objects, CQ polls, and
  times the sender stopped on local slots, remote slots, tail-source slots, or
  QP send-WR capacity.

The key accounting change is that remote receive-ring capacity must be computed
from the posted frontier, not the completed frontier:

```text
remote_inflight = payloadSenderPostedTail - senderVisibleRemoteConsumedHead
remote_capacity = peerReceivePayloadRing.slotCount - remote_inflight
```

The current checkpoint implementation can use `sendQueue.consumedHead` for this
because it waits immediately after each batch. Once posting and completion are
decoupled, using `consumedHead` would undercount in-flight receiver slots and
could overwrite the remote payload ring.

The local source-slot lifetime rule is the opposite side of the same split:

```text
local_outstanding = payloadSenderPostedTail - payloadSenderCompletedHead
local_source_capacity = sendQueue.queueControl->slotCount - local_outstanding
```

The backend must not be allowed to reuse a shared-memory send slot until local
NIC completion has covered that slot's payload write. Therefore
`sendQueue.queueControl->consumedHead` should continue to mean "safe for backend
reuse", and it should advance only from send-CQ completions, never merely from
successful `ibv_post_send()`.

#### Proposed sender loop

The optimized sender pump should have three phases each time the service loop
visits a bound sending sink:

1. Drain data-doorbell completions through
   [`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3103)
   and refresh `senderVisibleRemoteConsumedHead` from
   `localSenderHeadMirror.counters`.
2. Poll, but do not block, the send CQ for payload publish checkpoints. Each
   successful checkpoint advances `payloadSenderCompletedHead` and then stores
   that value into `sendQueue.queueControl->consumedHead`.
3. Post more semantic payload objects while all windows have room:
   backend-published objects, local source slots, remote receive slots,
   registered tail-source slots, and QP send WRs.

Posting one object still means:

1. write the in-band
   [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:289)
   into the local send slot
2. post the contiguous registered payload slot via
   [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2966)
3. post the receiver-visible tail doorbell via
   [`TupleSinkServicePostPeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3182)

The difference is that most tail doorbells remain unsignaled. A signaled tail
doorbell is requested when any of these is true:

- `payloadSenderPostedTail - payloadSenderCompletedHead` reaches the configured
  source-lifetime window
- the registered `payloadPublishTailBuffers` ring is close to wrapping
- QP send-WR capacity is close to full
- the backend has no more currently published objects, so a checkpoint would
  release slots before the next burst
- close/drain/error handling needs a terminal local-completion frontier

#### Completion identification

The current low-level post helper sets `ibv_send_wr.wr_id` to the source buffer
address in
[`TupleSinkServicePostWriteBufferInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1133).
That is enough for a synchronous "wait for any expected opcode" helper, but it
is not enough for a durable asynchronous payload pipeline. The full pipeline
should add a payload-specific post helper that accepts an explicit `wr_id`, with
the signaled tail WR carrying the semantic frontier sequence it completes.

The corresponding CQ helper should be nonblocking:

```text
TupleSinkServicePollPeerSendCompletionRdma(connection, expected_kind, &frontier)
```

For the first implementation, keeping at most one signaled payload checkpoint
outstanding per sink is acceptable and simpler. In that case the service can
store `payloadSenderSignaledTail` in the sink state and interpret the next
payload send CQE as covering that frontier. However, tagging the WR with the
frontier is still the cleaner design because the send CQ is shared by control
and payload writes on the same peer connection.

#### Window sizing and QP capacity

The current connection setup in
[`TupleSinkServiceInitConnectionResources()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:710)
creates a send CQ of `32` entries and an RC QP with `max_send_wr = 32`. Today
each semantic payload object posts two send WRs. Therefore a real pipeline needs
one of these two policies:

- keep `max_send_wr = 32` and enforce a conservative payload window of at most
  12-14 objects, leaving WR headroom for control/close writes and avoiding a
  full QP
- increase QP/CQ depth and size the tail-source ring, signaled-checkpoint
  interval, and sender windows from that negotiated capacity

For research runs, the conservative first step is better: keep the QP depth
unchanged, add explicit `payloadSenderOutstandingWr` accounting, and stop
posting before the QP is close to full. If the counters show the QP window is
the new limiter, then increase `max_send_wr` and the send CQ together as a
separate measured change.

#### Tail-source ring lifetime

The registered `payloadPublishTailBuffers` ring in
[`TupleSinkServicePeerConnectionHandle`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:157)
exists because an unsignaled tail WR cannot safely borrow stack memory or a
single reusable 64-bit field. The full pipeline should treat this ring as a
real resource:

```text
tail_source_inflight = payloadSenderPostedTail - payloadSenderCompletedHead
tail_source_capacity = CITUS_REMOTE_EXEC_PEER_PAYLOAD_PUBLISH_TAIL_SLOTS -
                       tail_source_inflight
```

The sender must not post a tail update whose `value %
CITUS_REMOTE_EXEC_PEER_PAYLOAD_PUBLISH_TAIL_SLOTS` would reuse a source word
that is not covered by an observed local completion. This is a correctness rule,
not just an optimization.

#### Close and drain behavior

The existing close/drain checks still look at
[`TupleSinkServiceQueueIsDrained()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5936),
which ultimately depends on the backend-visible queue head/tail. With a
persistent posted/completed split, "nothing left to post" and "all source slots
safe to reuse" are no longer the same fact. Close should therefore use this
order:

1. stop accepting new backend publications for the sink if the close path needs
   a finite drain
2. post all objects through `publishedTail`, respecting remote capacity
3. force a signaled checkpoint if `payloadSenderPostedTail >
   payloadSenderCompletedHead`
4. wait or poll until `payloadSenderCompletedHead == payloadSenderPostedTail`
5. only then allow the existing control close to treat the local send queue as
   drained

This preserves the current no-copy property: the backend's queue slots are the
actual RDMA source buffers, and they are released only after the NIC is done
reading them.

#### Candidate approaches

There are three reasonable implementation levels:

- **Keep bounded synchronous batches.** This is what is implemented now. It is
  simple and already fixed the measured 1 MiB basebackup cliff, but the sender
  still blocks once per batch and cannot overlap CQ waiting with additional
  posting.
- **Single asynchronous checkpoint.** Add `payloadSenderPostedTail` and one
  pending signaled frontier. Post until a window is full, poll CQ
  opportunistically, and advance `consumedHead` when the single checkpoint
  completes. This should remove the remaining sender-side serialization with
  modest state and is the recommended next coding step.
- **Multiple asynchronous checkpoints.** Add an explicit signaled-frontier ring
  and use WR IDs to complete exact frontiers. This gives the cleanest long-term
  transport substrate if several sinks share a peer connection heavily, but it
  is more engineering than needed for the next basebackup experiment.

The recommended next step was the single asynchronous checkpoint design with
explicit WR IDs. The implementation ended up slightly more aggressive: tagged
WR IDs made multiple in-flight signaled checkpoints cheap enough, and the 32-WR
QP limit immediately showed up as too small for the 64-slot basebackup geometry.

### Implementation checkpoint: asynchronous posted/completed split

The current payload sender now implements the posted/completed split in the
shared payload path:

- [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5199)
  keeps `payloadSenderPostedTail`, `payloadSenderCompletedHead`, and
  `payloadSenderLastSignaledTail` as sender-side frontiers.
- `sendQueue.queueControl->consumedHead` remains the backend-visible source-slot
  reuse frontier and is advanced only when a tagged send CQE is observed.
- Remote receive-ring capacity is charged against `payloadSenderPostedTail -
  senderVisibleRemoteConsumedHead`, so posted but locally incomplete WRs still
  occupy remote slots.
- [`TupleSinkServicePostPeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3518)
  now tags signaled payload tail WRs with the local `serviceSinkId` and semantic
  frontier sequence.
- [`TupleSinkServicePollPeerPayloadSendCompletionRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3603)
  drains tagged payload completions without confusing them with synchronous
  control/bootstrap send completions.
- [`TupleSinkServiceWaitForSendCompletion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:760)
  preserves tagged payload completions that arrive while a synchronous control
  write is waiting on the shared send CQ.

The first conservative async attempt kept the old `32` send-WR QP and a
14-object payload window. That was correct but slower for 1 MiB basebackup
objects, because the service repeatedly left the payload post loop to poll the
same send CQ through the full service loop. The implementation therefore also
changed the transport geometry:

- [`CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:59)
  is now `256`, and
  [`TupleSinkServiceInitConnectionResources()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1011)
  uses that value for the send CQ and QP `max_send_wr`.
- `payloadPublishTailBuffers` now has `128` registered tail-source slots, so the
  sender can keep a 64-slot basebackup geometry in flight without reusing tail
  source words.
- [`HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:120)
  is `63`, intentionally one below the tail-source ring wrap boundary used by
  the current basebackup experiment.
- When the local source/QP window is full,
  [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5405)
  waits directly for one tagged payload send completion instead of returning
  through the whole service loop. Remote-ring-full still returns to the service
  loop so incoming receiver-head doorbells can be consumed.

Two adjacent hot-path cleanups were required to make the async version useful:

- [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3429)
  and the payload tail post helper no longer call
  `TupleSinkServicePrepareConnectionForWrite()` per payload WR. The sender pump
  checks readiness and drains events outside the per-object burst loop.
- [`TupleSinkServicePumpIncomingPeerMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2889)
  no longer clears a 34 KiB stack control message before knowing that a message
  is present.

The RDMA CM event path also had to be made less intrusive. A zero-timeout
`poll()` guard in
[`TupleSinkServicePollForCmEvent()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:490)
prevents `rdma_get_cm_event()` from blocking the single-threaded service, but
listener and persistent-connection CM checks are now periodic once a peer
connection exists because CM disconnect events are not the payload data path.

Measured after rebuilding, installing, and restarting both service processes:

- 64 x 1 MiB slots: `6.60s`, `objects=25788`,
  `payload_bytes=23196131136`, about `3515 MB/s`.
- Default 8 x 8 MiB slots: `6.01s`, `objects=6466`,
  `payload_bytes=23196131136`, about `3860 MB/s`.

Compared with the previous synchronous-batch checkpoint, this improves the 1 MiB
run from `7.20s` to `6.60s` and the default run from `6.50s` to `6.01s`.

### Next optimization plan: batch publication at the RDMA substrate

The remaining 1 MiB-vs-8 MiB gap should not be treated as evidence that RDMA
bandwidth is sensitive to MiB-scale payload sizes. The current implementation
still publishes at semantic-object granularity:

- one payload RDMA write through
  [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3429)
- one tail `WRITE_WITH_IMM` through
  [`TupleSinkServicePostPeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3518)
- one receiver-side immediate event consumed by
  [`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3803)

That is a general transport cost, not a basebackup-specific cost. Any sink
family that publishes many small typed objects would pay the same object-rate
verbs and doorbell overhead.

The next substrate-level optimization should introduce **batch publication**:

```text
semantic objects N..M are copied or already resident in registered source slots
ordinary RDMA writes publish each slot body
one final tail WRITE_WITH_IMM publishes remote tail=M
receiver drains all slots up to the visible tail
one tagged send CQE releases sender source slots through M
```

This preserves the existing correctness rule: the receiver still waits for a
responder-visible `WRITE_WITH_IMM` before reading peer-written payload slots.
The difference is that the doorbell publishes a range of typed objects rather
than exactly one object.

#### Proposed substrate API shape

Add a lower-level batch API below the sink-specific sender loop, roughly:

```text
TupleSinkServicePostPeerPayloadBatchRdma(
    connection,
    serviceSinkId,
    localRegisteredSlots[],
    remotePayloadRing,
    firstSequence,
    objectCount,
    finalTailSequence,
    signaledCompletionFrontier)
```

The API should be semantic-object agnostic. It should not know whether the slot
contains tuple-view batches, basebackup archive chunks, or a future object
family. It should know only:

- the local registered source addresses and byte lengths
- the remote payload-ring slot addresses
- the remote tail address/rkey
- the sink id used for the responder-visible data doorbell
- the semantic sequence range covered by the final tail

The existing sender,
[`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5199),
would still own object-specific validation and transport-header preparation,
but once it has a contiguous publishable range, it should hand that range to the
batch RDMA helper instead of posting one object at a time.

#### Doorbell coalescing rule

The batch helper should post ordinary RDMA writes for each payload slot in the
range, then one tail `WRITE_WITH_IMM` for `finalTailSequence`.

Receiver behavior already mostly matches this model:

- data doorbells are queued by sink id, not by object sequence
- [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5726)
  reads the receiver-owned payload-ring tail after consuming a data doorbell
- it then consumes slots until its local receive head reaches that tail

So the receiver should not need a new semantic abstraction for batch
publication. It only needs to tolerate one doorbell advancing the tail by more
than one object, which is already the natural ring-consumer model.

#### Work-request chaining

After range publication is correct, the next mechanical optimization is to
post the batch as a linked WR chain:

```text
payload WR for sequence N
payload WR for sequence N+1
...
payload WR for sequence M
tail WRITE_WITH_IMM for sequence M, optionally signaled
```

The current helper stack eventually calls one `ibv_post_send()` per WR through
[`TupleSinkServicePostWriteBufferInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1172).
For small-object streams, syscall-free does not mean free: each post still
builds a WQE, rings/updates the QP doorbell path, and executes provider code.
A chained batch should reduce provider-call overhead from roughly two calls per
semantic object to one call per batch.

Keep this as the second step, not the first. It is easier to validate
doorbell-range semantics with the existing one-WR-at-a-time post helpers, then
replace the internal post helper with a chained implementation while preserving
the same batch API contract.

#### Sender-window and completion policy

Batch publication should keep the existing posted/completed split:

- `payloadSenderPostedTail` advances to the batch tail after the batch WRs are
  posted successfully
- `payloadSenderCompletedHead` advances only when the tagged signaled tail WR
  completes locally
- backend-visible `sendQueue.queueControl->consumedHead` follows
  `payloadSenderCompletedHead`
- remote capacity is charged against `payloadSenderPostedTail -
  senderVisibleRemoteConsumedHead`

The batch size should be bounded by all of:

- backend-published contiguous objects
- local source slots available before reuse
- remote receive-ring slots available before overwrite
- QP send WR capacity
- tail-source ring capacity
- a configured maximum batch object count

For the current geometry, a reasonable first experiment is:

- keep `HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS` at `63`
- use a publication batch of `8` or `16` objects
- publish one remote tail doorbell per batch
- request one signaled send completion per batch tail

This should reduce 1 MiB object-rate overhead without changing basebackup object
size or tuple-copy object semantics.

#### Correctness constraints

Batch publication must preserve these invariants:

- Remote tail is written only after every payload slot in the published range
  has been posted before it on the same RC QP.
- Receiver-side slot consumption remains gated by a data doorbell and an
  acquire read of the receiver-owned tail.
- Sender source slots are not released to the backend until the signaled batch
  completion covering those slots is observed.
- The sender must not reuse a tail-source buffer until local completion covers
  the signaled tail WR that used it.
- Close/drain must force a final batch tail doorbell if there are posted or
  locally prepared objects not yet published to the remote tail.

These are the same invariants as the current async checkpoint implementation;
the only change is that the unit of remote publication becomes a sequence range.

#### Measurement plan

Measure this as a substrate optimization, not only as a basebackup result:

1. Add counters in the transport or sink state for:
   `objects_posted`, `payload_wrs_posted`, `tail_doorbells_posted`,
   `signaled_checkpoints`, `send_cq_completions`, and
   `receiver_data_doorbells_consumed`.
2. Run the existing two-service basebackup blackhole workload with 1 MiB and
   8 MiB geometry.
3. Run a tuple-copy path benchmark with many small tuple batches if available,
   because that exercises the same RDMA substrate with a different typed object.
4. Compare throughput and object-rate counters before and after:
   the 1 MiB basebackup case should show fewer tail doorbells and fewer
   provider post calls per byte, while payload bytes and object counts remain
   unchanged.

Expected result: the 1 MiB-vs-8 MiB gap should shrink because the 1 MiB path
has four times as many semantic objects and benefits most from reducing
per-object doorbell/post overhead. If the gap does not shrink, the next suspects
are backend producer cadence, receiver validation work, or cache effects in the
shared-memory queue rather than RDMA publication itself.

### Implementation checkpoint: range publication and WR-chain post

The batch publication plan has now been implemented in the shared payload
substrate, without adding a basebackup-specific RDMA data plane:

- [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5289)
  still owns semantic-object validation and transport-header preparation. For
  basebackup objects, it validates `CitusRemoteBaseBackupMessageHeader` before
  publication at
  [`tuple_sink_service_process.c:5528`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5528).
- The sender computes a contiguous `batchLimit` bounded by backend-published
  objects, local source-slot availability, remote receive-ring availability,
  the local in-flight object window, and
  `HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH` at
  [`tuple_sink_service_process.c:5485`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5485).
- Each typed object becomes a generic
  [`TupleSinkServicePeerPayloadWriteDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:51),
  so the RDMA helper sees only registered source bytes, remote slot addresses,
  and the final semantic frontier. It does not know whether the object is a
  tuple batch or a basebackup archive chunk.
- [`TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3479)
  builds one linked verbs chain: ordinary payload RDMA writes first, then one
  signaled `IBV_WR_RDMA_WRITE_WITH_IMM` tail publication carrying the completed
  semantic frontier.
- [`HOMER_SERVICE_PAYLOAD_SEND_COMPLETION_BATCH`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:112)
  is currently `16`, and a compile-time guard ensures it fits the fixed
  descriptor array sized by
  [`CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:48).
- The receiver needed no new typed progress or batch object. It already drains
  the receiver-owned payload ring until local consumed head reaches the visible
  published tail in
  [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5762),
  and it validates/counts basebackup objects at
  [`tuple_sink_service_process.c:5883`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5883).

The abstraction result is important: we did **not** add a second descriptor
family for basebackup. We generalized the existing RDMA substrate by using one
generic payload-write descriptor below the semantic-object layer. The sink still
contains typed DB semantic objects; the transport helper only publishes already
prepared registered bytes and a typed sequence frontier.

Measured after rebuilding, installing, and restarting both service processes on
May 12, 2026:

- First 64 x 1 MiB run after restart: `8.92s`, `objects=25788`,
  `payload_bytes=23196142912`, about `2601 MB/s`. Treat this as a cold
  end-to-end outlier, not as transport steady state.
- Warmed 64 x 1 MiB repeats: `5.08s`, `5.08s`, `5.06s`, and `4.25s`, with
  `payload_bytes` around `23196144xxx`. That is roughly `4566-5458 MB/s`.
- Default 8 x 8 MiB repeats: `5.84s`, `5.87s`, `4.81s`, `4.82s`, and `4.72s`,
  with `objects=6466` and `payload_bytes` around `23196145xxx`. That is roughly
  `3952-4914 MB/s`.

Compared with the previous async posted/completed checkpoint (`6.60s` for
1 MiB and `6.01s` for default), the warmed 1 MiB path improved materially
(`5.06-5.08s`, with one `4.25s` run), while default-size objects are at least on
par and sometimes faster once the run is warm. The wall-clock command still
includes PostgreSQL checkpoint and cache effects, so the best interpretation is
that range publication removed a real object-rate bottleneck but the remaining
variance is no longer explained by RDMA payload size alone.

### Current milestone status

As of May 12, 2026, the RDMA payload-substrate optimization milestone is reached
for the basebackup blackhole workload. The current code preserves the intended
architecture:

- `RemoteExecutionSession` remains the high-level session/control abstraction.
- Sinks still carry typed DB semantic objects, including tuple-view batches and
  `BaseBackupArchiveStream` objects.
- The RDMA layer is a common substrate below those typed objects. It now accepts
  generic registered payload descriptors and sequence-frontier publication, not
  basebackup-specific data-plane objects.
- Basebackup progress remains local/inferred, with no separate performance-path
  progress object.

The practical performance lesson is also now part of the design: for large
semantic objects, raw RDMA payload size was not the limiting factor; object-rate
publication, completion visibility, and end-to-end PostgreSQL producer effects
were. The substrate changes remove the obvious per-object serialization without
changing basebackup semantics.

## Still deferred

- The control mailbox is still one-message-at-a-time and keeps its synchronous signaled publish policy. That path is not the basebackup hot path, and keeping it simple helps debugging. A richer multi-slot control ring is still future work, but it should preserve the same responder-visible publication rule.
- Internal transport-only counters/timestamps are still useful if future work
  needs to separate RDMA pump throughput from end-to-end `pg_basebackup`
  checkpoint, filesystem, tar, and manifest costs.
- If later prototypes enable relaxed-ordering MRs or require explicit remote flush semantics, revisit the publication rule explicitly rather than assuming `WRITE_WITH_IMM` alone covers every possible future placement/persistence requirement.

### Observed RDMA response-timeout failure mode

During pgbench transaction smoke testing, a direct single-client transaction
failed once after the worker-side service had already executed and consumed the
`TX_BEGIN_ATTACH` completion:

- farnet0 logged `consumed backend command completion session=1 command=tx_begin_attach sequence=1 state=completed`
- farnet1 then reported `operation=StartRemoteExecutionCommand detail=timed out waiting for RDMA CM event ESTABLISHED`
- the farnet1 service also logged a best-effort disconnect timeout with
  `reason=response-timeout`

The most useful interpretation is not “the SQL command failed.” The worker-side
command completed. The failure was in returning the peer control response to the
coordinator-side service.

The current code path is:

- worker service handles the peer request in [`TupleSinkServiceProcessIncomingMailboxRequest()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2459)
- it builds the response and publishes it through [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2166)
- coordinator service waits for a response in [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3210)
- response consumption normally requires [`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1938) to observe a `WRITE_WITH_IMM` receive completion before [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2116) reads the mailbox

For the current one-message-at-a-time control path, the implementation now has a
prototype fallback in [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2116): if the mailbox tail is visibly newer than the consumed head even though no
control doorbell has been drained, consume the fixed-width mailbox message by
tail observation. This relies on same-QP RDMA write ordering: the slot write is
posted before the tail write, so a visible newer tail implies the slot contents
are ready for this prototype.

This fallback is not the final concurrency design. It is a narrowly scoped
single-message control-path robustness measure. The later multi-client/session
work should revisit whether the command control plane needs a multi-slot ring,
separate command QPs, explicit service scheduling, or stronger CQ handling
rather than extending this fallback into a general multiplexing mechanism.

## Related

- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader transport-layer design space beneath the session/sink abstraction.
- [tuple_sink_data_plane_completion_plan.md](../data-movement/tuple_sink_data_plane_completion_plan.md): staged data-plane plan that now assumes doorbelled publication rather than plain memory polling.
- [cross_node_service_to_service_control_path.md](../connection-management/cross_node_service_to_service_control_path.md): higher-level cross-node control-path design that this RDMA publication rule now sharpens.
- [cross_node_tuple_sink_peer_control_checkpoint.md](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md): current implementation checkpoint that now uses the doorbelled control/data publication rule.
