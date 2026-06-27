# Citus Implementations

## Purpose

Track the current state of Citus-side prototype code that we are actively landing. These notes should describe the actual behavior, feature gates, shortcuts, and caveats of our implementation work, not just the intended design.

## Subdirectories

<!-- kb-subdirs:start -->
- `connection-management/`: Ongoing implementation progress for the new session-centric control-plane wrapper layer, service-owned SHM control path, and service-side compatibility session work in Citus.
- `transport/`: Implementation checkpoints for the landed Homer stack, including the current frontend/service boundary, shared ABI split, RDMA substrate, tuple COPY, pgbench, and basebackup paths.
- `tuple-route/`: Ongoing implementation progress for the experimental tuple-route service path in Citus.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `../../citus/`: Grounded Citus architecture and existing workflows.
- `../../future-directions/citus/`: Citus-focused design and research notes that these implementations are advancing.
