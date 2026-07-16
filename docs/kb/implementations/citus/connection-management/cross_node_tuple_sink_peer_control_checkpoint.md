# Cross-node tuple-sink peer-control checkpoint

<!-- kb-summary: Current cross-node tuple-sink control, semantic decode, batching, terminal signaling, and worker borrow/use/release checkpoint. -->

## Scope

- **What this doc explains**: the current implementation checkpoint for the tuple-sink prototype: full tuple-view contract exchange, peer-provisioned exact sinks, service-side semantic receive decode, typed command/completion dispatch, batching/backpressure, and the worker-side borrow/use/release insert path.
- **What this doc does NOT cover**: cross-transaction backend reuse, non tuple-sink `RemoteExecutionSession` modes, a richer multi-slot control ring, or canonical wire serialization.
- **Primary directory**: `docs/kb/implementations/citus/connection-management/`
- **Doc type**: `implementation`

## Current status note

This is the canonical implementation note for the active tuple-sink vertical slice.

Older implementation notes in this directory remain useful as historical checkpoints, but they should no longer be read as the latest state for:

- tuple-view contract handling
- receive-side semantic decode
- batching/backpressure
- worker-side tuple consumption
- borrow/use/release

## Why this exists

The earlier local-only and wrapper checkpoints stopped before the current end-to-end worker path existed:

- the wrapper note [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md) introduced the backend-facing [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034) API shape
- the local service-owned checkpoint [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md) moved local SHM control ownership into the standalone service

The current checkpoint goes further. It now covers the grounded path where:

1. the sender backend opens a tuple-sink session with a full [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:124)
2. the local service peer-opens an exact receive sink on the remote service through [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3831)
3. the remote service receives tuple payload, semantically decodes it into the backend-visible receive sink through [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415), and publishes batches locally
4. the worker-side socketless backend drains those batches and inserts them through [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202)
5. the worker hot path now borrows by-reference values directly from the receive batch instead of copying them out first through [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3223)

So the current remaining work is no longer “finish the first tuple-sink vertical slice.” The remaining work is narrower follow-up: speculative startup, better terminal failure surfacing, cross-transaction reuse, non tuple-sink modes, and later wire-format redesign.

## Key code pointers

- backend-side session open, contract construction, and sender-side batching:
  - [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:583)
  - [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1542)
  - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034)
  - [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2917)
  - [`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2744)
  - [`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3109)
  - [`FlushRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3120)
- control-path protocol objects:
  - [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:124)
  - [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:276)
  - [`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75)
- service-side peer open, contract install, payload decode, and sink lifetime:
  - [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3831)
  - [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4044)
  - [`TupleSinkServiceEnsureTupleViewContract()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2538)
  - [`TupleSinkServiceFindExactReceiveAttachableSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3260)
  - [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5158)
  - [`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2217)
  - [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415)
  - [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4720)
  - [`TupleSinkServiceMarkSendQueueTerminal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3625)
  - [`TupleSinkServiceMaybeReclaimSinkAndSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3679)
- tuple-sink queue/batch substrate:
  - [`TryReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1038)
  - [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1180)
  - [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1353)
  - [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1425)
  - [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1529)
  - [`DecodeTupleViewFromTupleSinkRow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1592)
  - [`BorrowNextTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1725)
  - [`ReleaseBorrowedTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1792)
  - [`CitusTupleSinkReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1499)
- worker-side command/consume/insert path:
  - [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603)
  - [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1682)
  - [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:646)
  - [`RemoteExecBackendExecuteTxBeginAttachCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:460)
  - [`RemoteExecBackendExecuteTxPrepareCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:531)
  - [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:568)
  - [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:601)
  - [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202)
  - [`CreateTupleSinkInsertExecutionState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:67)
  - [`ExecStoreVirtualTuple()`](/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:1641)
  - [`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus/src/backend/executor/execReplication.c:490)
- coordinator-side orchestration:
  - [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2651)
  - [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2815)
  - [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2881)
  - [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3129)
  - [`ExperimentalTupleSinkPlacementOwnsDelivery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2627)

## Exact behavior implemented now

### 1. Full tuple-view contract is now the active control-path schema contract

The old digest-only contract is no longer the active open/attach model.

The backend now builds the full per-attribute contract in [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:583), sends it in the local open request [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:276), and also sends it in the cross-node peer-open request [`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75).

The service validates and installs that contract in [`TupleSinkServiceEnsureTupleViewContract()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2538).

This matters because the service can now semantically decode incoming rows immediately on the receiving side, without waiting for a later backend-local attach to provide missing tuple-shape metadata.

### 2. Peer open now provisions the exact receive sink with authoritative geometry

The sender-side local open still starts at [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034), but success now implies more than “local handle exists”.

The service-side send open in [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5158) calls [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3831), and the remote service eagerly provisions the exact receive sink in [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4044).

One important correction landed during batching validation: once an exact receive sink has been peer-provisioned, its geometry is authoritative. The worker-side local receive attach path now finds that existing sink in [`TupleSinkServiceFindExactReceiveAttachableSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3260) and must not reject it just because the worker still has a different local slot-capacity GUC. That authoritative-geometry rule is enforced in [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5158).

### 3. Receive-side service path now semantically decodes into the local receive sink

The active receive service path no longer republishes the transported batch image blindly.

[`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4720) now validates the incoming batch and rebuilds the local receive-side tuple-view batch through [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415), which in turn walks each row through [`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2217).

This is still not canonical cross-platform serialization. The active wire and sink row layouts remain intentionally the same for now, and the receive service still enforces the current equal-width invariant while rebuilding the receive-side batch. But the semantic boundary is already in the right place: the service owns wire-row to tuple-view-sink reconstruction, not the backend worker.

### 4. Batching and backpressure now exist below the COPY caller

The sender path is no longer forced into fail-fast one-tuple-per-submit behavior.

At the low level, the tuple-sink/session layers now expose try-style reserve/append calls:

- [`TryReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1038)
- [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1180)
- [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2917)

The first wait policy is centralized in [`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2744): it retries on transient fullness, checks interrupts, sleeps briefly, and stops on terminal state.

The default COPY producer now appends into a session-managed stream via [`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3109) and flushes it with [`FlushRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3120), rather than explicitly reserving and publishing one batch per tuple in the coordinator loop.

The explicit batched path also exists and is exercised by the COPY integration hooks under [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3129).

### 5. The worker now uses typed command dispatch plus the socketless backend bridge

The experimental tuple-sink path is no longer a service-only or SQL-wrapper-only prototype.

The coordinator starts typed worker commands in [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2651). The service publishes those commands through the current one-slot mailbox in [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603), waits for startup through [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1682), and hands execution to the socketless worker backend loop in [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:646).

That backend loop now owns the full typed lifecycle:

- transaction attach through [`RemoteExecBackendExecuteTxBeginAttachCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:460)
- tuple ingest through [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202)
- transaction finalization through [`RemoteExecBackendExecuteTxPrepareCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:531), [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:568), and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:601)

The coordinator-side data path also now really switches away from the old COPY payload for placements owned by the tuple-sink path through [`ExperimentalTupleSinkPlacementOwnsDelivery()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2627).

### 6. The receive hot path now uses borrow / use / release instead of copy-out

The old “borrow later” plan is now implemented on the worker fast path.

At the tuple-sink layer:

- row decode still happens through the sequential row walk in [`DecodeTupleViewFromTupleSinkRow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1592)
- the borrowed path is exposed through [`BorrowNextTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1725)
- the borrowed lifetime ends through [`ReleaseBorrowedTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1792)

At the session layer:

- [`RemoteTupleView`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:200) is now the borrowed wrapper over caller-owned `Datum *values` / `bool *isnull`
- [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3223) fills those arrays
- [`ReleaseBorrowedRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3254) ends the borrow

At the worker:

- the execution slot’s [`tts_values`](/data/dbcomm/postgres-citus/src/include/executor/tuptable.h:125) and [`tts_isnull`](/data/dbcomm/postgres-citus/src/include/executor/tuptable.h:127) arrays are bound once
- [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:202) borrows each row at [worker_tuple_sink_insert.c:361](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:361), calls [`ExecStoreVirtualTuple()`](/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:1641) and [`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus/src/backend/executor/execReplication.c:490) at [worker_tuple_sink_insert.c:376](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:376), then releases the borrow at [worker_tuple_sink_insert.c:380](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:380)

The important performance consequence is narrow and real: the worker hot path no longer pays the extra `datumCopy()` for by-reference fields before handing the slot to the executor. The final storage write inside [`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus/src/backend/executor/execReplication.c:490) is still inevitable, but the earlier sink-to-worker copy is gone.

### 7. Slot, batch, and borrowed-tuple lifetime rules are now explicit

One receive slot is still one fixed-size batch. The receive-side lifecycle is:

1. the worker borrows a batch through [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3132), which wraps [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1425)
2. tuples are walked sequentially from that slot
3. each borrowed tuple remains valid until the worker releases it or borrows the next tuple from the same batch
4. the slot remains pinned until the whole batch is released through [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3294), which reaches [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1529)

Two defensive rules landed with that:

- the batch handle tracks an active borrowed tuple and release misuse is checked loudly
- [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1529) force-clears a leaked borrow during cleanup instead of raising a second masking error

This batch-scoped lifetime is also why the worker release must stay after [`ExecSimpleRelationInsert()`](/data/dbcomm/postgres-citus/src/backend/executor/execReplication.c:490), not merely after [`ExecStoreVirtualTuple()`](/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:1641). Triggers, generated columns, and constraints still execute inside the insert path.

### 8. The low-level receive API distinguishes failure, but the tuple-session wrapper does not yet propagate it

The current byte-ring frontend keeps graceful peer close and terminal failure as different states. [`CitusTupleSinkReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:2295) exposes the successful peer-close flag. [`CitusTupleSinkReceivePeerFailed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:2314) checks for `CITUS_TUPLE_SINK_TERMINAL_FAILED` and returns its failure code, while [`CitusTupleSinkReceiveTerminalState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:2343) exposes the raw terminal state to callers that must distinguish failure from graceful quiescence.

The substrate contract is therefore no longer “failure and EOF are the same bit”: peer-closed is successful EOF, while `FAILED` is an error and carries a failure code. However, this distinction is **not yet end-to-end on the tuple-worker path**. [`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:1448) still exposes only the graceful-close predicate, and the worker loop checks only that wrapper in [`ExecuteWorkerTupleSinkInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:350). Propagating the low-level failed state through the session wrapper and worker remains a current gap.

## Validation state

The current vertical slice has been validated in three relevant ways:

- service-side semantic receive decode and worker insert path completed end-to-end through the worker service and socketless backend path
- batching/backpressure was exercised with multi-tuple batches on the worker path, including explicit-batch validation and a `WOULD_BLOCK` test seam
- the borrow/use/release worker path was revalidated from a clean runtime baseline after removing stale runtime state, using a 250-row COPY run whose worker shard count matched the coordinator-visible inserted range

One operational caveat is now grounded enough to record here: validation must start from a clean baseline. Stale `postgres: remote exec backend` children and lingering SysV IPC can keep old runtime state alive even after PostgreSQL and `citus_tuple_sink_service` are restarted. The repository runbook in [AGENTS.md](/data/dbcomm/postgres-citus/AGENTS.md) now records that cleanup sequence explicitly.

## Design choices and corrections that matter now

- The old tuple-contract digest design is superseded in the active open/attach path. The full [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:124) is now the service-visible schema contract.
- The receive-side service is no longer just a byte forwarder. It owns semantic row reconstruction into the local receive sink.
- The worker fast path is no longer copy-out-first. It now borrows directly into slot-owned arrays.
- The current sink row format is still compact and sequential, not literal PostgreSQL `Datum[]` on wire or in the sink. The small remaining decode cost is the row walk itself: unpack the null bitmap, load by-value fields through [`fetch_att()`](/data/dbcomm/postgres-citus/src/include/access/tupmacs.h:52), and bind by-reference fields as borrowed pointers into the batch.
- The current row packing still uses conservative `MAXALIGN`-style row walking. That is safe but likely over-aligned. It is a follow-up density/performance topic, not a correctness gap.

## Known gaps after this checkpoint

- Speculative startup is still future. The command plane is still one in-flight command per session in [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603), and the sender still waits synchronously for startup in [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1682) and [`StartRemoteExecutionCommandThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1811).
- Receive-side transport failure still does not surface as a distinct backend-visible terminal bit. The worker still relies on [`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3178) for EOS.
- The current implementation is still transaction-scoped, not cross-transaction reusable. Sessions are opened in [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034) under transaction lifetime, and the socketless backend exits on terminal tx commands in [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:568) and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:601).
- Non tuple-sink `RemoteExecutionSession` modes are still not implemented.
- Control-plane generalization is still future. There is still no richer multi-slot control ring, no child-sink open path, and no keepalive protocol beneath the current persistent peer transport.
- Canonical serialization is still future. The current format still assumes same-ABI tuple layout semantics and still keeps wire row width intentionally equal to the current receive-sink row width.
- Concurrent SQL-command sessions are explicitly deferred. The pgbench transaction
  milestone is single-client only for now; multi-client runs need a follow-up
  design for peer command multiplexing rather than a PostgreSQL advisory lock
  around the wrapper. The current peer RDMA request path still documents its
  one-outstanding-request limitation in
  [`TupleSinkServiceSendPeerRequestRdmaInternal()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3198).
- The farnet pgbench smoke test exposed a peer-control response timeout after
  worker-side command completion. That is now recorded in
  [rdma_publication_visibility_and_doorbells.md](../../../future-directions/citus/transport/rdma_publication_visibility_and_doorbells.md#observed-rdma-response-timeout-failure-mode), along with the current tail-observation fallback in
  [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2116).

## Related

- [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md): earlier wrapper-layer checkpoint that introduced the backend-facing session API.
- [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md): earlier local SHM control checkpoint below this cross-node step.
- [tuple_sink_data_plane_completion_plan.md](../../../future-directions/citus/data-movement/tuple_sink_data_plane_completion_plan.md): remaining future work after the now-landed tuple-sink vertical slice.
- [remote_execution_session_control_plane.md](../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md): longer-term backend-visible control-plane abstraction this implementation is advancing.
