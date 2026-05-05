# Off-path local-table distribution: buffer ownership, allocation, and copy costs

## Scope

- **What this doc explains**: The sender-side and receiver-side memory/buffer workflow for distributing a local table into shard placements, with emphasis on serialization, COPY transport, allocations, and copy boundaries.
- **What this doc does NOT cover**: End-to-end correctness semantics, metadata locking, or non-COPY shard transfer modes such as logical replication.
- **Primary directory**: `docs/kb/citus/data-movement/`

## Why this exists (context)

The current research focus is reducing memory copying and allocation on the hot data path. For this workflow, the question is not just "does it use COPY?", but:

- where the tuple bytes are materialized
- where Citus adds extra buffers beyond PostgreSQL COPY itself
- which copies are protocol-forced and which are purely implementation-side

Related future-direction note: [`intermediate_results_service_future_directions.md`](../../future-directions/citus/intermediate-results/intermediate_results_service_future_directions.md) captures the tuple-service / RDMA redesign space that could replace this COPY-byte path.
Concrete first-prototype design note: [`off_path_local_table_rdma_tuple_service_first_prototype.md`](../../future-directions/citus/data-movement/off_path_local_table_rdma_tuple_service_first_prototype.md).
Current implementation-progress note: [`local_batch_materialization_checkpoint.md`](../../implementations/citus/tuple-route/local_batch_materialization_checkpoint.md).

## Key code pointers

- [`CopyFromLocalTableIntoDistTable()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c#L2687) sets up the off-path copy-local-table workflow
- [`DoCopyFromLocalTableIntoShards()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c#L2790) scans the source table and hands tuples to the DestReceiver
- [`CitusCopyDestReceiverStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1994) initializes COPY output state and per-column output functions
- [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274) is the sender-side hot path
- [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467) serializes one row into COPY format
- [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1119) hands serialized bytes to libpq
- [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738) wraps `PQputCopyData()`
- [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4020) starts `COPY ... FROM STDIN` on the worker and sends binary headers if needed
- [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3568) allocates per-placement buffers
- [`ReceiveCopyBegin()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L172) starts PostgreSQL COPY FROM STDIN on the worker
- [`CopyGetData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L247) receives raw `CopyData` chunks from the coordinator
- [`CopyReadBinaryData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L709) copies from COPY input buffers into caller-provided buffers
- [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862) parses one incoming row
- [`CopyReadBinaryAttribute()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L1994) copies one binary field into `attribute_buf` and calls `ReceiveFunctionCall()`
- local-placement variant:
  - [`WriteTupleToLocalShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L69)
  - [`DoLocalCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L203)
  - [`ReadFromLocalBufferCallback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L258)

## Current behavior / grounded findings

### Workflow split

For remote placements, the workflow is:

1. coordinator scans local heap tuples
2. Citus serializes each tuple into COPY-format row bytes
3. libpq buffers and sends `CopyData` to the worker
4. worker PostgreSQL COPY FROM receives `CopyData`
5. worker PostgreSQL COPY parser reconstructs typed values and inserts the row

For local placements, Citus still serializes into COPY-format bytes first, but then feeds those bytes back into PostgreSQL COPY FROM through a callback rather than through the network.

That local path is already a clue that the current code chooses COPY-format reuse over a direct tuple handoff, even when networking is absent.

## Architecture / data flow

### Sender side: coordinator scan to libpq output

[`DoCopyFromLocalTableIntoShards()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c#L2790) performs a heap scan and calls [`copyDest->receiveSlot(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c#L2809) for each tuple.

The hot send path inside [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274) is:

- switch into per-tuple executor context at [`multi_copy.c:2289`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2289)
- [`slot_getallattrs(slot)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2291)
- route tuple to a shard via [`ShardIdForTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2296)
- serialize with [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2422) or [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2442)
- send serialized bytes with [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2450) -> [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)

The reusable serialization buffer is [`copyOutState->fe_msgbuf`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2076).

### Extra Citus buffering when multiple placements share a connection

The key Citus-specific extra buffering layer is not in PostgreSQL COPY itself. It is in the per-placement buffer [`CopyPlacementState->data`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L205), allocated in [`InitializeCopyShardState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3649).

When a placement is not the active one on a shared connection, [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2413) does:

- serialize row into [`copyOutState->fe_msgbuf`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2416)
- copy that serialized row into [`currentPlacementState->data`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2425)

That is an extra Citus-side copy that does not exist in the one-connection-per-placement case.

When the placement later becomes active, Citus sends the buffered bytes from [`currentPlacementState->data`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2405) and resets that `StringInfo`.

### libpq send side

[`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1119) forwards `dataBuffer->data` and `dataBuffer->len` to [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738).

[`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L750) calls `PQputCopyData()`. That means the sender-side chain is:

- tuple -> temporary per-attribute output objects inside `AppendCopyRowData()`
- per-attribute output objects -> contiguous row bytes in `fe_msgbuf`
- optionally `fe_msgbuf` -> per-placement `placementState->data`
- final row/chunk bytes -> libpq `PGconn` output buffer via `PQputCopyData()`
- libpq output buffer -> socket

### Worker receive side: network to PostgreSQL COPY parser

On the worker, the coordinator’s `COPY ... FROM STDIN` is handled by standard PostgreSQL COPY FROM. [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4020) starts the remote COPY command and checks for [`PGRES_COPY_IN`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L4036).

The worker-side receive chain then is:

- `CopyData` arrives in backend transport buffer
- [`pq_getmessage(cstate->fe_msgbuf, ...)`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L305) copies one `CopyData` message body into [`cstate->fe_msgbuf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L186)
- [`pq_copymsgbytes(...)`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L342) copies from `fe_msgbuf` into the destination requested by [`CopyGetData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L247)

For the binary path used by this workflow when [`copyOutState->binary`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2075) is true:

- [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862) reads the row field count and iterates attributes
- [`CopyReadBinaryAttribute()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L1994) resets and enlarges [`attribute_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2015)
- [`CopyReadBinaryData()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2019) copies field bytes into `attribute_buf`
- [`ReceiveFunctionCall()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2029) turns that field buffer into a typed `Datum`

So the worker binary receive path is usually:

- socket -> backend receive buffer
- backend receive buffer -> `cstate->fe_msgbuf`
- `cstate->fe_msgbuf` -> `raw_buf` / caller scratch via `CopyGetData()`
- `raw_buf` -> `attribute_buf` per field
- `attribute_buf` -> typed `Datum`

That is more copying than a superficial “binary COPY is zero-copy” reading would suggest.

### Local-placement variant

For local placements, [`WriteTupleToLocalShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L69) still serializes the row into [`localCopyOutState->fe_msgbuf`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L88).

Then [`DoLocalCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L203) calls PostgreSQL [`BeginCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L222) with [`ReadFromLocalBufferCallback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L258), which copies out of the serialized local buffer with [`memcpy_s(...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L265).

So the local path avoids the network, but it still does:

- tuple -> COPY-format `StringInfo`
- `StringInfo` -> PostgreSQL COPY FROM parser callback
- COPY parser -> typed `Datum` -> insert

It does not bypass serialization/deserialization.

## Behavior details

### Sender-side allocations

Sender-side hot-path allocations come from:

- per-tuple executor context in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2288)
- type output/send inside [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467)
- optional coercion path execution before serialization
- `StringInfo` growth in [`copyOutState->fe_msgbuf`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2076)
- `StringInfo` growth in per-placement [`placementState->data`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L3651) when buffering is active

Like PostgreSQL `printtup`, most per-row temporary allocations are reclaimed by resetting the executor per-tuple context, but the serialized row bytes in `fe_msgbuf` and `placementState->data` are persistent/reusable buffers.

### Receiver-side allocations

Worker-side PostgreSQL COPY FROM allocates a long-lived [`copycontext`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1428) and several reusable buffers in [`BeginCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1389):

- [`raw_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1588)
- [`input_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1601) in transcoding text mode
- [`line_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1608) in text mode
- [`attribute_buf`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1611)
- per-column input function arrays [`in_functions`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1629) and [`typioparams`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1630)

The row-level temporary lifetime is managed by the executor per-tuple context switched in PostgreSQL [`CopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L990).

## Where the extra copies are

### Mostly unavoidable with current protocol/API shape

- per-attribute type output object -> serialized row bytes in `AppendCopyRowData()`
- libpq/transport buffering on the sender side
- worker-side FE/BE `CopyData` message extraction into `cstate->fe_msgbuf`
- worker-side binary field copy into `attribute_buf` before `ReceiveFunctionCall()`

These follow from the current PostgreSQL COPY and type-I/O interfaces.

### Citus-specific or at least more avoidable

- `copyOutState->fe_msgbuf` -> `placementState->data` copy when multiple placements share one connection
- local-placement path serializing into COPY bytes and then immediately reparsing them through local COPY FROM

Those are the highest-signal candidates if the goal is to cut copies without redesigning PostgreSQL COPY itself.

## Future directions / design ideas

### Direction 1: eliminate the extra Citus-side rebuffering when a connection is shared

- **Status**: `recommended first experiment`
- **Goal**: avoid serializing into `copyOutState->fe_msgbuf` and then copying again into `placementState->data`
- **Grounding in current code**:
  - current buffered-placement path copies serialized row bytes at [`multi_copy.c:2425`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2425)
- **Candidate approaches**:
  - serialize directly into the destination placement buffer when the placement is not active
  - maintain one reusable serialization buffer per placement instead of a global `fe_msgbuf` plus copy-into-placement-buffer
- **Required interface change**:
  - this is not just a buffer swap; [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467) currently writes through `CopyOutState`
  - removing the implicit [`copyOutState->fe_msgbuf`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2076) dependency requires a destination-oriented serializer API, e.g. “append this row into this specific buffer”
- **Pros**:
  - local Citus-only change
  - removes one full-row memcpy on the shared-connection path
- **Cons**:
  - more per-placement buffer state
  - still does not remove type-I/O temporaries or libpq copies
  - serializer refactoring is required; it is not a trivial local patch

### Direction 2: batch multiple rows per `PQputCopyData()` call even on active placements

- **Status**: `recommended`
- **Goal**: reduce libpq call overhead and encourage larger network writes
- **Grounding in current code**:
  - active placement path often sends one serialized row at a time via [`SendCopyDataToPlacement(copyOutState->fe_msgbuf, ...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2450)
- **Candidate approaches**:
  - give active placements their own accumulation buffer and flush by threshold rather than per row
  - keep binary headers/footers and copy-command boundaries the same
- **Required interface change**:
  - if `fe_msgbuf` is removed from the hot path, active-placement batching should use the same destination-oriented serializer contract as Direction 1
- **Pros**:
  - preserves PostgreSQL COPY semantics
  - likely improves both CPU efficiency and socket write behavior
- **Cons**:
  - increases buffering latency
  - needs careful coordination with active-placement switching and error handling

### Direction 3: special-case the one-connection-per-placement case harder

- **Status**: `recommended`
- **Goal**: optimize the common case described in [`multi_copy.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L160), where no placement sharing happens
- **Grounding in current code**:
  - the current code still routes through the generic active/buffered placement logic
- **Candidate approaches**:
  - separate a fast path where the placement is known active and no switch-over buffering is possible
  - pre-open COPY once, keep a per-connection batch buffer, flush only on threshold/end
- **Pros**:
  - targets the hot case
  - avoids extra branchy control logic on every tuple
- **Cons**:
  - code duplication unless structured carefully

### Direction 4: local-placement fast path that bypasses COPY reparse

- **Status**: `high upside, more invasive`
- **Goal**: avoid serialize-to-COPY then reparse-from-COPY when sender and receiver are the same backend
- **Grounding in current code**:
  - [`WriteTupleToLocalShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L69) serializes
  - [`DoLocalCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c#L203) immediately reparses with PostgreSQL COPY FROM
- **Candidate approaches**:
  - direct tuple insert path for local placements using already-available `TupleTableSlot`
  - or direct conversion into target slot values without round-tripping through COPY text/binary
- **Pros**:
  - removes an entire serialize/deserialize loop for local shards
- **Cons**:
  - loses the nice code reuse and behavior matching of COPY
  - must preserve trigger/constraint/default behavior expected from COPY

### Direction 5: PostgreSQL COPY receive-side surgery

- **Status**: `possible but not the first target`
- **Goal**: remove copies on the worker-side `COPY FROM STDIN` parser path
- **Grounding in current code**:
  - worker-side copies go through [`pq_getmessage()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L305), [`pq_copymsgbytes()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L342), and [`CopyReadBinaryAttribute()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L2019)
- **Candidate approaches**:
  - more direct parsing from `fe_msgbuf`
  - streaming binary typreceive APIs that avoid `attribute_buf`
- **Pros**:
  - larger theoretical gain on the worker ingest side
- **Cons**:
  - invasive PostgreSQL core changes
  - much broader blast radius than a Citus-side buffering fix

## Bottom line

The highest-signal findings are:

- this workflow is already binary COPY when possible, so it has already picked the better PostgreSQL family
- the main extra Citus-side copy is `fe_msgbuf` -> `placementState->data` on shared connections
- the main structural inefficiency on local placements is the COPY round-trip inside the same backend
- the worker-side COPY FROM parser still performs several unavoidable copies under the current PostgreSQL interfaces

So the best first optimization targets are:

1. remove or reduce Citus-side rebuffering for shared connections
2. batch more rows per send on active placements
3. consider a direct local-placement fast path

Only after that does it make sense to consider invasive PostgreSQL COPY receive-side changes.

## Interactions and cross-links

- [`connection_management_control_plane.md`](/data/dbcomm/postgres-citus/docs/kb/citus/connection-management/connection_management_control_plane.md) is the canonical control-plane note for the four layers that set up the remote session, placement-safe connection choice, remote transaction state, and active COPY sink before this workflow pushes tuple bytes.
- [`off_path_bulk_copy_workflow.md`](/data/dbcomm/postgres-citus/docs/kb/citus/data-movement/off_path_bulk_copy_workflow.md) is the higher-level workflow note for this path
- [`push_pull_intermediate_results.md`](/data/dbcomm/postgres-citus/docs/kb/citus/intermediate-results/push_pull_intermediate_results.md) covers the other major COPY-family movement path in Citus
- [`datarow_vs_copy_and_citus_transport.md`](/data/dbcomm/postgres-citus/docs/kb/postgres/serialization/datarow_vs_copy_and_citus_transport.md) is the canonical protocol-family distinction note
