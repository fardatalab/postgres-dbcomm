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

Immediate follow-up implementation work:

1. Add an async DPU-write path with queued data DMA plus queued publish DMA to
   test DPU-issued publication order without waiting for every data completion.
2. Add a sync-event variant for DPU-issued DMA writes followed by sync-event
   notify, because the current sync-event evidence covers host CPU writes
   followed by host sync-event update.
3. Replace descriptor files with COMCH setup in the harness and measure cold
   setup latency.
4. Add a multi-stream version of async DPU pull to model several DB backends or
   Homer lanes sharing the DPU DMA engine.
5. Add relaxed-ordering and CPU cache pre-touch stress variants to make the
   empirical memory-visibility evidence harsher.

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
