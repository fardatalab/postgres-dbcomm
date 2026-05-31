# Transport

## Purpose

Index forward-looking transport and control-plane designs for Citus, especially where current libpq-based connection management may need a new abstraction for RDMA or external data-plane services.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md) - Future design for Homer service progress, nonblocking peer-transport state machines, multi-slot peer-control rings, RDMA egress scheduling, neutral payload-stream state, push command completions, traffic classes, multiple QPs, and large-object fragmentation.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md) - RDMA publication/visibility rules, including why the tuple-sink prototype now uses WRITE_WITH_IMM doorbells rather than trusting memory polling alone as the responder-visible publish event.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md) - TODO
<!-- kb-docs:end -->

## Related

- `../../../citus/connection-management/connection_management_control_plane.md`: Grounded current behavior that motivates these transport abstractions.
- `../connection-management/remote_execution_session_control_plane.md`: Chosen higher-level control-plane abstraction that transport designs must serve.
- `../intermediate-results/intermediate_results_service_future_directions.md`: Broader tuple-service design space that depends on the transport/control-plane choice.
