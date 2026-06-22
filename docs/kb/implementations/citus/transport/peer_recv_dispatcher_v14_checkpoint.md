# Peer Recv Dispatcher v14 Checkpoint

## Scope

- **What this doc explains**: the landed Slice 2A checkpoint for the peer
  recv-CQ dispatcher: peer protocol v14 immediate-data ABI, one permanent
  transport-owned recv dispatcher, direct dispatch of all steady-state WIMM
  CQEs, and removal of the optional peer-client-completion handler path from the
  service pump.
- **What this doc does NOT cover**: direct binding tables, split recv-CQ versus
  CM drain APIs, bootstrap/canonical/teardown recv-CQ owner phases, or typed
  ready-state replacement for command/control/payload. Those remain later Slice
  2 work.
- **Primary code**: `/data/dbcomm/citus-dbcomm`
- **Design source**:
  [`homer_completion_publication_v27_recovery_plan.md`](../../../future-directions/citus/transport/homer_completion_publication_v27_recovery_plan.md)

## Current Landed Behavior

The peer protocol version is now
[`CITUS_REMOTE_EXEC_PEER_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:33)
`14`.

The peer WIMM immediate ABI is documented next to the transport macros in
[`remote_execution_peer_transport_rdma.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:64):

```text
bits 31:30 = doorbell kind
bits 29:0  = kind-local token

00 = payload
01 = peer-client completion
10 = peer-client command
11 = control
```

Control uses token `0`; payload, completion, and command require nonzero
tokens. This removes the old raw-zero control sentinel that overlapped the
payload namespace.

[`HomerPeerRecvDispatcher`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:215)
is installed when
[`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5093)
creates the peer transport. The dispatcher currently has one service callback,
`onClientCompletion`, because peer-client completion publication must access the
service session table to validate the v27 body/seal and CPU-publish frontend
ready state.

Command, payload, and control event materialization remains inside the transport
for this checkpoint because their existing pending structures are
transport-owned. The recv-CQ collector decodes every WIMM CQE through
[`TupleSinkServiceDecodePeerDoorbellImmediate()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:567)
and then dispatches by kind inside
[`TupleSinkServiceDrainPeerConnectionEvents()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3955).

The service-level peer pump no longer supplies per-grant optional completion
handlers. Its permanent completion bridge is
[`TupleSinkServiceDispatchPeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20284),
which delegates to the v27 receiver-side publication handler after the physical
collector has decoded the completion token.

## Design Adjustment During Validation

The first implementation attempt made `HomerPeerRecvDispatcher` carry dynamic
callbacks for command, payload, control, and completion. That was correct but
too expensive for the current c4 hot path: remote c4 dropped to roughly
`7.7k-8.8k TPS` while c1 stayed correct.

The validated implementation keeps only the service-owned completion callback
indirect and performs command/payload/control materialization directly in the
transport collector. This better matches the ownership model:

- command pending state is transport-owned;
- payload pending state is transport-owned;
- control mailbox readiness is transport-owned;
- completion frontend-ready publication crosses into service session state.

After that correction, c4 recovered to the Slice 1 performance band.

## Remaining Slice 2 Work

This checkpoint intentionally does not complete every planned Slice 2A item from
the future-direction plan. The next slices should still implement:

- direct kind-specific binding tables for command, completion, and payload
  tokens;
- split recv-CQ and CM drain APIs;
- explicit bootstrap/canonical/teardown recv-CQ owner phases;
- typed completion ready state, then deletion of residual
  `pendingClientCompletionDoorbell*` fields and cleanup helpers;
- typed command, control, and payload ready state, followed by deletion of their
  legacy pending arrays/counters;
- final assertion/debug counter that rejects any noncanonical steady-state
  recv-CQ poll.

Do not reintroduce optional semantic handlers or a generic CQE/token FIFO as a
replacement for those typed states.

## Validation Evidence

Build/install used no-stats binaries:

```text
sudo -n -u dbcomm make -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
sudo -n -u dbcomm make install-headers install-service-bin install
```

The installed prefix was synced to `farnet0`, then farnet1 PostgreSQL and both
Homer services were restarted from a clean shared-memory baseline.

Remote RDMA c1:

```text
-t 1000:   1000/1000 transactions, 0 failures, 502 TPS cold including setup
-t 100000: 100000/100000 transactions, 0 failures, 3938 TPS, p99 0.279 ms
```

Remote RDMA c4:

```text
warmup:    40000/40000 transactions, 0 failures, 9666 TPS, p99 0.620 ms
measured1: 40000/40000 transactions, 0 failures, 9531 TPS, p99 0.633 ms
measured2: 40000/40000 transactions, 0 failures, 9188 TPS, p99 0.658 ms
```

Service logs on both hosts showed normal RDMA setup and session lifecycle
messages, with no immediate decode errors, completion seal mismatch, descriptor
mismatch, or failed-transaction symptoms.
