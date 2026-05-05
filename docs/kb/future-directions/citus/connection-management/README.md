# Connection Management

## Purpose

Index forward-looking connection/control-plane designs for Citus, especially where the current backend-local `MultiConnection` model should evolve into a higher-level remote execution session abstraction owned by an external service.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [cross_node_service_to_service_control_path.md](cross_node_service_to_service_control_path.md) - Detailed next-step plan for the missing cross-node service-owned control path that binds compatible sessions and exact tuple sinks across nodes before RDMA data transport.
- [remote_execution_session_control_plane.md](remote_execution_session_control_plane.md) - Chosen future-direction control-plane abstraction: one backend-visible remote execution session API, internal session/operation split, and phased convergence plan for the current tuple-route prototype.
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current Citus behavior that this future design intends to subsume.
- `cross_node_service_to_service_control_path.md`: Concrete phased plan for extending the landed local-only service/session layer into an end-to-end service-owned control path.
- `../transport/rdma_transport_control_plane_abstraction.md`: Transport-oriented RDMA/service design space beneath the higher-level control-plane abstraction.
- `../../../implementations/citus/connection-management/remote_execution_session_wrapper_checkpoint.md`: Current implementation-progress note for the session-centric wrapper layer that is converging toward this design.
- `../../../implementations/citus/connection-management/local_service_owned_tuple_sink_control_checkpoint.md`: Newer implementation-progress note where the standalone service owns tuple-sink open/close and queue allocation.
- `../../../implementations/citus/tuple-route/`: Current implementation-progress notes for the tuple-route/data-plane substrate underneath that wrapper.
