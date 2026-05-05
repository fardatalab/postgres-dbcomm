# RDMA transport control-plane abstraction for Citus

## Scope

- **What this doc explains**: design options for adapting current Citus connection/control-plane behavior to an RDMA-based data path, especially when tuple movement is offloaded to a per-node external service.
- **What this doc does NOT cover**: wire-format details, queue memory layouts, or implementation details for a chosen RDMA library/provider.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`

Important scope refinement:

- this note now focuses on transport-specific consequences and options beneath the chosen higher-level control-plane abstraction
- the canonical chosen control-plane design is recorded separately in [`../connection-management/remote_execution_session_control_plane.md`](../connection-management/remote_execution_session_control_plane.md)
- some earlier sections still discuss an `OpenRoute(...)` first-cut idea; treat that as historical scaffolding for what is now the session-plus-sink design

## Why this exists (context)

The current codebase has one libpq-centric control plane. It establishes and caches remote PostgreSQL sessions, opens remote SQL/COPY sinks on top of those sessions, and runs all IO through libpq wait loops.

For the RDMA/service direction, the open question is not only "how do we move bytes faster?". It is also:

- what part of current `MultiConnection` should remain
- whether the backend should directly manage RDMA transport handles
- whether a future route concept should be exposed to the backend as a first-class identifier, or hidden behind an opaque handle

Important clarification:

- the original code is libpq-centric because that is what exists today
- the long-term external service does **not** need to remain libpq-centric internally
- once the service owns the remote execution session abstraction, it can implement its own control/data channels, scheduling, and transport stack as long as it preserves the higher-level semantics Citus needs

The canonical grounded note is [`connection_management_control_plane.md`](../../../citus/connection-management/connection_management_control_plane.md).

Current implementation progress is tracked separately in:

- [`local_batch_materialization_checkpoint.md`](../../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md)
- [`local_service_owned_tuple_sink_control_checkpoint.md`](../../../implementations/citus/connection-management/local_service_owned_tuple_sink_control_checkpoint.md)
- [`cross_node_tuple_sink_peer_control_checkpoint.md`](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md)

Important RDMA publication refinement:

- the current tuple-sink prototype should no longer treat plain memory polling of a separately written tail/head word as the responder-visible publication rule
- the chosen direction is now documented in [`rdma_publication_visibility_and_doorbells.md`](rdma_publication_visibility_and_doorbells.md)
- the current implementation therefore keeps the 64-bit state words in memory, but uses `RDMA_WRITE_WITH_IMM` as the responder-visible publish/doorbell step for both control and tuple payload channels

Important correction from the current implementation checkpoint:

- the landed prototype no longer uses the old shared-memory route registry in active code
- the landed prototype now uses a well-known shared-memory control region for local backend<->service open/close, plus a service-owned sink table and per-sink send/receive queues
- the remaining route-registry code is historical reference only, not the current control-path implementation
- this note therefore treats registry-based discovery as an earlier bootstrap option to compare against more explicit service-owned control protocols

## Grounding in current code

- cached peer/session connections are established in [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277) and [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:973)
- remote SQL/COPY command IO is driven by [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565), [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683), [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738), and [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:822)
- remote sink opening for off-path COPY is explicit in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020)
- tuple routing to shard placements happens before serialization in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274)
- placement/co-location conflict tracking is separate from transport caching in [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281)

Those code paths imply that a future transport abstraction must separate:

- peer transport identity
- logical sink/flow identity
- remote transaction / SQL control semantics

## Candidate approaches

### Bootstrap seam: shared-memory route registry

- **Idea**: keep one well-known shared-memory registry that lists active routes and the shm names of their per-route queues, and let the local service discover routes by scanning that registry.
- **Grounding in current implementation**:
  - registry name [`CITUS_TUPLE_ROUTE_REGISTRY_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L31)
  - backend-side mapping in [`MapTupleSinkRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L214) and [`LocateOrCreateTupleSinkRegistryEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L279)
  - service-side scan in [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L263)
- **Pros**:
  - simplest current rendezvous mechanism for a separate local process
  - keeps the first process boundary debuggable before RDMA control is introduced
  - does not yet require a richer service-owned control queue or RDMA control QP
- **Cons**:
  - polling/scanning is a poor long-term control mechanism
  - global registry lifecycle and cleanup are harder to reason about than explicit route-open/close messages
  - encourages treating route discovery as shared-state lookup rather than explicit control flow

Recommendation:

- treat the registry only as a historical bootstrap seam
- do not treat it as the preferred long-term route-open/control abstraction

### Approach 1: replace `MultiConnection` with a generic transport object

- **Idea**: generalize current `MultiConnection` into a transport-agnostic object with pluggable backends for libpq and RDMA.
- **Pros**:
  - one visible object model for callers
  - existing callers already expect "get connection, send command/copy, wait, cleanup"
  - easiest story if the goal is to replace sockets inside the backend without introducing an external service boundary
- **Cons**:
  - `MultiConnection` is not transport-pure today; it embeds SQL session identity, remote transaction state, metadata-connection semantics, and placement bookkeeping
  - pulling RDMA/QP state into the same object risks over-coupling transport and SQL concerns
  - poor fit if the long-term architecture uses a separate per-node external service, because backends then should not manage QPs directly

### Approach 2: keep `MultiConnection` for SQL control plane, add a parallel RDMA data-plane cache in the backend

- **Idea**: leave libpq-based `MultiConnection` in place for remote SQL control commands and transactions, but add a second backend-local peer transport cache for RDMA tuple movement.
- **Pros**:
  - smaller blast radius than replacing `MultiConnection`
  - preserves current remote transaction behavior and metadata-connection semantics
  - a future `routeId` can be introduced cleanly as a data-plane flow/sink identifier layered above the RDMA peer transport
- **Cons**:
  - backend now owns two peer caches and two wait/cleanup mechanisms
  - transport readiness and failure state become harder to reason about
  - weaker fit if the real end goal is an external tuple service rather than direct backend-managed RDMA

### Approach 3: service-owned RDMA transport, backend-visible route handles

- **Idea**: keep current backend SQL/libpq control plane for remote setup where needed, but move RDMA peer/QP management into the external per-node service. Backends see only the local session/sink-facing service API returned by [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099).
- **Pros**:
  - best fit to the planned external serializer/network service
  - keeps QP lifecycle out of PostgreSQL/Citus backends
  - aligns with the grounded fact that current sink identity is already above transport, e.g. [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020) opens a specific COPY sink on top of an already established connection
  - lets the service keep whatever internal route identity it needs, while the backend only holds an opaque route handle
- **Cons**:
  - requires a new local control protocol between backends and the service
  - makes the system explicitly dual-layer: backend-to-service locally, service-to-service remotely
  - remote SQL control commands and tuple data-plane setup need careful coordination

## Recommended direction

For the external-service architecture, **Approach 3** is the cleanest target.

The main correction to the earlier intuition is:

- a route concept may still be necessary internally
- but not because it replaces a socket or QP identifier
- and it does not need to be exposed to the backend as a separate named `routeId` in the first design

In other words:

- peer/QP identity answers: "which remote service instance are we connected to?"
- route/sink identity answers: "which remote tuple consumer or data sink on that peer should receive these batches?"

One more refinement from the current checkpoint:

- the backend may continue to compute a deterministic route specification and use it to rendezvous with the local service
- but the current shared-memory registry should eventually give way to an explicit route-open/close control protocol
- the route concept itself survives; the registry-based discovery mechanism does not have to

That distinction is already visible in current Citus:

- peer/session cache: [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:240)
- active COPY sink above the connection: [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:192) with [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020)

## Scope boundary of the earlier `OpenRoute(...)` idea

`OpenRoute(...)` was a useful first backend-facing abstraction for the **tuple data plane**, but it is no longer the chosen direction. The current design instead uses [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) plus an exact tuple-sink operation spec under that session.

Why it is not universal:

- current connection caching is keyed by SQL session identity, not just remote node identity; see [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:240) and [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277)
- current remote transaction state is attached to `MultiConnection`; see [`RemoteTransaction`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h:61) and [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859)
- current placement/co-location conflict tracking decides which connection is safe to use for a placement access; see [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281) and [`CanUseExistingConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:754)
- current adaptive execution also relies on exclusive claiming and ordered connection acquisition; see [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1293) and the worker ordering/claiming logic in [`adaptive_executor.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:1585)

Recommended interpretation of that earlier design step:

- the backend still needs an operation-local tuple-sink concept below the higher-level session
- that exact tuple-sink binding should not be confused with a generic remote SQL/session/transaction-capable connection
- current Citus connection/session infrastructure should remain the authority for SQL control-plane work, transaction begin/commit/abort semantics, metadata-connection semantics, and placement conflict checks

Important refinement from the control-plane analysis:

- semantically, current Citus separates a relatively stable remote execution context from the more ephemeral operation/sink state opened on top of it
- that does **not** force the future backend-facing API to use two distinct control calls
- a future service may still expose one backend-visible `Open...` call that bundles both concerns in one `(sessionIntentSpec, operationSpec)` pair
- if so, the service should still keep the two layers separate internally:
  - execution-context state: session compatibility, placement-safe binding, transaction policy
  - operation state: query command, COPY sink, tuple sink, result stream
- this distinction matters because it preserves the original Citus control-plane semantics without forcing the backend to micromanage them

## Proposed abstraction layers

### Layer 1: peer transport cache

- **Owner**: external service
- **Role**: establish/reuse RDMA peer transport to another node's service
- **Identity**: peer service endpoint or node identity, not per-shard/per-query flow
- **Current analogue**: [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:240) plus async establishment in [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1353) and [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:973)

### Layer 2: logical sink handle

- **Owner**: backend-visible service API plus service-internal bookkeeping
- **Role**: represent a remote tuple consumer opened on top of a peer transport
- **Examples**:
  - one off-path shard-copy sink
  - one intermediate-result producer/consumer
  - one worker-result stream
- **Current analogue**:
  - COPY sink opened by [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020)
  - per-placement routing inside [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274)
- **Earlier first-design simplification**:
  - expose only an opaque sink handle returned by an `Open...` call
  - do not expose a distinct backend-visible sink id yet
  - let the service map the handle to whatever internal exact sink key it needs

### Layer 3: batch handles

- **Owner**: backend and service jointly, through shared-memory ownership rules
- **Role**: submit or receive deformed tuple batches on an exact sink
- **Current analogue**: currently missing as a first-class concept; COPY bytes in [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738) play this role implicitly today

## How this maps to the current control path

Current Citus/libpq shape:

1. establish/reuse peer session
   - [`StartNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:208)
   - [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:973)
2. open remote sink or command context
   - [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565)
   - [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)
   - [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020)
3. push data
   - [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:738)
4. close sink
   - [`PutRemoteCopyEnd()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:786)
   - [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4048)

Recommended service/RDMA shape after the control-plane convergence:

1. open remote execution session plus initial tuple-sink operation
   - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099)
   - the service ensures or reuses the peer transport internally
2. submit / poll tuple batches
   - [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208)
   - [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)
   - [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290)
3. release / close
   - [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1388)
   - [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183)

The important point is that the backend API stays session/sink-centric, not transport-centric. Peer/QP lifecycle remains a service-internal concern.

Important refinement from implementation bring-up:

- if the backend must encode destination identity into a deterministic route specification, prefer a concrete Citus `nodeId` over `groupId`
- the current sender-side bring-up code does exactly that via [`LookupNodeForGroup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_cache.c:1300) in [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3672)
- `groupId` remains useful for placement/co-location decisions, but it is not the best long-term route key because it names a placement group rather than a concrete destination node
- hostname/port should remain transport-layer details hidden behind the service-owned peer transport cache

## How current Citus subsystems should affect the RDMA/service design

### Remote SQL transaction state

- First-stage recommendation:
  - keep remote SQL transaction state attached to the existing backend/libpq control plane while the tuple data plane is being prototyped
  - this minimizes blast radius and keeps the new work focused on tuple movement
- Long-term direction:
  - if the external service grows into the full remote execution session owner, it should also own the remote transaction/session semantics currently represented by [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859), [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1018), and [`CheckRemoteTransactionsHealth()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1592)
  - at that point, those semantics are no longer bound to libpq specifically; they become requirements of the higher-level remote execution session abstraction

### Placement routing and conflict tracking

- Placement/co-location routing should remain a backend control-plane responsibility.
- The route API should consume the result of that decision, not replace it.
- For example, off-path shard COPY still needs the safety properties enforced today by [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281), [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:368), and [`ConnectionAccessedDifferentPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:788).

### First-cut `fullIntentSpec` for a remote execution session

To answer "when may an existing remote execution session be reused?", the future abstraction should stop reasoning from ad hoc connection flags alone and instead classify intent explicitly.

A first-cut `fullIntentSpec` should contain at least these semantic fields:

1. **Session-compatibility fields**
   - destination node identity
   - effective user / role identity
   - database identity
   - protocol mode such as normal SQL vs replication-style mode
   - current Citus grounding: [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:240)

2. **Placement-safety fields**
   - placement or colocated-access domain
   - access type: `SELECT`, `DML`, `DDL`
   - "clean session" requirement for cases like COPY on non-colocated placements
   - current Citus grounding: [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281), [`FindPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:517), [`ConnectionAccessedDifferentPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:788)

3. **Transaction-context fields**
   - may join current distributed transaction or must stay outside it
   - whether transaction failure is critical for the caller
   - later, snapshot / exported-snapshot / isolation requirements if the service owns more of the SQL/session layer
   - current Citus grounding: `OUTSIDE_TRANSACTION` in [`MultiConnectionMode`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:72), plus [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859) and [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1018)

4. **Operation fields**
   - command mode, result-stream mode, COPY-ingest mode, tuple-sink mode, replication mode
   - operation-specific sink ownership, ordering, and lifetime
   - current Citus grounding: [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4294), [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4329), [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:165)

5. **Reuse / pinning policy**
   - reusable if compatible
   - force fresh session
   - pin exclusively for active operation lifetime
   - metadata-singleton behavior
   - current Citus grounding: `FORCE_NEW_CONNECTION`, `REQUIRE_METADATA_CONNECTION`, and `REQUIRE_CLEAN_CONNECTION` in [`MultiConnectionMode`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:72), plus [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1293)

With that shape, reuse is no longer "same backend owns one connection". Reuse becomes:

- allowed when all semantic fields above are compatible and the session is not currently pinned for an incompatible active operation
- disallowed when compatibility fails or when the current session is explicitly pinned / force-fresh / metadata-singleton constrained

This is the key distinction for the external-service design:

- the service may centralize ownership and scheduling of remote execution sessions
- but it must still preserve semantic compatibility boundaries above the transport
- any later RDMA multiplexing must stay below those boundaries

### Prefer a struct of orthogonal enums, not one giant mode enum

A single monolithic `fullIntentSpec` enum would be too coarse. Current Citus compatibility checks are already multi-dimensional: session key, placement safety, transaction context, and active operation mode are not one axis.

A better first cut is a struct composed from a few orthogonal enums:

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

Important representation clarification:

- these are intended as **separate typed fields** inside `RemoteExecutionSessionIntentSpec`, not as one global shared flag namespace
- therefore their numeric values do **not** need to be globally unique across all enums
- only fields that are intentionally modeled as combinable bitmasks should use `1 << n`-style unique values, and that uniqueness only matters **within that field**
- for the current design, `opKind`, `txPolicy`, `freshnessPolicy`, `ownershipPolicy`, and `placementScope` should stay ordinary enums rather than bit flags
- if later we need true combinations for some dimension, add a dedicated bitmask field instead of overloading every enum into a global flag set

These enums are only the first decomposition. The exact struct fields can still evolve, but the point is:

- `opKind` answers what operation state must exist on top of the remote execution session
- `accessKind` answers what placement-safety rules apply
- `txPolicy` answers whether the session may participate in the caller's transaction context
- `freshnessPolicy` answers whether reuse is allowed, requires a clean session, or must force a new one
- `ownershipPolicy` answers whether the resulting session may still be handed out to another compatible claimant while this operation is active
- `placementScope` answers whether compatibility is node-wide, placement-specific, or co-location-domain specific

This is already closer to current Citus flags and helpers than one big enum:

- `FORCE_NEW_CONNECTION`, `REQUIRE_CLEAN_CONNECTION`, and default reuse in [`MultiConnectionMode`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:72) map naturally to `freshnessPolicy`
- `OUTSIDE_TRANSACTION` in [`MultiConnectionMode`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:72) maps naturally to `txPolicy`
- `FOR_DML` / `FOR_DDL` in [`StartPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:236) map naturally to `accessKind`
- [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1293) maps naturally to `ownershipPolicy`

### First workflow mapping onto the enum sketch

#### Workflow A: simple remote SQL command

- `opKind = REMOTE_EXEC_OP_SQL_COMMAND`
- `accessKind = REMOTE_EXEC_ACCESS_NONE`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT` or `REMOTE_EXEC_TX_OUTSIDE_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE`
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_SHARE_IF_COMPATIBLE`
- `placementScope = REMOTE_EXEC_SCOPE_NODE`

Grounding:

- generic session acquisition through [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277)
- optional transaction transition through [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:859)
- command send/result through [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)

#### Workflow B: placement-bound SELECT / DML / DDL

- `opKind = REMOTE_EXEC_OP_SQL_COMMAND`
- `accessKind = SELECT / DML / DDL`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE`
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_SHARE_IF_COMPATIBLE` in the common case
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT` or `REMOTE_EXEC_SCOPE_COLOCATION_DOMAIN`

Grounding:

- placement-safe selection in [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:281)
- conflict detection in [`FindPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c:517)

#### Workflow C: current off-path COPY ingest

- `opKind = REMOTE_EXEC_OP_COPY_INGEST`
- `accessKind = REMOTE_EXEC_ACCESS_DML`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT`
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN` for the high-performance separate-connection case on hash-distributed placements
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_PIN_EXCLUSIVE` while the active COPY sink is in use
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT`

Grounding:

- placement choice in [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4017)
- clean-session requirement through `REQUIRE_CLEAN_CONNECTION` at [`multi_copy.c:4144`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4144)
- exclusive pin through [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4216)
- active sink open/close through [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4294) and [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4329)

#### Workflow D: future tuple-sink data plane

- `opKind = REMOTE_EXEC_OP_TUPLE_SINK`
- `accessKind = REMOTE_EXEC_ACCESS_DML`
- `txPolicy = REMOTE_EXEC_TX_JOIN_CURRENT` in the first Citus-compatible design
- `freshnessPolicy = REMOTE_EXEC_FRESHNESS_REUSE_IF_COMPATIBLE` or `REMOTE_EXEC_FRESHNESS_REQUIRE_CLEAN`, depending on whether tuple-sink semantics still require isolated placement ownership
- `ownershipPolicy = REMOTE_EXEC_OWNERSHIP_PIN_EXCLUSIVE` for a first design, with room to relax later if the service can safely multiplex active tuple routes
- `placementScope = REMOTE_EXEC_SCOPE_PLACEMENT`

This is exactly where the external service can improve on current Citus:

- keep the same semantic fields
- but stop forcing FE/BE COPY sink semantics onto the transport
- and decide independently whether one active tuple route really needs exclusive semantic ownership or only exclusive transport-side ordering/backpressure management within the service

### Next design step: from taxonomy to concrete ABI

The next control-plane design step should not be another round of ad hoc route keys. It should be:

1. define a concrete `RemoteExecutionSessionIntentSpec` struct plus a separate `RemoteExecutionOperationSpec`
2. define the compatibility / reuse predicate over that struct
3. define the backend-facing lifecycle APIs that consume it

#### Proposed backend-facing control-plane lifecycle

Use one backend-visible session object:

```c
typedef struct RemoteExecutionSessionData *RemoteExecutionSession;

RemoteExecutionSession OpenRemoteExecutionSession(
    const RemoteExecutionSessionIntentSpec *sessionIntentSpec,
    const RemoteExecutionOperationSpec *operationSpec);

void CloseRemoteExecutionSession(RemoteExecutionSession session);
```

The important point is:

- `OpenRemoteExecutionSession(...)` resolves or creates a semantically compatible remote execution session from `sessionIntentSpec` and then binds the requested active operation mode from `operationSpec`
- the service may still keep execution-context state and operation state as separate internal layers
- the backend does not need to manage those layers separately

#### Proposed data-plane API families on top of one session handle

The session is backend-visible; the valid verbs depend on `opKind`.

1. **SQL command mode**

```c
void SubmitRemoteCommand(RemoteExecutionSession session,
                         const RemoteCommandSpec *commandSpec);

RemoteCommandResultHandle PollRemoteCommandResult(
    RemoteExecutionSession session);
```

Use for workflows grounded today in [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683).

2. **Query result stream mode**

```c
RemoteResultBatchHandle PollRemoteResultBatch(
    RemoteExecutionSession session);

void ReleaseRemoteResultBatch(RemoteResultBatchHandle batchHandle);
```

Use for distributed query/result workflows that currently live above libpq result retrieval rather than COPY sink state.

3. **COPY ingest / tuple sink mode**

```c
RemoteSendBatchHandle ReserveRemoteSendBatch(
    RemoteExecutionSession session,
    uint32 tupleCountHint);

void SubmitRemoteSendBatch(RemoteExecutionSession session,
                           RemoteSendBatchHandle batchHandle);

RemoteRecvBatchHandle PollRemoteRecvBatch(
    RemoteExecutionSession session);

void ReleaseRemoteRecvBatch(RemoteRecvBatchHandle batchHandle);
```

Use for:

- current off-path COPY-like ingest semantics
- future tuple-sink data plane

The transport and queue implementation under these verbs may be COPY/libpq today, RDMA tomorrow, or some hybrid during bring-up.

#### Why `session` and `batchHandle` are different concepts

The `RemoteExecutionSession` names the long-lived semantic context:

- session compatibility
- placement / transaction constraints
- active operation mode
- service-internal transport bindings such as peer transport, route state, and local queue endpoints

The `batchHandle` names one concrete producer-owned or consumer-borrowed buffer region:

- one send slot reserved for serialization/publication
- or one receive slot borrowed from the local tuple/result arena
- with its own payload bytes, tuple count, and release lifetime

That split matches the current tuple-route prototype already:

- route/session-like lifetime in [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:919)
- queue-scoped reserve/submit in [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1130) and [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1366)
- receive-side borrow/release shape in [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1412) and [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1475)

Important ownership rule:

- polling is session-scoped, because the service must know which semantic stream/operation queue to drain
- release is batch-scoped, because the returned object already identifies the concrete borrowed slot to give back

So the backend does **not** need to pass the session again to `ReleaseRemoteRecvBatch(...)` if the batch handle carries its parent-session identity internally.

#### Does the session "include" both transport ring and local arena?

Semantically yes, structurally no.

- The session should own or reference all service-internal resources needed to move this operation's data:
  - peer transport binding or route binding
  - transport-ring / wire-publication state
  - local send/receive queue or arena endpoints
- But those resources should remain internal implementation details of the service/session manager, not fields the backend manipulates directly.

For the backend API, the right mental model is:

- `RemoteExecutionSession` = "which semantic remote execution context / stream am I talking to?"
- `batchHandle` = "which concrete borrowed or writable buffer object am I holding right now?"

That is why `PollRemoteRecvBatch(session)` still needs the session, but `ReleaseRemoteRecvBatch(batchHandle)` can be batch-only in the borrow/use/release model.

#### Immediate follow-up work after the ABI sketch

After the API families above, the next grounded design task is to classify each field of `RemoteExecutionSessionIntentSpec` and `RemoteExecutionOperationSpec` into one of:

- contributes to session compatibility key
- contributes to operation compatibility / pinning only
- contributes only to payload metadata, not session reuse
- prototype-only scaffolding that should disappear

That field-by-field classification is what will let the current tuple-route prototype replace its rendezvous-heavy control path with a real remote execution session abstraction.

### Recommended next step for the prototype

Do **not** jump straight from the current tuple-route prototype to more RDMA work. The better next step is an interface-alignment pass that reconciles the existing data-plane prototype with the newer control-plane design.

Concretely:

1. keep the current tuple-route/local-service path as bootstrap implementation machinery
   - [`OpenCitusTupleSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:919)
   - [`ReserveCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1130)
   - [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1366)
   - [`PollCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1412)
   - [`ReleaseCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1475)
2. stop treating [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:50) as the future backend-facing control abstraction
3. define how the current route open / queue reserve / poll / release operations map onto:
   - `OpenRemoteExecutionSession(...)`
   - `ReserveRemoteSendBatch(...)`
   - `SubmitRemoteSendBatch(...)`
   - `PollRemoteRecvBatch(...)`
   - `ReleaseRemoteRecvBatch(...)`
4. classify every field currently flowing through `CitusTupleSinkKey` or the route registry as one of:
   - session-compatibility field
   - operation/open-session field
   - queue/transport-internal field
   - temporary bootstrap scaffolding

The purpose of this step is to preserve the current useful data-plane progress while removing the conceptual mismatch between:

- the older exact-sink bootstrap API
- the newer remote-execution-session control-plane abstraction

Only after that alignment should the prototype move on to:

- true borrow semantics instead of copy-out
- service-to-service control channel cleanup
- RDMA transport implementation

In short:

- do not restart the data-plane design from scratch
- do not keep layering more transport work on top of the old route abstraction either
- first wrap/reclassify the existing prototype around the new session-centric control plane

### Multiplexing

- The external service may still need multiplexing state above one peer transport, because current Citus already multiplexes multiple logical sinks over one connection in COPY paths.
- The crucial distinction is **what is being multiplexed**:
  - multiplexing multiple logical routes or operation streams over one RDMA transport can be valid if route demux, ordering, backpressure, and failure attribution are preserved
  - multiplexing multiple incompatible remote execution sessions, placement-safety domains, or transaction contexts into one shared semantic session would be incorrect
- In other words, transport sharing is a lower-layer optimization; it must stay strictly below the higher-level guarantees of the remote execution session abstraction.
- The grounded example is [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:164), with one `activePlacementState` and buffered inactive placements managed by [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2274).
- Current Citus COPY multiplexing is **not** true concurrent interleaving:
  - one placement is active on a connection
  - other placements accumulate bytes in their [`CopyPlacementState->data`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:201)
  - once buffered data exceeds [`CopySwitchOverThresholdBytes`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:139), [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2371) ends the current COPY, switches the active placement, and flushes the buffered bytes
  - shutdown drains any remaining buffered placements one by one in [`ShutdownCopyConnectionState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2804)
- The reason for that shape is the FE/BE COPY protocol: one libpq connection can only have one active `COPY FROM STDIN` sink at a time, opened by [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4020) and closed by [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4055).
- RDMA/message-based transport may allow cleaner interleaving than libpq COPY over one socket, so the exact buffering policy can improve, but the existence of multiple logical sinks per peer should be assumed.
- in the tuple-service design, this multiplexing question is specifically about the **service-to-service transport ring/QP side**
  - local backend/service tuple FIFOs or slot pools can remain simple per-session/per-route ownership structures
  - multiple logical routes can still be multiplexed over one RDMA connection/ring later by tagging wire messages and demultiplexing them in the remote service
  - whether that is beneficial depends on the QP/resource budget versus the added fairness/backpressure complexity
  - the service must still preserve higher-level isolation boundaries such as session compatibility, placement affinity/read-your-writes, transaction state, and active operation ownership

### Recommended service-side multiplexing model

There are three realistic designs.

#### Option 0: no multiplexing, one dedicated transport path per route/backend flow

- dedicate one QP / wire ring to one active route or one backend-owned route flow
- do not allow multiple active logical routes to share that transport path
- in the first prototype, prefer `SPSC` queue assumptions end to end

Pros:

- simplest first prototype
- preserves FIFO semantics naturally
- avoids route demux, fairness, and route-local backpressure inside one transport
- makes it plausible to collapse "wire queue" and "deserialized receive queue" into one bounded FIFO structure if the rest of the assumptions also hold

Cons:

- pushes complexity into transport/resource count instead of multiplexing logic
- if one backend or one coordinator COPY touches many placements on the same peer, QP/count and registered-buffer count can grow quickly
- loses the sharing benefit that current Citus gets from reusing one cached connection for multiple remote sinks
- likely not the long-term fit if the service is shared across many backends or if we want DPU-friendly transport consolidation

When this option is reasonable:

- first proof of concept
- one route per backend or per placement is acceptable
- we explicitly defer transport sharing and native demux
- one service thread per node is acceptable, so a future `MPSC` ingress path is unnecessary for the initial bring-up

#### Option A: compatibility-style multiplexing

- keep one active route per peer transport
- buffer batches for other routes
- switch active route when buffered work crosses a threshold or when the current route drains

Pros:

- closest match to current [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:164) behavior
- easier to reason about if the remote side still has a single active sink concept

Cons:

- preserves the main weakness of current COPY multiplexing: head-of-line blocking and extra buffering/copying at switch-over
- wastes the fact that RDMA/message transport can carry route-tagged batches naturally

#### Option B: native route-demux multiplexing

- allow multiple logical routes to be active concurrently on one peer transport
- every wire message carries a service-internal route key
- the remote service demultiplexes messages into per-route receive arenas/queues

Pros:

- removes the "one active sink per transport" restriction imposed today by FE/BE COPY
- avoids COPY-style switch-over and large route-local buffering
- maps naturally to a backend-visible session-plus-batch API while the service keeps internal exact sink ids

Cons:

- requires the service to own more scheduling, fairness, and per-route backpressure logic
- slightly larger control protocol, because route open/close and error states become first-class in the service

Recommendation:

- for the external tuple service, prefer **Option B** as the long-term direction
- for a first proof of concept, **Option 0** is the easiest if we are willing to spend more transport state
- if a first proof of concept needs to stay closer to current behavior while still sharing transports, Option A can be used as a temporary stepping stone, but it should be viewed as a compatibility mode, not the intended long-term design
- commands between external services, such as route open/close, ack/error, and end-of-stream publication, should use a dedicated RDMA control queue pair rather than SQL/UDF traffic
- a backend-visible SQL/UDF hook may still appear temporarily during bootstrap to start a worker backend participant, but that should be treated as scaffolding rather than target design
- the current shared-memory route registry belongs in the same "bootstrap scaffolding" bucket as any temporary SQL/UDF seam: useful for bring-up, but not the target control path

Current implementation note:

- the current landed checkpoint still stops before any of these transport options
- sender-side code opens a route handle and materializes a local batch, but it still leaves the real data path on the existing COPY transport
- so this transport note remains purely forward-looking today

### Receive-memory management: FIFO ring versus batch-slot pool

The term "arena" here should not be read as "malloc-style heap managed by the service". The practical design is a **preallocated pool of fixed-size or capped-size batch slots/pages**, plus descriptor rings.

#### Option 1: pure FIFO circular queue for deserialized tuple views

- tuples or tuple batches are placed directly into a circular queue
- backend consumption advances the head, producer advances the tail
- in the strictest form, the queue storage and the queue metadata are the same structure: a bounded circular buffer of fixed-size or capped-size slots

Pros:

- very simple ownership model
- naturally matches single-producer/single-consumer FIFO cases
- ring storage and data storage can be unified when slot sizing is fixed enough

Cons:

- only works cleanly when production and consumption are strictly FIFO and nearly rate-matched
- a borrowed batch in the middle of the queue prevents immediate reuse of older space behind it
- once native route-demux multiplexing exists, one slow route can create transport-visible head-of-line pressure if storage is too tightly coupled to one FIFO
- variable-size deserialized tuple batches make wrap-around and reclaim rules more awkward than for fixed-size descriptors

#### Option 2: preallocated batch-slot pool with descriptor rings

- shared memory is carved into N receive slots/pages
- free slots are tracked in a free ring or freelist
- ready batches are published to the backend through a completion ring
- releasing a batch returns its slot to the free pool

Pros:

- stable memory for borrow/release semantics
- no need to keep all receive data in one contiguous FIFO
- easier to support multiple active routes on one peer transport
- still highly predictable and preallocated, without heap-style allocation on the hot path

Cons:

- one more level of indirection through descriptors
- needs slot sizing or a small-class allocator strategy for variable-size batches

Recommendation:

- if we choose **Option 0** above and keep one FIFO route per transport/backend flow, **Option 1** becomes a reasonable first implementation
- for a shared-transport or native-demux design, prefer **Option 2**
- the wire side can still be a FIFO/ring per peer transport or per QP
- the receive-side pool should be understood as a bounded slot pool, not a general-purpose arena allocator
- if the service later becomes a many-backend ingress point, revisit `MPSC` or per-backend `SPSC` fan-in as a separate design step rather than baking that complexity into the first prototype

### Transport topology remains open

The transport note should keep this part open for now.

- one QP / wire ring per backend:
  - simpler ownership and naturally FIFO
  - pairs well with Option 0 and pure FIFO receive queues
  - higher transport-state count and weaker sharing
- one shared peer transport with many routes:
  - better long-term fit for native route demux
  - requires the service to own multiplexing and per-route receive-slot management

Recommendation:

- keep both topologies open as future directions
- a first prototype can reasonably choose one-QP-per-backend or one-QP-per-route specifically to avoid multiplexing
- do not let the backend API depend on which one is chosen

### Shared transport substrate, separate logical channels

One important refinement from the current control-plane discussion is that transport reuse should happen at the **peer RDMA implementation substrate**, not by collapsing all message classes into one shared ring.

The preferred long-term shape is:

- one peer-owned RDMA transport object per relevant peer
- one small control ring/channel under that transport for peer/session/sink lifecycle messages
- one or more data rings/channels under that transport for payload-carrying operation classes

For the current tuple-sink prototype, the minimum useful split is:

- one control ring/channel for `PEER_OPEN`, `PEER_CLOSE`, `PEER_ERROR`, and later teardown metadata
- one tuple-payload ring for serialized tuple batches

If later operation families justify it, the service may add more data-plane channels, for example:

- one tuple-sink payload ring
- one SQL/command payload ring
- one result-stream payload ring

Important semantic boundary:

- these rings/channels remain strictly **below** the backend-visible `RemoteExecutionSession`
- the number or shape of transport rings must not leak into the backend-visible session semantics

Why this is better than one unified ring:

- small control messages should not queue behind large tuple payload batches
- control and data want different slot sizing, batching, and backpressure policy
- the external service can only prioritize lifecycle/error traffic cleanly if it is not forced through the same publication queue as bulk payload

Why this is better than two totally unrelated transport stacks:

- peer bootstrap, registered-memory exchange, liveness, and completion plumbing can still be shared
- the control plane and data plane can converge on one RDMA implementation substrate without collapsing into one queue

Practical implication for the currently landed code:

- the short-lived per-request transport is already gone; the control path now uses a persistent per-peer RDMA transport owner through [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1861)
- after a one-time bootstrap exchange, control messages already move over a persistent mailbox plus `WRITE_WITH_IMM` doorbells via [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2109) and [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2060)
- the current requester-side CQ policy is now the first mostly-unsignaled version: mailbox slot writes and payload interior writes are unsignaled, while the final publish WR is the single signaled local completion checkpoint through [`TupleSinkServiceWaitForCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:489)
- the same publication rule now also applies to the tuple payload path through [`TupleSinkServicePostPeerInlineBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2876), [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2966), [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3040), and [`TupleSinkServiceConsumePeerDataDoorbellRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3097)
- the intended next step is now to keep that shared peer transport owner and add the tuple-payload data ring beside the existing control channel, rather than rewriting the control path again first
- the canonical post-control-plane implementation plan for that step now lives in [../data-movement/tuple_sink_data_plane_completion_plan.md](../data-movement/tuple_sink_data_plane_completion_plan.md)

SPSC clarification:

- one `SPSC` ring is still one-directional
- a bidirectional remote execution session therefore needs one outgoing and one incoming channel per logical ring class
- that is still sufficient for the current single-threaded external-service model in [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1994)
- `MPSC` or `MPMC` only becomes necessary if multiple threads start publishing directly into the same outgoing peer ring

### Connection-management lifecycle reuse

- We should reuse the **shape** of the current lifecycle, not literally graft QP objects into `MultiConnection`.
- The reusable shape is:
  - cache peer transports
  - establish asynchronously
  - track readiness/failure
  - restart/close on error or lifecycle end
- The best place for that reuse is inside the external service, because the backend-facing design is intentionally not supposed to manage RDMA directly.

## Recommended backend-facing API

- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099)
- [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183)
- [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1208)
- [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1235)
- [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1252)
- [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1290)
- [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1388)
- [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1363)

Notes:

- the backend supplies session semantics through [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:123) and operation-local tuple-sink details through [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h:107)
- the backend does not call `OpenPeerTransport(...)` directly
- the service may still use internal peer ids, session ids, and exact sink ids, but they are not part of the backend-facing API

## Open questions / TODO

- Should tuple-sink creation still be coordinated through the current libpq/SQL control path first, with RDMA only carrying tuple batches?
- Does one exact sink map one-to-one to one shard placement sink, or should one sink support multiple colocated placements on the same peer?
- When do we need to expose more than [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1099) / [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1183) to the backend, if ever?
- If the external service owns the peer transport cache, what failure information must still be surfaced back to the backend to preserve current `transactionCritical` semantics?
- Longer term, if transport, routing, and tuple receive/send paths all move into the service cleanly, can remote SQL/session/transaction control also migrate there, or should it remain permanently split from the tuple data plane?

## Related

- [`connection_management_control_plane.md`](/data/dbcomm/postgres-citus/docs/kb/citus/connection-management/connection_management_control_plane.md): grounded current behavior.
- [`cross_node_service_to_service_control_path.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/connection-management/cross_node_service_to_service_control_path.md): concrete next-step control-path plan that transport design must serve.
- [`intermediate_results_service_future_directions.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/intermediate-results/intermediate_results_service_future_directions.md): tuple-service data-plane design space using this control-plane split.
