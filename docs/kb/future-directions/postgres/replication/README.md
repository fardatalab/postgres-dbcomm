# PostgreSQL Replication Future Directions

## Purpose

Index forward-looking PostgreSQL replication design notes. Canonical grounded behavior should stay under `docs/kb/postgres/replication/`; this tree should cross-link back to that factual note and keep design proposals separate.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [base_backup_service_abstraction.md](base_backup_service_abstraction.md) - Proposes a base-backup archive-stream abstraction for the remote execution session/service/RDMA framework, including typed archive objects, service-backed `bbsink`, receiver sink choices, and performance tradeoffs.
- [replication_transport_offload_abstraction.md](replication_transport_offload_abstraction.md) - Explores how physical WAL replication could map to the external communication service, including borrowed-vs-copied WAL payloads, RDMA inline-control vs registered-payload publication, WRITE_WITH_IMM doorbells, and the standby-side inbound replication ring as the network-only borrowable sink.
<!-- kb-docs:end -->

## Related

- `../../../postgres/replication/`: Grounded PostgreSQL physical replication and base-backup notes.
- `../../citus/data-movement/command_dispatch_completion_plane.md`: Existing typed command/completion plane for contrast with the different replication boundary.
- `../../citus/transport/rdma_publication_visibility_and_doorbells.md`: Canonical RDMA publish/visibility note that the replication offload design now reuses.
