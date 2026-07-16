# Implementations

<!-- kb-summary: Implementation-progress notes and cross-repository contracts for active PostgreSQL and Citus prototypes. -->

## Purpose

Hold implementation-progress notes for prototypes and in-flight changes separately from both the grounded codebase KB and the future-direction design tree. This tree is where we record what our code currently does, what is still scaffolding, and which design decisions changed during implementation.

## Subdirectories

<!-- kb-subdirs:start -->
- `citus/`: Current implementation checkpoints and cross-component contracts for Citus-side Homer prototypes.
- `postgres/`: Current PostgreSQL-facing Homer implementation checkpoints and their cross-descendant lifecycle boundary.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Cross-repository build and ABI contracts shared by the PostgreSQL-facing and Citus-side Homer implementations.
<!-- kb-docs:end -->

## Related

- `../citus/`: Grounded Citus behavior in the existing codebase.
- `../future-directions/`: Forward-looking design notes that motivate the implementation work.
