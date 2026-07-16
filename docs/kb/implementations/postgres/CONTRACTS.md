# PostgreSQL Implementations — Contracts And Invariants

<!-- kb-summary: Cross-descendant lifecycle boundary between PostgreSQL client-SQL and basebackup Homer integrations. -->

> The two descendants share Homer client machinery but not their surrounding PostgreSQL lifecycle. Read the relevant leaf first.

## Cross-cutting contracts

### Does shared `HomerClient` machinery imply a shared PostgreSQL lifecycle?

- **ANSWER:** No. The client-SQL leaf owns pgbench session, command, result, and transaction semantics. The replication leaf preserves PostgreSQL's `bbsink`/basebackup lifecycle while replacing archive-object delivery.
- **TRAP:** Do not generalize a result-sink ownership, close, setup, error, or cleanup rule from one leaf into the other merely because both call Homer client APIs.
- **POINTERS:** pgbench Homer session/command flow in `src/bin/pgbench/pgbench.c:9333` and `:9755`; basebackup sink lifecycle in `src/backend/backup/basebackup_homer.c:337` and `:459`.

## Related

- [client-sql-session/CONTRACTS.md](client-sql-session/CONTRACTS.md)
- [replication/CONTRACTS.md](replication/CONTRACTS.md)
- [../CONTRACTS.md](../CONTRACTS.md)
