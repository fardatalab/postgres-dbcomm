# Homer Transport Hot-Path Optimization Plan

## Scope

- **What this doc explains**: staged optimization work for the Homer transport
  hot paths after the peer-client completion publication and tuple-result
  generation correctness repairs: CQ ownership cleanup, completion/command
  metadata shrinkage, result-payload/completion overlap, scheduler budget
  granularity, basebackup specialization, and measured acceptance gates.
- **What this doc does NOT cover**: new Homer user-visible functionality, the
  full guarded-action scheduler redesign, DOCA/DPU offload, or the service-to-
  service peer push-completion protocol beyond the shared CQ/lifetime pieces.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The current tree has recovered correctness for the remote client-SQL pgbench
path and now validates near the earlier c4 target, but the accepted path still
carries optimization debt that is visible in the hot path. The main theme is
that the corrected semantics are sound, but some fixes realize those semantics
with oversized metadata, extra events, object-level scheduler grants, shared-CQ
stash/replay behavior, and conservative completion/result ordering.

This plan is intentionally separate from
[`homer_unified_aggregate_action_scheduler_plan.md`](homer_unified_aggregate_action_scheduler_plan.md).
The scheduler redesign should consume cleaner transport facts later; this note
focuses on reducing current transport work while preserving the correctness
invariants established by the completion-publication and tuple-result generation
repairs.

## Key code pointers

- [`CitusRemoteExecCommandCompletion`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:449)
  embeds command hot state plus result queue and tuple-contract metadata.
- [`CitusTupleViewContract`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:166)
  is the fixed-capacity tuple-view contract that makes row-result completion
  records large.
- [`HomerClientCopyCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:923)
  currently avoids copying the large descriptor unless result flags require it,
  but the ABI still carries the large cold object.
- [`HomerClientCopyClientCompletionSlotStable()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:977)
  implements the current slot-local ready-epoch stable-read gate.
- [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2500)
  now uses a peek/apply/ack shape and acknowledges exactly the leased completion
  event after command identity validation.
- [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904)
  still has a terminal-drain loop with retry behavior; a later optimization
  should make incomplete payload frontier a nonblocking result, not a busy wait.
- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13072)
  publishes backend-node client-SQL completions into the peer frontend mailbox
  and still waits for tuple-result EOS/send dependencies before terminal
  completion visibility.
- [`TupleSinkServiceReservePeerClientCompletionPublishCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3668)
  reserves a global outstanding-table entry for peer-client completion source
  retirement.
- [`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3840)
  scans the global completion-publish table to retire a same-connection prefix.
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1840)
  decodes peer-client completion publish CQEs, but stashes them when the current
  callback set cannot consume that namespace.
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1985)
  replays stashed CQEs before polling new CQEs.
- [`TupleSinkServiceEncodePeerCommandWrId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1425)
  and
  [`TupleSinkServiceEncodePeerClientCompletionPublishWrId()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1472)
  show the current tagged WR-ID namespace that a lane-owned dispatcher should
  decode directly.
- [`HomerServiceBuildSessionMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26880)
  still rebuilds session machine facts by scanning active session slots.
- [`HomerServiceBuildPayloadMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27075)
  computes payload readiness and contains the local-blackhole admission fix at
  [`tuple_sink_service_process.c:27101`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27101).
- [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:380)
  now carries `resultGeneration` and `generationSequence` for immutable
  tuple-result identity.
- [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532)
  is current service-side EOS repair/scaffolding that should disappear from the
  optimized tuple-result model.

## Current behavior and grounded findings

- Current accepted validation after the generation/EOS and peek/apply/ack fixes
  is: remote c1 correct at about `4.17k` and `3.76k TPS`; remote c4 correct at
  about `10.47k` and `10.39k TPS`; local blackhole basebackup `10.44s`; remote
  RDMA basebackup warmed repeats `4.43s` and `4.32s`.
- Older `>11k TPS` remote c4 results remain the performance recovery target, but
  the implementation that exposed those results was not accepted for long-run
  correctness. This plan must preserve the current correctness band before
  chasing the old peak number.
- The generation and immutable-EOS repair should not inherently be expensive.
  The likely costs are how the corrected model is represented: large completion
  and command records, extra STARTED/EOS events, serialized terminal completion,
  fine-grained scheduler grants, global CQ owner scans, and extra producer-to-
  RDMA-source copies.
- CQ stashing has not been eliminated. The current connection state still has
  stashes for payload sends, command sends, and peer-client completion publish
  CQEs in
  [`TupleSinkServicePeerConnectionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:504).
- Basebackup shares the byte-ring transport substrate with SQL tuple results,
  but it should not inherit SQL-result command-generation lifecycle. Its
  optimized model is one backup stream generation per backup session, monotonic
  transport record ordinals, object/archive metadata as semantic payload, and a
  single final backup-stream EOS.

## Design qualifications to preserve

1. **FIFO queues are for physical ownership, not semantic policy.** Per-QP FIFOs
   should retire source slots and WR ownership in post order. Scheduler policy
   still decides which semantic work to run next. Recv-CQ immediates should be
   parsed into typed ready facts or direct state updates; do not make a generic
   FIFO of doorbells the semantic scheduler.
2. **The CQ dispatcher should be direct, not a generic callback framework.** The
   target hot path is one batched `ibv_poll_cq`, a WR-ID tag switch, generation
   validation, direct array/FIFO access, and typed counter updates. Avoid hash
   maps, vtables, linked lists, and callback-absent stash behavior in the steady
   state.

## Staged implementation plan

### Stage 0: preserve baseline and measurement guardrails

Goal: make every later optimization accept/reject decision against the same
correctness and performance surface.

Substeps:

1. Record the exact Postgres and Citus commits, dirty-state summary, build flags,
   service binary hashes, and farnet lane addresses before each comparison.
2. Add or verify compile-gated counters for:
   `completionEventsPublished`, `completionEventsAcked`, `completionBytesPosted`,
   `commandBytesPosted`, `resultDataRecordsPosted`, `resultEosRecordsPosted`,
   `startedCompletionsPublished`, `payloadFrontierWaits`,
   `terminalCompletionPayloadWaits`, `sendCqPolls`, `sendCqCqes`,
   `sendCqEmptyPolls`, `sendCqStashHits`, `sendCqRetiredOwnerRefs`,
   `schedulerCandidatesBuilt`, `schedulerGrantsIssued`, and
   `schedulerReadyMachinesScanned`.
3. Keep stats builds out of performance numbers. Use stats only to explain a
   rejection or to compare relative mechanism counts.
4. Maintain a small performance history table in this doc or the implementation
   note after each completed stage.

Acceptance:

- Remote c1/c4 pgbench correctness matches the current accepted state.
- Remote c4 no-stats warmed runs remain in the current `~10.3k-10.5k TPS` band
  before optimizations are credited.
- Basebackup local blackhole and remote RDMA warmed runs still pass.

### Stage 1: isolate basebackup from SQL-result lifecycle

Goal: keep the immutable byte-ring transport identity shared, but prevent
transaction-focused generation/EOS work from shaping the basebackup hot path.

Substeps:

1. Audit basebackup record headers and service validation so basebackup uses a
   stream-local generation/ordinal model rather than command-local SQL result
   generation.
2. Confirm basebackup does not depend on STARTED/terminal command-completion
   result events, tuple-view EOS repair, or result-drain client behavior.
3. Split basebackup counters from SQL tuple-result counters:
   `basebackupRecordsPosted`, `basebackupBytesPosted`,
   `basebackupObjectsPosted`, `basebackupEosPosted`,
   `basebackupRecordsPerWimm`, `basebackupBytesPerWimm`, and
   `basebackupSendCqRetireBatches`.
4. Record which generation checks are transport-level and which are SQL-result
   specific. Basebackup should pay only the former in the hot path.

Acceptance:

- Local blackhole basebackup and remote RDMA basebackup remain correct.
- Basebackup warmed time does not regress from the current `~4.3s-4.4s` remote
  RDMA band.
- Basebackup code no longer needs to reason about SQL result STARTED/terminal
  lifecycle or tuple-view EOS repair.

### Stage 2: lane-owned send-CQ dispatcher and per-QP owner FIFOs

Goal: make the physical send CQ the owner of CQ polling and eliminate global
owner scans plus callback-absent CQE stashing.

Substeps:

1. Introduce a lane/QP-owned send-CQ dispatcher API. The dispatcher must decode
   every WR-ID namespace that can appear on that CQ: command-send, peer-client
   completion publish, payload-send, and control/bootstrap.
2. Add fixed owner arrays and per-QP FIFOs. A peer-client completion publish
   owner ref should carry at least `{qpGeneration, postOrdinal, sessionIndex,
   sourceSlotIndex, sourceGeneration}`. Command-send owner refs should mirror
   that shape for command source slots.
3. Change peer-client completion publish retirement from the global scan in
   [`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3840)
   to FIFO prefix retirement: pop while `head.postOrdinal <= checkpointOrdinal`
   and the QP generation matches.
4. Move command-send retirement to the same FIFO pattern after peer-client
   completion retirement is stable.
5. Fold payload and control send-CQ handling into the same dispatcher. Payload
   WR-IDs may continue to encode semantic frontier directly until a payload owner
   array is needed.
6. Remove `stashed*SendCompletion*` steady-state paths and replace callback-
   absent handling with dispatcher-owned direct handling. Unknown WR-ID tags and
   stale generations should be hard diagnostic failures.

Acceptance:

- No CQE stash hits occur in the no-stats-equivalent diagnostic build.
- Peer-client completion publish retirement cost is proportional to owner refs
  actually retired, not to the global table size.
- Remote c1/c4 and basebackup correctness remain unchanged.
- c4 TPS is not allowed to regress; improvement is expected but not required
  until the metadata and payload-overlap stages land.

### Stage 3: terminal completion and payload-frontier overlap

Goal: remove over-serialization between result payload source retirement and
terminal command completion publication.

Substeps:

1. Change terminal completion semantics from "payload send CQ retired" to
   "terminal completion names the required payload frontier." The completion
   should carry `resultGeneration`, `requiredResultPublishedTail`, and the EOS
   record ordinal/frontier needed for command application.
2. Make the client-side result drain nonblocking. Replace terminal busy-wait in
   [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904)
   with statuses such as `WOULD_BLOCK`, `PROGRESS`, `EOS`, and `ERROR`.
3. In pgbench integration, lease the terminal completion, attempt result drain,
   and ACK/apply the completion only when the required frontier is visible. A
   missing frontier should yield/retry instead of spinning in the client helper.
4. Remove terminal publication waits that are only protecting local source
   buffer retirement. Source-slot lifetime belongs to the lane CQ dispatcher;
   payload visibility belongs to the required frontier.
5. Keep same-QP payload/completion ordering as an optional future fast path for
   tiny SQL results, not as the correctness contract.

Acceptance:

- A terminal completion can become visible before the sender has locally retired
  the payload source WR, but the frontend does not apply/ACK it until the named
  payload frontier is readable.
- Header-mismatch retry loops are not part of normal terminal drain behavior.
- Remote c1/c4 correctness remains stable; c4 should move closer to or exceed
  the historical `~11k TPS` band if payload/completion serialization was a
  dominant cost.

### Stage 4: hot/cold split for completion and command metadata

Goal: stop transmitting and copying large cold descriptors for ordinary command
and completion events.

Substeps:

1. Split command completion into a compact hot record and a descriptor identity.
   The hot record should contain command sequence/kind/state, result generation,
   required payload frontier, row/insert counts, scalar result fields, flags,
   and descriptor ID/version.
2. Move queue descriptor and tuple contract into an immutable descriptor table.
   Publish or update the descriptor only when result shape, queue geometry, or
   protocol version changes.
3. Remove unconditional STARTED result events for stable descriptor cases.
   pgbench's one-row SELECT should normally need payload visibility plus one
   terminal completion, not a STARTED descriptor event plus terminal completion.
4. Audit command publication with the same hot/cold split. The RDMA write length
   should be `sizeof(envelope) + actual payload bytes`, not a maximum-size
   command slot, for small commands such as COMMIT.
5. Cache tuple-view contracts or shape fingerprints in the persistent backend
   session so
   [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:688)
   and the backend bridge contract builders do not clear and copy the full
   fixed-capacity contract per stable SELECT shape.

Acceptance:

- Measured completion bytes per pgbench transaction drop sharply.
- STARTED completion count for stable pgbench SELECT shape goes to zero or near
  zero.
- Remote c1/c4 correctness remains stable; c1 latency should improve because
  fewer cache lines and DMA bytes are touched per transaction.

### Stage 5: immutable EOS batching and scheduler work budgets

Goal: keep immutable result records while avoiding an extra scheduler/RDMA
round trip for common tiny results.

Substeps:

1. Add a trailing-batch policy in the producer: keep the final partial batch
   unpublished until query shutdown when possible, mark it DATA+EOS before first
   publication, and publish it once.
2. For exact-full final batches or genuinely empty results, append a compact
   EOS-only record. Do not mutate already-published records.
3. Make the service batch adjacent DATA and EOS records under one payload
   frontier publication when a separate EOS record is necessary.
4. Replace object-count-only scheduler grants with bounded work budgets:
   `maxRecords`, `maxBytes`, `maxWrs`, and `maxCqPollBatches`.
5. For foreground SQL, drain all ready records for the selected result generation
   up to a small byte budget and include EOS in the same grant when adjacent.
6. For basebackup, use larger byte/WR budgets when foreground work is absent and
   account fairness with deficit/credit instead of limiting bulk to one or two
   records.

Acceptance:

- One-row SELECT normally produces one DATA+EOS record rather than DATA plus
  separate EOS.
- Payload WIMM/frontier publications per transaction drop without weakening
  immutable record semantics.
- Basebackup throughput does not regress when foreground SQL is absent.

### Stage 6: incremental ready structures and scheduler scan reduction

Goal: remove repeated rediscovery of idle work without taking on the full
guarded-action redesign yet.

Substeps:

1. Add ready-session bitmaps for frontend command rings and backend completion
   rings.
2. Add an intrusive or indexed ready list for payload streams with producer
   backlog, remote-credit blockage, or send-CQ retirement pressure.
3. Feed lane-CQ dispatcher typed counters directly into ready facts instead of
   rebuilding them from broad scans.
4. Keep the planner bounded: reason over ready machines and resource-pressure
   machines, not the entire active table every pass.
5. Preserve the existing machine/candidate API until the larger unified
   guarded-action scheduler replaces it.

Acceptance:

- `schedulerReadyMachinesScanned / transactions` and
  `schedulerCandidatesBuilt / transactions` drop materially.
- Remote c4 performance improves or stays flat; no correctness regression in
  c1/c4/basebackup.

### Stage 7: basebackup bulk-data optimization

Goal: optimize basebackup as a bulk byte-stream workload after the shared
transaction hot path is stable.

Substeps:

1. Split basebackup semantic metadata from chunks:
   `BACKUP_OBJECT_BEGIN`, `BACKUP_OBJECT_CHUNK`, `BACKUP_OBJECT_END`, and one
   final backup-stream EOS.
2. Stop retransmitting fixed archive names or object metadata in every chunk.
3. Batch adjacent chunks under one remote-tail WIMM and use periodic signaled
   checkpoints.
4. Direct-register stable producer byte rings and post RDMA directly from them
   with one or two SGEs around wrap. This is the larger direct-registration
   optimization and should not be mixed into the first basebackup cleanup.
5. Separate replication/bulk QP/CQ work from foreground completion work if
   measurements show foreground interference.

Acceptance:

- `sourceCopiesPerGiB` drops once direct registration lands.
- `bytesPerWimm` and `bytesPerSignaledCqe` increase for remote RDMA basebackup.
- Foreground pgbench under background basebackup has lower p95/p99 interference
  than the pre-optimization baseline.

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

## Validation matrix

Every stage must run at least:

- remote RDMA pgbench c1 correctness smoke and warmed performance repeat.
- remote RDMA pgbench c4 correctness and warmed performance repeat.
- local Homer blackhole basebackup.
- remote RDMA basebackup warmup plus warmed repeats.

When a stage changes scheduler fairness, CQ dispatch, or bulk payload behavior,
also run foreground pgbench with background remote RDMA basebackup and compare
p95/p99 plus basebackup wall time against the previous accepted stage.

Use no-stats builds for performance acceptance. Stats builds are diagnostic
only, and a run after timeout, interrupted cleanup, or stale process detection is
not an acceptance run.

## Performance history

| Stage | State | Evidence |
| --- | --- | --- |
| Baseline after generation/EOS and peek/apply/ack fixes | accepted current floor | remote c1 about `4.17k` and `3.76k TPS`; remote c4 about `10.47k` and `10.39k TPS`; local blackhole basebackup `10.44s`; remote RDMA basebackup warmed `4.43s` and `4.32s` |
| Historical rough performance ceiling | correctness not accepted | earlier remote c4 around `11k TPS`; preserve as recovery target, not as correctness baseline |

## Open questions

- Whether the same-QP DATA/EOS/completion-ready sequence is worthwhile as a
  foreground-only fast path after the required-frontier design is correct.
- Whether payload WR-IDs should keep semantic frontier encoding permanently or
  move to owner-array indexes once the lane-owned dispatcher is in place.
- Whether descriptor publication should be service-owned, backend-owned, or
  frontend-library-owned when moving toward true zero-copy result production.
- Whether direct registration of producer byte rings is enough for basebackup or
  whether the bulk path also needs separate QPs/CQs to isolate foreground p99.
