# Citus Transport Implementations

## Purpose

Track implementation checkpoints for Homer transport-layer prototype work in
Citus, including RDMA substrate changes that are separate from the higher-level
remote execution session semantics.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md) - Current checkpoint for the service-to-service byte-ring RDMA substrate used by Homer basebackup streams.
<!-- kb-docs:end -->

## Related

- `../../../future-directions/citus/transport/`: Transport scheduler, payload
  stream, and RDMA publication design notes.
- `../../../implementations/postgres/replication/`: PostgreSQL basebackup target
  integration notes that feed this transport path.
