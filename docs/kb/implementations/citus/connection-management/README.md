# Connection Management

<!-- kb-summary: Current implementation checkpoints and contracts for Homer session, compatibility, and connection management. -->

## Purpose

Capture the current implementation state of Citus-side control-plane prototype work. Use this directory when the question is "what control-plane/session abstraction have we actually landed in code so far?" rather than "how does current Citus work?" or "what is the final service design?"

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Current compatibility-intent, exact-operation, and data-plane/session-close contracts for Homer connection management.
- [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md) - Current cross-node tuple-sink control, semantic decode, batching, terminal signaling, and worker borrow/use/release checkpoint.
- [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md) - Earlier local checkpoint for service-owned SHM control, compatibility sessions, and exact tuple sinks.
- [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md) - Earlier wrapper checkpoint for the backend-visible RemoteExecutionSession over the tuple-sink substrate.
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current Citus control-plane behavior that the wrapper is trying to subsume.
- `../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md`: Chosen future-direction design that this implementation checkpoint is advancing.
- `cross_node_tuple_sink_peer_control_checkpoint.md`: Latest canonical implementation checkpoint for the active tuple-sink vertical slice.
- `local_service_owned_tuple_sink_control_checkpoint.md`: Earlier local-only service-owned control checkpoint beneath the newer cross-node note.
- `../tuple-route/local_batch_materialization_checkpoint.md`: Current tuple-route/data-plane checkpoint that the session wrapper is built on top of.
