# Intermediate results service: future directions

## Scope

- **What this doc explains**: exploratory designs for replacing file-backed intermediate results with a per-node service or other non-file interfaces, while preserving the semantics the current code depends on.
- **What this doc does NOT cover**: implementation details for a chosen design, or a commitment to any one approach.
- **Primary directory**: `docs/kb/future-directions/citus/intermediate-results/`

## Why this exists (context)

Current Citus intermediate results are file-backed and keyed by distributed transaction identity plus `resultId`. That choice is grounded in real cross-backend and cross-phase requirements, but it is not necessarily the only viable design. This doc captures the current constraints and the open design space we discussed, without committing to a specific replacement.

The canonical grounded note is [`why_file_backed_intermediate_results.md`](../../../citus/intermediate-results/why_file_backed_intermediate_results.md).

Transport/control-plane follow-up: [`rdma_transport_control_plane_abstraction.md`](../transport/rdma_transport_control_plane_abstraction.md) narrows the peer-transport versus route/stream abstraction question that Approach 5 depends on.

## Key code pointers

- [`IntermediateResultsDirectory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L720) - current per-node namespace derivation from distributed transaction identity.
- [`AssignDistributedTransactionId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L855) - coordinator-side creation of the distributed transaction identity.
- [`assign_distributed_transaction_id()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/backend_data.c#L145) - worker-side attachment to an existing distributed transaction identity.
- [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405) - producer-side COPY row serialization.
- [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100) - pull-side opaque byte fetch and append.
- [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447) - current filename-based read/parse path.
- [`DoLocalCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L202) - existing callback-driven COPY input example.
- [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274) - current off-path local-table-to-shard tuple routing, serialization, and COPY send hot path.
- [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4020) - current worker-side `COPY ... FROM STDIN` setup for off-path shard writes.
- [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738) - current libpq-based COPY data-plane enqueue plus flush-threshold logic.
- [`CheckForDistributedDeadlocks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/distributed_deadlock_detection.c#L105) - reminder that the distributed transaction identity is also used by other coordination subsystems.

## Current behavior / grounded findings

- Intermediate results are currently **per-node, cross-backend, distributed-transaction-scoped** objects. The storage namespace is shared by all participating backends on that node.
- The current data format contract is COPY-format bytes, not raw tuple memory.
- Readers typically consume results later than writers, often in a different backend and a later execution phase.
- Current code depends on `exists`, `size`, empty-result materialization, and cleanup-on-transaction-end semantics.
- Missing-file warnings due to concurrent failure/cleanup inside the same distributed transaction are already part of the behavior surface.

## Architecture / data flow

- **Current entry points**:
  - producers: [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405), [`worker_partition_query_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/partitioned_intermediate_results.c#L116)
  - transport: [`ReceiveQueryResultViaCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L623), [`fetch_intermediate_results()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L926)
  - consumers: [`read_intermediate_result()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L825), [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447)
- **Current storage contract**:
  - control plane: transaction-scoped namespace keyed by distributed transaction identity + `resultId`
  - data plane: append-only sequential COPY byte stream

## Invariants and assumptions

- One writer should own one `resultId` at a time.
- Many `resultId`s may coexist within one transaction namespace.
- Readers should only observe finalized results.
- A transaction failure can invalidate the whole namespace.
- Cleanup should happen from transaction-end hooks, not from normal readers.
- A redesign does not need cluster-global shared storage; per-node storage is enough, because cross-node movement is already explicit in current Citus.

## Future directions / design ideas

- **Status**: `exploratory`
- **Goal**: remove regular files from the steady-state intermediate-result contract, while preserving cross-backend lifetime, bounded memory, and later SQL consumption.
- **Grounding in current code**:
  - cross-backend namespace sharing is required by [`IntermediateResultsDirectory()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L720), [`ReadIntermediateResultsIntoFuncOutput()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L875), and [`RemoveIntermediateResultsDirectories()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L748)
  - byte-stream semantics are visible in [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405), [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100), and [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111)

### Approach 1: file-semantics-compatible per-node object service

- **Idea**: replace the backing store, but keep a file-like contract: named result objects, stat/exists/size, sequential reads, sequential appends, finalize, cleanup.
- **Pros**:
  - smallest change to current executor/planner assumptions
  - easiest to preserve `read_intermediate_result(...)` costing and fetch skip logic
  - easier migration path, because the API looks like today's file usage
- **Cons**:
  - retains more "filesystem-shaped" semantics than may be strictly necessary
  - may preserve abstraction baggage that only exists because the implementation used files first

Suggested API surface:

- `AttachTransaction(userId, initiatorNodeId, transactionNumber, timestamp)`
- `OpenWriter(resultId, options)`
- `Append(writer, bytes)`
- `Finish(writer)`
- `AbortWriter(writer)`
- `OpenReader(resultId)`
- `ReadChunk(reader, outBuf, maxLen)`
- `CloseReader(reader)`
- `StatResult(resultId)` returning `exists`, `state`, `size`
- `MarkTransactionFailed()`
- `CleanupTransaction()`

### Approach 2: callback-oriented COPY source/sink service

- **Idea**: keep the control plane, but make the data plane explicitly stream-oriented. Instead of "open file and read bytes", expose COPY-byte readers and writers that can be wired into PostgreSQL COPY callbacks.
- **Pros**:
  - cleaner match for the actual required behavior, which is mostly append + sequential read
  - avoids pretending that the object store is a filesystem
  - aligns with existing callback-driven parsing capability shown by [`DoLocalCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L202)
- **Cons**:
  - requires more executor integration work on the read side, because current intermediate-result consumption is filename-based
  - planner/costing paths still need metadata APIs if `size` remains important

Suggested API surface:

- control plane:
  - `AttachTransaction(...)`
  - `OpenWriter(resultId, options)`
  - `Finish(writer)`
  - `AbortWriter(writer)`
  - `GetResultMetadata(resultId)` returning `state`, `size`
  - `MarkTransactionFailed()`
  - `CleanupTransaction()`
- data plane:
  - `Append(writer, bytes)`
  - `OpenCopySource(resultId)`
  - `CopySourceRead(sourceHandle, outBuf, minRead, maxRead)`
  - `CloseCopySource(sourceHandle)`

This design removes file semantics from the read/write path, but not from the control plane. The namespace/result lifecycle rules still remain.

### Approach 3: hybrid RAM-first object service with spill

- **Idea**: keep either of the above APIs, but implement the backing store as RAM-first with transparent spill-to-disk.
- **Pros**:
  - most practical route if the goal is performance rather than semantic purity
  - can preserve current bounded-memory expectations
- **Cons**:
  - still needs spill management, object pinning, and cleanup complexity
  - does not remove the need for strong lifecycle management

This is likely the most realistic implementation shape even if the external API is changed.

### Approach 4: narrower direct-streaming optimization

- **Idea**: keep the general materialized-object contract, but optimize some producer/consumer pairs into direct streaming when they can be proven to run in one phase.
- **Pros**:
  - potentially highest performance on selected hot paths
  - avoids forcing the whole system onto one new abstraction
- **Cons**:
  - does not replace the general intermediate-result mechanism
  - complicated correctness boundary: most current users still need materialization

This approach is best seen as an optimization tier, not as a full replacement for the general design.

### Approach 5: tuple-oriented per-node service with shared-memory control plane and RDMA data plane

- **Idea**: move serialization/deserialization and network transport into a per-node external service. PostgreSQL/Citus backends talk to that service through shared-memory queues, while services on different nodes exchange data via RDMA.
- **Pros**:
  - best match for the research goal of separating tuple semantics from backend-local network/protocol code
  - can serve more than intermediate results: the same service boundary could also back off-path shard COPY, worker result streaming, or future tuple-shuffle paths
  - allows service-owned batching, RDMA-friendly memory registration, and centralized backpressure
  - matches the current off-path local-table distribution seam, because [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274) already deforms/routes tuples before handing them to COPY serialization
- **Cons**:
  - requires a new tuple-contract boundary; existing PostgreSQL/Citus code today speaks COPY bytes or FE/BE messages, not "logical tuples" to an external process
  - crossing into a separate process means some copy is still unavoidable unless the backend writes tuple payload directly into shared-memory pages/batches owned by the service API
  - much higher implementation and correctness burden than a file-like or COPY-callback service

Recommended API shape:

- backend-visible session/sink control plane:
  - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) with the session intent plus the intermediate-result operation spec
  - [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183)
- sender tuple plane:
  - [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208) returning a writable batch handle in shared memory
  - [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1235)
  - [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)
- receiver tuple plane:
  - [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290) returning a borrowed handle to service-owned tuple memory
  - [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1388)
  - [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1363) as an explicit alternative for callers that need longer-lived ownership rather than borrow/release

Recommended data-plane contract:

- not raw `HeapTuple`
- not COPY bytes
- a schema-versioned logical tuple/batch format owned by the service API:
  - null bitmap
  - fixed-width attribute region
  - varlena/indirect payload area
  - enough type metadata to reconstruct backend `Datum`s without reparsing COPY text/binary

Important refinement:

- the service should not be modeled as owning PostgreSQL heap/storage internals
- the backend/service boundary should stay at a deformed, DB-semantic tuple view:
  - backend inspects/deforms the slot
  - detoasts or normalizes values as needed for the chosen tuple contract
  - backend writes that tuple view into the shared-memory send batch exposed by the service
- this keeps the hard boundary between PostgreSQL storage internals and the networking service intact

Identifier refinement:

- the first backend-facing API should avoid exposing separate `peerId`, `streamId`, or exact sink id parameters on every operation
- instead, [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) should return an opaque session object that already owns the initial operation binding
- that backend-visible object stands for one logical data flow, such as:
  - one off-path shard-copy operation
  - one intermediate-result transfer
  - one worker-result flow
- the service may still keep separate peer-transport and internal exact sink identifiers under the hood
- if later we need more control or multiplexing, we can surface more explicit identifiers; they should not be part of the first backend API by default

Important constraint:

- if the service is a separate process, backends cannot hand it pointers into ordinary backend memory contexts and expect true zero-copy behavior
- the realistic zero-copy-ish design is:
  - sender backend deforms/copies tuple views into service-exposed shared-memory batch pages
  - local service RDMA-sends from those registered pages
  - remote service RDMA-receives into its own registered pages
  - receiver backend borrows a batch handle and reads directly from service-owned shared memory, then releases it

So the fast path should be **borrow/release**, not mandatory copy-out.

Control-plane implication:

- current Citus transport setup is connection-oriented and asynchronous:
  - [`StartNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L207) initiates a connection
  - [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L973) completes it with event-driven polling
  - connection establishment itself begins in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1353) using nonblocking libpq
  - polling/readiness transitions are driven by [`MultiConnectionStatePoll()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L806) and waitset construction in [`WaitEventSetFromMultiConnectionStates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L879)
  - intermediate-results push then begins the remote transaction and COPY command via [`PrepareIntermediateResultBroadcast()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L327)
- current off-path local-table distribution uses the same broad shape on the data path:
  - [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274) routes tuples to placements
  - [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4020) opens the worker-side sink
  - [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738) pushes COPY bytes onto the established transport
- an RDMA data plane should mirror that split rather than collapsing everything into one opaque "open QP and send" step:
  - service-internal transport cache keyed by remote peer identity
  - async peer/QP establishment with readiness/error states similar to the current `MultiConnection` lifecycle
  - backend-visible [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) / [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183) layered above that transport
  - higher-level transaction/control RPCs kept separate from the tuple data plane, at least in the first design
- the practical control-path layering for a first design is:
  - service-internal peer/QP lifecycle: "connect this node pair"
  - backend-visible route lifecycle: "open this logical tuple flow"
  - batch lifecycle: "reserve, submit, complete, release"

Recommended service-side queueing model:

- backend -> local service submission:
  - a single shared MPSC descriptor ring is acceptable for a first proof of concept
  - for a hot-path design, prefer one SPSC submission ring per backend and let the single service thread poll many rings; that avoids the cacheline contention and atomic pressure of a single global MPSC queue
  - in either variant, the ring should carry descriptors, not inline tuples
  - descriptors point to service-exposed shared-memory tuple batches that the backend filled
- service -> remote service:
  - per-peer transport/QP owns an SPSC ingress/egress ring for serialized wire payload
  - that wire ring is transport memory only; it should not double as backend-visible tuple memory
- local service -> backend receive side:
  - deserialize from the per-peer ingress ring into a separate receive-batch arena
  - publish batch-completion descriptors to the backend, rather than a FIFO of individual tuples
  - receiver backend peeks or borrows a batch, consumes tuples, then releases the whole batch
- optional API:
  - keep both borrow/release and copy-out semantics available on the receive side

Why batch descriptors are preferred over a plain tuple FIFO:

- lower metadata overhead
- easier borrow/release semantics
- cheaper reclamation
- better fit for RDMA sends/receives and batching
- still compatible with an optional copy-out API per tuple
- separating the transport ingress ring from the receive-batch arena avoids tying backend stall time to transport-ring head advancement

First-design implication for off-path shard COPY:

- sender-side hook point:
  - after routing in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2296), replace [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2422) / [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2450) with tuple-batch submission to the service
- receiver-side hook point:
  - do not deserialize back into COPY bytes and re-enter PostgreSQL COPY FROM
  - instead, add a tuple-batch consumer path parallel to [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4020) and PostgreSQL [`ReceiveCopyBegin()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L172)
  - the service should deliver already-deserialized tuple views; the worker-side consumer should turn those directly into insertable row state

## Proposed state machines

### Namespace state

Recommended user-visible states:

- `ACTIVE`
- `FAILED`

Recommended internal terminal states:

- `CLEANING`
- implicit "not found" after cleanup

The namespace is the per-node storage scope for one distributed transaction identity. In practice, normal callers should not observe a long-lived `DEAD` state; once cleanup completes, lookup should simply fail.

### Result state

Recommended user-visible states:

- `WRITING`
- `READY`
- failure

Recommended internal terminal states:

- `ABORTED`
- `DELETED`

Required invariants:

- exactly one writer owns a given `resultId`
- readers only open `READY`
- `READY` may have zero length
- namespace failure invalidates all non-terminal results

## Risks / tradeoffs

- A metadata registry alone is insufficient; the payload store and transaction-lifecycle rules are the hard parts.
- If planner costing continues to depend on result size, the redesign still needs cheap `size` visibility.
- If transaction-end cleanup becomes weaker than today's directory cleanup, stale objects will accumulate or wrong readers may succeed.
- If failure invalidation semantics change, current warning/error behavior around concurrent failures may change in subtle ways.
- A callback-oriented API simplifies parsing integration conceptually, but it is not automatically a smaller patch than preserving file semantics.
- A tuple-oriented external service only pays off if the tuple contract is stable enough and the backend/service shared-memory ownership model is designed carefully from the beginning.

## Next experiments / implementation steps

- Define the minimal control-plane contract first: transaction attach/fail/cleanup, result ownership, result metadata, finalize visibility.
- Decide whether the read-side data plane should remain file-like or become callback-oriented.
- Prototype a small in-process mock object service behind one read path, likely the consumer side of [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447).
- Measure how much the planner and fetch paths really rely on `size` and `exists`, and whether that should be preserved or redesigned.
- Keep spill out of the first prototype only if the prototype is explicitly bounded and non-production; otherwise spill is required from the start.
- If the long-term direction is RDMA + tuple service, prototype the tuple contract and shared-memory ownership model before committing to wire protocol details.
- For COPY-based workflows, identify the exact hook where COPY byte production/consumption would be replaced by tuple-service submit/poll APIs.

## Open questions / TODO

- Should the first redesign target consumer-side parsing (`ReadFileIntoTupleStore`) or producer-side storage first?
- Should `size` remain part of the object-service contract, or should planner/costing be redesigned alongside storage?
- Do we want to preserve current missing-file warning behavior exactly, or replace it with explicit namespace/result failure states?
- Is a file-semantics-compatible API a migration tool only, or a desired long-term abstraction?
