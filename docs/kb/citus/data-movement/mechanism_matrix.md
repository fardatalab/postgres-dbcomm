# Citus data-movement mechanism matrix

## Scope

- **What this doc explains**: For each major Citus data-movement scenario, which sending mechanism is used on the producer side, what the receiver side consumes, and whether the path is based on PostgreSQL `DataRow` or PostgreSQL/Citus `COPY`.
- **What this doc does NOT cover**: Full step-by-step workflow details for each scenario. Those stay in the sibling workflow docs.
- **Primary directory**: `docs/kb/citus/data-movement/`

## Mechanism matrix

### Worker query results streamed to coordinator

- **Scenario**: normal distributed execution where workers return rows to the coordinator
- **Producer-side send mechanism**:
  - worker backend uses PostgreSQL query-result protocol via [`printtup()`](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c#L746)
  - this is FE/BE `DataRow`, not COPY
- **Receiver-side mechanism**:
  - coordinator backend uses frontend libpq in [`ReceiveResults()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L3996)
  - receive is driven by [`PQgetResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c#L4026)
- **Format family**: PostgreSQL `DataRow`

### Distributed `COPY FROM` into shard placements

- **Scenario**: client issues `COPY` into a distributed table
- **Producer-side send mechanism**:
  - ingress parsing is PostgreSQL COPY via [`BeginCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c#L1389) and [`NextCopyFrom()`](/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c#L862)
  - Citus forwards tuples to placements through [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274)
  - network send uses [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)
- **Receiver-side mechanism**:
  - worker side consumes PostgreSQL COPY
- **Format family**: PostgreSQL COPY

### Off-path local-table distribution into shards

- **Scenario**: converting or copying a local table into distributed shards
- **Producer-side send mechanism**:
  - scan feeds tuples into [`CitusCopyDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2243)
  - hot send path is [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2274)
- **Receiver-side mechanism**:
  - worker side consumes COPY rows for shard insertion
- **Format family**: PostgreSQL/Citus COPY transport

### Push/broadcast intermediate results

- **Scenario**: coordinator or upstream executor broadcasts an intermediate result set
- **Producer-side send mechanism**:
  - [`RemoteFileDestReceiverReceive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L405)
  - row serializer is [`AppendCopyRowData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L1467)
  - network send is [`SendCopyDataOverConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L567) -> [`PutRemoteCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c#L738)
- **Receiver-side mechanism**:
  - worker intercepts [`ProcessCopyStmt()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2988)
  - worker writes opaque COPY payload to file via [`ReceiveQueryResultViaCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L623) and [`RedirectCopyDataToRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L51)
- **Format family**: Citus intermediate-result COPY format carried in PostgreSQL COPY protocol

### Pull/fetch intermediate results

- **Scenario**: consumer node pulls an intermediate-result file from another node
- **Producer-side send mechanism**:
  - source node serves `COPY ... TO STDOUT WITH (format result)` in [`FetchRemoteIntermediateResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1003)
  - source send path is [`SendQueryResultViaCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L605) and [`SendRegularFile()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c#L111)
- **Receiver-side mechanism**:
  - target node uses libpq [`PQgetCopyData()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1116) in [`CopyDataFromConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c#L1100)
  - received COPY chunks are written to a local file
- **Format family**: Citus intermediate-result COPY format carried in PostgreSQL COPY protocol

### Reading an intermediate-result file back into tuples

- **Scenario**: later executor step consumes an intermediate-result file
- **Producer-side send mechanism**:
  - none; file already contains serialized COPY-format rows
- **Receiver-side mechanism**:
  - [`ReadFileIntoTupleStore()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L447) uses [`BeginCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L471) and [`NextCopyFrom()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/multi_executor.c#L480)
- **Format family**: PostgreSQL/Citus COPY parsing from file

### Reference-table replication and shard transfer / rebalance

- **Scenario**: off-path maintenance movement
- **Producer-side send mechanism**:
  - block-writes mode uses COPY through [`TransferShards()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c#L472) and [`CopyShardTablesViaBlockWrites()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c#L1832)
  - logical-replication mode uses [`CopyShardTablesViaLogicalReplication()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/shard_transfer.c#L1760)
- **Receiver-side mechanism**:
  - depends on chosen transfer mode
- **Format family**:
  - block-writes mode: COPY
  - logical-replication mode: logical replication

## Bottom line

- **Normal worker query results to coordinator**: PostgreSQL `DataRow` / `printtup`
- **Distributed COPY ingestion and most tuple shuffling / intermediate-result movement**: PostgreSQL/Citus COPY family
- **Intermediate-result files**: serialized COPY-format rows persisted to disk, then later parsed again with PostgreSQL COPY readers
- **Some off-path shard movement**: logical replication instead of COPY
