# Peer-Client Completion v27 Checkpoint

## Scope

- **What this doc explains**: the landed Slice 1 implementation for the
  peer-client completion publication v27 protocol: frontend mailbox ABI v27,
  one full-slot `RDMA_WRITE_WITH_IMM`, receiver-service CPU publication, and
  descriptor trailing seals.
- **What this doc does NOT cover**: the later canonical recv-CQ dispatcher,
  direct immediate-data token ABI, typed ready-state materialization, or removal
  of the optional handler/FIFO paths.
- **Primary code**: `/data/dbcomm/citus-dbcomm`
- **Design source**:
  [`homer_completion_publication_v27_recovery_plan.md`](../../../future-directions/citus/transport/homer_completion_publication_v27_recovery_plan.md)

## Current Landed Behavior

The protocol version is now
[`CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:29)
`27`.
The control shared-memory name and frontend/peer completion shared-memory
prefixes are also bumped from `v26` to `v27`, so stale v26 regions cannot be
opened accidentally after the mailbox layout change.

The frontend completion mailbox
[`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:879)
no longer carries frontend-client `publishedEpoch` or
`peerCompletionDoorbellScratch`. The only frontend visibility gate is the
slot-local `readyEpochSlots[]` array. The service tracks the producer cursor in
[`TupleSinkServiceClientCompletionMailboxState.frontendCompletionPublishedEpoch`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:828).

The frontend client opens the mailbox in
[`HomerClientOpenCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:588)
and initializes `lastConsumedCompletionEpoch` from `consumedEpoch`, not from a
service-published aggregate hint. The frontend stable-copy path remains
[`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1107):
it polls `readyEpochSlots[slot]`, copies the full slot, checks the ready epoch
again, and validates the trailing completion seal before applying the command
completion.

## Publication Path

Local service-to-frontend completion publication in
[`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14013)
fills the completion body and trailing seal, publishes any descriptor
`readyVersion`, CPU-stores `readyEpochSlots[slot]`, and then advances the
service-local `frontendCompletionPublishedEpoch` cursor. It does not write a
frontend aggregate `publishedEpoch`.

Peer publication in
[`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14198)
now posts:

```text
descriptor cache hit:
    WR 1: full completion slot WRITE_WITH_IMM

descriptor cache miss:
    WR 1: descriptor body + descriptor seal, ordinary RDMA WRITE
    WR 2: full completion slot WRITE_WITH_IMM
```

The final WIMM is posted through
[`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5936).
The WIMM source range is the full
`CitusRemoteExecClientCompletionSlot`, so the receiver CQE corresponds to the
same WQE that wrote the completion body and seal.

The peer sender does not write frontend `readyEpochSlots[]` and does not write
descriptor `readyVersion[]`. The receiver-side doorbell handler
[`TupleSinkServiceHandlePeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20139)
validates the completion body/seal, validates any referenced descriptor
body/seal, CPU-publishes descriptor `readyVersion[]`, CPU-publishes
`readyEpochSlots[slot]`, and advances the receiver service-local cursor.

Two ownership details are part of this Slice 1 checkpoint:

- after observing the WIMM recv CQE, the receiver service performs an acquire
  fence before reading the RDMA-written completion slot and descriptor image;
- if the descriptor body WR was accepted but the final completion WIMM post
  fails, the source slot remains fenced off for connection teardown, but the
  send-CQ owner-table entry is cleared because no final WIMM CQE can arrive to
  retire it.

## Descriptor Seal

[`HomerResultDescriptorSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:604)
now has a trailing `sealVersion`. Local descriptor publication through
[`TupleSinkServicePublishResultDescriptorLocal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13714)
stores the descriptor body/seal before CPU-publishing `readyVersion`.

Frontend descriptor reads through
[`CitusRemoteExecReadResultDescriptorStable()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:800)
now reject a ready descriptor whose `sealVersion` does not match the requested
descriptor version.

## Validation Evidence

Build/install:

```text
sudo -n -u dbcomm make -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
sudo -n -u dbcomm make install-headers install-service-bin install
sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build \
  src/backend/postgres src/bin/pgbench/pgbench src/bin/pg_basebackup/pg_basebackup
sudo -n -u dbcomm meson install -C build --no-rebuild
```

Runtime validation used the lane-0 host path:

```text
farnet0 pgbench -> farnet0 Homer service -> RDMA -> farnet1 Homer service
-> farnet1 socketless backend
```

Remote c1 correctness/performance:

```text
-t 1000:   1000/1000 transactions, 0 failures, 502 TPS cold including setup
-t 100000: 100000/100000 transactions, 0 failures, 3940 TPS, p99 0.285 ms
```

Remote c4 after final v27 shared-memory-name rebuild, sync, and clean restart:

```text
warmup:    40000/40000 transactions, 0 failures, 9796 TPS, p99 0.616 ms
measured1: 40000/40000 transactions, 0 failures, 9764 TPS, p99 0.651 ms
measured2: 40000/40000 transactions, 0 failures, 9536 TPS, p99 0.678 ms
```

An earlier c4 repeat set without a clean restart drifted down to about
`6.3k-9.5k TPS` with regular `~25 ms` max-latency outliers. A clean runtime
baseline restored the measured c4 band. One runbook trap was also found during
validation: `pkill -x citus_tuple_sink_service` does not match the truncated
Linux `comm` name (`citus_tuple_sin`), so farnet0 kept an old service PID until
it was killed explicitly. Those runs are treated as diagnostic/runtime-state
contaminated rather than the Slice 1 acceptance number.

Service logs on both hosts showed normal RDMA setup and session lifecycle
messages, with no completion seal mismatch, descriptor mismatch, source reuse,
ordering-gap, or failed-transaction symptoms.

## Remaining Work

- Slice 2 still owns the canonical recv-CQ dispatcher, direct immediate-data ABI
  with kind/index/generation tokens, typed ready state, and FIFO/optional-handler
  deletion.
- Deterministic failure-injection gates from the future-direction plan were not
  added in this Slice 1 commit: forced descriptor miss/shape-change tests,
  explicit eight-slot mailbox wrap stress, delayed send-CQ retirement injection,
  body-post failure injection, and session-close-with-outstanding-owner
  injection.
- The full `10` warmed c4 plus `5` c4-with-basebackup acceptance matrix from the
  future plan was not run in this checkpoint. The committed evidence is the
  compile/install gate plus remote c1/c4 correctness and warmed c4 performance.
