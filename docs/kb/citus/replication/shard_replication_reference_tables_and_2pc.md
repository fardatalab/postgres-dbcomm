# Citus shard replication, reference tables, and distributed 2PC

## Scope

- **What this doc explains**: what “replication” still means inside this Citus tree, how the internal `REPLICATION_MODEL_*` metadata relates to the now-deprecated public shard-replication surface, and why distributed 2PC remains relevant even when worker-node HA uses PostgreSQL streaming replication.
- **What this doc does NOT cover**: detailed PostgreSQL WAL sender/receiver internals, Citus logical shard move/split workflows, or the current Homer prototype implementation.
- **Primary directory**: `docs/kb/citus/replication/`
- **Doc type**: `factual`

## Why this exists (context)

The user discussion mixed three different ideas under “replication”:

- PostgreSQL physical WAL replication between a primary and standby node
- old Citus shard-replication / placement-replication behavior
- Citus distributed commit coordination with worker-side `PREPARE TRANSACTION` / `COMMIT PREPARED`

Those are not the same mechanism.

Current user-facing Citus docs emphasize PostgreSQL streaming replication for worker-node HA, but this tree still contains the older shard-replication metadata and still uses distributed 2PC in important places. This note records that split so future prototype work does not accidentally conflate “replication” with “distributed commit”.

## Key code pointers

- [`REPLICATION_MODEL_COORDINATOR`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:66), [`REPLICATION_MODEL_STREAMING`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:67), [`REPLICATION_MODEL_2PC`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:68) - internal replication-model enum values stored in Citus metadata.
- [`citus.replication_model`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2404) - deprecated user-facing GUC.
- [`shared_library_init.c:2966`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2966) - warning that setting `citus.replication_model` has no effect.
- [`citus.shard_replication_factor`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2459) - user-facing shard-replication-factor GUC still present in this tree.
- [`DistributedTableReplicationIsEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/node_protocol.c:1052) - current code check for whether shard replication is enabled.
- [`DecideDistTableReplicationModel()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1843) - picks the internal metadata model for a distributed table.
- [`create_distributed_table.c:1647`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1647) - single-shard tables use `REPLICATION_MODEL_STREAMING`.
- [`create_distributed_table.c:1654`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1654) - reference tables use `REPLICATION_MODEL_2PC`.
- [`create_shards.c:129`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:129) - rejects `REPLICATION_MODEL_STREAMING` when `replicationFactor > 1`.
- [`CreateReferenceTableShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:325) - creates the single reference-table shard and replicates it to all active nodes.
- [`create_shards.c:322`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:322) and [`create_shards.c:363`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:363) - reference tables replicate to all active nodes and derive replication factor from active-node count.
- [`metadata_cache.c:929`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_cache.c:929) - internal check identifying `REPLICATION_MODEL_2PC` tables as reference tables.
- [`TaskListRequires2PC()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:124) - decides when a task list needs distributed 2PC.
- [`Use2PCForCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:428) - turns on coordinated 2PC for the current transaction.
- [`StartRemoteTransactionPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:767) and [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:501) - issue worker-side `PREPARE` / `COMMIT` or `COMMIT PREPARED`.
- [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1140) - coordinator-side fanout of worker commit completion.

## Current behavior / grounded findings

### PostgreSQL physical replication and Citus shard replication are different layers

PostgreSQL physical replication is whole-node WAL shipping between a primary and standby. Citus shard replication is Citus metadata about maintaining multiple placements of the same logical shard on worker nodes.

The important operational conclusion for this tree is:

- worker-node HA can rely on PostgreSQL streaming replication of the whole node
- but Citus metadata and transaction code still contain older concepts related to replicated shard placements and reference-table replication

So “Citus relies on PostgreSQL replication” is only partly true. For node-level HA, yes. For distributed write coordination across workers, Citus still adds its own layer.

### The public `citus.replication_model` surface is deprecated, but the internal enum values are still live

- [`citus.replication_model`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2404) is explicitly marked deprecated.
- [`shared_library_init.c:2966`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2966) says setting it has no effect.

But the underlying metadata values are still used inside the code:

- [`REPLICATION_MODEL_STREAMING`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:67)
- [`REPLICATION_MODEL_2PC`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:68)
- [`REPLICATION_MODEL_COORDINATOR`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:66)

That means “deprecated public setting” is not the same thing as “all internal replication-model semantics are gone”.

### In this tree, shard replication is still a Citus-side placement concept

- [`citus.shard_replication_factor`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2459) still exists.
- [`DistributedTableReplicationIsEnabled()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/node_protocol.c:1052) treats shard replication as enabled when the configured factor is greater than 1.

That is a Citus-specific mechanism, not PostgreSQL physical or logical replication.

### `REPLICATION_MODEL_STREAMING` in this tree does not mean PostgreSQL physical streaming to multiple shard placements

The name is easy to misread.

- [`DecideDistTableReplicationModel()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1843) returns [`REPLICATION_MODEL_STREAMING`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1861) when the table is hash-distributed and shard replication is not enabled.
- [`create_shards.c:129`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:129) rejects `REPLICATION_MODEL_STREAMING` when `replicationFactor > 1`.

So in this codebase, `REPLICATION_MODEL_STREAMING` is not “replicate one shard placement to several workers using PostgreSQL WAL streaming.” It is closer to “the normal non-replicated hash-distributed-table path”.

### Reference tables still use replicated placements plus distributed 2PC

Reference tables remain the clearest place where Citus-specific replication semantics still matter.

- [`create_distributed_table.c:1654`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1654) marks reference tables as `REPLICATION_MODEL_2PC`.
- [`CreateReferenceTableShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:325) creates a single logical shard and replicates it to all active nodes.
- [`metadata_cache.c:929`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_cache.c:929) treats `REPLICATION_MODEL_2PC` as the reference-table indicator.

So yes, in current practical terms the old shard-replication model matters most for reference tables, even though the older surface area is broader than that.

### Distributed 2PC still matters even when shard replication is not used

This is the most important correction.

Distributed 2PC is not the same thing as Citus shard replication. It is about making several worker transactions commit coherently.

- [`TaskListRequires2PC()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:124) decides when a task list needs distributed 2PC.
- [`Use2PCForCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:428) marks the current coordinated transaction to use 2PC.
- [`StartRemoteTransactionPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:767) sends worker-side prepare.
- [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:501) sends worker-side commit / commit prepared.
- [`CoordinatedRemoteTransactionsCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1140) completes the coordinated fanout.

That logic still matters for:

- multi-shard writes
- multi-placement writes
- reference-table writes

even if ordinary distributed tables are no longer maintained through the old shard-replication path.

## Pitfalls, caveats, and hidden constraints

- A tempting but wrong shortcut is to read `REPLICATION_MODEL_STREAMING` as “this table uses PostgreSQL physical replication for multiple placements”. In this tree, the code says the opposite: streaming model is only allowed when shard replication is effectively off.
- Another tempting shortcut is to treat “Citus docs prefer PostgreSQL streaming replication now” as “distributed 2PC is obsolete”. That is false. Node-level HA and distributed commit coordination solve different problems.
- The source tree still contains old shard-replication knobs and metadata while the user-facing docs de-emphasize them. Prototype work should trust executable code paths over stale naming.
- “Reference table replication” is Citus logical placement replication plus distributed 2PC, not PostgreSQL standby WAL replay of a separate shard object.

## Implementation progress / prototype state

- **Status**: `not-started`
- **What exists in code now**: original Citus metadata and distributed 2PC behavior only.
- **Feature gates / switches**: `citus.shard_replication_factor`; deprecated/no-op `citus.replication_model`; distributed transaction paths that elect 2PC.
- **Assumptions / shortcuts / scaffolding**: none for the replication topic itself.
- **Changes from the motivating design**: none yet.
- **Corrections from earlier assumptions / rejected approaches**:
  - earlier assumption: “Citus replication is just PostgreSQL physical replication”
  - grounded correction: Citus still has its own placement metadata and distributed commit coordination above worker-node PostgreSQL instances
- **Canonical future-direction note**: none yet; the relevant prototype direction is the command/control-plane work under `docs/kb/future-directions/citus/data-movement/`.

## Architecture / data flow

### Entry points

1. Table creation decides internal replication-model metadata through [`DecideDistTableReplicationModel()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1843).
2. Reference-table creation uses [`CreateReferenceTableShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:325) to create a single shard placement on all active nodes.
3. Distributed execution decides whether 2PC is needed via [`TaskListRequires2PC()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tasks.c:124).
4. The transaction layer enables coordinated 2PC with [`Use2PCForCoordinatedTransaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:428).
5. Remote worker transactions are prepared and committed through [`StartRemoteTransactionPrepare()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:767) and [`StartRemoteTransactionCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:501).

### Data structures

- table metadata replication model: [`pg_dist_partition.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/pg_dist_partition.h:66)
- reference-table classification from metadata cache: [`metadata_cache.c:929`](/data/dbcomm/citus-dbcomm/src/backend/distributed/metadata/metadata_cache.c:929)
- placement replication factor from active nodes for reference tables: [`create_shards.c:363`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:363)

### State / lifecycle

- public shard-replication configuration is partly deprecated/confusing
- internal table metadata still stores a replication-model character
- distributed 2PC is chosen per coordinated transaction when needed

## Behavior details

### Happy path for a normal non-replicated distributed table

1. [`DecideDistTableReplicationModel()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1843) returns `REPLICATION_MODEL_STREAMING` when shard replication is disabled.
2. The table ends up with one shard placement per shard, not replicated placements across workers.
3. Worker-node HA, if configured operationally, is then a PostgreSQL node-level replication concern, not a Citus shard-placement concern.

### Happy path for a reference table

1. [`create_distributed_table.c:1654`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1654) marks it as `REPLICATION_MODEL_2PC`.
2. [`CreateReferenceTableShard()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:325) creates one shard and replicates it to all active nodes.
3. Writes that touch those placements rely on Citus distributed 2PC to commit coherently across worker transactions.

### Concurrency / ordering constraints

- Citus distributed 2PC preserves worker-side ordering/atomicity at the transaction layer.
- PostgreSQL physical replication, by contrast, preserves node-level WAL durability and standby catch-up order.
- Those layers compose, but they are not interchangeable.

## Invariants and assumptions

- Node-level PostgreSQL physical replication does not replace Citus distributed transaction coordination.
- Reference-table replication remains a first-class Citus concept in this tree.
- Internal `REPLICATION_MODEL_*` metadata is still meaningful even though the old public GUC surface is deprecated.

## Design/implementation mismatches to remember

- Current docs and current code are not perfectly aligned. The public surface is de-emphasized, but the code still contains the machinery and metadata.
- The name `REPLICATION_MODEL_STREAMING` is misleading if read through a PostgreSQL-physical-replication lens.
- For Homer prototype work, the Citus-specific part to preserve is mostly transaction/session ordering, not WAL shipping.

## Configuration / flags

- [`citus.shard_replication_factor`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2459)
- deprecated/no-op [`citus.replication_model`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c:2404)

## Interactions and cross-links

- **Overlapping KB areas**: `docs/kb/citus/transactions/` for detailed timing; `docs/kb/postgres/replication/` for actual physical WAL replication.
- **Shared code / canonical docs**: this is the canonical factual note for Citus-side replication/2PC distinctions.
- **Implementation note / factual note / future-direction note**: current prototype control-plane evolution is documented under [`../../future-directions/citus/data-movement/command_dispatch_completion_plane.md`](../../future-directions/citus/data-movement/command_dispatch_completion_plane.md).

## Future directions / design ideas

- **Status**: `exploratory`
- **Goal**: keep Citus worker transaction semantics intact while replacing command/control transport paths with the external service.
- **Grounding in current code**: worker-side transaction control still lives in [`remote_transaction.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:501) and [`transaction_management.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:428), so any offload layer has to preserve those ordering points even if worker-node HA is delegated to PostgreSQL physical replication.
- **Candidate approaches**: typed command/control planes are already explored under the Homer future-direction notes.
- **Risks / tradeoffs**: conflating placement replication, node replication, and distributed commit would create the wrong abstraction boundary.
- **Next experiments / implementation steps**: keep using typed transaction commands for the worker prototype; do not invent a separate “replication object” for Citus unless the target is actually node-level WAL transport.

## Debugging notes / gotchas

- Symptom: assuming reference-table writes can ignore distributed 2PC because WAL replication exists.
  - Likely cause: confusing node-level HA with cross-worker transaction atomicity.
- Symptom: assuming `REPLICATION_MODEL_STREAMING` means multiple replicated placements.
  - Check [`create_shards.c:129`](/data/dbcomm/citus-dbcomm/src/backend/distributed/operations/create_shards.c:129) and [`create_distributed_table.c:1861`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1861).

## Open questions / TODO

- If the Citus public shard-replication surface continues to disappear, should the KB later split “legacy shard replication” and “reference-table replication + 2PC” into separate notes?
- For the Homer prototype, which remaining transaction-control paths still implicitly assume libpq connection semantics rather than typed command completion?
