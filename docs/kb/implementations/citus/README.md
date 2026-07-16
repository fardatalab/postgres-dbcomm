# Citus Implementations

<!-- kb-summary: Current implementation checkpoints and cross-component contracts for Citus-side Homer prototypes. -->

## Purpose

Track the current state of Citus-side prototype code that we are actively landing. These notes should describe the actual behavior, feature gates, shortcuts, and caveats of our implementation work, not just the intended design.

## Subdirectories

<!-- kb-subdirs:start -->
- `connection-management/`: Current implementation checkpoints and contracts for Homer session, compatibility, and connection management.
- `transport/`: Current implementation checkpoints, plans, and high-signal contracts for the Homer transport stack.
- `tuple-route/`: Historical tuple-route checkpoints and current tuple-view queue lifetime contracts.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Cross-component semantic-operation and stream/session identity contracts for Citus-side Homer implementations.
<!-- kb-docs:end -->

## Related

- `../../citus/`: Grounded Citus architecture and existing workflows.
- `../../future-directions/citus/`: Citus-focused design and research notes that these implementations are advancing.
