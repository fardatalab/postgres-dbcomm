# PostgreSQL Implementations

<!-- kb-summary: Current PostgreSQL-facing Homer implementation checkpoints and their cross-descendant lifecycle boundary. -->

## Purpose

Track implementation-progress notes for PostgreSQL-facing prototype work, especially places where the external Homer service replaces ordinary frontend/backend or replication communication paths.

## Subdirectories

<!-- kb-subdirs:start -->
- `client-sql-session/`: Current Homer client-SQL session implementation checkpoints and result/completion contracts.
- `replication/`: Current Homer replication and basebackup implementation checkpoints and lifecycle contracts.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Cross-descendant lifecycle boundary between PostgreSQL client-SQL and basebackup Homer integrations.
<!-- kb-docs:end -->

## Related

- `../../postgres/`: Grounded PostgreSQL behavior in the existing codebase.
- `../../future-directions/postgres/`: PostgreSQL-facing design notes that motivate these implementations.
