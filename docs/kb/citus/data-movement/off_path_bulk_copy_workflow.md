# Off-path bulk movement: distribute local table data into shards (COPY-based transport)

## Scope

This doc captures the **off-path bulk data movement** workflow where a local table’s existing rows are copied into shard placements during table distribution.

It ties the earlier “off-path uses COPY” characterization to concrete coordinator + worker interactions in Citus/Postgres.

Primary timers (overview):

- Coordinator-side orchestration + scan: `CreateCitusTable_`, `CopyFromLocalTableIntoDistTable_`, `DoCopyFromLocalTableIntoShards_`
- Per-tuple send pipeline: `CitusSendTupleToPlacements_` → `SerializeAndCopyRow_` + `SendCopyDataToPlacement_`
- Optional local placement path: `WriteTupleToLocal_`, `DoLocalCopy_`
- Worker-side ingest: Postgres `Receiver_CopyFrom` / `NextCopyFrom_` / `CopyFromInsertIntoTable` (when workers receive COPY streams)

## Coordinator-side workflow (Citus)

### 1) Entry point: `CreateCitusTable()` decides whether to copy local data

- `CreateCitusTable(...)` is timed by `CreateCitusTable_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1114`, end at `:1384`).
- Local-data copy is invoked via `CopyLocalDataIntoShards(relationId)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1371`.

### 2) Coarse “copy local -> shards” wrapper: `CopyFromLocalTableIntoDistTable()`

- `CopyFromLocalTableIntoDistTable(...)` starts `CopyFromLocalTableIntoDistTable_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2696`.
- It returns early for partitioned tables (ends `CopyFromLocalTableIntoDistTable_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2710`).
- Normal completion ends `CopyFromLocalTableIntoDistTable_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2778`.

Key implementation details:

- This is **not** a user-visible `COPY table ...` command. Instead, it performs a heap scan and uses a Citus DestReceiver to speak COPY to workers.
- It constructs a DestReceiver with `CreateCitusCopyDestReceiver(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2755`.
- It initializes/shuts down the receiver via:
  - `copyDest->rStartup(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2762`
  - `copyDest->rShutdown(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2768`
  - `copyDest->rDestroy(...)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2769`

### 3) Tuple scan loop: `DoCopyFromLocalTableIntoShards()`

- The per-tuple scan loop is timed by `DoCopyFromLocalTableIntoShards_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2792` … `:2819`).
- Inside the loop, each tuple is handed to the DestReceiver via `copyDest->receiveSlot(slot, copyDest)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2809`.

## Per-tuple send pipeline: Citus COPY DestReceiver (`multi_copy.c`)

The DestReceiver implementation is:

- `CitusCopyDestReceiverReceive(...)` defined at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2243`.

For each tuple/slot, the receiver:

1) Determines shard routing (e.g., `ShardIdForTuple(...)`) and per-shard/per-placement state.
2) Optionally buffers or writes local-placement data:
   - `WriteTupleToLocal_` around local handling (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2335`, `:2350`, and shutdown at `:2683`).
   - The local “flush to shard” implementation uses `WriteTupleToLocalShard(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c:70`) and ultimately `DoLocalCopy(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/local_multi_copy.c:164`).
3) Sends data to remote placements over COPY:
   - `CitusSendTupleToPlacements_` wraps the per-placement loop (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2358` … `:2456`).
   - `SerializeAndCopyRow_` wraps `AppendCopyRowData(...)` + buffer append (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2419` and `:2439`).
   - `SendCopyDataToPlacement_` wraps the send path (`/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2379`, `:2391`, `:2448`).
   - COPY command setup is driven by `StartPlacementStateCopyCommand(...)` (called at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2393`, defined at `:4020`).

At end-of-copy, any remaining local buffered tuples are flushed:

- `DoLocalCopy_` wraps `FinishLocalCopy(copyDest)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2692` … `:2694`.

## Worker-side behavior (Postgres)

For *remote* placements, the coordinator sends a normal Postgres COPY stream over libpq:

- The worker receives a `COPY ... FROM STDIN` command targeting a shard relation and ingests the stream using standard Postgres COPY FROM code paths.

Relevant Postgres timing spots on the worker side include:

- `Receiver_CopyFrom` (`/data/dbcomm/postgres-citus/src/backend/commands/copy.c:307`)
  - `NextCopyFrom_` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:997`)
    - `CopyFrom_CopyGetData` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:252`)
  - `CopyFromInsertIntoTable` (`/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1187`)

## Accuracy of the earlier characterization (off-path)

The writeup’s characterization is accurate in the important sense:

- Off-path bulk movement **piggybacks on COPY** for serialization + protocol framing + IO.

But it is worth being precise about what is “COPY” here:

- The coordinator is not necessarily executing `COPY table ...` to *read* local data.
- Instead, it **heap-scans** the local table and uses a DestReceiver that speaks COPY to workers as the transport.

