# Homer Payload and Control Unification Plan

## Summary

The next unification milestone should clean up the Homer substrate around two
planes:

- the byte-ring payload stream lifecycle shared by tuple COPY, client-SQL tuple
  results, and basebackup
- the peer command/control lifecycle used by tuple COPY and client SQL remote
  execution

This is not a DB-semantic adapter rewrite. Tuple encoding, worker tuple
insertion, basebackup object production, and SQL command semantics should remain
specialized. The target is to make shared RDMA, stream lifecycle, peer-control,
and scheduler-facing facts explicit enough that bidirectional payload streams can
be scheduled without hard-coding "COPY", "basebackup", or another DB workload
name into the scheduler.

## Current Shape

The implementation already has a neutral payload container:
[`HomerPayloadObjectFamily`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:823)
separates tuple-view batches, basebackup streams, and future WAL streams, while
[`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:833)
holds the shared queue, RDMA, frontier, completion, ACK, and traffic-scheduling
state.

The byte-ring predicate already treats basebackup and tuple-view batches as the
same transport shape:
[`HomerServicePayloadStreamUsesByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2773).
The sender ultimately posts byte-ring RDMA batches through
[`TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5818),
and receiver credit is returned through
[`TupleSinkServicePostPeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6120).

The remaining problem is not that every workload has a separate RDMA substrate.
The problem is that names, lifecycle boundaries, and scheduler facts still carry
history from the original tuple-sink and basebackup-specific implementations.

## Workstream 1: Generic Byte-Ring Payload Names and Boundaries

Goal: make the shared byte-ring path read like a shared payload path, not a
basebackup path reused by tuple COPY.

Implementation checklist:

- Rename
  [`HomerServicePumpOutgoingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13349)
  to a generic byte-ring payload sender name. This is done; the old
  `HomerServicePumpOutgoingByteRingBaseBackup()` symbol is gone.
- Rename
  [`HomerServicePumpIncomingByteRingPayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15259)
  to a generic byte-ring payload receiver name. This is done; the old
  `HomerServicePumpIncomingByteRingBaseBackup()` symbol is gone.
- Keep the object-family branches explicit:
  - tuple-view batch receive is service-passive; the peer writes into the
    backend-visible byte ring and the worker/frontend consumer advances
    `consumedHead`
  - basebackup receive is service-active; the service parses/counts/blackholes
    archive and manifest objects
  - future WAL can be added as another object-family branch
- Update comments and KB links so the shared path is described as "byte-ring
  payload" rather than "basebackup byte-ring".
- Keep wrapper aliases only if needed to reduce patch risk during the first
  cleanup; remove them before calling the workstream complete.
- Audit stats/log text around receiver doorbells, receiver-head ACK publishes,
  and payload completion logs so benchmark artifacts do not keep using
  misleading basebackup-only names for tuple-view traffic.

This should be a mostly mechanical cleanup with low behavior risk, but it is a
prerequisite for the later lifecycle cleanup because stale names hide shared
invariants.

Settlement status: no major design question remains here. The only judgment call
is patch shape: a direct rename is preferable if the diff stays reviewable;
temporary wrappers are acceptable only as a short-lived staging aid.

Implementation status: complete as of June 6, 2026. The code uses direct renames
without wrapper aliases, and the shared comments now describe the path as
byte-ring payload. The outgoing byte-ring helper keeps basebackup-only behavior
inside explicit basebackup object-family branches, while tuple-view records are
forwarded as already-built transport records. The incoming byte-ring helper
documents the key boundary: tuple-view receive is service-passive and only
returns receiver credit; basebackup receive is service-active and parses/
blackholes basebackup records before returning credit.

Validation evidence for this completion:

- Build/install: `make -j8 service-bin client-bin` passed when run as `dbcomm`;
  `sudo -n make install-headers install-service-bin install` installed the
  rebuilt service/client/extension artifacts; the installed prefix was synced to
  `farnet0` with `rsync --rsync-path='sudo -n rsync'`.
- Citus backend-to-backend Homer COPY, using 10M rows,
  `citus.experimental_tuple_sink_batch_tuple_target=64`, and
  `citus.experimental_tuple_sink_slot_capacity_bytes=4096`, passed one warmup
  and three measured runs. Artifact directory:
  `/tmp/homer_b2b_copy_10m_w1_1780777681`. Times were warmup `8.35s`, then
  `5.39s`, `5.12s`, and `5.28s`; every run returned
  `count=10000000`, `min=1`, `max=10000000`, and `sum=500000050000000`.
- Remote RDMA Homer pgbench passed c1 and c4 correctness/performance smokes.
  Warmed c1 artifact `/tmp/homer_pgbench_w1_warm_1780777767.log` processed
  `20000/20000` transactions with zero failures, `4647.68 TPS`, and p99
  `0.235 ms`. C4 artifact `/tmp/homer_pgbench_c4_w1_1780777783.log` processed
  `40000/40000` transactions with zero failures, `11441.05 TPS`, and p99
  `0.549 ms`.
- Remote RDMA Homer basebackup blackhole passed one warmup and two warmed
  repeats. Artifact directory `/tmp/homer_basebackup_w1_1780777797` reported
  `5.88s`, `4.58s`, and `4.56s`.
- Concurrent remote RDMA Homer pgbench plus background remote RDMA basebackup
  passed. Artifact directory `/tmp/homer_concurrent_w1_1780777826` reported
  pgbench `10000/10000` transactions with zero failures, `3557.19 TPS`, p99
  `0.467 ms`, while basebackup completed in `4.71s`.
- Post-run process/log sanity: only the intended PostgreSQL postmaster and
  Homer service remained on each host, and both service logs had no matching
  `failed`, `timeout`, `REM_ACCESS`, `transport broken`, `payloadTransportBroken`,
  or `ERROR` lines.

## Workstream 2: Shared Payload Lifecycle Invariants

Goal: make stream lifetime rules generic across tuple COPY and basebackup:

```text
open -> peer bind -> register/mirror -> publish -> ACK/credit -> EOS -> terminal drain -> reclaim
```

Implementation checklist:

- Centralize and document peer-bind initialization in
  [`HomerServiceBindPeerToPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11569).
- Treat sender-head mirror lifetime, receiver-head ACK completion retirement,
  source byte release, and terminal EOS as byte-ring invariants rather than
  object-family-specific fixes.
- Keep terminal close/reclaim behavior visible in
  [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16031),
  but factor any repeated checks into helpers whose names describe the generic
  invariant.
- Preserve the important tuple passive-receiver rule: for tuple-view byte-ring
  receive, the service returns credit but does not parse or copy tuple records.
  The worker path under
  [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:440)
  remains the DB-semantic consumer.
- Add or update stream-state comments around sender frontiers, receiver-head ACK
  frontiers, and transport scheduling metadata in
  [`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:833)
  so ownership is clear:
  - sender-side publication and completion release source bytes/slots
  - receiver-side consumed-head ACKs return peer credit and later retire local
    source-word WRs
  - terminal close waits for the right side of the stream to drain before
    reclaiming peer binding and local slot ownership
- Factor only invariant checks that are repeated or easy to get wrong. Do not
  split the hot sender/receiver loops into tiny helpers just for naming; byte-ring
  hot-path readability and cache behavior matter more than cosmetic factoring.

The motivation is concrete: tuple COPY and basebackup now share enough byte-ring
machinery that ACK/mirror/terminal bugs should be prevented by one set of
invariants, not fixed separately per workload.

Settlement status: the tuple passive-receiver rule is the main settled boundary.
Any proposal that makes the service parse tuple COPY records again needs a
separate performance/design discussion because it would move DB-semantic work
back into the Homer service.

Implementation status: complete as of June 6, 2026. The shared lifecycle
frontier reset is now factored into
[`HomerServiceResetPayloadSenderFrontiers()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11523)
and
[`HomerServiceResetPayloadReceiverCreditFrontiers()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11548),
which are used by peer bind, stream reuse, and peer-binding cleanup. The peer
bind helper now documents the full payload-stream lifecycle boundary. The
stream-state comments also document the delayed sender-head mirror
deregistration rule at
[`deferSenderHeadMirrorDeregistration`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:859),
because late tuple byte-ring receiver ACK WRs can outlive exact-stream teardown.

Important correction found during validation: the first helper extraction
over-reset the remote byte-ring frontier in
[`HomerServicePrepareTupleResultStreamForCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11661).
That function resets semantic/source producer frontiers for a new row-producing
command, but the peer receive byte ring remains an absolute byte stream across
commands. Resetting `payloadByteSenderPostedTail` to zero let a valid
consumed-head mirror from the previous command look ahead of the sender and
tripped the byte-ring sender invariant during remote Homer pgbench. The fixed
code preserves `payloadByteSenderPostedTail` at
[`HomerServicePrepareTupleResultStreamForCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:11707).

Validation evidence for this completion:

- Build/install/sync: `sudo -n -u dbcomm make -j8 service-bin client-bin`
  passed; `sudo -n make install-headers install-service-bin install` installed
  the fixed build; the installed prefix was synced to `farnet0` with
  `rsync --rsync-path='sudo -n rsync'`.
- Citus backend-to-backend Homer COPY, using 10M rows,
  `citus.experimental_tuple_sink_batch_tuple_target=64`, and
  `citus.experimental_tuple_sink_slot_capacity_bytes=4096`, passed one warmup
  and three measured runs after the fix. Artifact directory:
  `/tmp/homer_b2b_copy_10m_w2_fix_1780778328`. Times were warmup `8.71s`, then
  `5.42s`, `5.11s`, and `5.19s`; every run returned
  `count=10000000`, `min=1`, `max=10000000`, and `sum=500000050000000`.
- Remote RDMA Homer pgbench passed c1 and c4 correctness/performance smokes.
  The warmed c1 artifact `/tmp/homer_pgbench_w2_fix_c1_warm_1780778297.log`
  processed `20000/20000` transactions with zero failures, `4738.61 TPS`, and
  p99 `0.230 ms`. The c4 artifact `/tmp/homer_pgbench_w2_fix_c4_1780778311.log`
  processed `40000/40000` transactions with zero failures, `11583.52 TPS`, and
  p99 `0.553 ms`.
- Remote RDMA Homer basebackup blackhole passed one warmup and two warmed
  repeats. Artifact directory `/tmp/homer_basebackup_w2_fix_1780778362`
  reported `5.76s`, `4.65s`, and `4.50s`.
- Concurrent remote RDMA Homer pgbench plus background remote RDMA basebackup
  passed. Artifact directory `/tmp/homer_concurrent_w2_fix_1780778389` reported
  pgbench `10000/10000` transactions with zero failures, `3674.45 TPS`, p99
  `0.458 ms`, while basebackup completed in `4.90s`.
- Post-run process/log sanity: only the intended PostgreSQL postmaster and
  Homer service remained on each host, and both service logs had no matching
  `failed`, `timeout`, `REM_ACCESS`, `transport broken`, `payloadTransportBroken`,
  `ERROR`, or `frontier invariant` lines.

## Workstream 3: Peer Command/Control Lifecycle Cleanup

Goal: unify the peer-control operation machinery while preserving typed command
semantics.

The peer push-completion prerequisite lives in this workstream and is now
implemented for the normal service-to-service command completion path. The
detailed implementation/status note is
[peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md).
The implementation replaces normal service-to-service `POLL_COMMAND_COMPLETION`
with a requester-owned, backend-visible peer completion ring; peer polling
remains as a fallback/debug path while broader mixed-workload validation
continues.

Tuple COPY uses both a payload stream and remote worker commands. The
coordinator opens the tuple stream, starts the worker transaction and COPY ingest
command in
[`StartExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2635),
and waits for the ingest command in
[`FinishExperimentalTupleSinkWorkerInsert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2809).
Client SQL uses the same broad typed-command surface, but with different command
semantics. Basebackup currently opens a payload stream only; the remote side is a
Homer service receiver/blackhole, not a spawned remote PostgreSQL backend.

Implementation checklist:

- Factor the repeated async peer-control shape:
  - stream open in
    [`TupleSinkServiceProgressStreamOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17968)
  - command-session open in
    [`TupleSinkServiceProgressCommandOpenAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17317)
  - start-command and poll-completion forwarding in
    [`TupleSinkServiceProgressLocalControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18109)
- Keep typed command semantics separate:
  - `TX_BEGIN_ATTACH` carries distributed transaction attachment/replay state
  - `COPY_INGEST_FROM_TUPLE_SINK` binds remote worker work to an exact tuple
    payload stream
  - `SQL_EXECUTE` and client-SQL commands carry SQL/session semantics
- Make peer command endpoint ownership explicit in session state comments and
  helper names. Tuple COPY currently obtains its peer command endpoint as a
  consequence of stream open; that relationship should be documented as a
  payload-bound command session, not hidden in stream-open side effects.
- Done: peer service-to-service push completion now uses a direct
  backend-visible peer completion ring on the normal path. The remaining cleanup
  is to narrow or remove fallback polling after broader validation.
- Do not collapse `OPEN_SESSION` and `START_COMMAND` conceptually. A future wire
  fast path may fuse them for startup latency, but session/channel
  establishment and command dispatch/completion remain different abstractions.
- Keep peer-control scheduler facts operation-neutral. A fact may say "stream
  open pending", "command open pending", "start command pending", or "completion
  poll pending"; it should not say "COPY is pending" as a scheduling reason.
- Separate three implementation concerns even if they share helper code:
  endpoint/session lifetime, operation state-machine progress, and command
  payload semantics. Mixing these would make peer-ring completion cleanup harder
  because delivery would again depend on operation-specific polling code.

Settlement status: keep `OPEN_SESSION` and `START_COMMAND` distinct. Normal peer
completion now uses the direct peer completion ring when available. The next
question is whether aggregate peer-control still hides materially different
readiness phases now that hot terminal-completion polling is no longer expected
on the normal path; if it does, split peer-control readiness into explicit
per-operation/per-phase facts.

## Workstream 4: Common Payload-Stream Scheduler Facts

Goal: complete the shared fact model for bidirectional payload streams so tuple
COPY, client-SQL tuple-result streams, basebackup, and future payload families
can all be scheduled from neutral service-progress and egress facts.

This is not "add tuple COPY facts." Tuple COPY is the workload that currently
exposes the weakness, because its receive byte-ring progress is service-passive
and worker-coupled. The fix should be generally useful for any RDMA payload
stream whose progress depends on outgoing publication, incoming doorbells,
receiver credit, ACK completion retirement, local consumer backpressure, and
terminal drain.

Existing egress facts already cover much of the outgoing payload side through
[`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3015),
including ready bytes/objects, remote credit, local completion pressure, and
traffic metadata. The missing work is mainly first-layer service-progress facts
for payload-stream sources whose next useful action is not simply "send more
payload bytes."

Current implementation state after the June 6, 2026 W4 slice:

- Done: first-layer source state now carries generic readiness/blockage reasons
  in
  [`HomerProgressReasonMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1295),
  [`HomerProgressSourceCore`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1350),
  [`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1383),
  and [`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1632).
- Done: payload-source traffic metadata is synchronized from each stream through
  [`HomerServiceSyncPayloadProgressSourceMetadata()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12799),
  and its service-progress CPU/liveness class is derived by
  [`HomerServicePayloadCpuClassForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12774).
  This keeps the first-layer class derived from the operation-neutral traffic
  class instead of making the service-progress scheduler understand tuple COPY,
  client SQL, or basebackup semantics.
- Done: neutral payload readiness/blockage facts are computed by
  [`HomerServicePayloadStreamReasonMasks()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4659),
  refreshed for explicit per-stream ready sources by
  [`HomerServiceRefreshPayloadProgressSourceFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4789),
  and carried through grant feedback by
  [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5103).
- Payload egress facts already exist in
  [`HomerPayloadEgressReadyFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1491)
  and are built by
  [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3096).
- Transport traffic class is already workload-neutral and maps tuple/client-SQL
  payloads to foreground payload and basebackup to bulk payload in
  [`HomerServicePayloadTrafficClassForOpKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2031).
- Dense payload scans intentionally do not refresh per-stream source cores in
  [`HomerServiceExecuteAggregatePayloadProgressScan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5409).
  Aggregate scans represent the whole payload table as one source, so detailed
  per-stream facts are only useful on the explicit per-source path. The W4
  implementation also gates payload reason-bit accumulation in
  [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:16805)
  when the aggregate path has no per-source feedback consumer. This was a
  measured hot-path concern for tuple COPY.

Implementation checklist:

- Done: synchronize per-stream `HomerTransportTrafficClass` into the corresponding
  `HomerProgressSourceCore` when a payload stream is initialized, rebound, or
  reset. This lets first-layer policies order foreground tuple/client-SQL payload
  streams and bulk basebackup streams without reinterpreting DB operation kinds.
- Done: add neutral readiness/blockage reason bits for payload-stream sources:
  - `OUTGOING_PAYLOAD_READY`
  - `OUTGOING_COMPLETION_PENDING`
  - `INCOMING_DOORBELL_PENDING`
  - `INCOMING_CREDIT_PUBLISH_NEEDED`
  - `INCOMING_ACK_COMPLETION_PENDING`
  - `LOCAL_CONSUMER_BACKPRESSURE`
  - `REMOTE_CREDIT_BLOCKED`
  - `LOCAL_SEND_RESOURCE_PRESSURE`
  - `CLOSE_OR_RECLAIM_PENDING`
- Done: preserve these reason bits through the same path that already updates feedback:
  executor-local progress in the payload pump, merge through
  [`HomerProgressResultMergePayloadDelta()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3855),
  then update source feedback in
  [`HomerServiceUpdateProgressFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5103).
- Partly done: generic receive-side feedback now exposes receiver doorbells,
  receiver-credit publication need, receiver ACK completion retirement, and local
  consumer backpressure as reason masks. More quantitative fields such as exact
  pending receiver ACK count/frontier, pending receiver-credit bytes/objects, or
  outstanding receiver ACK WR count remain future policy inputs if a policy needs
  them.
- Keep tuple-view passive receive explicit. In the tuple-view byte-ring branch
  inside the current byte-ring receiver
  [`HomerServicePumpIncomingByteRingBaseBackup()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:15316),
  the service returns credit but does not parse/copy tuple records. The generic
  fact should be "local consumer has/has not advanced the receive head," not
  "worker inserted tuple batch."
- Keep outgoing egress facts generic. Existing egress fields such as
  `readyBytes`, `readyObjects`, `blockedOnRemoteCredit`, `blockedOnLocalResources`,
  and `sourceReleaseBlocked` are still the right vocabulary. If later policies
  need more, prefer age/first-ready and fragment/record pressure facts over
  workload names.
- Add targeted diagnostics behind compile-time macros while implementing this
  workstream so policies can be debugged without paying extra hot-path stores in
  performance builds.

This workstream directly enables tuple COPY scheduling research, but the output
should be a more complete payload-stream fact model for all workloads. It should
not attempt to make a new policy clever yet; it should make the facts complete
and cheap enough that future policies can reason about stream liveness,
backpressure, and egress pressure without knowing DB-level semantics.

Settlement status: W4 is implemented as a fact-model slice. Facts remain neutral
and reusable across workloads, and generic readiness/blockage reason bits live
directly in `HomerProgressResult`, `HomerProgressSourceCore`, and
`HomerProgressSourceFeedback`, not in a payload-only side structure. The
per-stream `cpuClass` question is also settled for now: payload sources derive
their CPU/liveness class from the stream's traffic class, so foreground
tuple/client-SQL payloads map to hot data and bulk/basebackup payloads map to
cold setup/maintenance without making the scheduler branch on DB operation kind.

Validation evidence for W4:

- Build/deploy: `git diff --cached --check` and `make -j8 service-bin
  client-bin` passed in `/data/dbcomm/citus-dbcomm`; installed artifacts were
  synced to `farnet0`.
- Remote Homer pgbench after final W4 trim:
  `/tmp/homer_w4_final_pgbench_1780786205` and
  `/tmp/homer_w4_final_pgbench_warm_1780786227`.
  Warm c1 was correct at `4359.37 TPS`, p99 `0.232 ms`, zero failed
  transactions. c4 was correct at `7999.31 TPS`, p99 `0.566 ms`, zero failed
  transactions.
- Remote RDMA basebackup after final W4 trim:
  `/tmp/homer_w4_final_basebackup_1780786241`. Warmup was `5.74 s`; repeats
  were `4.46 s` and `4.45 s`; all runs completed successfully.
- Concurrent remote Homer pgbench plus background remote RDMA basebackup:
  `/tmp/homer_w4_final_concurrent_1780786269`. Foreground c1 pgbench was
  correct at `3210.70` and `3185.23 TPS`, p99 `0.448` and `0.481 ms`, while
  basebackup completed in `4.94` and `5.01 s`.
- Concurrent performance caveat: the older W1/W2 notes had a hotter foreground
  band, but a warmed same-day parent-commit A/B at `7be63cdb8` produced
  `3460.26`, `3214.69`, and `3224.77 TPS` with basebackup at `4.99 s` in
  `/tmp/homer_parent_concurrent_ab_warm_1780786777`. Treat the W4 concurrent
  result as correct and comparable to the current parent band.
- Backend-to-backend Citus COPY after final W4 trim:
  `/tmp/homer_w4_copy_10m_trim3_1780785961`. Correctness check matched
  `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`; warmed
  runs were `7.71`, `7.98`, and `8.05 s`.
- COPY performance caveat: the older saved post-peer-push baseline was
  `7.46/7.62/7.32 s`, but a direct same-day parent-commit A/B at `7be63cdb8`
  reproduced a slower current band of `8.01/8.30/7.99 s` in
  `/tmp/homer_parent_copy_10m_ab_1780786118`. Treat the W4 COPY result as
  correct and comparable to today's parent band, not as evidence that W4 caused
  the older-baseline delta.

## Non-Goals

- Do not rewrite tuple normalization or tuple-view batch encoding in the Citus
  tuple adapter.
- Do not rewrite worker tuple insertion in
  [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:440).
- Do not rewrite PostgreSQL basebackup object production in
  [`bbsink_homer_begin_backup()`](/data/dbcomm/postgres-citus/src/backend/backup/basebackup_homer.c:234).
- Do not implement remote PostgreSQL materialization for basebackup in this
  milestone. The current remote basebackup receiver remains the Homer service
  blackhole/accounting path.
- Do not remove fallback peer `POLL_COMMAND_COMPLETION` until broader
  mixed-workload validation shows the peer completion ring is sufficient outside
  the tuple COPY acceptance path.

## Verification Plan

Each behavior-affecting step should be verified against the workloads that now
share the substrate:

- Build Citus/Homer service and client binaries.
- Run backend-to-backend Citus tuple COPY, including the larger slot/batch shape
  recorded in the current baseline note.
- Run Homer pgbench c1 and c4 smoke/performance checks to catch command/result
  regressions.
- Run local and remote Homer basebackup to catch byte-ring/basebackup receiver
  regressions.
- Compare warmed results against the latest recorded baseline before claiming a
  unification step is complete.

## Related

- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md): scheduler-ready substrate and current service-progress/egress scheduler boundary.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md): RDMA publication and doorbell rules that byte-ring unification must preserve.
- [../../../implementations/citus/transport/byte_ring_payload_stream_checkpoint.md](../../../implementations/citus/transport/byte_ring_payload_stream_checkpoint.md): current implementation checkpoint for the shared byte-ring payload substrate.
- [../data-movement/command_dispatch_completion_plane.md](../data-movement/command_dispatch_completion_plane.md): typed command dispatch/completion plane for tuple COPY and client SQL.
- [../data-movement/homer_tuple_copy_batching_and_copy_reduction.md](../data-movement/homer_tuple_copy_batching_and_copy_reduction.md): current tuple COPY performance baseline and future copy-reduction options.
- [../../postgres/replication/base_backup_service_abstraction.md](../../postgres/replication/base_backup_service_abstraction.md): basebackup object-family semantics above the neutral payload stream.
