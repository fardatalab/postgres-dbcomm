# Experimental tuple-route substrate under the service-owned control checkpoint

## Scope

- **What this doc explains**: the current tuple-route queue/batch substrate that sits underneath the newer `RemoteExecutionSession` control path: tuple-view layout, queue ownership, batch lifecycle, and loopback forwarding behavior.
- **What this doc does NOT cover**: RDMA transport, remote service control commands, or the worker-side tuple-consume/insert loop.
- **Primary directory**: `docs/kb/implementations/citus/tuple-route/`
- **Doc type**: `implementation`

Historical naming note:

- this document intentionally preserves the older `tuple_route_*` wording because it records the earlier checkpoint as it was originally implemented
- the active code and current control-plane notes have since moved to `tuple_sink_*` naming for the same substrate

Current status note:

- this note is now historical substrate context, not the latest implementation state
- the current canonical implementation checkpoint is [cross_node_tuple_sink_peer_control_checkpoint.md](../connection-management/cross_node_tuple_sink_peer_control_checkpoint.md), which records the later merged batch format, full tuple-view contract, service-side semantic receive decode, batching/backpressure, and worker-side borrow/use/release path
- so any copy-out-only or pre-contract behavior described below should be read as “state at this earlier checkpoint”, not “current active behavior”

## Why this exists (context)

The earlier design notes for the off-path local-table tuple-service prototype and RDMA transport describe the intended service boundary and target data path. This doc records what we actually implemented so far.

The current code now lands a shared-memory external-service loopback checkpoint:

- it defines a normalized tuple-view batch format
- it keeps the tuple-view send/receive queues and batch substrate
- it adds a standalone manually launched service process at [`citus_tuple_sink_service`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L1)
- it now uses a service-owned local control protocol in [`remote_execution_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h#L1) instead of the older registry-scan rendezvous path
- it now exposes a backend-facing remote execution session wrapper in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h#L1) and [`remote_execution_session.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L1)
- it exercises that format from the real sender path in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2394)
- it moves submitted batches across a real process boundary before the backend polls the receive queue
- it still leaves actual transport on the existing COPY path

So this is now the first real external-service checkpoint, but still without RDMA and still with loopback forwarding rather than inter-node transport.

For the canonical description of the wrapper layer itself, see [remote_execution_session_wrapper_checkpoint.md](../connection-management/remote_execution_session_wrapper_checkpoint.md). For the newer service-owned local control path that replaced the registry-scan bootstrap, see [local_service_owned_tuple_sink_control_checkpoint.md](../connection-management/local_service_owned_tuple_sink_control_checkpoint.md). This note remains focused on the tuple-route/data-plane substrate that those layers currently delegate to.

## Key code pointers

- tuple-route protocol:
  - well-known registry name [`CITUS_TUPLE_ROUTE_REGISTRY_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L31)
  - max route count [`CITUS_TUPLE_ROUTE_MAX_SHARED_ROUTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L23)
  - [`CitusTupleRouteRegistryHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L84)
  - [`CitusTupleRouteRegistryEntry`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L93)
  - [`CitusTupleRouteQueueControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L74)
  - [`CitusTupleRouteAttributeEntry`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L104)
  - [`CITUS_TUPLE_ROUTE_ATTRIBUTE_FLAG_NULL`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L117)
  - [`CitusTupleRouteTupleHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L126)
- backend-facing tuple-route API:
  - [`OpenCitusTupleRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L770)
  - [`ReserveCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L989)
  - [`AppendTupleViewToCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1086)
  - [`SubmitCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1230)
  - [`PollCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1273)
  - [`ReleaseCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1321)
  - [`CopyOutTupleViewFromCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1354)
- backend-facing remote execution session wrapper:
  - [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
  - [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h)
  - [`InitRemoteTupleSinkSendSessionIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`InitRemoteTupleSinkSendOperationSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`InitRemoteTupleSinkReceiveSessionIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`InitRemoteTupleSinkReceiveOperationSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
  - [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c)
- tuple normalization helpers:
  - [`NormalizeTupleRouteAttribute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L224)
  - [`TupleRouteTupleBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L298)
- sender-side integration:
  - paired session state in [`CopyPlacementState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L190)
  - [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2559)
  - [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2637)
  - call site inside [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2394)
- worker-side consumer integration:
  - this earlier checkpoint eventually gained a temporary worker-side SQL bootstrap consumer for receive-side validation
  - that seam has since been removed from the current tree after the control plane became end-to-end enough for the tuple-sink prototype
- shared-memory mapping helpers:
  - [`MapTupleRouteRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L214)
  - [`LocateOrCreateTupleRouteRegistryEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L279)
  - [`MapTupleRouteQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L340)
- standalone service:
  - service process main loop [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L263)
  - route attach [`TupleRouteServiceAttachRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L184)
  - slot forwarding [`TupleRouteServicePumpRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L209)
- build/install:
  - standalone binary target [`tuple_sink_service_bin`](/data/dbcomm/citus-dbcomm/Makefile#L6)
  - separate build rule [`service-bin`](/data/dbcomm/citus-dbcomm/Makefile#L25)
  - install rule [`install-service-bin`](/data/dbcomm/citus-dbcomm/Makefile#L58)
  - extension object exclusion for the standalone `main()` file [`OBJS := $(filter-out ...)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile#L39)
- feature gates:
  - [`citus.enable_experimental_tuple_sink_routing`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2533)
  - [`citus.experimental_tuple_sink_slot_capacity_bytes`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2545)

## Build / runtime facts

- The homer service is a **standalone binary built separately from `citus.so`**, not part of the extension shared library:
  - top-level build target [`service-bin`](/data/dbcomm/citus-dbcomm/Makefile#L25)
  - binary path variable [`tuple_sink_service_bin`](/data/dbcomm/citus-dbcomm/Makefile#L6)
  - installed binary location through [`install-service-bin`](/data/dbcomm/citus-dbcomm/Makefile#L58), which places it under the PostgreSQL `bindir` as `citus_tuple_sink_service`
- The extension build explicitly excludes the standalone service object from `citus.so`:
  - [`OBJS := $(filter-out utils/homer/tuple_sink_service_process.o,$(OBJS))`](/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile#L39)
- Current runtime assumption:
  - PostgreSQL/Citus backends do **not** launch the service automatically
  - the service is manually started as `/data/dbcomm/pg-citus/bin/citus_tuple_sink_service`
  - backends assume it is already running when experimental tuple routing is enabled

## Historical route-registry checkpoint

The original external-process loopback checkpoint used a well-known shared-memory registry. That is **no longer the active control path**. The current code moved sink open/close and queue naming behind the local service-owned protocol documented in [local_service_owned_tuple_sink_control_checkpoint.md](../connection-management/local_service_owned_tuple_sink_control_checkpoint.md).

The registry material below is kept here only as historical context for the earlier bootstrap shape.

The current checkpoint introduced one well-known shared-memory route registry:

- the registry shm name is [`CITUS_TUPLE_ROUTE_REGISTRY_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L31)
- the registry layout is [`CitusTupleRouteRegistryHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L84) plus an array of [`CitusTupleRouteRegistryEntry`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L93)
- the route count is capped by [`CITUS_TUPLE_ROUTE_MAX_SHARED_ROUTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L23)

The registry is **not** the tuple queue itself and is **not** the future RDMA control plane. In the current code it only acts as a bootstrap rendezvous/catalog between backends and the standalone service:

- route discovery and lookup:
  - sender/receiver handles map the registry through [`MapTupleRouteRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L214)
  - per-route entries are found or created through [`LocateOrCreateTupleRouteRegistryEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L279)
- queue discovery:
  - each route entry stores the deterministic send/receive queue shm names in [`sendQueueShmName`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L110) and [`receiveQueueShmName`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L111)
  - those names are derived by [`FormatTupleRouteQueueNames()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L176)
- queue geometry:
  - slot count and slot capacity live in [`slotCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L102) and [`slotCapacityBytes`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L103)
- lightweight liveness/accounting:
  - handle counts live in [`sendHandleCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L105) and [`receiveHandleCount`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L106)
  - service attach state lives in [`serviceAttached`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L107)
  - service heartbeat currently updates [`heartbeatCounter`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L96) from [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L263)

So the current responsibility split is:

- registry: route catalog and queue rendezvous
- per-route send/receive queues: tuple-batch storage and publication state
- standalone service: route scan/attach plus slot forwarding

## Current behavior / grounded findings

- The sender now opens paired experimental send/receive **remote execution sessions** per placement through [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2559), while the session layer internally delegates to tuple-route handles.
- The wrapper layer currently supports only tuple-sink / COPY-ingest-like prototype operations:
  - session open/close through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L368) and [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L401)
  - send-side reserve/append/submit through [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L422), [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L449), and [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L466)
  - receive-side poll/copy-out/release through [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L503), [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L576), and [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L601)
- Backend route handles now map shared-memory registry/queue objects through [`MapTupleRouteRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L212) and [`MapTupleRouteQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L309).
- The standalone service process scans the registry in [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L263), attaches each route with [`TupleRouteServiceAttachRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L184), and mirrors batches from send queue to receive queue with [`TupleRouteServicePumpRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c#L209).
- For every tuple/placement pair, the sender now:
  - reserves a real queue slot through [`ReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L422)
  - writes the normalized tuple-view payload into that slot via [`AppendTupleViewToRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L449)
  - publishes the slot through [`SubmitRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L466)
  - immediately polls it back through the receive session with [`PollRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L503)
  - reconstructs caller-owned `Datum[]` / `isnull[]` with [`CopyOutRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L576)
  - releases the receive-side slot wrapper with [`ReleaseRemoteRecvBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c#L601)
- The worker bootstrap consumer now also uses the same wrapper layer instead of opening tuple-route handles directly:
  - the later worker-side SQL bootstrap consumer used the same receive-side session intent/session-open/polling shape
  - that consumer has since been removed from the current tree
- The actual network/data transport still goes through the original COPY path in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2443) and [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2480).
- The batch format is no longer the earlier coarse “fixed scalar region + varlena region” sketch. It is now:
  - slot-local [`CitusTupleRouteSlotHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L67)
  - one [`CitusTupleRouteBatchHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L81)
  - repeated tuple payloads, each starting with [`CitusTupleRouteTupleHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L126)
  - a heap-style non-null bitmap
  - a fixed-width array of [`CitusTupleRouteAttributeEntry`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L104)
  - one maxaligned value region containing copied attribute payload bytes
- The prototype keeps the tuple-view format `natts`-wide rather than filtering the layout down to the exact set of columns that the current COPY byte path emits. Dropped/generated attributes are represented explicitly with flags and null-like state in [`NormalizeTupleRouteAttribute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L224).

## Implementation progress / prototype state

- **Status**: `partial`
- **What exists in code now**:
  - deterministic route spec construction and route handle open/close
  - shared-memory route registry and per-route send/receive queues
  - standalone `citus_tuple_sink_service` process for loopback forwarding
  - per-route scratch memory context
  - tuple normalization with detoasting and fixed-slot size enforcement
  - real cross-process `reserve -> submit -> service forward -> poll -> release` lifecycle
  - backend-facing remote execution session wrapper that maps the tuple-sink prototype onto the newer control-plane vocabulary
  - true copy-out reconstruction API for receive-side batches
  - sender-side invocation against real tuples on `create_distributed_table(...)`
  - worker-side bootstrap consumer invocation against the same wrapper layer
- **Feature gates / switches**:
  - [`citus.enable_experimental_tuple_sink_routing`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2533)
  - [`citus.experimental_tuple_sink_slot_capacity_bytes`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2545)
- **Assumptions / shortcuts / scaffolding**:
  - service process is manually launched; there is no service lifecycle management yet
  - route discovery still uses a registry scan rather than a dedicated service-control path
  - no RDMA yet
  - the receive side is still polled by the same backend for validation, not a worker-side consumer
  - the loopback validation checks tuple shape/null bitmap round-trip, not full type-aware equality
- **Changes from the motivating design**:
  - the backend-facing abstraction is no longer a raw tuple-route handle for the new prototype callers; it is now a remote execution session wrapper layered above tuple-route bootstrap internals
  - the implemented batch format uses per-attribute entries plus one aligned value region, not the earlier rough “fixed scalar region vs varlena region” description
  - the current implementation keeps `natts`-wide tuples and marks dropped/generated attributes explicitly instead of defining a sender-visible filtered column map
  - [`CopyOutTupleViewFromCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L814) currently uses true copy-out semantics for pass-by-reference values rather than borrow/release semantics
- **Canonical future-direction note**:
  - [`off_path_local_table_rdma_tuple_service_first_prototype.md`](../../../future-directions/citus/data-movement/off_path_local_table_rdma_tuple_service_first_prototype.md)

## Architecture / data flow

- **Entry points**:
  - sender route open in [`MaybeOpenExperimentalTupleRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2539)
  - sender validation materialization in [`MaterializeExperimentalTupleRouteTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2586)
  - local route/batch lifecycle in [`tuple_sink_service.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L415)
- **Control flow**:
  1. open paired send/receive loopback routes lazily per placement
  2. reserve one real queue slot
  3. deform the slot via [`slot_getallattrs(slot)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L855)
  4. normalize each attribute via [`NormalizeTupleRouteAttribute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L224)
  5. append tuple bytes into the reserved slot
  6. submit the slot into the backend-local queue
  7. poll it back immediately through the paired receive handle
  8. copy out `Datum[]` / `isnull[]` from the received slot
  9. release the received slot
  10. continue with the original COPY serialization/send path
- **Data structures**:
  - route state in [`CitusTupleRouteHandleData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L36)
  - shared queue state in [`CitusTupleRouteSharedStateData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L72)
  - batch wrapper state in [`CitusTupleRouteBatchHandleData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L56)
  - wire-agnostic batch layout in [`tuple_sink_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L17)
- **State / lifecycle**:
  - send and receive loopback handles are placement-scoped and share one backend-local route queue
  - queue slots are reused only after the receive-side release advances `consumedHead`
  - per-route scratch allocations are reset for every tuple in [`AppendTupleViewToCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L856) and again after the tuple is copied into the slot at [`tuple_sink_service.c:948`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L948)

## Behavior details

### Route handle behavior

- [`OpenCitusTupleRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L514) validates the protocol version and slot-capacity GUC, creates or reuses one backend-local shared queue per route, validates tuple-descriptor reuse with [`equalTupleDescs(...)`](/data/dbcomm/postgres-citus/src/backend/access/common/tupdesc.c#L419), allocates per-route scratch arrays, and creates a route-local scratch context.
- [`CloseCitusTupleRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L669) drops only the handle-local scratch allocations immediately. The shared queue survives until both send and receive open counts in [`CitusTupleRouteSharedStateData`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L72) reach zero.

### Queue and batch behavior

- [`ReserveCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L722) reserves one fixed slot from the backend-local queue instead of allocating a private buffer. It currently fails if the queue is full because this checkpoint expects immediate loopback polling.
- [`AppendTupleViewToCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L813) uses `slot_getallattrs`, normalizes every attribute, computes the tuple’s aligned size, and errors out if the tuple cannot fit in the configured slot capacity.
- Non-null attributes are copied into a maxaligned value region, and [`CitusTupleRouteAttributeEntry.valueOffset`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L106) records each payload position relative to the tuple start.
- [`SubmitCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L957) now advances [`publishedTail`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L68) after the slot payload is complete, and enforces in-order submission.
- [`PollCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L999) returns the next published slot wrapper for the receive-side loopback handle, or `NULL` if nothing new is published yet.
- [`ReleaseCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1047) now has split semantics:
  - send-side wrapper release drops only the local wrapper after submission
  - receive-side wrapper release advances [`consumedHead`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h#L69) so the slot can be reused

### Attribute normalization rules currently implemented

- by-value attributes: store the exact `attlen` bytes via [`store_att_byval()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L700)
- fixed-length by-reference attributes: copy `attlen` bytes directly
- varlena attributes: detoast into the route scratch context via [`PG_DETOAST_DATUM_PACKED`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L274), then copy the packed bytes
- cstring attributes: copy `strlen + 1` bytes
- dropped/generated-stored attributes: mark with explicit flags and do not materialize payload bytes

### Copy-out behavior

- [`CopyOutTupleViewFromCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L1080) reconstructs one tuple from a polled queue slot.
- By-value attributes are reconstructed directly with [`fetch_att()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L867).
- Pass-by-reference attributes are copied into the caller’s current memory context using [`datumCopy()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L874).
- This means the current public API is copy-out semantics, not borrow semantics.

## Invariants and assumptions

- The tuple-route API remains local bootstrap machinery in [`tuple_sink_service.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_service.h#L3), but the newer backend-facing wrapper is now defined in [`remote_execution_session.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_session.h#L1).
- The batch layout assumes homogeneous nodes and host-endian interpretation for now.
- Tuple payload bytes are aligned relative to the tuple start so a later borrow/release API can point into batch memory safely.
- The prototype enforces a fixed slot budget per route. If one tuple exceeds the configured slot capacity, the current code errors immediately instead of falling back to the original COPY path.
- The queue path is now a real inter-process local path, but it is still loopback scaffolding rather than the final transport.
- The current wrapper only implements tuple-sink / COPY-ingest-like session opens. SQL-command and query-result session modes remain future design only.

## Configuration / flags

- [`citus.enable_experimental_tuple_sink_routing`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2533) gates all route opening and sender-side materialization.
- [`citus.experimental_tuple_sink_slot_capacity_bytes`](/data/dbcomm/citus-dbcomm/src/backend/distributed/shared_library_init.c#L2545) sets the total local slot size used by [`OpenCitusTupleRoute()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L415) and enforced by [`AppendTupleViewToCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L661).

## Interactions and cross-links

- **Overlapping KB areas**:
  - [`off_path_local_table_distribution_buffering_and_copy_costs.md`](../../../citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md)
  - [`connection_management_control_plane.md`](../../../citus/connection-management/connection_management_control_plane.md)
- **Shared code / canonical docs**:
  - grounded off-path local-table COPY path: [`off_path_local_table_distribution_buffering_and_copy_costs.md`](../../../citus/data-movement/off_path_local_table_distribution_buffering_and_copy_costs.md)
  - target prototype design: [`off_path_local_table_rdma_tuple_service_first_prototype.md`](../../../future-directions/citus/data-movement/off_path_local_table_rdma_tuple_service_first_prototype.md)
  - transport/control design: [`rdma_transport_control_plane_abstraction.md`](../../../future-directions/citus/transport/rdma_transport_control_plane_abstraction.md)
- **Implementation note / factual note / future-direction note**:
  - this doc is the canonical implementation-progress note for the current tuple-route checkpoint

## Future directions / design ideas

- **Status**: `active`
- **Goal**: replace the current local materialize-and-discard checkpoint with a real queue-backed tuple path and eventually with the external-service/RDMA data plane
- **Grounding in current code**:
  - sender still continues through [`SendCopyDataToPlacement()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c#L2480)
  - [`PollCitusTupleRouteBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c#L778) is still stubbed
- **Candidate approaches**:
  - publish local batches into an SPSC shared-memory FIFO for loopback validation before adding RDMA
  - introduce a borrow-style receive API in parallel with the current copy-out API
  - decide whether generated/dropped-column omission needs a more explicit column-map contract on the receiver side
- **Risks / tradeoffs**:
  - the current `natts`-wide format may diverge from what the eventual worker-side insert contract wants
  - copy-out semantics are easier to debug now but do not represent the intended zero-copy-ish receive direction
  - immediate sender-side materialization on every placement adds overhead while still not replacing the COPY path
- **Next experiments / implementation steps**:
  - replace materialize-and-discard with a local sender-service FIFO
  - add a matching receiver-side local FIFO and route-consume loop
  - keep the SQL/libpq control plane but move service-to-service route control out of the temporary worker UDF seam

## Gotchas

- This checkpoint changes the tuple-route protocol details from what earlier design notes described. The implementation is now the canonical source for the current batch format.
- The earlier in-backend paired-handle loopback checkpoint has now been replaced by the standalone service process and shared-memory queue rendezvous. The current code does exercise a real cross-process boundary, but the service still only mirrors queue slots locally and does not yet perform RDMA transport.
- The older worker-side SQL/UDF scaffold was only temporary validation scaffolding and has since been removed from the current tree. This checkpoint remains useful for the tuple queue/batch substrate, not for the later worker-side orchestration shape.
- The route registry is an implementation expedient for this checkpoint. It is the cheapest way to rendezvous backends and the standalone service before a dedicated service-control path exists. It should not be mistaken for the settled long-term route-open/control protocol.

## Open questions / TODO

- When we add the worker-side queue path, should the receive API expose borrowed tuple views first-class instead of relying on `CopyOut...`?
- Do generated-stored attributes need a dedicated attribute-map contract instead of today’s explicit omitted/null flags?
- When the local FIFO arrives, should `SubmitCitusTupleRouteBatch()` become the queue-publication point or should the queue own the slot from `Reserve...` onward?
