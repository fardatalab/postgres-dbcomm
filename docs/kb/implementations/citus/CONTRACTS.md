# Citus Implementations — Contracts And Invariants

<!-- kb-summary: Cross-component semantic-operation and stream/session identity contracts for Citus-side Homer implementations. -->

> Read the narrowest child index first. This file owns only rules shared by multiple Citus implementation areas; component-local transport rules remain in `transport/CONTRACTS.md`.

## Symbol entries

### `HomerServicePayloadStreamEntry.semanticOpKind` versus `objectFamily` — semantic operation versus hot-path payload interpretation

- **MEANS:** `semanticOpKind` preserves the external database/control-plane operation, while `objectFamily` caches the payload family used for hot-path validation and dispatch.
- **DOES NOT MEAN:** The fields are interchangeable enums, or that every operation requires a distinct wire payload family.
- **CONTRACT / INVARIANT:** Map the semantic operation to a payload family when the stream is created. Family-specific validation and dispatch use `objectFamily`; every new payload-bearing operation must update the operation-to-family and traffic-class mappings and reject an invalid family.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** `HomerServicePayloadStreamEntry` in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2473`; operation-family mapping at `:4843`; traffic-class mapping at `:4868`; stream initialization in `HomerServiceCreatePayloadStreamEntry()` at `:27460`.

### `serviceStreamId` versus `parentServiceSessionId` — child payload lifetime versus compatibility-session lifetime

- **MEANS:** `serviceStreamId` identifies one exact payload stream; `parentServiceSessionId` identifies the compatibility/session-control lifetime that owns it.
- **DOES NOT MEAN:** Session, stream, and older protocol “sink” identifiers name the same object. “Sink” wording remains in some migrated protocol names but does not collapse the two lifetimes.
- **CONTRACT / INVARIANT:** Stream lookup, close, and reuse preserve both the exact stream identity and its parent session. Queue/RDMA payload lifetime belongs to the stream; compatibility and command-plane lifetime belong to the parent session.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** identity fields in `HomerPayloadStreamState` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1939`; parent-scoped stream lookup in `HomerServiceFindPayloadStreamBySessionAndKey()` at `:27127`; stream identity allocation and parent binding in `HomerServiceCreatePayloadStreamEntry()` at `:27460`; exact result-stream teardown lookup in `TupleSinkServiceReleaseServiceOwnedDpuResultSinkAfterBackendTeardown()` at `:29495`.

## Related

- [connection-management/CONTRACTS.md](connection-management/CONTRACTS.md)
- [tuple-route/CONTRACTS.md](tuple-route/CONTRACTS.md)
- [transport/CONTRACTS.md](transport/CONTRACTS.md)
