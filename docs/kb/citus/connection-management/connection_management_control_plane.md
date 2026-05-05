# Original Citus control plane: four facets, API layers, and workflow slices

## Scope

- **What this doc explains**: the original Citus control plane as it exists today, broken into four semantic facets plus the shared libpq execution substrate underneath them.
- **What this doc does NOT cover**: tuple serialization costs, worker-side PostgreSQL COPY parsing internals, or the final RDMA/external-service design.
- **Primary directory**: `docs/kb/citus/connection-management/`

## Why this exists

The current tuple-route / RDMA prototype work started from the data plane first. That exposed a design gap: the prototype had a workable local tuple path, but it did not yet model the control plane at the same semantic level as original Citus.

From first principles, original Citus must answer four different questions before tuple bytes move:

1. which remote PostgreSQL session should this operation use?
2. which placement or colocated access must stay on which session?
3. what remote SQL transaction / command state must exist on that session?
4. for protocol-shaped workflows like COPY, which operation-specific sink is active on that session right now?

Current Citus answers those questions in separate layers. That split is why `MultiConnection` is not just "a socket", and it is also why the current tuple-route prototype's exact tuple-sink key [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L50) is not yet the right long-term abstraction. Today it is mostly acting as an open-time exact-sink rendezvous key for the prototype, not as a clean semantic replacement for the original control plane.

## Key code pointers

- transport/session cache:
  - [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L240)
  - [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L162)
  - [`GetNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L195)
  - [`StartNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L208)
  - [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277)
  - [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466)
  - [`WaitEventSetFromMultiConnectionStates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L879)
  - [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L973)
  - [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1353)
- placement/co-location routing:
  - [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281)
  - [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368)
  - [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4017)
- remote transaction / command state:
  - [`RemoteTransaction`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h#L61)
  - [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
  - [`RemoteTransactionsBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L879)
  - [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1018)
- shared command/COPY IO substrate:
  - [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565)
  - [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
  - [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)
  - [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822)
- operation-specific sink state for COPY:
  - [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165)
  - [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L190)
  - [`ConstructCopyStatement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1030)
  - [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2303)
  - [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4294)
  - [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4329)

## First-principles summary

Current Citus control-plane state is not one monolith, and it is not a strictly nested stack either. It is better understood as a layered control graph:

1. peer/session transport cache
2. placement/co-location routing and conflict safety
3. remote SQL transaction and command state
4. operation-specific sink state

The nonblocking libpq send/flush/wait engine in [`remote_commands.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1) executes parts of facets 3 and 4, but it is not itself a semantic control-plane facet. It is the shared communication substrate.

That distinction matters:

- semantic layers answer "what logical state must exist?"
- the libpq substrate answers "how do we drive socket IO and result readiness?"
- facet 1 is the base capability
- facet 2 and facet 3 both depend on facet 1, but neither is simply "under" the other in all workflows
- facet 4 depends on facet 1 and usually facet 3, and in placement-bound flows it also depends on facet 2

## The four facets

### Facet 1: peer/session transport cache

- **Responsibility**: cache and reuse remote PostgreSQL sessions keyed by SQL-session identity, not by task or tuple stream.
- **Primary state**:
  - [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L240) keys by hostname, port, user, database, and replication mode.
  - [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L162) carries the libpq handle plus session-lifetime bookkeeping.
- **Top-layer APIs**:
  - [`GetNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L195)
  - [`StartNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L208)
  - [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277)
- **Internal communication-stack APIs**:
  - [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466)
  - [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1353)
  - [`WaitEventSetFromMultiConnectionStates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L879)
  - [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L973)
- **Why this is split out**:
  - connection establishment is expensive and asynchronous
  - the same remote PostgreSQL session can be reused across many operations
  - the cache key is about SQL-session compatibility, not about one data-flow route

### Facet 2: placement/co-location routing and conflict safety

- **Responsibility**: decide which cached session is safe to use for a placement access, so Citus preserves read-your-own-writes and avoids conflicting placement usage.
- **Primary state**:
  - placement-to-connection associations built by [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368)
  - COPY-path placement choice in [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4017)
- **Top-layer APIs**:
  - [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281)
  - workflow-specific helpers such as [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4017) inside the COPY path
- **Internal communication-stack APIs**:
  - [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368) is lower-level bookkeeping under the public placement-selection path
- **Why this is split out**:
  - the correctness rule is not "pick any connected socket"
  - the rule is "pick a session consistent with prior accesses to the same placement or colocated group"
  - this is a DB-engine correctness layer sitting above raw transport reuse

### Facet 3: remote SQL transaction and command state

- **Responsibility**: ensure the chosen remote session is in the right SQL transaction state and then drive remote commands against it.
- **Primary state**:
  - per-session transaction state in [`RemoteTransaction`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h#L61)
  - lazy begin / critical-failure semantics in [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859) and [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1018)
- **Top-layer APIs**:
  - [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
  - [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1018)
  - [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565)
  - [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
- **Internal communication-stack APIs**:
  - [`RemoteTransactionsBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L879)
  - the lower-level begin/commit/abort helpers declared in [`remote_transaction.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h#L99)
  - [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822), which does the actual nonblocking flush/consume/wait work
- **Why this is split out**:
  - a cached session may or may not already be inside a remote transaction
  - SQL correctness and 2PC/error semantics belong to the DB-engine layer, not to transport setup
  - multiple higher-level workflows reuse the same remote transaction state machine

### Facet 4: operation-specific sink state

- **Responsibility**: represent the operation currently active on top of an already chosen remote session. For COPY, this means "which `COPY ... FROM STDIN` sink is active on this connection?"
- **Primary state**:
  - per-connection COPY state in [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165)
  - per-sink placement state in [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L190)
- **Top-layer APIs**:
  - workflow entry in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2303)
  - sink open/close in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4294) and [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4329)
- **Internal communication-stack APIs**:
  - [`ConstructCopyStatement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1030), which materializes the exact remote sink command
  - [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738), which pushes COPY bytes on the wire
- **Why this is split out**:
  - a single remote session can only have one active COPY sink at a time
  - COPY therefore needs explicit sink-open, sink-close, buffering, and switchover state
  - this is not transport reuse and not transaction begin; it is protocol-shaped operation state

## Shared communication substrate under control and data concerns

[`remote_commands.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1) is the shared nonblocking libpq substrate.

It is used by:

- facet 3 for SQL command send/result receive through [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
- facet 4 for COPY byte pushing through [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)
- higher-level data movement more generally, because remote queries, command results, and COPY payload all share the same session/socket machinery

The common execution engine is [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822).

This is why it is better to think of `remote_commands.c` as a communication-stack substrate, not as a separate semantic control-plane facet and not as "control plane only". It underlies both orchestration traffic and payload movement.

## Workflow slices

### Workflow A: simple remote SQL / metadata command

Typical shape:

1. acquire or reuse a remote session with [`GetNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L195) or [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277)
2. ensure remote transaction state with [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859) if the command is transaction-scoped
3. send the command with [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565)
4. receive the result with [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)

Facets used:

- facet 1: yes
- facet 2: sometimes skipped if no placement-specific safety is needed
- facet 3: yes
- facet 4: no

This is the baseline example showing that original Citus control plane is not inherently COPY-specific.

### Workflow B: placement-bound command or DML

Typical shape:

1. pick a safe connection for one or more placement accesses with [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281)
2. record placement/connection association via [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368)
3. begin remote transaction if necessary with [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
4. send remote SQL with [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565)

Facets used:

- facet 1: yes
- facet 2: yes
- facet 3: yes
- facet 4: no

This is the clearest example that placement safety is a distinct control-plane concern above transport/session reuse.

### Workflow C: off-path local-table distribution into shards via COPY

Typical shape in the current coordinator path:

1. source tuples reach [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2303)
2. the shard and placement state are established inside [`GetShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3811)
3. remote placement connections are chosen through [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4017)
4. if needed, remote transaction state is initialized through [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
5. the remote shard sink is opened with [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4294), which builds shard-specific SQL in [`ConstructCopyStatement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1030)
6. serialized COPY rows are pushed through [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)
7. the sink is closed with [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4329)

Facets used:

- facet 1: yes
- facet 2: yes
- facet 3: yes
- facet 4: yes

This workflow is the main reason the fourth facet matters. COPY is not only "bytes on a connection"; it is "bytes on a connection while a specific remote COPY sink is active".

### Workflow D: distributed query / result-streaming execution

Representative query-execution paths do not use COPY sink state. They still need the earlier facets:

1. session acquisition through APIs such as [`GetNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L195) or other `MultiConnection` acquisition helpers
2. placement-safe reuse through [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281) when shard-placement access matters
3. remote transaction/command orchestration through [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859), [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565), and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
4. result materialization then uses libpq result retrieval instead of COPY sink open/close

Facets used:

- facet 1: yes
- facet 2: often yes
- facet 3: yes
- facet 4: no

This is why the top-level abstraction cannot be only "placement plus COPY sink". Some flows are node/session/query oriented rather than sink oriented.

## Which APIs are exposed upward vs kept lower in the stack

### APIs commonly used by upper Citus workflow code

- session acquisition:
  - [`GetNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L195)
  - [`StartNodeConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L208)
  - [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L277)
- placement-safe connection choice:
  - [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281)
  - workflow-private helpers such as [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4017)
- remote transaction and command state:
  - [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
  - [`MarkRemoteTransactionCritical()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L1018)
  - [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565)
  - [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)
- COPY workflow layer:
  - [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2303)
  - [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4294)
  - [`EndPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4329)

### APIs that are mostly lower-level communication-stack or bookkeeping machinery

- transport/cache internals:
  - [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466)
  - [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1353)
  - [`WaitEventSetFromMultiConnectionStates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L879)
  - [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L973)
  - [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1293)
  - [`UnclaimConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1306)
- lower-level transaction state machine:
  - [`RemoteTransactionsBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L879)
  - start/finish helpers declared in [`remote_transaction.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h#L99)
- communication substrate:
  - [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822)
  - libpq-level flush/input/busy loops hidden under it
- COPY protocol bookkeeping:
  - [`ConstructCopyStatement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1030)
  - buffering/switchover state in [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165) and [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L190)

## What the current top-level abstraction really is

The DB-engine-facing abstraction in original Citus is not one universal object like "route" or even "connection". At the top layer, current workflow code usually expresses an execution intent roughly like:

- I need a remote PostgreSQL session compatible with `(host, port, user, database, mode)`
- I may need it to be the safe session for one or more placement accesses
- I may need a remote transaction or remote SQL command context on that session
- I may need a specific operation sink on that session, such as `COPY ... FROM STDIN`

That means the current top-layer abstraction is really closer to:

- **execution intent over a remote session**, refined by placement and operation semantics

and not merely:

- "reach this node/placement"

For example:

- Workflow A uses mainly remote-session plus command semantics.
- Workflow B adds placement-safety constraints.
- Workflow C adds an operation sink (`COPY ... FROM STDIN`) on top.
- Workflow D uses remote-session plus query/result semantics without COPY sink state.

So a future unified abstraction should cover:

1. target scope
   - node/session
   - placement/co-location access
   - operation sink
2. semantic constraints
   - remote transaction state needed or not
   - metadata connection constraints
   - exclusivity / reuse policy
3. operation kind
   - remote SQL command
   - COPY ingest sink
   - query/result stream
   - future tuple route / tuple sink

In other words, the future abstraction should be **intent-oriented**, not just "get connection" and not just "open route".

## Round trips in representative workflows

The exact count depends on connection warmth and authentication setup, but the current shape is:

### Workflow A: simple remote command on an existing healthy session

- `0` RTT for placement routing if no placement-safe lookup is needed; that part is local bookkeeping
- `0` RTT for connection setup if the session is already cached and connected
- `0` or `1` RTT for remote transaction begin via [`RemoteTransactionBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L859)
- about `1` RTT for command/result through [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L565) and [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683)

### Workflow B: placement-bound command/DML on an existing healthy session

- placement choice and connection association in [`StartPlacementListConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L281) and [`AssignPlacementListToConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L368) are local CPU work, not network RTTs
- then the same RTT pattern as Workflow A:
  - optional `BEGIN` RTT
  - command/result RTT

### Workflow C: off-path COPY on an existing healthy session

- placement choice is still local bookkeeping
- `0` or `1` RTT for remote transaction begin
- about `1` RTT to open the remote COPY sink in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4294), because it sends the `COPY ... FROM STDIN` command and waits for `PGRES_COPY_IN`
- the row stream itself is pipelined by [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738), so it is not one request/response RTT per tuple
- about `1` RTT to end the COPY and get the final result in [`EndRemoteCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1166)
- if multiple placements share a connection, every sink switchover effectively adds a sink-close plus sink-open sequence

### Cold-session penalty

If the session is not cached, connection establishment in facet 1 adds the PostgreSQL/libpq startup handshake cost before any of the above. That is several protocol exchanges hidden behind [`StartConnectionEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1353) and [`FinishConnectionListEstablishment()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L973), and it is usually the most expensive pure-control-plane latency component.

## Current inefficiencies

### 1. One object (`MultiConnection`) carries too many concerns

[`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L162) mixes:

- peer/session identity
- transport readiness and reuse
- remote transaction state
- placement references
- COPY flush accounting

That makes the abstraction convenient for current Citus, but it also makes it hard to replace only one layer cleanly.

### 2. Placement safety and operation-sink state are bolted on separately

The current split between facet 2 and facet 4 is correct semantically, but it means workflows like off-path COPY must carry both:

- placement-safe connection choice
- active COPY sink switching/buffering

That combination causes the buffering/switchover complexity in [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165).

### 3. COPY sink open/close adds extra command-style RTTs

Even after session acquisition and transaction begin, off-path COPY still needs:

- `COPY ... FROM STDIN` open
- `COPY` end/result

This is a protocol-shaped inefficiency relative to a persistent tuple sink.

### 4. Control and data share the same libpq substrate

[`remote_commands.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L1) carries:

- control/orchestration traffic such as `BEGIN`, metadata commands, and query text
- data-plane bytes such as COPY payload

That reuse is pragmatic, but it couples control progress and data flushing to the same session/socket machinery.

### 5. Placement routing is local bookkeeping, but it is backend-local bookkeeping

The placement/co-location safety state currently lives in backend memory and transaction state. That means a future external service cannot take over the full control plane unless it also learns or owns those constraints.

### 6. Heavy COPY flush can delay control progress

This is not just a theoretical coupling. The current code shows two concrete head-of-line patterns, but one correction is important: this is primarily **within one backend's ownership of one active connection**, especially inside one COPY workflow. It is not usually "random small SQL commands interleaving with large COPY payload on the same socket" because current Citus tries hard to avoid that.

First, [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738) accumulates bytes into libpq and, once [`RemoteCopyFlushThreshold`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L39) is exceeded, immediately blocks inside [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822) for that one connection. While the backend is in that loop:

- it keeps trying [`PQflush(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L838)
- it keeps consuming input with [`PQconsumeInput(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L851)
- it sleeps in [`WaitLatchOrSocket(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L869)

During that time, the backend is not progressing unrelated control work on other connections unless the caller later returns to a broader wait loop.

Second, the same substrate is used for command completion. [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L683) also falls into [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L704) when the result is not ready. So one backend alternates between control waits and payload flush waits using the same one-connection loop.

In the COPY workflow that creates a practical tail-latency problem:

- active payload flush on one connection can delay when the backend gets around to opening/switching another COPY sink in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2390)
- shutdown drains buffered placements one by one in [`ShutdownCopyConnectionState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3066), with repeated [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3091), [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3093), and `COPY` end, so late buffered data can hold up completion even though the work is already logically available

What usually **does not** happen in current Citus:

- unrelated upper-layer SQL commands being multiplexed onto the same active COPY socket from the same backend

Why:

- the FE/BE COPY protocol already imposes strong operation-state constraints
- COPY connections are often claimed exclusively via [`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1293), which prevents reuse through the normal connection-acquisition APIs
- even when a connection is reused across multiple placements, the reuse is within the COPY workflow itself through [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165), not by interleaving arbitrary remote commands on the same socket

So the real current HOL story is narrower and more precise:

- same backend
- same remote connection
- same libpq socket/protocol state
- usually same COPY workflow, especially when multiple placement sinks share that connection

This is one of the strongest arguments for splitting control and data transports in the external service.

### 7. Better scheduling is only partial today

Current Citus does have one important optimization: some control transitions are batched across many connections. For example, [`RemoteTransactionsBeginIfNecessary()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c#L879) sends `BEGIN` to all needed connections first and then waits with [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L925), which uses one `WaitEventSet` over many sockets.

But this batching is not a general scheduler. The limitations are:

- it only applies in code paths that explicitly build a connection list and call [`WaitForAllConnections()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L925)
- hot COPY flushing still uses per-connection blocking progress in [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738) -> [`FinishConnectionIO()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L822)
- operation transitions like COPY sink open/close are still driven from per-backend workflow code rather than a central scheduler
- separate backends cannot coordinate with each other at all; each backend has its own connection cache, wait loops, and local prioritization

So current Citus has **batched waits in some places**, not a **global scheduling layer**.

### 8. Per-backend state duplicates work and prevents cross-backend optimization

The current connection/control state is per backend:

- [`InitializeConnectionManagement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L99) creates one backend-local [`ConnectionContext`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L56)
- the connection cache [`ConnectionHash`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L53) is backend-local
- each [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L162) carries its own remote transaction state, placement references, and COPY flush counters
- COPY-specific overlays like [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165) are also backend-local

The overheads of that design are:

- repeated connection/session setup work across different backends
- duplicated placement-association bookkeeping across different backends
- repeated wake/sleep/syscall activity in many backend-local wait loops
- no cross-backend fairness or prioritization; one backend cannot help another drain a hot socket or prioritize a waiting control transition
- higher aggregate memory footprint for caches and protocol state

This also means the strongest socket/protocol HOL effects are mostly per backend today:

- one backend's blocked or busy connection does not literally stall another backend's libpq state machine, because they do not share the same `MultiConnection` cache
- however, the node still sees aggregate contention in NIC bandwidth, kernel scheduling, worker CPU time, and remote PostgreSQL resource usage
- and because each backend schedules only for itself, node-wide prioritization is still poor even if same-socket HOL is backend-local
- current connection reuse therefore happens **within** one backend's cache, not across backends
- the earlier shorthand "same backend / same connection / same workflow" should be read as the strongest current HOL case, not as a universal statement that every workflow always gets a dedicated connection

### 9. `ClaimConnectionExclusively()` protects against same-backend reentry, not cross-backend fighting

[`ClaimConnectionExclusively()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L1293) is still necessary even though connection/session ownership is per backend.

Its job is to prevent **other code paths in the same backend** from reusing the same cached `MultiConnection` while an ongoing operation has effectively pinned it.
This is the missing distinction behind the "per-backend cache" model: one backend still has multiple internal acquisition paths such as placement-safe lookups, adaptive-executor task assignment, nested distributed SQL, and COPY helper flows, and all of them consult the same backend-local connection cache.

It is also stronger and narrower than a generic "connection is in use" bit:

- a connection can already carry meaningful state such as an open remote transaction and still be reusable if current placement/user/flag constraints permit it; [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L466) only rejects active remote transaction state for callers using `OUTSIDE_TRANSACTION`, not for all callers
- placement-safe reuse explicitly relies on such reusable in-transaction sessions through [`GetConnectionIfPlacementAccessedInXact()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L479) and [`CanUseExistingConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L754)
- `claimedExclusively` therefore means "do not hand this cached session out to any other local claimant until release", not "this session has no state" or "this session is the only one touching the remote node"

The current code shows several real uses:

- the connection cache skips claimed sessions in [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c#L487)
- off-path COPY claims new placement connections in [`CopyGetPlacementConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4216) so nested distributed work in the same backend does not accidentally steal them
- that exact motivation is documented in the COPY path comment at [`multi_copy.c:4100`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4100): a function invoked during COPY could otherwise run a distributed query and reuse the same connection
- worker-side shard-copy setup also claims its connection in [`ConnectToRemoteAndStartCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/worker_shard_copy.c#L109)
- adaptive executor claims sessions after assigning tasks in [`adaptive_executor.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L1608)

So `ClaimConnectionExclusively()` is not a top-level semantic workflow abstraction like "open remote COPY sink". It is a lower-level ownership guard inside the communication/control stack.

For the future remote execution session abstraction, this means:

- the backend probably should not call a separate `Claim...` API
- but the abstraction still needs an exclusivity/pinning policy internally
- that policy answers: "once this remote execution session is bound to an active operation, may any other local claimant reuse it before release?"

## Why original Citus is designed this way

### Session reuse must be amortized

Remote PostgreSQL sessions are expensive to open and validate. That is why the transport cache in facet 1 is keyed by SQL-session compatibility through [`ConnectionHashKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h#L240) instead of by one query task or one tuple stream.

### Placement correctness is not a transport problem

Choosing "any working connection to node X" is not enough. Citus must preserve placement affinity and avoid conflicting access patterns, which is why facet 2 is separate and built around placement accesses in [`placement_connection.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/placement_connection.c#L1).

### Remote transaction state must survive connection reuse

The same cached `MultiConnection` may already be inside a remote transaction, may have failed transaction state, or may need to be marked critical. That is why facet 3 is separate and embedded in [`RemoteTransaction`](/data/dbcomm/citus-dbcomm/src/include/distributed/remote_transaction.h#L61), not folded into connection establishment.

### COPY protocol forces explicit sink state

For off-path COPY data movement, one libpq connection can only host one active `COPY FROM STDIN` sink at a time. That is why facet 4 exists and why [`CopyConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L165) keeps `activePlacementState` plus buffered inactive placements.

## Grounded implication for the tuple-route / RDMA design

The current tuple-route prototype should not try to "replace `MultiConnection`" with one route key. The original Citus control plane already shows why that would be too coarse:

- facet 1 is about cached remote SQL sessions
- facet 2 is about placement-safe usage of those sessions
- facet 3 is about remote SQL transaction state on those sessions
- facet 4 is about the currently active operation sink on top of those sessions

So a future tuple-service control plane should likely expose a higher-level `OpenRoute(...) -> routeHandle` abstraction above the transport cache, while keeping in mind that:

- some current semantics belong above transport reuse
- some belong above transaction state
- some are operation-specific sink state

More concretely, a better service-facing abstraction would likely have two layers:

1. **execution-context resolution**
   - input: target scope plus semantic constraints
   - output: opaque context handle representing the chosen remote session / placement-safe binding / transaction policy
2. **operation open**
   - input: context handle plus operation kind and operation-specific sink/query metadata
   - output: opaque operation handle used for command exchange or tuple/data movement

That shape is broader than `OpenRoute(...)`, and it matches the original workflows better:

- query/result execution needs an execution context and a query operation
- off-path COPY needs an execution context and a COPY sink operation
- future tuple service needs an execution context and a tuple-sink operation

This is the direction that would let an external service eventually own all four facets, instead of only owning the transport/data plane while the backend still reconstructs too much control-plane state itself.

## Why offloading the full control plane can improve more than CPU usage

If the external service owns the remote execution session abstraction rather than just the data path, it can improve several things at once:

### 1. Fewer control transitions on the steady-state fast path

The service can keep long-lived remote execution sessions and, where semantics allow, long-lived operation state. That can reduce repeated:

- session establishment
- `BEGIN`/transaction setup
- operation open/close such as COPY sink transitions

### 2. Lower latency by separating control and data transports

Instead of one libpq socket/session carrying everything, the service can use:

- a low-latency control path for opens, acks, errors, transaction transitions, and small metadata messages
- a high-throughput data path for tuple batches or result payload

That directly addresses the head-of-line issue described above.

### 3. Centralized scheduling across all local backend requests

A single service event loop can schedule:

- session creation/reuse
- operation opens/closes
- control acknowledgements
- data sends/receives

across all local backend requests, rather than leaving each backend to wait on its own local subset of connections.

### 4. Fewer duplicated caches and less repeated bookkeeping

The service can centralize:

- remote session compatibility state
- placement-safe bindings
- transaction policy/state
- active operation state

instead of making every backend keep its own copies.

### 5. Better pipelining opportunities

Once control and data are centralized, the service can:

- open contexts proactively
- batch or piggyback control transitions
- keep future operations queued behind already-open sessions
- prioritize short control messages over bulk data flush

Those are hard to do well when all logic is embedded in many independent backends.

That is why the current prototype's route key is best treated as bootstrap scaffolding, not as the final semantic control-plane object.

## Related

- [`off_path_local_table_distribution_buffering_and_copy_costs.md`](/data/dbcomm/postgres-citus/docs/kb/citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md): hot-path COPY buffering and copy boundaries layered on top of this control plane.
- [`rdma_transport_control_plane_abstraction.md`](/data/dbcomm/postgres-citus/docs/kb/future-directions/citus/transport/rdma_transport_control_plane_abstraction.md): future RDMA/service abstraction options grounded in these four facets.
- [`local_batch_materialization_checkpoint.md`](/data/dbcomm/postgres-citus/docs/kb/implementations/citus/tuple-route/local_batch_materialization_checkpoint.md): current tuple-route prototype state, which still uses a bootstrap route rendezvous mechanism rather than a final control plane.
