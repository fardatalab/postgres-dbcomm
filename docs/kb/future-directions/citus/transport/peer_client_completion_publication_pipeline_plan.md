# Homer Peer-Client Completion Publication Pipeline Plan

## Scope

- **What this doc explains**: the targeted plan for fixing cross-node client-SQL
  command completion publication: reader-side slot validation, nonblocking RDMA
  source lifetime, send-CQ retirement, and scheduler-visible retry/resource
  facts.
- **What this doc does NOT cover**: the full unified aggregate-action scheduler
  migration, service-to-service peer command completion rings, payload byte-ring
  lifecycle cleanup, or DOCA/DPU offload.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

Remote client-SQL pgbench uses a peer-client completion path: the backend-node
Homer service publishes command completion records directly into the frontend
client's completion mailbox over RDMA. Current code keeps this path correct by
doing a blocking signaled RDMA write for the final `publishedEpoch` update, but
that creates a hot-path local send-CQ wait per visible completion. It also
revealed a modeling problem: completion publication is not just "consume a
backend completion ring record." It is an egress pipeline with source-buffer
lifetime, remote visibility, retry state, and resource pressure.

This work is related to the aggregate-action scheduler plan, but should be
implemented as a focused publication-pipeline cleanup first. The scheduler
redesign should then consume the cleaner facts instead of carrying this bug-prone
blocking helper shape forward.

## Key code pointers

- [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12397) is the current cross-node client-SQL completion publisher.
- [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12565) posts the completion body from the per-session scratch completion buffer.
- [`TupleSinkServiceWritePeerUint64Rdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6258) writes the remote `publishedEpoch` using `connectionState->outgoingPublishedTailBuffer`.
- [`TupleSinkServiceWriteConnectionBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4433) posts one signaled RDMA WRITE and waits for its local send completion at [`remote_execution_peer_transport_rdma.c:4464`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4464).
- [`TupleSinkServiceCommandCompletionRdmaBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12230) computes the current variable-size completion write prefix. The first async peer-client completion path should use full fixed-size completion writes unless measurement proves the prefix optimization matters.
- [`HomerClientPollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2402) treats `publishedEpoch` as the ready frontier, copies a completion slot at [`homer_client.c:2435`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2435), then validates command kind/sequence at [`homer_client.c:2436`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2436).
- [`HomerClientOpenSqlSession()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:609) already checks the mapped client completion mailbox protocol version. The mailbox layout change must extend this mixed-binary guard with version/size agreement.
- [`HomerClientStartCommandDirect()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2100) publishes frontend command slots with a slot-local `readySeq` before the aggregate `publishedEpoch`; the service and backend validate that same ready word in [`TupleSinkServicePumpRemoteClientSqlCommands()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14183) and [`remote_execution_backend_bridge.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:2277).
- [`TupleSinkServiceReserveClientSqlCommandWriteCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3290) and [`TupleSinkServiceRetireClientSqlCommandWriteCompletionsThrough()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3479) are the current command-forwarding model for fixed metadata slots, tagged send-CQ checkpoints, and range retirement of older unsignaled WRs.
- [`TupleSinkServicePublishPendingPeerClientCommandCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13730) retries peer-client completions that were semantically blocked by payload/result-send dependencies.
- [`HomerServiceBuildSessionMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25721) maps `peerClientCompletionDeferred` to `HOMER_PROGRESS_WAIT_PAYLOAD_FRONTIER` at [`tuple_sink_service_process.c:25782`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25782).
- [`HOMER_PROGRESS_ACTION_PUBLISH_COMMAND_COMPLETION`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26544) currently routes a completion-ring source through [`HomerServiceExecuteCompletionProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10005), which consumes backend completion mailbox records.
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1601) and [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1811) are the tagged send-CQ retirement surface that should grow a peer-client completion publish completion kind.

## Current behavior / grounded findings

- The current peer-client completion publisher performs two RDMA writes: an
  unsignaled completion-body write, then a signaled blocking `publishedEpoch`
  write. Local scratch reuse is safe only because the second write waits for send
  completion on the same RC QP.
- The current frontend completion consumer uses aggregate `publishedEpoch` as the
  primary readiness gate. Unlike the command path, the client completion mailbox
  has no slot-local `readySeq` / `readyEpoch` word.
- The current peer-client completion write can use a variable-size prefix. That
  is good for bytes, but a reused remote slot can retain old cold descriptor
  fields. Readers should ignore absent fields by flags, but the first async path
  should remove this stale-field risk with full fixed-size completion writes.
- The command-forwarding path already demonstrates the right local CQ retirement
  idea: source objects stay stable until a tagged signaled checkpoint retires the
  older unsignaled WR range on the same QP. That fixed metadata table is not the
  source storage itself; it is local lifetime/dispatch metadata.
- The current deferred peer-client completion list is a semantic dependency list.
  It should mean "payload/result-stream ordering is not satisfied yet", not
  "local RDMA source slots are full."
- The current selected `PUBLISH_COMMAND_COMPLETION` executor still combines
  collection and publication for backend completion-ring sources. A retry that
  has already staged a completion has no new backend ring record to consume, so
  source-credit retry must not depend on another backend completion event.

## Target invariants

- `publishedEpoch` is an aggregate hint, not sufficient proof that the selected
  slot body is CPU-visible and belongs to the expected command.
- A frontend completion slot is consumable only when its slot-local ready word
  equals the expected epoch. The client must use a stable-read protocol: check
  ready before copy, use a read barrier, copy the completion, use another read
  barrier, re-check the ready word, then validate command sequence, command kind,
  protocol, command state, and flag-dependent descriptor fields.
- `readyEpoch > expected` is an overrun/protocol-loss signal, not an indefinite
  not-ready state.
- "Posted to the NIC" and "source slot retired by local CQE" are separate
  states. A successfully posted completion must not be posted again, but its
  source slot must not be reused until send-CQ retirement.
- The completion body write, slot-local ready-word write, and aggregate
  `publishedEpoch` hint write for one completion must be posted on the same
  ordered RC QP, in that order, for the first implementation.
- The source ring must never become full with no outstanding signaled checkpoint.
  If it does, that is a scheduler/source-retirement bug, not normal backpressure.
- The first signaled checkpoint policy is `signaledInterval <= ringSlots / 2`,
  with pressure-triggered signaling before the source ring can become full.
- `peerClientCompletionDeferred` remains payload/result semantic deferral only.
  Source-ring-full and local CQ pressure are command/control resource pressure.
- `PUBLISH_COMMAND_COMPLETIONS` must be able to run from staged state even when
  there is no new backend completion-ring record.
- Client completion mailbox layout changes require hard protocol/version/size
  gating so mixed old/new `pgbench`, `libhomer_client.a`, and
  `citus_tuple_sink_service` binaries fail fast instead of interpreting the ring
  differently.

## Current Implementation Progress

Status as of June 17, 2026 on `homer-state-machine-scheduler-milestone`:

- Slice 1 is implemented in code and compile-checked: the client completion
  mailbox has slot-local `readyEpochSlots`, version/size/slot-count gating, and
  client-side stable slot reads. Workload validation is still required after
  install/sync.
- Slice 2 is implemented and compile-checked: peer-client completion
  publication now owns a per-session registered source ring and posts the
  completion body, ready epoch, and aggregate published epoch from that ring.
  [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12980)
  now reserves a source slot first and fills `sourceSlot->completion` directly,
  rather than building a separate scratch completion and copying it into the
  source slot. This is not true zero copy, but it removes the avoidable
  intermediate completion image while preserving service-side transformation
  and source-slot ownership.
- Slice 3 is implemented in code and compile-checked: the session has staged
  publication metadata (`peerClientCompletionPublishPending`,
  `peerClientCompletionPublishPendingSequence`,
  `peerClientCompletionPublishBlockedReasonMask`, and related fields) so
  completion publication can retry after collection without consuming another
  backend completion-ring record. Source-credit blockage is represented as
  `HOMER_PROGRESS_REASON_COMPLETION_PUBLISH_SOURCE_CREDIT` and is not added to
  the payload semantic deferred list.
- Slice 4 and Slice 5 were implemented first as a three-WR memory-polled
  variant: peer-client completion publication posted the completion body,
  slot-local ready epoch, and aggregate published epoch with async one-sided
  writes instead of the blocking
  [`TupleSinkServiceWriteConnectionBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4675)
  path. The final WR carried a peer-client-completion WR-id tag routed by
  [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1777),
  and the command-send CQ drain retired source slots through
  [`HomerServiceHandlePeerClientCompletionPublishCqeFromDrain()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10332).
  The first validation exposed and fixed an owner-state bug in
  [`TupleSinkServiceReservePeerClientCompletionPublishCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3660):
  the source slot may already be `POSTED` after the body/ready WRs when the
  final checkpoint owner is attached. With that fix, remote RDMA c1 passed, but
  remote RDMA c4 failed correctness under pressure.
- The remote RDMA c4 failure invalidated the earlier plan ordering that treated
  the three-WR memory-polled completion path as the correctness baseline and
  postponed `RDMA_WRITE_WITH_IMM` to a later policy experiment. Slice 4b is now
  implemented as a correctness bridge for a responder-visible
  `WRITE_WITH_IMM` publication gate. The normal path in
  [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13091)
  posts one fixed-size `RDMA_WRITE_WITH_IMM` through
  [`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6052).
  The receiver handles the immediate in
  [`TupleSinkServiceHandlePeerClientCompletionDoorbell()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18776)
  and publishes receiver-local `readyEpochSlots[]` and `publishedEpoch` with CPU
  stores. This validates the ordering idea, but it is not the final hot-path
  design because it puts receiver-service CQ parsing, token demux, session lookup,
  and CPU publication in front of the frontend client's normal memory polling.
- The next target is Slice 4c: a host-fast-path self-publishing WIMM ready-word
  path implemented as one body `RDMA_WRITE` followed by one
  `RDMA_WRITE_WITH_IMM` whose payload is the 8-byte
  `readyEpochSlots[slotIndex]` word that the frontend client already polls. This
  is the implementation target because same-message body-before-ready visibility
  is not established as a usable invariant on the current farnet setup. A future
  one-WR `[body][ready]` WIMM slot remains possible only after the target MR/MKey
  and QP ordering contract proves it safe. In both cases, the frontend client
  does not consume the immediate and normal completion visibility must not depend
  on receiver-service token decode or session demux.
- Slice 4b validation exposed a second ownership bug: shared send-CQ drains can
  consume a peer-client completion checkpoint before the completion-publish owner
  callback is installed. Command/payload paths already had stash handling for
  this shape; peer-client completion publication now stashes and replays such
  CQEs rather than treating them as fatal or leaving the source ring full.
- Slice 4c is implemented as the host-fast-path two-WR publication shape:
  [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13105)
  posts the completion body with RDMA WRITE and then posts
  `readyEpochSlots[slotIndex]` with RDMA WRITE WITH IMM through
  [`TupleSinkServicePostPeerRegisteredClientCompletionBytesWithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6018).
  The recv-CQ parser drains the immediate for receive-credit/repost progress, but
  it no longer queues or CPU-publishes peer-client completion visibility. The
  frontend polling gate is the WIMM-written ready word.
- The client library has a temporary defensive terminal replay key in
  [`HomerClientRememberTerminalCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1021)
  and
  [`HomerClientReplayTerminalCompletionIfPresent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1036).
  This was added after GDB showed a client stuck with pgbench command-pending
  state even though the same terminal completion epoch had already been consumed.
  It is a guardrail, not the final explanation of the long-run c4 wedge.
- Machine-baseline send-CQ readiness now accounts for peer-client completion
  publish checkpoints in
  [`HomerMachineBaselinePolicyCommandSendCqDue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5800).
  Without this, source-credit pressure for completion publication could fail to
  schedule the shared command/control send-CQ drain promptly.
- Part of Slice 6 is already present as scaffolding: candidate construction sees
  staged peer-client completion publication even when the backend completion
  ring is empty, and maps source-credit pressure to command/control send-CQ
  relief rather than payload-frontier wait.

Validation evidence on June 17, 2026:

- Installed Citus/Homer commit `8c5c93628` on both farnet hosts, no-stats build
  with `CPPFLAGS='-D_GNU_SOURCE'`.
- Current fast-link validation used `farnet1 10.10.1.101/enp33s0f0np0` to
  `farnet0 10.10.1.100/enp33s0f0np0`; both links reported `400000Mb/s`, four
  lanes, link detected.
- Three-WR Slice 4/5 remote RDMA c1 smoke completed `1000/1000`, zero failures.
  Warm repeats completed `20000/20000`, zero failures, at about `4213 TPS`
  and `3807 TPS`, with p99 about `0.258 ms` and `0.288 ms`.
- Three-WR Slice 4/5 remote RDMA c4 rejected the slice for multi-client
  correctness: one run
  aborted after `15209/40000` transactions with tuple result byte-ring header
  mismatches and a pushed-completion sequence mismatch
  `expected sequence=25693 got sequence=25686`. That seven-gap shape matches
  the known weak publication/visibility issue, not a scheduler-policy decision.
- Slice 4b receiver-service WIMM validation rebuilt and reinstalled no-stats Citus/Homer on both
  hosts, then restarted PostgreSQL and both Homer services from a clean process
  baseline. Remote c1 smoke completed `1000/1000`, zero failures, at about
  `492 TPS` with cold setup included in max latency. Warm remote c1 completed
  `20000/20000`, zero failures, at about `3640 TPS`, p95 `0.289 ms`, p99
  `0.299 ms`.
- Remote c4 after the WIMM gate and peer-client completion CQE stash fix
  completed two warmed runs without failures: `40000/40000` at about
  `8988 TPS`, p95 `0.579 ms`, p99 `0.674 ms`; and `40000/40000` at about
  `8746 TPS`, p95 `0.590 ms`, p99 `0.690 ms`. Service logs on both hosts had
  no `error`, `failed`, `mismatch`, `stale`, `ring full`, `checkpoint`, `fatal`,
  or `panic` messages after these runs.
- This accepts the WIMM publication gate for c1/c4 correctness, but the current
  c4 band is still below the older roughly `11k TPS` target. Follow-up discussion
  concluded that the likely regression is not "WIMM is slow" by itself, but the
  receiver-service publication shape: the frontend still polls memory, while the
  service must first poll recv CQ, parse an immediate, queue/pop a token, decode
  session identity, and CPU-store the words that let the frontend polling path
  succeed. Slice 4c should remove that extra receiver-service publication hop.
- Slice 4c no-stats validation on June 17, 2026 used the same
  `homer-state-machine-scheduler-milestone(sha: 8c5c93628)` base plus dirty
  WIMM/replay changes. Remote c1 completed `1000/1000`, zero failures; the cold
  run included setup latency, but p50/p95 stayed around `0.250/0.266 ms`.
  Short remote c4 repeats completed correctly: warm `-c4 -j4 -t2500` runs were
  about `10.36k` to `10.40k TPS`, with p95 around `0.51 ms` and p99 around
  `0.57-0.60 ms`.
- Slice 4c is not accepted for long-run c4 correctness/performance yet. A
  `-c4 -j4 -t10000` run wedged after the short repeats. GDB showed one remaining
  pgbench client in `CSTATE_WAIT_RESULT`, waiting for `TX_COMMIT` sequence
  `58522`; the frontend session had `lastConsumedCompletionEpoch=66882`, while
  the farnet1 service session had already published sequence `58522` at epoch
  `66882`. The client replay key still pointed at an earlier terminal sequence
  (`58515`), so the current failure is no longer a simple missing RDMA
  publication; it is a frontend completion consumption/application ordering bug
  or an untracked consumer path. Do not claim performance recovery until this
  long-run c4 wedge is fixed.

Handoff diagnosis after the rejected Slice 4c long-run validation:

- The planned Slice 4c changes are the two-WR sender-side publication shape,
  client-polled slot-local `readyEpochSlots[]`, and prompt recv-CQ credit
  draining without receiver-service token demux on the host-client visibility
  path.
- The peer-client completion send-CQ owner accounting and shared-CQ stash work
  are adjacent ownership fixes exposed by Slice 4c validation. They are not the
  central publication model, but they are consistent with the current shared CQ
  design because completion-publish checkpoints use the same connection send CQ
  as command/control traffic.
- The `lastTerminalCompletion*` replay fields in
  [`HomerClientSession`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:53)
  and helpers in
  [`homer_client.c`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1021)
  are an improvised diagnostic/guardrail, not part of the intended Slice 4c
  design. They try to recover if a frontend terminal completion is already
  acknowledged but the pgbench state machine still asks for that same command.
  The final long-run failure showed that this guardrail is insufficient and may
  be the wrong abstraction to keep.
- The likely root problem is a destructive-consumption boundary in
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2447):
  it advances `lastConsumedCompletionEpoch` and remote `consumedEpoch`, while
  [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3665)
  and
  [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3497)
  separately apply that event to pgbench state. If any helper consumes/skips an
  event without the pgbench state transition, the event is gone and the client
  can wait forever for a later terminal event that will never exist.
- The most suspicious client-side behavior is the recursive older no-result
  completion skip branch in
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2561).
  It lets the client library decide that a visible event is not useful to the
  current pgbench command and acknowledges it internally. That may be valid only
  after a clearer event ownership model proves which completions are ignorable.
- Next design discussion should prefer replacing replay with an explicit
  peek/apply/ack contract: copy and validate one frontend completion event
  without advancing the consumed epoch, let pgbench or a higher-level client
  state machine apply it, then acknowledge exactly the event that was applied.
  An alternative is to move command-state application fully into the Homer client
  library so `TryCommandCompletion()` never exposes a consumed-but-unapplied
  event boundary. Do not continue adding replay/skipping cases until this
  ownership boundary is settled.

## Implementation plan

### Slice 0: baseline and diagnostic surface

Status: implemented as diagnostic scaffolding; workload validation pending.

1. Record the current branch/commit and confirm the failed async experiment is
   not present.
2. Add compile-gated counters for the new pipeline before changing behavior:
   `peerClientCompletionPublishBlockingEpochWrites`,
   `peerClientCompletionPublishPosted`,
   `peerClientCompletionPublishSignaled`,
   `peerClientCompletionPublishRetired`,
   `peerClientCompletionPublishSourceFull`,
   `peerClientCompletionPublishBlockedOnSourceCredit`,
   `peerClientCompletionPublishOutstandingSignaledCheckpoints`,
   `peerClientCompletionPublishRingFullWithoutCheckpoint`,
   `peerClientCompletionPublishReadyEpochMiss`, and
   `peerClientCompletionPublishDuplicateSuppressed`.
3. Keep counters under a stats macro for diagnosis; performance runs must use a
   no-stats build.

Acceptance:

- Current remote pgbench c1/c4 still passes with no behavior change.
- The blocking epoch-write counter matches visible peer-client completion
  publications, proving the measurement point is on the intended path.

### Slice 1: reader-side slot-local validation

Status: implemented and compile-checked; workload validation pending.

1. Extend `CitusRemoteExecClientCompletionMailbox` with slot-local ready words,
   preferably `readyEpochSlots[CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS]`
   to avoid changing `CitusRemoteExecCommandCompletion` itself.
2. Update local completion publication in
   [`TupleSinkServicePublishClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12261) to write the completion body, then the slot-local ready epoch, then aggregate `publishedEpoch`.
3. Update peer-client completion publication to write the remote ready word before
   the aggregate `publishedEpoch`. In this slice the final aggregate write may
   still use the existing blocking helper.
4. Update [`HomerClientPollCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2402) and
   [`HomerClientMarkPushedCompletionConsumedIfPresent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:975) so they treat
   `publishedEpoch` as a hint and use the stable-read protocol:
   `ready1 == expected`, read barrier, copy completion, read barrier,
   `ready2 == expected`, then semantic validation. If `readyEpoch > expected`,
   report overrun/protocol loss.
5. Treat protocol/version/size agreement as a hard Slice 1 gate. Mixed old/new
   `pgbench`, `libhomer_client.a`, and `citus_tuple_sink_service` binaries must
   reject each other before the ring is used.

Acceptance:

- Local and remote pgbench correctness passes.
- Artificially delaying or reordering the aggregate `publishedEpoch` observation
  causes a not-ready retry, not a copied stale/partial completion.
- No `completion mismatch` errors appear in the client logs under remote c1/c4.
- Mixed old/new binaries fail fast on protocol/version/size checks.

### Slice 2: peer-client completion publish source ring

Status: partially implemented and compile-checked; direct-fill cleanup is the
current immediate sub-slice.

1. Add a fixed per-session registered source ring for peer-client completion
   publication. Start with a small power-of-two slot count such as 16 so c1 has
   slack and c4 can expose source-credit pressure without dynamic allocation.
2. Each source slot should carry the full fixed-size completion image, the
   slot-local ready epoch source word, the aggregate `publishedEpoch` source
   word, QP post ordinal, remote slot index, command sequence, service session
   id, connection handle, and state (`FREE`, `RESERVED`, `POSTED`,
   `POSTED_SIGNALED_CHECKPOINT`, `RETIRED`, `FAILED_OR_INFLIGHT`, `FAILED`).
3. Add a bounded connection/QP-local FIFO of posted peer-client completion
   source refs:

   ```c
   typedef struct PeerClientCompletionPublishOutstandingRef
   {
       uint64_t qpPostOrdinal;
       uint32_t sessionIndex;
       uint16_t sourceSlotIndex;
   } PeerClientCompletionPublishOutstandingRef;
   ```

   This is the chosen retirement model. Per-session source rings keep source
   ownership direct; the connection FIFO makes same-QP cumulative retirement
   precise without scanning unrelated sessions.
4. Add direct per-session indices/counters, not a hash map. The hot executor
   should reserve a source slot by index and attach a compact WR id to the
   signaled checkpoint.
5. Fill `sourceSlot->completion` directly after reservation. Do not keep a
   separate peer-client completion scratch object on this path unless a later
   fallback path proves it is needed.

Acceptance:

- Source slots are never reused before local CQ retirement.
- A forced tiny source ring reports source-credit blockage instead of entering
  payload deferral.
- No dynamic allocation occurs in the per-completion publish path.
- Peer-client completion publication does not copy from a separate scratch
  completion into the registered source slot.
- The connection-local outstanding FIFO can retire a same-QP prefix without
  scanning all sessions that may have posted source slots on that QP.

### Slice 3: minimal staged publication state

Status: implemented and compile-checked; workload validation pending.

1. Add explicit staged/pending fields, for example
   `peerClientCompletionPublishPending`,
   `peerClientCompletionPublishPendingSequence`,
   `peerClientCompletionPublishPostedEpoch`, and
   `peerClientCompletionPublishBlockedReasonMask`.
2. Stage the backend completion state before attempting peer-client RDMA
   publication. A retry after source credit returns must not require another
   backend completion-ring record.
3. Keep `peerClientCompletionDeferred` only for semantic payload/result
   dependencies.
4. Add a resource-pressure reason such as
   `HOMER_PROGRESS_REASON_COMPLETION_PUBLISH_SOURCE_CREDIT`. Add a dedicated wait
   mask such as `HOMER_PROGRESS_WAIT_COMPLETION_PUBLISH_SEND_CQ` if mask space
   allows; otherwise map it to command/control send-CQ wait while preserving the
   explicit reason flag.
5. Add debug assertions: if blocked reason includes completion-publish source
   credit, the path must not set `peerClientCompletionDeferred` and must not set
   `HOMER_PROGRESS_WAIT_PAYLOAD_FRONTIER`.

Acceptance:

- A completion that has already been collected can be retried after source credit
  returns without consuming another backend completion-ring record.
- Source-credit blockage never sets `HOMER_PROGRESS_WAIT_PAYLOAD_FRONTIER`.
- The payload deferred list contains only semantic payload/result dependencies.

### Slice 4: asynchronous post helper

Status: implemented and compile-checked; remote c4 rejected the three-WR
memory-polled variant.

1. Keep [`TupleSinkServiceWriteConnectionBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:4675) and
   [`TupleSinkServiceWritePeerUint64Rdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6568) synchronous.
2. Add a dedicated helper such as
   `TupleSinkServiceTryPostPeerClientCompletionAsync()` with explicit result
   values:
   `NOOP`, `POSTED`, `BLOCKED_SEMANTIC_PAYLOAD`,
   `BLOCKED_SOURCE_CREDIT`, `BLOCKED_REMOTE_CREDIT`, and `FAILED`. An internal
   `ALREADY_POSTED` result may map externally to `NOOP` while feeding the
   duplicate-suppressed counter.
3. On success, post the completion body, slot-local ready word, and aggregate
   `publishedEpoch` hint without waiting, on the same ordered RC QP and in that
   order. Mark the logical completion as posted so retry cannot duplicate it, but
   leave the source slot unavailable until CQ retirement.
4. If a multi-WR sequence partially posts and a later WR fails, treat the
   connection/session as failed. Do not free a source slot that might already be
   referenced by a posted WR. Once any WR in the sequence may have been posted,
   the slot must move to `FAILED_OR_INFLIGHT` until transport reset cleanup.
5. Use full fixed-size `CitusRemoteExecCommandCompletion` writes for peer-client
   async publication unless measurement later proves the variable-prefix
   optimization is necessary.

Acceptance:

- The blocking epoch-write counter goes to zero for peer-client completion
  publication.
- Duplicate publication is suppressed by posted-state, not by source-slot reuse.
- Remote c1 remains correct with a source ring larger than the signaled interval.
- Reusing a mailbox slot after a previous descriptor-heavy completion cannot
  expose stale cold fields to the reader.

### Slice 4b: receiver-service WIMM publication bridge

Status: implemented and remote c1/c4 correctness-validated on June 17, 2026,
but not accepted as the final performance shape.

The three-WR peer-client completion publisher writes the completion body, a
slot-local `readyEpoch`, and aggregate `publishedEpoch` as separate one-sided
RDMA writes. That shape is too expensive for the hot command-completion path and
still leaves the frontend client polling memory-resident DMA state as the
publication gate. Remote c4 validation exposed this weakness as stale/mismatched
completion visibility.

Implemented bridge:

1. Keep the registered multi-slot source ring and tagged local send-CQ
   retirement from Slices 4 and 5.
2. Replace the body/ready/aggregate RDMA sequence in
   [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13091)
   with one fixed-size `RDMA_WRITE_WITH_IMM` into the remote
   `completionSlots[slotIndex]` when the connection supports the normal Homer
   WIMM publication rule.
3. The immediate data is a compact receiver-local doorbell token. It identifies
   the receiver service session whose already-written completion slot should be
   published; it is not the 64-bit epoch value.
4. The receiver service handles the recv-CQ immediate and publishes local shared
   memory with CPU release stores to `readyEpochSlots[slotIndex]` and
   `publishedEpoch`. After this point the frontend client still uses the
   slot-local stable-copy protocol, but it is polling receiver-local CPU
   publication state instead of treating peer DMA memory polling as the remote
   visibility gate.
5. The sender source slot is retired only by its local tagged send CQE. The
   receiver-side WIMM CQE proves receiver-visible publication; it does not prove
   source-buffer lifetime on the sender.
6. Receiver-side progress is explicit peer transport recv-CQ work. Do not hide
   it inside peer-control op helpers or completion-publish retry helpers.

Acceptance:

- Peer-client completion publication posts one network WR per completion in the
  normal path.
- Receiver-local `readyEpochSlots[]` and `publishedEpoch` are advanced only from
  the WIMM recv-CQ handler for peer-client completions.
- Remote c1 and remote c4 complete without command-completion sequence mismatch.
- Local source slots still retire through the tagged send-CQ path with a
  signaled interval greater than one.

### Slice 4c: two-WR self-publishing WIMM ready word

Status: next implementation target.

The lesson from Slice 4b is that `WRITE_WITH_IMM` should provide the publication
gate without forcing the receiver service to become the per-completion publisher
for a frontend client that is already polling shared memory. The fast path should
deliver the gate into the same memory word the client polls. Because
same-message body-before-ready visibility is not established as a usable current
invariant, this slice deliberately uses two ordered WRs.

Target design:

1. Keep the registered multi-slot source ring and tagged local send-CQ
   retirement from Slices 4 and 5.
2. Use two ordered WRs on the same RC QP for each peer-client completion:
   - an RDMA WRITE of the fixed-size `CitusRemoteExecCommandCompletion` body into
     `completionSlots[slotIndex]`
   - a following `RDMA_WRITE_WITH_IMM` of the 8-byte `nextEpoch` value into
     `readyEpochSlots[slotIndex]`
3. The WIMM-written ready word, not receiver-service CPU publication, is the
   frontend-visible publication gate. The immediate value is optional
   routing/debug metadata for the receiver service; normal frontend visibility
   must not depend on immediate parsing, token demux, session lookup, or a CPU
   store in `TupleSinkServiceHandlePeerClientCompletionDoorbell()`.
4. The frontend client should poll the expected slot-local ready word directly.
   `publishedEpoch` may remain a local-path hint or diagnostic counter, but peer
   client completion consumption must not wait for receiver-service advancement of
   aggregate `publishedEpoch`.
5. The client still performs the cheap stable-copy validation:
   `readyEpochSlots[slotIndex] == expectedEpoch`, copy the completion, re-check
   the same ready word, then validate protocol, command sequence, command kind,
   command state, and flag-dependent descriptor fields. The client does not get
   the immediate value on this host fast path.
6. Ordering prerequisite: the publication gate must not become visible before the
   completion body. For this two-WR slice, that requires same-QP ordered WR
   visibility and a target mailbox MR/MKey whose ordering policy does not permit
   the ready-word WIMM to pass the prior body WRITE. If relaxed-ordering MKeys can
   violate that contract, the mailbox MR must be registered without relaxed
   ordering or this fast path must be disabled.
7. The receiver service still drains recv CQ and reposts notification receives
   promptly to preserve transport receive credit and to keep future diagnostics or
   scheduler facts current. That prompt handling is not what makes the host client
   observe completion readiness; the client observes the WIMM-written host memory
   gate directly.
8. Future DPU/offload design remains open. If the host client continues polling a
   host-resident completion mailbox, the final publication boundary must still
   write host-visible memory eventually. Two plausible offload shapes remain:
   - mixed host/DPU RDMA, where some fast-path publications can still target
     host memory directly when ordering and isolation allow it
   - DPU-first ingress, where peer RDMA lands on the DPU, the DPU consumes the
     WIMM/CQ event, and the DPU later publishes into the host-visible client ABI
     at the boundary
9. Future one-WR host optimization is explicitly out of scope for this slice.
   A single `RDMA_WRITE_WITH_IMM` that writes `[body][readyEpoch footer]` can be
   revisited only after the target MR/MKey and QP ordering contract proves that
   ready visibility implies prior body visibility for the same WR.

Acceptance:

- Remote c1/c4 correctness passes without command-completion sequence mismatch.
- Warmed c1 returns to the previous `4.5k-4.8k TPS` band, and warmed c4 returns
  to the previous roughly `11k TPS` band, within normal run variance.
- The normal peer-client completion visibility path performs no receiver-service
  token queue/pop, session demux, or CPU publication store.
- The two-WR path's hardware/MR ordering evidence is recorded, including whether
  the target mailbox MR must be registered without relaxed ordering.
- Source slots still retire only through tagged local send-CQ checkpoints.

### Slice 5: tagged send-CQ retirement

Status: implemented and accepted for the WIMM c1/c4 validation band. Further
performance work should still measure CQ retirement batch size and source-credit
pressure under longer c4/concurrent runs.

1. Add a tagged WR kind for peer-client completion publication checkpoints in the
   same tagged completion dispatch family as command/payload completions.
2. On a signaled checkpoint CQE, pop the connection-local outstanding FIFO prefix
   where `qpPostOrdinal <= checkpointOrdinal`, and free each referenced
   session/source slot.
3. Feed typed retirement counts and frontiers into service progress feedback.
4. Signal periodically and under pressure. The first default should use
   `signaledInterval <= sourceRingSlots / 2`, and should force a signaled final
   WR when `activeSlots + slotsNeededForThisPublish >= ringSlots - safetyMargin`.
5. Treat source-ring-full with no outstanding signaled checkpoint as a bug. The
   `peerClientCompletionPublishRingFullWithoutCheckpoint` counter must remain
   zero.

Acceptance:

- CQ drain retires peer-client completion source slots without scanning unrelated
  sessions.
- Remote c1/c4 correctness passes with signaled interval greater than one.
- Forced signaled-every-one mode and batched checkpoint mode produce identical
  visible completion order.
- A source ring cannot deadlock full with no signaled checkpoint outstanding.

### Slice 6: scheduler-visible candidate facts

Status: partially implemented as scaffolding; full scheduler feedback still
needs explicit candidate/policy cleanup. Treat Slice 4c as a performance
prerequisite before using the current receiver-service WIMM bridge as a scheduler
policy baseline.

1. Candidate construction must emit `PUBLISH_COMMAND_COMPLETIONS` for staged
   pending completions even when `HomerServiceCompletionRingReadyCount()` is
   zero.
2. If source credit is blocking publication, candidate construction must raise a
   `DRAIN_SEND_CQ` / command-control send-CQ resource-relief candidate.
3. Scheduler feedback should distinguish semantic payload deferral, remote
   mailbox credit, local source-ring credit, posted-but-not-retired source slots,
   and CQ-retired source slots.

Acceptance:

- Staged pending completions remain visible to scheduling even when no backend
  completion-ring records are ready.
- Source-credit blockage demands command/control send-CQ resource relief.
- Debug assertions reject any path that maps completion-publish source credit to
  payload-frontier wait.

### Slice 7: logical split of collection and publication

Status: compatibility bridge to the larger scheduler redesign.

1. Introduce the code boundary now even if the current enum/action names are not
   fully renamed: `COLLECT_BACKEND_COMPLETIONS` stages command completion state;
   `PUBLISH_COMMAND_COMPLETIONS` publishes staged state to the local or peer
   frontend completion mailbox.
2. Keep the existing selected completion-ring executor as a compatibility wrapper
   until the aggregate-action scheduler migration owns these as separate
   candidates.
3. Make the split visible in comments, counters, and blocked-reason reporting so
   later scheduler work does not have to infer it from side effects.

Acceptance:

- Completion collection can make progress without immediate peer-client RDMA
  publication if source credit is unavailable.
- Completion publication can make progress without new backend completion-ring
  input once the staged state exists.

### Slice 8: remote publication policy cleanup

Status: no longer the first WIMM implementation point. Slice 4b proved the WIMM
ordering idea but also showed that receiver-service CQ dispatch is the wrong host
fast path. Slice 4c is the required two-WR self-publishing WIMM ready-word path;
this later slice is only for policy cleanup or optional A/B variants after Slice
4c is stable.

1. Keep the frontend stable slot-copy validation as a defensive local protocol
   check.
2. Treat the WIMM-written ready word as the normal remote visibility gate for
   peer-client completions on host-polled client/backend paths.
3. Keep receiver-service WIMM CQ handling for transport progress, receive repost,
   diagnostics, optional wakeups, and future offload/scheduler facts, not as the
   normal host client publication step.
4. Compare future variants only if a new design preserves the body-before-ready
   gate and the same local source-ring retirement scheme.

Acceptance:

- The two-WR WIMM ready-word path remains correct on remote c1/c4.
- Any alternative policy must match WIMM correctness and must not reintroduce
  hidden receiver-side progress requirements.

## Verification plan

Correctness checks:

1. Build and install Postgres plus Citus/Homer on both hosts, then verify the
   installed `pgbench`, `citus_tuple_sink_service`, and `libhomer_client.a`
   agree on the completion mailbox protocol/version/size contract.
2. Run local farnet1 pgbench c1 as a smoke test for local completion mailbox
   behavior.
3. Run remote RDMA farnet0-to-farnet1 pgbench c1 and c4 from a clean runtime
   baseline.
4. Run concurrent remote pgbench plus background remote RDMA basebackup to catch
   payload semantic deferral regressions.
5. Rebuild with diagnostic counters and a deliberately tiny completion-publish
   source ring to force source-credit blockage.

Performance checks:

1. Use no-stats binaries for accepted measurements.
2. Treat first post-restart run as warmup; compare warmed repeat bands.
3. Record TPS, p95/p99 latency, service CPU, completion publications per
   transaction, signaled CQEs per transaction, source-ring-full count, and CQ
   retirement batch size.
4. Acceptance for the first async source-ring version is correctness plus no
   remote c1/c4 regression. The target is to remove the per-completion blocking
   local CQ wait and improve c1 latency/TPS while preserving c4 stability.
5. Keep Slice 4b as the accepted remote correctness bridge, not as the accepted
   performance baseline. The next performance baseline is Slice 4c: body WRITE
   plus WIMM ready-word self-publication.

## Pitfalls, caveats, and hidden constraints

- Do not make `TupleSinkServiceWriteConnectionBytes()` globally asynchronous. It
  documents synchronous semantics and may be used by control paths that rely on
  immediate local retirement.
- Do not use one reusable scratch completion for async publication. That recreates
  the exact source-lifetime hazard the source ring is meant to remove.
- Do not route source-credit blockage through
  `TupleSinkServicePublishPendingPeerClientCommandCompletions()`. That helper is
  payload/result semantic retry, not local CQ/resource retry.
- Do not assume `publishedEpoch` is a remote visibility fence by itself. The
  reader must validate the slot-local ready word and command identity.
- Do not require the frontend client to consume an RDMA immediate on the host
  fast path. The client polls memory; the immediate belongs to the receiver
  service's transport CQ path unless a separate client CQ/event path is explicitly
  designed.
- Do not put receiver-service token decode/session demux on the normal
  peer-client completion visibility path. If the WIMM WR already writes the
  slot-local ready word, the frontend can observe completion readiness without a
  receiver CPU store.
- Do not describe recv-CQ handling as the host-client publication mechanism in
  Slice 4c. The receiver should drain CQEs promptly for credit/repost and
  diagnostics, but host-client visibility comes from the WIMM-written memory gate.
- Do not collapse the fallback two-WR path into one `[body][ready]` WIMM unless
  the target MR/MKey and QP ordering evidence proves that ready visibility implies
  prior body visibility for the same WR.
- Do not retire source slots from an unrelated CQE. Range retirement is valid
  only for older WRs on the same RC QP as the signaled checkpoint, and the chosen
  implementation uses a connection-local FIFO of posted source-slot refs to avoid
  scanning unrelated sessions.
- Do not judge the `WRITE_WITH_IMM` policy until receiver-side progress ownership
  is explicit. A remote CQE that the frontend client does not consume may add
  work without improving the client-visible polling path; this is exactly why
  Slice 4c moves the publication gate into the polled ready word.
- Do not confuse direct-fill source slots with true zero copy. Direct fill means
  the Homer service materializes the final `CitusRemoteExecCommandCompletion`
  directly into a registered source slot. True zero copy would mean the
  DB-backend or frontend/client API writes directly into an RDMA-retired source
  slot that the Homer service later posts from, which is a larger ownership and
  ABI change.

## Larger True-Zero-Copy Direction

The long-term preferred direction is a cooperative registered source-ring ABI
between the DB-backend/frontend library and the Homer service:

1. The producer reserves a source slot from a ring whose memory is registered for
   the RDMA connection or for the chosen transport domain.
2. The producer writes the final transport-visible command or completion object
   directly into that slot, then publishes a slot-local ready word.
3. The Homer service observes ready slots, posts RDMA from those slots, and owns
   source-slot retirement until the relevant send CQE or tagged checkpoint says
   the RNIC no longer references the bytes.
4. The producer must not reuse the slot until Homer publishes a local retired
   frontier or per-slot state.

This can remove service-side object copying for simple command/completion
objects, but it is not just an implementation detail. It changes the shared
memory contract between producer and Homer service. In particular,
result-producing completions currently require service-side transformation in
[`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:12681):
the service maps backend-node result stream identity to frontend-node receive
stream identity and writes the peer receive queue descriptor into the completion.
True zero copy for those completions requires one of these designs:

- expose final peer receive-stream metadata to the producer before it fills the
  source slot;
- split the object so the producer fills stable DB facts and Homer patches only a
  small service-owned transport descriptor region before posting;
- keep a deliberate service-side serialization/materialization stage for
  commands or completions that need nontrivial transformation.

The current implementation should therefore proceed with direct-fill registered
source slots and async local CQ retirement first. True zero copy is a follow-on
redesign once the source-slot lifetime protocol, staged publication facts, and
send-CQ retirement are correct and measurable.

## Interactions and cross-links

- [`homer_unified_aggregate_action_scheduler_plan.md`](homer_unified_aggregate_action_scheduler_plan.md) should consume the cleaner staged publication and source-credit facts when it migrates `PUBLISH_COMMAND_COMPLETIONS` and `DRAIN_SEND_CQ`.
- [`peer_service_push_completion_implementation_plan.md`](peer_service_push_completion_implementation_plan.md) covers service-to-service peer command completion rings. This note covers peer-client completion publication into the frontend client's completion mailbox.
- [`rdma_publication_visibility_and_doorbells.md`](rdma_publication_visibility_and_doorbells.md) remains the broader publication/visibility note for comparing memory polling, publish words, and doorbell-style notification.
- [`homer_transport_scheduler_and_payload_streams.md`](homer_transport_scheduler_and_payload_streams.md) contains the broader scheduler-ready history and payload-stream context that motivated this narrower cleanup.

## Open questions / TODO

- Choose the exact source-ring size after the first no-stats c1/c4 measurements.
  The initial signaled checkpoint interval is already chosen as
  `signaledInterval <= ringSlots / 2`.
- Revisit whether source storage should stay per-session after measurements. The
  first design uses per-session source rings plus a connection-local outstanding
  FIFO for same-QP retirement.
- Decide whether to add a dedicated `HOMER_PROGRESS_WAIT_COMPLETION_PUBLISH_SEND_CQ`
  enum or reuse command/control send-CQ wait with a specific blocked-reason flag.
- Decide whether slot-local ready words should be a parallel array or a wrapped
  completion-slot struct before editing the shared protocol header.
