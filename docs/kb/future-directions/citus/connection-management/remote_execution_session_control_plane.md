# Remote execution session control-plane design for Citus

## Scope

- **What this doc explains**: the chosen future-direction control-plane abstraction for the external-service architecture: one backend-visible remote execution session API, the exact high-level API families on top of it, and how the current tuple-route prototype should converge toward it.
- **What this doc does NOT cover**: low-level RDMA verb sequences, provider-specific CQ/QP details, or the exact memory layout of tuple batches on the wire.
- **Primary directory**: `docs/kb/future-directions/citus/connection-management/`

## Why this exists

The earlier tuple-route bootstrap prototype started from the data plane and only later exposed that the initial exact-sink bootstrap API was not modeling the same semantics as original Citus control flow.

The grounded current behavior in [`connection_management_control_plane.md`](../../../citus/connection-management/connection_management_control_plane.md) shows that original Citus already separates four semantic facets:

1. remote session compatibility and reuse
2. placement / co-location safety
3. remote transaction and command state
4. operation-specific sink state

The current tuple-route bootstrap API around [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957) and the exact tuple-sink key [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:50) is useful for bring-up, but it is not the right long-term backend-facing abstraction. It mixes rendezvous details, exact tuple-sink identity, queue discovery, and operation semantics into one bootstrap object.

This note records the chosen future direction: the backend should talk in terms of a **remote execution session**, not a tuple-sink key or a transport object.

## Implementation progress

The first wrapper checkpoint is now in code and documented in [remote_execution_session_wrapper_checkpoint.md](../../../implementations/citus/connection-management/remote_execution_session_wrapper_checkpoint.md).

The newer local service-owned control checkpoint is now in [local_service_owned_tuple_sink_control_checkpoint.md](../../../implementations/citus/connection-management/local_service_owned_tuple_sink_control_checkpoint.md).

The newer cross-node control checkpoint is now in [cross_node_tuple_sink_peer_control_checkpoint.md](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md).

What has landed so far:

- a backend-facing wrapper in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:1) and [`remote_execution_session.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1)
- the public wrapper input is now explicitly split into [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h) and [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h), so session compatibility semantics are no longer conflated with exact tuple-flow bootstrap fields
- a fixed-width local shared-memory control protocol in [`remote_execution_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:1)
- backend-side peer endpoint resolution and full tuple-view contract construction in [`BuildPeerEndpointFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:404) and [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:583)
- service-owned compatibility-session resolution plus tuple-sink open/close, queue naming, and sink allocation in [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1)
- sender-initiated cross-node peer open/close for the tuple-sink subset through [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1008), [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1150), and [`TupleSinkServiceHandlePeerCloseSinkRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1334)
- full-contract carriage on both local open and peer open through [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:276), [`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75), and service-side validation in [`TupleSinkServiceEnsureTupleViewContract()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2538)
- sender-side integration in [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2559)
- the earlier worker-side SQL bootstrap consumer has now been removed from the current tree because the service-owned control path is already complete enough for the tuple-sink prototype

Important current limitation:

- the service now owns a local compatibility-session layer, but that reuse is still only local and only preserves service-side compatibility metadata; it does not yet own the expensive remote SQL/RDMA state that the final design wants to reuse
- the current backend<->service control queue is still only local SHM IPC; the newly landed cross-node half now already owns a persistent per-peer RDMA transport substrate in [`remote_execution_peer_transport_rdma.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1), and after bootstrap it already uses a persistent one-sided control mailbox/channel for the tuple-sink prototype
- the concrete next-step design for the richer end-to-end peer-session model still lives in [cross_node_service_to_service_control_path.md](cross_node_service_to_service_control_path.md), which remains broader than the current landed tuple-sink-only implementation

Important refinement:

- for the current tuple-sink prototype, that means the control plane is now complete enough and the next blocker is the cross-node data plane
- the remaining control-path future work is broader generalization, not a prerequisite for resuming tuple transport work
- the canonical next-step data-plane design now lives in [../data-movement/tuple_sink_data_plane_completion_plan.md](../data-movement/tuple_sink_data_plane_completion_plan.md)

## Chosen abstraction

### Backend-visible object

Use one backend-visible `RemoteExecutionSession` object:

```c
typedef struct RemoteExecutionSessionData *RemoteExecutionSession;
```

This object is:

- **higher-level than a current `MultiConnection`**:
  - it is not just a cached backend-local PostgreSQL session
  - it includes the semantic constraints that make a remote context safe to reuse
- **higher-level than a route handle**:
  - it is not only "where tuple batches go"
  - it names the semantic remote execution context plus the active operation mode
- **lower-level than a whole query plan**:
  - it is the concrete remote context that one workflow instance uses to submit commands, stream tuples, or receive results

### Internal split that remains hidden from the backend

The backend sees one object, but the service should still keep two internal layers:

1. **execution-context state**
   - session compatibility
   - placement-safe binding
   - transaction policy / health

2. **operation state**
   - SQL command mode
   - query result stream mode
   - COPY-ingest mode
   - tuple-sink mode
   - replication mode

This is the key correction to the earlier exact-sink-bootstrap design: one backend-visible API does **not** require one flat internal object.

## Exact backend-facing API direction

### 1. Session lifecycle

```c
RemoteExecutionSession OpenRemoteExecutionSession(
    const RemoteExecutionSessionIntentSpec *sessionIntentSpec,
    const RemoteExecutionOperationSpec *operationSpec);

void CloseRemoteExecutionSession(RemoteExecutionSession session);
```

Semantics:

- [`OpenRemoteExecutionSession(...)`](#) resolves or creates a remote execution context compatible with the supplied session intent and then binds the requested operation spec under that session
- if the operation mode requires active sink/query state, it binds that during the open
- the backend does not separately open peer transports, route IDs, or sink handles

### 2. SQL command mode

Use when `sessionIntentSpec->opKind == REMOTE_EXEC_OP_SQL_COMMAND`.

```c
void SubmitRemoteCommand(RemoteExecutionSession session,
                         const RemoteCommandSpec *commandSpec);

RemoteCommandResultHandle PollRemoteCommandResult(
    RemoteExecutionSession session);
```

Grounding in current Citus:

- command send is currently [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565)
- result retrieval is currently [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)

### 3. Query result stream mode

Use when `sessionIntentSpec->opKind == REMOTE_EXEC_OP_QUERY_RESULT_STREAM`.

```c
RemoteResultBatchHandle PollRemoteResultBatch(
    RemoteExecutionSession session);

void ReleaseRemoteResultBatch(RemoteResultBatchHandle batch);
```

This keeps result-batch lifetime separate from session lifetime.

### 4. Tuple send mode

Use when `sessionIntentSpec->opKind` is `REMOTE_EXEC_OP_COPY_INGEST` or `REMOTE_EXEC_OP_TUPLE_SINK`.

```c
RemoteSendBatchHandle ReserveRemoteSendBatch(
    RemoteExecutionSession session,
    uint32 tupleCountHint);

void AppendTupleViewToRemoteSendBatch(RemoteSendBatchHandle batch,
                                      TupleTableSlot *slot);

void SubmitRemoteSendBatch(RemoteExecutionSession session,
                           RemoteSendBatchHandle batch);
```

Grounding in the current prototype:

- send-side reserve is currently [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1168)
- tuple materialization is currently [`AppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1260)
- submit is currently [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1404)

### 5. Tuple / result receive mode

```c
RemoteRecvBatchHandle PollRemoteRecvBatch(
    RemoteExecutionSession session);

bool BorrowRemoteTupleView(RemoteRecvBatchHandle batch,
                           uint32 tupleIndex,
                           RemoteTupleView *tupleView);

void CopyOutRemoteTupleView(RemoteRecvBatchHandle batch,
                            uint32 tupleIndex,
                            Datum *values,
                            bool *isnull);

void ReleaseRemoteRecvBatch(RemoteRecvBatchHandle batch);
```

Important semantic choice:

- `BorrowRemoteTupleView(...)` is the intended future fast path
- `CopyOutRemoteTupleView(...)` remains a supported fallback / compatibility path
- `ReleaseRemoteRecvBatch(...)` is the batch-level release in the borrow/use/release model

Grounding in the current prototype:

- polling is currently [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1450)
- batch release is currently [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1513)
- copy-out is currently [`CopyOutTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1562)
- borrow semantics do **not** exist yet in the implementation

## Why `session` and `batch` are different objects

The session and batch must stay separate.

### `RemoteExecutionSession`

Long-lived semantic context:

- session compatibility and reuse domain
- placement / transaction constraints
- active operation mode
- service-internal route / peer-transport / queue bindings

### `RemoteSendBatchHandle` / `RemoteRecvBatchHandle`

Short-lived buffer object:

- one writable send slot
- or one borrowed receive slot
- one specific payload region
- one specific release lifetime

This matches the current tuple-route checkpoint design:

- route/session-like open/close in [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957) and [`CloseCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1118)
- queue slot lifetime in [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1168), [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1450), and [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1513)

The key API consequence is:

- `PollRemoteRecvBatch(...)` is session-scoped
- `ReleaseRemoteRecvBatch(...)` is batch-scoped

The backend should not have to pass the session again just to release a borrowed batch.

## Exact session-intent direction

Do **not** model the session intent as one giant enum. The compatibility semantics are multi-dimensional.

### First-cut enum families

```c
typedef enum RemoteExecOpKind
{
    REMOTE_EXEC_OP_SQL_COMMAND,
    REMOTE_EXEC_OP_QUERY_RESULT_STREAM,
    REMOTE_EXEC_OP_COPY_INGEST,
    REMOTE_EXEC_OP_TUPLE_SINK,
    REMOTE_EXEC_OP_REPLICATION
} RemoteExecOpKind;

typedef enum RemoteExecAccessKind
{
    REMOTE_EXEC_ACCESS_NONE,
    REMOTE_EXEC_ACCESS_SELECT,
    REMOTE_EXEC_ACCESS_DML,
    REMOTE_EXEC_ACCESS_DDL,
    REMOTE_EXEC_ACCESS_METADATA
} RemoteExecAccessKind;

typedef enum RemoteExecTxPolicy
{
    REMOTE_EXEC_TX_JOIN_CURRENT,
    REMOTE_EXEC_TX_OUTSIDE_CURRENT
} RemoteExecTxPolicy;

typedef enum RemoteExecFreshnessPolicy
{
    REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE,
    REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN,
    REMOTE_EXEC_FRESHNESS_FORCE_FRESH
} RemoteExecFreshnessPolicy;

typedef enum RemoteExecOwnershipPolicy
{
    REMOTE_EXEC_OWNERSHIP_SHARE_IF_COMPATIBLE,
    REMOTE_EXEC_OWNERSHIP_PIN_EXCLUSIVE
} RemoteExecOwnershipPolicy;

typedef enum RemoteExecPlacementScope
{
    REMOTE_EXEC_SCOPE_NODE,
    REMOTE_EXEC_SCOPE_PLACEMENT,
    REMOTE_EXEC_SCOPE_COLOCATION_DOMAIN
} RemoteExecPlacementScope;
```

Representation rule:

- these are **separate typed fields**, not one shared flag namespace
- they do **not** need globally unique numeric values across all enums
- only a deliberately combinable bitmask field should use `1 << n` style values, and that uniqueness only matters within that field

### First-cut struct shape

```c
typedef struct RemoteExecutionSessionIntentSpec
{
    uint32_t destinationNodeId;
    Oid effectiveUserId;
    Oid databaseId;

    RemoteExecOpKind opKind;
    RemoteExecAccessKind accessKind;
    RemoteExecTxPolicy txPolicy;
    RemoteExecFreshnessPolicy freshnessPolicy;
    RemoteExecOwnershipPolicy ownershipPolicy;
    RemoteExecPlacementScope placementScope;

    bool transactionCritical;
} RemoteExecutionSessionIntentSpec;
```

This struct should stay cleanly aligned with the semantics Citus already reasons about when choosing, reusing, or pinning a remote execution context. It should **not** carry tuple-flow bootstrap fields, tuple descriptors, or sink-local identifiers.

### Separate operation spec

```c
typedef union RemoteExecutionOperationSpec
{
    RemoteExecTupleSinkOperationSpec tupleSink;
} RemoteExecutionOperationSpec;
```

Two important notes:

- `RemoteExecutionOperationSpec` is where the exact active-operation descriptor belongs. For the current tuple-sink prototype that means bootstrap tuple-flow fields such as `TupleDesc`, `CitusTupleSinkTransactionId`, `shardId`, `placementId`, `relationId`, and `routeOrdinal`. Backend PID is intentionally no longer part of the contract.
- [`CitusTupleSinkTransactionId`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:37) is used here only as a currently available prototype grounding point for distributed transaction identity; it is **not** a commitment that the final control plane will literally reuse the tuple-route type name.

The important boundary is:

- `RemoteExecutionSessionIntentSpec`: reusable compatibility / reuse semantics
- `RemoteExecutionOperationSpec`: exact operation-local state and bootstrap fields

That split is now implemented in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h).

## Reuse rule

The compatibility predicate should be:

- **reusable** if all session-compatibility, placement-safety, transaction, and ownership constraints are compatible
- **not reusable** if:
  - the existing session is pinned exclusively for an active incompatible operation
  - the caller requires a fresh or clean session and the existing one does not satisfy it
  - user/database/protocol mode differs
  - placement/co-location safety would be violated
  - transaction policy is incompatible

This is the current Citus logic, just made explicit at a higher level:

- generic reuse in [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277) and [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466)
- placement-safe reuse in [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281) and [`FindPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:517)
- no-reuse pinning in [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1293)

## Workflow mapping

### Workflow A: simple remote SQL command

- `opKind = REMOTE_EXEC_OP_SQL_COMMAND`
- `accessKind = REMOTE_EXEC_ACCESS_NONE`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT` or `REMOTE_EXEC_TX_OUTSIDE_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE`
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_SHARE_IF_COMPATIBLE`
- `placementScope = REMOTE_EXEC_SCOPE_NODE`

### Workflow B: placement-bound SELECT / DML / DDL

- `opKind = REMOTE_EXEC_OP_SQL_COMMAND`
- `accessKind = SELECT / DML / DDL`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE`
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_SHARE_IF_COMPATIBLE` in the common case
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT` or `REMOTE_EXEC_SCOPE_COLOCATION_DOMAIN`

Grounding:

- placement-safe connection choice in [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281)

### Workflow C: current off-path COPY ingest

- `opKind = REMOTE_EXEC_OP_COPY_INGEST`
- `accessKind = REMOTE_EXEC_ACCESS_DML`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN` in the high-performance separate-placement case
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_PIN_EXCLUSIVE` while the COPY sink is active
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT`

Grounding:

- placement logic in [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4017)
- clean-session branch at [multi_copy.c:4144](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4144)
- exclusive pin at [multi_copy.c:4216](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4216)

### Workflow D: future tuple-sink data plane

- `opKind = REMOTE_EXEC_OP_TUPLE_SINK`
- `accessKind = REMOTE_EXEC_ACCESS_DML`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT` in the first Citus-compatible design
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE` or `REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN`, depending on whether tuple-sink semantics still need isolated placement ownership
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_PIN_EXCLUSIVE` in the first design, with room to relax later if the service safely multiplexes active tuple routes
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT`

## Service-internal architecture implied by this API

The external service should own:

1. **session manager**
   - resolve / create compatible remote execution sessions
   - enforce reuse compatibility
   - track pinning / ownership policy

2. **operation manager**
   - open and close active command / stream / tuple-sink state on top of a session
   - track per-session operation mode

3. **transport manager**
   - own peer transport cache
   - own QP / ring lifecycle and failures
   - remain strictly below session semantics

4. **local queue / arena manager**
   - own local send and receive FIFO / arena endpoints
   - hand batch objects to backends

This is the important layering rule:

- the backend sees only sessions and batches
- route IDs, QPs, ring descriptors, and local queue objects remain internal

## How this serves the current tuple-route prototype

The current prototype should **not** be discarded. It should be reclassified.

### Keep as bootstrap machinery

Keep:

- [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:957)
- [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1168)
- [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1404)
- [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1450)
- [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1513)

### Reclassify

Treat as:

- `OpenCitusTupleSink()` -> current bootstrap implementation of `OpenRemoteExecutionSession(...)` for `REMOTE_EXEC_OP_TUPLE_SINK`
- route registry entries and shm names -> temporary internal session/queue lookup mechanism
- [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:50) -> temporary bootstrap exact tuple-sink identity, **not** the future backend-facing control abstraction

### Immediate implementation plan

1. **Keep pushing tuple-route details behind the wrapper**
   - stop adding new backend-visible callers of the raw tuple-route API
   - keep route spec / registry only as internal bootstrap details

2. **Preserve current batch APIs, but rename them conceptually**
   - tuple-route batch reserve/submit/poll/release become the bootstrap implementation of the session-level data-plane verbs

4. **Only then continue the data-plane prototype**
   - add true borrow semantics
   - replace registry discovery with explicit service control
   - add RDMA transport and control QPs

This is the crucial reconciliation step between the current data-plane progress and the newer control-plane understanding.

## Decision summary

The control-plane future direction is now:

- one backend-visible **remote execution session**
- one `RemoteExecutionSessionIntentSpec` composed only from orthogonal compatibility semantics
- one separate `RemoteExecutionOperationSpec` for the exact active operation
- one family of data-plane APIs that operate on the session and on short-lived batch objects
- route/transport/queue internals hidden behind the service boundary
- the current tuple-route prototype retained as bootstrap implementation machinery, but no longer treated as the target abstraction

## Related

- [`../../../citus/connection-management/connection_management_control_plane.md`](../../../citus/connection-management/connection_management_control_plane.md): grounded current Citus semantics this design must preserve
- [`../../postgres/client-sql-session/client_sql_session_offload_plan.md`](../../postgres/client-sql-session/client_sql_session_offload_plan.md): PostgreSQL client-session extension of the same remote-execution-session abstraction, introducing `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`.
- [cross_node_service_to_service_control_path.md](cross_node_service_to_service_control_path.md): concrete next-step design for turning the local-only service/session layer into an end-to-end cross-node control path
- [`../transport/rdma_transport_control_plane_abstraction.md`](../transport/rdma_transport_control_plane_abstraction.md): RDMA and transport-level choices that sit underneath this session abstraction
- [`../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md`](../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md): current implementation-progress checkpoint that should converge toward this design
