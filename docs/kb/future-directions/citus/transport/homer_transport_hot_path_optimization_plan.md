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
  [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2468)
  are the accepted measured frontend completion ownership path.
- [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2526)
  is still a destructive compatibility wrapper: it peeks, copies, validates, and
  ACKs internally. Stateful measured clients should not use it.
- [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904)
  still has terminal-drain retry/spin behavior; the target is a bounded
  nonblocking drain API.
- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13139)
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
4. **Descriptor storage is not the hot completion mailbox.** The completion
   mailbox remains the multi-slot hot event ring polled by the host client. The
   Stage 4 descriptor structure is a cold versioned side table for large queue
   attachment and tuple-shape metadata. Do not turn descriptor publication into
   a second hot FIFO/event mailbox. A two-slot descriptor side table is only
   valid with explicit reuse credit proving that no completion can still
   reference the overwritten descriptor version; the first implementation uses
   four append-only descriptor slots to avoid that credit path.

## June 20 review reconciliation

The latest external review is accepted with these concrete plan decisions:

- **Compact completion sizing**: the hot completion record uses
  `HOMER_COMPLETION_INLINE_DETAIL_BYTES = 128` and keeps `detail` byte-counted.
  A larger `detail[256]` would violate the `sizeof(...) <= 256` target once the
  fixed header fields are included. The target label is "bounded compact
  completion", not "single cache-line completion".
- **Stage 3a is independently implementable**: its drain target has
  `hasRequiredFrontier = false` until the compact completion ABI exists. The
  exact `requiredTail` and `requiredEosOrdinal` become authoritative only after
  the Stage 3b/4a protocol bundle lands.
- **Stage 3b and Stage 4a are one deployable ABI bundle**: compact completions
  that reference descriptor versions must not be installed without the versioned
  descriptor table, descriptor publisher, and client stable-read/cache path.
- **Descriptor publication ownership is settled for the first implementation**:
  the frontend-side Homer service publishes the frontend-visible descriptor side
  table. The backend produces tuple shape/result metadata, and the client only
  caches and validates descriptor versions.
- **STARTED suppression depends on pre-armed binding, not result-size
  prediction**: terminal-only delivery is safe only after the frontend already
  has the descriptor, queue attachment, generation, and start tail needed to
  drain payload before terminal completion.
- **QP send ownership is physical and typed**: the long-term owner FIFO is
  QP-global, carries an owner kind, and uses partial-post reset-required rules.
  Namespace-specific FIFOs from Stage 2b/2c are stepping stones, not the final
  ownership model.
- **Performance acceptance is tolerance-based**: c1, c4, p99, and basebackup
  comparisons use warmed medians and tolerances instead of accepting or
  rejecting a stage based on one outlier run.
- **Nonblocking drain statuses are settled**: Stage 3a/3b uses the
  `NOT_READY`, `PROGRESS`, `COMPLETE`, `VISIBILITY_PENDING`, and `ERROR`
  contract. `VISIBILITY_PENDING` is a bounded retry condition, not an internal
  spin; persistent malformed records escalate outside the hot helper after
  bounded event-loop retries.
- **Failed-query result termination is explicit**: Stage 5a must publish an
  immutable `ERROR+EOS` frontier, either as sequence `1` when no data was
  published or as `N+1` after already-published data. The terminal failure
  completion names that frontier, and the frontend drains/discards through it
  before applying the command failure.
- **Ready bitmaps must not create a new cache-line bottleneck**: Stage 6 uses
  cache-line isolated bitmap words and records already-set/exchange/fallback
  counters so the active-table scan replacement is measurable.
- **Direct-MR wrap handling distinguishes source and destination wrap**: local
  source-ring wrap may use SGEs, remote destination-ring wrap requires multiple
  WRs, and the tail WIMM remains last.

### June 20 follow-up review clarification

The follow-up review is accepted as a tightening of the same plan, not a new
direction. In particular:

- The phrase "two-slot mailbox" must not be used for the current client
  completion mailbox. The hot host-client completion mailbox is already the
  multi-slot SPSC event ring in
  [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:772),
  with
  [`CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:757)
  currently set to eight.
- If someone says "two-slot mailbox" in the Stage 4 descriptor discussion, the
  only coherent interpretation is a double-buffered cold descriptor side table.
  That is rejected for the first implementation because descriptor-slot reuse
  needs explicit proof that no completion can still reference the overwritten
  descriptor version.
- Two descriptor slots would be sufficient only under a stricter reuse model:
  one descriptor version may still be referenced while the next descriptor body
  is being prepared, and the publisher receives explicit credit proving that the
  older physical slot is no longer referenced before wrapping. That could be
  true for a single-command-in-flight prototype or for a future
  `consumedDescriptorVersion` protocol, but it is extra ownership machinery and
  should not be smuggled into the first descriptor split.
- The first implementation therefore keeps the current
  [`HOMER_RESULT_DESCRIPTOR_SLOTS`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:483)
  value of four and treats descriptor slots as append-only until session
  teardown. If descriptor churn ever exceeds four versions in one session, fail
  explicitly instead of adding an unplanned remote descriptor-credit protocol or
  silently falling back to descriptor-heavy completions.
- The middle-stage ordering remains:

  ```text
  3a  bounded nonblocking drain with optional target
  3b  compact completion + required frontier
  4a  versioned descriptor side table, publisher, and client stable read/cache
       ---------------------------------------------------------------
       one protocol/deployment acceptance unit for row-producing completions

  4b  stable-binding pre-arm
  4c  STARTED suppression guarded by pre-arm
  4d  variable-length command publication, only after the live completion
      ownership path is stable enough to avoid conflating failures
  ```

  The Stage 3b/4a deployment bundle has these concrete checkpoints:

  1. Define the compact hot completion layout, the four-slot descriptor side
     table, the stable-read helpers, and the optional drain target fields under
     one protocol version.
  2. Embed the descriptor side table in the existing frontend completion mailbox
     shared-memory object. Do not create a second hot event mailbox.
  3. Add frontend-service-owned descriptor publication: cache the current
     queue attachment and tuple contract per session, publish a new descriptor
     version only on shape or queue replacement, and fail explicitly if the
     four append-only descriptor slots are exhausted.
  4. Teach the client stable-read path to reconstruct the legacy completion
     view from the compact hot completion plus the referenced descriptor
     version, and to return descriptor-not-ready as a bounded retry condition.
  5. Fill `requiredResultTail` and `requiredEosRecordOrdinal` for terminal
     row-result completions from the frontend-visible byte-ring frontier after
     the payload tail publication WR has been successfully posted.
  6. Switch local and peer frontend completion publication to the compact record
     only after descriptor publication and client stable reads are in place.
  7. Validate mixed-binary failure, descriptor cache hit/miss counters,
     descriptor exhaustion, remote c1/c4 correctness, p99, and local/remote
     basebackup non-regression before accepting the protocol version.

This clarification is especially important for Stage 4d: compact command
publication should not be used to mask or debug descriptor/completion lifecycle
problems. It is an independent byte-reduction optimization and should be
validated only against a stable live completion publication path.

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
   [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2526)
   as a destructive read-and-ack wrapper. New measured/stateful clients should
   use the peek/apply/ack path:
   [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2338)
   ->
   [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3685)
   ->
   [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3506)
   ->
   [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2468).

### Acceptance

- No-stats warmed remote c1/c4 and basebackup pass with the current accepted
  correctness band.
- Stats build reports the counters above with consistent definitions.
- Residual EOS synthesis and destructive completion wrapper semantics are no
  longer ambiguous to implementers.

### Implementation progress and acceptance - June 19, 2026

- Stage 0 instrumentation landed in the Citus/Homer source tree for the
  counters that correspond to current code paths:
  - [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2526)
    and its public declaration now explicitly document the destructive
    copy-and-ACK behavior. Measured stateful clients should keep using
    [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2338)
    and
    [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2468)
    instead of the wrapper.
  - `HOMER_CLIENT_COMPLETION_STATS` adds a frontend diagnostic aggregate for
    `completion_events_acked`, incremented in
    [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2468)
    and printed during
    [`HomerClientCloseSession()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1181).
  - `HOMER_SERVICE_CLIENT_SQL_STATS` now logs Stage-0 aliases
    `completion_events_published`, `completion_bytes_posted`, and
    `command_bytes_posted`. The publish counters are updated only after
    successful peer-client completion READY-WIMM publication in
    [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13139);
    command bytes are updated after successful remote client-SQL command WR
    posting in
    [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14987).
  - Terminal completion dependency waits use a per-session diagnostic latch so
    `terminal_completion_payload_waits` and
    `terminal_completion_result_send_cq_waits` count transitions into blocked
    state, not scheduler retry iterations. The latch is tied to the existing
    deferred peer-client completion state around
    [`TupleSinkServiceMarkDeferredPeerClientCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3493).
  - `HOMER_SERVICE_PAYLOAD_STATS` now logs `payload_records_per_wimm`,
    `payload_bytes_per_wimm`, and `producer_to_registered_source_copy_bytes`.
    The first two are updated at successful payload visibility WIMM posts in
    [`HomerServicePumpOutgoingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19777)
    and
    [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20506).
    `producer_to_registered_source_copy_bytes` is currently expected to remain
    zero for the direct registered-source paths; future staging copies must
    increment it at the copy site.
- Residual tuple-view EOS synthesis is not dead. It is still selected through
  [`HomerServiceAppendTupleViewEosForStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19732),
  so Stage 0 fenced the helper comment instead of deleting it. Stage 5 still
  owns removal by moving to immutable backend-produced EOS records.
- No-stats acceptance workloads were run by the verification subagent after a
  clean build/install/sync on both `farnet1` and `farnet0`; raw logs are under
  `/tmp/homer_stage0_no_stats_acceptance_20260619_191849`.
  - Remote RDMA pgbench c1 processed all transactions with rc `0`. Warmed
    repeats were `4166.214459`, `3724.086179`, and `3739.058580 TPS`; p99 was
    `0.259 ms`, `0.291 ms`, and `0.290 ms`.
  - Remote RDMA pgbench c4 processed all transactions with rc `0`. Warmed
    repeats were `10441.781326` and `10324.384415 TPS`; p99 was `0.591 ms` and
    `0.594 ms`.
  - Local Homer blackhole basebackup completed with rc `0` in `4.18s` and
    `4.17s`.
  - Remote RDMA blackhole basebackup completed with rc `0`; after warmup
    `5.74s`, warmed repeats were `4.12s`, `4.30s`, and `4.31s`.
  - Verification caveats: the first cleanup attempt used an over-broad
    `pkill -f` and killed its own shell, then the runner reran cleanup safely;
    `pkill -x` missed the truncated service process name on `farnet0`, so the
    dbcomm-owned service was killed by argv path. Final preflight was clean
    except for expected PostgreSQL and Homer service processes.
- Stats-counter acceptance was run by the verification subagent with a
  stats-enabled build/install/sync, short correctness workloads, and a final
  no-stats rebuild/install/sync restore; raw logs are under
  `/tmp/homer_stage0_stats_acceptance_20260619_192511`.
  - Remote RDMA pgbench c1 `-t 2000` and remote RDMA basebackup blackhole both
    returned rc `0`.
  - Required field evidence is in
    `/tmp/homer_stage0_stats_acceptance_20260619_192511/31_required_field_evidence.txt`.
    It shows frontend `completion_events_acked=16003`, backend-node
    `completion_events_published=16003`,
    `completion_bytes_posted=541029424`,
    `terminal_completion_payload_waits=1994`,
    `terminal_completion_result_send_cq_waits=0`, frontend-node
    `command_bytes_posted=700374048`, pgbench payload
    `payload_records_per_wimm=4000` and `payload_bytes_per_wimm=416000`, and
    basebackup payload `payload_records_per_wimm=6578` and
    `payload_bytes_per_wimm=23294957061`. The direct registered-source paths
    reported `producer_to_registered_source_copy_bytes=0`, as expected.
  - Service logs contained NUL bytes, so the runner used `rg -a` for counter
    extraction.
  - After diagnostics, the no-stats runtime was restored. Final hashes matched
    on both hosts for `postgres`, `pgbench`, `pg_basebackup`,
    `citus_tuple_sink_service`, `libhomer_client.a`, and `citus.so`; string
    probes confirmed stats strings were absent from the restored runtime.
- Stage 0 is accepted for current-code diagnostics and no-stats workload
  correctness/performance. Counters for later mechanisms such as descriptor
  publication/cache hits and nonblocking result-drain statuses remain planned
  under their corresponding future stages and are not expected to be nonzero
  before those mechanisms exist.

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

### Protocol/version changes

Stage 1 is a transport-header ABI migration, not an additive fourth identity
layer. The implementation choice is:

```text
resultGeneration  -> streamGeneration
ringRecordOrdinal -> recordOrdinal
sinkSequence      -> removed from the common transport envelope, or renamed to
                     a reserved field that producers set to zero and validators
                     ignore
generationSequence remains tuple-result-only command-local DATA/EOS sequence
```

The implementation must bump the tuple-sink transport protocol version, return
the service-issued nonzero basebackup stream generation at stream open, and
propagate that generation in peer-open metadata. Current basebackup producer
behavior writes `resultGeneration = 0` and duplicates sequence into multiple
fields; Stage 1 should eliminate that duplication rather than adding another
identity representation.

Basebackup validation after the migration uses:

```text
transport.streamGeneration == opened backup stream generation
transport.recordOrdinal + 1 == basebackupHeader.streamSequence
```

It must not check tuple-result `generationSequence`, descriptor state, STARTED
state, or tuple-view EOS generation.

### State transitions and ownership

- SQL tuple-result generation remains command/result owned.
- Basebackup stream generation is opened once for the backup session and remains
  stable until `BACKUP_STREAM_END` or failure.
- `OBJECT_END` and `OBJECT_ERROR` may carry transport EOS when they terminate
  the stream; do not add an extra empty terminal record unless the semantic
  object model requires it.
- Distinguish basebackup semantic endings:
  - `BACKUP_OBJECT_END`: ends one archive or manifest object.
  - `BACKUP_STREAM_END`: ends the whole backup and carries transport EOS.
  - `BACKUP_STREAM_ERROR`: terminal failure and carries transport EOS.

### Acceptance

- Local blackhole and remote RDMA basebackup remain correct.
- Basebackup warmed time does not regress from the current `~4.3s-4.4s` remote
  RDMA band.
- Basebackup hot path no longer depends on SQL result STARTED/terminal lifecycle
  or tuple-view EOS repair.

### Implementation progress and acceptance - June 19, 2026

- Stage 1 transport-header ABI migration is implemented in the Citus/Homer
  source tree and cross-machine workload accepted.
  [`CITUS_TUPLE_SINK_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:21)
  is now `10`.
- [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:386)
  now uses `streamGeneration` and `recordOrdinal`. `generationSequence` remains
  tuple-result-only until the Stage 5 EOS cleanup, and `reservedSequence` is
  zero on new basebackup records.
- The queue descriptor still uses the historical field name
  `resultGeneration`. This is intentional until the Stage 4 descriptor split:
  for SQL tuple results it remains the result generation, while basebackup
  stream opens use it to return the service-issued nonzero stream generation
  [tuple_sink_protocol.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:233).
- Basebackup local producer open now rejects zero stream generation, and
  producer records use `stream->queueDescriptor.resultGeneration` as the common
  transport `streamGeneration` while keeping the semantic stream sequence in
  `CitusRemoteBaseBackupMessageHeader.streamSequence`
  [homer_client.c](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1500)
  [homer_client.c](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1766).
- The service returns `streamEntry->stream.serviceStreamId` as the basebackup
  stream generation in the local send queue descriptor
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23009).
  Remote basebackup receive validates the sender's stream generation using the
  peer stream id recorded during peer open, because unfragmented byte-ring
  records are forwarded without rewriting their common transport header
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19151).
- Common payload-header readiness now checks protocol, expected stream
  generation, `recordOrdinal + 1`, header size, and object-family-specific
  fields. Tuple-result validation checks `generationSequence`; basebackup
  validation rejects nonzero tuple-only sequence fields and validates semantic
  ordering through the basebackup payload header
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21830).
- Tuple-result producer/consumer code no longer uses the demoted common-header
  duplicate sequence. The backend-local tuple sink fills `streamGeneration`,
  `recordOrdinal`, and tuple-only `generationSequence`
  [tuple_sink_service.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:278),
  and receive-side tuple validation relies on `generationSequence` plus the
  tuple batch header
  [tuple_sink_service.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1938).
- Caveat: for the current forced-fragmentation debug path, `recordOrdinal` is
  still the basebackup object ordinal on all fragments of one object, not a
  unique physical fragment ordinal. This preserves the existing reassembly
  contract and keeps unfragmented hot-path records direct-copyable. A true
  physical fragment ordinal would require one more receiver-side reassembly
  frontier and is not part of this Stage 1 implementation.
- Build evidence so far: no-stats `make -B -j8 service-bin client-bin
  CPPFLAGS='-D_GNU_SOURCE'` passed after formatting; stats-enabled `make -B -j8
  service-bin client-bin CPPFLAGS='-D_GNU_SOURCE
  -DHOMER_SERVICE_PAYLOAD_STATS=1 -DHOMER_SERVICE_CLIENT_SQL_STATS=1
  -DHOMER_CLIENT_COMPLETION_STATS=1'` also passed after formatting.
- No-stats build/install/sync and workload validation were run by the dedicated
  validation subagent; raw logs are under
  `/tmp/homer_stage1_validation_20260619_194551`, with the summarized verdict in
  `/tmp/homer_stage1_validation_20260619_194551/17_acceptance_verdict.txt`.
  - Build/install, rsync to `farnet0`, binary hash matching, installed protocol
    version `10`, and absence of stats strings all passed.
  - Remote RDMA pgbench c1 returned rc `0` with zero failures. Warmed repeats
    were `4159.162844`, `3727.666386`, and `3724.548762 TPS`; p95 was
    `0.250`, `0.280`, and `0.278 ms`; p99 was `0.261`, `0.296`, and
    `0.293 ms`.
  - Remote RDMA pgbench c4 returned rc `0` with zero failures. Warmed repeats
    were `10442.449182`, `10292.221911`, and `10237.237748 TPS`; p95 was
    `0.536`, `0.536`, and `0.539 ms`; p99 was `0.611`, `0.593`, and
    `0.607 ms`.
  - Local Homer blackhole basebackup completed with rc `0` in `3.96s` and
    `3.88s`.
  - Remote RDMA blackhole basebackup completed with rc `0`; after warmup
    `6.17s`, warmed repeats were `4.21s`, `4.29s`, and `4.19s`.
  - Service-log scan reported no obvious error/mismatch/failure strings. Final
    process preflight showed only expected PostgreSQL and Homer services.
- Stage 1 is accepted for correctness and performance. The only caveat is that
  remote c4 warmed repeats 2 and 3 are slightly below the Stage 0
  `10.3k-10.4k` band, but still within a small tolerance and with no correctness
  failures or service-log errors.

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

### Current topology audit - June 19, 2026

- Each `TupleSinkServicePeerConnectionState` owns one `sendCompletionQueue` and
  one `recvCompletionQueue`
  [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:446).
  [`TupleSinkServiceInitConnectionResources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2387)
  creates both CQs, then assigns them as `qpInitAttr.send_cq` and
  `qpInitAttr.recv_cq`
  [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2413).
  Therefore the physical ownership unit for Stage 2 is one lane/QP send CQ, not
  one semantic command, payload, completion, or control source.
- Current WR-ID namespaces are still tag based:
  payload uses the top bit and encodes payload completion kind, sink id, and
  frontier; command uses the command tag plus outstanding index; control uses
  control tag plus response flag, op/slot index, and generation; peer-client
  completion publish uses its own tag plus outstanding index
  [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:76).
- Current send-CQ consumers are still mixed:
  - bootstrap setup polls one CQE at a time through
    [`TupleSinkServicePollCompletionOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1270);
  - cold blocking waits use
    [`TupleSinkServiceWaitForSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1927);
  - the generic tagged drain polls in
    [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2053);
  - legacy semantic payload and command pollers still poll the same
    `sendCompletionQueue`
    [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6851)
    [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7037);
  - peer-control helpers still call the legacy tagged drain as hidden progress
    while publishing or polling peer-control ops
    [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7764)
    [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7994);
  - peer-pump `SEND_CQ` progress also calls the legacy tagged drain
    [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8347).
- The current stashes are still live for payload, command, and peer-client
  completion-publish CQEs in `TupleSinkServicePeerConnectionState`
  [remote_execution_peer_transport_rdma.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:504).
  Stashing remains the compatibility mechanism for callback-absent or
  semantic-owner-specific drains. Stage 2 is not complete until steady-state
  scheduler drains no longer need these stashes.

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
    uint32_t stashCqes;
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
    uint64_t postOrdinal;
    uint32_t ownerIndex;
    uint32_t sourceGeneration;
    uint16_t ownerKind;
    uint16_t reserved;
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
8. A QP-global FIFO is preferred over one FIFO per semantic namespace because a
   signaled checkpoint proves completion of all prior WRs on that QP. The
   `ownerKind` dispatches retired refs to `COMMAND_SOURCE`,
   `CLIENT_COMPLETION_SOURCE`, `PAYLOAD_SOURCE`, or `CONTROL_SOURCE`.
9. `qpGeneration` may live in the lane/FIFO instead of every ref, but the
   checkpoint WR-ID must carry enough generation information to reject stale CQEs.
10. If an owner ref is reserved and zero WRs are posted, roll back the FIFO tail.
    If one or more WRs are posted and the sequence later fails, leave the owner
    teardown-owned and mark the QP reset-required.
11. FIFO capacity must cover the maximum logical source owners that can be
    outstanding under `max_send_wr` and all source-ring geometries.

### Migration commits

- `2a`: introduce the canonical send-CQ poll owner while calling existing
  handlers underneath.
- `2b`: migrate peer-client completion publish retirement from the global table
  and scan in
  [`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4047)
  to per-QP FIFO prefix retirement.
- `2c`: migrate command-send retirement to per-QP FIFO.
- `2d`: move payload and control send-CQ handling into direct dispatcher paths.
- `2e`: delete stash and callback-absent paths for steady-state CQEs.

Preserve existing WR-ID encodings until the corresponding FIFO namespace is
working.

### Implementation progress - Stage 2a

- Stage 2a now adds the canonical send-CQ accounting surface
  `HomerSendCqBudget` and `HomerSendCqDrainResult`
  [remote_execution_peer_transport_rdma.h](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:173).
  `stashCqes` is intentionally explicit: it records CQEs that were consumed
  from the physical CQ but deferred into the legacy stash instead of retired
  through the eventual owner FIFO.
- [`TupleSinkServiceDrainPeerSendCqRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2169)
  is the new canonical Stage-2 send-CQ drain entry point. It accepts
  `HomerSendCqBudget`, reports polls, empty polls, CQEs, typed retired refs, and
  stash CQEs, and currently delegates to the existing tagged-CQE router. This is
  scaffolding, not ownership migration.
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1742)
  now increments typed result fields only when a callback/direct owner actually
  retires that namespace. If a CQE falls back to legacy stash, it increments
  `stashCqes` instead of pretending owner retirement occurred.
- [`HomerServiceDrainOnePeerSendCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10470)
  now uses `TupleSinkServiceDrainPeerSendCqRdma()` with a one-poll-batch budget.
  The scheduler grant policy is unchanged; this only routes the existing
  scheduler-owned CQ drain through the canonical result surface.
- Caveat: Stage 2a does not yet remove `TupleSinkServicePollPeerCommandSendCompletionRdma()`,
  `TupleSinkServicePollPeerPayloadSendCompletionRdma()`, peer-control hidden
  tagged drains, or the stash arrays. Those are the actual ownership migrations
  for `2b` through `2e`.

### Implementation progress - Stage 2b

- Stage 2b migrates peer-client completion-publish retirement from a global
  outstanding-table scan to a per-connection FIFO. The existing outstanding
  table remains the owner metadata store and WR-ID namespace for this slice, but
  steady-state checkpoint retirement no longer scans all
  `HOMER_SERVICE_PEER_CLIENT_COMPLETION_PUBLISH_OUTSTANDING_WRITES` entries.
- `TupleSinkServicePeerClientCompletionPublishLaneFifo` stores outstanding table
  indexes in post order per `TupleSinkServicePeerConnectionHandle`
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1026).
  This is intentionally namespace-specific for peer-client completion publish;
  the generic `HomerPeerSendOwnerFifo` still belongs to the later shared owner
  migration.
- `TupleSinkServiceReservePeerClientCompletionPublishCompletion()` now enqueues
  the reserved outstanding index into that lane FIFO before marking the entry
  active
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3932).
  If body posting fails before the ready-WIMM, the existing clear path removes
  the FIFO entry while clearing the outstanding table entry
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3962).
- `TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()` now
  finds the checkpoint entry's connection FIFO and pops the FIFO prefix through
  the checkpoint index
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4047).
  A missing FIFO for an active checkpoint is treated as a fatal ownership
  inconsistency, not as a fallback to the global scan.
- Caveat: this removes the global scan for peer-client completion publish only.
  Command sends still use their existing global table/prefix retirement, and
  payload/control CQE ownership still depends on the legacy tagged drain plus
  stash compatibility. Those remain `2c` and `2d`.

### Implementation progress - Stage 2c

- Stage 2c migrates remote client-SQL command-send retirement from a global
  outstanding-table scan to a per-connection FIFO. The existing outstanding
  table remains the WR-ID metadata store for command sends, but signaled
  checkpoint CQE retirement no longer scans all
  `HOMER_SERVICE_CLIENT_SQL_COMMAND_OUTSTANDING_WRITES` entries.
- `TupleSinkServiceClientSqlCommandWriteLaneFifo` stores command outstanding
  indexes in post order per `TupleSinkServicePeerConnectionHandle`
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1013).
  This mirrors the Stage 2b completion-publish FIFO, but it is still
  namespace-specific. The later shared `HomerPeerSendOwnerFifo` migration will
  unify command, completion, payload, and control owners under one QP-global
  owner FIFO.
- `TupleSinkServiceReserveClientSqlCommandWriteCompletion()` now fills the
  command outstanding-table entry, enqueues its index into the lane FIFO, and
  only then marks the entry active and updates active/signaled counts
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4295).
  If FIFO enqueue fails before any WR is posted, the partially filled entry is
  cleared and no source ownership is published.
- `TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()` now finds
  the checkpoint entry's connection FIFO, verifies that the checkpoint index is
  present, and pops only the FIFO prefix through that checkpoint
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4505).
  A missing FIFO or missing checkpoint is treated as a fatal ownership
  inconsistency, not as a fallback to the old global scan.
- `TupleSinkServiceClearClientSqlCommandWriteCompletionIndex()` removes active
  command outstanding indexes from the lane FIFO before clearing their table
  entry
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4583).
  This keeps zero-WR post failures and session cleanup from leaving stale FIFO
  references.
- Initial Stage 2c validation exposed a remaining no-callback send-CQ owner:
  aggregate peer-control progress and the explicit peer-control `SEND_CQ`
  action could still call the RDMA-layer peer pump, which invokes
  `TupleSinkServiceDrainTaggedSendCompletionsInternal()` without command or
  peer-client-completion callbacks. On a shared command/control QP that path
  consumed command CQEs into the legacy command stash until it filled with
  `command send-completion stash is full`. The fix is to exclude send-CQ
  retirement from aggregate peer-control phase masks
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11253)
  and to route `HOMER_PROGRESS_ACTION_DRAIN_PEER_CONTROL_SEND_CQ` through the
  typed Stage-2 send-CQ drain before falling back to the legacy peer-control
  send-CQ pump only when there is no command/payload ownership to retire
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:28198).
  This is a concrete ownership correction: peer-control progress must not drain
  a shared physical send CQ without the callbacks needed for all WR-ID
  namespaces that can appear on that CQ.
- Caveat: Stage 2c preserves the pre-existing two-WR command fallback failure
  behavior. If the command-record WR succeeds but the ready-word WR fails, the
  current code still clears local ownership through the generic failure path
  rather than marking the QP reset-required. The Stage 2 partial-post invariant
  still needs to be enforced when payload/control/direct dispatcher cleanup is
  completed; this FIFO slice does not introduce that issue, but it also does not
  solve it.
- Build evidence so far: no-stats Citus/Homer
  `make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'` passed after
  `git clang-format`, and `git diff --check` passed in `citus-dbcomm`.
  Full cross-machine workload validation is being rerun after the no-callback
  peer-control send-CQ correction.

### Rejected Stage 2c intermediate state

- Validation artifact
  `/tmp/homer_stage2c_validation_20260619_202228/14_acceptance_verdict.txt`
  rejected the first Stage 2c implementation. Build/install/sync succeeded, but
  remote RDMA pgbench c1 stalled before producing TPS. The farnet0 service log
  showed
  `resetting outgoing peer transport ... send-CQ drain failure detail=command send-completion stash is full`
  followed by repeated `remote client SQL command path is not ready session=1`.
  This rejected the idea that command FIFO migration alone was sufficient while
  peer-control `SEND_CQ` still had a no-callback drain path on the same physical
  CQ.

### Validation - Stage 2c

- Dedicated validation agent accepted Stage 2c after the no-callback
  peer-control send-CQ correction. Raw artifacts are under
  `/tmp/homer_stage2c_validation_rerun_20260619_203426`; the verdict is
  `/tmp/homer_stage2c_validation_rerun_20260619_203426/09_acceptance_verdict.txt`.
- Build/deploy passed as `dbcomm`: no-stats Citus/Homer `service-bin
  client-bin`, Citus/Homer install, targeted Postgres build/install, installed
  ownership, no stats-marker strings, protocol version `10U`, and farnet1/farnet0
  hash match for `postgres`, `pgbench`, `pg_basebackup`,
  `citus_tuple_sink_service`, `libhomer_client.a`, and `citus.so`.
- Remote RDMA pgbench c1 from `farnet0` to `farnet1` completed all runs with
  zero failed transactions. Warmed TPS was `4094.682993`, `3723.058783`, and
  `3720.202803`; p99 was `0.275`, `0.297`, and `0.294 ms`.
- Remote RDMA pgbench c4 completed all runs with zero failed transactions.
  Warmed TPS was `10388.450121`, `10384.377742`, and `10292.921095`; p99 was
  `0.596`, `0.593`, and `0.600 ms`. This is within tolerance versus Stage 2b
  and shows no c4 regression from command-send FIFO migration.
- Local Homer blackhole basebackup completed with rc `0`; after warmup `4.20s`,
  warmed runs were `4.16s`, `4.14s`, and `3.98s`.
- Remote RDMA blackhole basebackup completed with rc `0`; after warmup `6.04s`,
  warmed runs were `4.13s`, `4.19s`, and `4.13s`.
- Focused log scan found no Stage-2c ownership or previous-stall errors in the
  fresh service/workload logs: no `command send-completion stash is full`, no
  `send-CQ drain failure`, no `remote client SQL command path is not ready`, no
  `command write checkpoint`, no `command write lane FIFO`, no
  `command write clear could not remove lane FIFO`, and no
  `stale command write checkpoint`.
- Validation caveats:
  - the tree was dirty by design, so this validates the current working tree,
    not a committed SHA;
  - pgbench/basebackup per-run timeouts were used only to bound recurrence of
    the prior stall, and no timeout fired;
  - one Postgres `FATAL` in the archived tail came from the earlier rejected
    validation run before the accepted rerun setup began.

### Implementation progress - Stage 2d

- Stage 2d moves scheduler-visible payload and ACK send-CQ retirement away from
  payload-only CQ pollers and through the typed Stage-2 drain dispatcher.
- `HomerServiceExecutePayloadProgressPlan()` now treats
  `HOMER_PAYLOAD_PROGRESS_ACTION_SEND_CQ` as physical lane ownership: it first
  calls `HomerServiceDrainTypedPeerSendCq()` and clears the payload-local
  `SEND_CQ` bit before invoking
  `HomerServicePumpPayloadStream()`
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10450).
  The dense aggregate payload scan likewise excludes `SEND_CQ` from its
  per-stream action mask and drains the typed CQ path once before scanning active
  payload streams
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10496).
- `HomerServiceDrainTypedPeerSendCq()` is the local service helper that builds a
  CQ-drain plan with
  `HomerServiceBuildCqDrainProgressPlan()` and executes it through
  `HomerServiceExecuteCqDrainProgressPlan()`
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11292).
  This keeps command-send, peer-client-completion publish, payload-data, ACK,
  and control WR-ID namespaces visible to one drain path instead of requiring
  payload-specific callbacks.
- The outgoing byte-ring sender and older slot-ring sender now use
  `HomerServiceDrainTypedPeerSendCq()` when they opportunistically retire local
  send completions before source reuse or wait under local source-window
  pressure
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20327)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20410)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21060)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21140).
  The wait predicates are stream-local frontier movement, not merely "some CQE
  drained", so a command CQE or another stream's CQE cannot release the current
  stream's source storage.
- Receiver consumed-head ACK retirement now also uses the typed drain through
  `HomerServiceDrainReceiverHeadAckCompletions()`, which is threaded through
  credit publication, incoming byte-ring payload, incoming slot-ring payload,
  and close/reclaim paths
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21761)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21888)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22158)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22527)
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22936).
- The old service-level selected payload send-CQ helper
  `HomerServiceDrainPayloadSendCqForStream()` was deleted because it became dead
  after routing selected and aggregate `SEND_CQ` work through the typed drain.
  The lower-level RDMA payload-only pollers still exist in
  `remote_execution_peer_transport_rdma.c`; Stage 2e still owns deleting or
  fencing the remaining stash/callback-absent compatibility layer after Stage 2d
  validation.
- Build evidence so far: after `git clang-format`, no-stats Citus/Homer
  `make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'` passed and
  `git diff --check` passed in both `citus-dbcomm` and `postgres-citus`.

### Validation - Stage 2d

- Dedicated validation agent accepted Stage 2d. Raw artifacts are under
  `/tmp/homer_stage2d_validation_20260619_205313`; the verdict is
  `/tmp/homer_stage2d_validation_20260619_205313/43_acceptance_verdict.txt`.
- Build/deploy passed as `dbcomm`: no-stats Citus/Homer `service-bin
  client-bin`, Citus/Homer install, targeted Postgres build/install, installed
  prefix rsync to `farnet0`, no stats-marker strings, and farnet1/farnet0 hash
  match for `postgres`, `pgbench`, `pg_basebackup`,
  `citus_tuple_sink_service`, `libhomer_client.a`, and `citus.so`.
- Remote RDMA pgbench c1 from `farnet0` to `farnet1` completed all warmed runs
  with zero failed transactions. Warmed median was `3667.106658 TPS`, average
  latency `0.273 ms`, p95 `0.282 ms`, p99 `0.298 ms`, and max `6.328 ms`.
  Warmed TPS varied across runs `2-4` as `4027`, `3667`, and `3666`; treat this
  as measurement variance unless repeated runs show a trend.
- Remote RDMA pgbench c4 completed all warmed runs with zero failed
  transactions. Warmed median was `10297.099642 TPS`, average latency
  `0.388 ms`, p95 `0.533 ms`, p99 `0.607 ms`, and max `17.775 ms`. This is
  within the Stage 2 tolerance band and does not show a c4 regression from
  payload/ACK CQ routing.
- Local Homer blackhole basebackup completed with rc `0`; warmed median real
  time was `4.05s`.
- Remote RDMA blackhole basebackup completed with rc `0`; warmed median real
  time was `4.14s`.
- Focused runtime scans were clean for Stage-2d CQ ownership failures and old
  stash/full symptoms: no `payload send-completion stash is full`, no
  `command send-completion stash is full`, no
  `peer-client completion publish stash is full`, no `send-CQ drain failure`, no
  `typed send-CQ drain failed`, no `typed send-CQ wait failed`, no
  `payload-typed-completion`, no `byte-ring-typed-completion`, no
  `receiver head-ACK completion drain failed`, and no
  `remote client SQL command path is not ready`.
- Validation caveats:
  - the first cleanup attempt exited `143` because a broad `pkill -f` pattern
    matched the cleanup shell; cleanup was rerun with bracketed patterns and the
    accepted results were collected after a clean process preflight;
  - `farnet1`'s data directory is not readable by the interactive user, so the
    validation agent collected the local service log via
    `sudo -n -u dbcomm cat`.

### Implementation progress - Stage 2e

- Stage 2e removes the RDMA-layer stash storage and semantic-specific send-CQ
  poller APIs from the Citus/Homer source tree. The stash slot constants,
  payload/command stash records, and per-connection stash fields were removed
  from
  [`TupleSinkServicePeerConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:474).
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1551)
  now requires typed owner callbacks for payload, command, and peer-client
  completion-publish WR-ID namespaces. A callback-absent drain may still retire
  control WR-IDs, but if it sees command, payload, or completion-publish CQEs it
  fails with an explicit ownership error instead of hiding the CQE for a later
  semantic poller.
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1802)
  no longer replays peer-client-completion publish CQEs from a side buffer. The
  public header no longer declares
  `TupleSinkServicePollPeerPayloadSendCompletionRdma()`,
  `TupleSinkServicePollPeerReceiverHeadAckCompletionRdma()`,
  `TupleSinkServicePollPeerCommandSendCompletionRdma()`, or
  `TupleSinkServicePeerCommandCompletionPollResult`
  [remote_execution_peer_transport_rdma.h](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:720).
- The selected command path now threads `HomerServicePayloadStreamEntry` through
  [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10998)
  into
  [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15344).
  Its opportunistic command source-retirement helper
  [`TupleSinkServiceDrainRemoteClientSqlCommandWriteCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4773)
  now calls
  [`HomerServiceDrainTypedPeerSendCq()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11255)
  instead of the removed command-only RDMA poller. This is the key ownership
  fix: command-source progress can still release local command slots before
  posting more commands, but it can also resolve payload and ACK CQEs if those
  arrive on the same physical CQ.
- Build and static hygiene evidence: no-stats
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`
  passed; `git diff --check` passed in `citus-dbcomm`; a focused source grep for
  the removed stash symbols and old semantic-specific poller APIs returned no
  matches in `remote_execution_peer_transport_rdma.c`,
  `remote_execution_peer_transport_rdma.h`, and
  `tuple_sink_service_process.c`.
- Validation status: accepted by cross-machine validation on June 19, 2026.
  Artifacts are under `/tmp/homer_stage2e_validation_20260619_211642`; the main
  script is `validate_stage2e.sh`, the command transcript is `commands.log`, and
  the corrected service-log scan commands are in
  `manual_service_log_scan_commands.txt`.
- Validation evidence: no-stats Citus/Homer build/install, targeted Postgres
  build/install, install-prefix rsync to `farnet0`, and installed hash
  comparison all passed; `deploy/sha256.diff` is empty. Warmed workload results:
  remote RDMA pgbench c1 `4197.10`, `3772.36`, `3733.26` TPS, median
  `3772.36` TPS; remote RDMA pgbench c4 `10294.24`, `10298.13`, `10279.52` TPS,
  median `10294.24` TPS; local Homer blackhole basebackup `4.15`, `4.16`,
  `4.15` seconds, median `4.15` seconds; remote RDMA blackhole basebackup
  `4.12`, `4.20`, `4.27` seconds, median `4.20` seconds. All workload commands
  returned success and pgbench reported zero failed transactions.
- Focused validation scan result: clean. The corrected scan included service
  logs from both hosts and found no ownership/stash/fallback/error patterns such
  as `send-CQ drain received`, `without payload owner callback`,
  `without command owner callback`, `without owner callback`,
  `send-CQ drain failure`, `typed command send-CQ drain failed`,
  `remote client SQL command path is not ready`, old stash strings, CQ reset
  strings, failed transactions, or timeouts.
- Remaining caveat after validation: callback-absent send-CQ drains still exist
  in the
  RDMA peer-control/control-resource paths, but they are now fenced as
  control-only paths. If real workload validation reports
  `send-CQ drain received ... without ... owner callback`, that is evidence that
  a remaining peer-control helper is still making hidden physical send-CQ
  progress and must be moved behind the service-owned typed drain.

### Validation - Stage 2b

- Dedicated validation agent accepted Stage 2b with no-stats build/install/sync
  and cross-machine workloads. Raw artifacts are under
  `/tmp/homer_stage2b_validation_20260619_200606`; the verdict is
  `/tmp/homer_stage2b_validation_20260619_200606/19_acceptance_verdict.txt`.
- Build/deploy passed as `dbcomm`: Citus/Homer `service-bin client-bin`,
  Citus/Homer install, targeted Postgres build/install, installed ownership,
  no stats-marker strings, protocol version `10U`, and farnet1/farnet0 hash
  match for `postgres`, `pgbench`, `pg_basebackup`, `citus_tuple_sink_service`,
  `libhomer_client.a`, and `citus.so`.
- Remote RDMA pgbench c1 from `farnet0` to `farnet1` completed all runs with
  zero failed transactions. Warmed TPS was `4187.687111`, `3760.545038`, and
  `3726.895345`; p99 was `0.259`, `0.293`, and `0.298 ms`.
- Remote RDMA pgbench c4 completed all runs with zero failed transactions.
  Warmed TPS was `10509.033960`, `10425.596298`, and `10388.120976`; p99 was
  `0.597`, `0.589`, and `0.613 ms`. This is within tolerance versus Stage 1
  and shows no c4 regression from the peer-client completion FIFO migration.
- Local Homer blackhole basebackup completed with rc `0` in `4.02s`, `4.01s`,
  and `4.19s`.
- Remote RDMA blackhole basebackup completed with rc `0`; after warmup `5.34s`,
  warmed runs were `4.33s`, `4.30s`, and `4.33s`.
- Focused service-log scan found no Stage-2b ownership errors: no
  `peer-client completion checkpoint index`, `lane FIFO`, `no lane FIFO`,
  `did not contain checkpoint`, or `send-CQ drain failed`.
- Validation caveats:
  - farnet1 PostgreSQL logged repeated Citus warnings about connecting to
    `dbcomm@10.10.1.100:5432`; farnet0 PostgreSQL was intentionally not running
    for this pgbench/basebackup validation shape, so this is not a Homer
    transport failure.
  - farnet1 service log shows non-fatal `stale payload progress grant` lines
    after local basebackup cleanup. Workloads completed correctly and these are
    not Stage-2b FIFO/checkpoint/drain failures, but they remain worth tracking
    when scheduler cleanup resumes.

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
    HOMER_RESULT_DRAIN_COMPLETE,
    HOMER_RESULT_DRAIN_VISIBILITY_PENDING,
    HOMER_RESULT_DRAIN_ERROR
} HomerClientResultDrainStatus;

typedef struct HomerClientResultDrainBudget
{
    uint32_t maxRecords;
    uint64_t maxBytes;
} HomerClientResultDrainBudget;

typedef struct HomerClientResultDrainTarget
{
    bool hasRequiredFrontier;
    uint64_t requiredGeneration;
    uint64_t requiredTail;
    uint64_t requiredEosOrdinal;
} HomerClientResultDrainTarget;

bool HomerClientDrainResultSinkUntil(
    HomerClientResultSink *sink,
    const HomerClientResultDrainTarget *target,
    const HomerClientResultDrainBudget *budget,
    HomerClientResultDrainStatus *status,
    uint64_t *drainedRowCount,
    char *errorMessage,
    size_t errorMessageBytes);
```

### State transitions and ownership

- `HomerClientDrainResultSinkUntil()` must never spin internally.
- Stage 3a uses `target->hasRequiredFrontier = false` because the old completion
  ABI does not yet carry `requiredTail` or `requiredEosOrdinal`. It implements
  bounded, non-spinning drain using current EOS semantics.
- Stage 3b sets `target->hasRequiredFrontier = true`, making the terminal
  frontier authoritative.
- `HOMER_RESULT_DRAIN_NOT_READY` means no complete next record is currently
  published.
- `HOMER_RESULT_DRAIN_PROGRESS` means records were consumed, but the budget was
  exhausted before the target was reached.
- `HOMER_RESULT_DRAIN_COMPLETE` means the required tail was reached and the exact
  EOS ordinal was consumed, or current EOS semantics completed when no explicit
  target exists yet.
- `HOMER_RESULT_DRAIN_VISIBILITY_PENDING` means the tail gate is visible but the
  record body is not yet stable; increment a separate counter and return to the
  event loop.
- Pgbench retains the terminal completion lease and retries later; it does not
  ACK the completion until apply succeeds.
- Running-command opportunistic drain may use small budgets, but terminal apply
  must be bounded and retryable.
- `maxRecords == 0` means no record-count limit. `maxBytes == 0` means no byte
  limit.
- If the next valid record exceeds `maxBytes` and no record has yet been
  processed, process that one record anyway to avoid budget livelock.

### Targeted tests

- Completion arrives before payload.
- Payload arrives before completion.
- Required tail posted but source not retired.
- Partial payload post failure.

### Acceptance

- Header-mismatch retry loops are not part of normal terminal drain behavior.
- Pgbench can hold a terminal completion lease across `NOT_READY` result-drain
  returns without ACKing it.

### Implementation progress - Stage 3a

- The public client API now includes
  [`HomerClientResultDrainStatus`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:101),
  [`HomerClientResultDrainBudget`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:110),
  [`HomerClientResultDrainTarget`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:116),
  and
  [`HomerClientDrainResultSinkUntil()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:297).
  The implemented function includes a `uint64_t *drainedRowCount` out parameter
  so existing pgbench row accounting can remain tied to the result-sink drain
  frontier without requiring a second API call.
- [`HomerClientResultSink`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:81)
  now records `eosRecordOrdinal` beside `eosSeen`. Stage 3a still calls the
  drain target with `hasRequiredFrontier = false`, but carrying the ordinal now
  keeps the implementation shape compatible with the Stage 3b explicit frontier
  target.
- [`HomerClientDrainResultSinkUntil()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2960)
  is the non-spinning parser. It drains only already-published complete records,
  returns `NOT_READY` when no complete next record is available, returns
  `VISIBILITY_PENDING` when a published frontier names a record whose full body
  is not yet available, and returns `PROGRESS` when it consumed records or a
  wrap gap but did not reach EOS/target. Validation rejected the tempting
  shortcut of treating a complete-looking malformed header as immediate
  corruption: a stale header can still contain plausible size fields while the
  published tail is already visible. Stage 3a therefore returns
  `VISIBILITY_PENDING` for header-envelope mismatch and lets the pgbench event
  loop retry the leased completion without ACKing it.
- `HOMER_CLIENT_COMPLETION_STATS` now also reports
  `result_drain_visibility_pending_events` from
  [`HomerClientCloseSession()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1271),
  incremented when
  [`HomerClientDrainResultSinkUntil()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2960)
  returns `VISIBILITY_PENDING`. The counter compiles out of no-stats
  performance builds.
- [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:3194)
  remains as a compatibility wrapper around the new API. It may still block when
  legacy callers pass `requireEos = true`, but the measured pgbench path no
  longer uses that wrapper.
- Pgbench now drains result payload through
  [`HomerDrainPendingResultSink()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3503).
  Terminal apply in
  [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3558)
  returns `HOMER_COMPLETION_NOT_APPLIED_RETRY` if the drain status is
  `NOT_READY`, `PROGRESS`, or `VISIBILITY_PENDING`.
  [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3733)
  already preserves the active mailbox lease on that retry result and therefore
  does not ACK the terminal completion until the result drain reaches current
  EOS semantics.
- Build/static evidence before workload validation: no-stats
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`
  passed in `citus-dbcomm`; `sudo -n -u dbcomm make install-headers
  install-service-bin install` refreshed the installed client header/library for
  the Postgres link; `sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build
  src/bin/pgbench/pgbench` passed in `postgres-citus`; `git diff --check`
  passed for the touched Citus/Homer client files and `pgbench.c`.
- Validation status: accepted by cross-machine validation on June 19, 2026.
- Rejected validation attempt: `/tmp/homer_stage3a_validation_20260619_213005`
  rebuilt/installed/synced successfully and produced acceptable warmed
  performance (`c1` median `3729.458376 TPS`, `c4` median `10288.605678 TPS`,
  local basebackup median `4.13s`, remote RDMA basebackup median `4.14s`), but
  the c4 warmup aborted after `30198/40000` transactions with
  `failed to drain Homer result sink for sql_execute: tuple result byte-ring
  record header mismatch`. The failure showed that Stage 3a must classify this
  timing window as `VISIBILITY_PENDING`, not a fatal header mismatch.
- Accepted validation retry: `/tmp/homer_stage3a_validation_retry_20260619_213911`
  rebuilt/installed/synced no-stats Citus/Homer and targeted Postgres artifacts
  as `dbcomm`; installed hashes matched across `farnet1` and `farnet0` for
  `postgres`, `pgbench`, `pg_basebackup`, `citus_tuple_sink_service`,
  `libhomer_client.a`, and `citus.so`. Warmups completed successfully,
  including c4 warmup `rc=0`, `40000/40000`, zero failed transactions,
  `10344.824 TPS`, p95 `0.513 ms`, and p99 `0.576 ms`.
- Accepted warmed workload results: remote RDMA pgbench c1 `4120.634`,
  `3704.433`, `3709.128` TPS with p99 `0.263`, `0.298`, `0.300 ms`; remote RDMA
  pgbench c4 `10301.913`, `10231.911`, `10181.832` TPS with p99 `0.583`,
  `0.586`, `0.591 ms`; local Homer blackhole basebackup `4.06`, `4.18`,
  `4.03` seconds; remote RDMA blackhole basebackup `4.31`, `4.12`, `4.34`
  seconds.
- Accepted scan result: the final complete scan found no requested
  rejection/error patterns, including no `tuple result byte-ring record header
  mismatch`; the broader failed-transaction scan only found expected
  `number of failed transactions: 0 (0.000%)` lines. The only caveat was
  non-fatal `stale payload progress grant` service-log messages after local
  basebackup cleanup; validation runtime was stopped afterward and the final
  process preflight was clean on both hosts.

## Stage 3b: one compact completion ABI with required frontier

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
#define HOMER_COMPLETION_INLINE_DETAIL_BYTES 128

_Static_assert(sizeof(CitusRemoteExecCommandCompletionHot) <= 256,
               "completion hot record must stay bounded");
```

`detailBytes` must be `<= HOMER_COMPLETION_INLINE_DETAIL_BYTES`. Treat `detail`
as byte-counted data; publishers may NUL-terminate for convenience, but readers
must use `detailBytes` as authoritative. A 128-byte inline detail keeps the hot
record under the 256-byte target on normal 64-bit layouts. The essential change
is removing the full tuple contract from every completion event; the resulting
record is bounded and compact, though not literally one cache line.

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

`requiredResultTail` is the absolute published tail in the frontend-visible
receive byte ring. It is not the backend producer-ring tail, the service
source-ring posted tail, the local source-retirement frontier, or a modulo ring
offset.

Failure behavior:

```text
No payload WR posted:
    release reserved ownership normally.

One or more payload WRs posted, but tail WIMM not posted:
    mark payload lane/QP reset-required;
    do not publish terminal completion;
    do not reuse source storage normally.
```

### Protocol/version changes

- Bump the completion mailbox protocol/version once for the compact completion
  plus descriptor-table deployment unit.
- Mixed old/new `pgbench`, `libhomer_client.a`, and
  `citus_tuple_sink_service` must fail fast.
- Keep the old `CitusRemoteExecCommandCompletion` only as an explicitly named
  compatibility layout during migration.

### Acceptance

- Completion bytes per pgbench transaction drop materially.
- Terminal completion can be posted before local payload source retirement, but
  the frontend applies/ACKs only after draining the named required frontier.
- Remote c1/c4 correctness remains stable.

### Implementation progress - Stage 3b/4a ABI scaffolding

- The compact completion constants and future hot record are now compile-visible
  in
  [`remote_execution_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:482):
  [`CitusRemoteExecCommandCompletionHot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:507)
  carries `requiredResultTail`, `requiredEosRecordOrdinal`, and
  `resultDescriptorVersion`, and the `_Static_assert` keeps the record within
  the bounded `<= 256` byte target
  [remote_execution_control_protocol.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:553).
- The live local/backend/client/peer completion mailboxes still use the legacy
  [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:452).
  This is intentional: Stage 3b and Stage 4a are a single deployable protocol
  bundle, so this sub-step does not bump the live protocol version or switch the
  mailbox layout.
- Header-only conversion helpers are present for the later migration:
  [`CitusRemoteExecFillHotCompletionFromLegacy()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:609)
  maps the current completion image into the compact hot record while accepting
  the future required frontier and descriptor version as explicit arguments.
  These helpers are inert until the live mailbox migration calls them, so they
  do not add hot-path work to the accepted Stage 3a runtime.
- Compile/static evidence for this scaffolding: no-stats
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`
  passed in `citus-dbcomm`; `sudo -n -u dbcomm make install-headers
  install-service-bin` refreshed the installed header/library for downstream
  compilation; `sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build
  src/bin/pgbench/pgbench` passed in `postgres-citus`; `git diff --check`
  passed for the touched protocol header. Full cross-machine workload validation
  is deferred until the live compact completion/descriptor publication path is
  wired, because this sub-step does not change the runtime mailbox behavior.

## Stage 4a: versioned cold descriptor side table

Goal: publish queue attachment and tuple shape once per descriptor version, then
reference that version from compact completions. This is part of the same
completion protocol deployment unit as Stage 3b: compact completion,
descriptor table, publisher, and client stable-read/cache may be developed in
separate commits, but there is no supported installed state where compact
row-producing completions exist without a usable descriptor table.

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

First descriptor table:

```c
#define HOMER_RESULT_DESCRIPTOR_SLOTS 4

typedef struct HomerResultDescriptorSlot
{
    uint64_t bodyVersion;
    uint32_t descriptorBytes;
    uint32_t flags;
    CitusTupleSinkQueueAttachment queue;
    CitusTupleViewContract contract;
} HomerResultDescriptorSlot;

typedef struct HomerResultDescriptorTable
{
    uint64_t readyVersion[HOMER_RESULT_DESCRIPTOR_SLOTS];
    HomerResultDescriptorSlot slots[HOMER_RESULT_DESCRIPTOR_SLOTS];
} HomerResultDescriptorTable;
```

This descriptor side table is separate from
[`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485).
The existing completion mailbox is a multi-slot hot event ring. The descriptor
table is a cold side table for large queue/tuple-shape descriptors referenced by
compact completion events.

Publication order:

```text
write bodyVersion + descriptor body
write readyVersion last
completion references descriptorVersion
```

Reader protocol:

```text
slot = descriptorVersion & (HOMER_RESULT_DESCRIPTOR_SLOTS - 1)
ready1 = readyVersion[slot]
copy bodyVersion + descriptorBytes + body
ready2 = readyVersion[slot]

accept only when:
    ready1 == expectedVersion
    bodyVersion == expectedVersion
    ready2 == expectedVersion
```

First retention model:

```text
Descriptor slots are append-only until session teardown.
If more than HOMER_RESULT_DESCRIPTOR_SLOTS descriptor versions are needed in one
session, fail fast with an explicit descriptor-exhaustion error.
```

Four full fixed-capacity contracts are roughly `~130 KiB` per session, which is
acceptable for the benchmark-oriented first implementation and avoids adding
remote descriptor-credit propagation before there is evidence that descriptor
churn matters.

Do not describe this as a two-slot mailbox. If that phrase appears in design
discussion, it usually means a double-buffered descriptor side table with
stable-read slots, not the existing hot completion mailbox. The hot completion
mailbox is already an eight-slot SPSC event ring in
[`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485).
The descriptor structure is a separate cold versioned side table. The first
implementation uses four append-only descriptor slots, not two slots, because
four fixed-capacity tuple contracts are still small enough for a session-local
side table and avoid remote descriptor-credit protocol work.

A literal two-slot descriptor mailbox is rejected for the first implementation:
it would require either a tight reuse credit from the client ACK path or careful
proof that no completion can still reference the older physical slot. Because
current measured client-SQL has at most one command completion in flight but the
descriptor object is large and cold, a four-slot append-only table gives simpler
debuggability without adding hot-path credit traffic.

In other words, the "two slots" idea is a double-buffering proposal for the
cold descriptor body, not a sizing proposal for the host-client completion
mailbox. Two physical descriptor slots are enough only if the publisher can
prove that the older slot's descriptor version is no longer referenced by any
unapplied completion. The current first implementation intentionally avoids
that proof and its remote-credit plumbing by using four non-reused descriptor
versions per session.

Descriptor publication ownership:

```text
The frontend-side Homer service owns frontend-visible descriptor publication.
The backend produces tuple shape and result metadata.
The service owns queue allocation/translation and publishes the descriptor.
The peer/backend completion publisher references the resulting descriptorVersion.
The client library caches and validates descriptors only.
```

Descriptor publication is cold, so receiver-service CPU publication is
acceptable. The hot completion path remains one-sided and client-polled.

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
- Descriptor exhaustion fails explicitly; it must not silently re-enable the old
  descriptor-heavy completion path on measured runs.

### Implementation progress - Stage 4a ABI scaffolding

- The first descriptor-table layouts are now compile-visible:
  [`CitusTupleSinkQueueAttachment`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:488),
  [`CitusResultStreamBinding`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:499),
  [`HomerResultDescriptorSlot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:538),
  and
  [`HomerResultDescriptorTable`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:547).
  `HOMER_RESULT_DESCRIPTOR_SLOTS` is fixed at four and checked as a power of
  two.
- Header-only helpers
  [`CitusRemoteExecFillQueueAttachmentFromDescriptor()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:572)
  and
  [`CitusRemoteExecFillStreamBindingFromDescriptor()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:594)
  preserve the intended split between cold queue attachment and per-command
  binding. They are not yet connected to service-owned descriptor publication or
  client-side stable descriptor reads.
- The descriptor-table stable-read protocol is also represented in header-only
  helper form:
  [`HomerResultDescriptorReadStatus`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:553),
  [`CitusRemoteExecPublishResultDescriptor()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:672),
  and
  [`CitusRemoteExecReadResultDescriptorStable()`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:695)
  implement the planned `readyVersion/bodyVersion/readyVersion` shape. These
  helpers currently use ordinary header-level loads/stores; the live service and
  client migration should wrap publication and reads with the existing
  service/client atomic helper conventions when wiring the shared-memory table.
- Runtime acceptance is still future work for the Stage 3b/4a bundle. The next
  implementation step must add a frontend-side service-owned descriptor table,
  publish descriptor versions with the stable-read protocol, and make compact
  completions reference those versions before the live mailbox protocol can be
  switched and benchmarked.

### Implementation progress - Stage 4a live mailbox ABI

- The frontend completion mailbox shared-memory ABI now reserves space for the
  cold descriptor side table by embedding
  [`HomerResultDescriptorTable`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:547)
  as
  [`CitusRemoteExecClientCompletionMailbox::resultDescriptorTable`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:799).
  This makes the descriptor table address stable for later service-owned
  descriptor publication without introducing a second shared-memory object.
- Because the mailbox layout changed, the Homer control protocol and shared
  memory names were bumped from `v21` to `v22`:
  [`CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:28),
  [`CITUS_REMOTE_EXEC_CONTROL_SHM_NAME`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:29),
  [`CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_PREFIX`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:37),
  and
  [`CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_PREFIX`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:39).
- This is a live ABI step, but not yet a live descriptor-data-path step.
  Runtime publication and frontend consumption still use legacy
  [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:452)
  records. The descriptor side table is present, zeroed with the mailbox, and
  mapped by clients, but it is not yet populated or referenced by completions.
- Compile/static evidence: no-stats
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`
  passed in `citus-dbcomm`; `sudo -n -u dbcomm make install-headers
  install-service-bin` installed the v22 headers and service/client library;
  `sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build src/backend/postgres
  src/bin/pgbench/pgbench src/bin/pg_basebackup/pg_basebackup` passed in
  `postgres-citus`; and `sudo -n -u dbcomm meson install -C build --no-rebuild`
  installed the rebuilt Postgres tree. Cross-machine workload validation is
  required for acceptance because stale binaries or stale shared-memory names
  would fail at runtime despite the source-level compile passing.
- Validation exposed one concrete install prerequisite: after a Homer control
  ABI bump, `citus.so` must also be rebuilt and installed with
  `sudo -n -u dbcomm make install`, not only `install-headers
  install-service-bin`. The first local and remote attempts failed because
  `/data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so` still
  contained `citus_remote_execution_control_v21` while `pgbench`, the service,
  headers, and `libhomer_client.a` were on v22. The symptom was
  `completion mailbox published unsupported protocol=21` on local pgbench and
  remote c1 timeout followed by RDMA peer-control/send-CQ flush errors. After
  `make install`, `strings citus.so` reported only
  `citus_remote_execution_control_v22`; restarting PostgreSQL was required to
  unload the stale shared object.
- Runtime acceptance for this live ABI step passed after the full install,
  PostgreSQL restart, install-prefix sync to `farnet0`, and clean v22 shared
  memory baseline. Raw logs are under
  `/tmp/homer_stage4a_mailbox_descriptor_validation_20260619_220733`.
  - ABI string checks showed v22 names in local and remote `pgbench`,
    `citus_tuple_sink_service`, and `citus.so`.
  - Local pgbench smoke after reinstall completed `1000/1000` transactions at
    `6171.011058 TPS` with p99 `0.197 ms`
    (`/tmp/homer_stage4a_local_smoke_after_citus_install_20260619_220629`).
  - Remote RDMA c1 processed all transactions with rc `0`. Warmup was
    `2744.473591 TPS`; warmed repeats were `4165.823087` and
    `3740.921718 TPS`.
  - Remote RDMA c4 processed all transactions with rc `0`. Warmup/repeats were
    `10575.329659`, `10566.379050`, and `10495.745550 TPS`.
  - Local Homer blackhole basebackup completed in `3.90s`, `3.88s`, and
    `3.88s`.
  - Remote RDMA blackhole basebackup completed with cold/warmup `6.34s`, then
    warmed repeats `4.25s` and `4.25s`.
  - Current service-log scan had no v21/protocol-mismatch/RDMA failure lines.
    The farnet1 service log did include three `stale payload progress grant`
    diagnostics during basebackup; treat that as residual scheduler diagnostic
    noise for a later cleanup, not as a Stage 4a ABI failure.

### Implementation progress - Stage 3b/4a live hot completion

- The Stage 3b/4a deployment bundle is now live in the Citus/Homer source tree
  as control protocol `v23`. The frontend completion mailbox slots now carry
  [`CitusRemoteExecCommandCompletionHot`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:507)
  instead of the descriptor-heavy legacy completion record, while
  [`CitusRemoteExecClientCompletionMailbox::resultDescriptorTable`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:831)
  keeps the cold four-slot descriptor side table in the same shared-memory
  object.
- Descriptor publication is service-owned. The service caches the current queue
  attachment and tuple contract in
  `TupleSinkServiceClientCompletionMailboxState`, builds compact completions
  through
  [`TupleSinkServiceBuildClientHotCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13380),
  and publishes descriptors with
  [`TupleSinkServicePublishResultDescriptorLocal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13307)
  on the local path or descriptor-body/ready-version RDMA writes in
  [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13730)
  on the peer path.
- The client stable-read path reconstructs the legacy completion view from the
  compact hot record plus descriptor side table in
  [`HomerClientCopyHotCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:986).
  Descriptor-not-ready and descriptor-body visibility races remain bounded
  retry conditions through
  [`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1070)
  and
  [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2451);
  mismatches and overruns still fail fast.
- Terminal row-result completions carry the exact frontend-visible drain target.
  [`TupleSinkServiceFillHotCompletionRequiredFrontier()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13418)
  fills `requiredResultTail` from the frontend-visible byte-ring posted tail and
  `requiredEosRecordOrdinal` from the posted EOS transport ordinal. Pgbench
  stores that target from the completion lease before applying the terminal
  event in
  [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3815).
- Dedicated no-stats cross-machine validation accepted the live hot-record
  migration. Raw artifacts are under
  `/tmp/homer_stage3b4a_hot_completion_validation_20260619_225540`.
  - Rebuilt and installed no-stats Citus/Homer with
    `CPPFLAGS='-D_GNU_SOURCE'`, rebuilt the relevant Postgres targets,
    installed the prefix, and synced `/data/dbcomm/pg-citus/` to `farnet0`.
  - Installed hash checks matched between `farnet1` and `farnet0` for
    `pgbench`, `pg_basebackup`, `citus_tuple_sink_service`, `citus.so`, and
    `libhomer_client.a`.
  - String checks found `citus_remote_execution_control_v23` and the v23
    client/peer completion shared-memory names in the expected binaries and
    libraries.
  - Remote RDMA pgbench c1 passed with zero failed transactions: cold/warmup
    `3415.298797 TPS`, warmed `4330.552830` and `3899.867775 TPS`.
  - Remote RDMA pgbench c4 passed with zero failed transactions:
    `10708.873908`, `10673.793915`, and `10658.580357 TPS`.
  - Local Homer blackhole basebackup completed in `4.17s`, `3.98s`, and
    `4.02s`.
  - Remote RDMA blackhole basebackup completed with cold/warmup `6.21s`, then
    warmed `4.10s`, `4.08s`, and `4.32s`.
  - The validation-window log scan found no protocol mismatch, hot-record or
    descriptor mismatch, `FATAL`, RDMA failure, invalid command-ring slot,
    tuple-result byte-ring header mismatch, or nonzero failed-transaction
    lines. Final local and remote process preflights were clean.
  - Caveat: two early cleanup wrapper attempts self-matched broad `pkill -f`
    patterns and exited with code `143` before validation started. The validator
    reran cleanup with split safe commands; those early logs are diagnostic-only
    and outside the measurement window.
- Descriptor-counter acceptance also passed in a focused stats-enabled
  diagnostic run. Raw artifacts are under
  `/tmp/homer_stage3b4a_descriptor_stats_validation_20260619_230448`.
  - Stats runtime was built with
    `CPPFLAGS='-D_GNU_SOURCE -DHOMER_SERVICE_CLIENT_SQL_STATS=1 -DHOMER_CLIENT_COMPLETION_STATS=1 -DHOMER_SERVICE_PAYLOAD_STATS=1'`,
    installed, and synced to `farnet0`; relevant installed hashes matched
    across hosts.
  - Remote RDMA pgbench c1 `-t 5000` completed `5000/5000` transactions with
    zero failures. TPS was `2075.271767`, but this was a cold, short,
    stats-enabled diagnostic run and is not a performance acceptance number.
  - Publishing-side descriptor counters on `farnet1` showed exactly the desired
    stable-shape behavior:
    `result_descriptor_publishes=1`, `result_descriptor_bytes=33464`,
    `result_descriptor_cache_hits=9999`,
    `result_descriptor_cache_misses=1`, and
    `result_descriptor_exhaustions=0`. The receiver side on `farnet0` reported
    zero descriptor counters, as expected for this direction because descriptor
    publication happens on `farnet1`.
  - Context counters were `completion_events_published=40003` and
    `completion_bytes_posted=10560792`.
  - After the diagnostic run, Citus/Homer was rebuilt, reinstalled, and synced
    with no-stats `CPPFLAGS='-D_GNU_SOURCE'`. Restored hashes matched across
    hosts, `citus_tuple_sink_service` no longer contained the stats-only
    descriptor/client-SQL strings, and final process preflights were clean.
  - Caveat: two early cleanup attempts self-selected transient wrapper PIDs
    before the measurement window; the validator switched to process-name-based
    cleanup and the actual validation/runtime window was clean.
- Stage 3b/4a is accepted for the live frontend completion hot-record migration.
  The next implementation stage should be Stage 4b stable-binding pre-arm, not
  Stage 4d compact command publication; Stage 4d remains deferred until the
  live completion ownership path is no longer moving.

## Stage 4b: stable-binding pre-arm

Goal: make the frontend able to drain a known result binding before terminal
completion, so STARTED suppression is a correctness-preserving optimization
rather than a prediction about result size.

### Rule

```text
Pre-arm only when all of the following are true:

    descriptor version is already cached;
    queue attachment is unchanged;
    previous generation was completely drained;
    new result generation is deterministically commandSequence;
    new startByteTail equals the previous generation's drained tail;
    frontend records the binding at command submission.
```

When these hold, the frontend has the queue attachment, descriptor, generation,
and start tail needed to drain records that arrive before the terminal
completion. When any condition fails, the command must use the descriptor-ready
STARTED path before payload can create producer backpressure.

### Acceptance

- Pgbench can arm a stable one-column SELECT binding at command submission
  after descriptor warmup.
- Descriptor miss, queue replacement, incomplete previous-generation drain, or
  nondeterministic binding falls back to STARTED/DESCRIPTOR_READY before payload
  can block.
- No correctness rule depends on predicting that a query result is small.

### Implementation progress and acceptance - Stage 4b

- Stage 4b stable-binding pre-arm is implemented in pgbench without suppressing
  STARTED. The frontend caches only the stable proof facts in
  [`CState`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:667):
  `homer_stable_result_binding_valid` and
  `homer_stable_result_drained_tail`. It does not copy another full completion
  image; the already-open
  [`HomerClientResultSink`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:90)
  remains the owner of the queue descriptor and tuple contract.
- The pre-arm predicate is implemented by
  [`HomerTryPrearmStableResultBinding()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3557).
  It only runs for `SQL_EXECUTE` tuple results, requires an already-open sink,
  requires the previous generation to have reached EOS, requires the current
  consumed head to equal the remembered drained tail, then rebinds the sink to
  `resultGeneration = commandSequence` and
  `startByteTail = homer_stable_result_drained_tail`.
- The cache is learned only after terminal result drain succeeds through
  [`HomerRememberStableResultBinding()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3535).
  Real STARTED/TUPLE_SINK_READY completions still validate any pre-armed binding
  through
  [`HomerPrearmedResultBindingMatchesCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3518)
  before the completion event is ACKed. A mismatch is a protocol error, not a
  fallback, because pre-arm may already have allowed early byte-ring drain.
- Pre-arm failure before STARTED is treated as an optimization miss: pgbench
  logs a debug message, invalidates the cached stable binding, and waits for the
  normal STARTED descriptor path. Failure, result-sink close, and session close
  invalidate the cache through
  [`HomerInvalidateStableResultBinding()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3502).
- Dedicated no-stats cross-machine validation accepted Stage 4b. Raw artifacts
  are under `/tmp/homer_stage4b_prearm_validation_20260619_231539`.
  - Build/install/sync passed: Citus/Homer no-stats rebuild with
    `CPPFLAGS='-D_GNU_SOURCE'`, Postgres target rebuild/install, install-prefix
    sync to `farnet0`, matching hashes for `pgbench`, `pg_basebackup`,
    `citus_tuple_sink_service`, `citus.so`, and `libhomer_client.a`, v23
    control/mailbox strings present, and stats-only strings absent.
  - A short remote RDMA pgbench debug smoke passed and captured pre-arm evidence
    in `pgbench_remote_c1_debug_prearm_evidence.txt`, including
    `client 0 pre-armed Homer result sink for sql_execute sequence=12 tail=208`
    and repeated later command sequences.
  - Remote RDMA pgbench c1 passed with zero failed transactions:
    `4369.720285`, `3893.968029`, and `3912.369190 TPS`.
  - Remote RDMA pgbench c4 passed with zero failed transactions:
    `10795.918279`, `10725.456469`, and `10655.999301 TPS`.
  - Local Homer blackhole basebackup completed in `4.02s`, `4.15s`, and
    `4.07s`.
  - Remote RDMA blackhole basebackup completed with warmup `5.66s` and warmed
    repeats `4.31s`, `5.36s`, `4.14s`, and `4.34s`. The `5.36s` warmed repeat
    is an outlier; the warmed median remains about `4.31s`, consistent with the
    previous acceptance band.
  - Log scans found no protocol mismatch, pre-arm binding mismatch,
    hot-record/descriptor mismatch, `FATAL`, RDMA failure, invalid command-ring
    slot, tuple-result byte-ring header mismatch, or nonzero failed transaction
    lines. Final process preflights on both hosts were clean.
- Stage 4b is accepted. Stage 4c may now suppress STARTED only when this
  pre-arm predicate succeeds; descriptor miss, queue replacement, incomplete
  previous-generation drain, and nondeterministic binding must continue to use
  STARTED/DESCRIPTOR_READY.

## Stage 4c: STARTED suppression

Goal: avoid STARTED events whose only purpose is descriptor readiness.

### Rule

```text
Suppress STARTED only when Stage 4b has pre-armed the result binding.

If Stage 4b pre-arm succeeds:
    do not publish STARTED solely for descriptor readiness.

If the descriptor version changes:
    publish descriptor first;
    publish STARTED/DESCRIPTOR_READY only if the frontend may need to drain
    before terminal completion.

For descriptor miss or queue replacement:
    publish descriptor-ready/STARTED before payload can create backpressure.
```

For pgbench's stable one-column SELECT, STARTED should go to zero after the
first descriptor publication and pre-arm setup. Correctness must not depend on
predicting whether a result is small.

### Targeted tests

- Stable descriptor reuse.
- Shape change.
- Queue replacement.
- Descriptor version wrap.
- Mixed binaries.
- Stable pgbench shape emits no STARTED after cache warmup.

### Implementation progress - Stage 4c

- Stage 4c is implemented in the current working tree and accepted for the
  current no-stats cross-machine validation tier.
- The frontend now marks commands whose result binding was pre-armed with
  `CITUS_REMOTE_EXEC_COMMAND_FLAG_RESULT_BINDING_PREARMED`
  [remote_execution_control_protocol.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:176).
  This flag is intentionally an explicit contract from the frontend; the service
  must not infer pre-arm from descriptor-cache state alone.
- Pgbench predicts the direct command-ring sequence with
  [`HomerClientPredictNextCommandSequence()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2128)
  before command publication. The helper is direct-mailbox-only and checks that
  the command ring is not full before returning `publishedEpoch + 1`.
- [`HomerStartCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3797)
  sets `CITUS_REMOTE_EXEC_COMMAND_FLAG_RESULT_BINDING_PREARMED` only when
  [`HomerTryPrearmStableResultBinding()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3557)
  succeeds for the predicted sequence. If the actual sequence differs, pgbench
  treats it as a protocol error; that should not happen for the current
  single-producer direct command mailbox.
- The service suppression predicate is
  [`TupleSinkServiceShouldSuppressPrearmedStartedCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13324).
  It requires all of:
  `STARTED`, `SQL_EXECUTE`, the explicit pre-armed command flag,
  `TUPLE_SINK_READY`, and a cached descriptor/contract match against the
  frontend-visible queue attachment.
- Local completion publication suppresses only the descriptor-readiness STARTED
  event after building the legacy completion image
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13710).
  Peer completion publication evaluates the same predicate only after backend
  result descriptors have been translated to the frontend receive queue
  [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14030).
  This keeps descriptor-cache equality in the same namespace the frontend
  pre-armed.
- Suppression updates the per-session last-published state through
  [`TupleSinkServiceMarkSuppressedClientCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13341)
  so the scheduler does not keep retrying a deliberately omitted STARTED event.
  Terminal completion is still published and still carries the required result
  frontier.
- `HOMER_SERVICE_CLIENT_SQL_STATS` now records
  `local_started_completions_suppressed` and
  `peer_started_completions_suppressed` in the wide `client_sql_stats` service
  log line. These counters are diagnostic-only and are expected to be absent
  from no-stats performance binaries.
- First validation rejected the initial implementation even though the no-stats
  workloads passed. Raw artifacts are under
  `/tmp/homer_stage4c_validation_20260619_233214`.
  - No-stats remote RDMA pgbench passed with zero failed transactions: c1 warmed
    repeats `4261.760755`, `3742.929606`, and `3795.352325 TPS`; c4 warmed
    repeats `10503.352145`, `10374.918427`, and `10462.185347 TPS`.
  - Local Homer blackhole basebackup passed with warmed repeats `4.14s`,
    `4.06s`, and `4.15s`; remote RDMA basebackup passed with warmed repeats
    `4.35s`, `4.18s`, and `4.34s`.
  - The stats diagnostic failed the Stage 4c-specific proof:
    `peer_started_completions_suppressed=0` on `farnet1` despite
    `result_descriptor_cache_hits=39999` and `result_descriptor_cache_misses=1`.
    This means descriptor caching and pre-arm shape were present, but the service
    suppression predicate never observed the explicit pre-arm command flag.
- Root-cause correction after the rejected run: remote direct-RDMA client-SQL
  command forwarding writes
  [`CitusRemoteExecLocalCommandRecord.commandFlags`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:126)
  to the backend command mailbox, but the backend-to-service completion image did
  not echo the flags back. On the backend-node service,
  [`TupleSinkServiceConsumeOneCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15193)
  restored `currentCommandKind`, `currentCommandState`, and
  `currentCommandSequence` from the backend completion, while
  `currentCommandFlags` stayed zero for the direct-RDMA remote path. The fix is
  to carry `commandFlags` in
  [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:460),
  populate it in
  [`PublishCompletionToMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1408),
  and restore it before evaluating the suppression predicate. The compact hot
  frontend completion intentionally does not carry `commandFlags`; adding that
  field there violated the bounded `<= 256` byte hot-record assertion, and the
  frontend does not need the flag.
- Static/build evidence so far: `git clang-format` was applied to the touched C
  files; no-stats Citus/Homer
  `sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'`
  passed; `sudo -n -u dbcomm make install-headers install-service-bin install`
  passed; Postgres
  `sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build src/backend/postgres src/bin/pgbench/pgbench src/bin/pg_basebackup/pg_basebackup`
  passed; targeted `git diff --check` passed in both trees.
- Focused retry artifact `/tmp/homer_stage4c_suppression_retry_20260619_234954`
  showed the command-flag correction made the suppression predicate fire:
  `peer_started_completions_suppressed=10569`,
  `result_descriptor_cache_hits=10570`, and
  `result_descriptor_cache_misses=1`. That run was still rejected because the
  remote pgbench process did not complete cleanly before service teardown, so it
  was mechanism evidence, not an acceptance run.
- Accepted validation artifact:
  `/tmp/homer_stage4c_correctness_20260620_000900`.
  No-stats runtime passed remote RDMA pgbench c1 correctness and warmed
  performance with zero failed transactions: c1 repeats `3823.201404`,
  `3841.397900`, and `4233.117797 TPS`; c4 repeats `10611.993675`,
  `10448.046176`, and `10485.972390 TPS`. Local Homer blackhole basebackup
  passed with `4.20s`, `4.14s`, and `4.03s`; remote RDMA basebackup passed
  with `5.69s` cold/warmup then `4.19s` and `4.14s` warmed.
- The completed stats-enabled c1 run in the same artifact provides the Stage 4c
  counter success signal: `completions_consumed=160003`,
  `peer_started_completions_suppressed=19999`, and
  `completion_events_published=140004`. The equality
  `completion_events_published == completions_consumed -
  peer_started_completions_suppressed` proves descriptor-readiness STARTED
  completions were omitted while terminal and other completion events continued
  to publish. `result_descriptor_cache_hits=20000` and
  `result_descriptor_cache_misses=1` show the stable descriptor path warmed once
  and was reused. After the stats run, no-stats runtime was restored, synced to
  `farnet0`, and marker scans on both hosts found no stats strings in
  `pgbench`, `citus_tuple_sink_service`, or `libhomer_client.a`.

## Stage 4d: compact variable-length command publication

Goal: apply the same hot/cold principle to frontend command publication.

### Substeps

Stage 4d is no longer "shorten the RDMA SGE over the legacy command slot".
It is a small command-publication protocol migration. The first accepted
implementation must do these in order:

1. Add publication correctness before byte reduction:
   - define a compact command header with protocol/version, `headerBytes`,
     `commandBytes`, `payloadBytes`, command kind, command flags, command
     sequence, target sink id, and a body identity such as `bodyEpoch`;
   - bump the backend command protocol version so mixed old/new binaries reject
     the command slot before execution;
   - teach the backend command reader to stable-copy a command into
     backend-local storage before dispatch rather than executing from a borrowed
     command-ring slot after one `readySeq` observation.
2. Fix body/ready partial-post behavior:
   - command publication owner state is
     `FREE -> RESERVED -> BODY_POSTED -> READY_POSTED -> RETIRED`;
   - failure before `BODY_POSTED` rolls back normally;
   - failure after `BODY_POSTED` but before `READY_POSTED` marks the QP/session
     reset-required and leaves cleanup to teardown;
   - failure after `READY_POSTED` is owned by CQ retirement or QP teardown.
3. Split frontend command progress into accepted and retired frontiers while
   keeping the frontend command mailbox as the registered source:
   - service collection uses a service-owned accepted cursor, not shared
     `consumedEpoch`;
   - after body/ready WRs are successfully posted, advance accepted epoch only;
   - advance frontend-visible `consumedEpoch` only when the typed send-CQ owner
     retires the command WR/source range.
4. Only then serialize command-kind-specific bytes:
   - COMMIT, ABORT, and SESSION_CLOSE are header-only;
   - CLIENT_SQL_TX_BEGIN carries only transaction-mode fields;
   - SQL_EXECUTE carries a compact SQL payload header plus `sqlBytes`;
   - TX_BEGIN_ATTACH carries fixed attach fields plus `replayTextBytes`.
5. Add a true verbs-inline fast path after the registered-source path is
   correct:
   - query/store the QP inline capacity during connection setup;
   - use `IBV_SEND_INLINE` only when `commandBytes <= maxInlineData`;
   - treat true inline source storage as reusable after successful
     `ibv_post_send`; keep registered-source storage reusable only after CQ
     retirement.
6. Remove the rejected service-owned source ring from the target path. It needs
   extra staged/post/retire cursors and teardown invariants, and provides no
   first-order benefit for small commands if direct registered source plus true
   inline are available.

### Acceptance

- Backend execution uses a stable copied command image. A ready/body visibility
  race can produce bounded `VISIBILITY_PENDING` diagnostics, but not immediate
  FATAL on the first complete-looking mismatch.
- `commandBytes` is authoritative and kind-specific validation rejects malformed
  compact commands.
- `commandBytesPosted` drops for pgbench transactions after the correctness
  substeps are accepted.
- Frontend command credit advances only on command send-CQ retirement unless the
  command was sent with true verbs inline.
- Partial body/ready post failures enter reset-required state instead of
  clearing local ownership and retrying on the live QP.
- Repeat-run remote RDMA pgbench c1 and c4 pass warmed correctness/performance
  runs, and session teardown reports zero outstanding command owners before
  deregistration/reuse.

### Rejected implementation attempt - June 20, 2026

A first Stage 4d attempt made
[`TupleSinkServiceLocalCommandRecordRdmaBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13266)
return command-kind-specific byte prefixes and enabled compact RDMA writes
directly from the frontend command mailbox slot in
[`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15348).
Validation artifact:
`/tmp/homer_stage4d_compact_rdma_validation_20260619_221520`.

The attempt is rejected for two reasons. The more direct explanation for the
captured invalid-slot failure is a remote body/ready visibility race introduced
by compacting bytes while retaining the legacy command-slot reader. The sender
reduced the body WRITE to a command-kind-specific prefix of
[`CitusRemoteExecLocalCommandRecord`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:122),
but the legacy command slot publishes
[`readySeq`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:152)
after the full maximum-sized record. A prefix-only write cannot include that
ready footer without transmitting the intervening unused bytes.

During remote c1 validation, the backend later FATALed with:

```text
remote exec backend received invalid command ring slot
slot_count=64 protocol=10 command_sequence=113759
expected_sequence=113759 ready_sequence=113759
```

The backend reader at
[`remote_execution_backend_bridge.c:2254`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2254)
polls `readySeq`, breaks as soon as it equals the expected sequence, borrows
`commandSlot->record`, validates once, and FATALs immediately on mismatch at
[`remote_execution_backend_bridge.c:2281`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2281).
The error detail then reads the same shared fields again. That explains the
observed shape where `readySeq` was expected, validation failed, and the detail
printed expected protocol/sequence values by the time the error was formatted.
This is a proof that Stage 4d cannot be implemented as a sender-only byte-prefix
optimization over the legacy slot; it needs a compact publication ABI and a
stable backend reader.

The attempt also exposed a real source-lifetime defect. The sender posted RDMA
from the frontend command slot, then advanced frontend `consumedEpoch` after
posting the WR in
[`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15992),
before a send CQE proved the NIC had consumed the source bytes. In the measured
one-command-in-flight pgbench path, semantic command completion makes immediate
frontend overwrite unlikely, so this is probably not the proximate cause of the
captured invalid-slot FATAL. It is still not a valid long-term source-credit
contract for non-inline RDMA writes.

Current corrective decision:

```text
HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA = 0
HOMER_SERVICE_COMPACT_PEER_CLIENT_COMPLETION_RDMA = 0
```

The compact command byte helper remains in the source tree for the next attempt
as
[`TupleSinkServiceLocalCommandRecordRdmaBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13539),
but the live measured path must stay on the accepted fixed-slot behavior. The
active code gate documents this at
[`HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:397),
and
[`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15768)
again posts from the registered frontend command mailbox source slot rather than
from a service-owned command source ring.

The remaining long-term candidate is true zero-copy frontend command storage
whose producer credit is not released until the send-CQ owner path retires the
RDMA source range. A service-owned intermediate source ring may still be useful
as a diagnostic design, but it is no longer an accepted straightforward next
step: the first implementation of that model failed repeat-run liveness
validation, documented below.

The same rule applies to legacy compact peer-client completion prefix writes:
do not measure an unaccepted partial compact-completion layout before the
compact completion and descriptor side table are deployed as one protocol unit.

Corrective validation after disabling both compact gates used artifact
`/tmp/homer_stage4d_disabled_validation_manual_20260619_223906`. The normal
fixed-slot command path and fixed-layout completion path passed remote RDMA
pgbench and basebackup again:

```text
remote c1 pgbench:
    warmup: 3312.766574 TPS
    warmed: 4100.748838 TPS, 3697.707255 TPS

remote c4 pgbench:
    warmed: 10381.663698 TPS, 10310.219025 TPS, 10350.686432 TPS

local Homer blackhole basebackup:
    4.20s, 4.03s, 4.01s

remote RDMA basebackup:
    warmup: 5.51s
    warmed: 4.27s, 4.08s, 4.29s
```

This checkpoint restores the previous accepted performance band and preserves
the Stage 4d lesson without treating the failed compact attempt as a regression
in the already-accepted transport stages. Two subagent validation attempts for
this checkpoint stalled before producing workload logs, so the accepted evidence
is the manual no-stats validation artifact above.

### Rejected service-owned command source-ring attempt - June 20, 2026

A second Stage 4d attempt tried to fix the frontend-source lifetime race by
copying each frontend command into a service-owned registered source slot ring,
then posting RDMA writes from that service-owned storage. The implementation
renamed `clientSqlCommandScratchRegionHandle` to
`clientSqlCommandSourceRegionHandle`, replaced the single scratch record with
`clientSqlCommandSourceSlots[]`, registered that source-slot ring during
`CLIENT_SQL_SESSION` peer open, copied commands with a new
`TupleSinkServiceCopyLocalCommandRecordForRdma()` helper, and set
`HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA = 1`.

Validation artifact:
`/tmp/homer_stage4d_validation_20260620_003329`, with verdict in
`/tmp/homer_stage4d_validation_20260620_003329/VERDICT.md`.

The no-stats rebuild/install/sync passed and marker scans found no stats strings,
but remote RDMA pgbench c1 failed liveness before c4, basebackup, or stats-counter
diagnostics were run:

```text
remote-c1 warmup: 20000/20000, failed=0, TPS=3631.121617, p99=0.232 ms
remote-c1 run=1:  20000/20000, failed=0, TPS=4674.301671, p99=0.234 ms
remote-c1 run=2:  started but produced no completion summary within the expected envelope
```

The stall-time service log tails did not show an explicit invalid-slot,
source-credit, send-CQ, or command failure error. This rejects the implementation
as a safe Stage 4d checkpoint, but it does not by itself prove that compact
command bytes are impossible. The leading interpretation is that the extra
service-owned source ring added another command-publication ownership frontier
without a sufficiently explicit progress/credit invariant. Until that invariant
is redesigned and instrumented, Stage 4d should not continue by adding small
surface fixes to the source-ring attempt.

After the failed validation, the source-ring code path was backed out of the live
working tree and the no-stats fixed-slot runtime was rebuilt, installed, and
synced to `farnet0`. The current code keeps both compact gates disabled and uses
the registered frontend command mailbox as the command RDMA source, matching the
previous accepted fixed-slot checkpoint.

### Current Stage 4d diagnosis and discussion boundary - June 20, 2026

Stage 4d should not be resumed as another local byte-packing patch. The current
diagnosis separates two defects that were conflated in the first note:

- The captured invalid-slot FATAL is most directly explained by a remote
  body-versus-ready visibility race. Stage 4d shortened the body bytes but kept
  the legacy command slot and legacy reader, where `readySeq` is a footer after
  the maximum-sized record and the backend does one borrowed read after seeing
  `readySeq == expected`. That is a protocol mismatch, not merely an ownership
  bookkeeping bug.
- Source lifetime is still wrong for non-inline RDMA writes. Advancing frontend
  `consumedEpoch` after post means "service accepted the command", not "the NIC
  no longer needs the source slot". That must be replaced by accepted-versus-
  retired command frontiers.
- The service-owned source-ring attempt fixed the immediate direct-source
  lifetime issue by copying into service-owned registered storage, but it did not
  fix the reader/publication ABI problem. It also introduced a new staged source
  state machine: frontend accepted, service source reserved/copied, body posted,
  ready posted, CQ-retired, and source released. The repeat-run c1 stall makes
  session teardown, generation reuse, owner cleanup, or a leaked staged source
  frontier especially suspicious.
- The live fixed-slot checkpoint avoids the Stage 4d races by treating the
  frontend command mailbox as the registered source and by keeping compact
  command writes disabled at
  [`HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:397).
  It still releases frontend credit after post, so it is a tolerated current
  checkpoint because validation restored the accepted performance band, not the
  target ownership model.

Before implementing Stage 4d again, settle the following ownership contract:

1. **Publication ABI**: compact command publication must not be a prefix of
   `CitusRemoteExecLocalCommandRecord`. Define a compact header, authoritative
   `commandBytes`, body identity, and kind-specific payload validation.
2. **Backend reader**: add a stable-copy reader that treats the first
   ready/body mismatch as visibility pending, retries while observed body
   identity/fingerprint changes, and escalates a stable repeated mismatch after
   a bounded number of event-loop passes.
3. **Partial-post state**: command publication must have explicit body-posted
   and ready-posted states. Clearing a reserved owner after body post but before
   ready post is invalid; that state is reset-required.
4. **Source/credit owner**: keep the frontend command mailbox as the first
   registered source design, but add service-owned accepted and retired cursors.
   Shared `consumedEpoch` advances only when CQ retirement or true inline post
   makes the frontend source reusable.
5. **CQ owner**: command WR retirement must flow through the typed shared-CQ
   dispatcher/owner FIFO rather than hidden polling or ad hoc per-path waits.
   Stage 4d should not add another CQ-consumer path.
6. **Fast path**: after correctness is accepted, add true `IBV_SEND_INLINE`
   for small compact commands that fit the QP's real inline capacity. Do not
   call registered scratch/source copies "inline".
7. **Instrumentation**: the next attempt needs counters for command source
   reservations, reservation stalls, body posts, ready posts, inline posts,
   registered posts, CQ retirements, accepted/retired epochs, frontend credit
   advances, body visibility-pending retries, persistent body mismatches,
   partial body-post failures, owner FIFO high water, close-time outstanding
   owners, and source reuse before retirement.

### Concrete Stage 4d implementation sequence

1. **4d-0 publication correctness**
   - Add the compact command header and protocol/version fields, but initially
     allow fixed-size body copies if that makes the stable reader easier to
     land.
   - Replace the backend borrowed-slot dispatch in
     [`remote_execution_backend_bridge.c:2254`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2254)
     with stable-copy/read/validate logic.
   - Add bounded visibility-pending diagnostics instead of immediate FATAL for
     first ready/body mismatches.
   - Fix body/ready partial-post handling in the command owner path around
     [`TupleSinkServiceReserveClientSqlCommandWriteCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4342)
     and the failure branches in
     [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15964).

2. **4d-1 direct-source ownership**
   - Add per-session accepted and retired command epochs in
     `TupleSinkServiceSessionState`.
   - Collect commands with `publishedEpoch > acceptedEpoch`, not
     `publishedEpoch > consumedEpoch`.
   - Move frontend-visible `consumedEpoch` advancement from the post path at
     [`tuple_sink_service_process.c:15992`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15992)
     to the typed command send-CQ retirement callback around
     [`TupleSinkServiceRetireClientSqlCommandWriteCompletionEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4426).
   - Add session generation and QP/post generation facts to command owners.
   - On session close, do not clear active posted command owners with
     [`TupleSinkServiceClearClientSqlCommandWriteCompletionsForSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4682)
     merely to empty the table. Drain them or mark/reset the QP/session, then
     deregister memory only after posted owners are retired or teardown-owned.

3. **4d-2 compact body**
   - Serialize the compact command header plus kind-specific payload into the
     existing frontend command storage shape or a versioned compact slot.
   - Use the frontend command mailbox as the non-inline registered source; do
     not revive the service-owned source ring as the primary path.
   - Keep the body/ready gate explicit for the general variable-length path.

4. **4d-3 true inline fast path**
   - Query and record actual `maxInlineData` on the command QP.
   - Use real `IBV_SEND_INLINE` for compact commands that fit.
   - Count inline vs registered command posts and bytes separately.

5. **4d-4 cleanup and acceptance**
   - Delete or fence the sender-only prefix helper once the compact ABI exists.
   - Remove temporary compact gates only after the direct-source and inline
     paths pass correctness/performance validation.
   - Validate remote RDMA pgbench c1/c4 warmed repeats, basebackup
     non-regression, stats counters, and repeat-run session teardown.

### Accepted Stage 4d registered-source checkpoint - June 20, 2026

Stage 4d now has an accepted compact-command implementation checkpoint. It
first landed the direct registered-source correctness work that previously
blocked compact command publication, and then added the true verbs-inline
fast-retire path for compact commands that fit the negotiated QP inline
capacity.

Implemented code shape:

- The backend command protocol is now v11 at
  [`CITUS_REMOTE_EXEC_BACKEND_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:27).
  [`CitusRemoteExecLocalCommandRecord`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:122)
  carries authoritative `headerBytes`, `commandBytes`, `payloadBytes`, and
  `bodyEpoch`, with
  [`CITUS_REMOTE_EXEC_LOCAL_COMMAND_HEADER_BYTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_backend_protocol.h:161)
  defining the compact header boundary.
- Backend dispatch no longer borrows the command slot after one `readySeq`
  observation. [`RemoteExecBackendReadStableCommandRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:353)
  stable-copies the command image, uses
  [`RemoteExecBackendValidateCommandRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:272)
  for kind-specific byte validation, and only then hands the copied record to
  the dispatch loop at
  [`ExecuteRemoteExecBackendCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2435).
- The service fills byte-counted frontend command records through
  [`TupleSinkServiceFinalizeLocalCommandRecordBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13559)
  before publication. The direct registered-source path remains enabled by
  [`HOMER_SERVICE_COMPACT_CLIENT_SQL_COMMAND_RDMA`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:397).
- The remote command sender now separates service-accepted and source-retired
  command frontiers with
  `clientSqlRemoteCommandAcceptedEpoch` and
  `clientSqlRemoteCommandRetiredEpoch` in
  [`TupleSinkServiceSessionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:994).
  [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15835)
  advances the accepted epoch after body/ready posts succeed, while
  [`TupleSinkServiceRetireClientSqlCommandWriteCompletionEntry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4427)
  advances frontend-visible `consumedEpoch` only after the typed command send-CQ
  owner retires the source range.
- The RDMA transport now exposes
  [`TupleSinkServicePeerConnectionCanInlineWriteRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5556)
  so command publication can tell whether a single-SGE WR will be posted with
  `IBV_SEND_INLINE`. The compact-command branch in
  [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16220)
  uses that predicate only when there are no older registered command sources
  awaiting CQ retirement (`acceptedEpoch == retiredEpoch`). If both the compact
  body and ready word fit inline, the sender posts both WRs, immediately advances
  `clientSqlRemoteCommandAcceptedEpoch`, `clientSqlRemoteCommandRetiredEpoch`,
  and frontend `consumedEpoch`, and avoids allocating a command send-CQ owner.
  Commands that do not satisfy this exact all-inline predicate stay on the
  registered-source path and release frontend credit only through typed send-CQ
  retirement.
- [`TupleSinkServiceClearClientSqlCommandWriteCompletionsForSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4687)
  no longer clears active posted command owners to make teardown look clean. If
  session cleanup still sees active command owners, it is a reset-required
  prototype violation, not a safe local retry.
- The standalone Homer client now computes the same v11 command byte facts in
  [`HomerClientFinalizeCommandRecordBytes()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2018)
  before committing frontend command slots through
  [`HomerClientInitializeCommandRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2065).

Validation evidence from the no-stats runtime rebuilt, installed, and synced to
`farnet0` on June 20, 2026:

```text
remote RDMA pgbench c1 smoke:
    2000/2000 transactions, failed=0
    TPS=1158.866050, p99=0.263 ms, max=1265.960 ms
    note: this was a cold setup smoke; the max includes first-use setup cost

remote RDMA pgbench c1 warmed repeats:
    run 1: failed=0, TPS=4554.369724, p99=0.240 ms, max=6.285 ms
    run 2: failed=0, TPS=4062.262294, p99=0.277 ms, max=6.411 ms
    run 3: failed=0, TPS=4094.392116, p99=0.275 ms, max=6.285 ms

remote RDMA pgbench c4 warmed repeats:
    run 1: failed=0, TPS=11199.597262, p95=0.503 ms, p99=0.584 ms
    run 2: failed=0, TPS=11223.426434, p95=0.502 ms, p99=0.590 ms

remote RDMA basebackup:
    run 1: rc=0, real=6.19s
    run 2: rc=0, real=4.33s

local Homer blackhole basebackup:
    run 1: rc=0, real=4.15s
    run 2: rc=0, real=4.16s
```

The important Stage 4d performance result is the remote c4 recovery back to the
roughly 11k TPS band while compact command publication is enabled. Service log
tails did not show `refusing to reset`, `reset required`, or invalid-command
diagnostics during these runs. The PostgreSQL log did contain repeated Citus
maintenance-daemon warnings about `10.10.1.100:5432` because farnet0 PostgreSQL
was intentionally not running for this pgbench/basebackup validation shape; that
is not a Homer command-mailbox failure.

Remaining follow-up:

- Registered-source commands still intentionally treat the frontend source slot
  as reusable only after command send-CQ retirement. Do not relax that path
  without another ownership proof.
- The inline path is active, but the observed mix is workload dependent. Use the
  diagnostic-only `remote_command_inline_posts` and
  `remote_command_registered_posts` counters before drawing microbenchmark
  conclusions about command-kind coverage or inline capacity sensitivity.

Revalidation note, June 20, 2026: after later Stage 5a/5b working-tree changes
were present, the no-stats runtime was rebuilt, installed, and synced again from
the active `postgres-citus` and `citus-dbcomm` worktrees. This is not an isolated
Stage 4d-only binary, but it rechecks that the Stage 4d command-publication
contract still holds under the current implementation state:

```text
artifact: /tmp/homer_stage4d_validate_1781951407

remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=3514.161544, max=1310.634 ms
    run 2 warmed: failed=0, TPS=4535.251261, max=5.396 ms
    run 3 warmed: failed=0, TPS=4133.062287, max=5.460 ms
    run 4 warmed: failed=0, TPS=4120.644222, max=5.412 ms

remote RDMA pgbench c4:
    run 1: failed=0, TPS=11303.142076, max=15.711 ms
    run 2: failed=0, TPS=11279.520620, max=15.319 ms
    run 3: failed=0, TPS=11110.067999, max=15.332 ms

local Homer blackhole basebackup:
    4.01s, 4.07s, 4.06s

remote RDMA basebackup:
    warmup 5.72s; warmed 4.12s, 4.28s
```

The installed `pgbench`, `citus_tuple_sink_service`, and `libhomer_client.a`
all carried `/citus_remote_execution_control_v23`, and `git diff --check` was
clean in both repos. Current-run service/PostgreSQL log tails did not show
invalid command slots, reset-required command owner errors, CQ/source-credit
errors, `FATAL`, `PANIC`, or `ERROR`. The only matching service-tail diagnostics
were the known `stale payload progress grant` lines after payload/basebackup
cleanup, which remains a payload scheduler cleanup caveat rather than a Stage 4d
command-publication failure.

True-inline completion note, June 20, 2026: the Stage 4d-3 fast-retire path was
added on top of the accepted registered-source checkpoint. The no-stats runtime
was rebuilt, installed, and synced to `farnet0`, then restored again after a
stats-only diagnostic pass. The restored normal service binary had SHA-256
`c1eeddacaa18462edacf4e6d57d011ed5e1655d260348c36da406af60817a53b` on both
hosts and no `HOMER_SERVICE_*_STATS` marker strings.

```text
artifact: /tmp/homer_stage4d_inline_validate_1781953448

remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=2780.127758, p99=0.255 ms, max=1316.451 ms
    run 2 warmed: failed=0, TPS=4413.865186, p99=0.251 ms, max=5.544 ms
    run 3 warmed: failed=0, TPS=3947.238058, p99=0.302 ms, max=5.617 ms

remote RDMA pgbench c4:
    run 1: failed=0, TPS=11154.464798, p95=0.492 ms, p99=0.572 ms
    run 2: failed=0, TPS=10991.221312, p95=0.482 ms, p99=0.547 ms
    run 3: failed=0, TPS=10907.251550, p95=0.483 ms, p99=0.550 ms

local Homer blackhole basebackup:
    4.19s, 4.14s

remote RDMA basebackup:
    warmup 5.46s; warmed 4.31s, 4.15s

stats-only short remote c1:
    failed=0, TPS=1124.096929
    farnet0 command sender counters:
        remote_command_inline_posts=6003
        remote_command_registered_posts=8000
        remote_command_failures=0
```

This completes the planned Stage 4d command-publication work for the current
milestone: compact command records, stable backend reads, accepted-versus-retired
source ownership, typed CQ retirement for registered-source commands, and true
verbs-inline fast retirement for commands that satisfy the all-inline predicate.

Historical sequencing note: while Stage 4d was still unresolved, Stage 5a was
not allowed to proceed in the same implementation series because that would have
left the command-publication ownership bug unresolved and made future validation
harder to attribute. With the accepted inline checkpoint above, that specific
Stage 4d blocker is closed.

### Earlier-stage followups from the Stage 4d review

- **Stage 2**: typed CQ dispatch is still the right direction, but command
  body/ready partial-post reset semantics are an unfinished correctness item.
  Posted owners may be released only by CQ retirement or completed QP teardown,
  not by table cleanup. Audit the clear-on-close behavior above before
  reattempting Stage 4d.
- **Stage 3a**: nonblocking result drain should keep the visibility-pending
  behavior that avoided false FATALs, but it needs bounded escalation for stable
  repeated header mismatches. Track mismatch head bytes, published tail,
  generation, a small header fingerprint, and retry count; retry while those
  observations change and hard-error once they are stable for a bounded window.
- **Stage 3b/4a**: compact completion plus descriptor table is accepted, but the
  client still reconstructs a full legacy completion image. Long-term client APIs
  should pass hot completion plus cached descriptor reference/version rather than
  rebuilding the large legacy completion object on the hot path.
- **Stage 4b**: pre-arm currently synthesizes a full
  `CitusRemoteExecCommandCompletion` in
  [`HomerTryPrearmStableResultBinding()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3557)
  and re-enters
  [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2900).
  Replace that later with a narrow `HomerClientRebindResultSink()` helper that
  updates only descriptor version, result generation, start tail, first record
  ordinal, and consumed frontier.
- **Stage 4c**: the explicit pre-arm flag is sound, but sequence prediction at
  [`HomerClientPredictNextCommandSequence()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2127)
  relies on the current single-producer command mailbox. The cleaner Stage 4d
  API should reserve command sequence/slot first, pre-arm the exact reserved
  sequence, fill the compact command, then commit publication.

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

Failed-query wire semantics:

```text
No data published:
    publish ERROR+EOS sequence 1.

Some data published:
    discard unpublished partial batch;
    append immutable ERROR+EOS sequence N+1.
```

The failed terminal completion names that error frontier. The frontend drains or
discards through the required frontier and then applies the command failure.
Never expose partial rows as a successful result merely because they preceded
the error.

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

### Accepted Stage 5a success-path checkpoint - June 20, 2026

Stage 5a success-path tuple-result finalization is implemented and validated.
The normal row-producing SQL result path now finalizes an unpublished partial
batch as one immutable DATA+EOS record instead of submitting DATA and then
requiring a separate zero-row EOS record.

Implemented code shape:

- [`SubmitCitusTupleSinkBatchInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1662)
  is the shared publication helper for tuple-result DATA records. It fills the
  immutable transport header, including terminal flags, before advancing the
  byte-ring `publishedTail`.
- [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1726)
  remains the nonterminal DATA publication API.
- [`FinalizeCitusTupleSinkGeneration()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1865)
  is the new command/result-generation finalization API. If given an unpublished
  `terminalBatchHandle`, it publishes that batch with
  `CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS`; if there is no active batch, it
  publishes the existing compact zero-row EOS record for empty results or
  exact-full results whose final DATA record was already visible.
- [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1904)
  is now only the compatibility spelling for older callers and delegates to
  `FinalizeCitusTupleSinkGeneration(..., NULL)`.
- [`RemoteExecSqlResultFlushBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1307)
  now takes a `terminal` flag. Normal full-batch flushes call
  `SubmitCitusTupleSinkBatch()`, while
  [`RemoteExecSqlDestShutdown()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1394)
  calls `RemoteExecSqlResultFlushBatch(receiver, true)` so a final partial
  result batch becomes DATA+EOS before publication.
- Service-side tuple-view EOS synthesis remains unreachable on accepted paths:
  [`HomerServiceTupleViewEosAppendReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9375)
  returns `false`, so the service does not create a second semantic EOS delivery
  path.

Validation was run by a dedicated verification worker using a no-stats
`CPPFLAGS='-D_GNU_SOURCE'` rebuild/install/sync on both farnet hosts. The worker
also checked that installed `pgbench`, `citus_tuple_sink_service`, and
`libhomer_client.a` carried the current `/citus_remote_execution_control_v23`
string and that local/farnet0 hashes matched for `postgres`, `pgbench`,
`pg_basebackup`, `citus_tuple_sink_service`, `citus.so`, and
`libhomer_client.a`.

```text
remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=3496.377578, p95=0.230 ms, p99=0.241 ms, max=1308.432 ms
    run 2 warmed: failed=0, TPS=4540.589923, p95=0.230 ms, p99=0.241 ms, max=6.545 ms
    run 3 warmed: failed=0, TPS=4080.743218, p95=0.253 ms, p99=0.267 ms, max=6.272 ms
    run 4 warmed: failed=0, TPS=4064.814277, p95=0.255 ms, p99=0.271 ms, max=6.416 ms

remote RDMA pgbench c4:
    run 1 warmup: failed=0, TPS=11040.278524, p95=0.490 ms, p99=0.545 ms, max=18.550 ms
    run 2 warmed: failed=0, TPS=10973.398014, p95=0.490 ms, p99=0.542 ms, max=18.288 ms
    run 3 warmed: failed=0, TPS=11064.596776, p95=0.503 ms, p99=0.564 ms, max=18.388 ms
    run 4 warmed: failed=0, TPS=10989.986199, p95=0.505 ms, p99=0.579 ms, max=24.864 ms

local Homer blackhole basebackup:
    run 1 warmup: rc=0, real=4.09s
    run 2 warmed: rc=0, real=3.98s
    run 3 warmed: rc=0, real=4.05s
    run 4 warmed: rc=0, real=4.18s

remote RDMA basebackup:
    run 1 warmup: rc=0, real=5.71s
    run 2 warmed: rc=0, real=4.34s
    run 3 warmed: rc=0, real=4.22s
    run 4 warmed: rc=0, real=4.30s
```

Log checks did not find Stage 5a failure signatures in farnet1/farnet0 service
tails: no invalid tuple-result headers, EOS/PEER_CLOSED errors,
reset-required errors, basebackup errors, `ERROR`, `FATAL`, or `PANIC`.
PostgreSQL still logged expected Citus maintenance-daemon warnings for
`dbcomm@10.10.1.100:5432` because farnet0 PostgreSQL is intentionally not part
of these blackhole/RDMA validation runs.

Remaining caveats:

- The failed-query wire semantics in this section remain future work. The
  current accepted Stage 5a checkpoint covers successful empty/partial/exact-full
  result finalization. It does not yet add an explicit tuple-result
  ERROR+EOS record ABI for queries that fail after publishing some result data.
- The verifier saw four `stale payload progress grant` lines after local
  basebackup reclamation. The workloads completed cleanly, so this is not a
  Stage 5a correctness failure, but it should remain visible for later payload
  scheduler cleanup.

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

### Accepted Stage 5b bounded-grant checkpoint - June 20, 2026

Stage 5b is implemented as a bounded executor-grant cleanup, not a new fairness
policy. The implementation preserves the current scheduler ordering and
round-robin behavior, but removes one important hidden-progress shape: selected
payload egress grants no longer loop internally to post multiple RDMA batches
after the scheduler admitted one payload source.

Implemented code shape:

- [`HomerProgressBudget`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1779)
  and
  [`HomerProgressUsage`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1788)
  are the source-neutral vocabulary for bounded executor work. The current grant
  structs still carry existing source-specific fields, but executors now have
  explicit record/byte/WR/CQ-poll dimensions to report against.
- [`HomerPayloadProgressDelta`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1228)
  and
  [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2555)
  now carry `budgetExhausted`. This is currently feedback/diagnostic state, not
  a new plan-stop policy input.
- [`HomerServicePayloadProgressMarkBudgetExhausted()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11904)
  marks `stillReady` plus `budgetExhausted` when a selected payload grant stops
  because it consumed its granted coalescing window while more source data
  remains.
- [`HomerServicePumpOutgoingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20704)
  now posts at most one bounded adjacent byte-ring RDMA batch for a selected
  grant. If the source still has bytes beyond that batch, it reports
  `budgetExhausted`/`stillReady` instead of silently expanding the selected
  grant in an executor-local loop.
- [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21446)
  applies the same one-bounded-batch rule for the older slot-ring payload path.
- [`HomerProgressResultMergePayloadDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6514)
  and
  [`HomerProgressResultMergeResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6587)
  propagate `budgetExhausted` through aggregate progress results.

Validation was run by a dedicated verification worker with no-stats
`CPPFLAGS='-D_GNU_SOURCE'` rebuild/install/sync on both farnet hosts. The worker
checked matching local/farnet0 hashes for `postgres`, `pgbench`,
`pg_basebackup`, `citus_tuple_sink_service`, `citus.so`, and
`libhomer_client.a`; `pgbench`, service, and `libhomer_client.a` all carried the
current `/citus_remote_execution_control_v23` string. Raw artifacts were stored
under `/tmp/stage5b_verify_1781950851`.

```text
remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=3508.868578, p95=0.230 ms, p99=0.244 ms, max=1308.986 ms
    run 2 warmed: failed=0, TPS=4574.132276, p95=0.227 ms, p99=0.238 ms, max=5.484 ms
    run 3 warmed: failed=0, TPS=4093.401600, p95=0.252 ms, p99=0.267 ms, max=5.562 ms
    run 4 warmed: failed=0, TPS=4064.876238, p95=0.255 ms, p99=0.274 ms, max=5.534 ms

remote RDMA pgbench c4:
    run 1: failed=0, TPS=11193.194538, p95=0.483 ms, p99=0.540 ms, max=16.405 ms
    run 2: failed=0, TPS=11265.466782, p95=0.495 ms, p99=0.558 ms, max=15.330 ms
    run 3: failed=0, TPS=11038.514481, p95=0.487 ms, p99=0.543 ms, max=15.130 ms
    run 4: failed=0, TPS=11142.446146, p95=0.497 ms, p99=0.566 ms, max=15.452 ms

local Homer blackhole basebackup:
    run 1: rc=0, real=3.93s
    run 2: rc=0, real=3.89s
    run 3: rc=0, real=3.89s
    run 4: rc=0, real=3.89s

remote RDMA basebackup:
    run 1 warmup: rc=0, real=5.83s
    run 2 warmed: rc=0, real=4.29s
    run 3 warmed: rc=0, real=4.14s
    run 4 warmed: rc=0, real=4.22s
```

The important performance result is that c4 stayed around the accepted 11k TPS
band and warmed remote RDMA basebackup stayed in the recent 4.2-4.3s band, so
the one-bounded-batch grant boundary did not introduce the feared payload
throughput regression.

Log checks found no payload publish stalls, invalid tuple-result headers,
EOS/PEER_CLOSED errors, reset-required errors, basebackup errors, `ERROR`,
`FATAL`, or `PANIC` in the service tails. The same expected PostgreSQL Citus
maintenance-daemon warnings for `dbcomm@10.10.1.100:5432` remain because farnet0
PostgreSQL is intentionally not part of this validation shape.

Remaining caveat:

- The verifier again saw four `stale payload progress grant` lines after local
  blackhole basebackup reclamation. This was also observed in Stage 5a and did
  not affect correctness or throughput. It likely reflects a stack-local plan
  that still contains a payload source after the close/reclaim path has retired
  or reused the stream slot in the same service pass. Treat it as scheduler
  cleanup work for the payload lifetime/reclaim path, not as a Stage 5b
  rejection.

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
2. Cache-line isolate bitmap words to avoid replacing active-table scans with a
   new hot shared cache line:

   ```c
   typedef struct HomerReadyBitmapLine
   {
       _Alignas(64) _Atomic uint64_t bits;
       char padding[56];
   } HomerReadyBitmapLine;
   ```

   If session count exceeds 64, use an array of separately aligned words or
   shard by lane.
3. Add stable session-slot generation validation.
4. Add periodic fallback full scan.
5. Add `readyBitmapSetCalls`, `readyBitmapAlreadySet`,
   `readyBitmapExchanges`, and `readyBitmapFallbackDiscoveries`.
   `readyBitmapFallbackDiscoveries` should normally remain zero; the already-set
   ratio shows whether bitmap atomic traffic is excessive.
6. Have the Stage 2 CQ dispatcher set typed ready facts directly for send-CQ
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

### Stage 6a command-ready bitmap checkpoint - June 20, 2026

Stage 6a implements only the remote client-SQL command-ready bitmap. Completion
and resource-pressure bitmap lines are present in the ABI as reserved follow-up
storage, but no producer drives them yet.

Implemented code shape:

- The control protocol is now v24 at
  [`CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:28),
  with v24 control/client-completion/peer-completion shared-memory names. The
  protocol adds
  [`CitusRemoteExecOpenSessionResponse.serviceSessionIndex`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:376)
  so producers use the fixed session-table index directly instead of inferring
  it from opaque `serviceSessionId`.
- [`CitusRemoteExecReadyBitmapLine`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:960)
  cache-line isolates each 64-session bitmap word, and
  [`CitusRemoteExecControlRegion.commandReadySessions`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:973)
  carries the command-ready word. The same ABI block also reserves
  `completionReadySessions` and `resourcePressureSessions` for later slices.
- The client stores the returned fixed session index in
  [`HomerClientSession.serviceSessionIndex`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:66),
  validates it in
  [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1201),
  and clears it on session close.
- [`HomerClientMarkCommandReady()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:350)
  sets the command-ready bit only after
  [`HomerClientStartDirectCommand()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2256)
  has release-stored the command slot `readySeq` and mailbox
  `publishedEpoch`. The bit is a scheduler hint; the service still validates
  mailbox epochs before forwarding.
- [`TupleSinkServiceSetOpenSessionIdentity()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3529)
  fills both the opaque service id and explicit session index in every
  open-session response.
- [`HomerServiceAppendRemoteClientSqlCommandBitmapSources()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9489)
  consumes command-ready bits with one `atomic_exchange`, validates each named
  session with the normal command-mailbox readiness predicate, and appends the
  direct `REMOTE_CLIENT_SQL_COMMAND` source. If the ready set overflows after
  the exchange, it re-sets the unappended bits so ready work is not lost.
- [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9916)
  now uses the bitmap helper when the progress registry is initialized; the old
  broad command-source scan remains only in the non-registry fallback path.
- [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16025)
  re-sets the command-ready bit when a session still has more published commands
  after forwarding one command, preserving the lost-wakeup protocol across
  multi-ready mailboxes.

Validation evidence from the no-stats runtime rebuilt, installed, and synced to
`farnet0` on June 20, 2026:

```text
artifact: /tmp/homer_stage6a_validate_1781952600

installed string check:
    pgbench, citus_tuple_sink_service, and libhomer_client.a all carry
    /citus_remote_execution_control_v24

remote RDMA pgbench c1:
    warmup:   failed=0, TPS=3400.417911, p99=0.252 ms, max=1314.104 ms
    measured: failed=0, TPS=4405.278581, p99=0.246 ms, max=5.583 ms

remote RDMA pgbench c4:
    warmup:   failed=0, TPS=11020.070027, p95=0.494 ms, p99=0.565 ms
    measured: failed=0, TPS=11170.338729, p95=0.485 ms, p99=0.560 ms

local Homer blackhole basebackup:
    rc=0, real=4.21s

remote RDMA basebackup:
    warmup: rc=0, real=5.48s
    measured: rc=0, real=4.35s
```

The service-log tail scan on both hosts did not show reset-required, stash,
send-CQ drain, fallback, `ERROR`, `FATAL`, or `PANIC` patterns for this run.
The first c1 warmup retained the expected cold setup outlier; warmed c1/c4 and
remote basebackup are in the previously accepted performance band.

Independent clean verifier run:

```text
artifact: /tmp/homer_stage6a_verify_clean_1781952680

build/deploy:
    no-stats Citus/Homer rebuild with CPPFLAGS='-D_GNU_SOURCE'
    Postgres postgres, pgbench, and pg_basebackup rebuilt/installed
    /data/dbcomm/pg-citus synced to farnet0
    local and remote pgbench, citus_tuple_sink_service, and libhomer_client.a
    all carry /citus_remote_execution_control_v24
    no HOMER_SERVICE_PROGRESS_STATS marker in checked installed artifacts
    checked installed artifacts are dbcomm:dbcomm on both hosts

remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=3419.88, max=1315.539 ms
    run 2 warmed: failed=0, TPS=4446.65, max=5.536 ms
    run 3 warmed: failed=0, TPS=3992.42, max=6.071 ms
    run 4 warmed: failed=0, TPS=3990.77, max=6.077 ms

remote RDMA pgbench c4:
    run 1: failed=0, TPS=11124.30, max=15.563 ms
    run 2: failed=0, TPS=10967.25, max=15.381 ms
    run 3: failed=0, TPS=11062.15, max=15.564 ms
    run 4: failed=0, TPS=11004.21, max=15.207 ms

basebackup:
    local Homer blackhole: 4.02s, 4.02s, 4.17s, all rc=0
    remote RDMA: 5.94s warmup, then 4.21s and 4.16s, all rc=0
```

The clean verifier excluded an earlier contaminated attempt where another
validation shell issued `pg_ctl stop`. Its current log scan found no invalid
command slot, reset-required, source-credit/reuse, CQ, `FATAL`, `PANIC`, or
`ERROR` hits. It did find the already-known PostgreSQL maintenance warnings for
`10.10.1.100:5432` while farnet0 PostgreSQL was intentionally stopped, plus
three `stale payload progress grant` cleanup lines after basebackup.

Limitations and follow-up:

- This checkpoint removes the remote client-SQL command-source active-table scan
  from the normal registered-policy path only. Completion visibility,
  send-resource pressure, recv-CQ doorbells, and payload ready facts still need
  their own typed producers in later Stage 6 work.
- The no-stats performance run did not collect
  `commandReadyBitmapFallbackDiscoveries`. The counter path is present behind
  `HOMER_SERVICE_PROGRESS_STATS` in
  [`HomerServiceLogProgressStats()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6089),
  but a separate stats-enabled diagnostic pass is still needed if the next
  review requires direct proof that fallback discoveries remain zero.

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

### Accepted Stage 7a checkpoint - June 20, 2026

Stage 7a is implemented and validated. The implementation intentionally keeps
the physical producer byte-ring geometry conservative: each reservation still
requires enough contiguous space for the maximum basebackup semantic header, so
a record cannot straddle the local producer-ring wrap. Within that physical
reservation, however, the producer now exposes payload bytes immediately after
the actual semantic header for the next object kind. This gives compact
chunk/end records without adding a payload copy or a risky wrap-time relocation.

Implemented code shape:

- The basebackup semantic protocol is now v2 at
  [`CITUS_REMOTE_BASEBACKUP_PROTOCOL_VERSION`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:67).
  [`CitusRemoteBaseBackupMessageHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:417)
  carries authoritative `headerBytes` and `nameBytes`; the fixed semantic
  prefix is
  [`CITUS_REMOTE_BASEBACKUP_HEADER_FIXED_BYTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:433),
  while
  [`CITUS_REMOTE_BASEBACKUP_HEADER_MAX_BYTES`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:434)
  remains the worst-case physical reservation size.
- The client reserve API now has an object-aware path,
  [`HomerClientReserveBaseBackupRecordForObject()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1762),
  also declared for PostgreSQL callers at
  [`remote_execution_client.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:324).
  It records the reserved semantic header length and returns a payload pointer
  at `[transport header][actual semantic header]`, not after the legacy
  maximum header.
- [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1896)
  rejects reserve/submit shape mismatches, publishes
  `transportHeader->payloadBytes = headerBytes + payloadBytes`, writes
  `nameBytes` only for named object-boundary records, and clears repeated
  `totalBytes` for archive/manifest chunks.
- PostgreSQL's Homer bbsink now reserves by the next exact object kind in
  [`bbsink_homer_reserve_record()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:184).
  Archive/manifest chunks and end markers pass `name = NULL` through
  [`bbsink_homer_archive_contents()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:290),
  [`bbsink_homer_end_archive()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:305),
  [`bbsink_homer_manifest_contents()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:335),
  and
  [`bbsink_homer_end_manifest()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:349),
  so chunks no longer copy or retransmit the archive/manifest name.
- The service validates compact basebackup headers through
  [`HomerServiceValidateBaseBackupSemanticHeader()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20409)
  and
  [`HomerServiceBaseBackupHeaderNameShapeValid()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20353).
  The fragment path
  [`HomerServiceAppendOutgoingBaseBackupFragmentWrite()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13121)
  now forces the first fragment to carry the actual semantic header length, and
  [`HomerServicePayloadMinimumBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22832)
  uses the fixed basebackup prefix rather than the old max header.
- Payload stats now record both compact and legacy-equivalent semantic header
  bytes in
  [`HomerServiceRecordBaseBackupShapeStats()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2953).

Validation evidence from `/tmp/homer_stage7a_validate_1781954660`:

```text
no-stats installed hashes matched on farnet1 and farnet0:
    citus_tuple_sink_service 4a0718892cdd901c863c6b87ab2bdf8a429a0b4c74c9765f57b941875a9ba405
    libhomer_client.a       e812b1dfca05e1b63814176def39e53729622c9159d4ca1edb59b75fe62dff4a
    citus.so                c5bc80d3e261ad07181d3c6975171aa7077fd273507fc0a9c4931b3d30899156

remote RDMA pgbench c1:
    run 1 warmup: failed=0, TPS=2830.958112, p99=0.240 ms
    run 2 warmed: failed=0, TPS=4501.610901, p99=0.239 ms
    run 3 warmed: failed=0, TPS=4075.300136, p99=0.270 ms

remote RDMA pgbench c4:
    run 1: failed=0, TPS=11647.765740, p95=0.455 ms, p99=0.524 ms
    run 2: failed=0, TPS=11580.055092, p95=0.453 ms, p99=0.521 ms
    run 3: failed=0, TPS=11522.834802, p95=0.453 ms, p99=0.526 ms

local Homer blackhole basebackup:
    4.07s, 3.94s

remote RDMA basebackup:
    warmup 5.83s; warmed 4.13s, 4.12s

stats-enabled remote RDMA basebackup:
    semantic_header_bytes=370753
    legacy_header_bytes=1218080
    kind_objects[archive_chunk]=6613
    kind_payload_bytes[archive_chunk]=23314645504
```

The stats run proves the Stage 7a byte-volume acceptance condition: compact
basebackup semantic headers used about `30.4%` of the legacy-equivalent header
bytes for that stream. The no-stats build was restored and synced afterward;
`strings citus_tuple_sink_service | grep -c 'basebackup shape stats'` returned
`0` on both hosts.

Validation caveats:

- The validation script initially used a broad `pkill -f` pattern that matched
  its own shell command text. The validator switched to shorter process checks;
  no source/runtime code changed because of this.
- Service logs still showed two known `stale payload progress grant` diagnostics
  after local blackhole stream reclamation. They did not correlate with any
  workload failure and are still a payload scheduler cleanup caveat rather than
  a Stage 7a semantic-header failure.
- PostgreSQL logs again contained Citus maintenance-daemon warnings about
  `10.10.1.100:5432` because PostgreSQL was intentionally not running on
  `farnet0` for this validation shape. No Homer protocol/header/transport errors
  were found in the service logs.

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

Wrap cases:

- Local source-ring wrap can use multiple SGEs to gather into one contiguous
  remote range if the NIC supports the required `max_sge`.
- Remote destination-ring wrap requires multiple RDMA WRITE WRs because remote
  addresses are discontinuous.
- If both source and destination wrap, use multiple WRs, each with one or more
  SGEs.
- The final remote-tail WIMM is posted after all data WRs.
- Query `max_sge` and `max_send_wr` during connection setup. Reject unsupported
  geometry or fall back to the registered staging path.

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

Acceptance uses median warmed repeats, not individual best/worst runs:

```text
c4 median TPS:
    no worse than 3% below previous accepted stage

c1 median TPS:
    no worse than 5% below previous accepted stage

p99:
    no worse than 5-10%, depending on measured variance

basebackup median wall time:
    no worse than 3-5%
```

Credit an improvement only when it survives at least three warmed repeats and
exceeds ordinary variance.

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
  completion ABI migrations. Stage 3b plus Stage 4a form the single deliberate
  completion protocol deployment unit.
- Do not revive the old descriptor-heavy completion layout as a fallback for
  descriptor-table exhaustion in measured paths. Fail explicitly and size the
  first descriptor table for the benchmark shapes.

## Open questions

- Whether the same-QP DATA/EOS/completion-ready sequence is worthwhile as a
  foreground-only fast path after the required-frontier design is correct.
- Whether payload WR-IDs should keep semantic frontier encoding permanently or
  move to owner-array indexes once the lane-owned dispatcher is in place.
- For Stage 4a, descriptor publication is frontend-side-service-owned. The
  remaining future question is whether true zero-copy result production should
  later move descriptor construction or producer-slot ownership closer to the
  backend/frontend library.
- Whether direct registration of producer byte rings is enough for basebackup or
  whether Stage 7d QP/CQ isolation is necessary for foreground p99.
