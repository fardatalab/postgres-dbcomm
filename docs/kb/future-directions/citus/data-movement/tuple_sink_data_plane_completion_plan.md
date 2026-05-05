# Tuple-Sink Data-Plane Remaining Plan

## Scope

- **What this doc explains**: the remaining future-direction work after the first tuple-sink vertical slice has landed: speculative startup, richer backpressure/wakeup behavior, backend-visible failure surfacing, cross-transaction reuse, non tuple-sink generalization, and later wire-format redesign.
- **What this doc does NOT cover**: re-explaining the already-landed tuple-sink control plane, the already-landed first batching/backpressure checkpoint, or the already-landed borrow/use/release worker hot path.
- **Primary directory**: `docs/kb/future-directions/citus/data-movement/`
- **Doc type**: `future-direction`

## Current grounded state

The current implementation checkpoint is now [cross_node_tuple_sink_peer_control_checkpoint.md](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md).

That implementation note already covers the landed pieces that used to live in this plan:

- full [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:124) exchange on both local open and peer open
- service-side semantic receive decode in [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415)
- typed command/completion worker start through [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603) and [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:646)
- sender-side batching/backpressure checkpoint through [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2917), [`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2744), and [`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3109)
- worker-side borrow/use/release through [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3223) and [`ReleaseBorrowedRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3254)

So this note now focuses only on the parts that are still genuinely future.

## Remaining future work

### 1. Speculative startup and command-queue overlap

The main latency opportunity left is still startup overlap.

Today the sender still waits for startup synchronously:

- [`StartRemoteExecutionCommandThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1811) still treats command start as a blocking startup request
- [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1682) still spins the service until that command reaches `STARTED` or terminal completion
- the per-session command publication path is still one-slot-at-a-time in [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1603)

The next concrete design step should be:

1. replace the one-slot mailbox with a bounded FIFO command queue
2. preserve sequential worker dispatch order
3. split “enqueue accepted” from “started/completed”
4. let the sender enqueue `TX_BEGIN_ATTACH`, then enqueue `COPY_INGEST_FROM_TUPLE_SINK` behind it, then publish tuples behind both

One important correction remains explicit:

- speculative `COPY_INGEST_FROM_TUPLE_SINK` should mean “queued behind attach”
- it should **not** mean “executed before attach succeeds”

That dependency still exists in the worker dispatcher under [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:646).

### 2. Backpressure follow-up beyond the first checkpoint

The first batching/backpressure checkpoint is landed, but the more capable wakeup/scheduling work is still future.

Current state:

- low-level try/reserve status exists in [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2917)
- the first wait helper exists in [`WaitForRemoteSendBatchReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2744)
- default batching now lives under the sink/session layer through [`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3109)

What is still future:

- richer backend wakeups instead of sleep/backoff polling
- later network-aware scheduling decisions under that batching layer
- deciding whether manual explicit-batch callers should stay a niche surface or become a more widely used producer API

The first design rule should stay unchanged:

- batching policy lives under the sink/session layer
- but explicit batched reserve/submit remains available for callers that truly need exact control

### 3. Backend-visible receive-side transport failure

The worker still has no distinct receive-side “transport broken” terminal state.

What exists now:

- send-side blocked producers can stop through service-published terminal state in [`TupleSinkServiceMarkSendQueueTerminal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3625)
- receive-side backends only see EOS through [`CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_CLOSED`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:29), surfaced through [`CitusTupleSinkReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1499) and [`RemoteExecutionSessionReceivePeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:3178)

So a hard receive-side transport failure is still too service-local. The worker should eventually see a distinct terminal condition instead of only waiting for peer close.

### 4. Cross-transaction reuse and backend keepalive

The active implementation is still transaction-scoped.

Current grounded behavior:

- session objects are opened in [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034) under transaction lifetime
- the socketless backend exits on terminal tx commands in [`RemoteExecBackendExecuteTxCommitCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:568) and [`RemoteExecBackendExecuteTxAbortCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:601)

The longer-term control/data direction still wants something closer to Citus connection reuse:

- keep remote backend/process state alive across transactions when compatibility allows
- reattach a fresh transaction onto that backend later
- make session reuse mean more than “reopen a new backend under the same service-visible compatibility metadata”

This is still a control-plane plus backend-lifecycle problem, not a missing hot data-path feature.

### 5. Non tuple-sink `RemoteExecutionSession` modes

The session abstraction is still broader than the currently implemented path.

Current implementation is still rooted in the tuple-sink subset under [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2034), and the wrapper is still validated through the tuple-sink-specific open rules below [`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:379).

Still-future modes include:

- generic SQL command mode
- query result streaming
- replication-specific paths outside the current tuple-sink/COPY-ingest flow

### 6. Canonical serialization and intentional wire-vs-sink divergence

The current wire/data-plane format is still intentionally same-ABI oriented.

Current behavior:

- the full contract is still built from backend-local tuple metadata in [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:583)
- worker-side bind still decodes rows with native [`fetch_att()`](/data/dbcomm/postgres-citus/src/include/access/tupmacs.h:52) in [`DecodeTupleViewFromTupleSinkRow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1592)
- the receive service still enforces current equal-width wire-row and receive-row rebuild invariants in [`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2217) and [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2415)

So two future questions remain open:

1. do we want a canonical wire row format closer to PostgreSQL/Citus binary send paths?
2. if so, how far should the receive sink format diverge from that wire format, and where should the decode boundary live?

The currently preferred boundary is still:

- wire row -> backend-neutral tuple-view sink entry in the standalone service
- tuple-view sink entry -> PostgreSQL slot bind in the backend worker

### 7. Packing and alignment follow-up

The current row walk is correct but still conservative.

It already safely aligns row access for by-value loads in [`DecodeTupleViewFromTupleSinkRow()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1592), but the current format still uses conservative row packing rules rather than the leanest per-attribute alignment/density tradeoff.

So the remaining performance question here is no longer “do we need borrow/use/release first?” That is landed. The question is:

- should the current compact row body be repacked more tightly while preserving fast sequential decode and correct by-value alignment?

This should be treated as a follow-up density/performance pass, not a missing correctness feature.

## Immediate future code targets

- speculative startup and command-queue work:
  - [`remote_execution_session.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`command_dispatch_completion_plane.md`](command_dispatch_completion_plane.md)
- receive-side terminal failure surfacing:
  - [`tuple_sink_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)
  - [`tuple_sink_service.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`remote_execution_session.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- cross-transaction reuse and backend keepalive:
  - [`remote_execution_session.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`remote_execution_backend_bridge.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c)
  - [`postgres_local_command_dispatch_bridge.md`](postgres_local_command_dispatch_bridge.md)
- canonical serialization and wire/sink split:
  - [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)
  - [`tuple_sink_service.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c)
  - [`tuple_sink_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h)

## Deferred questions

- Should the next speculative-startup checkpoint keep the current one-backend-per-session rule while only changing the command queue, or should backend reuse work start at the same time?
- When receive-side transport failure becomes backend-visible, should that be a queue flag, a completion-plane terminal state, or both?
- If canonical serialization lands later, should the first canonical wire format track PostgreSQL/Citus binary COPY field framing or a different explicit tuple-view serializer?
- How aggressively should row packing move away from the current conservative same-ABI layout before we have measurements from the borrowed fast path?

## Related

- [cross_node_tuple_sink_peer_control_checkpoint.md](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md): current implementation checkpoint for the landed tuple-sink vertical slice.
- [command_dispatch_completion_plane.md](command_dispatch_completion_plane.md): remaining control/data future work above the current one-slot command mailbox.
- [postgres_local_command_dispatch_bridge.md](postgres_local_command_dispatch_bridge.md): longer-term PostgreSQL-side dispatcher/backend-pool design.
- [remote_execution_session_control_plane.md](../connection-management/remote_execution_session_control_plane.md): higher-level control-plane abstraction this remaining work still serves.
