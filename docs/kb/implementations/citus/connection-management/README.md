# Connection Management

## Purpose

Capture the current implementation state of Citus-side control-plane prototype work. Use this directory when the question is "what control-plane/session abstraction have we actually landed in code so far?" rather than "how does current Citus work?" or "what is the final service design?"

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md) - Current canonical checkpoint: full tuple-view contract exchange, peer-provisioned exact sinks, service-side semantic receive decode, batching/backpressure, typed command dispatch, and worker-side borrow/use/release for the tuple-sink prototype.
- [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md) - Earlier checkpoint: the standalone local service owns SHM control IPC, resolves compatibility sessions, and manages tuple sinks underneath `RemoteExecutionSession`.
- [remote_execution_session_wrapper_checkpoint.md](remote_execution_session_wrapper_checkpoint.md) - Earlier wrapper checkpoint: backend-visible `RemoteExecutionSession` over the tuple-route substrate before the later cross-node, semantic-decode, and borrow/use/release milestones landed.
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current Citus control-plane behavior that the wrapper is trying to subsume.
- `../../../future-directions/citus/connection-management/remote_execution_session_control_plane.md`: Chosen future-direction design that this implementation checkpoint is advancing.
- `cross_node_tuple_sink_peer_control_checkpoint.md`: Latest canonical implementation checkpoint for the active tuple-sink vertical slice.
- `local_service_owned_tuple_sink_control_checkpoint.md`: Earlier local-only service-owned control checkpoint beneath the newer cross-node note.
- `../tuple-route/local_batch_materialization_checkpoint.md`: Current tuple-route/data-plane checkpoint that the session wrapper is built on top of.
