# Homer Peer-Client Completion Publication Pipeline Plan

## Scope

- **What this doc explains**: the targeted plan for fixing cross-node client-SQL
  command completion publication: reader-side slot validation, nonblocking RDMA
  source lifetime, send-CQ retirement, and scheduler-visible retry/resource
  facts.
- **What this doc does NOT cover**: the full unified aggregate-action scheduler
  migration, service-to-service peer command completion rings, the full payload
  byte-ring lifecycle redesign, or DOCA/DPU offload. This note does record the
  tuple-result byte-ring EOS/command-boundary bug exposed during peer-client
  completion validation because it currently blocks this publication pipeline.
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
- June 19 follow-up: the clean fix is the explicit peek/apply/ack contract. The
  Slice 4c transport shape is not the next thing to rewrite. The long-run wedge
  evidence shows that publication can reach the client mailbox and still leave
  pgbench waiting because the frontend event can be acknowledged separately from
  pgbench state application. Treat this as a pre-existing pushed-completion API
  ownership bug that Slice 4c exposed by making publication faster and less
  serialized.
- Important caveat: the current measured pgbench hot path usually enters
  [`HomerClientStartDirectCommand()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2040)
  from
  [`HomerClientStartCommandWithCompletionFlags()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2177)
  once the direct command/completion mailboxes are mapped. Therefore
  [`HomerClientMarkPushedCompletionConsumedIfPresent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1100)
  is an unsafe helper and should be removed from the start-command path, but it
  is not necessarily the exact consumer responsible for the final captured
  direct-mailbox wedge. The generic destructive-consumption contract in
  [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2447)
  and its internal skip branch remain the primary root-cause target.

June 19 accepted follow-up after implementing Slice 4d and Slice 4d-r:

- Slice 4d's peek/apply/ack direction remains accepted for frontend completion
  ownership. The subsequent c1 failure was not solved by more command-completion
  replay/dedup logic; it was a tuple-result byte-ring generation/EOS ownership
  bug exposed by the cleaner pushed-completion path.
- Slice 4d-r is now implemented and validated. Tuple-result records carry an
  explicit result generation, physical byte-ring frontiers remain monotonic
  across persistent queue reuse, and EOS is backend-owned as an immutable
  in-band record rather than a post-publication mutation or service-side repair.
- The remaining local-blackhole basebackup stall was a separate scheduler
  admission hole: local blackhole streams are producer-side byte-ring smoke
  streams and must be admitted even when remote-payload reason masks are empty.
  The scheduler now admits them before the blackhole pump checks byte-ring
  readiness.
- Current accepted validation band on the fixed tree: remote c1 passed at about
  `4.17k` and `3.76k TPS`; remote c4 passed at about `10.47k` and
  `10.39k TPS`; local blackhole basebackup passed in `10.44s`; remote RDMA
  basebackup warmed repeats passed in `4.43s` and `4.32s`.

June 19 validation update after implementing Slice 4d:

- Slice 4d was implemented and pushed on
  `homer-state-machine-scheduler-milestone`: `postgres-citus` commit
  `555862ee997` and `citus-dbcomm` commit `8befe12df`.
- No-stats Citus/Homer and relinked `pgbench` were rebuilt, installed, synced to
  `farnet0`, and verified with matching hashes for
  `/data/dbcomm/pg-citus/bin/pgbench`,
  `/data/dbcomm/pg-citus/bin/citus_tuple_sink_service`, and
  `/data/dbcomm/pg-citus/lib/x86_64-linux-gnu/libhomer_client.a`.
- Remote RDMA c1 still failed correctness, so no performance result should be
  claimed from this state. The first failure happened before any successful
  transaction:

  ```text
  tuple result byte-ring record header mismatch at byte_head=80
  expected_sequence=2 protocol=8 sink_sequence=1 payload_bytes=56
  ```

- A live `farnet0` result-ring dump showed two tuple-result records at byte
  offsets `0` and `80`, both with `sinkSequence=1`. Later dumps showed that the
  second record can be the previous row record republished with EOS, not only a
  zero-row synthetic EOS marker.
- The "fallback" involved here is not a desired recovery path in the completion
  publication pipeline. It is the tuple-result EOS fallback in
  [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801):
  first
  [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1686)
  tries to mutate the last already-published tuple record to mark it terminal;
  if that fails,
  [`SubmitCitusTupleSinkEmptyEosRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1720)
  appends a separate terminal record. This dual terminal-result shape is now
  suspect because it creates two representations of result EOS and lets backend
  mutation timing race service-side payload publication.
- A service-side hardening attempt added
  [`HomerServiceTupleViewNextEosSequenceFromProducerBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19445)
  and used it from
  [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532)
  so synthetic EOS sequence selection scans producer byte-ring records instead
  of trusting `payloadSenderPostedTail + 1`. This did not remove the duplicate
  previous-row terminal marker because the duplicate can be produced by the
  backend/local tuple-sink EOS path before the service fallback is the decisive
  code path.
- A client-side diagnostic tolerance was added in
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2875):
  if the next visible record is a previous-sequence EOS/terminal marker, consume
  it without counting its tuple payload again. This improved progress but is not
  the target design and should not be extended into a general tolerance scheme.
- After that tolerance, remote RDMA c1 progressed to `10596/20000` transactions
  before failing:

  ```text
  tuple result byte-ring record header mismatch at byte_head=1695712
  expected_sequence=1 protocol=8 sink_sequence=2 payload_bytes=40
  transactions actually processed: 10596/20000
  tps = 2840.750670
  ```

  The TPS number is diagnostic-only because the run aborted.
- Current suspected cause: peer-client completion publication now reaches the
  frontend reliably enough to expose result-drain correctness. The persistent
  tuple-result byte ring is reused across commands, while tuple-view
  `sinkSequence` is command-local. EOS publication and command-boundary draining
  do not have one clean owner. Result termination can be represented by mutating
  the last already-published record or by publishing an extra terminal record;
  under backend/service timing this can leave the next command's frontend drain
  positioned at a record whose sequence is not command-local `1`.
- June 19 source review promotes several parts of that diagnosis from suspicion
  to confirmed protocol defects:
  - Published tuple-result records are mutable. `SubmitCitusTupleSinkBatch()`
    publishes `publishedTail` after filling the record, but
    [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1686)
    can later set `transportHeader->flags |=
    CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS` on that already-published record.
  - EOS has multiple semantic producers.
    [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
    first mutates the last record via
    [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1686),
    then falls back to
    [`SubmitCitusTupleSinkEmptyEosRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1720).
    The service also has
    [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532)
    to repair the race where it copied the final data record before seeing the
    backend's post-publication EOS mutation.
  - The producer resets a shared byte-ring control block across commands.
    [`RemoteExecResetResultQueueControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:388)
    clears both `publishedTail` and `consumedHead`, and
    [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:755)
    invokes that reset when reusing a persistent result queue for a compatible
    result shape. A local reset is not a distributed reclamation barrier for
    service-side posted state, RDMA-visible receiver rings, or frontend mappings.
  - [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:373)
    has only `protocolVersion`, `payloadBytes`, `sinkSequence`, `flags`, and
    `reserved0`; it has no command/result generation. Therefore
    `sinkSequence=1` cannot answer "sequence 1 of which command?"
- Current direction: stop adding client-side tolerance cases. The next repair
  slice must make tuple-result records immutable after publication, assign one
  backend-owned in-band EOS object per result generation, keep physical byte-ring
  frontiers monotonic for the mapping lifetime, and tag every tuple-result record
  with a result generation before c1/c4 performance validation resumes.

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

Status: implemented as a dirty prototype and short-run c4 validated, but not
accepted. Long-run c4 is blocked by Slice 4d frontend completion ownership.

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

### Slice 4d: frontend completion peek/apply/ack ownership

Status: implemented and accepted as the frontend completion ownership repair on
June 19, 2026. The first validation of this slice exposed, rather than caused, a
separate tuple-result byte-ring EOS/command-boundary ownership bug. Slice 4d-r
fixes that result-ring bug; only the combined Slice 4d + Slice 4d-r state should
be used for performance claims.

The current pushed-completion client API is destructive: reading a completion can
also advance `lastConsumedCompletionEpoch` and remote `consumedEpoch`. That is
unsafe for pgbench because
[`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3665)
still has to apply the event in
[`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3497)
after the library call returns. The clean ownership contract is:

1. **Peek/lease**: acquire and validate the next frontend completion event into
   session-owned client storage without acknowledging it.
2. **Apply**: let pgbench apply the copied event to its command/result-sink state.
3. **Ack**: advance the consumed epoch only after the event has been applied.

Long-term target: remove dual semantic delivery. The measured async pgbench path
should not treat the `START_COMMAND` return value and the pushed mailbox as two
independent representations of the same command state. Either start submission
returns only command-submission facts and all state transitions arrive through
the ordered completion stream, or any inline start result must carry the exact
completion epoch/event identity that pgbench later applies and acknowledges. A
helper that scans the pushed mailbox by only `commandKind + commandSequence` is
not a valid deduplication protocol.

Immediate cleanup before adding the new API:

1. Remove the call to
   [`HomerClientMarkPushedCompletionConsumedIfPresent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1100)
   from the control-slot path in
   [`HomerClientStartCommandWithCompletionFlags()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2177).
   A start-command helper must not consume pushed mailbox events behind the
   caller's state machine. If the helper remains for a compatibility path, it
   should become debug-only or be deleted after the new API lands.
2. Remove or disable the recursive older no-result skip branch in
   [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2561).
   In the one-command-in-flight pgbench path, a sequence/kind mismatch should be
   a non-destructive mismatch result or protocol error. The client library must
   not acknowledge an event merely because it believes the current caller will
   not use it.
3. Keep `lastTerminalCompletion*` replay fields only as temporary diagnostics
   during this slice. Do not add new replay cases. Delete replay after long c4
   passes with peek/apply/ack.

New client API shape:

```c
typedef enum HomerClientCompletionPeekStatus
{
    HOMER_CLIENT_COMPLETION_PEEK_NOT_READY = 0,
    HOMER_CLIENT_COMPLETION_PEEK_READY,
    HOMER_CLIENT_COMPLETION_PEEK_BODY_VISIBILITY_PENDING,
    HOMER_CLIENT_COMPLETION_PEEK_OVERRUN,
    HOMER_CLIENT_COMPLETION_PEEK_MISMATCH
} HomerClientCompletionPeekStatus;

typedef struct HomerClientCompletionLease
{
    uint64_t epoch;
    uint64_t sessionGeneration;
    uint32_t slotIndex;
    CitusRemoteExecCommandCompletion completion;
} HomerClientCompletionLease;
```

1. Add one session-owned pending completion lease to
   [`HomerClientSession`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:53),
   for example `completionLeaseActive` plus `completionLease`. The lease is the
   client library's durable copy of the mailbox event until pgbench applies and
   acknowledges it.
2. Add `HomerClientPeekNextCompletionEvent(session, lease, status,
   errorMessage, errorMessageBytes)`. The function must:
   - compute `expectedEpoch = session->lastConsumedCompletionEpoch + 1`
   - if a lease is already active, return that same lease without rereading or
     modifying the mailbox
   - stable-copy the expected slot using `readyEpochSlots[slotIndex]`
   - require the body-resident epoch described below to equal `expectedEpoch`
   - validate protocol version and command state
   - return `NOT_READY`, `READY`, `BODY_VISIBILITY_PENDING`, `OVERRUN`, or
     `MISMATCH`
   - activate the session lease only for a valid `READY` event
   - not store `consumedEpoch`
   - not update `lastConsumedCompletionEpoch`
   - not recurse
   - not skip or coalesce visible events
3. Pgbench may validate the leased event against its pending command kind and
   sequence after peek. A mismatch is a non-destructive protocol error in the
   current one-command-in-flight model; it must not be treated as a reason to
   skip or acknowledge the event.
4. Add `HomerClientAckCommandCompletion(session, eventEpoch, sessionGeneration,
   errorMessageBytes)`. The function must:
   - require an active lease
   - require `eventEpoch == session->lastConsumedCompletionEpoch + 1`
   - require the epoch and session generation to match the active lease
   - release-store the mailbox `consumedEpoch`
   - update `session->lastConsumedCompletionEpoch`
   - clear the active lease
   - update terminal diagnostic fields only after the caller confirms the event
     was applied
5. Session reset invalidates the active lease. ACK of a stale, future,
   non-leased, or wrong-generation epoch should fail a debug assertion and
   return a hard error in non-assert builds.
6. Keep the old destructive
   [`HomerClientTryCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2447)
   as a compatibility wrapper only after the new API exists. It may internally
   call peek and ack for non-pgbench callers that intentionally want the old
   semantics, but pgbench must not use it on the measured path.

Publication epoch in the body:

1. Add a body-resident publication epoch to the frontend client completion slot,
   for example:

   ```c
   typedef struct CitusRemoteExecClientCompletionSlot
   {
       uint64_t bodyEpoch;
       CitusRemoteExecCommandCompletion completion;
   } CitusRemoteExecClientCompletionSlot;
   ```

   This may replace the current bare `completionSlots[]` element type in
   [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:491).
2. The sender writes `bodyEpoch = nextEpoch` together with the completion body,
   then writes the slot-local ready word with WIMM.
3. The reader accepts a slot only when
   `ready1 == expectedEpoch`, `bodyEpoch == expectedEpoch`, and
   `ready2 == expectedEpoch`. If the ready word is new but the body epoch is old
   or different, return `BODY_VISIBILITY_PENDING`, increment a diagnostic
   counter, and retry without ACK. Do not classify the stale body as a legitimate
   older command completion.
4. This does not remove the Slice 4c ordering requirement. It gives validation
   evidence and a non-destructive retry path if body visibility trails the ready
   gate.

Pgbench conversion:

1. Convert
   [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3665)
   to call `HomerClientPeekNextCompletionEvent()`.
2. If peek returns `NOT_READY`, keep the existing running-result-sink drain
   behavior and return `commandComplete=false`.
3. If peek returns `BODY_VISIBILITY_PENDING`, do not apply or acknowledge
   anything. Return incomplete after optional result-sink drain and count the
   event separately from ordinary not-ready polls.
4. If peek returns `READY`, validate the leased event against
   `homer_pending_command_kind` and `homer_pending_command_sequence`, then call
   [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3497)
   with the leased event. Only after event application should pgbench call
   `HomerClientAckCommandCompletion()` with the lease epoch and generation.
5. Refine `HomerApplyCommandCompletion()`'s result contract so pgbench can
   distinguish:
   - event application succeeded and the command remains running
   - event application succeeded and the command completed successfully
   - event application succeeded and the backend reported command failure
   - event application chose not to apply yet and should retry without ACK
   - event application failed locally while opening/draining the result sink

   Backend `FAILED` is still an applied terminal event and should be acked before
   pgbench transitions to error. A local frontend failure while applying the
   event should not silently ack; it should mark the Homer session/connection
   broken or take a deliberate cleanup path.
6. Duplicate STARTED/sink-ready events must be handled explicitly and
   idempotently. If a STARTED event was already observed through the immediate
   start response and later appears in the pushed mailbox, pgbench should apply
   the duplicate to the extent needed, then ack that exact mailbox epoch. It must
   not consume a later terminal completion just because it has the same command
   sequence.

Terminal event invariant:

1. Disable and do not plan to restore the result-descriptor flag stripping
   optimization in
   [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13105)
   for the fixed-size publication path. If a terminal completion has result-sink
   state, it should carry `TUPLE_SINK_READY` and the descriptor as a
   self-contained final snapshot. Repeating the descriptor is acceptable because
   pgbench already checks
   `!homer_pending_result_sink_bound` before opening a result sink.
2. If descriptor bandwidth later matters, add an explicit persistent descriptor
   id/version. Do not encode dependency on an earlier STARTED event merely by
   clearing the descriptor flag on a terminal event.

Acceptance:

- `HomerClientTryCommandCompletion()` is no longer used by pgbench's measured
  Homer path.
- No frontend completion event advances `lastConsumedCompletionEpoch` or mailbox
  `consumedEpoch` before pgbench has applied that event.
- Every acknowledged epoch has exactly one corresponding applied-event record in
  the pgbench/Homer diagnostic counters.
- Repeated peek before ACK returns the same session-owned lease.
- ACK of a stale, future, non-leased, or wrong-generation epoch fails.
- Backend `FAILED` completions are acknowledged after pgbench applies the failure
  transition.
- `readyEpoch == expected && bodyEpoch != expected` retries without consuming and
  increments a distinct diagnostic counter.
- Mismatched command kind or sequence is never acknowledged.
- The recursive older-completion skip branch is gone from the measured path.
- The `lastTerminalCompletion*` replay path is either removed or remains
  diagnostic-only with a counter/log proving it is not used during accepted c1/c4
  runs.
- Terminal result completions are self-contained and do not depend on an earlier
  STARTED/sink-ready event for descriptor state.
- Remote c1 passes.
- Remote c4 `-t2500` short repeats pass.
- Remote c4 `-t10000` long repeats pass at least three times from a clean
  process baseline, with no client stuck in `CSTATE_WAIT_RESULT`.
- Warmed c4 performance returns to the previous roughly `11k TPS` target band
  before the Slice 4c transport work is accepted as recovered.
- Slice 4b and Slice 4c both pass the same long-c4 ownership test. Slice 4c
  additionally reports zero persistent body-epoch mismatches.

Implementation progress:

- Added a session-owned frontend completion lease in
  [`HomerClientSession`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_client.h:38)
  and exposed
  [`HomerClientPeekNextCompletionEvent()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2330)
  plus
  [`HomerClientAckCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2465).
- Converted
  [`receiveHomerCommand()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3685)
  to peek, validate the leased event, apply it through
  [`HomerApplyCommandCompletion()`](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3506),
  and ACK only after application.
- Removed the terminal replay/dedup path from
  [`homer_client.c`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1019)
  and removed START-response mailbox consumption from
  [`HomerClientStartCommandWithCompletionFlags()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2250).
- Added a body-resident epoch to frontend completion slots in
  [`CitusRemoteExecClientCompletionMailbox`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:485)
  and made the peer RDMA publisher write `bodyEpoch + completion` before the
  ready WIMM.
- Kept remote mailbox credit enforcement as Slice 4f; this implementation still
  relies on the current normal mailbox geometry rather than a sender-side remote
  consumed-epoch mirror.
- Initial validation rejected the Slice 4d-only state before performance
  measurement. Remote c1 first failed at
  `byte_head=80 expected_sequence=2 sink_sequence=1`; after a diagnostic
  previous-sequence terminal-marker tolerance in
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904),
  it progressed to `10596/20000` transactions and then failed at
  `byte_head=1695712 expected_sequence=1 sink_sequence=2`. This is now tracked
  as the tuple-result EOS/command-boundary ownership blocker fixed by Slice
  4d-r, not as a completion-publication performance result.
- The service-side helper
  [`HomerServiceTupleViewNextEosSequenceFromProducerBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19445)
  and client-side previous-sequence terminal-marker handling are diagnostic
  hardening from the failed validation, not final architecture. They should not
  grow into a broad "tolerate malformed result stream" policy.

Resolved blocker that forced Slice 4d-r:

- The tuple-result EOS fallback in
  [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
  chooses between mutating the last already-published record via
  [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1686)
  and appending a separate terminal record via
  [`SubmitCitusTupleSinkEmptyEosRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1720).
  This should be treated as a design bug until proven otherwise. The next slice
  should define one owner and one command-boundary rule for tuple-result EOS and
  byte-frontier advancement before further c4 performance work.

### Slice 4d-r: tuple-result generation and immutable EOS repair

Status: implemented and accepted for remote pgbench c1/c4 plus local/remote
basebackup validation on June 19, 2026. This slice supersedes the diagnostic
service-side EOS scan and frontend previous-sequence EOS tolerance.

The Slice 4d completion lease is still the right ownership model for command
completion events. The blocker is the tuple-result payload stream: it does not
provide stable identity, single ownership, immutable publication, or exact
generation acknowledgement. Fix this before returning to c4 performance work.

Target invariants:

1. Bytes below a tuple-result ring's `publishedTail` are immutable until the
   consumer advances `consumedHead` past them.
2. The backend producer appends exactly one semantic EOS record per result
   generation. The service transports DATA/EOS records and gates terminal
   completion on EOS visibility; it does not create or repair semantic EOS.
3. Physical byte-ring frontiers are monotonic for the mapping lifetime. Do not
   reset `publishedTail` or `consumedHead` between commands on a reused
   queue/mapping.
4. Every tuple-result DATA/EOS record carries a result generation. The initial
   generation can be the command sequence already used by
   [`RemoteExecInitResultSinkKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:375).
5. Frontend result draining binds to a descriptor that names the generation and
   start frontier. The client validates generation before command-local sequence.
6. Queue/session lifetime close, transport failure, and per-command result EOS
   are distinct states. Do not encode per-command EOS only as
   `CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_CLOSED` when the queue remains open
   for later commands.

Rejected mitigations:

- accepting previous-sequence EOS in
  [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2875);
- retry-spinning on a valid but semantically mismatched tuple-result header;
- scanning producer bytes to infer a synthetic EOS sequence in
  [`HomerServiceTupleViewNextEosSequenceFromProducerBytes()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19445);
- appending service-side semantic EOS in
  [`HomerServiceAppendTupleViewEosRecordToProducerByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19532);
- republishing a data record as a terminal record;
- full shared-control-block resets in
  [`RemoteExecResetResultQueueControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:388)
  for a persistent queue.

Implementation outcome:

- The tuple-sink transport ABI is now protocol version `9` and
  [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:380)
  carries `headerBytes`, `recordKind`, `resultGeneration`,
  `ringRecordOrdinal`, and `generationSequence`.
- [`RemoteExecPrepareResultQueueGeneration()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:405)
  assigns the per-command tuple-result generation and records the producer
  `startByteTail`. [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:792)
  no longer treats reused persistent result queues as command-local frontier
  resets.
- [`ResetCitusTupleSinkSendHandle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1200)
  retargets logical tuple-sink state for the new generation without resetting
  the physical byte-ring control block.
- [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1767)
  is intentionally disabled for byte-ring publication, and
  [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1863)
  appends an explicit backend-owned EOS record through
  [`SubmitCitusTupleSinkEmptyEosRecord()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1781).
  Published DATA records are no longer mutated into terminal records.
- [`HomerServicePrepareTupleResultStreamForCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17626)
  binds the service-side stream to the backend descriptor's generation and
  source start tail before publishing the peer-visible result descriptor.
- [`HomerServicePayloadTransportHeaderReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21679)
  validates the new transport envelope and generation/sequence identity before
  tuple-result or basebackup records are considered ready.
- [`HomerClientOpenResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2727)
  binds frontend draining to the descriptor's `resultGeneration` and
  `startByteTail`; [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2904)
  validates generation before command-local sequence and no longer accepts
  previous-sequence EOS as a tolerance path.
- Basebackup records also fill the v9 transport fields in
  [`HomerClientSubmitBaseBackupRecord()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1697)
  with `resultGeneration = 0`. This is deliberate: basebackup is not a reused
  client-SQL tuple-result generation, but it still uses the same transport header
  shape.

Implementation correction discovered during validation:

- The first post-implementation local basebackup blackhole validation stalled
  after the producer filled the byte ring. The generation/header work was not
  the cause. The scheduler admission path skipped local blackhole streams when
  the generic remote-payload reason masks were empty, before
  [`HomerServicePopulatePayloadMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26980)
  could mark `PAYLOAD_LOCAL_BLACKHOLE` ready. The fix admits local blackhole
  streams even with empty generic masks at
  [`tuple_sink_service_process.c:27101`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27101),
  then lets the existing pump
  [`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21045)
  consume/release the producer byte ring. This does not change remote RDMA
  payload readiness.
- Why the scheduler was involved at all: local Homer blackhole basebackup is not
  an RDMA sender and does not look like ordinary peer-bound outgoing payload.
  Its work is a service-side drain of the backend producer byte ring so
  PostgreSQL can keep publishing archive/basebackup chunks. In the current
  `machine-baseline` policy, payload work is run only after a payload stream is
  admitted as a machine candidate and converted into a payload grant. The
  blackhole path had an always-ready action in
  [`HomerServicePopulatePayloadMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27012),
  but [`HomerServiceBuildPayloadStreamMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:27084)
  filtered out streams with no generic `readyReasonMask` or
  `blockedReasonMask` before that action could be attached. As a result, the
  blackhole pump was never scheduled, the producer byte ring was never released,
  and the `pg_basebackup TARGET 'homer:mode=blackhole,...'` workload stalled
  after a small amount of data.
- The fix is deliberately narrow: compute `localBlackholeReady =
  HomerServicePayloadStreamIsLocalBaseBackupBlackhole(streamEntry)` during
  candidate construction and bypass the empty-reason-mask filter only for that
  local smoke path. Remote RDMA payload streams still require their normal
  outgoing/incoming/CQ/credit reason masks. This keeps the scheduler model honest:
  local blackhole is an explicit payload action that must be granted, not hidden
  progress inside some unrelated service loop.

Historical implementation steps, now completed:

1. Add narrow compile-gated tracing before changing semantics. Record command
   sequence/result generation, record kind, command-local sequence, physical
   byte start/tail, published/consumed frontiers, EOS decision, and service post
   frontier at:
   - [`SubmitCitusTupleSinkBatch()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1582)
   - [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
   - [`RemoteExecEnsureSessionResultQueue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:755)
   - the tuple-view branch in
     [`HomerServicePostOutgoingPayloadByteRing()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19740)
   - [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2875)
2. Bump the tuple-result transport ABI and reject mixed binaries. Extend
   [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:373)
   with `headerBytes`, `recordKind`, `resultGeneration`,
   `ringRecordOrdinal`, and `generationSequence`. Keep
   `generationSequence` as the command-local 1..N validator and make
   `ringRecordOrdinal` monotonic for the physical ring.
3. Replace post-publication EOS mutation with explicit backend-appended EOS:
   - remove the published-record mutation path in
     [`MarkCitusTupleSinkLastRecordEos()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1686);
   - make [`MarkCitusTupleSinkPeerClosed()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service.c:1801)
     always append an immutable EOS record for row-producing tuple results;
   - canonicalize zero-row results as `EOS sequence 1`, one-row results as
     `DATA sequence 1` plus `EOS sequence 2`, and N-batch results as
     `DATA 1..N` plus `EOS N+1`.
4. Remove service-side semantic EOS synthesis. The service may observe missing
   EOS as a protocol error and fail the stream, but it must not manufacture a
   replacement EOS after forwarding data.
5. Stop per-command control-block reset for persistent result queues. Replace
   [`RemoteExecResetResultQueueControl()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:388)
   reuse with generation-open initialization that records:
   `resultGeneration`, `startByteTail`, `firstRingRecordOrdinal`, and
   `nextGenerationSequence=1`. Only initialize `publishedTail`/`consumedHead`
   when creating a fresh queue mapping or after full producer/service/frontend
   teardown.
6. Carry the result-generation descriptor in STARTED/sink-ready completion
   metadata, and terminal completion metadata if needed:

   ```c
   typedef struct CitusRemoteExecResultStreamDescriptor
   {
       uint64_t resultGeneration;
       uint64_t startByteTail;
       uint64_t firstRingRecordOrdinal;
   } CitusRemoteExecResultStreamDescriptor;
   ```

   The frontend must open/bind a result sink to this descriptor before draining.
7. Update [`HomerClientDrainResultSink()`](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:2875)
   to validate `resultGeneration` first, then `generationSequence`. A wrong
   generation is a hard protocol error unless it is before `startByteTail` and
   can be skipped by an explicit wrap-gap rule. Do not consume a mismatched
   generation as a tolerance path.
8. Add service-side generation state for tuple-view streams:
   `GENERATION_OPEN`, `DATA_COLLECTED`, `EOS_COLLECTED`, `EOS_POSTED`,
   `EOS_SEND_CQE_RETIRED`, `TERMINAL_COMPLETION_PUBLISHED`, and
   `GENERATION_RECLAIMABLE`. Terminal command completion must not be published
   until the backend-owned EOS record for the same generation reaches the
   required payload visibility/source-release frontier.
9. For the first robust version, keep one command in flight per session and
   require the previous generation to be reclaimable before opening the next
   result generation. Later, generation tags can allow multiple generations in
   one physical byte ring once remote credit/reclamation is measured.
10. Delete diagnostic-only scaffolding after the generation protocol passes c1:
    previous-sequence EOS acceptance, synthetic EOS sequence scans, service-side
    semantic EOS append, and any long header-mismatch retry loop that treats a
    semantically wrong record as a visibility delay.

Validation checkpoints:

- Compile-gated trace confirms the old failure interleaving or an equivalent
  violation before semantic changes land; if tracing shows a different
  interleaving, the invariants above still remain required unless disproven.
- Fault-injection or targeted sleeps at these points cannot produce duplicate
  terminal records or cross-generation sequence aliasing:
  after DATA tail publication, after service collection but before RDMA post,
  before backend EOS append, before terminal completion publication, before the
  next command begins, and near byte-ring wrap.
- Asserted invariants:
  - published tuple-result bytes never mutate;
  - producer code never writes consumer-owned `consumedHead` on queue reuse;
  - exactly one EOS exists per result generation;
  - EOS sequence is N+1 after N DATA records;
  - terminal completion references the same generation as EOS;
  - frontend-consumed records always match the bound generation.
- Remote RDMA c1 correctness passes from a clean process baseline.
- Remote c4 short and long correctness passes from a clean process baseline.
- Only after correctness passes, warmed c4 performance is compared against the
  previous roughly `11k TPS` target band. Any remaining gap must be attributed
  with counters before moving on to broader scheduler-policy work.

June 19 validation evidence after Slice 4d-r plus the local-blackhole scheduler
admission fix:

- Rebuilt Citus/Homer no-stats binaries with `CPPFLAGS='-D_GNU_SOURCE'`,
  reinstalled into `/data/dbcomm/pg-citus`, relinked the Postgres server where
  needed, synced the installed prefix to `farnet0`, and verified matching
  service/client-library hashes on both hosts.
- Remote RDMA pgbench c1 correctness passed:
  - `20000/20000`, zero failures, `4166.78 TPS`, p50 `0.238 ms`,
    p95 `0.250 ms`, p99 `0.264 ms`;
  - repeat `20000/20000`, zero failures, `3758.97 TPS`, p50 `0.264 ms`,
    p95 `0.279 ms`, p99 `0.294 ms`.
- Remote RDMA pgbench c4 correctness and performance recovered to the expected
  near-11k band:
  - `40000/40000`, zero failures, `10473.76 TPS`, p50 `0.371 ms`,
    p95 `0.533 ms`, p99 `0.618 ms`;
  - repeat `40000/40000`, zero failures, `10388.04 TPS`, p50 `0.372 ms`,
    p95 `0.533 ms`, p99 `0.603 ms`.
- Local Homer blackhole basebackup validated the generation-zero basebackup
  transport path: `RC=0`, `real 10.44s`.
- Remote RDMA basebackup validated correctness and did not regress: warmup
  `6.78s`, warmed repeats `4.43s` and `4.32s`.
- A false lead during validation was rejected: increasing basebackup
  `recordBytes` by `sizeof(CitusRemoteBaseBackupMessageHeader)` failed
  immediately with `basebackup record is too large`. This proved
  `slotReservedPrefixBytes` already includes the semantic basebackup header, so
  the final code keeps `recordBytes = slotReservedPrefixBytes + payloadBytes`.

### Slice 4e: partial-post failure and source-retirement cleanup

Status: required cleanup after Slice 4d, but not the primary cause of the
captured long-run c4 wedge. This is a correctness gate before treating
multi-WR publication failure as retryable work.

Slice 4c posts a multi-WR publication sequence. If the body WRITE posts but the
ready WIMM post fails, the source slot may still be referenced by an unsignaled
WR and cannot be safely released or retried as normal work. The current
`FAILED_OR_INFLIGHT` state is not explicit enough for clean recovery.

1. Split source slot states into at least:
   `FREE`, `RESERVED`, `BODY_POSTED`, `READY_POSTED`, `RETIRED`, and
   `FAILED_NEEDS_QP_RESET`.
2. If failure happens before any WR posts, clear the owner entry and release the
   source slot normally.
3. If failure happens after the body WR posts but before the ready WIMM posts,
   mark the source/owner as `FAILED_NEEDS_QP_RESET`, mark the peer
   connection/session failed, and prevent retry on that QP. This is a terminal
   transport state for that connection, not a normal blocked-publication state.
4. Cleanup of `FAILED_NEEDS_QP_RESET` state must happen through connection reset
   or QP teardown, not normal source-ring reuse.
5. Replace the current global peer-client completion CQ owner scan with the
   connection-local FIFO described in Slice 2 when practical. The global table is
   acceptable for prototype evidence, but the connection FIFO is the cleaner
   same-QP range-retirement model.

Acceptance:

- A forced failure before the body post releases source state normally.
- A forced failure after body post but before ready WIMM marks the connection
  failed/reset-required and never reuses that source slot on the live QP.
- Normal c1/c4 runs retire source slots through send-CQ checkpoints exactly as
  before.

### Slice 4f: remote mailbox credit enforcement

Status: required after Slice 4d/4e for a complete long-term protocol; may be
implemented before 4e if validation shows mailbox pressure before partial-post
fault injection.

Peek/apply/ack intentionally holds a frontend completion event unconsumed until
the semantic consumer has applied it. That makes the ownership boundary correct,
but it also means the peer-side producer must enforce the existing mailbox credit
contract before it chooses a remote slot. The frontend client completion mailbox
already has a fixed slot count and explicit `consumedEpoch`; local producers can
load those words directly. The missing piece is the cross-node producer's mirror
or refresh path for the remote `consumedEpoch`. Multiple slots provide slack,
but they are not by themselves a credit protocol unless the producer knows which
slots the remote client has acknowledged. One command can produce multiple
visible events, so overwrite protection cannot be inferred from local source-ring
availability.

1. Maintain a sender-side mirror of remote `consumedEpoch` for the peer-client
   completion mailbox.
2. Publish optimistically while
   `publishedEpoch - knownRemoteConsumedEpoch` is comfortably below
   `CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS`.
3. Near a high-water mark, refresh known remote credit by RDMA READ or an
   explicit receiver-to-sender credit update.
4. Block peer-client completion publication before
   `publishedEpoch - knownRemoteConsumedEpoch == slotCount`.
5. Report this as `BLOCKED_REMOTE_CREDIT`, separate from local source-ring credit
   and semantic payload dependency waits.
6. Feed this fact into the scheduler as command/completion resource relief,
   not as payload-frontier blockage.

Acceptance:

- A tiny completion mailbox blocks on `BLOCKED_REMOTE_CREDIT` before overwrite.
- Source-ring-credit exhaustion and remote-mailbox-credit exhaustion are counted
  and scheduled separately.
- Holding an active client lease cannot cause the sender to overwrite the leased
  slot.
- Remote c1/c4 correctness and performance runs show no steady-state remote
  credit stalls with the normal mailbox geometry, or the stalls are measurable
  and explainable.

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
5. The current accepted performance baseline is Slice 4c plus Slice 4d/4d-r:
   body WRITE plus WIMM ready-word self-publication, frontend peek/apply/ack,
   immutable generation-tagged tuple-result records, and the local-blackhole
   scheduler admission fix.

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
- Measure whether remaining c4 variance is mostly scheduler/source-retirement
  noise, send-CQ retirement interval, remote mailbox credit behavior, or
  foreground/backend CPU placement before broad scheduler-policy changes.
