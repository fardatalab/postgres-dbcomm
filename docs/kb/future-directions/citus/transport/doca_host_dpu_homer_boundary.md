# DOCA Host-DPU Homer Boundary

## Scope

- **What this doc explains**: how the current Homer host-process boundary maps to a future host-DPU boundary, and which local DOCA APIs look relevant for DMA, progress, batching, and notification.
- **What this doc does NOT cover**: measured DOCA throughput on the current farnet hardware, a finalized wire format, DPA-kernel porting, or a complete implementation plan.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The long-term target is a DPU-offloaded communication stack for PostgreSQL/Citus. The current Homer prototype is not offloaded yet: PostgreSQL/Citus backends communicate with a standalone `citus_tuple_sink_service` process on the same host through POSIX shared memory, and the service then drives peer RDMA/control/payload work. Enabling DPU offload means the first boundary to replace is the local backend-to-Homer process boundary, turning it into a host-to-DPU boundary while preserving the current session, command, completion, and payload-stream semantics.

The main correction from a naive "use DOCA DMA" framing is that DMA is only the data-movement piece. The current boundary also has control-slot ownership, wakeups/progress, completion rings, and payload publication rules. A DPU plan therefore needs at least:

- a data path for host memory rings and DPU-local staging/state
- a control path for session/open/start/close and descriptor exchange
- a notification/progress path for wakeups or bounded polling
- a responder-visible publication rule equivalent to the current peer RDMA doorbell rule where correctness depends on it

## Current Homer Boundary To Preserve

The backend-facing control region is a fixed shared-memory slot array. [`CitusRemoteExecControlRegion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:605) contains the protocol version, slot count, heartbeat counter, and `CitusRemoteExecControlSlot` array. [`MapRemoteExecutionControlRegion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1590) opens `CITUS_REMOTE_EXEC_CONTROL_SHM_NAME`, `mmap()`s it, and initializes protocol metadata if needed.

Command completions are not just a status word. [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485) is a small SPSC completion ring for frontend client SQL sessions, while [`CitusRemoteExecPeerCommandCompletionRing`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:515) is the requester-service-owned peer completion ring. [`MapRemoteExecutionPeerCommandCompletionRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1845) maps the peer completion ring by shared-memory name after command-session open.

Session open is already service-owned. [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2588) maps the local control region, opens command sessions through the local service, maps peer completion rings for SQL command sessions, or opens tuple-sink queues for tuple payload operations. This is the right backend API to keep stable while the service implementation moves from host process to DPU process.

The standalone service loop is scheduler-driven, not a blocking per-request loop. [`TupleSinkServiceControlState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1290) stores the local control-region mapping. [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1370) splits service work into RDMA CM, peer control, local control, command ring, completion ring, payload stream, CQ drain, target completion, and heartbeat sources. [`HomerServicePump()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26197) builds ready sets and execution plans, then runs the selected source executors.

Local control is a shared-memory state machine today. [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:24579) scans ready control slots, advances async continuations, publishes responses, and bumps the heartbeat before cold work such as RDMA peer opens. Its async command open path, [`TupleSinkServiceProgressCommandOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22208), can establish peer connections, register peer-visible memory, and wait for peer responses over multiple service passes.

Payload queues are SPSC rings with explicit head/tail state. [`CitusTupleSinkQueueControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:186) is the backend-visible queue control block. [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:201) is the service-to-service payload-ring control block. [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:373) is the service-to-service opaque payload header. Any host-DPU path should preserve this SPSC shape initially because it maps well to cache-line polling, DMA batches, and simple ownership.

The existing peer RDMA publication rule is stronger than memory polling. [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4235) writes a control-ring message and uses a final `WRITE_WITH_IMM` tail update as the responder-visible publication doorbell. [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6761) is the helper for that final 64-bit publish write. The local host-DPU boundary needs its own publication/wakeup rule; DOCA DMA completion on the submitter alone is not the same as remote peer visibility.

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

Task submission is nonblocking. [`doca_task_submit()`](/opt/mellanox/doca/include/doca_pe.h:330) routes a task to its context asynchronously, and ownership transfers to DOCA until completion. [`doca_task_try_submit()`](/opt/mellanox/doca/include/doca_pe.h:388) validates task inputs but the DOCA sample README says this is for development because validation hurts performance. [`doca_task_submit_ex()`](/opt/mellanox/doca/include/doca_pe.h:348) accepts submit flags.

There are two relevant batching knobs:

- [`DOCA_TASK_SUBMIT_FLAG_FLUSH`](/opt/mellanox/doca/include/doca_pe.h:76): without this flag, the context may aggregate tasks and flush to hardware later; with it, this task and earlier tasks are flushed
- [`DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`](/opt/mellanox/doca/include/doca_pe.h:84): allows deferred completion callbacks and fewer hardware completion notifications for a batch

DOCA also has task-batch APIs. [`doca_task_batch_submit()`](/opt/mellanox/doca/include/doca_pe.h:367) and [`doca_task_batch_try_submit()`](/opt/mellanox/doca/include/doca_pe.h:409) exist, with supported batch sizes from 16 to 128 tasks in [`doca_task_batch_max_tasks_number`](/opt/mellanox/doca/include/doca_pe.h:48). These are experimental in this installed header set.

Task reuse is the API pattern to prefer for Homer. The PE resubmit sample explicitly says task resubmission can improve performance and reduce memory use. [`dma_task_resubmit()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:98) replaces source/destination buffers on a completed DMA task and resubmits it. [`create_dma()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:190) preconfigures four DMA tasks, and [`run()`](/opt/mellanox/doca/samples/doca_common/pe_task_resubmit/pe_task_resubmit_sample.c:314) allocates buffers/tasks once before submitting. This is the right shape for Homer payload streams: preallocate per-stream or per-lane task pools and recycle them.

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

This mirrors the current peer RDMA design. [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4235) writes the mailbox body and uses a final `WRITE_WITH_IMM` tail update as the responder-visible publication doorbell. [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6761) implements that final 64-bit publish write. For host-DPU, the equivalent is not necessarily RDMA immediate data, but the same invariant must hold: the consumer must not trust a descriptor, tail, or completion until the producer has crossed an explicit publish frontier.

Concrete host-DPU cases:

- **DPU writes completion/result data into host memory**: DPU posts DMA writes for payload/status bytes, waits for the DMA task completion callback or completion frontier, then publishes a host-visible epoch/tail with a final small DMA write or sync-event update. The host should only consume slots at or below the published epoch. If the final publish is a DMA write into host memory, keep it separate from the data writes and do not reuse source buffers until the publish task has completed.
- **Host CPU writes request data for DPU to pull**: the host fills the slot and descriptor, executes a release fence or store-release on the published epoch/tail, then optionally notifies the DPU with sync-event or COMCH. The DPU should observe the event only as a hint, then DMA-read or CPU-read the published frontier and validate the slot sequence before using the slot.
- **DPU DMA-reads host payloads after host publication**: the DPU should not DMA-read a descriptor/payload range just because a wakeup arrived. It should first read a stable published frontier and only post DMA reads for complete slots. This protects against reading a half-filled descriptor when the wakeup and memory visibility are not a single ordered primitive.
- **COMCH control messages**: COMCH can carry small setup/control messages and export blobs directly. If a COMCH message is used as a doorbell for separately DMA-written memory, treat it like sync-event: send it only after the producing side has completed the data-write path, and include an epoch/sequence so the receiver can validate the memory state.

The open correctness requirements to validate on BlueField are therefore:

- whether sync-event notify after an observed DMA completion is sufficient for the remote CPU to see all prior DMA destination bytes without an additional cache invalidation or host-side fence
- whether host CPU release-store plus sync-event/COMCH wakeup is sufficient before a DPU DMA read of the published slot
- whether DPU DMA writes into normal host shared memory are coherent for host CPU polling, or whether selected mappings require [`doca_mmap_advise_task_invalidate_cache_alloc_init()`](/opt/mellanox/doca/include/doca_mmap_advise.h:191) on the reading side
- whether relaxed ordering can be enabled safely after the above protocol is in place, or whether it causes stale/torn descriptor observations in stress tests

## Candidate Host-DPU Boundary

### Recommended First Prototype

Keep PostgreSQL/Citus backend APIs and current Homer queue semantics intact, but replace the service side of the local shared-memory mapping with a DPU-resident Homer process:

1. Backend allocates or maps long-lived host memory regions for the control region, completion rings, and payload queues.
2. Backend or a host-side shim registers those host ranges with DOCA `doca_mmap`, using `DOCA_ACCESS_FLAG_PCI_READ_ONLY` for backend-to-DPU producer-only rings and `DOCA_ACCESS_FLAG_PCI_READ_WRITE` where the DPU must publish completions or consumed heads.
3. Host transfers memory descriptors, queue metadata, and session identifiers to the DPU service over COMCH during setup. Avoid the sample's file-based descriptor handoff.
4. The DPU Homer process imports host memory descriptors, creates `doca_buf` descriptors for control slots and payload windows, and drives DMA tasks from its own service-progress loop.
5. DPU-side Homer keeps service-owned scheduler state, peer transport state, and remote RDMA QPs on the DPU side.
6. Hot payload movement uses preallocated/reused DMA tasks and batching. Control/open/close/wakeup uses COMCH or sync-event rather than one COMCH message per payload object.

This preserves the current source ownership model: PostgreSQL/Citus backends still produce command requests and payload records; Homer still owns transport scheduling and peer-service semantics; only the local transport between those layers changes.

### Direction Of Copies

For backend-to-Homer payloads, prefer DPU-initiated DMA reads from host producer rings into DPU-local staging or directly into DPU-visible peer-send buffers. This keeps host backends from paying per-object DOCA task submission and lets the DPU service batch based on its scheduler grants.

For Homer-to-backend completions and result payloads, prefer DPU DMA writes into host completion/result rings, followed by an explicit notification or by host-side polling of a published epoch. If using DMA writes for completion publication, preserve the same rule as the peer RDMA design: payload/status bytes first, then a publish word or sync-event update that the consumer treats as the visibility boundary.

For very small control requests, measure two options instead of assuming DMA wins:

- COMCH message carries the whole request/response for cold control paths
- host memory slot plus sync-event/COMCH wakeup carries only a doorbell

The current local control path is cold enough that debuggability may matter more than squeezing the last cycles out of COMCH during session open.

## Performance Guidance

Use long-lived memory registration. Do not register/export/import an mmap per command or payload object. The DOCA samples demonstrate one-off setup for clarity, but Homer should register the control region, completion rings, and payload ring windows once per service/session/stream lifetime.

Use fixed-size rings and task pools. The current Homer SPSC queue model already gives bounded memory and monotonic sequence counters. Map that to a bounded set of `doca_buf` descriptors and `doca_dma_task_memcpy` objects. Avoid dynamic allocation on payload progress.

Let the DPU pull batches. The current scheduler already has payload egress grants in [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069) and a pass-wide payload budget in [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3844). A DOCA adapter should consume those grants to choose how many DMA tasks to post before flushing or asking for completions.

Prefer `doca_task_submit()` in production and reserve `doca_task_try_submit()` for debug builds. The DOCA README says validation is useful during development but has performance cost.

Use `doca_task_submit_ex()` flags experimentally. Default non-FLUSH submission may aggregate tasks; explicit `DOCA_TASK_SUBMIT_FLAG_FLUSH` can mark batch boundaries; `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` may reduce completion notifications. The right policy is likely "flush/report at grant boundary or ring-pressure boundary," not "flush/report every record."

Keep hot progress polled first. The current Homer service loop is a busy-polling systems prototype. DOCA PE event mode is valuable for idle/cold paths, but the sample documentation warns of latency/performance tradeoffs. Treat polling versus event mode as a measured scheduler policy, not a compile-time architectural commitment.

Avoid relaxed ordering until the host-DPU publish protocol is explicit. `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` is tempting for throughput, but the existing RDMA publication note already documents why memory polling alone is not a strong publication rule. If relaxed ordering is enabled, every consumer-visible record needs a carefully ordered publish/notify step.

## DPA-Kernel Scope Boundary

DOCA DPA exists, but it is not the first target for porting Homer. [`doca_dpa_create()`](/opt/mellanox/doca/include/doca_dpa.h:151), [`doca_dpa_start()`](/opt/mellanox/doca/include/doca_dpa.h:181), and [`doca_dpa_set_app()`](/opt/mellanox/doca/include/doca_dpa.h:213) show the DPACC/DPA application model. That is useful for tiny kernels or very hot packet/queue operations later, but the current Homer service has a large C state machine, PostgreSQL/Citus session semantics, environment configuration, logging, and RDMA connection management. The near-term offload should be a DPU Arm userspace service using DOCA DMA/COMCH/sync-event APIs, with DPA considered only after profiling identifies a small hot loop worth moving.

## Pitfalls And Corrections

- **DMA is not a doorbell by itself**: DMA task completion is local to the side that submitted the task. Use sync-event, COMCH, or a carefully ordered host-memory publish word for host-DPU visibility.
- **Do not copy the sample descriptor handoff literally**: the DMA sample writes export descriptors and buffer addresses to files. Homer needs descriptor exchange over a control channel, probably COMCH during service/session setup.
- **Do not move peer RDMA correctness backward**: the current peer path intentionally uses `WRITE_WITH_IMM` doorbells. The host-DPU path can use different APIs, but it must keep an equivalent publication boundary when the consumer must know that bytes are stable.
- **Do not make the backend submit hot DMA tasks**: that would move scheduling and hardware submission cost into PostgreSQL/Citus backends. The better fit is DPU-side Homer pulling from host rings under service-progress grants.
- **Do not assume DPU offload means DPA kernels**: DPU Arm userspace is the better first porting target for this C-heavy service. DPA is a later micro-offload option.

## Open Questions And Experiments

1. Measure DPU DMA read bandwidth and latency for current Homer object sizes: control-slot sized records, tuple-view payload slots, and basebackup byte-ring chunks.
2. Measure DPU DMA write latency for completion-ring publication and result-ring publication.
3. Compare COMCH wakeups, sync-event wakeups, and pure polling for local control slots and hot payload rings.
4. Measure `doca_task_submit_ex()` policies: no explicit flush, flush per grant, flush per N records, and `OPTIMIZE_REPORTS` on/off.
5. Check whether host virtual addresses exported through `doca_mmap_export_pci()` remain stable enough for long-lived PostgreSQL shared-memory mappings across backend lifetimes, service restarts, and postmaster restarts.
6. Decide the first host-memory layout for DPU access: reuse current POSIX shared-memory objects directly, or add a host-side shim that owns DOCA registration and exposes a stable descriptor table to the DPU.
7. Validate cache coherency and memory ordering rules on the actual BlueField/farnet setup before enabling `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`.

## Cross-Links

- [doca_dma_ordering_visibility_validation_plan.md](doca_dma_ordering_visibility_validation_plan.md): standalone host-DPU validation plan for DMA ordering, visibility, sync-event publication, COMCH wakeups, and relaxed-ordering assumptions before Homer integration.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): current peer RDMA publication rule that the host-DPU boundary should not weaken.
- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md): existing service-progress and payload-stream scheduler context.
- [service_progress_control_data_plane_scheduler.md](service_progress_control_data_plane_scheduler.md): current scheduler state-machine direction that a DOCA PE source should integrate with.
- [rdma_transport_control_plane_abstraction.md](rdma_transport_control_plane_abstraction.md): broader service-owned transport/control abstraction.
