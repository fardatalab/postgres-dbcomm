# Citus Implementations — Contracts And Invariants

<!-- kb-summary: Cross-component semantic-operation and stream/session identity contracts for Citus-side Homer implementations. -->

> Read the narrowest child index first. This file owns only rules shared by multiple Citus implementation areas; component-local transport rules remain in `transport/CONTRACTS.md`.

## Symbol entries

### `HOMER_EVENT(...)` versus `HOMER_TRACE_EVENT(...)` — stable proof record versus optional diagnostic trace

- **MEANS:** `HOMER_EVENT` emits one bounded, versioned `HOMER_EVENT v=1 ...` record for cold lifecycle,
  terminal, teardown, and alarm transitions. `HOMER_TRACE_EVENT` is a default-off diagnostic surface for
  potentially frequent observations.
- **DOES NOT MEAN:** `HOMER_EVENT` is a general printf replacement, accepts arbitrary whitespace-bearing prose,
  or makes a hot-loop logging site acceptable. A disabled `HOMER_TRACE_EVENT` deliberately does not evaluate its
  arguments and therefore cannot be an acceptance anchor.
- **CONTRACT / INVARIANT:** Required prefix fields are `v severity component event run`; later fields are
  whitespace-free `key=value` tokens. `run` comes from a sanitized `HOMER_VALIDATION_RUN_ID`, or `-`. Bare
  `session` requires `session_kind`; bare `generation` is reserved for an exact peer-QP generation. Overflow emits
  `truncated=1`, which validation treats as `INCONCLUSIVE`. Always-on events remain bounded and off hot/retry loops;
  retry sites use caller-owned first-eight-then-powers-of-two counters plus a terminal suppression summary.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** serializer and macros in
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_event.h:78` and `:188`; parser/schema checks in
  `/data/dbcomm/postgres-citus/tools/farnet_validation/checks.py:31`, `:95`, and `:212`; initial dual-emission sites
  are DPU spawn begin/complete (`homer_service_dpu_spawn.c:252`, `:558`), frontend DPU-doorbell attachment
  (`homer_frontend_agent.c:1670`), peer send frontier/control retirement
  (`remote_execution_peer_transport_rdma.c:7013`, `:5564`), head-mirror/Stage-2b teardown
  (`tuple_sink_service_process.c:52463`, `:52526`), and DPU-DMA teardown completion
  (`homer_service_dpu_dma.c:7114`).
- **CURRENT STABLE EVENT NAMES:** `frontend_agent/dpu_doorbell_attached`; `dpu_spawn/begin` and `complete`;
  `peer_transport/send_frontier` and `control_retirement`; `service/head_mirror_accounting` and
  `stage2b_teardown`; `dpu_dma/teardown_complete`. Identity/counter field names at these sites are parser ABI: add
  optional fields freely, but version the schema before deleting or changing a required field's meaning.
- **TEMPTING WRONG MOVE:** changing an English legacy log line and its regex together while omitting the stable
  event makes the proof dependent on prose again. During migration, retain the human line and add the event.

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
