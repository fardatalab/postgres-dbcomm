# libpq IO (FE/BE)

## Purpose

Explain how FE/BE socket IO and waiting work in this codebase and how timing spots (`PG_*`) map to those paths.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [socket_wait_and_io.md](socket_wait_and_io.md) - Mapping for `PG_FE_*`, `PG_BE_*`, wait hooks, and the `QUERY_ACTIVE_WALL` exclusion path.
<!-- kb-docs:end -->

## Related

- `../../instrumentation/timing-spots/timing_spots.md`: Canonical spot meanings.
- `../serialization/`: Canonical row-assembly and send-buffer lifetime notes for `printtup` and FE/BE message construction.
