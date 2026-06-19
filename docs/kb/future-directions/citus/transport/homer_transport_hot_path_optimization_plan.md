# Homer Transport Hot-Path Optimization Plan

## Scope

- **What this doc explains**: an implementation-ready staged optimization plan
  for Homer transport hot paths after the peer-client completion publication,
  frontend peek/apply/ack, and tuple-result generation/EOS correctness repairs.
  It covers measurement gates, common transport identity, lane-owned CQ
  dispatch, result-frontier/completion overlap, compact completion descriptors,
  immutable EOS batching, scheduler budgets, ready-bitmaps, and basebackup bulk
  tuning.
- **What this doc does NOT cover**: new Homer user-visible functionality, the
  full guarded-action scheduler redesign, DOCA/DPU offload, or the service-to-
  service peer push-completion protocol beyond shared CQ/lifetime mechanics.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The current tree has recovered correctness for remote client-SQL pgbench and
basebackup, but the accepted path still carries avoidable hot-path work:
oversized command/completion records, extra STARTED/EOS events, conservative
payload/completion serialization, global CQ owner scans, callback-scoped CQ
stashing, repeated scheduler scans, and basebackup semantic metadata repeated in
bulk chunks.

This plan is separate from
[`homer_unified_aggregate_action_scheduler_plan.md`](homer_unified_aggregate_action_scheduler_plan.md).
The larger scheduler redesign should consume cleaner transport facts later. This
note defines concrete sub-slices that can be implemented and validated directly
without making each patch rediscover the ABI, ownership, and failure behavior.

## Key code pointers

- [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:449)
  currently embeds command hot state, queue binding, full tuple contract, and
  detail text in one fixed-width completion record.
- [`CitusTupleSinkQueueDescriptor`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:225)
  mixes mostly-static queue attachment fields with per-command binding fields
  such as `resultGeneration`, `startByteTail`, and `firstRingRecordOrdinal`.
- [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:166)
  is the fixed-capacity tuple-view contract that makes row-result completion
  records large.
- [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:380)
  carries the current transport identity fields, including `resultGeneration`,
  `ringRecordOrdinal`, `generationSequence`, and `sinkSequence`.
- [`CitusHomerPayloadByteRingControl`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:277)
  defines the shared byte-ring frontier contract used by tuple-result and
  basebackup streams.
- [`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:977)
  implements the slot-local ready-epoch stable-read gate for pushed frontend
  completions.
- [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2338),
  [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3685),
  [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3506),
  and
  [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2452)
  are the accepted measured frontend completion ownership path.
- [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2500)
  is still a destructive compatibility wrapper: it peeks, copies, validates, and
  ACKs internally. Stateful measured clients should not use it.
- [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904)
  still has terminal-drain retry/spin behavior; the target is a bounded
  nonblocking drain API.
- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13072)
  publishes backend-node client-SQL completions into the peer frontend mailbox
  and currently gates terminal completion on tuple-result payload dependencies.
- [`TupleSinkServiceReservePeerClientCompletionPublishCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3668)
  reserves a global outstanding-table entry for peer-client completion source
  retirement.
- [`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3840)
  scans the global completion-publish table to retire a same-connection prefix.
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1840)
  decodes peer-client completion publish CQEs, but stashes them if the current
  callback set cannot consume that namespace.
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1985)
  is the closest current send-CQ drain surface, but it still accepts partial
  callback sets and replays stashed CQEs.
- [`TupleSinkServiceWaitForSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1883),
  payload send polling at
  [`remote_execution_peer_transport_rdma.c:6743`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6743),
  command send polling at
  [`remote_execution_peer_transport_rdma.c:6928`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928),
  and peer-pump send-CQ polling at
  [`remote_execution_peer_transport_rdma.c:8236`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8236)
  are existing send-CQ poll paths that must be accounted for before there is a
  single owner per physical CQ.
- [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532)
  is residual service-side tuple-view EOS repair/scaffolding. The accepted
  direction is backend-owned immutable EOS; residual use/dead code must be
  removed or explicitly fenced before optimizing the result path.
- [`HomerServiceAppendOutgoingBaseBackupFragmentWrite()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12421)
  shows that basebackup already fragments semantic objects into RDMA publication
  descriptors; Stage 7 tunes this existing range path before adding new
  mechanisms.
- [`HomerServiceBuildSessionMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26880)
  and
  [`HomerServiceBuildPayloadMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27075)
  still rebuild readiness through active-table scans.

## Current behavior and performance baselines

- Current accepted validation after the generation/EOS and peek/apply/ack fixes
  is: remote c1 correct at about `4.17k` and `3.76k TPS`; remote c4 correct at
  about `10.47k` and `10.39k TPS`; local blackhole basebackup `10.44s`; remote
  RDMA basebackup warmed repeats `4.43s` and `4.32s`.
- Historical performance ceilings observed before later latent correctness
  defects were fixed:

  | Checkpoint | Remote c1 | Remote c4 | Remote basebackup |
  | --- | ---: | ---: | ---: |
  | May 31 | `4,820.286 TPS` | `11,144.880 TPS` | `4.35 s` |
  | May 30 | `4,869.43 TPS` | `11,457.72 TPS` | `4.32 s` |
  | May 30 concurrent | - | `9,486.10 TPS` | `4.45 s` |

  These are performance targets, not accepted correctness baselines.
- CQ stashing has not been eliminated. The current connection state still has
  stashes for payload sends, command sends, and peer-client completion publish
  CQEs in
  [`TupleSinkServicePeerConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:504).
- Basebackup shares the byte-ring transport substrate with SQL tuple results,
  but it must not inherit SQL-result command-generation lifecycle.

## Design qualifications to preserve

1. **FIFO queues are for physical ownership, not semantic policy.** Per-QP FIFOs
   retire source slots and WR ownership in post order. Scheduler policy still
   decides which semantic work to run next. Recv-CQ immediates should be parsed
   into typed ready facts or direct state updates, not queued as a generic
   semantic FIFO.
2. **The CQ dispatcher should be direct, not a generic callback framework.** The
   target hot path is one batched `ibv_poll_cq`, a WR-ID tag switch, generation
   validation, direct array/FIFO access, and typed counter updates. Avoid hash
   maps, vtables, linked lists, and callback-absent stash behavior.
3. **Batching must not hide ready producer work.** Do not restore producer-side
   delayed `publishedTail` for basebackup or tuple-result streams merely to
   improve batch counters. Prefer publishing ready producer work promptly and
   batching at the transport range / CQ / scheduler-budget layer.

## Stage Template

Every implementation stage should carry these subsections in the implementation
note or commit message:

- `Prerequisites`
- `Files and functions changed`
- `New data structures/API`
- `State transitions and ownership`
- `Failure behavior`
- `Protocol/version changes`
- `Migration commits`
- `Targeted tests`
- `Counters and acceptance`
- `Old code deleted after acceptance`

## Stage 0: baseline, exact counters, and residual cleanup

Goal: make later optimization decisions measurable and remove stale scaffolding
that would mislead implementers.

### Substeps

1. Record exact Postgres/Citus commits, dirty-state summary, build flags, binary
   hashes, CPU placement, farnet lane addresses, DB state, basebackup byte count,
   and benchmark commands before each comparison.
2. Use one cold/warmup run plus `3-5` warmed repeats. Use median TPS or median
   wall time as the primary result; retain min/max and p95/p99.
3. Define counters with exact increment points:

   | Counter | Exact increment condition |
   | --- | --- |
   | `completionEventsPublished` | Ready-WIMM successfully posted, not merely staged |
   | `completionEventsAcked` | Release-store of completion mailbox `consumedEpoch` succeeds |
   | `completionBytesPosted` | Sum of successful completion-body WR lengths |
   | `commandBytesPosted` | Sum of successful command-body WR lengths |
   | `payloadFrontierWaits` | Transition into blocked-on-frontier, not each poll iteration |
   | `terminalCompletionPayloadWaits` | Terminal completion becomes blocked on required payload frontier |
   | `sendCqPolls` | Every `ibv_poll_cq` call on a send CQ |
   | `sendCqEmptyPolls` | `ibv_poll_cq` returns zero on a send CQ |
   | `sendCqRetiredOwnerRefs` | Number of FIFO owner refs actually removed |
   | `ownerFifoHighWater` | Maximum FIFO occupancy observed per lane/QP |
   | `schedulerReadyMachinesScanned` | Ready-machine entries inspected, excluding inactive table slots |
   | `resultDescriptorPublishes` | Descriptor body plus ready version successfully published |
   | `resultDescriptorBytes` | Descriptor bytes successfully published |
   | `resultDescriptorCacheHits` | Completion references frontend-cached descriptor version |
   | `resultDescriptorCacheMisses` | Completion references a descriptor version the frontend does not yet have |
   | `resultDrainWouldBlock` | Nonblocking result drain returns not-ready for required frontier |
   | `resultDrainRecords` | Tuple-result transport records consumed by frontend drain |
   | `payloadRecordsPerWimm` | Records covered by each payload tail WIMM |
   | `payloadBytesPerWimm` | Bytes covered by each payload tail WIMM |
   | `producerToRegisteredSourceCopyBytes` | Bytes copied from producer storage into registered staging source |
   | `basebackupSourceCopiesPerGiB` | Staging-copy bytes divided by accepted basebackup payload GiB |
4. Audit residual service-side EOS synthesis. If
   [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532)
   is dead, delete it. If it is still reachable, fence it as rejected
   compatibility code and create the Stage 5 removal task before optimizing EOS.
5. Rename or document
   [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2500)
   as a destructive read-and-ack wrapper. New measured/stateful clients should
   use the peek/apply/ack path:
   [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2338)
   ->
   [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3685)
   ->
   [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3506)
   ->
   [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2452).

### Acceptance

- No-stats warmed remote c1/c4 and basebackup pass with the current accepted
  correctness band.
- Stats build reports the counters above with consistent definitions.
- Residual EOS synthesis and destructive completion wrapper semantics are no
  longer ambiguous to implementers.

## Stage 1: common transport identity and basebackup lifecycle split

Goal: define one generic transport identity used by SQL tuple results and
basebackup, while keeping object-family validation separate.

### New data structures/API

Use a common identity shape:

```c
typedef struct HomerTransportRecordIdentity
{
    uint64_t streamGeneration;
    uint64_t recordOrdinal;
} HomerTransportRecordIdentity;
```

For SQL tuple results:

```text
streamGeneration    = command/result generation
recordOrdinal       = monotonic physical byte-stream ordinal
generationSequence  = command-local DATA/EOS sequence
```

For basebackup:

```text
streamGeneration    = service-issued nonzero generation for the opened backup stream
recordOrdinal       = monotonic across the entire backup stream
semantic sequence   = basebackup metadata field, not tuple-result generationSequence
```

Do not use zero as a permanent basebackup generation. Nonzero generation lets
receivers reject stale records from a prior mapping or service lifetime.

Split validation into explicit layers:

```c
bool HomerServiceValidateTransportEnvelopeCommon(...);
bool HomerServiceValidateTupleResultRecord(...);
bool HomerServiceValidateBaseBackupRecord(...);
```

Common validation checks only protocol, header size, record byte bounds, stream
generation, record ordinal, and record kind. Tuple-result validation checks
tuple result generation, tuple batch fields, and result descriptor assumptions.
Basebackup validation must not execute tuple-result descriptor, sequence, or EOS
generation checks.

### State transitions and ownership

- SQL tuple-result generation remains command/result owned.
- Basebackup stream generation is opened once for the backup session and remains
  stable until `BACKUP_STREAM_END` or failure.
- `OBJECT_END` and `OBJECT_ERROR` may carry transport EOS when they terminate
  the stream; do not add an extra empty terminal record unless the semantic
  object model requires it.

### Acceptance

- Local blackhole and remote RDMA basebackup remain correct.
- Basebackup warmed time does not regress from the current `~4.3s-4.4s` remote
  RDMA band.
- Basebackup hot path no longer depends on SQL result STARTED/terminal lifecycle
  or tuple-view EOS repair.

## Stage 2: lane-owned send-CQ dispatcher and per-QP FIFOs

Goal: make each physical send CQ have exactly one polling owner and remove
global owner scans plus callback-absent CQE stashing.

### Prerequisites

Before coding FIFO migration, draw and record the actual command, payload,
control, and completion QP/CQ topology. List every current `ibv_poll_cq` call
site and classify it as bootstrap, recv CQ, send CQ, payload-specific send CQ,
command-specific send CQ, or scheduler lane drain. After migration there must be
exactly one polling owner for each physical send CQ.

Current send-CQ poll sites to audit include:

- bootstrap send progress at
  [`remote_execution_peer_transport_rdma.c:1270`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1270)
- blocking send wait at
  [`TupleSinkServiceWaitForSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1883)
- tagged send drain at
  [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1985)
- payload send polling at
  [`remote_execution_peer_transport_rdma.c:6743`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6743)
- command send polling at
  [`remote_execution_peer_transport_rdma.c:6928`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6928)
- peer-pump send CQ draining at
  [`remote_execution_peer_transport_rdma.c:8236`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8236)

### New data structures/API

```c
typedef struct HomerSendCqBudget
{
    uint32_t maxPollBatches;
    uint32_t maxCqes;
} HomerSendCqBudget;

typedef struct HomerSendCqDrainResult
{
    uint32_t polls;
    uint32_t emptyPolls;
    uint32_t cqes;
    uint32_t commandRefsRetired;
    uint32_t completionRefsRetired;
    uint32_t payloadRefsRetired;
    uint32_t controlRefsRetired;
    uint32_t readyReasonMask;
} HomerSendCqDrainResult;

bool HomerServiceDrainPeerSendLane(
    HomerServiceState *service,
    HomerPeerSendLane *lane,
    const HomerSendCqBudget *budget,
    HomerSendCqDrainResult *result);
```

```c
typedef struct HomerPeerSendOwnerRef
{
    uint64_t qpGeneration;
    uint64_t postOrdinal;
    uint32_t ownerIndex;
    uint32_t sourceGeneration;
} HomerPeerSendOwnerRef;

typedef struct HomerPeerSendOwnerFifo
{
    uint32_t head;
    uint32_t tail;
    uint32_t count;
    uint32_t capacity;
    HomerPeerSendOwnerRef refs[HOMER_SEND_OWNER_FIFO_CAPACITY];
} HomerPeerSendOwnerFifo;
```

### State transitions and ownership

1. Owner ref is reserved before the first WR in that logical publish sequence is
   posted.
2. FIFO order equals QP post order.
3. A signaled checkpoint retires the FIFO prefix through its `postOrdinal`.
4. FIFO full is owner-credit backpressure; no WR is posted.
5. QP reset increments `qpGeneration`.
6. Teardown releases remaining owners through reset cleanup, not normal CQ
   retirement.
7. A stale-generation CQE on a live QP is fatal. A teardown flush CQE is handled
   by the teardown state machine.

### Migration commits

- `2a`: introduce the canonical send-CQ poll owner while calling existing
  handlers underneath.
- `2b`: migrate peer-client completion publish retirement from the global table
  and scan in
  [`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3840)
  to per-QP FIFO prefix retirement.
- `2c`: migrate command-send retirement to per-QP FIFO.
- `2d`: move payload and control send-CQ handling into direct dispatcher paths.
- `2e`: delete stash and callback-absent paths for steady-state CQEs.

Preserve existing WR-ID encodings until the corresponding FIFO namespace is
working.

### Targeted tests

- FIFO wrap.
- Checkpoint retires N refs.
- Early CQE before ordinary scheduler drain.
- QP generation reset.
- Owner FIFO full.
- CQE during teardown.

### Acceptance

- No CQE stash hits occur in a diagnostic build.
- Peer-client completion publish retirement cost is proportional to owner refs
  actually retired, not the global table size.
- Remote c1/c4 and basebackup correctness remain unchanged.
- No c4 TPS regression is accepted from CQ cleanup.

## Stage 3a: nonblocking result-drain API

Goal: remove client-side terminal busy wait before changing the completion ABI.

### New data structures/API

```c
typedef enum HomerClientResultDrainStatus
{
    HOMER_RESULT_DRAIN_NOT_READY = 0,
    HOMER_RESULT_DRAIN_PROGRESS,
    HOMER_RESULT_DRAIN_FRONTIER_REACHED,
    HOMER_RESULT_DRAIN_EOS,
    HOMER_RESULT_DRAIN_ERROR
} HomerClientResultDrainStatus;

typedef struct HomerClientResultDrainBudget
{
    uint32_t maxRecords;
    uint64_t maxBytes;
} HomerClientResultDrainBudget;

bool HomerClientDrainResultSinkUntil(
    HomerClientResultSink *sink,
    uint64_t requiredGeneration,
    uint64_t requiredTail,
    uint64_t requiredEosOrdinal,
    const HomerClientResultDrainBudget *budget,
    HomerClientResultDrainStatus *status,
    char *errorMessage,
    size_t errorMessageBytes);
```

### State transitions and ownership

- `HomerClientDrainResultSinkUntil()` must never spin internally.
- If the required tail is not visible, return `HOMER_RESULT_DRAIN_NOT_READY`.
- Pgbench retains the terminal completion lease and retries later; it does not
  ACK the completion until apply succeeds.
- Running-command opportunistic drain may use small budgets, but terminal apply
  must be bounded and retryable.

### Targeted tests

- Completion arrives before payload.
- Payload arrives before completion.
- Required tail posted but source not retired.
- Partial payload post failure.

### Acceptance

- Header-mismatch retry loops are not part of normal terminal drain behavior.
- Pgbench can hold a terminal completion lease across `NOT_READY` result-drain
  returns without ACKing it.

## Stage 3b/4a: one compact completion ABI with required frontier

Goal: avoid two consecutive completion protocol migrations. Introduce one new
completion hot record that carries both the required result frontier and the
descriptor version.

### New data structures/API

Candidate hot record:

```c
typedef struct CitusRemoteExecCommandCompletionHot
{
    uint32_t protocolVersion;
    uint16_t headerBytes;
    uint16_t commandState;

    uint32_t commandKind;
    uint32_t resultFlags;

    uint64_t commandSequence;
    uint64_t resultGeneration;

    uint64_t requiredResultTail;
    uint64_t requiredEosRecordOrdinal;
    uint64_t resultDescriptorVersion;

    uint64_t insertedTupleCount;
    uint64_t processedRowCount;
    int64_t scalarInt64;

    uint32_t scalarTypeOid;
    uint8_t scalarIsNull;
    uint8_t reserved[3];

    uint32_t postCommandState;
    uint32_t postCommandStateFlags;

    uint32_t detailBytes;
    char detail[HOMER_COMPLETION_INLINE_DETAIL_BYTES];
} CitusRemoteExecCommandCompletionHot;
```

First target:

```c
_Static_assert(sizeof(CitusRemoteExecCommandCompletionHot) <= 256,
               "completion hot record must stay cache-local");
```

The inline detail budget may start at 256 bytes if separating error text is not
worth the first migration. The essential change is removing the full tuple
contract from every completion event.

### Required-frontier state machine

```text
EOS_COLLECTED
    -> PAYLOAD_TAIL_POSTED(requiredTail, eosOrdinal)
    -> TERMINAL_COMPLETION_POSTABLE
    -> TERMINAL_COMPLETION_POSTED
    -> PAYLOAD_SOURCE_RETIRED
    -> COMPLETION_SOURCE_RETIRED
```

Terminal completion becomes postable after the payload tail publication WR has
been successfully posted, not after its local send CQE retires the source. If
payload posting partially fails before the remote tail WIMM is posted, terminal
completion must not be published.

### Protocol/version changes

- Bump the completion mailbox protocol/version.
- Mixed old/new `pgbench`, `libhomer_client.a`, and
  `citus_tuple_sink_service` must fail fast.
- Keep the old `CitusRemoteExecCommandCompletion` only as an explicitly named
  compatibility layout during migration.

### Acceptance

- Completion bytes per pgbench transaction drop materially.
- Terminal completion can be posted before local payload source retirement, but
  the frontend applies/ACKs only after draining the named required frontier.
- Remote c1/c4 correctness remains stable.

## Stage 4b: versioned cold descriptor mailbox

Goal: publish queue attachment and tuple shape once per descriptor version, then
reference that version from compact completions.

### New data structures/API

Split the current descriptor:

```c
typedef struct CitusTupleSinkQueueAttachment
{
    uint32_t protocolVersion;
    uint32_t slotCount;
    uint32_t slotCapacityBytes;
    uint32_t slotReservedPrefixBytes;
    uint32_t direction;
    uint32_t descriptorFlags;
    char queueShmName[CITUS_TUPLE_SINK_SHM_NAME_BYTES];
} CitusTupleSinkQueueAttachment;

typedef struct CitusResultStreamBinding
{
    uint64_t descriptorVersion;
    uint64_t resultGeneration;
    uint64_t startByteTail;
    uint64_t firstRingRecordOrdinal;
} CitusResultStreamBinding;
```

The immutable descriptor contains queue attachment, tuple contract, descriptor
version, actual descriptor bytes, and shape fingerprint. The hot completion
contains per-command binding.

First descriptor mailbox:

```c
#define HOMER_RESULT_DESCRIPTOR_SLOTS 2

typedef struct HomerResultDescriptorSlot
{
    uint64_t bodyVersion;
    uint32_t descriptorBytes;
    uint32_t flags;
    CitusTupleSinkQueueAttachment queue;
    CitusTupleViewContract contract;
} HomerResultDescriptorSlot;

typedef struct HomerResultDescriptorMailbox
{
    uint64_t readyVersion[HOMER_RESULT_DESCRIPTOR_SLOTS];
    HomerResultDescriptorSlot slots[HOMER_RESULT_DESCRIPTOR_SLOTS];
} HomerResultDescriptorMailbox;
```

Publication order:

```text
write bodyVersion + descriptor body
write readyVersion last
completion references descriptorVersion
```

Later, the fixed-capacity tuple contract can become variable-sized using:

```text
offsetof(CitusTupleViewContract, attributes)
  + attributeCount * sizeof(CitusTupleContractAttribute)
```

### Acceptance

- Stable pgbench one-column SELECT causes descriptor cache hits after the first
  descriptor publication.
- Shape change and queue replacement publish a new descriptor version.
- Mixed-binary descriptor protocol mismatches fail fast.

## Stage 4c: stable-descriptor STARTED suppression

Goal: avoid STARTED events whose only purpose is descriptor readiness.

### Rule

```text
If the result queue and tuple-shape fingerprint match the frontend-cached
descriptor version:
    do not publish STARTED solely for descriptor readiness.

If the descriptor version changes:
    publish descriptor first;
    publish STARTED/DESCRIPTOR_READY only if the frontend may need to drain
    before terminal completion.

If no rows can fill the result ring before terminal:
    terminal may be the first event referencing the new descriptor.
```

For pgbench's stable one-column SELECT, STARTED should go to zero after the
first descriptor publication.

### Targeted tests

- Stable descriptor reuse.
- Shape change.
- Queue replacement.
- Descriptor version wrap.
- Mixed binaries.
- Stable pgbench shape emits no STARTED after cache warmup.

## Stage 4d: compact variable-length command publication

Goal: apply the same hot/cold principle to frontend command publication.

### Substeps

1. Define a compact command envelope with command sequence, command kind, flags,
   payload bytes, and protocol version.
2. Post `sizeof(envelope) + actual payloadBytes`, not the maximum command slot,
   for COMMIT, BEGIN, short SQL, and TX attach payloads.
3. Preserve source lifetime through the Stage 2 owner FIFO. The local maximum
   slot may remain for allocation simplicity, but the RDMA write length and hot
   memset/copy must use actual bytes.

### Acceptance

- `commandBytesPosted` drops for pgbench transactions.
- No command source-ring reuse occurs before send-CQ retirement.

## Stage 5a: final partial DATA+EOS publication

Goal: keep immutable result records while avoiding a separate EOS record for the
common tiny-result case.

### New API

```c
RemoteExecSqlResultFlushBatch(receiver, bool terminal);
```

### Rules

- Nonterminal flush occurs only when the batch is full.
- At shutdown, a partial current batch is finalized with EOS before first
  publication.
- Empty result publishes EOS-only sequence `1`.
- If the last batch was exactly full and already published, append compact
  EOS-only.
- Never mutate a published record.
- DATA+EOS wire representation is:

  ```text
  recordKind = DATA
  flags |= EOS
  tupleCount may be nonzero
  ```

Rename `MarkCitusTupleSinkPeerClosed()` semantics. The queue is persistent
across commands; the operation is finalizing one result generation, not closing
a peer. A clearer target API is:

```c
FinalizeCitusTupleSinkGeneration(...);
```

The current global `PEER_CLOSED` flag must not remain the per-command terminal
mechanism.

### Targeted tests

- Empty result.
- One-row result.
- Exact-full batch.
- Multi-batch result.
- Ring wrap.
- Query failure.

### Acceptance

- One-row SELECT normally produces one DATA+EOS record.
- Service-side tuple-view EOS repair is deleted or unreachable on accepted
  paths.

## Stage 5b: bounded record/byte/WR/CQ work budgets

Goal: improve batching through explicit executor budgets without introducing a
new fairness policy prematurely.

### New data structures/API

```c
typedef struct HomerProgressBudget
{
    uint32_t maxRecords;
    uint64_t maxBytes;
    uint32_t maxWrs;
    uint32_t maxCqPollBatches;
} HomerProgressBudget;

typedef struct HomerProgressUsage
{
    uint32_t records;
    uint64_t bytes;
    uint32_t wrs;
    uint32_t cqPollBatches;
    bool moreReady;
    bool budgetExhausted;
} HomerProgressUsage;
```

Every executor stops when any bound is reached and reports `moreReady`.

### Rules

- Use fixed class budgets in the interim scheduler.
- Preserve existing round-robin/fairness policy.
- Do not introduce deficit round-robin in this stage; that belongs in a later
  substage or the unified scheduler plan.

### Acceptance

- Payload records and bytes per grant increase for ready adjacent work.
- Foreground SQL includes adjacent DATA/EOS in one grant when possible.
- Basebackup throughput does not regress when foreground work is absent.

## Stage 6: producer-set ready bitmaps and typed CQ ready facts

Goal: reduce active-table scans without adding lost-wakeup races.

### Lost-wakeup protocol

Producers set bits after publishing:

```c
publish record body;
release_store(slot->readyEpoch, epoch);
release_store(mailbox->publishedEpoch, epoch);
atomic_fetch_or(readyBitmap, sessionBit, memory_order_release);
```

The service consumes bits with exchange:

```c
bits = atomic_exchange_explicit(&readyBitmap, 0, memory_order_acq_rel);
```

For each selected session:

1. Drain within budget.
2. Recheck the ring.
3. If still ready, set the bit again.

If a producer publishes while the service is draining, its `fetch_or` remains
set for the next pass. This avoids the clear-after-publish lost-wakeup race.

### Substeps

1. Add separate bitmaps for command readiness, completion readiness, and resource
   pressure.
2. Add stable session-slot generation validation.
3. Add periodic fallback full scan.
4. Add `readyBitmapFallbackDiscoveries`; it should normally remain zero.
5. Have the Stage 2 CQ dispatcher set typed ready facts directly for send-CQ
   resource relief, recv-CQ doorbells, and owner FIFO backpressure.

### Targeted tests

- Producer publishes during bitmap exchange.
- Session slot reuse.
- Fallback scan finds no missed work in steady state.
- CQ dispatcher sets resource-pressure bits without broad scans.

### Acceptance

- `schedulerReadyMachinesScanned / transaction` and
  `schedulerCandidatesBuilt / transaction` drop materially.
- No fallback discoveries in normal warmed c1/c4/basebackup runs.

## Stage 7a: basebackup semantic-header shrinkage

Goal: stop repeating object name and static object metadata in every basebackup
chunk.

### Target semantic model

```text
BACKUP_OBJECT_BEGIN:
    objectId, archiveIndex, nameBytes, totalBytes, name[]

BACKUP_OBJECT_CHUNK:
    objectId, streamOffset, payloadBytes

BACKUP_OBJECT_END:
    objectId, finalBytes

BACKUP_STREAM_END:
    transport EOS
```

Chunks should not retransmit object name or total size.

### Acceptance

- Basebackup semantic bytes per GiB drop.
- Object boundary across ring wrap remains correct.

## Stage 7b: tune existing remote range publication

Goal: measure and tune the already-existing basebackup range publication path
instead of duplicating it.

### Substeps

1. Verify that current basebackup uses the existing range descriptor path around
   [`HomerServiceAppendOutgoingBaseBackupFragmentWrite()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12421).
2. Measure:

   ```text
   recordsPerWimm
   bytesPerWimm
   WRsPerWimm
   bytesPerSignaledCqe
   rangeBuildStopsByReason
   ```

3. Tune:

   ```text
   max descriptors per range
   max bytes per range
   signaled interval
   pressure-triggered checkpoint
   ```

4. Do not restore producer-side delayed `publishedTail`; current code publishes
   each producer record immediately to preserve pipeline overlap.

### Acceptance

- `bytesPerWimm` and `bytesPerSignaledCqe` increase.
- Remote RDMA basebackup warmed wall time improves or stays flat.

## Stage 7c: direct-register producer byte rings

Goal: remove producer-to-service registered staging copies for stable producer
byte rings.

### Source-lifetime protocol

1. Register the complete producer byte-ring mapping at stream open.
2. Service posts directly from producer storage.
3. Around wrap, use two SGEs or two WRs without copying.
4. Producer `consumedHead` advances only after a send checkpoint retires all
   covered source bytes.
5. MR deregistration occurs only after QP drain/reset.
6. Registration failure falls back to the existing registered staging path.
7. Partial-post failure follows the same reset-required rules as other multi-WR
   publication.

### Acceptance

- `producerToRegisteredSourceCopyBytes` approaches zero for accepted direct-MR
  basebackup paths.
- `basebackupSourceCopiesPerGiB` drops.
- Direct-MR teardown and QP reset do not release a source range still readable
  by the NIC.

## Stage 7d: optional replication QP/CQ isolation

Goal: isolate bulk basebackup from foreground latency only when measurement
justifies the extra transport topology.

### Trigger

Add a dedicated replication QP/CQ only if, after Stage 7b budget tuning,
concurrent basebackup increases foreground pgbench p99 by more than `10%` or CQ
/ payload counters show bulk head-of-line blocking.

### Acceptance

- Foreground pgbench with background remote RDMA basebackup improves p95/p99
  without regressing standalone basebackup materially.

## Validation Matrix

Every stage must run at least:

- remote RDMA pgbench c1 correctness smoke and warmed performance repeats.
- remote RDMA pgbench c4 correctness and warmed performance repeats.
- local Homer blackhole basebackup.
- remote RDMA basebackup warmup plus warmed repeats.

When a stage changes scheduler fairness, CQ dispatch, payload range building, or
bulk transport behavior, also run foreground pgbench with background remote RDMA
basebackup and compare p95/p99 plus basebackup wall time against the previous
accepted stage.

Use no-stats builds for performance acceptance. Stats builds are diagnostic
only. A run after timeout, interruption, manual cleanup, stale process discovery,
or mismatched binary hash is not an acceptance run.

## Decisions that should not be repeated

- Do not reintroduce dual delivery plus deduplication for frontend command
  completions. The accepted direction is one explicit peek/apply/ack owner.
- Do not rely on receiver-service token demux to publish host-client visibility
  when the host client already polls its mailbox memory.
- Do not treat `publishedEpoch` alone as a completion-body visibility proof.
  Slot-local ready epoch and stable reads remain required.
- Do not use client-side tolerance for tuple-result sequence mismatches as a
  performance feature. Generation and immutable EOS fixed the ownership bug; the
  optimized path should make mismatches impossible.
- Do not force basebackup into SQL-result STARTED/terminal lifecycle just
  because it shares the byte-ring transport substrate.
- Do not make descriptor split and required-frontier completion two separate
  completion ABI migrations. Stage 3b/4a is the single deliberate completion
  protocol migration.

## Open questions

- Whether the same-QP DATA/EOS/completion-ready sequence is worthwhile as a
  foreground-only fast path after the required-frontier design is correct.
- Whether payload WR-IDs should keep semantic frontier encoding permanently or
  move to owner-array indexes once the lane-owned dispatcher is in place.
- Whether descriptor publication should be service-owned, backend-owned, or
  frontend-library-owned when moving toward true zero-copy result production.
- Whether direct registration of producer byte rings is enough for basebackup or
  whether Stage 7d QP/CQ isolation is necessary for foreground p99.
