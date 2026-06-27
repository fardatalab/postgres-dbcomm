# DOCA DMA Ordering And Visibility Validation Plan

## Scope

- **What this doc explains**: an implementable standalone validation plan for host-DPU DMA ordering, visibility, sync-event publication, and COMCH wakeup assumptions before integrating DOCA into Homer.
- **What this doc does NOT cover**: direct integration into PostgreSQL/Citus or `citus_tuple_sink_service`, peer RDMA replacement, DPA kernels, or final performance tuning.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The DOCA host-DPU Homer boundary plan in [doca_host_dpu_homer_boundary.md](doca_host_dpu_homer_boundary.md) intentionally treats DMA completion, sync-event notification, and COMCH message delivery as separate mechanisms until measured otherwise. The local headers do not state that sync-event or COMCH acts as a release/acquire fence for arbitrary earlier DMA or CPU writes. Before Homer depends on host-DPU rings, we need a standalone experiment that can answer:

1. When the DPU DMA-writes payload bytes and then publishes a 64-bit epoch, can the host ever observe the epoch before the payload is fully visible?
2. When the host CPU writes a slot and publishes a tail, can the DPU DMA-read a stale, torn, or partially initialized descriptor/payload?
3. Does sync-event notify/wait safely replace a memory-resident publish word, or should it only be treated as a wakeup carrying a separately validated epoch?
4. Does COMCH message arrival safely publish separately DMA-written memory, or should COMCH remain a cold-path/control-plane channel?
5. Which DOCA task-submission and batching policies preserve correctness and give useful latency/throughput for Homer-like ring updates?

The experiment should model Homer's local boundary shape, but it should not depend on Postgres, Citus, or the existing Homer service process. The current Homer structures to keep in mind are [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485), [`CitusRemoteExecPeerCommandCompletionRing`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:515), [`CitusTupleSinkQueueControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:186), and [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:201). Those structures use monotonic head/tail or epoch fields and SPSC ownership, so the validation harness should do the same.

## DOCA DMA Usage Insights For Homer DPU Offload

These are the current design rules from the standalone harness. They are
Homer-facing conclusions, not a replacement for the detailed dated experiment
log below.

- Treat a DOCA DMA context as the ordering, completion, and completion-report
  batching domain. `doca_dma_set_ordered_completions()` applies to one DMA
  context, and `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` / non-optimized report
  sentinels are also reasoned about per context.
- Treat a DOCA PE as the progress/polling domain. Multiple contexts can be
  attached to one PE, so "one workload class per DMA ctx" does not require "one
  PE per workload class" up front. Start with shared PE only when shared progress
  is simpler; split PEs later if measurements show progress interference or
  priority isolation needs. A single `doca_pe_progress()` call is not a CQ drain:
  the local header says it finds the next context with a completed task and
  invokes its callback, returning `1` if progress was made and `0` otherwise
  ([`doca_pe_progress()`](/opt/mellanox/doca/include/doca_pe.h:181)). The
  scheduler should explicitly drain a PE with a loop or bounded budget when it
  wants to process many completions across sessions.
- Put payload DMA and the matching publication tail on the same DMA context
  when the protocol relies on same-context ordering or same-context completion
  reasoning. If payload and publication are on different contexts, explicitly
  gate the publication DMA on observed payload completion from the data context.
  Same-thread submission to two contexts is not a publication-ordering mechanism.
- For the host-produce / DPU-pull Homer shape, the host should publish a
  memory-resident frontier or sync-event value only after filling host memory.
  The DPU should pull with a window of async DMA reads, track completed
  contiguous records, and publish/consume only that completed frontier. The
  current no-sync DPU-pull loop is
  [`run_dpu_host_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1367);
  its current one-progress-call loop at
  [`doca_pe_progress(dma->pe)`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1443)
  is a harness policy, not the target scheduler policy.
- For the DPU-produce / host-consume shape, the DPU should similarly keep many
  payload DMA writes in flight, track the completed contiguous frontier, and
  publish the frontier only after the data-DMA completions needed for that
  frontier have been observed.
- `OPTIMIZE_REPORTS` plus a flushed non-optimized sentinel is useful for
  streaming small DMA tasks because it coalesces initiator-side completion
  reporting. It is not useful for a strict one-64B-record command dependency
  chain where each command has to publish before the next command can proceed.
  This is distinct from application-level batching: it does not merge records,
  rings, or publication tails. It only reduces or defers initiator-side
  completion/callback reporting for DMA tasks already submitted to the same DMA
  context.
- Enable ordered completions when using a sentinel completion as the frontier.
  The harness has seen some variants pass without ordered completions, but the
  clean protocol should ask DOCA for the same-context completion property it
  depends on.
- If the application can make `K > 1` records contiguous in both source and
  destination and those records have the same publication fate, prefer one larger
  DMA task over `K` small DMA tasks followed by one tail update. Multiple DMA
  tasks before one publication still make sense when records live in separate
  per-session rings, the ring wraps, source/destination ranges are not
  contiguous, lifetimes/credits differ, or avoiding a staging copy matters.
- Separate per-session rings imply separate semantic publication tails. Multiple
  pgbench sessions can share one DMA ctx and one PE for submission/progress, and
  can benefit from ctx-wide DOCA completion-report coalescing, but they cannot
  coalesce into one application tail unless Homer adds a new multiplexed queue
  abstraction. The scheduler must still update per-session completed frontiers
  and per-session consumed/tail values.
- Sync-event is best treated as a publish/wakeup object for the protocol value,
  not as a general-purpose proof that arbitrary unrelated writes are visible. In
  the current Homer design, use it for cold/control setup and for hot-path
  wakeup/publication only when the exact data-before-event protocol has been
  validated. Otherwise, poll a memory frontier and use DMA completions for the
  DPU's local completion frontier.
- COMCH should remain control plane for setup, descriptor exchange, and policy
  messages. Do not put Homer hot-path payload movement on COMCH unless a
  separate benchmark proves it is competitive with DMA.
- Current queue-depth evidence suggests `--async-window=32` is a conservative
  first default for mixed small/medium records. Larger records reach their
  plateau with lower depths, often `8-16`, so a size-aware or workload-class
  policy should reduce queue depth for large transfers.
- The current PCI-export harness could not enable
  `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`; firmware/global
  `PCI_WR_ORDERING=force_relax` remains the adversarial validation environment.
  The DOCA protocol should therefore remain correct under relaxed PCI ordering
  and not depend on per-mmap relaxed-ordering configuration until a supported
  path is confirmed.

## Relevant DOCA Starting Points

The existing DOCA samples are good scaffolding, but the validation harness should be its own small program pair so we can keep hot loops, counters, and failure modes under our control.

- Host memory export: [`dma_copy_host()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_host/dma_copy_host_sample.c:102) sets mmap permissions, registers a host memory range, starts the mmap, and exports it with [`doca_mmap_export_pci()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_host/dma_copy_host_sample.c:142).
- DPU memory import and DMA copy: [`dma_copy_dpu()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:162) connects a PE to the DMA context, starts the context, imports the host mmap with [`doca_mmap_create_from_export()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:261), creates `doca_buf`s, allocates a DMA memcpy task, submits it with [`doca_task_submit()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:314), and progresses completions with [`doca_pe_progress()`](/opt/mellanox/doca/samples/doca_dma/dma_copy_dpu/dma_copy_dpu_sample.c:323).
- DMA context setup: [`allocate_dma_resources()`](/opt/mellanox/doca/samples/doca_dma/dma_common.c:462) creates a DMA context, converts it to `doca_ctx`, and configures task callbacks with [`doca_dma_task_memcpy_set_conf()`](/opt/mellanox/doca/samples/doca_dma/dma_common.c:476).
- Command-line option style: [`register_dma_params()`](/opt/mellanox/doca/samples/doca_dma/dma_common.c:207) shows the existing `doca_argp` pattern for PCI address, descriptor path, buffer path, and DPU-only source/destination buffer counts.
- Sync-event setup: [`se_init()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:62) waits for a COMCH-delivered sync-event export blob and imports it. [`se_communicate_sync()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:100) uses synchronous wait/update APIs, while [`se_communicate_async()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:140) uses wait and notify tasks.
- COMCH message shape: the control-path server sends a response with [`server_send_pong()`](/opt/mellanox/doca/samples/doca_comch/comch_ctrl_path_server/comch_ctrl_path_server_sample.c:188), and the client sends/receives messages with [`client_send_ping_pong()`](/opt/mellanox/doca/samples/doca_comch/comch_ctrl_path_client/comch_ctrl_path_client_sample.c:130) and [`message_recv_callback()`](/opt/mellanox/doca/samples/doca_comch/comch_ctrl_path_client/comch_ctrl_path_client_sample.c:108).
- Build dependencies available on the current host include `doca-dma`, `doca-comch`, `doca-common`, and `doca-argp` through `pkg-config`.

## Harness Placement And Binaries

Use a repo-local standalone experiment directory so the work is versioned with the Homer plan but does not touch Postgres/Citus runtime paths:

```text
homer/doca_validation/
  README.md
  Makefile or meson.build
  doca_homer_validation_common.h
  doca_homer_validation_host.c
  doca_homer_validation_dpu.c
```

The first implementation should build two binaries:

- `doca_homer_validation_host`: runs on the x86 host, allocates and exports host memory, runs host-side polling/validation, and optionally writes descriptor files for the DPU process.
- `doca_homer_validation_dpu`: runs on the DPU Arm side, imports host memory, runs DMA read/write/publish modes, and reports counters.

For the first DMA-only phase, prefer descriptor files and a simple launch script instead of COMCH. This follows the sample's descriptor-transfer style but avoids mixing COMCH ordering into the DMA experiment. Once DMA behavior is understood, add COMCH only as an explicit later phase.

The host should allocate the tested memory with page alignment and cache-line alignment. Use `posix_memalign()` or `mmap()` with a size rounded to page boundaries. Register one long-lived memory region, not one region per iteration, because Homer would register control regions and payload rings once per session/service lifetime.

## Shared Test Memory Layout

The exported host memory should contain one cache-line-separated control block plus a ring of fixed-size slots. The layout should intentionally resemble Homer's SPSC rings while adding debug fields that expose tearing and stale reads.

```c
#define HDV_MAGIC 0x48445631444d4155ULL /* "HDV1DMAU" */
#define HDV_CACHELINE 64

typedef struct HdvControlBlock
{
    uint64_t magic;
    uint32_t version;
    uint32_t mode;
    uint32_t slot_count;
    uint32_t slot_bytes;
    uint64_t iterations;
    uint64_t payload_seed;

    /* Producer publishes this only after slot bytes are stable. */
    _Atomic uint64_t published_epoch;

    /* Consumer advances this after validation so slot reuse is explicit. */
    _Atomic uint64_t consumed_epoch;

    _Atomic uint64_t start_epoch;
    _Atomic uint64_t done_epoch;
    _Atomic uint64_t error_epoch;
    uint64_t error_code;
    uint64_t reserved[7];
} HdvControlBlock;

typedef struct HdvSlotHeader
{
    uint64_t seq_begin;
    uint64_t seq_end;
    uint64_t generation;
    uint32_t payload_bytes;
    uint32_t flags;
    uint64_t checksum;
    uint64_t poison_before;
    uint64_t poison_after;
} HdvSlotHeader;
```

Each slot should be:

```text
[HdvSlotHeader][payload bytes][padding to slot_bytes]
```

The producer writes:

1. `seq_begin = epoch`
2. `generation = epoch / slot_count`
3. `payload_bytes`
4. deterministic payload bytes derived from `(seed, epoch, offset)`
5. checksum over header fields and payload
6. `seq_end = epoch`
7. final publish frontier

The consumer validates:

1. `published_epoch >= expected_epoch`
2. slot index is `expected_epoch & (slot_count - 1)`
3. `seq_begin == expected_epoch`
4. `seq_end == expected_epoch`
5. `generation == expected_epoch / slot_count`
6. payload checksum matches
7. poison fields around the payload match

The `seq_begin` plus `seq_end` pair is deliberately redundant. It catches a consumer that sees a partially written descriptor or stale payload after a newer publish value.

## Core Modes

### Mode A: DPU DMA Write, Host Polls DMA Publish Word

Purpose: validate the completion/result direction that would write into host-visible completion rings such as [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485).

Producer: DPU.

Consumer: host CPU.

Protocol:

1. Host allocates and exports the whole `HdvControlBlock + slots` region with `DOCA_ACCESS_FLAG_PCI_READ_WRITE`.
2. Host initializes all slots to poison, sets `published_epoch = 0`, `consumed_epoch = 0`, exports the mmap, and waits.
3. DPU imports the host mmap.
4. DPU prepares a DPU-local source buffer for one slot body and a DPU-local 64-bit source buffer for the publish epoch.
5. For each epoch, DPU fills the DPU-local source buffer, posts a DMA memcpy to the target host slot, waits for or tracks completion according to the mode variant, then posts a DMA memcpy of the 64-bit publish epoch to `control->published_epoch`.
6. Host busy-polls `published_epoch` with an acquire load, validates all newly published slots, then stores `consumed_epoch` with release semantics.

Variants:

- A1: wait for data DMA completion before submitting the publish DMA.
- A2: submit data DMA then publish DMA back-to-back on the same DMA context without waiting; require ordered completions if supported.
- A3: submit a batch of N slot DMA writes, then one publish DMA for the final epoch.
- A4: use [`doca_task_submit_ex()`](/opt/mellanox/doca/include/doca_pe.h:348) with [`DOCA_TASK_SUBMIT_FLAG_FLUSH`](/opt/mellanox/doca/include/doca_pe.h:76) only on the publish task.
- A5: same as A3/A4 with [`DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`](/opt/mellanox/doca/include/doca_pe.h:84).
- A6: repeat A1-A5 with [`doca_dma_set_ordered_completions()`](/opt/mellanox/doca/include/doca_dma.h:340) enabled and disabled where supported.
- A7: repeat the passing subset with `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`; this is an adversarial mode, not a default.

Pass condition:

- zero checksum, sequence, poison, or generation failures over at least `10^9` slot publications or the longest practical overnight run
- no host observation of `published_epoch = E` while slot `E` still contains any field from an older epoch

Useful performance counters:

- total iterations
- slot bytes
- batch size
- DMA tasks submitted
- DMA completions observed
- publish tasks submitted
- PE progress calls
- host poll iterations
- p50/p95/p99/p999 time from DPU publish submission to host validation
- throughput in bytes/sec and slots/sec

### Mode B: Host CPU Write, DPU DMA Read After Host Publish

Purpose: validate the likely backend-to-DPU pull path where PostgreSQL/Citus fills host memory and DPU Homer pulls complete slots.

Producer: host CPU.

Consumer: DPU.

Protocol:

1. Host exports the test region with `DOCA_ACCESS_FLAG_PCI_READ_ONLY` if the DPU only reads, or `DOCA_ACCESS_FLAG_PCI_READ_WRITE` if the DPU also writes `consumed_epoch`.
2. Host fills one slot at a time in the same sequence/checksum format.
3. Host publishes `published_epoch` with a C11 release store.
4. DPU polls or DMA-reads the control block frontier. For the first implementation, keep this simple: DPU reads a small remote control range by DMA into a DPU-local control snapshot buffer.
5. DPU posts DMA reads for complete slots only, validates them locally, and optionally DMA-writes `consumed_epoch` back to the host.

Variants:

- B1: host uses release store for `published_epoch`; DPU reads control then reads slot.
- B2: host uses a full `atomic_thread_fence(memory_order_release)` before storing `published_epoch`.
- B3: DPU reads a batch of slots after one frontier read.
- B4: DPU reads the slot payload before reading the frontier as a negative control. This must fail or at least be flagged as invalid protocol.
- B5: host intentionally publishes before finishing the payload as a negative control. This must be detected quickly.
- B6: repeat passing modes with relaxed ordering enabled in the exported mmap.

Pass condition:

- DPU never validates a slot with mismatched `seq_begin`, `seq_end`, checksum, generation, or poison after observing a published frontier that claims the slot is ready.
- Negative controls produce failures, proving the harness catches torn or early-published records.

Useful performance counters:

- host publish rate
- DPU DMA read tasks per slot and per batch
- DPU PE progress calls
- time from host publish to DPU validation
- effective batch size and bytes/sec

### Mode C: DPU DMA Write, Sync-Event Publish

Purpose: determine whether sync-event can be the publish/wakeup step after DPU DMA writes host memory.

Producer: DPU.

Consumer: host CPU.

Protocol:

1. Use the same memory layout as Mode A.
2. DPU writes data slots with DMA.
3. DPU observes data DMA completion according to the variant.
4. DPU updates a sync-event value to the final epoch.
5. Host waits on the sync-event value, then validates slots through that epoch.

Variants:

- C1: DPU waits for data DMA completion, then sync-event notify.
- C2: DPU submits data DMA and sync-event notify back-to-back without waiting. This is a negative or high-risk mode unless DOCA documents same-PE cross-context ordering later.
- C3: DPU writes `published_epoch` by DMA and also notifies sync-event. Host treats sync-event only as wakeup and validates against memory-resident `published_epoch`.
- C4: host uses synchronous wait APIs such as [`doca_sync_event_wait_eq()`](/opt/mellanox/doca/include/doca_sync_event.h:1569).
- C5: host uses async wait tasks analogous to [`se_communicate_async()`](/opt/mellanox/doca/samples/doca_common/sync_event_remote_pci/sync_event_remote_pci_sample.c:140).

Interpretation:

- If C1 passes but C2 fails, sync-event is usable only after explicit DMA completion.
- If C3 is the only robust mode, sync-event should be a wakeup, not the authoritative publication frontier.
- Even if C1 passes, keep Mode A's memory-resident epoch validation in the first Homer integration unless official DOCA documentation states stronger ordering.

### Mode D: Host CPU Write, Sync-Event Wakeup, DPU DMA Read

Purpose: validate whether host release-publish plus sync-event wakeup is safe for DPU-pull.

Producer: host CPU.

Consumer: DPU.

Protocol:

1. Host fills slots.
2. Host release-stores `published_epoch`.
3. Host updates sync-event to the same epoch.
4. DPU waits on sync-event, then DMA-reads `published_epoch` and slots, and validates.

Variants:

- D1: DPU treats sync-event value as a wakeup only, then reads the memory-resident frontier.
- D2: DPU treats sync-event value as the frontier and directly DMA-reads slots.
- D3: host sync-event update before release-store as a negative control.

Interpretation:

- D1 is the preferred Homer shape because missed wakeups can be recovered by polling the frontier.
- D2 is only acceptable if it survives stress and we are comfortable making sync-event the frontier.

### Mode E: COMCH As Control Message Or Doorbell

Purpose: decide whether COMCH should stay cold-path only or can be a coarse doorbell.

Producer and consumer: both directions.

Protocol:

1. Reuse the memory layout from Modes A/B.
2. For setup, COMCH can replace descriptor files by carrying mmap export blobs and metadata.
3. For doorbell tests, producer writes data/publish state first, then sends a COMCH message containing `{mode, final_epoch, checksum_of_control_block}`.
4. Receiver handles the message callback and validates memory.

Variants:

- E1: COMCH setup only; DMA hot path unchanged.
- E2: COMCH doorbell after DMA completion or host release-publish.
- E3: COMCH message before data completion as a negative control.
- E4: COMCH carries the entire small control request, with no separate DMA payload, to estimate cold control-path cost.

Interpretation:

- COMCH should be accepted immediately for cold setup if E1 works.
- COMCH should not be used per Homer payload object unless E2 latency is surprisingly low and CPU overhead is acceptable.
- E4 is relevant for session open/control slots, not byte-ring payloads.

## Failure Detection

The harness must fail loudly on the first correctness violation and dump enough state to understand the ordering bug:

```text
mode
variant
epoch
slot_index
published_epoch
consumed_epoch
seq_begin
seq_end
generation
expected_generation
payload_bytes
expected_checksum
observed_checksum
first_bad_payload_offset
producer_task_id
publish_task_id
last_data_completion_epoch
last_publish_completion_epoch
relaxed_ordering_enabled
flush_policy
ordered_completions_enabled
```

Keep the last 1024 events in a small ring on both host and DPU. Each event should contain timestamp, event kind, epoch, task id, and frontier values. Do not log every iteration on the hot path; update counters and only dump the event ring on failure or at the end.

The negative controls are mandatory. A test harness that never catches intentionally early publication is not valid evidence for Homer.

## Timing And Measurement

Correctness comes first. After a variant survives stress, collect timing in the same harness:

- use `clock_gettime(CLOCK_MONOTONIC_RAW)` on each side for local intervals
- avoid cross-host absolute timestamp comparisons unless clock synchronization is explicitly validated
- measure per-side intervals:
  - producer slot fill time
  - DMA submit time
  - time to DMA completion callback
  - publish submit time
  - consumer poll/wait time
  - consumer validation time
- report warm steady-state results after a fixed warmup epoch count

Suggested default run sizes:

```text
slot_count: 1024 or 8192
slot_bytes: 256, 4096, 65536, 1048576
batch_size: 1, 4, 16, 64
iterations: at least 10 million for quick runs; overnight for final ordering confidence
```

The slot sizes intentionally cover likely Homer shapes:

- 256 bytes: tiny command/completion metadata, close to [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:443) scale
- 4 KiB to 64 KiB: tuple/result payload fragments
- 1 MiB: basebackup byte-ring chunks and large payload-window behavior

## Implementation Phases

### Phase 0: Build And One-Shot DMA Sanity

Goal: prove the local build and host-DPU descriptor transfer work.

Implementation:

1. Create `homer/doca_validation/`.
2. Add a Makefile or Meson build using `pkg-config` dependencies `doca-dma`, `doca-common`, and `doca-argp`.
3. Copy only the minimal helper patterns from the DOCA DMA sample: device open, mmap create/start/export, mmap import, buffer inventory, DMA task allocation, PE progress.
4. Implement `--role=host|dpu`, `--mode=sanity`, `--pci-addr=...`, `--descriptor-path=...`, `--buffer-info-path=...`.
5. Run one DPU DMA read from host memory and validate a checksum.

Exit criteria:

- host exports a page-aligned region
- DPU imports it and copies bytes successfully
- the result is validated by checksum, not just printed

### Phase 1: Mode A DPU DMA Write With DMA Publish

Goal: answer the highest-priority completion/result publication question.

Implementation:

1. Host exports writable host memory.
2. DPU writes slot payloads and a final memory-resident publish epoch.
3. Host validates epochs while DPU runs.
4. Implement A1, A3, and A4 first. Add A2/A5/A6/A7 after the baseline passes.

Exit criteria:

- A1 and A3 pass stress with relaxed ordering disabled
- negative early-publish mode fails quickly
- we know whether batching many slot writes under one publish frontier is safe

### Phase 2: Mode B Host Publish, DPU DMA Pull

Goal: validate the backend-to-DPU pull model.

Implementation:

1. Host produces slots and release-publishes `published_epoch`.
2. DPU reads the frontier and slots by DMA.
3. DPU validates locally and writes or reports consumed frontier.

Exit criteria:

- release-store publication survives stress
- intentional early host publish is caught
- DPU pull batch sizes can be measured without Homer in the loop

### Phase 3: Sync-Event Wakeup/Publish

Goal: decide whether sync-event is a publish primitive, a wakeup primitive, or unsafe for hot-path assumptions.

Implementation:

1. Reuse Mode A and B data paths.
2. Add sync-event setup using the remote PCI sample pattern: COMCH or initial descriptor handoff for the event, then wait/update.
3. Test C1/C3 and D1 first. Treat C2/D2 as high-risk variants.

Exit criteria:

- clear recommendation: sync-event as wakeup only, sync-event as authoritative frontier, or sync-event only for cold path
- measured wakeup latency and CPU cost

### Phase 4: COMCH Setup And Doorbell

Goal: decide COMCH's role.

Implementation:

1. Replace file descriptor transfer with COMCH setup messages.
2. Add COMCH doorbell variants that carry final epoch.
3. Add small-message mode where COMCH carries the whole request/response.

Exit criteria:

- COMCH setup is known to work for mmap/sync-event descriptors
- COMCH per-doorbell cost is measured
- decision recorded for cold control path vs hot payload path

### Phase 5: Relaxed Ordering And Cache Invalidation

Goal: test the tempting performance knobs only after safe baselines exist.

Implementation:

1. Repeat passing Mode A/B/C/D variants with `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`.
2. If host polling sees stale data, test whether an explicit cache invalidate path using [`doca_mmap_advise_task_invalidate_cache_alloc_init()`](/opt/mellanox/doca/include/doca_mmap_advise.h:191) is applicable to the reader and memory type.

Exit criteria:

- explicit decision on whether relaxed ordering is allowed for Homer host-DPU rings
- explicit decision on whether cache invalidation is required or unnecessary for normal host shared memory

## Runbook Shape

The exact PCI addresses should be discovered per machine, but the initial
farnet access paths verified on June 12, 2026 were:

- from `farnet1`, `ssh dpu` reaches `farnet1-bf3-a`
- from `farnet0`, `ssh dpu` reaches `farnet0-bf3-a`
- `farnet1-bf3-a` fast-link addresses: `10.10.1.202` and `10.10.1.203`
- `farnet0-bf3-a` fast-link addresses: `10.10.1.200` and `10.10.1.201`
- both DPU fast-link interfaces reported `400000Mb/s`, 4 lanes, full duplex,
  and link detected
- host fast-link source-policy routing is a diagnostic isolation tool while both
  host ports remain in `10.10.1.0/24`; without it, replies sourced from the
  second host address can still select the first interface, which exposes the raw
  cross-lane `ib_read_bw` failure as RDMA transport retry exhaustion after
  QP/GID metadata exchange
- for host-to-host RDMA/RoCE verification, `ib_read_bw` with explicit devices is
  the better check: `mlx5_0` maps to the first host link and `mlx5_1` maps to
  the second, with IPv4 RoCE v2 GID index `3` on both hosts
- matching host-port pairs passed short `ib_read_bw` sanity checks, but
  cross-port pairs failed in the raw June 12 check with `transport retry counter
  exceeded`; a separate source-policy routing experiment made the failure move
  earlier to ARP/connectivity and `tcpdump` showed ARP requests from one farnet1
  lane arriving only on the same-index farnet0 lane, while the opposite farnet0
  lane saw no packet
- treat the current farnet host fabric as two working same-index L2 domains
  until switch/VLAN/fabric forwarding is corrected and cross-lane
  `ib_read_bw` passes

Start with the `farnet1` host and its local DPU. The current harness lives in
`homer/doca_validation/` and builds one binary, selected at runtime with
`--role=host` or `--role=dpu`. The key implementation points are:

- [`HdvControlBlock`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:25) and [`HdvSlotHeader`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:51) define the host-exported control block and slot header.
- [`hdv_fill_slot()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:117) writes `seq_begin`, payload, checksum, poison fields, and final `seq_end`.
- [`validate_slot()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:419) validates sequence, generation, payload bytes, checksum, and poison fields.
- [`run_host()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:470) exports host memory, writes descriptor files, validates DPU-written slots for Mode A, and produces host-written slots for Mode B.
- [`hdv_dma_copy()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:797) is the synchronous DOCA DMA task wrapper used by the DPU-side experiments.
- [`run_dpu()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:885) imports host memory and drives DPU DMA writes, DPU DMA reads, final publish DMA writes, and consumed-epoch writes.

```sh
# Build on host.
cd /data/dbcomm/postgres-citus/homer/doca_validation
make

# Copy source and build on the DPU so the binary is native aarch64.
rsync -az --delete /data/dbcomm/postgres-citus/homer/doca_validation/ dpu:/tmp/doca_validation/
ssh dpu "make -C /tmp/doca_validation clean && make -C /tmp/doca_validation"

# Start host in export/validate mode. Omit --pci-addr to use the first
# DMA-capable DOCA device, or use --pci-addr=21:00.0 on the current farnet1.
./doca_homer_validation \
  --role=host \
  --mode=write-publish \
  --descriptor-path=/tmp/hdv.desc \
  --buffer-info-path=/tmp/hdv.buf \
  --slot-count=8192 \
  --slot-bytes=4096 \
  --batch-size=16 \
  --iterations=10000000

# Transfer descriptor files for the DMA-only phases.
rsync -az /tmp/hdv.desc /tmp/hdv.buf dpu:/tmp/

# Start DPU side.
ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode=write-publish \
  --descriptor-path=/tmp/hdv.desc \
  --buffer-info-path=/tmp/hdv.buf \
  --slot-count=8192 \
  --slot-bytes=4096 \
  --batch-size=16 \
  --iterations=10000000 \
  --flush=publish"
```

The host process must remain alive while the DPU imports and uses the exported
mmap. The descriptor files are only a bootstrap channel for the experiment; a
Homer integration should replace them with COMCH setup messages.

## Implementation Progress And Results

### June 12, 2026: DMA Harness Milestone

Implemented a standalone DMA validation harness under `homer/doca_validation/`.
It currently covers:

- **Mode A / `write-publish`**: DPU fills a DPU-local slot, DMA-writes the slot into host memory, waits for the data DMA completion, and DMA-writes a 64-bit `published_epoch`. The host polls `published_epoch`, validates all slots through that epoch, and CPU-stores `consumed_epoch`.
- **Mode A negative / `early-publish`**: DPU DMA-writes `published_epoch` before the slot DMA. The host validator should fail immediately.
- **Mode B / `host-write-dpu-read`**: host CPU fills slots and release-stores `published_epoch`; DPU DMA-reads the frontier and slots, validates locally, and DMA-writes `consumed_epoch` plus final counters.
- **COMCH smoke using stock sample**: the stock COMCH control-path sample built and passed between farnet1 host and its DPU.
- **Sync-event stock sample attempt**: the stock remote-PCI sync-event sample built and established COMCH, but the attempted symmetric invocation left both sides waiting for a sync-event export blob and did not reach wait/notify exchange.

Commands used for the custom DMA runs followed the runbook above. The host was
`farnet1`; the DPU was `ssh dpu` from `farnet1`. The harness auto-selected a
DMA-capable DOCA device unless the stock samples needed explicit PCI arguments.
The COMCH stock sample used host `-p 21:00.0`, DPU `-p 03:00.0`, and DPU
representor `-r 21:00.0`.

Custom DMA result artifacts:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| `write-publish` | `slot_count=64`, `slot_bytes=4096`, `iterations=128`, `batch=1` | PASS | `/tmp/hdv.desc`, `/tmp/hdv.buf` smoke output |
| `early-publish` | `slot_count=64`, `slot_bytes=4096`, `iterations=4`, `delay=50000us` | Expected FAIL: host saw poisoned `seq_begin` for epoch 1 | `/tmp/hdv_neg.desc`, `/tmp/hdv_neg.buf` |
| `write-publish` | `slot_count=1024`, `slot_bytes=4096`, `iterations=10000`, `batch=1` | PASS, DPU `285.37 MiB/s`, 10000 data DMA, 10000 publish DMA | `/tmp/hdv_run_batch1_1781298653` |
| `write-publish` | `slot_count=1024`, `slot_bytes=4096`, `iterations=10000`, `batch=16` | PASS, DPU `318.64 MiB/s`, 10000 data DMA, 625 publish DMA | `/tmp/hdv_run_batch16_1781298667` |
| `write-publish` | same as above, `batch=16`, `--flush=none` | PASS, DPU `319.96 MiB/s` | `/tmp/hdv_run_batch16_flushnone_1781298681` |
| `write-publish` | `slot_count=1024`, `slot_bytes=4096`, `iterations=10000`, `batch=64` | PASS, DPU `325.61 MiB/s`, 157 publish DMA | `/tmp/hdv_run_batch64_1781298696` |
| `write-publish` | `slot_count=256`, `slot_bytes=65536`, `iterations=1000`, `batch=16` | PASS, DPU `369.68 MiB/s` | `/tmp/hdv_run_64k_batch16_1781298710` |
| `write-publish` | `slot_count=64`, `slot_bytes=1048576`, `iterations=128`, `batch=4` | PASS, DPU `367.34 MiB/s` | `/tmp/hdv_run_1m_batch4_1781298727` |
| `host-write-dpu-read` | `slot_count=1024`, `slot_bytes=4096`, `iterations=10000`, `batch=16` | PASS, DPU `200.16 MiB/s`, 10000 slot reads, 10 frontier reads, 10000 consumed writes | `/tmp/hdv_run_hostwrite_batch16_1781298839` |

Interpretation:

- DPU DMA write followed by a separate DMA publish word was coherent for the host CPU in all short runs above. The host never observed a published epoch with a torn or stale slot in the passing modes.
- The `early-publish` negative control failed immediately, proving the validator can catch an invalid publication order. This supports keeping the explicit "data first, publish last" protocol in Homer rather than treating any wakeup as sufficient.
- Publishing one epoch for a batch of slots preserved correctness in these runs. Reducing publish DMA count from 10000 to 625 improved the 4 KiB run from about `285 MiB/s` to about `319 MiB/s`; `batch=64` improved only slightly further, so per-slot task allocation/submission/completion dominates this first harness.
- The current harness is correctness-oriented, not a raw DMA bandwidth benchmark. It allocates/frees DOCA buffers and tasks for every operation, waits synchronously for every data DMA, fills and validates deterministic payload bytes on CPU, and writes `consumed_epoch` frequently. Those choices intentionally favor debuggability. The hundreds-of-MiB/s numbers are therefore evidence of software/task overhead in the first harness, not evidence of the DMA engine's bandwidth limit.
- `--flush=none` did not change correctness or throughput materially in the synchronous `batch=16` run. This does **not** prove flush is unnecessary for an asynchronous queued implementation, because this harness waits for each data task completion before publish.
- Host CPU release-store publication plus DPU DMA read passed for the tested 4 KiB run. However, the current DPU-pull mode writes `consumed_epoch` every slot, which is too conservative for the Homer hot path; batching consumed updates is the next obvious optimization.
- COMCH control-path setup works on this machine with host `21:00.0`, DPU `03:00.0`, representor `21:00.0`. The stock sample exchanged one small client message and one server response successfully.
- The stock sync-event remote-PCI sample was not yet converted into evidence for sync-event wait/notify semantics. The attempted run established COMCH but both sides blocked in `se_init()` waiting for an export blob. After code review, the likely cause is that both sides ran the remote/importer sample; the intended pair is the local/exporter sample plus the remote/importer sample. Treat sync-event ordering/visibility questions as still open.

### June 12, 2026: Clarifications After DOCA Header Review

The current `write-publish` result tells us only an empirical fact about the
tested path: on this farnet1 host/DPU, a DPU-side local payload fill followed by
a synchronous DOCA DMA slot write, followed after task completion by a separate
DMA write of `published_epoch`, was visible to the host CPU in the expected
order. It does not yet prove any of these stronger properties:

- Multiple in-flight DMA writes complete or become visible in submission order.
- A queued publish DMA submitted before observing every data-DMA completion is
ordered after those data writes.
- A sync-event notify operation orders earlier DMA writes unless DOCA explicitly
documents that relation or the experiment validates it.
- Host CPU cachelines that were actively read before DPU DMA are always
invalidated/coherent without additional platform-specific fences or cache
management.

`DOCA_TASK_SUBMIT_FLAG_FLUSH` is a DOCA context submission flush, not a memory
cache flush. The header says the flag tells `doca_ctx` to flush this task and
previous tasks to hardware; otherwise the context may aggregate tasks before
flushing them. `doca_ctx_flush_tasks()` can flush tasks that were submitted
without the flag. Therefore, `publish-flush` in the current harness means "make
the publish task and prior submitted tasks leave the DOCA context promptly." It
does not by itself establish a cross-object memory-ordering guarantee between
DMA and sync-event.

Sync-event facts from the headers and samples:

- A sync-event is a 64-bit value with configured publisher/subscriber locations
  including CPU, DPA, GPU, remote PCI, and remote net.
- The event value can live at a CPU address or in a `doca_buf`; the `doca_buf`
  path requires read/write/atomic permissions, and remote PCI export requires
  remote PCI read/write permissions.
- Synchronous APIs include `doca_sync_event_get()`,
  `doca_sync_event_update_set()`, `doca_sync_event_update_add()`,
  `doca_sync_event_wait_gt()`, and `doca_sync_event_wait_eq()`. The wait APIs
  are documented as busy-wait or yield-wait on the event value.
- Asynchronous task APIs exist for get, notify-set, notify-add, wait-equal, and
  wait-not-equal. These can be run through a DOCA PE like DMA tasks.
- The stock local-PCI sample creates and exports the sync-event over COMCH; the
  stock remote-PCI sample waits for that COMCH export blob and imports it. The
  earlier smoke attempt ran the importer shape on both sides, so it only proved
  COMCH setup, not sync-event signaling.

Host-produce / DPU-pull status:

- The current harness includes an initial `host-write-dpu-read` mode, which is
  closer to the intended DPU-offloaded Homer shape than the DPU-write/host-poll
  mode. In that mode, the host CPU fills slots in host memory, release-stores
  `published_epoch`, and the DPU DMA-reads both the frontier and the slots.
- That mode is only a first correctness probe. It still uses synchronous DMA
  reads, validates every slot byte on the DPU CPU, and DMA-writes
  `consumed_epoch` after every slot. Therefore, its throughput is not a useful
  ceiling for DPU-pull Homer.
- For the eventual Homer input path, the more relevant design is host-owned
  rings plus DPU pull: DB backends write descriptors/payloads into registered
  host memory, publish a monotonic frontier, and the DPU consumes by DMA-reading
  batches. The DPU should write consumed/credit state back sparsely, not per
  record.

Sync-event as RDMA-write-with-immediate equivalent:

- The property we want is stronger than "the remote side wakes up." We want a
  publish operation whose completion/observation implies all prior data writes
  in the associated stream are visible to the consumer, like the practical
  protocol role of RDMA WRITE_WITH_IMM on an ordered QP.
- The DOCA sync-event header documents event update/wait/get operations on a
  64-bit value, but the comments reviewed so far do not explicitly say that a
  sync-event notify/set orders or flushes arbitrary earlier DOCA DMA tasks or
  host CPU stores into unrelated memory.
- Therefore, sync-event must be treated as unproven for authoritative
  publication until a custom test validates the exact sequence we need. It may
  still be useful as a wakeup-only mechanism paired with a DMA-written or
  CPU-stored frontier that carries the actual published tail.

Next performance harness shape:

1. Add an async DMA mode with a configurable in-flight window. Configure
   `doca_dma_task_memcpy_set_conf()` with many tasks, preallocate `doca_buf`s
   and `doca_dma_task_memcpy` objects, and reuse them with
   `doca_dma_task_memcpy_set_src()` / `doca_dma_task_memcpy_set_dst()`.
2. Submit slot DMA tasks without waiting for each completion. Drive completion
   callbacks from `doca_pe_progress()` and keep a producer/consumer ring of
   reusable task records.
3. Use `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` for intermediate data tasks and
   `DOCA_TASK_SUBMIT_FLAG_FLUSH` or `doca_ctx_flush_tasks()` at explicit batch
   boundaries. Measure both task rate and MiB/s.
4. Add two publication variants: `publish-after-completions`, where the DPU
   submits the publish operation only after observing all data-DMA completions
   for a batch; and `queued-publish`, where it submits data tasks followed
   immediately by a publish task with flush. The second variant directly tests
   whether queued task order is enough for Homer-style publication.
5. Add sync-event publication variants: sync-event as the authoritative frontier
   value, and sync-event as a wakeup-only signal after a DMA-written frontier.
   The correctness oracle remains slot validation after wakeup/publication.
6. Add a harsher coherence mode where the host pre-touches and continuously
   reads target slot cachelines before publish, then validates after publish.
   Run with relaxed ordering off and on.
7. Add a raw-bandwidth mode that disables payload generation and full validation
   so the DMA engine and DOCA task overhead can be separated from CPU slot work.

### June 12, 2026: Async DMA And Sync-Event Publication Results

Implemented the next harness milestone:

- `host-write-dpu-read` now supports `--async-window=N`. With `N > 1`, the DPU
  preallocates reusable DMA tasks and reusable `doca_buf` objects, submits a
  window of host-memory DMA reads, progresses completions through a DOCA PE, and
  advances host credits only for the contiguous validated prefix.
- `--payload-mode=header` still moves the full slot by DMA but validates only
  per-slot sequence/checksum metadata. This separates DMA/task throughput from
  CPU payload generation and byte-by-byte validation. `--payload-mode=full`
  remains the stronger correctness mode.
- `sync-event-publish` creates a host-owned remote-PCI sync-event, exports it to
  the DPU, fills host slots, and then calls `doca_sync_event_update_set()` with
  the published tail. The DPU waits with `doca_sync_event_wait_gt()`, reads the
  event value, then DMA-reads and validates slots.
- `sync-event-early-publish` publishes the sync-event before filling the slot.
  With `--ready-delay-ms`, the DPU can be placed in the wait before the host
  publishes, making this a useful negative control.

Important implementation pointers:

- [`HdvPayloadMode`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:23) selects full vs header-only payload validation.
- [`run_dpu_host_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1175) is the memory-frontier async DPU-pull loop.
- [`hdv_async_pool_create()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1094) preallocates reusable DMA tasks and buffers.
- [`hdv_async_submit_host_read()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1150) retargets the reusable `doca_buf`s and resubmits a reusable DMA memcpy task.
- [`async_data_submit_flags()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1066) keeps completion reporting reliable by default and makes `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` opt-in.
- [`run_dpu_sync_event_read()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1412) imports the sync-event and validates post-event synchronous DMA reads.
- [`run_dpu_sync_event_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1600) imports the sync-event, waits for published tails, and drains each published range with async DMA reads.
- [`run_host()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:555) creates/exports the host sync-event and performs sync-event publish or early-publish.

Async DPU-pull results:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| async DPU pull, full validation | `slot=4 KiB`, `iterations=256`, `window=16`, `batch=16` | PASS, DPU `343.46 MiB/s` | `/tmp/hdv_async_smoke_1781301292` |
| async DPU pull, header validation | `slot=64 KiB`, `iterations=16384`, `window=64`, `batch=64` | PASS, DPU `25914.79 MiB/s` | `/tmp/hdv_async_64k_w64_1781301307` |
| async DPU pull, header validation | `slot=64 KiB`, `iterations=262144`, `window=128`, `batch=128`, about `16 GiB` moved | PASS, DPU `33038.51 MiB/s` | `/tmp/hdv_async_64k_w128_16g_1781301322` |
| async DPU pull, full validation | `slot=4 KiB`, `iterations=65536`, `window=64`, `batch=64` | PASS, DPU `332.87 MiB/s`; CPU validation dominates | `/tmp/hdv_async_full_4k_w64_1781301335` |
| async DPU pull, header validation | `slot=4 KiB`, `iterations=1048576`, `window=128`, `batch=128`, about `4 GiB` moved | PASS, DPU `18084.48 MiB/s` | `/tmp/hdv_async_4k_w128_1781301349` |

Interpretation:

- The prior hundreds-of-MiB/s numbers were a synchronous-task artifact. With a
  windowed DPU-pull path, DOCA DMA read bandwidth from host memory reaches the
  expected PCIe-class range on this machine: about `18 GiB/s` for 4 KiB DMA
  reads and about `33 GiB/s` for 64 KiB DMA reads in the tested header-validation
  runs.
- Full payload validation remains intentionally slow because the DPU CPU scans
  every byte after DMA. Use full mode for correctness probes and header mode for
  DMA/task throughput.
- The current async implementation still uses descriptor-file setup and
  synchronous DMA writes for consumed/counter updates. A Homer integration should
  use COMCH for setup and sparse credit updates.

Sync-event results:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| stock local/exporter + remote/importer | sync sample, synchronous API | PASS: host signaled value 1, DPU waited and signaled value 2 | `/tmp/hdv_sync_event_stock_1781301386` |
| stock local/exporter + remote/importer | sync sample, async task API | PASS: PE-based notify/wait path works | `/tmp/hdv_sync_event_stock_async_1781301402` |
| custom sync-event publish | `slot=4 KiB`, `iterations=256`, `batch=16`, full validation | PASS | `/tmp/hdv_sync_event_custom_smoke_1781301556` |
| custom sync-event early publish | `slot=4 KiB`, `iterations=4`, `batch=1`, `ready-delay=2000ms`, `early-delay=50000us` | Expected FAIL: DPU saw poisoned `seq_begin` for epoch 1 | `/tmp/hdv_sync_event_custom_negative2_1781301606` |
| custom sync-event publish | `slot=4 KiB`, `iterations=262144`, `batch=128`, header validation, about `1 GiB` moved | PASS, DPU `1681.77 MiB/s`, `65` waits because host publish coalesced | `/tmp/hdv_sync_event_stress_1g_1781301657` |
| custom sync-event publish | `slot=4 KiB`, `iterations=50000`, `batch=1`, header validation | PASS, `49` waits because a 1024-slot ring still allowed coalescing | `/tmp/hdv_sync_event_stress_batch1_1781301673` |
| custom sync-event publish | `slot=4 KiB`, `iterations=100000`, `slot_count=1`, `batch=1`, header validation | PASS, `100000` waits for `100000` publishes | `/tmp/hdv_sync_event_stress_ring1_100k_1781301707` |

Sync-event interpretation:

- Remote-PCI sync-event works on this host/DPU pair in both stock synchronous
  and stock async-task forms.
- In the custom harness, host CPU writes followed by
  `doca_sync_event_update_set(tail)` were empirically sufficient for the DPU to
  wake, DMA-read host memory, and validate the corresponding slots in the tested
  runs, including a 100k one-slot run where every publish caused a wait.
- The early-publish negative control failed when the DPU was already waiting,
  which shows the test can distinguish "event was delivered" from "data was
  actually written before publish."
- This is strong empirical evidence that sync-event can serve the practical
  publish role for the tested host-CPU-write/DPU-DMA-read sequence. It is still
  not a formal API guarantee that sync-event orders arbitrary unrelated DOCA DMA
  tasks or CPU stores; the header comments reviewed so far document value
  update/wait semantics, not a general memory-ordering contract.
- The conservative Homer design remains: use sync-event as the wakeup/publish
  mechanism only with a protocol that writes data first, executes an appropriate
  CPU or DOCA submission fence for the producer type, and treats the event value
  or a separately validated tail as the authoritative frontier. For DPU-issued
  DMA writes followed by sync-event notify, run a separate test before assuming
  the same ordering.

### June 12, 2026: Sync-Event Async Pull Throughput Tuning

Added `sync-event-async-pull`, which combines the sync-event publish gate with
the async DPU-pull DMA engine. This is the closest standalone harness shape to
the current target Homer input path:

1. Host CPU fills fixed-size slots in registered host memory.
2. Host executes a release fence and calls `doca_sync_event_update_set(tail)`.
3. DPU waits on the remote-PCI sync-event with `doca_sync_event_wait_gt()`.
4. DPU treats the event value as the published tail for this experiment and
   drains the published range with a reusable window of async DMA reads.
5. DPU validates slot headers, advances contiguous completion, and DMA-writes
   sparse `consumed_epoch` updates back to host memory.

Throughput artifacts:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| sync-event async pull, full validation smoke | `slot=4 KiB`, `iterations=512`, `batch=16`, `window=16` | PASS, DPU `234.65 MiB/s`, host `5.92 MiB/s`, `11` waits | `/tmp/hdv_se_async_smoke_1781302519` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=262144`, `batch=128`, `window=128`, about `16 GiB` moved | PASS, DPU `32654.37 MiB/s`, host `20022.41 MiB/s`, `137` waits | `/tmp/hdv_se_async_64k_w128_16g_1781302535` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=512`, `window=256`, about `32 GiB` moved | PASS, DPU `34848.73 MiB/s`, host `16515.08 MiB/s`, `139` waits | `/tmp/hdv_se_async_64k_w256_b512_32g_noreports_1781302694` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=2048`, `window=256`, about `32 GiB` moved | PASS, DPU `33156.98 MiB/s`, host `25058.79 MiB/s`, `85` waits | `/tmp/hdv_se_async_64k_w256_b2048_32g_1781302714` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=4096`, `window=256`, about `32 GiB` moved | PASS, DPU `33578.80 MiB/s`, host `25546.14 MiB/s`, `127` waits | `/tmp/hdv_se_async_64k_w256_b4096_32g_1781302731` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=8192`, `window=256`, about `32 GiB` moved | PASS, DPU `33096.51 MiB/s`, host `25064.41 MiB/s`, `63` waits | `/tmp/hdv_se_async_64k_w256_b8192_32g_1781302748` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=4096`, `window=512`, about `32 GiB` moved | PASS, DPU `33528.40 MiB/s`, host `16002.86 MiB/s`, `127` waits | `/tmp/hdv_se_async_64k_w512_b4096_32g_1781302765` |
| sync-event async pull, header validation | `slot=64 KiB`, `iterations=524288`, `batch=4096`, `window=128`, about `32 GiB` moved | PASS, DPU `33573.33 MiB/s`, host `24997.29 MiB/s` | `/tmp/hdv_se_async_64k_w128_b4096_32g_1781302785` |
| sync-event async pull, header validation | `slot=4 KiB`, `iterations=1048576`, `batch=4096`, `window=256`, about `4 GiB` moved | PASS, DPU `7414.67 MiB/s`, host `4518.96 MiB/s` | `/tmp/hdv_se_async_4k_w256_b4096_4g_1781302803` |
| sync-event async pull, header validation | `slot=4 KiB`, `iterations=1048576`, `batch=8192`, `window=256`, about `4 GiB` moved | PASS, DPU `7947.33 MiB/s`, host `4781.50 MiB/s` | `/tmp/hdv_se_async_4k_w256_b8192_4g_1781302921` |
| sync-event async pull, header validation | `slot=4 KiB`, `iterations=1048576`, `batch=16384`, `window=256`, about `4 GiB` moved | PASS, DPU `7936.10 MiB/s`, host `4674.55 MiB/s` | `/tmp/hdv_se_async_4k_w256_b16384_4g_1781302937` |
| sync-event async pull, header validation | `slot=1 MiB`, `iterations=32768`, `batch=512`, `window=128`, about `32 GiB` moved | PASS, DPU `35511.40 MiB/s`, host `26482.34 MiB/s` | `/tmp/hdv_se_async_1m_w128_b512_32g_1781302954` |
| sync-event async pull, header validation repeat | `slot=1 MiB`, `iterations=32768`, `batch=512`, `window=128`, about `32 GiB` moved | PASS, DPU `35509.03 MiB/s`, host `26327.82 MiB/s` | `/tmp/hdv_se_async_1m_w128_b512_32g_repeat_1781302998` |
| sync-event async pull, header validation | `slot=2 MiB`, `iterations=16384`, `batch=256`, `window=128`, about `32 GiB` moved | PASS, DPU `35333.18 MiB/s`, host `16745.23 MiB/s` | `/tmp/hdv_se_async_2m_w128_b256_32g_1781302971` |

Rejected/unsafe report-optimization variants:

- `/tmp/hdv_se_async_64k_w256_b512_32g_1781302552` stalled with
  `completed=4076 submitted=4096 published=4096 consumed=4076` when data tasks
  used `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` by default.
- `/tmp/hdv_se_async_4k_w256_b4096_4g_optreports_1781302857` stalled with
  `completed=8190 submitted=8192 published=8192 consumed=8190` even after the
  harness forced non-optimized reports on some boundary tasks.
- The current harness therefore keeps `OPTIMIZE_REPORTS` disabled by default.
  Do not enable it for Homer-like correctness or throughput claims until there
  is a proven sentinel/drain pattern that guarantees all reusable task records
  receive observable completion.

June 26 follow-up on the report-sentinel hypothesis:

- Added `--flush-report-sentinel`, implemented in
  [`async_data_submit_flags()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1172).
  This is narrower than `--flush=all`: optimized intermediate payload tasks
  remain aggregateable, while the non-optimized async report sentinel is
  submitted with `DOCA_TASK_SUBMIT_FLAG_FLUSH`.
- Mechanism interpretation: `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS` gives us a
  form of completion-report batching/coalescing. Intermediate payload tasks may
  defer completion callbacks. A later non-optimized report sentinel is the
  callback boundary, and `DOCA_TASK_SUBMIT_FLAG_FLUSH` on that sentinel makes the
  boundary task and previous aggregated tasks reach hardware instead of relying
  on context-side aggregation. This is not documented as "batching DMA
  execution"; it is batching/coalescing the initiator-side completion reporting
  and hardware submission doorbell behavior.
- Reproduced the old DPU-pull stall with
  `/tmp/hdv_optreports_baseline_opt_1782521551`: `sync-event-async-pull`,
  `slot=4 KiB`, `slot-count=8192`, `batch=4096`, `window=256`,
  `iterations=1048576`, `--optimize-reports`. The DPU timed out at
  `completed=4094 submitted=4096 published=4096 consumed=4094`; the host timed
  out producing epoch `8193` because consumed credit never advanced past the
  first batch.
- The same run passed with `--optimize-reports --flush-report-sentinel`:
  `/tmp/hdv_optreports_flush_sentinel_no_order_1782521620`.
- The same run also passed with ordered completions enabled:
  `/tmp/hdv_optreports_flush_sentinel_ordered_1782521593`.
- The no-optimized-report control also passed:
  `/tmp/hdv_optreports_no_opt_control_1782521631`.
- Interpretation: a flushed non-optimized report sentinel fixes the reproduced
  DPU-pull deferred-callback liveness failure. This is a completion-reporting
  result, not a memory-publication proof. It says the DPU initiator can make its
  reusable task bookkeeping live; it does not by itself say that a remote host
  observing a separate frontier has RDMA `WRITE_WITH_IMM`-style visibility
  semantics.
- Throughput conclusion: do not treat the sentinel-flush run as a bandwidth win.
  The DPU-side MiB/s was low in these reruns even for the no-opt control, so this
  artifact set is liveness evidence only.
- Performance follow-up on the high-throughput 1 MiB DPU-pull shape:
  `sync-event-async-pull`, `slot=1 MiB`, `slot-count=1024`, `batch=512`,
  `window=128`, `iterations=8192`, header validation. With a shorter 1 second
  host ready delay, DPU-side bandwidth was essentially unchanged:
  no optimized reports was `35075.59 MiB/s`
  (`/tmp/hdv_optreports_perf_rdy1s_no_opt_1782523379`),
  `--optimize-reports --flush-report-sentinel` was `34914.14 MiB/s`
  (`/tmp/hdv_optreports_perf_rdy1s_opt_flush_1782523382`), and adding
  `--ordered-completions` was `34616.58 MiB/s`
  (`/tmp/hdv_optreports_perf_rdy1s_opt_flush_ordered_1782523384`). The PE
  progress-call counts were also in the same rough band, about `1.39M-1.45M`.
  Current conclusion: the sentinel-flush pattern fixes the liveness failure but
  has not shown a meaningful throughput benefit in this harness.
- Ordered-completion nuance: `doca_dma_set_ordered_completions()` is useful for
  initiator-side reasoning because it requests DMA completions in submission
  order for a context. In DPU-pull, however, the host's sync-event publication
  happens before the DPU issues the DMA reads; observing that host publication
  only authorizes the DPU to pull. It does not mean the DPU's later payload DMA
  reads have completed. If the DPU submits a sentinel DMA task after earlier
  payload DMA tasks, then ordered completions make observing the sentinel
  completion a sufficient initiator-side indication that earlier submitted DMA
  completions have also been returned. Without ordered completions, our
  sentinel-flush tests still passed empirically, and DOCA's `OPTIMIZE_REPORTS`
  comment says a non-optimized task completion also delivers preceding deferred
  callbacks; but the cleaner protocol should still enable ordered completions
  when using a sentinel as a completion frontier.
- DPU-push contrast: `write-publish-async` did not reproduce the same stall in a
  matching 1 GiB check. Optimized reports, sentinel flush, sentinel flush plus
  ordered completions, and no optimized reports all completed
  (`/tmp/hdv_optreports_push_baseline_opt_1782521662`,
  `/tmp/hdv_optreports_push_flush_sentinel_1782521672`,
  `/tmp/hdv_optreports_push_flush_sentinel_ordered_1782521682`,
  `/tmp/hdv_optreports_push_no_opt_1782521710`), but all showed hundreds of
  millions of PE progress calls and are diagnostic-only until the DPU-push
  progress loop is retuned.
- No-sync-event DPU-pull clarification: if the host publishes only a
  memory-resident frontier and the DPU polls that frontier with DMA reads, then
  sync-event is not involved in the hot path. The relevant publication for the
  DPU's next stage is the DPU's own completed-read frontier. In that shape, a
  flushed non-optimized sentinel plus ordered completions is a reasonable
  initiator-side completion frontier: the DPU submits payload DMA reads, then a
  later sentinel, and observing the sentinel completion means earlier submitted
  reads have completed when ordered completions are enabled.
- Added `--size-pattern=mixed-64-1k` to model small control records mixed with
  small payload records. It alternates 64 B and 1 KiB records in a 1 KiB ring
  stride.
- Small-message no-sync-event DPU-pull performance test:
  `host-write-dpu-read`, `slot-count=4096`, `batch=1024`, `window=128`,
  `iterations=1000000`, header validation. The DPU polls the memory frontier and
  drains host slots with async DMA reads. Two passes showed a clear DPU-side
  elapsed-time benefit from `--optimize-reports --flush-report-sentinel`:

  | Record shape | Variant | DPU MiB/s pass 1 | Artifact pass 1 | DPU MiB/s pass 2 | Artifact pass 2 |
  | --- | --- | ---: | --- | ---: | --- |
  | fixed 64 B | no optimized reports | `195.42` | `/tmp/hdv_nosync_opt_64b_no_opt_1782524985` | `197.64` | `/tmp/hdv_nosync_opt_rep2_64b_no_opt_1782525018` |
  | fixed 64 B | optimized + flushed sentinel | `383.16` | `/tmp/hdv_nosync_opt_64b_opt_flush_1782524987` | `380.61` | `/tmp/hdv_nosync_opt_rep2_64b_opt_flush_1782525020` |
  | fixed 64 B | optimized + flushed sentinel + ordered | `348.49` | `/tmp/hdv_nosync_opt_64b_opt_flush_ordered_1782524989` | `334.93` | `/tmp/hdv_nosync_opt_rep2_64b_opt_flush_ordered_1782525022` |
  | fixed 1 KiB | no optimized reports | `3110.48` | `/tmp/hdv_nosync_opt_1k_no_opt_1782524990` | `3172.74` | `/tmp/hdv_nosync_opt_rep2_1k_no_opt_1782525023` |
  | fixed 1 KiB | optimized + flushed sentinel | `5275.68` | `/tmp/hdv_nosync_opt_1k_opt_flush_1782524992` | `5917.03` | `/tmp/hdv_nosync_opt_rep2_1k_opt_flush_1782525025` |
  | fixed 1 KiB | optimized + flushed sentinel + ordered | `5989.20` | `/tmp/hdv_nosync_opt_1k_opt_flush_ordered_1782524993` | `5363.48` | `/tmp/hdv_nosync_opt_rep2_1k_opt_flush_ordered_1782525026` |
  | mixed 64 B / 1 KiB | no optimized reports | `1668.20` | `/tmp/hdv_nosync_opt_mixed64_1k_no_opt_1782524995` | `1686.22` | `/tmp/hdv_nosync_opt_rep2_mixed64_1k_no_opt_1782525028` |
  | mixed 64 B / 1 KiB | optimized + flushed sentinel | `2928.05` | `/tmp/hdv_nosync_opt_mixed64_1k_opt_flush_1782524996` | `3000.86` | `/tmp/hdv_nosync_opt_rep2_mixed64_1k_opt_flush_1782525030` |
  | mixed 64 B / 1 KiB | optimized + flushed sentinel + ordered | `3100.34` | `/tmp/hdv_nosync_opt_mixed64_1k_opt_flush_ordered_1782524998` | `2960.70` | `/tmp/hdv_nosync_opt_rep2_mixed64_1k_opt_flush_ordered_1782525031` |

- Interpretation: smaller records are where completion-report coalescing starts
  to matter. In this no-sync DPU-pull shape, the optimized + flushed sentinel
  variant roughly doubled fixed-64B DPU-side MiB/s and improved 1 KiB and mixed
  64B/1KiB by about `1.7-1.9x` in these two passes. Host-side elapsed had more
  variance because the host can fill the ring and then wait for consumed credit;
  use the DPU-side elapsed for this completion-overhead question.
- The improvement did not come from fewer outer `doca_pe_progress()` calls. The
  optimized variants reported more PE progress calls in this harness. The more
  plausible explanation is reduced DOCA/HW completion-report pressure or
  callback-delivery overhead per useful byte, not a simpler polling loop.
- Homer command-chain caveat: for a single session where command `N+1` depends
  on completion `N`, the coalescing depth may be only one 64 B command record.
  We tested this as a strict `K=1` proxy with `host-write-dpu-read`,
  `slot=64 B`, `batch=1`, `iterations=100000`.
  With `slot-count=1` and `async-window=1`, the harness uses the synchronous
  DPU-read path and publishes every consumed command individually; all variants
  completed, but `OPTIMIZE_REPORTS` cannot apply because each read is already
  the report boundary:
  `/tmp/hdv_cmdchain_k1_64b_no_opt_1782526600`,
  `/tmp/hdv_cmdchain_k1_64b_opt_flush_1782526602`,
  `/tmp/hdv_cmdchain_k1_64b_opt_flush_ordered_1782526604`.
  With `slot-count=2` and `async-window=2`, the async path runs and the host can
  be at most one command ahead. DPU-side MiB/s was effectively flat:
  no optimized reports `20.46`
  (`/tmp/hdv_cmdchain_k1_64b_async_no_opt_1782526624`),
  optimized + flushed sentinel `20.19`
  (`/tmp/hdv_cmdchain_k1_64b_async_opt_flush_1782526626`), and optimized +
  flushed sentinel + ordered `20.61`
  (`/tmp/hdv_cmdchain_k1_64b_async_opt_flush_ordered_1782526627`).
  Interpretation: for a one-DMA command/completion dependency chain, completion
  coalescing has no meaningful DPU-side benefit in the current harness. The
  earlier `1.7-2x` small-record win applies to streaming/pipelined small DMAs or
  commands with multiple DMA tasks before one command completion, not to a
  single 64 B DMA that must publish before the next command can proceed.

Interpretation:

- Best stable sync-event-gated DPU-pull result so far is `slot=1 MiB`,
  `batch=512`, `window=128`: about `35.5 GiB/s` DPU-side DMA-read throughput
  and about `26.3-26.5 GiB/s` host-observed producer/consumer throughput over a
  32 GiB transfer.
- Best 64 KiB result for host-observed throughput is `batch=4096` with
  `window=256`: about `33.6 GiB/s` DPU-side and about `25.5 GiB/s` host-side.
- Best 4 KiB result is `batch=8192` with `window=256`: about `7.95 GiB/s`
  DPU-side and about `4.78 GiB/s` host-side. Small slots expose per-task and
  per-slot overhead much more strongly.
- `window=128` or `window=256` is enough for the tested machine. `window=512`
  did not improve DPU-side throughput and hurt host-observed throughput in the
  64 KiB run.
- Large publish/credit batches reduce sync-event and consumed-update overhead.
  The best batch depends on slot size: 4 KiB preferred the largest tested batch,
  64 KiB peaked around 4096 slots, and 1 MiB performed best with 512 slots.
- These runs empirically support the practical Homer direction: host backends
  can write registered host memory, publish a tail through sync-event, and let
  the DPU pull large published ranges asynchronously. The test still does not
  turn sync-event into a documented general memory fence for unrelated writes;
  it validates this exact protocol on this host/DPU stack.

### June 26, 2026: DPU-Push Sync-Event Publication And Mixed-Size Rings

Current machine state:

- `farnet1` branch during this validation: `homer-dpu-migration`.
- Both BF3 PFs reported `PCI_WR_ORDERING force_relax(1)`, so these runs were
  collected under the more adversarial PCI write-ordering setting.

Implemented harness changes:

- Added `dpu-sync-event-publish`, where the host creates a sync-event with
  remote-PCI publisher and CPU subscriber locations, the DPU DMA-writes host
  slots, and then the DPU calls `doca_sync_event_update_set(batch_end)`.
- Added `dpu-sync-event-early-publish`, where the DPU updates the sync-event
  before writing the matching slot. This is the negative control for DPU-push
  sync-event publication.
- Added `--size-pattern=fixed` and `--size-pattern=mixed-64-4k`. The mixed
  mode uses a 4 KiB ring stride but alternates 64 byte records and 4 KiB records,
  so one run exercises small control-style DMA records interleaved with larger
  payload-style DMA records.
- Throughput reporting now uses actual record bytes moved, not only ring stride
  bytes, so mixed runs are not overstated.

Important implementation pointers:

- [`HdvSizePattern`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:32) defines fixed and mixed-size record modes.
- [`hdv_record_bytes()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:95) computes each epoch's active DMA length.
- [`run_host()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:613) configures sync-event direction. For DPU-push modes, it uses remote-PCI publisher and CPU subscriber locations.
- [`run_dpu_write_sync_event()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1911) is the DPU-push sync-event publisher path.

DPU-push sync-event publication results under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| DPU push, fixed small records | `slot=64 B`, `iterations=100000`, `batch=128`, full validation | PASS, DPU `40.64 MiB/s`, host validation `2.66 MiB/s` | `/tmp/hdv_dpu_push_64b_100k_1782509826` |
| DPU push, fixed 4 KiB records | `slot=4 KiB`, `iterations=100000`, `batch=128`, full validation | PASS, DPU `332.99 MiB/s`, host validation `166.88 MiB/s` | `/tmp/hdv_dpu_push_4k_100k_1782509828` |
| DPU push, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=100000`, `batch=128`, full validation | PASS, DPU `298.97 MiB/s`, host validation `108.04 MiB/s` | `/tmp/hdv_dpu_push_mixed_100k_1782509831` |
| DPU push, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `42.21 MiB/s`, host validation `16.95 MiB/s` | `/tmp/hdv_dpu_push_64b_1m_full_1782509890` |
| DPU push, fixed 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `328.90 MiB/s`, host validation `299.37 MiB/s` | `/tmp/hdv_dpu_push_4k_1m_full_1782509893` |
| DPU push, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `296.00 MiB/s`, host validation `251.51 MiB/s` | `/tmp/hdv_dpu_push_mixed_1m_full_1782509907` |
| DPU push, early sync-event negative | `slot=4 KiB`, `iterations=4`, `batch=1`, `early-delay=50000us`, full validation | Expected FAIL: host saw poisoned `seq_begin` for epoch 1 after event value 1 | `/tmp/hdv_dpu_push_early_negative_1782510004` |

Host-produce / DPU-pull mixed-size results under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| sync-event async pull, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `155.98 MiB/s`, host `33.06 MiB/s` | `/tmp/hdv_host_pull_64b_1m_b1024_1782509852` |
| sync-event async pull, fixed 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `223.56 MiB/s`, host `177.70 MiB/s` | `/tmp/hdv_host_pull_4k_262k_b1024_1782509854` |
| sync-event async pull, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `220.63 MiB/s`, host `145.71 MiB/s` | `/tmp/hdv_host_pull_mixed_262k_b1024_1782509860` |

Interpretation:

- This is the missing empirical evidence for the DPU DMA-write / host-consume
  direction: under `PCI_WR_ORDERING=force_relax`, DPU DMA writes followed by
  DPU sync-event publication passed full host-side validation for 64 B, 4 KiB,
  and mixed 64 B / 4 KiB records.
- The negative mode failed immediately, proving the host validator is actually
  sensitive to sync-event publication arriving before the matching DMA write.
- These runs use synchronous per-record DMA writes in the DPU-push path, so the
  DPU-side MiB/s numbers are correctness evidence, not the final throughput
  ceiling. The next performance question is async/windowed DPU writes plus
  sync-event publication after observed data-DMA completions.
- The evidence is still empirical rather than an official DOCA memory-ordering
  contract. For Homer, the practical rule should be: data first, wait for or
  otherwise prove data-DMA completion, then publish the frontier through
  sync-event. Avoid queued data-DMA plus sync-event notify without explicit
  validation.

Immediate follow-up implementation work:

1. Add an intentionally early async DPU-write negative mode that DMA-publishes
   a frontier before the matching queued data-DMA completions. This would prove
   the async DPU-write validator catches the same class of bug as the existing
   synchronous early-publish modes.
2. Replace descriptor files with COMCH setup in the harness and measure cold
   setup latency.
3. Add a multi-stream version of async DPU pull to model several DB backends or
   Homer lanes sharing the DPU DMA engine.
4. Add CPU cache pre-touch stress variants and investigate whether another DOCA API or newer runtime exposes per-mmap PCI relaxed ordering for this host-DPU DMA path.

### June 26, 2026: No-Sync-Event Publication Contrast Runs

After validating sync-event publication in both directions, we also ran the
same small, large, and mixed-size shapes without sync-event publication. These
runs answer a narrower empirical question: whether the existing memory-resident
frontier schemes happen to work on this machine under the current
`PCI_WR_ORDERING=force_relax` setting.

Modes under test:

- `write-publish`: DPU DMA-writes host slots, then DPU DMA-writes
  `HdvControlBlock.published_epoch`. Host polls the host-resident frontier and
  validates host-resident slots.
- `write-publish-async`: DPU queues many DMA writes into host slots, observes
  their data-DMA completions, and only then DMA-writes
  `HdvControlBlock.published_epoch` for the completed contiguous frontier.
- `host-write-dpu-read`: host CPU-writes slots and release-stores
  `HdvControlBlock.published_epoch`; the DPU polls the frontier through DMA
  reads and drains slots with async DMA reads.

The two modes are intentionally not symmetric. `write-publish` uses DMA for the
publication word; `host-write-dpu-read` uses a host CPU store for publication
and DMA only for the DPU consumer. That matches two realistic Homer contrasts:
DPU-issued completion/result publication versus host backend-owned producer
rings that the DPU pulls.

No-sync-event DPU-write / host-consume results under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| DPU DMA write + DMA frontier, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `42.58 MiB/s`, host validation `42.58 MiB/s` | `/tmp/hdv_nosync_dpu_write_64b_1m_1782511160` |
| DPU DMA write + DMA frontier, fixed 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `331.79 MiB/s`, host validation `331.69 MiB/s` | `/tmp/hdv_nosync_dpu_write_4k_1m_1782511163` |
| DPU DMA write + DMA frontier, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, full validation | PASS, DPU `301.44 MiB/s`, host validation `301.45 MiB/s` | `/tmp/hdv_nosync_dpu_write_mixed_1m_1782511176` |

No-sync-event host-write / DPU-pull results under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| Host CPU write + CPU frontier, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `174.41 MiB/s`, host `38.12 MiB/s` | `/tmp/hdv_nosync_host_pull_64b_1m_1782511184` |
| Host CPU write + CPU frontier, fixed 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `224.00 MiB/s`, host `176.52 MiB/s` | `/tmp/hdv_nosync_host_pull_4k_262k_1782511185` |
| Host CPU write + CPU frontier, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `220.79 MiB/s`, host `145.51 MiB/s` | `/tmp/hdv_nosync_host_pull_mixed_262k_1782511191` |

Interpretation:

- Both no-sync-event directions passed full validation for small, large, and
  mixed records. This means we did not observe torn or stale slot data after the
  consumer observed the memory-resident frontier in these runs.
- This does **not** promote the frontier word to a documented RDMA
  `WRITE_WITH_IMM`-style publication guarantee. It only says that the exact
  harness protocol worked empirically on this BF3 setup.
- For Homer, sync-event remains the stronger candidate for an explicit
  notification/publish operation because it provides a waitable event value.
  The no-sync-event memory frontier may still be useful as a low-overhead fast
  path or fallback if repeated stress keeps passing and we are comfortable with
  an empirical rather than documented ordering basis.
- The next useful harshness tests are larger multi-hour mixed runs, an
  intentionally early async DPU-write negative control, and a separate search
  for a DOCA API/runtime that actually accepts per-mmap PCI relaxed ordering.

### June 26, 2026: Async No-Sync-Event Publication Stress

We added and validated `write-publish-async` to make the DPU-write no-sync
direction comparable to the already-windowed `host-write-dpu-read` direction.
The new mode is implemented by
[`run_dpu_write_publish_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1911):
the DPU fills reusable local slots, queues async DMA writes into host memory,
tracks completed epochs, advances only the contiguous completed frontier, and
then DMA-writes `HdvControlBlock.published_epoch`.

The publication under test is intentionally separate from sync-event:

- Data path: queued async DOCA DMA writes from DPU memory to host ring slots.
- Completion gate: DPU observes DOCA data-DMA task completions.
- Publication path: a later small DOCA DMA write of the host-resident
  `published_epoch` word.
- Host consumer: host CPU polls `published_epoch` and validates host-resident
  slots.

Async no-sync-event DPU-write / host-consume results under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| Async DPU DMA write + DMA frontier, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `212.11 MiB/s`, host validation `212.15 MiB/s` | `/tmp/hdv_async_nosync_dpu_async_write_64b_1m_1782511600` |
| Async DPU DMA write + DMA frontier, fixed 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `384.09 MiB/s`, host validation `383.92 MiB/s` | `/tmp/hdv_async_nosync_dpu_async_write_4k_1m_1782511602` |
| Async DPU DMA write + DMA frontier, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `375.36 MiB/s`, host validation `375.19 MiB/s` | `/tmp/hdv_async_nosync_dpu_async_write_mixed_1m_1782511613` |

Matched no-sync-event host-write / DPU-async-pull reruns under `force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| Host CPU write + CPU frontier, async DPU pull, fixed small records | `slot=64 B`, `iterations=1000000`, `batch=1024`, `window=128`, full validation | PASS, DPU `179.83 MiB/s`, host `26.97 MiB/s` | `/tmp/hdv_async_nosync_host_async_pull_64b_1m_1782511620` |
| Host CPU write + CPU frontier, async DPU pull, fixed 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `223.25 MiB/s`, host `176.02 MiB/s` | `/tmp/hdv_async_nosync_host_async_pull_4k_262k_1782511622` |
| Host CPU write + CPU frontier, async DPU pull, mixed 64 B / 4 KiB records | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, full validation | PASS, DPU `220.67 MiB/s`, host `145.07 MiB/s` | `/tmp/hdv_async_nosync_host_async_pull_mixed_262k_1782511628` |

Interpretation:

- Both directions now have async/windowed no-sync-event validation coverage for
  fixed 64 B, fixed 4 KiB, and mixed 64 B / 4 KiB records.
- For DPU-push, the important new evidence is that a DMA frontier write issued
  after observed data-DMA completions empirically published the earlier queued
  DMA writes to the host consumer. We did not observe torn or stale slot data.
- This is still an empirical machine/protocol result, not a documented DOCA
  replacement for RDMA `WRITE_WITH_IMM`. The protocol remains defensible only
  if the DPU publication word is issued after the data-DMA completions that it
  claims to publish.
- The next stricter correctness test is an async early-publication negative
  mode that deliberately writes the frontier before data completions and checks
  that the host validator fails quickly.

### June 26, 2026: Completion-Gated Publication Does Not Imply DMA Depth 1

Question: if the DPU waits for data-DMA completion before publishing a frontier,
does that collapse the DMA pipeline to a single in-flight payload DMA?

Answer from the harness: no, not if the implementation separates the payload
frontier from the publication frontier. The completion-gated
`write-publish-async` mode keeps up to `--async-window` payload DMA writes in
flight, observes data-DMA completions, advances a contiguous completed frontier,
and only then posts the small DMA write that publishes
`HdvControlBlock.published_epoch`. That is different from the synchronous helper
path in [`hdv_dma_copy()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1092),
which submits one DMA task and spins in `doca_pe_progress()` until its callback
marks the task done before returning.

Implementation pointers:

- [`run_dpu_write_publish_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1911)
  is the completion-gated, windowed DPU-write path. It submits new data DMA work
  while `in_flight < config->async_window`, records completed epochs, and only
  publishes the completed contiguous prefix.
- [`hdv_async_submit_host_write()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1307)
  retargets a reusable DMA task and submits a payload DMA write.
- [`write_remote_u64()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1158)
  is still synchronous for the small publication word, so publication itself is
  completion-waited, but this does not force the preceding payload stream to
  have window depth 1.

Completion-depth contrast results under `PCI_WR_ORDERING=force_relax`:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| Synchronous DPU DMA write + DMA frontier | `slot=4 KiB`, `iterations=262144`, `batch=1024`, header validation | PASS, DPU `2181.99 MiB/s`, host `2188.17 MiB/s` | `/tmp/hdv_completion_depth_1782515810/sync_4k` |
| Async completion-gated publish, depth 1 | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=1`, header validation | PASS, DPU `2075.24 MiB/s`, host `2081.45 MiB/s` | `/tmp/hdv_completion_depth_1782515810/async_w1_4k` |
| Async completion-gated publish, depth 32 | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=32`, header validation | PASS, DPU `15190.02 MiB/s`, host `15232.95 MiB/s` | `/tmp/hdv_completion_depth_1782515810/async_w32_4k` |
| Async completion-gated publish, depth 128 | `slot=4 KiB`, `iterations=262144`, `batch=1024`, `window=128`, header validation | PASS, DPU `14578.38 MiB/s`, host `14632.40 MiB/s` | `/tmp/hdv_completion_depth_1782515810/async_w128_4k` |
| Async completion-gated publish, depth 1 | `slot=1 MiB`, `iterations=8192`, `batch=128`, `window=1`, header validation | PASS, DPU `10097.64 MiB/s`, host `10247.25 MiB/s` | `/tmp/hdv_completion_depth_1m_1782515835/async_w1_1m` |
| Async completion-gated publish, depth 32 | `slot=1 MiB`, `iterations=8192`, `batch=128`, `window=32`, header validation | PASS, DPU `30059.34 MiB/s`, host `29843.69 MiB/s` | `/tmp/hdv_completion_depth_1m_1782515835/async_w32_1m` |
| Async completion-gated publish, depth 128 | `slot=1 MiB`, `iterations=8192`, `batch=128`, `window=128`, header validation | PASS, DPU `29728.42 MiB/s`, host `30017.14 MiB/s` | `/tmp/hdv_completion_depth_1m_1782515835/async_w128_1m` |

Interpretation:

- Waiting for data-DMA completion before publishing does not inherently serialize
  the data path. It serializes only the publication frontier behind the completed
  contiguous prefix. The data path can still have many outstanding payload DMA
  tasks.
- A call-site that submits one DMA and immediately waits for that exact task,
  such as the synchronous `hdv_dma_copy()` helper, behaves like pipeline depth 1.
  The async mode demonstrates the alternative: multiple payload tasks in flight,
  completion callbacks retire them, and publication advances independently in
  batches.
- This is the DOCA DMA analogue of batching CQ progress in the RDMA transport:
  do not wait for one work request and publish one object at a time. Poll or
  process a batch of completions, advance the largest contiguous completed
  frontier, and publish that frontier once. The publication cadence is therefore
  decoupled from both individual payload task submission and individual payload
  task completion, while still refusing to publish bytes whose data-DMA
  completion has not been observed.
- In these runs, moving from `window=1` to `window=32` improved 4 KiB DPU-write
  throughput from about `2.1 GiB/s` to about `14.8 GiB/s`, and 1 MiB throughput
  from about `9.9 GiB/s` to about `29.4 GiB/s`. Larger `window=128` did not
  improve over `window=32` in this specific DPU-push test.
- For Homer, the useful design rule is therefore: do not block the producer loop
  on every payload DMA; track a separate completed frontier and publish only
  completed contiguous ranges. That preserves the publication safety property we
  want to test while keeping the payload DMA stream pipelined.

### June 27, 2026: Split DMA Context Publication Ordering

Question: if one thread submits payload DMA tasks to one DOCA DMA context and
then submits the tail/publication DMA to another DOCA DMA context, does the
same-thread submission order empirically provide a usable publication-ordering
invariant?

Harness changes:

- Added `HDV_MODE_WRITE_PUBLISH_SPLIT_NOWAIT` and
  `HDV_MODE_WRITE_PUBLISH_SPLIT_COMPLETION` to
  [`HdvMode`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation_common.h:14).
- Reused the async DPU-push loop in
  [`run_dpu_write_publish_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1947)
  with separate `data_dma` and `publish_dma` arguments.
- For split modes, [`run_dpu()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:2318)
  initializes a second DMA context with
  [`hdv_dpu_init()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1076),
  so payload writes and publication writes are submitted through independent
  DOCA DMA contexts.
- `write-publish-split-completion`: data ctx submits payload DMA; the harness
  waits for the data ctx's completed contiguous frontier; publication ctx then
  DMA-writes `published_epoch`.
- `write-publish-split-nowait`: data ctx submits payload DMA; publication ctx
  immediately DMA-writes the submitted frontier before payload completions are
  observed. Local task-lane reuse still waits for data completion, so the unsafe
  variable is specifically cross-context publication ordering.

Results:

| Mode | Parameters | Result | Artifact |
| --- | --- | --- | --- |
| single ctx completion-gated, 1 MiB | `write-publish-async`, `slot-count=64`, `slot=1 MiB`, `window=32`, `batch=1`, `iterations=1024`, full validation | PASS, DPU `377.85 MiB/s`, host `381.18 MiB/s`, 34 publish DMA writes | `/tmp/hdv_singlectx_1m_cmp_1782533888` |
| split completion-gated, 1 MiB | `slot-count=64`, `slot=1 MiB`, `window=32`, `batch=1`, `iterations=1024`, full validation | PASS, DPU `375.74 MiB/s`, host `379.15 MiB/s` | `/tmp/hdv_split_completion_1782533294` |
| split nowait, 1 MiB | same shape as above | PASS, DPU `381.64 MiB/s`, host `393.40 MiB/s` | `/tmp/hdv_split_nowait_1782533314` |
| split nowait, harsher 1 MiB | `slot-count=128`, `slot=1 MiB`, `window=128`, `batch=1`, `iterations=4096`, full validation | PASS, DPU `375.61 MiB/s`, host `387.28 MiB/s` | `/tmp/hdv_split_nowait_harsh_1782533337` |
| split nowait, 64 B | `slot-count=4096`, `slot=64 B`, `window=128`, `batch=1`, `iterations=1000000`, full validation | FAIL at host epoch 96: `seq_begin=11936128518282651045 expected=96`; DPU later saw publish DMA I/O failure after host exited | `/tmp/hdv_split_nowait_64b_1782533374` |
| split completion-gated, 64 B | same 64 B shape | PASS, DPU `37.19 MiB/s`, host `37.15 MiB/s` | `/tmp/hdv_split_completion_64b_1782533391` |
| single ctx completion-gated, 64 B | `write-publish-async`, same 64 B shape | PASS, DPU `328.23 MiB/s`, host `327.97 MiB/s`, 15375 publish DMA writes | `/tmp/hdv_singlectx_64b_cmp_1782533867` |

Interpretation:

- Same-thread submission to two DOCA DMA contexts is not a sufficient
  publication-ordering mechanism. The 64 B nowait split-context run exposed
  host validation failure quickly.
- The 1 MiB nowait passes are still informative, but they are not a contract.
  In the harsher 1 MiB run, the DPU progress log showed host `consumed` moving
  beyond the DPU's observed completion frontier during the run. That means
  remote host memory can become visible before the DPU has received the
  initiator-side completion callback; it does not mean publication from another
  context is ordered behind data DMA.
- The positive 64 B result proves that using separate contexts is not inherently
  broken. It is safe in this harness only when the publication context is gated
  by the data context's observed completed contiguous frontier.
- Performance is not neutral. For the 1 MiB full-validation shape, single ctx
  and split completion-gated ctx were effectively tied at about `376-378 MiB/s`;
  the host payload scanner dominates that run. For the 64 B shape, single ctx
  was much faster: `328.23 MiB/s` versus `37.19 MiB/s`. The single-ctx run
  coalesced the completed frontier into only `15375` publish DMA writes for
  `1000000` records, while the split completion-gated run published every
  record and spent far more time in progress/callback bookkeeping. The current
  split implementation is therefore a correctness validator, not a tuned
  scheduler design.
- For Homer, keep command/completion payload records and their tail publication
  on the same DMA context when possible. If later scheduling wants separate
  contexts for priority or workload-class isolation, the publication step must
  be completion-gated across contexts. Do not rely on one thread submitting data
  to ctx A and tail to ctx B as an RDMA `WRITE_WITH_IMM` analogue.

### June 27, 2026: Coalescing And Progress-Loop Design Corrections

Recent discussion clarified three separate mechanisms that were easy to conflate:

1. **PE progress drain**: one call to `doca_pe_progress()` is one progress
   attempt, not a full CQ-style drain. The header contract for
   [`doca_pe_progress()`](/opt/mellanox/doca/include/doca_pe.h:181) says it
   finds the next context with a completed task and returns `1` when progress
   was made. A scheduler that wants to amortize polling across many sessions
   should call it until it returns `0`, or use a bounded budget to avoid
   starving higher-priority work.
2. **DOCA completion-report coalescing**: `OPTIMIZE_REPORTS` plus a flushed
   non-optimized sentinel can coalesce initiator-side completion reporting across
   many DMA tasks submitted to the same DMA context. If many pgbench sessions
   share one command-pull DMA context, this can reduce callback/report overhead
   across sessions. It does not merge per-session rings, per-session DPU-side
   sinks, or per-session tail publication.
3. **Application batching**: if one session has multiple contiguous records with
   the same visibility fate, Homer should prefer one larger DMA task and one tail
   update over many small DMA tasks plus one tail. If records are in separate
   session rings, wrap in a ring, or land in separate DPU rings/sinks, the
   application cannot coalesce them into one semantic tail without a new
   multiplexed queue abstraction.

The single-ctx DPU-push comparison above also exposed an incidental harness
effect. In `write-publish-async`, publication uses synchronous
[`write_remote_u64()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1177),
which calls [`hdv_dma_copy()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1119).
While waiting for the tail DMA completion, `hdv_dma_copy()` repeatedly progresses
the same PE. In the single-ctx DPU-push case, those progress calls also retire
payload completions on the same ctx/PE, so the next loop may observe a much
larger completed frontier and issue fewer tail writes. That is real latency
hiding, but it is accidental and policy-poor: tail-DMA wait time decides how
much payload completion progress happens.

Cleaner target loop:

```text
submit payload DMA work within per-class/per-session window
drain or budget-progress the PE explicitly
retire task completions into per-session completed frontiers
submit more payload DMA work when credits allow
submit async tail/consumed updates when a per-session policy says to publish
continue progressing payload and tail tasks without blocking the scheduler on
one synchronous tail write unless correctness or queue capacity requires it
```

For the likely Homer DPU-pull path, the relevant implementation today is
[`run_dpu_host_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1367)
or the sync-event variant
[`run_dpu_sync_event_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1736),
not the DPU-push `write-publish-async` comparison. In DPU-pull, the DPU pulls
from host-published per-session rings and then updates per-session consumed
frontiers. Multiple sessions sharing one DMA ctx can still benefit from
ctx-wide DOCA completion-report coalescing, but application publication remains
per session.

Current design implication for Homer:

- Latency-sensitive command/completion rings likely have `K=1` within one
  session because the next command depends on the previous completion. Here,
  deep application tail batching is not expected to help; shared ctx
  report-coalescing across sessions may still reduce DMA completion overhead if
  the sentinel interval is bounded by latency.
- Throughput-oriented byte streams, such as basebackup-like payload rings,
  should use larger contiguous DMA tasks and less frequent per-session tail
  publication when the byte-ring layout allows it.
- Separate workload classes may use separate DMA contexts to isolate latency and
  batching policy. Keep payload and the publication that depends on it in the
  same ctx when possible; if not, publish only after explicit completion-gating
  from the payload ctx.

### June 26, 2026: DMA Queue-Depth Sweet Spot By Message Size

We swept the harness's DMA queue-depth knob, `--async-window`, across fixed
record sizes from 64 B through 1 MiB. These runs used header validation to focus
on DOCA DMA/task throughput rather than DPU CPU payload scanning.

Common parameters:

- `PCI_WR_ORDERING=force_relax` on both BF3 PFs.
- `--slot-count=1024`
- `--batch-size=1024`
- `--payload-mode=header`
- `--size-pattern=fixed`
- windows tested: `1, 2, 4, 8, 16, 32, 64, 128, 256`
- DPU-push mode: `write-publish-async`, implemented by
  [`run_dpu_write_publish_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1911).
- DPU-pull mode: `host-write-dpu-read` with `--async-window > 1`, implemented
  by [`run_dpu_host_read_async()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:1330).

Artifacts:

- DPU-push summary: `/tmp/hdv_qdepth_push_1782517561/summary.tsv`
- DPU-pull summary: `/tmp/hdv_qdepth_pull_1782517699/summary.tsv`
- The earlier failed push sweep `/tmp/hdv_qdepth_push_1782517408` is
  diagnostic-only: rsync copied the x86 host binary over the DPU binary and the
  DPU failed with `Exec format error`. The rerun rebuilt the DPU binary after
  source sync and is the valid result set.

Definition used below:

- **Best window**: highest DPU-side MiB/s observed in the one-pass sweep.
- **95% knee**: smallest window whose DPU-side MiB/s is at least 95% of the best
  observed value for that size. This is usually the better queue-depth choice
  than the absolute best because large windows can add bookkeeping pressure and
  run-to-run noise without material throughput gain.

DPU-push, DPU DMA-write into host memory followed by completion-gated DMA
frontier publication:

| Record size | Best window | Best DPU MiB/s | 95% knee window | 95% knee MiB/s | Suggested queue depth |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64 B | 64 | 246.13 | 32 | 235.08 | 32-64 |
| 1 KiB | 64 | 3976.53 | 32 | 3972.36 | 32-64 |
| 4 KiB | 64 | 15652.47 | 32 | 15376.78 | 32-64 |
| 16 KiB | 32 | 29706.55 | 8 | 29526.54 | 8-32 |
| 64 KiB | 16 | 30276.25 | 8 | 30039.63 | 8-16 |
| 256 KiB | 256 | 30236.39 | 4 | 30153.79 | 4-16; deeper gave no meaningful gain |
| 1 MiB | 64 | 30233.45 | 4 | 29621.76 | 4-16; deeper mostly noise |

DPU-pull, DPU DMA-read from host memory after a host CPU frontier:

| Record size | Best window | Best DPU MiB/s | 95% knee window | 95% knee MiB/s | Suggested queue depth |
| --- | ---: | ---: | ---: | ---: | ---: |
| 64 B | 16 | 222.42 | 16 | 222.42 | 16 |
| 1 KiB | 16 | 3489.22 | 16 | 3489.22 | 16 |
| 4 KiB | 64 | 13525.05 | 16 | 13131.63 | 16-64 |
| 16 KiB | 32 | 34648.36 | 16 | 34041.36 | 16-32 |
| 64 KiB | 32 | 35427.52 | 16 | 35330.62 | 16-32 |
| 256 KiB | 64 | 35590.75 | 8 | 34858.54 | 8-16; deeper mostly noise |
| 1 MiB | 64 | 35632.68 | 8 | 34538.44 | 8-16; deeper mostly noise |

Interpretation:

- Queue depth matters most for small records because each DMA task carries little
  payload. Depth 1 is not enough even for 1 KiB and 4 KiB records.
- For DPU-push writes, 64 B through 4 KiB records benefitted up to about
  `window=32-64`. Larger windows (`128`, `256`) hurt the small-record cases.
- For DPU-pull reads, 64 B and 1 KiB peaked around `window=16`, while 4 KiB
  needed a deeper window (`32-64`) to reach the absolute best in this run.
- For 16 KiB and larger records, the practical knee is much lower: roughly
  `window=8-16` for push and `window=8-16` for pull, with large records reaching
  the PCIe-class plateau around 30-35 GiB/s.
- A single fixed queue depth for an initial Homer DPU prototype should likely be
  `32` if we want one conservative value across small and medium messages. A
  size-aware policy can use lower depths for large transfers: around `16` for
  DPU-pull and `8-16` for DPU-push once record size is at least 16-64 KiB.
- These are one-pass standalone harness numbers, not a final integrated Homer
  policy. Before using them as a default, repeat the sweep under the intended
  CPU placement, sync-event publication shape, and multi-backend load.

### June 26, 2026: DOCA PCI Relaxed-Ordering Flag Probe

We attempted to rerun the async no-sync-event matrix with the harness
`--relaxed-ordering` switch, which adds
`DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` to the host mmap permissions at
[`doca_mmap_set_permissions()`](/data/dbcomm/postgres-citus/homer/doca_validation/doca_homer_validation.c:676).
This did not reach the DMA experiments: all six host setup attempts failed
with `doca_mmap_set_permissions failed: Invalid input`.

Failed matrix setup artifacts:

| Intended mode | Artifact | Failure |
| --- | --- | --- |
| `write-publish-async`, 64 B | `/tmp/hdv_async_nosync_relaxed_dpu_async_write_64b_1m_1782511979` | `Invalid input` at mmap permissions |
| `write-publish-async`, 4 KiB | `/tmp/hdv_async_nosync_relaxed_dpu_async_write_4k_1m_1782511979` | `Invalid input` at mmap permissions |
| `write-publish-async`, mixed 64 B / 4 KiB | `/tmp/hdv_async_nosync_relaxed_dpu_async_write_mixed_1m_1782511979` | `Invalid input` at mmap permissions |
| `host-write-dpu-read`, 64 B | `/tmp/hdv_async_nosync_relaxed_host_async_pull_64b_1m_1782511980` | `Invalid input` at mmap permissions |
| `host-write-dpu-read`, 4 KiB | `/tmp/hdv_async_nosync_relaxed_host_async_pull_4k_262k_1782511980` | `Invalid input` at mmap permissions |
| `host-write-dpu-read`, mixed 64 B / 4 KiB | `/tmp/hdv_async_nosync_relaxed_host_async_pull_mixed_262k_1782511980` | `Invalid input` at mmap permissions |

To isolate the failure from the harness, we compiled scratch probes under
`/tmp` against the same DOCA 3.2 headers and libraries:

- `DOCA_ACCESS_FLAG_PCI_READ_ONLY`: `doca_mmap_set_permissions()`,
  `doca_mmap_start()`, and `doca_mmap_export_pci()` succeeded.
- `DOCA_ACCESS_FLAG_PCI_READ_WRITE`: `doca_mmap_set_permissions()`,
  `doca_mmap_start()`, and `doca_mmap_export_pci()` succeeded.
- `DOCA_ACCESS_FLAG_PCI_READ_ONLY | DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`:
  `doca_mmap_set_permissions()` returned `Invalid input`.
- `DOCA_ACCESS_FLAG_PCI_READ_WRITE | DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`:
  `doca_mmap_set_permissions()` returned `Invalid input`.
- Adding `DOCA_ACCESS_FLAG_LOCAL_READ_WRITE` did not change that result:
  `LOCAL_READ_WRITE | PCI_READ_ONLY | PCI_RELAXED_ORDERING` and
  `LOCAL_READ_WRITE | PCI_READ_WRITE | PCI_RELAXED_ORDERING` both returned
  `Invalid input`.
- `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` alone was accepted by
  `doca_mmap_set_permissions()` and `doca_mmap_start()`, but
  `doca_mmap_export_pci()` returned `Operation not permitted`, so it is not a
  usable permission set for this host-DPU PCI DMA export.

Interpretation:

- On the current farnet1 DOCA 3.2 runtime, the exposed mmap API does not let us
  combine PCI export permissions with `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING`.
  The async ordering experiments therefore cannot currently be rerun with this
  per-mmap flag.
- This does not contradict the existing `PCI_WR_ORDERING=force_relax` machine
  state. The firmware/global PCI policy can still be relaxed even though the
  DOCA mmap API rejects the per-mmap relaxed-ordering bit for this PCI-export
  path.
- Practical next step if we need per-mkey/per-mmap relaxed ordering is to look
  for a different DOCA API path, a release-note caveat, or a newer DOCA build;
  the current DMA/export path cannot exercise it.

### June 26, 2026: Small-Message Sync-Event Latency Probe

We ran a forced ping-pong shape to estimate sync-event cost for very small
message exchanges. The harness used `slot-count=1`, `batch-size=1`,
`async-window=1`, `slot-bytes=64`, full validation, and a 5 second host
`ready-delay` so DPU process startup did not contaminate the host-side measured
loop. In this configuration, every message must be consumed before the producer
can publish the next one.

Host-produce / DPU-consume results:

| Mode | Iterations | Host measured loop | Approx per message | Artifact |
| --- | --- | --- | --- | --- |
| no-sync `host-write-dpu-read` | 100000 | `0.537499 s` | `5.37 us` | `/tmp/hdv_latency_ready_100k_host_to_dpu_nosync_1782513549` |
| sync-event `sync-event-publish` | 100000 | `0.761142 s` | `7.61 us` | `/tmp/hdv_latency_ready_100k_host_to_dpu_sync_1782513555` |

DPU-produce / host-consume results:

| Mode | Iterations | Host measured loop | Approx per message | Artifact |
| --- | --- | --- | --- | --- |
| no-sync `write-publish` | 100000 | `0.481642 s` | `4.82 us` | `/tmp/hdv_latency_ready_100k_dpu_to_host_nosync_1782513561` |
| sync-event `dpu-sync-event-publish` | 100000 | `0.511707 s` | `5.12 us` | `/tmp/hdv_latency_ready_100k_dpu_to_host_sync_1782513566` |

We also used `strace -c` on 10k sync-event runs:

- DPU-side `sync-event-publish` with 10k `doca_sync_event_wait_gt()` calls
  made only `561` total syscalls; it did not make a syscall per event wait.
- Host-side `dpu-sync-event-publish` with 10k host waits made only `595` total
  syscalls; again, no syscall-per-message behavior was visible.

Interpretation:

- `doca_sync_event_wait_gt()` is a blocking DOCA API call, but in this harness
  it does not appear to block by yielding to the Linux scheduler per message.
  The syscall profile is consistent with userspace/DOCA progress or polling
  around mapped/device state, with setup/control ioctls rather than one syscall
  per event.
- For tiny 64 B ping-pong exchanges, sync-event adds measurable latency:
  roughly `+2.24 us/message` for host-produce / DPU-consume and
  `+0.30 us/message` for DPU-produce / host-consume in these runs.
- The benefit of sync-event is therefore not lower hot ping-pong latency. Its
  value is wakeup/doorbell behavior when avoiding constant frontier polling is
  more important than shaving microseconds from an always-hot small-message
  exchange.

### June 26, 2026: Sync-Event CPU-Time Probe

We followed up the syscall probe with `/usr/bin/time -v` on the consumer side
of the 64 B ping-pong cases. The question was whether `doca_sync_event_wait_gt()`
actually saves CPU compared with polling the memory frontier, even though it
does not make one syscall per wait.

Host-produce / DPU-consume, with 5 second host `ready-delay` before publishing:

| Mode | Timed side | User CPU | Wall time | CPU % | Artifact |
| --- | --- | ---: | ---: | ---: | --- |
| no-sync `host-write-dpu-read` | DPU consumer | `4.01 s` | `4.18 s` | `96%` | `/tmp/hdv_cpu_idle_host_to_dpu_nosync_1782513724` |
| sync-event `sync-event-publish` | DPU consumer | `4.48 s` | `4.66 s` | `96%` | `/tmp/hdv_cpu_idle_host_to_dpu_sync_1782513730` |

Host-produce / DPU-consume, no ready delay:

| Mode | Timed side | User CPU | Wall time | CPU % | Artifact |
| --- | --- | ---: | ---: | ---: | --- |
| no-sync `host-write-dpu-read` | DPU consumer | `0.53 s` | `0.72 s` | `75%` | `/tmp/hdv_cpu_hot_host_to_dpu_nosync_1782513748` |
| sync-event `sync-event-publish` | DPU consumer | `0.76 s` | `0.94 s` | `82%` | `/tmp/hdv_cpu_hot_host_to_dpu_sync_1782513750` |

DPU-produce / host-consume, no ready delay:

| Mode | Timed side | User CPU | Wall time | CPU % | Artifact |
| --- | --- | ---: | ---: | ---: | --- |
| no-sync `write-publish` | host consumer | `1.74 s` | `1.77 s` | `98%` | `/tmp/hdv_cpu_hot_dpu_to_host_nosync_1782513752` |
| sync-event `dpu-sync-event-publish` | host consumer | `1.76 s` | `1.80 s` | `98%` | `/tmp/hdv_cpu_hot_dpu_to_host_sync_1782513754` |

Interpretation:

- In these harness modes, sync-event wait does **not** save CPU compared with
  memory-frontier polling. The timed waiters still consume most of a CPU core.
- The DPU-side host-produce/DPU-consume idle-delay case is the clearest signal:
  both no-sync and sync-event consumers spent about `96%` CPU while waiting and
  draining the 100k tiny messages.
- This suggests `doca_sync_event_wait_gt()` behaves like a userspace/device
  polling wait in this configuration, not like a sleep/yield primitive.
- For Homer, sync-event should not be justified as a CPU-saving idle wait unless
  a different DOCA wait mode or integration with an OS blocking primitive is
  found and measured. Its current value is a semantic doorbell/event value, not
  lower CPU consumption.

## Decision Matrix For Homer

After the phases above, record the result in this table:

| Question | Evidence Needed | Homer Decision |
| --- | --- | --- |
| Can DPU publish host completions with DMA write of payload then DMA write of epoch? | Mode A passes stress with no torn slots | Use DPU DMA writes for completion/result rings |
| Can one publish epoch cover many DMA-written slots? | Mode A3 passes at batch sizes 4/16/64 | Batch completion/result publication |
| Can host CPU publish slots for DPU pull with release-store? | Mode B passes stress | Let backend publish host rings and DPU pull |
| Is sync-event a safe publish frontier? | C1/D2 pass without memory epoch and official semantics or strong stress evidence | Maybe use sync-event value as frontier |
| Is sync-event only a wakeup? | C3/D1 pass and C2/D2 are weak or ambiguous | Keep memory epoch as frontier; sync-event wakes idle DPU/host |
| Is COMCH hot-path viable? | E2 latency and CPU cost are acceptable | Consider coarse COMCH doorbells |
| Should COMCH stay cold-path? | E1 works, E2/E4 too slow for hot path | Use COMCH for descriptor exchange/setup only |
| Can relaxed ordering be enabled? | Phase 5 passes all relevant modes | Enable only for validated mappings/modes |
| Is cache invalidation needed? | Phase 5 without invalidate fails or shows stale reads | Add explicit invalidate or avoid that memory type |

Default expected decision before testing: DMA for data movement, memory-resident monotonic epoch as the authoritative frontier, sync-event/COMCH as setup or wakeup, relaxed ordering disabled.

## Notes For Later Integration

If Mode A and B pass, the first Homer integration should still preserve the current publication discipline:

- backend-owned producer rings keep a single writer and monotonic `publishedTail`
- DPU-owned completion/result rings keep a single writer and monotonic `publishedEpoch`
- sync-event or COMCH carries only "go look through epoch E" until stronger semantics are proven
- slot reuse is gated by `consumedHead`/`consumedEpoch`, not by local DMA completion alone

That maps cleanly to the existing peer RDMA rule where [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4235) writes the mailbox body and then publishes via a final responder-visible operation.
