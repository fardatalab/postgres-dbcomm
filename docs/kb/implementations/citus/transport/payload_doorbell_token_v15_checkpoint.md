# Payload Doorbell Token v15 Checkpoint

## Scope

- **What this doc explains**: the landed Slice 2E-A checkpoint for Homer payload
  doorbell tokens: peer protocol v15, receiver-issued generation-bearing payload
  tokens, peer-open token exchange, sender use of the peer token in payload
  `WRITE_WITH_IMM` immediates, and the temporary bridge through the old pending
  payload-doorbell array.
- **What this doc does NOT cover**: the Slice 2E-B direct service materializer,
  fixed payload ready queues, or deletion of the legacy pending array. Those
  remain next-stage work.
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

## Temporary Bridge

Slice 2E-A deliberately leaves the old pending-array materialization in place
for one checkpoint. The transport field
[`pendingDataDoorbellSinkIds`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:505)
still has its legacy name, but it now stores payload doorbell tokens.

[`TupleSinkServiceQueuePeerPayloadDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:741)
validates token syntax and inserts the token into that temporary array.
Scheduler readiness and payload pumps still call
[`TupleSinkServicePeerDataDoorbellPendingRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6916)
and
[`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6862),
but they now pass the stream-local token rather than `serviceStreamId`.

This is not the final ownership model. Slice 2E-B must replace this bridge with
direct service-owned ready-queue materialization, and Slice 2E-C must delete the
legacy pending array and helper APIs.

## Validation

Validation used no-stats binaries built and installed as `dbcomm`, then synced
to `farnet0`. Both Homer services were restarted with the farnet host lane-0
addresses.

Evidence from June 22, 2026:

- Build/install: `sudo -n -u dbcomm make -B -j8 service-bin client-bin
  CPPFLAGS='-D_GNU_SOURCE'` plus `make install-headers install-service-bin
  install` completed successfully.
- Remote c1 cold smoke: `2000/2000`, zero failures.
- Remote c1 warmed smoke: `5000/5000`, zero failures, `3816 TPS`, p99
  `0.295 ms`.
- Remote RDMA basebackup: completed successfully in `5.96 s`.
- Remote c4 smoke: `12000/12000`, zero failures, `9458 TPS`, p99 `0.664 ms`.

## Remaining Work

- Slice 2E-B: add the permanent narrow payload dispatcher callback, direct
  token-to-stream validation, and fixed per-traffic-class array-backed ready
  queues.
- Slice 2E-C: delete `pendingDataDoorbellSinkIds`,
  `pendingDataDoorbellCount`, `TupleSinkServiceQueuePeerPayloadDoorbellRdma()`,
  `TupleSinkServiceConsumePeerDataDoorbellRdma()`, and
  `TupleSinkServicePeerDataDoorbellPendingRdma()`.
- Add the planned counters for payload token decode, binding lookup, enqueue,
  stale-token errors, queue compaction, and fallback discovery.
