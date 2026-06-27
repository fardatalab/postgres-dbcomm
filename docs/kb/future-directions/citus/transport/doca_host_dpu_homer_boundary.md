# DOCA Host-DPU Homer Boundary

## Scope

- **What this doc explains**: how the current Homer host-process boundary maps to a future host-DPU boundary, and which local DOCA APIs look relevant for DMA, progress, batching, and notification.
- **What this doc does NOT cover**: a finalized wire format, DPA-kernel porting, or a complete implementation plan. Measured DOCA behavior now lives in [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md); this note imports only the design consequences needed for the host-DPU boundary.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The long-term target is a DPU-offloaded communication stack for PostgreSQL/Citus. The current Homer prototype is not offloaded yet: PostgreSQL/Citus backends communicate with a standalone `citus_tuple_sink_service` process on the same host through POSIX shared memory, and the service then drives peer RDMA/control/payload work. Enabling DPU offload means the first boundary to replace is the local backend-to-Homer process boundary, turning it into a host-to-DPU boundary while preserving the current session, command, completion, and payload-stream semantics.

The main correction from a naive "use DOCA DMA" framing is that DMA is only the data-movement piece. The current boundary also has control-slot ownership, wakeups/progress, completion rings, and payload publication rules. A DPU plan therefore needs at least:

- a data path for host memory rings and DPU-local staging/state
- a control path for session/open/start/close and descriptor exchange
- a notification/progress path for wakeups or bounded polling
- a responder-visible publication rule equivalent to the current peer RDMA doorbell rule where correctness depends on it

## Current Design Status

The first important prerequisite is now complete: Homer has a clearer
PostgreSQL/Citus frontend versus service/backend split. The completed split is
recorded in [homer_frontend_service_separation_plan.md](homer_frontend_service_separation_plan.md).
The relevant current shape is:

- PostgreSQL/Citus backend callers include the public frontend API
  [`homer_frontend.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:1).
- The current POSIX-SHM frontend channel is isolated in
  [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1)
  and declared privately through
  [`homer_frontend_shm.h`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.h:1).
- Semantic request construction is separated into
  [`homer_frontend_control.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_control.c:1),
  while frontend policy/glue and tuple queue mechanics live in separate frontend
  files.
- The standalone service remains in
  [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1),
  and service-side RDMA stays out of `citus.so`. The split plan validated this
  with dynamic dependency checks and remote RDMA pgbench/basebackup acceptance.
- Shared fixed-width protocol declarations now live in the `homer_*_abi.h`
  headers, such as [`homer_control_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_control_abi.h:1),
  [`homer_queue_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:1),
  and [`homer_completion_abi.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:1).

This changes the DPU migration boundary. The DPU work should not be grafted onto
ordinary Citus call sites and should not re-entangle service code into the
frontend. The natural replacement point is the private frontend channel layer:
today it is POSIX SHM; the next transport can be a host-DPU bridge that presents
the same semantic request/queue/completion ABI to the frontend and service.

## Current Homer Boundary To Preserve

The backend-facing control region is a fixed shared-memory slot array in the
current SHM transport. [`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_shm_channel_abi.h:60)
contains the protocol version, slot count, heartbeat counter, and
`CitusRemoteExecControlSlot` array. The frontend SHM implementation in
[`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1)
opens the current local control shared-memory object, maps it, claims slots, and
waits for responses.

Command completions are not just a status word.
[`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:425)
is a small SPSC completion ring for frontend client SQL sessions, while
[`CitusRemoteExecPeerCommandCompletionRing`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:471)
is the requester-service-owned peer completion ring. The DPU bridge must
preserve the same ring semantics even if the storage backing changes from POSIX
SHM objects to registered host memory and DPU-imported descriptors.

Session open is already service-owned. [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:265)
uses the frontend implementation to open command sessions through the local
service, map or attach completion rings for SQL command sessions, and open
tuple-sink queues for tuple payload operations. This remains the backend API to
keep stable while the local channel changes from host-process SHM to host-DPU
DMA/doorbells.

The standalone service loop is scheduler-driven, not a blocking per-request loop. [`TupleSinkServiceControlState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1856) stores the local control-region mapping. [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1936) splits service work into RDMA CM, peer control, local control, command ring, completion ring, payload stream, CQ drain, target completion, and heartbeat sources. [`HomerServicePumpMachineBaselineOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:36089) builds ready sets and execution plans, then runs the selected source executors.

Local control is a shared-memory state machine today. [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33938) scans ready control slots, advances async continuations, publishes responses, and bumps the heartbeat before cold work such as RDMA peer opens. Its async command open path, [`TupleSinkServiceProgressCommandOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:31527), can establish peer connections, register peer-visible memory, and wait for peer responses over multiple service passes.

Payload queues are SPSC rings with explicit head/tail state. [`CitusTupleSinkQueueControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:63) is the backend-visible queue control block. [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_queue_abi.h:79) is the service-to-service payload-ring control block. [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:226) is the service-to-service opaque payload header. Any host-DPU path should preserve this SPSC shape initially because it maps well to cache-line polling, DMA batches, and simple ownership.

The existing peer RDMA publication rule is stronger than memory polling. [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7170) writes a control-ring message and uses a final `WRITE_WITH_IMM` tail update as the responder-visible publication doorbell. [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:9754) is the helper for that final 64-bit publish write. The local host-DPU boundary needs its own publication/wakeup rule; DOCA DMA completion on the submitter alone is not the same as remote peer visibility.

## DOCA APIs Found Locally

All DOCA paths below are from the local installation under `/opt/mellanox/doca/`.

### DMA And Memory Registration

DOCA DMA exposes asynchronous memcpy tasks. [`doca_dma_create()`](/opt/mellanox/doca/include/doca_dma.h:105) creates a DMA context, [`doca_dma_as_ctx()`](/opt/mellanox/doca/include/doca_dma.h:147) turns it into a generic `doca_ctx`, [`doca_dma_task_memcpy_set_conf()`](/opt/mellanox/doca/include/doca_dma.h:222) configures completion/error callbacks and task count, and [`doca_dma_task_memcpy_alloc_init()`](/opt/mellanox/doca/include/doca_dma.h:247) creates a copy task from source and destination `doca_buf`s. [`doca_dma_task_memcpy_set_src()`](/opt/mellanox/doca/include/doca_dma.h:273) and [`doca_dma_task_memcpy_set_dst()`](/opt/mellanox/doca/include/doca_dma.h:295) let a completed task be reused with new buffers.

`doca_mmap` is the memory registration and export layer. [`doca_mmap_create()`](/opt/mellanox/doca/include/doca_mmap.h:102), [`doca_mmap_add_dev()`](/opt/mellanox/doca/include/doca_mmap.h:199), [`doca_mmap_set_memrange()`](/opt/mellanox/doca/include/doca_mmap.h:427), and [`doca_mmap_start()`](/opt/mellanox/doca/include/doca_mmap.h:152) build a local registered range. [`doca_mmap_export_pci()`](/opt/mellanox/doca/include/doca_mmap.h:277) exports that range for a same-PCI device such as the DPU, while [`doca_mmap_create_from_export()`](/opt/mellanox/doca/include/doca_mmap.h:394) imports the descriptor on the other side. [`doca_mmap_set_dpa_memrange()`](/opt/mellanox/doca/include/doca_mmap.h:490) exists for DPA heap memory, but a DPA-kernel design is a later step.

Permissions matter for direction. [`DOCA_ACCESS_FLAG_PCI_READ_ONLY`](/opt/mellanox/doca/include/doca_types.h:87) allows same-PCI device reads from host memory. [`DOCA_ACCESS_FLAG_PCI_READ_WRITE`](/opt/mellanox/doca/include/doca_types.h:91) allows same-PCI reads and writes. [`DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`](/opt/mellanox/doca/include/doca_types.h:95) can improve performance but should not be enabled until the publication protocol explicitly tolerates reordering.

The sample shape for host-to-DPU DMA read is clear:

- host side: [`dma_copy_host()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_host/dma_copy_host_sample.c:102) sets `DOCA_ACCESS_FLAG_PCI_READ_ONLY`, registers the host buffer, exports the mmap, and writes the export descriptor plus host virtual address/length out for the DPU sample
- DPU side: [`dma_copy_dpu()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:162) imports the host mmap with `doca_mmap_create_from_export()`, creates source and destination `doca_buf`s, allocates a DMA memcpy task, submits it, and progresses the PE until completion

The sample uses files for descriptor transfer, but that is only demo scaffolding. Homer should exchange descriptors over an explicit control channel, not files.

### Progress, Async Completion, And Batching

DOCA contexts are driven by a progress engine. [`doca_ctx_start()`](/opt/mellanox/doca/include/doca_ctx.h:156) starts a context after configuration. [`doca_pe_create()`](/opt/mellanox/doca/include/doca_pe.h:164) creates a PE, and [`doca_pe_progress()`](/opt/mellanox/doca/include/doca_pe.h:197) polls for completed tasks/events and invokes callbacks. This matches the current Homer service-progress scheduler: DOCA PE progress can be another first-layer progress source or a subaction inside payload/local-control executors.

One `doca_pe_progress()` call is one progress attempt, not a full CQ-style
drain. The local header says it finds the next context with a completed task,
invokes the callback, and returns `1` if progress was made. A Homer DPU
scheduler should therefore make PE draining explicit: drain until empty for
throughput classes, or drain up to a bounded budget for latency-sensitive
classes.

Task submission is nonblocking. [`doca_task_submit()`](/opt/mellanox/doca/include/doca_pe.h:330) routes a task to its context asynchronously, and ownership transfers to DOCA until completion. [`doca_task_try_submit()`](/opt/mellanox/doca/include/doca_pe.h:388) validates task inputs but the DOCA sample README says this is for development because validation hurts performance. [`doca_task_submit_ex()`](/opt/mellanox/doca/include/doca_pe.h:348) accepts submit flags.

There are two relevant batching knobs:

- [`DOCA_TASK_SUBMIT_FLAG_FLUSH`](/opt/mellanox/doca/include/doca_pe.h:76): without this flag, the context may aggregate tasks and flush to hardware later; with it, this task and earlier tasks are flushed
- [`DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`](/opt/mellanox/doca/include/doca_pe.h:84): allows deferred completion callbacks and fewer hardware completion notifications for a batch

`OPTIMIZE_REPORTS` is DOCA/initiator-side completion-report coalescing, not
application batching. It can reduce callback/report overhead for many DMA tasks
submitted to the same DMA context, including tasks from different sessions. It
does not merge records, rings, DPU-side sinks, or per-session tail publication.
If `K > 1` records are contiguous in both source and destination and have the
same publication fate, prefer one larger DMA task and one tail update. Multiple
small DMA tasks before one publication still make sense when records live in
separate per-session rings, wrap around a ring, have different credits/lifetimes,
or would require an extra staging copy to make contiguous.

DOCA also has task-batch APIs. [`doca_task_batch_submit()`](/opt/mellanox/doca/include/doca_pe.h:367) and [`doca_task_batch_try_submit()`](/opt/mellanox/doca/include/doca_pe.h:409) exist, with supported batch sizes from 16 to 128 tasks in [`doca_task_batch_max_tasks_number`](/opt/mellanox/doca/include/doca_pe.h:48). These are experimental in this installed header set.

Task reuse is the API pattern to prefer for Homer. The PE resubmit sample explicitly says task resubmission can improve performance and reduce memory use. [`dma_task_resubmit()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:98) replaces source/destination buffers on a completed DMA task and resubmits it. [`create_dma()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:190) preconfigures four DMA tasks, and [`run()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:314) allocates buffers/tasks once before submitting. This is the right shape for Homer payload streams: preallocate per-stream or per-lane task pools and recycle them.

Treat the DMA context as the ordering, completion, and report-coalescing domain.
Treat the PE as the progress domain. Multiple DMA contexts may share one PE, but
same-thread submission to two contexts is not a publication-ordering rule. The
split-context validation showed that publishing a tail from ctx B before payload
completion on ctx A can expose stale data for small records. Keep a payload and
the publication that depends on it in the same DMA context when possible; if a
scheduler split requires separate contexts, publish only after an explicit
completed frontier from the payload context.

For event-driven operation, [`doca_pe_request_notification()`](/opt/mellanox/doca/include/doca_pe.h:293), [`doca_pe_get_notification_handle()`](/opt/mellanox/doca/include/doca_pe.h:218), and [`doca_pe_clear_notification()`](/opt/mellanox/doca/include/doca_pe.h:272) support an `epoll` style flow. The event sample's [`run_for_completion()`](/opt/mellanox/doca/samples/doca_common/pe_event/pe_event_sample.c:215) drains `doca_pe_progress()`, arms notification, waits in `epoll_wait()`, clears notification, and repeats. The sample README warns that event-driven mode reduces CPU utilization but may increase latency or reduce performance. For Homer, polling is the likely hot-path default; event mode is plausible for idle/cold setup paths.

### Notification And Doorbells

DOCA DMA completion is local to the submitter's PE. It answers "my DMA copy task completed," not "the other side has observed a published record."

For a host-DPU control/wakeup mechanism, the useful local APIs are:

- DOCA COMCH for small control messages. [`doca_comch_server_create()`](/opt/mellanox/doca/include/doca_comch.h:198) and [`doca_comch_client_create()`](/opt/mellanox/doca/include/doca_comch.h:396) create endpoints; [`doca_comch_server_task_send_alloc_init()`](/opt/mellanox/doca/include/doca_comch.h:652) and [`doca_comch_client_task_send_alloc_init()`](/opt/mellanox/doca/include/doca_comch.h:679) allocate send tasks; [`doca_comch_server_event_msg_recv_register()`](/opt/mellanox/doca/include/doca_comch.h:738) and [`doca_comch_client_event_msg_recv_register()`](/opt/mellanox/doca/include/doca_comch.h:756) register receive callbacks.
- DOCA Sync Event for a 64-bit cross-unit synchronization value. The header describes it as a software synchronization mechanism across CPU, DPU, DPA, GPU, and remote nodes in [`doca_sync_event.h`](/opt/mellanox/doca/include/doca_sync_event.h:15). [`doca_sync_event_create_from_export()`](/opt/mellanox/doca/include/doca_sync_event.h:215) imports an exported event, publisher/subscriber locations include CPU and remote PCI in [`doca_sync_event_add_publisher_location_cpu()`](/opt/mellanox/doca/include/doca_sync_event.h:332), [`doca_sync_event_add_publisher_location_remote_pci()`](/opt/mellanox/doca/include/doca_sync_event.h:380), [`doca_sync_event_add_subscriber_location_cpu()`](/opt/mellanox/doca/include/doca_sync_event.h:411), and [`doca_sync_event_add_subscriber_location_remote_pci()`](/opt/mellanox/doca/include/doca_sync_event.h:459). Async notify/wait tasks include [`doca_sync_event_task_notify_set_alloc_init()`](/opt/mellanox/doca/include/doca_sync_event.h:728) and [`doca_sync_event_task_wait_neq_alloc_init()`](/opt/mellanox/doca/include/doca_sync_event.h:1082).

The sync-event remote PCI sample combines COMCH descriptor exchange with sync-event wait/notify. [`se_init()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:62) waits for a COMCH-delivered export blob and imports it with `doca_sync_event_create_from_export()`. [`se_communicate_async()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:140) submits async wait and notify tasks.

### Ordering, Visibility, And Publication Assumptions

The local headers do not state that sync-event or COMCH automatically orders arbitrary earlier DMA writes, CPU stores, or writes submitted through another DOCA context. Treating a sync-event update as "like RDMA write-with-imm" is therefore only safe if the Homer protocol creates the ordering edge explicitly.

What is stated in the installed headers:

- DMA task completion is a submitter-side task event. The DMA completion callback type says it is called by [`doca_pe_progress()`](/opt/mellanox/doca/include/doca_dma.h:56) when the related task is identified as completed successfully, and that ownership of the task object returns to the user. It does not state that a later sync-event or COMCH operation orders that DMA write for a remote CPU.
- Ordered DMA completions are only completion-ordering metadata. [`doca_dma_set_ordered_completions()`](/opt/mellanox/doca/include/doca_dma.h:340) says the DMA context will return completions in submission order; it does not state that all destination memory visibility is ordered with other contexts' notify/message tasks.
- [`DOCA_TASK_SUBMIT_FLAG_FLUSH`](/opt/mellanox/doca/include/doca_pe.h:76) flushes this task and previous tasks to hardware because a context may otherwise aggregate tasks. [`doca_ctx_flush_tasks()`](/opt/mellanox/doca/include/doca_ctx.h:293) flushes tasks that were not flushed during submit. These are task-submission flushes, not documented cross-context memory fences between DMA and sync-event/COMCH.
- Sync-event is documented as a 64-bit cross-unit synchronization value in [`doca_sync_event.h`](/opt/mellanox/doca/include/doca_sync_event.h:20). The header exposes notify-set tasks through [`doca_sync_event_task_notify_set_alloc_init()`](/opt/mellanox/doca/include/doca_sync_event.h:728), wait tasks through [`doca_sync_event_task_wait_eq_alloc_init()`](/opt/mellanox/doca/include/doca_sync_event.h:956), and synchronous waits through [`doca_sync_event_wait_eq()`](/opt/mellanox/doca/include/doca_sync_event.h:1569). It does not explicitly say the wait has acquire semantics over arbitrary separately written memory, or that notify has release semantics for arbitrary earlier DMA/CPU writes.
- COMCH send and receive are message semantics. [`doca_comch_server_task_send_alloc_init()`](/opt/mellanox/doca/include/doca_comch.h:652) and [`doca_comch_client_task_send_alloc_init()`](/opt/mellanox/doca/include/doca_comch.h:679) allocate send tasks; [`doca_comch_server_event_msg_recv_register()`](/opt/mellanox/doca/include/doca_comch.h:738) and [`doca_comch_client_event_msg_recv_register()`](/opt/mellanox/doca/include/doca_comch.h:756) register receive callbacks. The headers do not state that receiving a COMCH message is a visibility barrier for separate DMA buffers.
- [`DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`](/opt/mellanox/doca/include/doca_types.h:95) explicitly allows the system to reorder mapped-memory accesses. Do not enable it for host-DPU rings until the publication protocol has been validated under that mode.

What the samples imply, but do not prove for Homer:

- [`dma_copy_dpu()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:299) allocates a DMA memcpy task, [`doca_task_submit()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:314) submits it, and the sample reads the DPU destination buffer only after the PE loop observes task completion in [`doca_pe_progress()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:323). This supports using DMA completion before local reuse/read of the task destination; it does not prove a DMA-plus-sync-event publication contract.
- [`se_init()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:62) uses COMCH to transfer a sync-event export blob, while [`se_communicate_sync()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:100) and [`se_communicate_async()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:140) show wait/notify. They demonstrate event signaling, not data-buffer publication after DMA.

The conservative Homer rule should be:

1. Data bytes and descriptors are written first.
2. The producer observes completion or executes a CPU release operation appropriate to how those bytes were produced.
3. Only after that does the producer publish a monotonic 64-bit epoch/tail, preferably as a separate publish operation.
4. A sync-event or COMCH message is a wakeup carrying, or pointing to, that epoch. The consumer treats the epoch/tail as the authoritative publication frontier, not the mere existence of a wakeup.
5. After waking, the consumer uses acquire-side loads for CPU-visible shared state and validates the slot sequence/epoch before reading the descriptor or payload.

This mirrors the current peer RDMA design. [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7170) writes the mailbox body and uses a final `WRITE_WITH_IMM` tail update as the responder-visible publication doorbell. [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:9754) implements that final 64-bit publish write. For host-DPU, the equivalent is not necessarily RDMA immediate data, but the same invariant must hold: the consumer must not trust a descriptor, tail, or completion until the producer has crossed an explicit publish frontier.

Concrete host-DPU cases:

- **DPU writes completion/result data into host memory**: DPU posts DMA writes for payload/status bytes, waits for the DMA task completion callback or completion frontier, then publishes a host-visible epoch/tail with a final small DMA write or sync-event update. The host should only consume slots at or below the published epoch. If the final publish is a DMA write into host memory, keep it separate from the data writes and do not reuse source buffers until the publish task has completed.
- **Host CPU writes request data for DPU to pull**: the host fills the slot and descriptor, executes a release fence or store-release on the published epoch/tail, then optionally notifies the DPU with sync-event or COMCH. The DPU should observe the event only as a hint, then DMA-read or CPU-read the published frontier and validate the slot sequence before using the slot.
- **DPU DMA-reads host payloads after host publication**: the DPU should not DMA-read a descriptor/payload range just because a wakeup arrived. It should first read a stable published frontier and only post DMA reads for complete slots. This protects against reading a half-filled descriptor when the wakeup and memory visibility are not a single ordered primitive.
- **COMCH control messages**: COMCH can carry small setup/control messages and export blobs directly. If a COMCH message is used as a doorbell for separately DMA-written memory, treat it like sync-event: send it only after the producing side has completed the data-write path, and include an epoch/sequence so the receiver can validate the memory state.

Current empirical status from the standalone harness:

- Host CPU write plus release/frontier publication, then DPU async DMA-read, passed small `64 B`, `4 KiB`, mixed `64 B / 4 KiB`, and larger-size stress under `PCI_WR_ORDERING=force_relax`.
- DPU async DMA-write plus a later DMA frontier write, after observing the completed contiguous payload frontier, passed the corresponding host-consume stress under `force_relax`.
- Sync-event worked as a practical publish/wakeup value for the validated host-produce/DPU-pull and DPU-produce/host-consume harness protocols, including early-publish negative controls that failed as expected.
- Split DMA contexts are not a publication ordering primitive. The 64 B `write-publish-split-nowait` validation failed when payload DMA and tail DMA were submitted to separate contexts without waiting for the data context's completed frontier. The split completion-gated variant passed.
- The installed headers still do not make sync-event or COMCH a documented general release/acquire fence for arbitrary unrelated memory. Treat the harness result as evidence for the exact protocol shape, not a license to reorder data writes and notification freely.
- The current PCI-export path could not combine `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` with PCI read/write permissions; `doca_mmap_set_permissions()` rejected that combination. Global/firmware `PCI_WR_ORDERING=force_relax` remains the adversarial validation environment.

The remaining correctness requirements before Homer integration are narrower:

- repeat the validated protocol in the actual Homer memory layouts, especially per-session command/completion rings and byte-ring payload windows
- decide whether sync-event is the authoritative frontier value or only a wakeup pointing to a memory-resident frontier for each ring type
- validate any split-context design with explicit completion-gating before publication; do not rely on same-thread submission order across DMA contexts
- re-check cache/coherency behavior if a future DOCA path requires [`doca_mmap_advise_task_invalidate_cache_alloc_init()`](/opt/mellanox/doca/include/doca_mmap_advise.h:191) or a different host-memory mapping mode
- re-run the stress matrix if per-mmap relaxed ordering becomes available through another DOCA API or runtime version

## Candidate Host-DPU Boundary

### Recommended First Prototype

Keep PostgreSQL/Citus backend APIs and current Homer queue semantics intact, but replace the private frontend channel with a host-DPU bridge. The completed frontend/service split makes this more precise than the older "backend or shim" wording:

1. Keep the public PostgreSQL/Citus frontend API in [`homer_frontend.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:1) stable.
2. Add a private host-side DPU channel implementation beside [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1), likely `homer_frontend_dma.c/.h` as anticipated by the split plan. This file should own host memory registration/export, descriptor setup, host-side ring publication, and any host-side sync-event/COMCH handles.
3. Add a DPU/service-side DMA engine file, for example `homer_service_dpu_dma.c/.h`, rather than putting DPU scheduling and DOCA task-pool code into the frontend file. This side imports descriptors, owns DOCA DMA contexts/PEs/task pools, drains completions, pulls host rings, and publishes DPU-owned completion/result frontiers.
4. Share only fixed-width ABI declarations and simple ring-layout helpers between the host frontend and DPU service. If a common source file is useful, keep it dependency-free: no PostgreSQL types, no service scheduler tables, and no DOCA objects unless that object is intentionally built only for the DPU/service target.
5. Transfer memory descriptors, queue metadata, sync-event exports, and session identifiers over COMCH during setup. Avoid the sample's file-based descriptor handoff.
6. DPU-side Homer keeps service-owned scheduler state, peer transport state, and remote RDMA QPs on the DPU side.
7. Hot payload movement uses preallocated/reused async DMA tasks and explicit PE progress. Control/open/close/setup uses COMCH; hot wakeup/publication uses either sync-event or a memory frontier according to the per-ring policy.

This preserves the current source ownership model: PostgreSQL/Citus backends still produce command requests and payload records; Homer still owns transport scheduling and peer-service semantics; only the local transport between those layers changes.

The separate "bridge" source-file idea is therefore directionally right, but it should not become one monolithic file linked by both the frontend API and the backend service. A cleaner split is:

- host frontend channel replacement: `homer_frontend_dma.c/.h`, with the same internal role that [`homer_frontend_shm.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_shm.c:1) has today
- DPU/service DMA engine: `homer_service_dpu_dma.c/.h` or similar, integrated as a new service-progress source near the current scheduler in [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1)
- shared ABI/header layer: existing `homer_*_abi.h` headers plus a possible `homer_dpu_bridge_abi.h` if descriptor-table state does not fit the current ABI files

### Direction Of Copies

For backend-to-Homer payloads, prefer DPU-initiated DMA reads from host producer rings into DPU-local staging or directly into DPU-visible peer-send buffers. This keeps host backends from paying per-object DOCA task submission and lets the DPU service batch based on its scheduler grants.

For Homer-to-backend completions and result payloads, prefer DPU DMA writes into host completion/result rings, followed by an explicit notification or by host-side polling of a published epoch. If using DMA writes for completion publication, preserve the same rule as the peer RDMA design: payload/status bytes first, then a publish word or sync-event update that the consumer treats as the visibility boundary.

For very small control requests, measure two options instead of assuming DMA wins:

- COMCH message carries the whole request/response for cold control paths
- host memory slot plus sync-event/COMCH wakeup carries only a doorbell

The current local control path is cold enough that debuggability may matter more than squeezing the last cycles out of COMCH during session open.

For pgbench-like command/completion rings, assume `K=1` within one session until proven otherwise: the next command often depends on the previous completion. Coalescing across sessions should therefore be DOCA/initiator-side completion-report coalescing on a shared DMA context, not a single semantic tail for many sessions. For throughput byte streams such as basebackup-like rings, use larger contiguous DMA tasks and less frequent per-session tail/consumed updates when the byte-ring layout allows it.

## Performance Guidance

Use long-lived memory registration. Do not register/export/import an mmap per command or payload object. The DOCA samples demonstrate one-off setup for clarity, but Homer should register the control region, completion rings, and payload ring windows once per service/session/stream lifetime.

Use fixed-size rings and task pools. The current Homer SPSC queue model already gives bounded memory and monotonic sequence counters. Map that to a bounded set of `doca_buf` descriptors and `doca_dma_task_memcpy` objects. Avoid dynamic allocation on payload progress.

Let the DPU pull batches. The current scheduler already has payload egress grants in [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6271) and a pass-wide payload budget in [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6046). A DOCA adapter should consume those grants to choose how many DMA tasks to post before flushing or asking for completions.

Prefer `doca_task_submit()` in production and reserve `doca_task_try_submit()` for debug builds. The DOCA README says validation is useful during development but has performance cost.

Use `doca_task_submit_ex()` flags experimentally, with the current bias from the harness. Default non-FLUSH submission may aggregate tasks; explicit `DOCA_TASK_SUBMIT_FLAG_FLUSH` can mark batch boundaries; `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` plus a flushed non-optimized sentinel can reduce initiator-side completion reporting for streams of small DMA tasks. This is not application batching and does not merge per-session tails. The right policy is likely "flush/report at grant boundary, sentinel interval, or ring-pressure boundary," not "flush/report every record."

Keep hot progress polled first. The current Homer service loop is a busy-polling systems prototype. DOCA PE event mode is valuable for idle/cold paths, but the sample documentation warns of latency/performance tradeoffs. Treat polling versus event mode as a measured scheduler policy, not a compile-time architectural commitment.

Make PE draining explicit. One [`doca_pe_progress()`](/opt/mellanox/doca/include/doca_pe.h:197) call is not a CQ drain; it is one progress attempt that returns whether progress was made. A throughput class should usually drain until the PE returns `0`; a latency-sensitive class may use a bounded budget so that one busy stream cannot monopolize the scheduler.

Start queue depth from the measured knees, not the absolute largest working window. The current standalone sweep suggests `async-window=32` as a conservative mixed small/medium default. DPU-pull reads of `64 B` and `1 KiB` peaked around `16`; `4 KiB` benefited up to `32-64`; records of `16 KiB` and larger reached the PCIe-class plateau around `8-16` or `16-32` depending on direction. Very large windows add bookkeeping pressure and sometimes reduce small-message throughput.

Treat sync-event as a semantic doorbell, not a CPU-saving wait. The small-message harness saw sync-event add latency to `64 B` ping-pong exchanges, and `/usr/bin/time -v` showed waiters still consumed most of a CPU. Its value is an explicit event value/wakeup when that simplifies publication or avoids blind polling; it is not currently evidence for lower CPU consumption.

Avoid depending on per-mmap relaxed ordering until the host-DPU publish protocol is explicit and the API path exists. `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` is tempting for throughput, but the current PCI-export path rejected the useful combinations with PCI read/write permissions. The harness still ran under global `PCI_WR_ORDERING=force_relax`, so the design should remain correct in that adversarial setting.

## DPA-Kernel Scope Boundary

DOCA DPA exists, but it is not the first target for porting Homer. [`doca_dpa_create()`](/opt/mellanox/doca/include/doca_dpa.h:151), [`doca_dpa_start()`](/opt/mellanox/doca/include/doca_dpa.h:181), and [`doca_dpa_set_app()`](/opt/mellanox/doca/include/doca_dpa.h:213) show the DPACC/DPA application model. That is useful for tiny kernels or very hot packet/queue operations later, but the current Homer service has a large C state machine, PostgreSQL/Citus session semantics, environment configuration, logging, and RDMA connection management. The near-term offload should be a DPU Arm userspace service using DOCA DMA/COMCH/sync-event APIs, with DPA considered only after profiling identifies a small hot loop worth moving.

## Pitfalls And Corrections

- **DMA is not a doorbell by itself**: DMA task completion is local to the side that submitted the task. Use sync-event, COMCH, or a carefully ordered host-memory publish word for host-DPU visibility.
- **Do not copy the sample descriptor handoff literally**: the DMA sample writes export descriptors and buffer addresses to files. Homer needs descriptor exchange over a control channel, probably COMCH during service/session setup.
- **Do not move peer RDMA correctness backward**: the current peer path intentionally uses `WRITE_WITH_IMM` doorbells. The host-DPU path can use different APIs, but it must keep an equivalent publication boundary when the consumer must know that bytes are stable.
- **Do not make the backend submit hot DMA tasks**: that would move scheduling and hardware submission cost into PostgreSQL/Citus backends. The better fit is DPU-side Homer pulling from host rings under service-progress grants.
- **Do not assume DPU offload means DPA kernels**: DPU Arm userspace is the better first porting target for this C-heavy service. DPA is a later micro-offload option.
- **Do not treat one PE progress call as a drain**: a service scheduler must loop or budget [`doca_pe_progress()`](/opt/mellanox/doca/include/doca_pe.h:197) explicitly.
- **Do not conflate DOCA report coalescing with application batching**: `OPTIMIZE_REPORTS` reduces/deferred initiator-side completion callbacks; it does not merge rings, destinations, or semantic publication tails.
- **Do not coalesce separate per-session rings into one tail by accident**: multiple sessions can share a DMA ctx/PE for task submission and progress, but their command/completion rings still need separate semantic frontiers unless Homer introduces a new multiplexed queue.
- **Do not rely on split-context same-thread ordering**: payload on ctx A and publication on ctx B is correct only after explicit completion-gating from ctx A. The 64 B split-nowait stress failed.
- **Do not copy the synchronous tail-wait artifact into the design**: the harness sometimes made progress while waiting for a synchronous tail DMA. That hid latency, but the cleaner service design is async tail publication plus explicit PE progress, so the scheduler can do useful work instead of blocking on one publish word.

## Open Questions And Experiments

1. Prototype the host-side channel replacement around the completed frontend split: `homer_frontend_dma.c/.h` should implement the private role currently held by `homer_frontend_shm.c`, not change ordinary Citus call sites.
2. Prototype the DPU/service DMA engine as a separate service-side module and integrate it as an explicit progress source. It should own DMA ctx/PE/task pools and maintain per-session completed/consumed frontiers.
3. Decide host memory registration ownership: backend process, postmaster-owned shared region, standalone host helper, or service-manager setup. The decision affects descriptor lifetime across backend exits and service restarts.
4. Replace descriptor-file harness setup with COMCH exchange for mmap descriptors, sync-event exports, queue metadata, and session identifiers.
5. Re-run the queue-depth and opt+flush sentinel sweeps in the actual Homer-like DPU-pull direction with PE draining fixed to match the intended scheduler policy.
6. Decide per-workload-class ctx/PE layout: command/completion latency class, tuple payload class, basebackup/byte-stream throughput class. Start with one ctx per class and shared PE only if the progress loop can enforce budgets.
7. Measure command latency with many sessions sharing one DMA ctx, `OPTIMIZE_REPORTS` for payload DMA, and bounded non-optimized sentinels. The target question is whether ctx-wide report coalescing helps without violating per-session latency.
8. Re-test sync-event as authoritative frontier versus wakeup-only in the exact Homer bridge layout before using it in the hot path.
9. Investigate a supported per-mmap/per-MKey relaxed-ordering path if needed; the current DOCA 3.2 PCI-export path rejected `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` combined with PCI read/write permissions.

## Cross-Links

- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md): standalone host-DPU validation plan for DMA ordering, visibility, sync-event publication, COMCH wakeups, and relaxed-ordering assumptions before Homer integration.
- [dpu_dma_backend_homer_service_plan.md](dpu_dma_backend_homer_service_plan.md): implementation-facing plan for the DPU-resident Homer service, grouped control-block discovery, host frontend DMA channel, and DPU-side DMA engine.
- [homer_frontend_service_separation_plan.md](homer_frontend_service_separation_plan.md): completed frontend/service split that isolates the current POSIX-SHM frontend channel and makes a future `homer_frontend_dma.c` replacement feasible.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): current peer RDMA publication rule that the host-DPU boundary should not weaken.
- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md): existing service-progress and payload-stream scheduler context.
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md): current scheduler state-machine direction that a DOCA PE source should integrate with.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader service-owned transport/control abstraction.
