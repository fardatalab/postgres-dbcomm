# Payload Doorbell Token v15 Checkpoint

<!-- kb-summary: Landed checkpoint for payload doorbell tokens and fixed service-owned payload-ready queues. -->

## Scope

- **What this doc explains**: the landed Slice 2E-A and Slice 2E-B checkpoints
  for Homer payload doorbells: peer protocol v15, receiver-issued
  generation-bearing payload tokens, peer-open token exchange, sender use of the
  peer token in payload `WRITE_WITH_IMM` immediates, and direct recv-CQ
  materialization into service-owned fixed payload ready queues.
- **What this doc does NOT cover**: the planned detailed payload-ready
  diagnostic counters. Those remain follow-up work after Slice 2E-C.
- **Primary code**: `/data/dbcomm/citus-dbcomm`
- **Design source**:
  [`homer_completion_publication_v27_recovery_plan.md`](../../../future-directions/citus/transport/homer_completion_publication_v27_recovery_plan.md)

## Current Landed Behavior

The peer protocol version is now
[`CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:33)
`15` because the payload immediate token wire meaning changed.

The payload token ABI is defined by
[`HOMER_PAYLOAD_TOKEN_INDEX_BITS`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:40),
[`HOMER_PAYLOAD_TOKEN_GENERATION_SHIFT`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:42),
[`HomerEncodePayloadDoorbellToken()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:625),
and
[`HomerDecodePayloadDoorbellToken()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:651):

```text
bits 29:12 = receiver-issued payload token generation
bits 11:0  = receiver-local payload stream index + 1
```

Encoded index zero and generation zero are invalid. The `+1` index encoding
keeps raw token zero invalid while still giving the receiver an O(1) stream-table
slot in the next slice.

Each live payload stream now stores
[`localPayloadDoorbellToken`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1162),
[`peerPayloadDoorbellToken`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1163),
and a token generation snapshot. The generation counter itself lives in the
service-lifetime
[`PayloadDoorbellTokenGenerations`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2722)
array, not inside the resettable stream entry.

[`HomerServiceEnsureLocalPayloadDoorbellToken()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18471)
issues the local receiver token after the stream is active. It refuses generation
wrap instead of silently recycling token zero.

The peer-open protocol exchanges both directions:

- [`localPayloadDoorbellToken`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:103)
  in `CitusRemoteExecPeerOpenRequest` is the opener-local token that the
  responder uses for credit/consumed-head WIMMs back to the opener.
- [`peerPayloadDoorbellToken`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:142)
  in `CitusRemoteExecPeerOpenResponse` is the responder-local token that the
  opener uses for payload/frontier WIMMs to the responder.

[`TupleSinkServicePayloadDoorbellImmediateData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12835)
now returns `peerPayloadDoorbellToken`, not `peerServiceStreamId`.

## Direct Service Materialization

Slice 2E-B moves payload recv-CQ handling out of the old connection-local
pending array and into a narrow permanent dispatcher callback.

[`HomerPeerRecvDispatcher.onPayloadReady`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:224)
is now mandatory at transport creation time. The physical recv-CQ parser calls
that callback for `CITUS_REMOTE_EXEC_PEER_DOORBELL_KIND_PAYLOAD` CQEs at
[`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4274).
The service wires it to
[`TupleSinkServiceDispatchPeerPayloadDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20720).

The dispatcher does only the planned O(1) materialization work:

- decode the payload token into stream-table index and token generation;
- validate active stream state, local token, token generation, peer binding,
  peer connection handle, peer connection generation, and traffic class;
- enqueue the stream into the service-owned payload ready queue.

It does not inspect payload records, drain payload rings, allocate memory, or
run the payload executor.

Each stream now also stores the peer connection generation and coalesced ready
queue state:
[`peerConnectionGeneration`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1165),
[`recvReadyEnqueued`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1166),
and `recvReadyStreamGeneration`. The peer binding path captures the connection
generation at
[`HomerServiceBindPeerToPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19273),
and clear/reclaim invalidates the ready membership at
[`HomerServiceClearPayloadStreamPeerBinding()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19501).

## Fixed Ready Queue

The ready structure is an array-backed ring queue, not a linked list:

- [`HomerPayloadReadyQueue`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1545)
  contains fixed `head`, `tail`, `count`, `highWater`, and entry array fields.
- [`PayloadReadyQueues`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2742)
  has one queue per transport traffic class.
- [`HomerServiceEnqueuePayloadReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18644)
  coalesces repeated WIMMs while `recvReadyEnqueued` is true.
- [`HomerServiceClaimPayloadReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18706)
  unlinks the selected stream at executor entry.
- [`HomerServiceCompactPayloadReadyQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18612)
  lazily removes stale entries only under pressure.

The fixed queue keeps per-CQE work bounded and allocation-free. It deliberately
allows stale interior entries after close/reuse or non-head claim; those entries
are ignored by
[`HomerServicePayloadReadyQueueEntryStillValid()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18570),
dropped when they reach the head, and compacted before overflow.

Candidate construction calls
[`HomerServiceAppendPayloadReadyQueueSources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18778)
to append exact stream candidates without claiming them. The selected payload
executor claims the queue entry and rechecks stream/frontier state before
draining. Dense aggregate payload scans are intentionally not appended in the
same pass when exact queued doorbell candidates exist, so receive-doorbell
discovery does not pay the broad active-stream scan.

## Legacy State

Slice 2E-C deletes the legacy transport pending-array state and helper APIs.
The zero-reference check after implementation was:

```sh
rg -n "pendingDataDoorbell|TupleSinkServiceConsumePeerDataDoorbellRdma|TupleSinkServicePeerDataDoorbellPendingRdma" \
  /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.[ch] \
  /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c
```

It returned no matches.

[`receivePayloadDoorbellPending`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1248)
intentionally remains. It is no longer a transport pending-array shadow. It is
executor-local drain state set after
[`HomerServiceClaimPayloadReadyForStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18759)
claims a coalesced ready-queue entry, and it keeps the receiver draining an
already-doorbelled ring across budget/backpressure boundaries without requiring
another CQE.

## Validation

Validation used no-stats binaries built and installed as `dbcomm`, then synced
to `farnet0`. Both Homer services were restarted with the farnet host lane-0
addresses.

Evidence from June 22, 2026:

- Slice 2E-A build/install: `sudo -n -u dbcomm make -B -j8 service-bin client-bin
  CPPFLAGS='-D_GNU_SOURCE'` plus `make install-headers install-service-bin
  install` completed successfully.
- Slice 2E-A remote c1 cold smoke: `2000/2000`, zero failures.
- Slice 2E-A remote c1 warmed smoke: `5000/5000`, zero failures, `3816 TPS`, p99
  `0.295 ms`.
- Slice 2E-A remote RDMA basebackup: first post-restart correctness check
  completed successfully in `5.96 s`; this was not a warmed performance sample.
- Slice 2E-A remote c4 smoke: `12000/12000`, zero failures, `9458 TPS`, p99
  `0.664 ms`.
- Slice 2E-B build/install: `sudo -n -u dbcomm make -B -j8 service-bin
  client-bin CPPFLAGS='-D_GNU_SOURCE'` plus `make install-headers
  install-service-bin install` completed successfully.
- Slice 2E-B remote c1 cold smoke: `2000/2000`, zero failures. This run had one
  cold outlier and reported `873 TPS`, so it is diagnostic only.
- Slice 2E-B remote c1 warmed smoke: `5000/5000`, zero failures, `3839 TPS`,
  p99 `0.287 ms`.
- Slice 2E-B remote c4 smoke: `12000/12000`, zero failures, `9505 TPS`, p99
  `0.636 ms`.
- Slice 2E-B remote RDMA basebackup: first post-restart correctness check
  completed successfully in `5.97 s`; this was not a warmed performance sample.
- Slice 2E-C zero-reference check for legacy pending-array symbols returned no
  matches.
- Slice 2E-C build/install: `sudo -n -u dbcomm make -B -j8 service-bin
  client-bin CPPFLAGS='-D_GNU_SOURCE'` plus `make install-headers
  install-service-bin install` completed successfully.
- Slice 2E-C remote c1 cold smoke: `2000/2000`, zero failures. This had the
  same one-time cold outlier shape as 2E-B and reported `875 TPS`, so it is
  diagnostic only.
- Slice 2E-C remote c1 warmed smoke: `5000/5000`, zero failures, `3878 TPS`,
  p99 `0.282 ms`.
- Slice 2E-C remote c4 smoke: `12000/12000`, zero failures, `9726 TPS`, p99
  `0.651 ms`.
- Slice 2E-C remote RDMA basebackup: first post-restart correctness check
  completed successfully in `5.81 s`; this was not a warmed performance sample.
- Slice 2E-C warmed remote RDMA basebackup repeat, run without restarting
  services after the validation sequence: run1 `4.32 s`, run2 `4.17 s`, run3
  `4.16 s`, run4 `4.24 s`. Treat runs 2-4 as the warmed band: `4.16-4.24 s`.

## Remaining Work

- Add the planned counters for payload token decode, binding lookup, enqueue,
  stale-token errors, queue compaction, and fallback discovery.
