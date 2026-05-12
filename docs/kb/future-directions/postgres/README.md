# PostgreSQL Future Directions

## Purpose

Index forward-looking PostgreSQL design notes. Canonical grounded behavior should stay under `docs/kb/postgres/`; this tree should cross-link back to those factual notes.

## Subdirectories

<!-- kb-subdirs:start -->
- `client-sql-session/`: Client-to-PostgreSQL transaction offload design using typed Homer session and command objects instead of frontend libpq.
- `replication/`: Replication/offload design notes grounded in physical WAL streaming behavior, including RDMA publication shape and standby inbound-ring design.
- `serialization/`: Serializer/send-path design notes and open performance experiments.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `../../postgres/`: Grounded PostgreSQL KB tree.
- `../citus/connection-management/remote_execution_session_control_plane.md`: Existing Citus remote-execution session abstraction that the client SQL session design extends.
