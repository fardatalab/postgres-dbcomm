# RDMA publication visibility and doorbells

## Scope

- **What this doc explains**: the RDMA publication/visibility rules that the tuple-sink control and data channels should rely on, and the currently chosen responder-visible publication mechanism.
- **What this doc does NOT cover**: provider-specific tuning, persistent flush semantics, or later batching refinements beyond the current one-signaled-publish checkpoint policy.
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

## Still deferred

- The current requester-side write policy is now the first useful mostly-unsignaled version: mailbox slot writes and payload header/payload writes are unsignaled, while the final publish WR stays signaled and locally waited. More aggressive periodic signaled checkpoints across multiple publish events are still future performance work.
- The current control mailbox is still one-message-at-a-time. A richer multi-slot control ring is still future work, but it should preserve the same responder-visible publication rule.
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
