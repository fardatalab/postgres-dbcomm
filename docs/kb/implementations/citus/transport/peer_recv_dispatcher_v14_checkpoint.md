# Peer Recv Dispatcher v14 Checkpoint

## Scope

- **What this doc explains**: the landed Slice 2A checkpoint for the peer
  recv-CQ dispatcher: peer protocol v14 immediate-data ABI, one permanent
  transport-owned recv dispatcher, direct dispatch of all steady-state WIMM
  CQEs, removal of the optional peer-client-completion handler path from the
  service pump, and the Slice 2D recv-CQ ownership/control-mailbox checkpoints.
- **What this doc does NOT cover**: direct binding tables or typed ready-state
  replacement for command and payload. Those remain later Slice 2 work.
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

Slice 2B followed this checkpoint and removed the now-dead peer-client
completion FIFO state:

- `pendingClientCompletionDoorbellHead`;
- `pendingClientCompletionDoorbellCount`;
- `pendingClientCompletionDoorbellTokens`;
- `TupleSinkServiceConsumePeerClientCompletionDoorbellRdma()`;
- the unused optional command/completion doorbell handler typedefs.

This was a cleanup stage, not a new retry-state implementation. After v14, the
canonical recv-CQ dispatcher handles peer-client completion WIMM CQEs
immediately. A matching CQE validates and CPU-publishes the frontend-ready epoch;
a stale CQE is discarded by the session-generation check in the receiver-side
completion handler. There is no intermediate completion-token FIFO producer left
to preserve.

This checkpoint intentionally does not complete every planned Slice 2 item from
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

Control mailbox readiness is now transport-CQE owned after Slice 2D1. The peer
control mailbox protocol version is
[`CITUS_REMOTE_EXEC_PEER_CONTROL_MAILBOX_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:59)
`2`, because the publication semantics changed even though the shared mailbox
layout still retains the old `publishedTail` word. The sender-side publication
point,
[`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4452),
now posts one full-message `RDMA_WRITE_WITH_IMM` directly to the remote mailbox
slot. It no longer posts a separate body WRITE followed by an 8-byte
`publishedTail` WIMM.

On the receiver,
[`TupleSinkServiceQueuePeerControlDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:614)
turns each decoded control CQE into `controlDoorbellArrivedTail` and the
level-triggered `controlMailboxReady` bit. It fails the connection if the CQE
frontier would overrun the local control mailbox ring. The mailbox precheck and
consumer,
[`TupleSinkServiceLocalMailboxMayHaveMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4366)
and
[`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4390),
only drain while `consumedHead < controlDoorbellArrivedTail`. They no longer
observe remote `publishedTail` as a semantic readiness source and no longer have
stale-late-doorbell reconciliation.

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

Slice 2B cleanup validation:

```text
Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'
Zero-reference check: no remaining pendingClientCompletionDoorbell,
    TupleSinkServiceConsumePeerClientCompletionDoorbellRdma,
    PeerCommandDoorbellHandler, PeerClientCompletionDoorbellHandler, or
    clientCompletionDoorbellHandler references

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 500 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3924 TPS, p99 0.281 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9785 TPS, p99 0.614 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9704 TPS, p99 0.631 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9497 TPS, p99 0.679 ms
```

The next cleanup removed the equally dead command-doorbell token FIFO:

- `pendingCommandDoorbellCount`;
- `pendingCommandDoorbellSessionIds`;
- `TupleSinkServiceConsumePeerCommandDoorbellRdma()`;
- `TupleSinkServicePopPeerCommandDoorbellRdma()`.

Before this cleanup, the recv-CQ dispatcher pushed command tokens into the
transport connection, but no service-side command executor popped that queue.
Current command readiness is built from the durable per-session command mailbox
fact (`publishedEpoch` versus accepted/retired epoch) in the scheduler. The
dispatcher now validates the command WIMM token and leaves readiness to that
existing mailbox-based path. This keeps behavior unchanged while removing the
generic token FIFO. The future direct-ready-bit slice should still replace the
session scan with exact typed readiness; this cleanup does not claim to have
implemented that optimization.

Command FIFO cleanup validation:

```text
Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'
Zero-reference check: no remaining pendingCommandDoorbell,
    TupleSinkServiceConsumePeerCommandDoorbellRdma,
    TupleSinkServicePopPeerCommandDoorbellRdma, or
    TupleSinkServiceQueuePeerCommandDoorbellRdma references

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 502 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3951 TPS, p99 0.281 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9798 TPS, p99 0.622 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9602 TPS, p99 0.631 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9489 TPS, p99 0.685 ms
```

Slice 2D0a recv-CQ owner-phase scaffolding validation:

```text
Code change:
    add explicit recv-CQ owner phases UNUSED, BOOTSTRAP, CANONICAL, TEARDOWN
    set BOOTSTRAP after incoming/outgoing connection resources are initialized
    set CANONICAL in TupleSinkServiceFinishPeerConnectionSetup()
    set TEARDOWN before connection resource release/reset
    guard existing TupleSinkServiceDrainPeerConnectionEvents() recv-CQ polling
    expose noncanonicalRecvCqPollAttempts in stats builds

Important non-changes:
    TupleSinkServiceApplyPeerBootstrapMessage() still sets bootstrapComplete
    TupleSinkServicePrepareConnectionForWrite() still performs hidden recv-CQ/CM progress
    control publication is still message WRITE plus tail WRITE_WITH_IMM
    pendingControlDoorbellCount and visible-tail fallback still exist

Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 499 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3958 TPS, p99 0.278 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9693 TPS, p99 0.640 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9586 TPS, p99 0.641 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9348 TPS, p99 0.663 ms
```

Slice 2D0b bootstrap ownership cleanup validation:

```text
Code change:
    TupleSinkServiceApplyPeerBootstrapMessage() only validates and stores the peer mailbox descriptor
    TupleSinkServiceApplyPeerBootstrapMessage() no longer sets bootstrapComplete
    TupleSinkServiceFinishPeerConnectionSetup() is the sole bootstrapComplete/CANONICAL transition
    passive-side setup no longer needs to undo an early bootstrapComplete=true

Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 499 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3948 TPS, p99 0.278 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9760 TPS, p99 0.624 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9565 TPS, p99 0.637 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9401 TPS, p99 0.682 ms
```

Slice 2D0c hidden recv-CQ ownership cleanup validation:

```text
Code change:
    add TupleSinkServiceDrainPeerConnectionRecvCq()
    add TupleSinkServiceDrainPeerConnectionCmEvents()
    keep the scheduled peer pump as the recv-CQ polling owner
    make TupleSinkServicePrepareConnectionForWrite() CM-only
    make TupleSinkServiceTryPublishPeerControlAsyncOp() CM-only for connection events
    make TupleSinkServicePollPeerRequestRdma() CM-only for connection events

Important non-changes:
    peer-op helpers still drain send CQ and response mailbox
    control publication is still message WRITE plus tail WRITE_WITH_IMM
    pendingControlDoorbellCount and visible-tail fallback still exist

Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 498 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3910 TPS, p99 0.281 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9720 TPS, p99 0.629 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9669 TPS, p99 0.654 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9375 TPS, p99 0.702 ms
```

Slice 2D1 one-WIMM control-mailbox publication validation:

```text
Code change:
    bump CITUS_REMOTE_EXEC_PEER_CONTROL_MAILBOX_PROTOCOL_VERSION to 2
    replace pendingControlDoorbellCount with controlDoorbellArrivedTail and controlMailboxReady
    make each decoded CONTROL CQE advance the CQE-derived arrival frontier
    fail fast if arrivedTail - consumedHead exceeds the local mailbox slot count
    make TupleSinkServicePublishControlMessage() post one full-message RDMA_WRITE_WITH_IMM
    stop writing remote publishedTail as the control publication gate
    make TupleSinkServiceTryConsumeLocalMailbox() drain only CQE-authorized slots
    delete visible-tail fallback and stale-late-doorbell handling

Important non-changes:
    publishedTail remains in the shared mailbox layout for now but is not receiver readiness
    CONTROL immediate token zero remains connection-scoped
    peer-op helpers still drain send CQ and response mailbox
    command and payload typed ready-state migrations remain later Slice 2 work

Build/install: passed with CPPFLAGS='-D_GNU_SOURCE'

remote c1 -t 1000:   1000/1000 transactions, 0 failures, 498 TPS cold including setup
remote c1 -t 100000: 100000/100000 transactions, 0 failures, 3929 TPS, p99 0.280 ms

remote c4 warmup:    40000/40000 transactions, 0 failures, 9654 TPS, p99 0.618 ms
remote c4 measured1: 40000/40000 transactions, 0 failures, 9254 TPS, p99 0.657 ms
remote c4 measured2: 40000/40000 transactions, 0 failures, 9345 TPS, p99 0.654 ms
```

Service logs on both hosts after Slice 2D1 showed normal RDMA setup and session
lifecycle messages, with no peer control mailbox protocol mismatch, control
ring sequence mismatch, overrun, immediate decode error, or failed pgbench
transaction.
