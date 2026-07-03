# Homer current implementation checkpoint

## Scope

- **What this doc explains**: the current landed Homer implementation across the Citus-facing frontend, the standalone Homer service, shared ABI headers, RDMA peer transport, tuple COPY, client SQL pgbench, and basebackup payload streams.
- **What this doc does NOT cover**: full future DPU placement, a final service-file split, a runtime frontend transport vtable, or a canonical cross-platform tuple wire format.
- **Primary directory**: `docs/kb/implementations/citus/transport/`
- **Doc type**: `implementation`

This is the current implementation map after the frontend/service separation refactor. Older checkpoint notes remain useful for milestone history, but several of their code pointers predate the refactor from `remote_execution_session.*` and `tuple_sink_service.*` into the current `homer_*` frontend files.

## Current Status

The active Homer stack is now split into:

- a PostgreSQL/Citus-facing frontend API in [`homer_frontend.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:1)
- frontend implementation files for session orchestration, control-message construction, POSIX SHM transport, queue attachment, tuple-view encoding, Citus policy, and transaction callbacks
- shared fixed-width ABI headers in `src/include/distributed/homer/homer_*_abi.h`
- the standalone service process in [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1)
- the service-side RDMA peer transport in [`remote_execution_peer_transport_rdma.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1)
- in-progress DPU DMA migration scaffolding in the standalone service scheduler:
  pulled command slots can be copied into a bounded service-owned dispatch queue,
  executed through a DPU response owner, and retained in a bounded
  pending-response queue for later Stage 8 DMA publication. This is not yet a
  host-visible DPU command path.

The mechanical separation has been validated with static include/link checks, local Homer smoke tests, remote RDMA pgbench, and remote RDMA basebackup. The main current operational caveat is not code-level: farnet lane-0 MTU must be consistent across hosts for RDMA-CM/Homer validation.

## Current Code Map

### Public Frontend API And Build Boundary

Ordinary PostgreSQL/Citus backend call sites include [`homer_frontend.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:1). The main public session/command/data-plane entry points are implemented in [`homer_frontend.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:1):

- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:265) opens the frontend-visible session through the local service.
- [`StartRemoteExecutionCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:521) and [`PollRemoteExecutionCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:582) drive typed commands.
- [`TryReserveRemoteSendBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:901), [`AppendTupleViewToRemoteSendStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:1093), and [`BorrowNextRemoteTupleView()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:1221) expose the current tuple-stream API.

The build boundary is explicit in [`src/backend/distributed/Makefile`](/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:46). `HOMER_FRONTEND_OBJS` names the frontend object group, `HOMER_SERVICE_OBJS` names service/RDMA objects, and [`OBJS = $(filter-out $(HOMER_SERVICE_OBJS),$(DIST_EXTENSION_OBJS))`](/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:61) keeps service-only code out of `citus.so`. [`SHLIB_LINK = $(libpq)`](/data/dbcomm/citus-dbcomm/src/backend/distributed/Makefile:73) keeps ordinary backend linkage free of `librdmacm` and `libibverbs`; the top-level [`Makefile`](/data/dbcomm/citus-dbcomm/Makefile:50) still links the standalone service with RDMA libraries.

### Frontend Internal Split

The current frontend implementation split is:

- [`homer_frontend_control.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1): semantic request builders and fixed-width conversion helpers. [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:333) builds the tuple contract carried in open-session control messages.
- [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1): current local POSIX shared-memory control channel. [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:46) is the frontend map/open boundary for the service control region.
- [`homer_citus_policy.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_citus_policy.c:1): Citus/PostgreSQL policy, spec initialization, and frontend feature gates.
- [`homer_citus_xact.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_citus_xact.c:1): transaction-finalization callbacks. [`RemoteExecutionSessionsPrepareAtPreCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_citus_xact.c:232), [`RemoteExecutionSessionsCommitAtCommit()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_citus_xact.c:319), and [`RemoteExecutionSessionsAbortAtAbort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_citus_xact.c:381) preserve the Citus transaction lifecycle integration.
- [`homer_tuple_queue_frontend.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c:1): queue attachment, byte-ring reservation/publication, batch lifetime, and queue diagnostics.
- [`homer_tuple_view_codec.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_view_codec.c:1): PostgreSQL-dependent tuple-view row encode/decode. [`TryAppendTupleViewToCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_view_codec.c:177) handles sender-side row materialization, while [`BorrowNextTupleViewFromCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_view_codec.c:443) handles receive-side row borrowing.

This split is still host-frontend code, not a DPU library. It deliberately keeps PostgreSQL executor types in the frontend API/codec layer and keeps them out of fixed-width ABI headers.

### Shared ABI Headers

The fixed-width ABI is now owned by split headers:

- [`homer_abi_version.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_abi_version.h:1): protocol versions, capacity constants, and fixed byte limits.
- [`homer_tuple_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:1): tuple-stream semantic structs, including [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:114).
- [`homer_queue_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:1): queue/ring descriptors, including [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:100).
- [`homer_control_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_control_abi.h:1): local semantic control requests/responses, including [`CitusRemoteExecControlRequestHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_control_abi.h:268).
- [`homer_completion_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:1): command completion records, client completion mailboxes, result descriptors, and peer completion rings.
- [`homer_shm_channel_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_shm_channel_abi.h:1): current POSIX SHM object names and local control-region layout, including [`CITUS_REMOTE_EXEC_CONTROL_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_shm_channel_abi.h:25) and [`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_shm_channel_abi.h:60).
- [`homer_basebackup_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_basebackup_abi.h:1): typed basebackup payload objects, including [`CitusRemoteBaseBackupMessageHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_basebackup_abi.h:44).

The older protocol headers now act as compatibility include surfaces where needed. New code should include the split `homer_*_abi.h` owner directly when it depends on one concrete ABI family.

### Standalone Service And RDMA Transport

The standalone service remains monolithic in [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1). It owns local control-slot dispatch, service/session/sink tables, local command mailboxes, backend-spawn coordination, payload-stream progress, and peer-control orchestration. The service starts from [`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36256).

The RDMA peer transport is service-only. [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7961) initializes listener and peer state, uses RDMA-CM listener setup around [`rdma_bind_addr()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8097) and [`rdma_listen()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8118), and outgoing connections resolve/connect through [`rdma_getaddrinfo()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5637) and [`rdma_connect()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5844).

The canonical send-CQ drain entry point is [`TupleSinkServiceDrainPeerSendCqRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3962), declared with its ownership comment in [`remote_execution_peer_transport_rdma.h`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:1025). Normal frontend/backend code should not gain RDMA verbs or RDMA-CM dependencies.

## Implemented Workload Paths

### Citus Backend-To-Backend Tuple COPY

The coordinator COPY path enters Homer from [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2883) and publishes rows through [`MaterializeExperimentalRemoteExecutionSessionTuple()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3131). Worker startup is driven by [`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2650) and closed by [`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2817).

The service publishes local worker commands through [`TupleSinkServicePublishLocalCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18560) and waits for startup through [`TupleSinkServiceWaitForCommandStartup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19228). The socketless backend command loop is [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2456).

The worker hot path is [`WorkerTupleSinkInsertExecuteCopyIngestCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:214). It borrows tuple views from the receive batch and stores them into executor slots without first copying by-reference datums into a separate worker-owned array.

The remote receive service path semantically rebuilds the local receive sink through [`TupleSinkServiceDecodeIncomingTupleViewBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20188), which decodes individual rows in [`TupleSinkServiceDecodeTupleViewRowIntoReceiveBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19990).

### Client SQL Pgbench

The current pgbench-facing path uses typed client SQL session commands rather than transparent PostgreSQL frontend protocol emulation. The SQL wrapper entry point for the current backend-visible smoke is [`citus_remote_exec_pgbench_transaction()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/remote_exec_pgbench_transaction.c:213). Row-producing SQL results are sent through the backend bridge destination receiver; [`RemoteExecSqlDestReceiveSlot()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1463) appends tuples into the result sink.

The validated runbook shape is remote `pgbench --homer` from farnet0 to the farnet1 service/backend using `--homer-peer-host 10.10.1.101`. It validates local frontend control, service-to-service RDMA command/completion transport, socketless backend dispatch, and tuple-result return through Homer.

### Basebackup Payload Streams

PostgreSQL registers `TARGET 'homer'` through [`builtin_backup_targets`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_target.c:42) and constructs the Homer sink in [`homer_get_sink()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_target.c:212). [`SendBaseBackup()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup.c:988) keeps PostgreSQL responsible for backup consistency, filesystem traversal, tar generation, manifest generation, and replication-command lifecycle.

The Homer sink is implemented in [`basebackup_homer.c`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:1). [`bbsink_homer_apply_detail()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:81) parses the `TARGET 'homer:...'` detail string; [`bbsink_homer_begin_backup()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:236) opens the Homer stream and exposes the first reserved payload slot as `bbs_buffer`; [`bbsink_homer_archive_contents()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:290) publishes archive chunks without copying into a second staging buffer; [`bbsink_homer_end_backup()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:359) publishes the terminal object and closes the stream.

On the Citus/Homer client side, [`HomerClientOpenBaseBackupStream()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1754) opens the stream, [`HomerClientReserveBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2142) reserves payload space, and [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2159) publishes typed basebackup objects.

The service data path treats basebackup as one payload-object family on the generic payload stream. [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27648) publishes local payload objects to the peer over RDMA; [`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:29496) consumes peer-written payload objects; [`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28267) is only a local smoke receiver, not a remote RDMA validation path.

### DPU DMA Migration Scaffolding

The DPU path is currently an implementation scaffold inside
[`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1),
not an accepted production transport. The current scheduler-facing slice has
three separate DPU command stages:

- `HOMER_PROGRESS_ACTION_DPU_COMMAND_PULL` submits bounded command-slot DMA
  pulls into the DPU DMA engine.
- `HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_DISPATCH` copies engine-owned
  staged commands into service-owned dispatch slots and releases the transient
  engine staging buffer.
- `HOMER_PROGRESS_ACTION_DPU_STAGED_COMMAND_EXECUTE` runs one service-owned
  staged command through `TupleSinkServiceDispatchLocalControlSlot()` with a DPU
  response owner, then stores the completed response in a bounded
  pending-response queue.

The pending-response queue is intentionally not host-visible. Stage 8 must drain
it by DMA-writing the response body followed by the response-ready publication
word on the same ordered DOCA DMA context. Until that exists, selected DPU command
API acceptance cannot be claimed.

The selected-DPU payload/basebackup path has progressed into Stage 9 mirrored
byte-ring implementation. The remote RDMA basebackup runtime gate now passes
through the selected-DPU mirror-only path, but mixed foreground command plus
background basebackup validation remains future Stage 9 promotion work.
Host-side basebackup publishing in
[`HomerClientReserveBaseBackupRecordForObject()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2786)
and
[`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2989)
now uses exact source-record sizes instead of a fixed selected-DPU source
envelope. The temporary fixed-envelope protocol was rejected because it added
source slack that was neither semantic payload nor physical wrap gap. The current
PostgreSQL `bbsink` integration uses a scratch buffer to adapt the generic
`bbsink` producer contract: it learns `archive_contents(len)`, reserves exactly
the transport header plus semantic header plus payload bytes through
`HomerClientReserveBaseBackupRecordForObjectPayload()`, copies the object into
the exact byte-ring record, and publishes that exact record. This is correct for
Stage 9 cleanup, but not the final no-copy shape; the later performance cleanup
should move reservation into producer-loop sites before archive bytes are read.

On the service side,
[`HomerServicePumpOutgoingDpuMirrorByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28827)
parses only bytes present in the completed DPU mirror range while using the
accepted host tail only to prove physical wrap. A full matching header whose
body is not yet mirrored is treated as a capped-DMA local view and requests a
mirror append; a full nonmatching header is a protocol/visibility failure.
[`HomerServiceAppendOutgoingDpuMirrorBaseBackupFragmentWrite()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16588)
now forwards exact basebackup object bytes from the DPU-local mirror and no
longer carries fixed-envelope slack. Source-skip is reserved for physical wrap
gaps. Grouped-control discovery is kept as bounded known work while host rings
are imported, because host tail publication has no separate doorbell and
suppressing grouped-control reads can deadlock a full producer ring waiting for
credit.

## Milestone History And Design Corrections

- **Remote execution session wrapper**: the early wrapper checkpoint introduced a backend-visible session API over the tuple-route substrate. See [remote_execution_session_wrapper_checkpoint.md](../connection-management/remote_execution_session_wrapper_checkpoint.md).
- **Local service-owned control path**: local SHM control ownership moved into the standalone service, while backend code still saw `RemoteExecutionSession`. See [local_service_owned_tuple_sink_control_checkpoint.md](../connection-management/local_service_owned_tuple_sink_control_checkpoint.md).
- **Cross-node tuple sink**: the tuple-sink vertical slice added full tuple-view contract exchange, peer-provisioned exact sinks, receive-side semantic decode, typed worker commands, and worker borrow/use/release. See [cross_node_tuple_sink_peer_control_checkpoint.md](../connection-management/cross_node_tuple_sink_peer_control_checkpoint.md).
- **Peer completion and payload publication**: v14/v15/v27 and byte-ring checkpoints landed immediate-data dispatch, payload doorbell tokens, peer-client completion mailboxes, full-slot completion publication, and byte-ring payload-stream improvements. See [peer_recv_dispatcher_v14_checkpoint.md](peer_recv_dispatcher_v14_checkpoint.md), [payload_doorbell_token_v15_checkpoint.md](payload_doorbell_token_v15_checkpoint.md), [peer_client_completion_v27_checkpoint.md](peer_client_completion_v27_checkpoint.md), and [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md).
- **Client SQL pgbench**: typed client SQL sessions proved the foreground pgbench transaction shape through Homer before the later refactor. See [client_sql_session_pgbench_checkpoint.md](../../postgres/client-sql-session/client_sql_session_pgbench_checkpoint.md).
- **Basebackup**: `TARGET 'homer'` proved the server-side `bbsink` integration and RDMA payload-stream reuse for basebackup archive/manifest objects. See [homer_base_backup_target_checkpoint.md](../../postgres/replication/homer_base_backup_target_checkpoint.md).
- **Frontend/service separation**: the latest refactor moved the public frontend header to `homer_frontend.h`, split frontend internals, split ABI ownership, and kept service/RDMA objects out of `citus.so`. See [homer_frontend_service_separation_plan.md](../../../future-directions/citus/transport/homer_frontend_service_separation_plan.md).
- **DPU command-pull scaffolding**: the current DPU migration has progressed
  through grouped-control discovery, command-slot DMA pull, service-owned staged
  dispatch, and a bounded executed-response pending-publication queue. This
  validates scheduler integration and response-owner lifetimes, but Stage 8
  response DMA publication is still required before a selected DPU command can
  complete on the host.
- **Selected-DPU mirrored basebackup**: Stage 9 validation corrected the
  byte-ring wrap protocol for capped DMA pulls. Padding records and a
  selected-DPU wrap flag were removed; regular pre-wrap transport headers now
  prove wrap gaps when a full header fits, and accepted host tails prove only
  trailer-smaller-than-header gaps. Follow-up design review rejected the fixed
  selected-DPU source envelope as a steady-state protocol. Stage 9 cleanup now
  publishes exact source-record sizes via a scratch-buffer adapter, while the
  final performance cleanup should move exact reservation into the PostgreSQL
  producer loop and remove that scratch copy.

Important corrections that survived validation:

- A separate `libhomer_frontend.a` was deferred. The current first-pass build boundary is the explicit `HOMER_FRONTEND_OBJS` object group linked into `citus.so`; that is enough to keep service/RDMA objects out of the extension while preserving behavior.
- `tuple_sink_service_process.c` remains intentionally monolithic. Splitting it is a later service-internal pass, not part of the frontend/service boundary refactor.
- `BuildTupleViewContractFromTupleDesc()` remains in the control-request builder layer because it builds the tuple contract carried by `OPEN_SESSION`; tuple-row byte encode/decode lives in `homer_tuple_view_codec.c`.
- The RDMA-CM failure during final validation was not fixed by changing Homer. The root cause was host lane-0 MTU mismatch: farnet1 `enp33s0f0np0` was MTU `9000` while farnet0 `enp33s0f0np0` was MTU `1500`. After setting farnet0 lane 0 to MTU `9000`, RDMA-CM perftest and remote Homer workloads passed.

## Validation Snapshot

Post-refactor static/build checks:

- old public include names `distributed/homer/remote_execution_session.h` and `distributed/homer/tuple_sink_service.h` no longer appear under `src`
- frontend split files have no RDMA verbs/RDMA-CM references
- fixed-width `homer_*_abi.h` headers have no PostgreSQL executor, `Datum`, `TupleDesc`, or `TupleTableSlot` dependencies
- installed `citus.so` depends on `libpq` but not `librdmacm` or `libibverbs`
- installed `citus_tuple_sink_service` depends on `librdmacm` and `libibverbs`
- `pgbench`, `citus_tuple_sink_service`, and `libhomer_client.a` agree on `/citus_remote_execution_control_v27`

Runtime validation after rebuilding/installing and syncing `/data/dbcomm/pg-citus` to farnet0:

- local `pgbench --homer`: `1000/1000`, `0` failures, about `6123 TPS`
- local Homer basebackup blackhole: completed, `real 4.02`
- remote RDMA-CM perftest after MTU correction: `client_rc=0`, `server_rc=0`, about `237.51 Gbit/s` average
- remote pgbench c1 smoke: `2000/2000`, `0` failures
- warmed remote pgbench c1: `20000/20000`, `0` failures, about `4104.75 TPS`, p99 `0.265 ms`
- remote pgbench c4: `40000/40000`, `0` failures, about `10134.20 TPS`, p99 `0.589 ms`
- remote RDMA basebackup: first run `real 6.22`, warmed repeat `real 4.14`
- service log scans after the corrected run had no `status=12`, transport retry, fatal, panic, or assertion lines
- selected-DPU remote RDMA basebackup after the no-padding wrap correction and
  no-stats rebuild:
  `pg_basebackup -X none -c fast -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2'`
  completed in `real 51.34`; receiver final head WIMM reported
  `final_tail=23732382600`, and DPU/receiver/postgres log scans had no
  protocol reset, stale header, DOCA task failure, timeout, fatal, panic, or
  assertion lines. The DPU log scan matched only expected reclaim and setup
  import-closing lifecycle lines.
- selected-DPU remote RDMA basebackup after exact source-record cleanup and
  DPU import teardown ownership cleanup:
  `pg_basebackup -X none -c fast -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2'`
  completed in `real 27.60`. The fixed-envelope/source-skip issue did not
  recur, PostgreSQL logged no errors, and the DPU owner-abort path released the
  borrowed mirror source instead of wedging teardown. This is still not a clean
  Stage 9 acceptance run: DPU and receiver service logs reported RDMA send/recv
  CQ close/reset messages after receiver quiesce, so peer close ordering remains
  follow-up lifecycle work.

The remote pgbench/basebackup numbers are validation checkpoints, not final tuned benchmark claims.

## Known Gaps And Caveats

- The service-side process remains a large monolithic file. It should be split into service modules only after the current frontend/service boundary is stable.
- The current local frontend channel is still POSIX shared memory. The DPU/DMA channel is intentionally future work; ordinary Citus call sites should not grow direct DMA or RDMA dependencies.
- The in-progress DPU command path has an internal pending-response queue but no
  host-visible response DMA publication yet. A command response stored there is
  durable service state for Stage 8, not a frontend completion.
- The tuple row format is still a prototype tuple-view row layout, not a canonical cross-platform wire serialization.
- The remote pgbench c4 path is correct but should still be treated as a shared command/completion transport scaling checkpoint, not final multi-client performance.
- Basebackup remote RDMA currently validates blackhole/consume semantics on the receiver service; remote materialization is future work.
- Selected-DPU basebackup correctness is validated for the standalone remote
  RDMA basebackup path. Mixed foreground command plus background basebackup
  validation is still required before treating Stage 9 as the complete migrated
  workload-interference baseline.
- Selected-DPU basebackup currently has a residual peer RDMA teardown caveat:
  successful runs may still close by reset after receiver quiesce. That no
  longer corrupts or wedges DPU mirror ownership, but the log scan is not clean
  and the peer close ordering should be fixed before Stage 9 is accepted. The
  current DPU setup `CLOSE_ACK` path proves only a narrow DMA-import drain; the
  Stage 10 plan now requires a scheduler-owned close-drain proof that also waits
  for borrowed mirrored byte ranges, RDMA send owners, receiver-head/final-close
  owners, ready refs, staged command/response queues, and payload close ops
  before the DPU sends `CLOSE_ACK` and before the host destroys exported mmap
  resources.
- The current farnet lane-0 validation path requires matching host MTUs. If farnet1 `enp33s0f0np0` is MTU `9000`, farnet0 `enp33s0f0np0` must also be MTU `9000`.

## Related

- [homer_frontend_service_separation_plan.md](../../../future-directions/citus/transport/homer_frontend_service_separation_plan.md): Detailed phased refactor plan and final acceptance evidence.
- [homer_transport_scheduler_and_payload_streams.md](../../../future-directions/citus/transport/homer_transport_scheduler_and_payload_streams.md): Scheduler and payload-stream design history.
- [homer_payload_control_unification_plan.md](../../../future-directions/citus/transport/homer_payload_control_unification_plan.md): Future unification of byte-ring payload lifecycle and peer command/control facts.
- [doca_host_dpu_homer_boundary.md](../../../future-directions/citus/transport/doca_host_dpu_homer_boundary.md): Future host/DPU placement plan grounded in the current frontend/service boundary.
