# Off-path local-table distribution: first RDMA tuple-service prototype

## Scope

- **What this doc explains**: a concrete first-prototype design and implementation plan for replacing the current COPY byte path in off-path local-table distribution with an external tuple service.
- **What this doc does NOT cover**: a general replacement for all Citus connection management, a shared-transport/native-demux production design, or a DPU-offloaded implementation.
- **Primary directory**: `docs/kb/future-directions/citus/data-movement/`

## Why this exists (context)

The current off-path local-table distribution path deforms tuples, serializes them into COPY bytes, sends them via libpq COPY, and reparses them on the worker side. The grounded current path is documented in [`off_path_local_table_distribution_buffering_and_copy_costs.md`](../../../citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md).

Current implementation progress is tracked separately in [`local_batch_materialization_checkpoint.md`](../../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md). That implementation note is the canonical source for what has actually landed in code so far.

Current status note:

- the newer tuple-sink control-plane work is now complete enough that the canonical next-step data-plane plan lives in [tuple_sink_data_plane_completion_plan.md](tuple_sink_data_plane_completion_plan.md)
- this older note still explains the broader prototype rationale and many useful data-path constraints, but parts of its earlier control/bootstrap discussion are now historical

Historical naming note:

- this design note still says `tuple-route` in several places because it was written against the earlier checkpoint vocabulary
- the active code and current control-plane notes have since renamed the substrate to `tuple_sink_*`
- where this note still says `OpenRoute(...)`, `routeSpec`, or `routeHandle`, read that as historical shorthand for the newer [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) plus exact tuple-sink operation binding

The latest landed checkpoint moved beyond pure materialize-and-discard:

- backend handles now map shared-memory route queues through a registry
- a standalone local `citus_tuple_sink_service` process mirrors send-queue slots into receive-queue slots
- the sender path already exercises `reserve -> append -> submit -> service-forward -> poll -> copyout -> release`
- transport is still local loopback plus the original COPY byte path, so this is still a scaffolding checkpoint rather than the final data plane

One correction to the earlier design discussion: the current implementation introduced a well-known shared-memory route registry as the rendezvous seam between backends and the standalone local service. That registry was not part of the original target architecture; it is a bootstrap mechanism for the current checkpoint and should not automatically be treated as the final control-plane design.

For a first prototype, we want to:

- target only the tuple data plane
- leave current libpq/SQL control plane, remote transaction state, and placement safety in place
- avoid direct RDMA exposure to PostgreSQL/Citus backends
- avoid multiplexing entirely
- use deformed tuple views as the backend/service object contract
- use single-threaded external services and FIFO queues where the stricter assumptions make them safe
- treat any worker-side SQL/bootstrap seam as temporary transactional orchestration scaffolding, not as the preferred external-service control path

## Big-picture service contract

The external service has two distinct jobs in this design.

### 1. Backend-facing tuple service

To PostgreSQL/Citus, the service should look like a local tuple I/O interface:

- sender backend submits tuple views through [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208), [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1235), and [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)
- receiver backend obtains tuple views through [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290), [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1388), and optionally [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1363)

The intended sender-side feel is "fire and forget" at the tuple-payload boundary:

- once the sender backend has successfully published a tuple-view batch into the local queue, it does not participate in serialization or network transmission for that batch anymore

Important correction:

- end-to-end success is still **not** fire-and-forget at the transaction/control level
- completion and failure still come back through the existing worker-side control command and libpq control plane described around [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)
- that worker-side control command is still distinct from the intended **service-to-service** control path, which should use a dedicated RDMA control queue pair

### 2. Service-internal RDMA transport

Under the hood, the service serializes tuple views, moves serialized payload over RDMA, and deserializes on the receiving node:

- sender service reads tuple views from the local sender tuple queue
- sender service serializes them into the RDMA transport ring
- sender service RDMA-writes serialized payload to the remote transport ring
- receiver service observes transport publication, then deserializes into the local receiver tuple queue
- receiver backend reads tuple views only from that local receiver tuple queue

So the first prototype has two different queueing objects with two different roles:

- local tuple queues:
  - hold deformed tuple views
  - are the backend/service contract
- RDMA transport ring:
  - holds serialized payload on the wire between services
  - is invisible to PostgreSQL/Citus backends

## Current implementation seam: route registry

The current landed checkpoint uses one well-known shared-memory route registry:

- registry shm name [`CITUS_TUPLE_ROUTE_REGISTRY_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L31)
- registry header [`CitusTupleRouteRegistryHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L84)
- registry entry [`CitusTupleRouteRegistryEntry`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L93)

Its current responsibilities are deliberately narrow:

- rendezvous between backend route handles and the standalone service
- discovery of per-route send/receive queue shm names
- publication of queue geometry such as [`slotCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L102) and [`slotCapacityBytes`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L103)
- lightweight liveness/accounting via [`sendHandleCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L105), [`receiveHandleCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L106), and [`serviceAttached`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L107)

It does **not** currently mean:

- a final backend-visible route-id API
- the future service-to-service RDMA control path
- SQL/session/transaction control ownership
- tuple payload storage itself

So for this prototype note, the right interpretation is:

- route registry: current bootstrap catalog
- route handle: backend-facing opaque local service handle
- future control path: still expected to move to explicit service-owned control messaging rather than long-term registry polling

## Corrections / design boundaries

- We should reuse the **shape** of current connection-management lifecycle, not attach RDMA QPs directly to backend [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:162).
- We should keep current SQL/libpq control-plane responsibilities intact for this prototype:
  - connection/session caching via [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277)
  - remote transaction begin/failure semantics via [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859) and [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1018)
  - placement/co-location safety via [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281)
- We are replacing only the COPY **tuple data plane** currently driven by [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:1467), [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:1119), and worker-side [`ReceiveCopyBegin()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:172) / [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:862).

## Current hook points to replace

### Sender side

- entry: [`CopyFromLocalTableIntoDistTable()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2687)
- scan loop: [`DoCopyFromLocalTableIntoShards()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2790)
- hot routing path: [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274)
- current remote sink open: [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020)

### Receiver side

- current worker-side byte parser start: [`ReceiveCopyBegin()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:172)
- current worker-side parse loop: [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:862)
- current insert half inside [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:948)

## Chosen first-prototype decisions

### 1. Keep current SQL/libpq control plane

Use existing Citus connection, placement, and transaction machinery unchanged to:

- choose the target placement/worker connection
- ensure remote transaction state is correct
- bootstrap a worker-side receiver context if needed
- wait for final completion/error

Important correction:

- this does **not** mean the preferred design is "SQL/UDF for control"
- commands between the external services should instead use a dedicated RDMA control queue pair
- a worker-side SQL/UDF hook is acceptable only as a temporary bring-up seam while the service-owned control path does not exist yet

### 2. Replace only the tuple data plane

Do **not** send tuple bytes through [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738). Instead:

- sender backend deforms a slot and writes a normalized tuple-view batch into a local shared-memory FIFO queue consumed by the sender-side external service
- sender-side service transmits that batch over RDMA
- receiver-side service lands the batch into a shared-memory FIFO queue visible to the worker backend
- worker backend reads tuple views from that FIFO and inserts them directly, without PostgreSQL COPY parsing

### 3. No multiplexing

For this first prototype:

- one active route gets one dedicated transport path
- no native route demux
- no compatibility-mode active-route switching either

This corresponds to **Option 0** in [`rdma_transport_control_plane_abstraction.md`](../transport/rdma_transport_control_plane_abstraction.md).

### 4. FIFO queues are acceptable in this prototype

Because we are deliberately avoiding multiplexing and route sharing:

- sender local queue can be a plain SPSC FIFO shared between backend and local service
- receiver local queue can also be a plain SPSC FIFO shared between local service and backend
- the remote receive queue can double as the RDMA landing area if its memory is shared between the receiver backend and the receiver service and registered for RDMA

That is precisely the narrower case where queue storage and data storage can collapse into the same bounded circular buffer.

Make the prototype assumption explicit:

- use `SPSC` for all three hot-path queues in the first prototype
  - sender backend -> sender service tuple queue
  - sender service -> receiver service RDMA transport ring
  - receiver service -> worker backend tuple queue
- later, if one service thread must ingest from many backends or publish to many local consumers, revisit `MPSC` or more general queueing schemes

### 5. RDMA fast path uses one-sided WRITE-based publication

Correction to the generic "use read/write verbs" statement:

- yes, use one-sided verbs rather than send/recv for the hot path
- but for the steady-state ring protocol, the first prototype should prefer **RDMA WRITE** in both directions
- RDMA READ should be optional, not required on the hot path

Reason:

- sender-to-receiver payload publication is naturally WRITE-based
- receiver-to-sender head update is also naturally WRITE-based
- RDMA READ adds request/response latency and is not obviously needed if the consumed head is pushed back

Control/data channel clarification:

- this WRITE-based recommendation is for the **data-plane payload ring**
- the service should still keep lifecycle/error/control traffic on a separate control channel/ring so it is not delayed behind bulk tuple payload
- both channels should still converge on the same peer RDMA implementation substrate rather than growing two unrelated transport stacks

## Concrete backend-facing API for this prototype

The current backend-facing shape is now session-centric:

- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099)
- [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183)
- [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208)
- [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1235)
- [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)
- [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290)
- [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1388)
- [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1363)

The exact tuple-sink identity remains service-owned under that API. The backend still never sees a peer/QP handle or a backend-visible transport object.

## Control-plane contract around the new tuple path

This prototype deliberately keeps the existing Citus/libpq SQL control plane in charge of remote transaction/session semantics. The new session/tuple-sink API is only the local backend-to-service contract.

That means there are two distinct control paths to keep separate in the design:

- backend-visible bootstrap/control for worker transaction context, which may temporarily reuse the existing [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) / [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683) path during bring-up
- service-to-service tuple-sink control, which should ultimately use its own RDMA control queue pair and should not prefer SQL/UDF traffic

Recommended first-prototype shape:

- sender backend computes a deterministic `routeSpec`
- sender backend may temporarily send a bootstrap remote control command on the claimed [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:162)
- worker backend enters a blocking route-consume-and-insert loop for that `routeSpec`
- sender backend opens its local service route and starts submitting tuple batches
- sender backend closes the local route when done
- worker backend exits the route loop only after the route reaches end-of-stream and all inserts are durable inside the current SQL transaction
- sender backend then receives the final libpq command result, just as current COPY waits for end-of-copy completion

Important correction:

- the backend should not treat the external service as a replacement for remote SQL transaction state
- the SQL command still defines the transactional sink lifetime
- the route only carries tuple payload for that already-established sink lifetime
- if a SQL/UDF bootstrap seam exists in the code, it should be treated as scaffolding rather than the preferred control-plane interface
- likewise, the current shared-memory route registry should be treated as bootstrap rendezvous scaffolding, not as the preferred final route-open protocol

Transport refinement from the control-plane work:

- the steady-state service-to-service design should use at least two logical RDMA channels:
  - one control ring/channel for session/sink lifecycle
  - one tuple-payload data ring/channel for serialized tuple batches
- both channels may share one peer transport substrate, but they should not be collapsed into one ring because that would reintroduce control-vs-payload head-of-line blocking

## Route specification for this prototype

`routeSpec` should be deterministic and computable on both ends from control-plane state, so the sender does not need a returned route token before the worker enters its receive loop.

Suggested fields:

- distributed transaction identity
- sender backend identity (for example PID or backend-local unique sequence)
- destination node identity
- target shard id / placement identity
- target relation identity
- route ordinal within the command if needed

This lets:

- sender backend construct the route spec before sending the worker-side control command
- worker backend open the matching receive route locally
- sender-side service and receiver-side service bind the same route without a backend-visible route-id exchange

For the first prototype, the `routeSpec` should stay close to existing off-path shard COPY routing decisions rather than becoming a generic global transport namespace. A practical initial encoding is:

- distributed transaction identity already established by Citus
- destination node identity
- shard id and placement identity chosen by [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3743)
- target relation OID
- sender backend PID

That keeps the route tied to the exact control-plane decision already made by the backend and avoids introducing an additional planner-visible routing abstraction too early.

Current implementation note:

- the current bring-up code derives destination route identity from [`LookupNodeForGroup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_cache.c:1300), i.e. a concrete `nodeId`
- this is preferable to using `groupId` directly
- `groupId` identifies a placement group, while `nodeId` is the concrete destination-node identity that better matches a route
- hostname/port are transport details and should stay below the backend/service route contract where possible
- the current shared-memory route registry uses that deterministic route spec only to rendezvous local queue endpoints with the standalone service; a future explicit control protocol may reuse the same route spec while replacing the registry polling mechanism

## Tuple-view batch layout

For the first prototype, assume homogeneous nodes and use one normalized in-memory/wire layout.

Implementation-progress note:

- the current code already chose a more concrete slot-local tuple format than this design note originally spelled out
- see [`local_batch_materialization_checkpoint.md`](../../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md) for the exact current layout and semantics
- the design below remains the target model, but the implementation note is now canonical for the landed byte/layout choices

### Route-level schema state

When opening a route, derive and cache a schema descriptor from the sender [`TupleDesc`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2276) and relation metadata:

- attribute count
- type OIDs
- byval/byref information
- fixed-length information
- alignment requirements

### Batch slot layout

Each queue slot should hold one bounded batch.

The current implemented shape is:

- `SlotHeader`
- `BatchHeader`
- repeated tuple records:
  - `TupleHeader`
  - heap-style non-null bitmap
  - fixed-width per-attribute entries carrying `valueOffset`, `valueBytes`, and flags
  - one maxaligned value region

That implemented shape is still consistent with the design goal here:

- fixed-width metadata up front
- no backend memory pointers crossing the service boundary
- copied/detoasted payload bytes in one slot-local region

### Value normalization rules

- by-value fixed-width types: store inline in normalized fixed-width fields
- fixed-length by-reference types: copy bytes inline or into a fixed-width field if convenient
- varlena/by-reference values: detoast and copy bytes into the batch payload region
- dropped/generated-stored attributes: in the current implementation, keep them explicit in the tuple-view metadata with omission/null flags rather than filtering them out of the layout entirely
- no backend memory pointers cross the service boundary

Prototype simplification:

- use host endianness
- assume same architecture on both nodes
- choose a fixed **preallocated per-route slot capacity** when the route opens
- if the queue is temporarily full, block and wait rather than falling back to the socket/COPY path
- add a debug log on queue-full blocking so backpressure is visible during bring-up
- if a single tuple cannot fit in one fixed slot at all, treat that as an explicit prototype limitation and error out rather than silently activating a second transport path

Important correction:

- schema knowledge alone does **not** give a safe exact upper bound for all tuple sizes, because varlena attributes can still grow arbitrarily
- for fixed-width-only schemas, the service can compute an exact tuple-view bound
- for general schemas, the first prototype should use a configured fixed slot capacity per route, not pretend the schema determines an exact maximum
- a practical first setting is a coarse capacity such as 1 KB per slot, but that is a policy choice, not something implied by the schema

## Queue and memory ownership model

Because this prototype intentionally avoids multiplexing and shared transport, the queue storage and the tuple-batch storage can be the same bounded circular buffer on both the sender-local and receiver-local sides.

Terminology for this note:

- **tuple queue**:
  - the shared-memory FIFO whose slots contain tuple-view batches
  - consumed locally by backend and local service
- **RDMA data region**:
  - the registered memory that carries serialized payload written by the remote service with one-sided verbs
  - consumed by the local receiver-side service, not directly by the backend
- **control block**:
  - the registered memory that holds published tail / consumed head / route flags

So for the first prototype, keep the **tuple queue** and the **RDMA data region** logically separate:

- RDMA moves serialized payload between services
- the receiver-side service deserializes that payload into the local tuple queue
- the worker backend consumes only the local tuple queue

This matches the intended boundary that the external service owns serialization/deserialization.

Recommended local queue shape:

- one SPSC queue between sender backend and sender service for outbound tuple batches
- one SPSC queue between receiver service and worker backend for inbound tuple batches
- fixed-size batch slots inside each queue
- one slot contains one complete tuple batch plus a small slot header

Recommended slot state model:

- `FREE`
- `RESERVED_BY_BACKEND`
- `READY_FOR_SERVICE`
- `INFLIGHT_ON_WIRE`
- `READY_FOR_BACKEND`
- back to `FREE` after consumer release

The implementation does not need an explicit enum in the hot path if it uses monotonically increasing publish/consume sequence numbers, but the above state model is the intended ownership contract.

Why a single FIFO is acceptable here:

- one producer and one consumer per queue
- strict FIFO consumption
- no route sharing
- no need to hold one batch while consuming a newer batch from the same queue

If any of those assumptions change later, this should evolve into the batch-slot-pool plus descriptor-rings design captured in [`rdma_transport_control_plane_abstraction.md`](../transport/rdma_transport_control_plane_abstraction.md).

Recommended RDMA transport shape:

- one SPSC serialized-payload ring per active route/QP
- receiver-side service polls or is notified for newly published transport slots
- receiver-side service deserializes from that transport ring into the local tuple queue

This deserialize step is part of the intended first-prototype flow and is not an optional future cleanup:

- the backend should never consume serialized RDMA transport slots directly
- the receiver-side service owns the transport ring and the deserialization step
- the backend only consumes tuple views from the local tuple queue

## Deferred routing/state offload

For the first prototype, keep `shardId` and `CopyShardState` management in the backend exactly where they are today:

- [`ShardIdForTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2296) still computes routing from the deformed tuple
- [`GetShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3543) and [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3568) still manage placement lists, local-placement flags, connection assignment, and local COPY/file state

Future direction:

- offloading `shardId` computation is plausible once the service owns enough partitioning metadata and hashing/range logic
- offloading full [`CopyShardState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:209) is a later step, because that structure is not only routing metadata; it also carries placement/control-path state such as [`placementStateList`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:223), [`containsLocalPlacement`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:220), and local COPY/file state
- in other words, `shardId` offload is a data-plane-adjacent optimization, while full `shardState` offload starts to merge control-plane responsibilities into the service

## Detailed end-to-end flow

### 1. Sender-side placement/control setup stays in Citus

Sender continues to use:

- [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3743)
- [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281)
- [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1018)
- [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1293) where current COPY already does so

### 2. Replace remote `COPY FROM STDIN` open with a worker-side route consumer command

Current code uses [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020) to send `COPY shard_X FROM STDIN`.

Prototype replacement:

- introduce a new control-path command, conceptually `StartPlacementStateTupleRouteCommand(...)`
- it still uses existing [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) on the current `MultiConnection`
- instead of opening COPY IN, it starts a worker-side command that:
  - opens the matching receive route with the local external service
  - blocks in a tuple-batch receive/insert loop
  - returns only after the route closes and all tuples are inserted

Important point:

- the libpq connection remains busy exactly as it does during current COPY IN
- but tuple bytes do not travel through libpq; only control and final completion do
- this is intentionally not wired into backend `MultiConnection` internals beyond the existing command lifecycle
- QP establishment/retry/readiness remains inside the service, modeled after the shape of [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1353), [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:973), and [`RestartConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1583)

Important correction:

- the worker-side "start consuming tuples for this shard/relation" command should still stay on the existing SQL/libpq control plane in the first prototype
- moving that particular command onto a service-to-service RDMA control path would start to reimplement remote backend command execution and transaction semantics inside the service, which this prototype is explicitly avoiding

### 3. Sender backend opens the local sender session/sink

After sending the worker-side control command, the sender backend opens the local sender-side [`RemoteExecutionSession`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:149) through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) with the exact tuple-sink operation spec for that flow.

No backend-visible remote token is required because the service-owned control plane resolves peer/session/sink state internally from the session intent plus operation spec.

### 4. Sender hot path: slot -> tuple-view batch

In [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274):

- keep [`slot_getallattrs(slot)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2291)
- keep [`ShardIdForTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2296)
- replace [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2422) / [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2450) with:
  - [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208)
  - tuple normalization into the reserved FIFO slot
  - [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)

Because there is no multiplexing, the prototype should also avoid [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:164) buffering/switch-over logic on this path.

### 5. Sender-side service: local FIFO -> RDMA WRITE publication

The sender-side external service thread:

- polls the local backend-to-service FIFO
- serializes tuple-view batches into the next RDMA transport slot
- chooses the next free remote transport slot according to the mirrored remote consumed head
- posts one or more unsignaled RDMA WRITEs for the serialized payload bytes
- posts a final publication operation for the remote transport tail/control word

Suggested publication rule:

- payload first
- publication word or published-tail update last

Suggested completion rule:

- only the final publication/control write must generate a CQE in the hot path
- payload writes remain unsignaled

### 6. Receiver-side service/backend: transport ring to local tuple queue

In this first prototype, the registered RDMA target memory on the receiver is a **serialized transport ring**, not the backend-visible tuple queue.

That means:

- sender-side RDMA WRITEs land in receiver-side transport-ring slots
- receiver-side service is still needed to:
  - own the QP and its lifecycle
  - observe remote publication
  - deserialize transport-ring payload into the local tuple queue
  - publish local tuple-queue readiness for the worker backend
  - propagate transport-ring head updates back to sender
- receiver backend polls only the local tuple queue through [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290)

This is the main place where the "no multiplexing" choice materially simplifies the design:

- we do not need route demultiplexing inside the receiver service
- the worker backend can safely assume FIFO ownership and release order on the tuple queue
- both the transport ring and the tuple queue can be SPSC

### 7. Receiver insert loop: tuple view -> slot -> existing insert semantics

Current worker path parses bytes through [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:862), then runs the insert half in [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:948).

Prototype replacement:

- new worker-side loop, conceptually `RouteCopyIntoRelation(...)`
- poll tuple-view batches from the local service
- materialize `myslot->tts_values` / `myslot->tts_isnull`
- call the same or equivalent insert/trigger/constraint/index path now living in [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1000)

## Receiver implementation options

### Option A: minimal PostgreSQL-fork refactor to extract a reusable insert helper

- extract the post-`NextCopyFrom()` insert logic from [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1000)
- keep PostgreSQL COPY parsing intact
- let both byte-parsing COPY and tuple-route receive feed the same insert helper once `myslot->tts_values` / `myslot->tts_isnull` are populated

Pros:

- best semantic reuse
- lower long-term divergence
- more realistic than a pure Citus-side implementation, because helpers such as [`CopyMultiInsertInfoStore()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:607) and the surrounding state inside [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:630) are currently private to `copyfrom.c`

Cons:

- larger up-front surgery in PostgreSQL COPY code
- requires touching the PostgreSQL fork, not only the Citus extension

### Option B: duplicate the insertion half in a prototype worker route loop

- create a new route receiver implementation that mirrors the insertion logic from [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1000)

Pros:

- faster first prototype
- isolates prototype risk

Cons:

- code duplication
- future drift risk
- in practice, more invasive than it sounds because `copyfrom.c` keeps critical helpers and bookkeeping private

Recommendation:

- prefer **Option A** for the first serious prototype
- if a throwaway bring-up path is needed, start with a very narrow Option B, but treat it as disposable scaffolding

Why the Citus-side duplicate is worse than it first sounds:

- the post-parse insert path in [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:630) is tightly coupled to local variables such as `insertMethod`, `multiInsertInfo`, `proute`, `bistate`, trigger flags, and `target_resultRelInfo`
- batching helpers like [`CopyMultiInsertInfoNextFreeSlot()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:586) and [`CopyMultiInsertInfoStore()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:607) are private to `copyfrom.c`
- the same loop also owns error/progress/reporting state such as `processed`, `excluded`, `skipped`, and callback setup for [`CopyFromErrorCallback`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:933)
- duplicating that in Citus would either copy a large amount of forked PostgreSQL logic or force ad hoc re-exports of many core internals

That is why the note recommends a small PostgreSQL-fork refactor instead of treating the worker side as a clean extension-only integration.

## RDMA queue/control protocol

### Transport choice

- reliable connected QP
- one dedicated QP per active route or backend flow
- no shared transport multiplexing
- SPSC assumptions end to end for the first prototype

### Remote memory layout per route

- remote data FIFO:
  - bounded circular queue of batch slots
  - registered and shared with receiver backend
- remote control block:
  - published tail / sequence
  - consumed head state
  - route state flags (open/closing/closed/error)

### Publication ordering

Use one-sided WRITEs with responder-visible doorbelled publication:

1. ordinary RDMA WRITEs for payload bytes into slot
2. final `WRITE_WITH_IMM` publish step for transport-ring sequence / tail

The receiver-side service should treat the responder-visible CQ event as the
publish notification, and only then read the memory-resident published
sequence/tail state.

### `WRITE_WITH_IMM` variant

`WRITE_WITH_IMM` is a plausible notification variant, but it should be treated carefully.

What it is good for:

- notifying the receiver-side service through a CQE that a batch publication happened
- avoiding a pure polling loop in the receiver-side service if we want event-driven wakeups

What it does **not** solve by itself:

- the worker backend still cannot observe the remote CQE directly
- if the worker backend is meant to read the shared-memory FIFO directly, there still needs to be a locally visible ready/published state in memory
- an ordinary RDMA WRITE of the tail/control word does **not** by itself generate a remote CQE
- only `WRITE_WITH_IMM` (or SEND-style operations) provides that kind of receiver-side completion event

Recommendation for the first prototype:

- use `WRITE_WITH_IMM` for the final transport-slot publication if we want an explicit receiver-service CQE wakeup
- still keep a published sequence/tail in the transport control block as the canonical transport readiness contract
- the receiver-side service, after handling the CQE and validating the published transport state, deserializes and then updates the **local tuple-queue** tail for the backend
- do not make the remote CQE the only publication state, because the backend still needs a locally visible tuple-queue readiness signal

### Head return

After the worker backend releases a batch:

- it advances the local consumed head in shared memory
- receiver-side service observes that advancement
- receiver-side service issues a WRITE back to the sender-side control block

So the steady-state fast path can remain WRITE-based in both directions. RDMA READ is optional for bring-up, debugging, or recovery paths.

Recommended first-prototype head rule:

- receiver backend advances local consumed head after `ReleaseReceivedBatch(...)`
- receiver-side service mirrors that head back to the sender via RDMA WRITE
- sender-side service treats returned head state as the steady-state free-space signal

That avoids RDMA READ on the hot path entirely.

## External service responsibilities

### Sender-side service

- own the sender QP and registered remote metadata
- poll local sender FIFO
- publish batches over RDMA
- observe returned head state
- handle route close / error transition

### Receiver-side service

- own the receiver QP lifecycle
- expose registered receive FIFO/control blocks
- mark local batch publication readiness if needed
- observe backend-consumed head and send it back
- handle route close / error transition

### Reused lifecycle shape

Inside the service, model peer/QP lifecycle after the current Citus connection-management shape:

- initialize cache entry
- asynchronous connect
- ready/failure states
- restart/close on error or shutdown

This mirrors the shape of:

- [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1353)
- [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:973)
- [`RestartConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1583)

without inserting QP state into backend `MultiConnection`.

## Concrete implementation plan

### Phase 1: backend/service control skeleton

1. Add a local service abstraction for:
   - `OpenRoute`
   - `CloseRoute`
   - `ReserveSendBatch`
   - `SubmitSendBatch`
   - `PollReceivedBatch`
   - `ReleaseReceivedBatch`
2. Define the concrete backend-facing header/API for that abstraction, not just the conceptual operation names.
3. Add deterministic `routeSpec` generation on the sender side.
4. Add a worker-side SQL command that starts the route receive loop instead of `COPY FROM STDIN`.
5. Keep the existing libpq remote command lifecycle intact so route setup failure still reports through current Citus error paths.

Control-path split for this phase:

- backend <-> local service:
  - concrete C header/API using the route and batch handles above
- service <-> service:
  - dedicated RDMA control protocol for small route-management messages is reasonable
  - a dedicated control QP per backend is the safer starting point than a shared node-wide control transport
- service <-> service data path:
  - one dedicated data QP per active route/backend flow is the simplest first design
- backend <-> remote backend:
  - keep using existing SQL/libpq command execution for the transactional worker-side receive command

### Phase 2: sender integration

1. Add an experimental route-enabled branch in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274).
2. Keep current placement routing and connection claiming.
3. Replace COPY serialization/send with tuple normalization into the local sender FIFO.
4. Keep current COPY path only as a coarse feature-flag fallback, not as a runtime "queue full" escape hatch.
5. Bypass the current shared-connection buffering logic in [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:164) for the experimental route-enabled path, because the prototype explicitly does not multiplex.

### Phase 3: worker receiver integration

1. Add worker-side route receive loop.
2. Implement tuple-view -> `Datum[]` / `isnull[]` reconstruction.
3. Prefer a small PostgreSQL-fork refactor that extracts the insert half of [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1000) into a reusable helper so triggers, constraints, partition routing, and indexes remain correct.
4. Keep the old COPY receive path unchanged as fallback.

### Phase 4: external service RDMA data path

1. Implement one dedicated route/QP pair.
2. Implement sender FIFO polling and RDMA WRITE publication.
3. Implement receiver-side head return.
4. If the queue is full, block and wait with a debug log rather than activating a second runtime data path.
5. Add route close/error handling; keep the old COPY path as a separate mode, not as an automatic in-band fallback.

### Phase 4a: file-level change map

Expected primary touch points:

- Citus sender/control path:
  - [`create_distributed_table.c`](../../../../../citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c)
  - [`multi_copy.c`](../../../../../citus-dbcomm/src/backend/distributed/commands/multi_copy.c)
  - [`remote_commands.c`](../../../../../citus-dbcomm/src/backend/distributed/connection/remote_commands.c) only for the new worker-side control command shape, not for tuple payload
- PostgreSQL fork worker insert path:
  - [`copyfrom.c`](../../../../../src/backend/commands/copyfrom.c)
  - possibly a new helper declaration in an exported header if the insert helper is shared with Citus code
- local service shim and shared-memory queue code:
  - likely new Citus-side source files rather than further bloating [`multi_copy.c`](../../../../../citus-dbcomm/src/backend/distributed/commands/multi_copy.c)
  - one backend-facing client wrapper
  - one service/main-loop implementation
  - one shared queue layout header used by both sides

The exact filenames can change, but the split above is the intended ownership boundary.

### Phase 5: instrumentation and validation

1. Add timing around:
   - route open
   - tuple normalization into local FIFO
   - service dequeue / RDMA publish
   - worker batch poll
   - tuple-view reconstruction
   - insert loop
2. Compare against current timing around:
   - [`CitusSendTupleToPlacements_`](/data/dbcomm/postgres-citus/src/include/timing_spots.h:87)
   - [`SerializeAndCopyRow_`](/data/dbcomm/postgres-citus/src/include/timing_spots.h:85)
   - [`SendCopyDataToPlacement_`](/data/dbcomm/postgres-citus/src/include/timing_spots.h:86)
   - [`NextCopyFrom_`](/data/dbcomm/postgres-citus/src/include/timing_spots.h:45)
   - [`CopyFromInsertIntoTable`](/data/dbcomm/postgres-citus/src/include/timing_spots.h:46)

## Major risks / caveats

- If route-spec derivation is not deterministic and collision-free enough, sender and receiver may bind the wrong route.
- If the worker-side insert loop diverges too far from [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:948), correctness differences will appear around triggers, defaults, generated columns, partitioning, or indexes.
- Fixed batch-slot sizing may reject oversized tuples; the fallback path must be explicit.
- A dedicated-QP/no-multiplexing prototype may use more transport state than we will ultimately want.
- RDMA publication correctness depends on strict ordering between payload writes and publish-word writes; the implementation must document and test those assumptions carefully.
- If the worker-side control command lifetime and the service-side route lifetime get out of sync, the remote SQL transaction can remain blocked on a route that the sender thinks is already finished.

## Next future directions after the prototype

- move from dedicated-QP/no-multiplexing to shared transport plus native route demux
- replace FIFO receive queues with bounded batch-slot pools if multiplexing is introduced
- consider offloading [`ShardIdForTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2296) once the service owns enough routing metadata
- revisit whether parts of [`CopyShardState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:209), [`GetShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3543), and [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3568) should migrate into the service when more control-plane responsibilities move
- revisit whether more of Citus connection/session/transaction lifecycle should migrate into the external service
- treat `WRITE_WITH_IMM` as the responder-visible publication step, not merely as an optional notification optimization
- later DPU offload:
  - replace cross-process copies with DMA into service-owned/registered memory where practical

## Related

- [`off_path_local_table_distribution_buffering_and_copy_costs.md`](/data/dbcomm/postgres-citus/docs/kb/citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md)
- [`cross_node_service_to_service_control_path.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/connection-management/cross_node_service_to_service_control_path.md)
- [`rdma_transport_control_plane_abstraction.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/transport/rdma_transport_control_plane_abstraction.md)
- [`intermediate_results_service_future_directions.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/intermediate-results/intermediate_results_service_future_directions.md)
