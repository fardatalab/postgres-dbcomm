# Send-CQE coalescing — implementation plan

<!-- kb-summary: Ordered stage plan for re-arming send-CQE coalescing on the peer RDMA transport, with the 2026-07-15 site-enumeration corrections to the design. -->

**Status: IMPLEMENTATION IN PROGRESS (2026-07-16).** Stage 1 is landed and validated (§51 in the retirement
audit). **Stage 2a is LANDED + VALIDATED** as Citus `bdcb7eb70`: the gate and 4-role basebackup passed
on the corrected final binary, with no Stage-2a/global alarms and no directional gate regression (D-S2a
acceptance below; audit §52). A post-implementation review replaced unsafe orphan/drop recovery on structurally
impossible ownership-insertion failures with the scoped fail-stop policy in D-S2a item 14. **Stage 2b is
IMPLEMENTED + VALIDATED below (D-S2b, 2026-07-16). The first runtime acceptance built/deployed cleanly and the
debug gate completed 5/5, but FAILED on false owner-shard ALARMs because the universal CQE callback tried to resolve
service lanes on a non-owner payload QP. The traffic-class guard fixed that boundary; the complete re-validation
then passed the unchanged gate + 4-role basebackup acceptance with zero alarms. Signalling remains always-on.
Next: Stage 2c policy flip only.**
Graduated from the design
[`../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`](../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md)
(C1–C5). Prerequisite **§29(b) send-queue accounting is LANDED** (`postedSendWrs` / `retiredSendWrs` /
`signalledFrontierMark[]`, `remote_execution_peer_transport_rdma.c:~1043`).

**Owner decision (2026-07-15): implement the FULL coalescing before S6.** Rationale is *cleanliness +
generic efficiency*, not a measured bottleneck: a uniform coalesced send-CQ handling (one checkpoint CQE per
~SQ/2 posts, polled uniformly) is simpler than per-WR signalling + a scheduled drain, and fewer send CQEs is a
worthwhile hot-path saving that does not need a pre-measurement to justify. Instrumentation of the coalesced
polling cost is folded into Stage 3 (a before/after vs the old scheduled drain is nice-to-have, not required).

---

## ⚠ CORRECTIONS to the design (2026-07-15 site enumeration, all VERIFIED). Read before implementing.

The design (C1–C5) was written earlier and several assumptions went stale. `T` = `tuple_sink_service_process.c`,
`R` = `remote_execution_peer_transport_rdma.c`.

1. **⛔ C5 IS DEAD — pool-deepening is DROPPED.** The depth-1 "~450µs/command blocking fence" C5 called *"where
   the performance actually is"* is `TupleSinkServicePublishPeerCommandCompletion` (`T:20599`), whose own comment
   says *"dead at HEAD and pending S7 deletion; audit S33.7 records why optimizing its synchronous fence would be
   correct but worthless."* It is the b2b-COPY / service-to-service completion path (broken at HEAD). Every
   **live** pool is already deep + non-blocking: command 64/63 (`T:22637`), peer-client-completion 16 (`T:403`),
   control req/resp 64 (`remote_execution_peer_transport_rdma.h:55`), payload staging 512/1024 (`R:146`). **There
   is no live shallow-pool blocking wait to deepen.** ⇒ The measured **235.4µs** per-command control wait is
   **NOT** this fence; its cause is unattributed and is a SEPARATE understanding-item (measure at gate/S6), not
   part of this work.
2. **C4 close arm — fine, just misnamed.** The peer close-checkpoints are `CLIENT_SQL_SESSION_CLOSE` (command
   lane, signalled tail `T:22759`) and `CLOSE_SINK` (payload lane, signalled WIMM `R:8651`) — **NOT**
   `CLOSE_SESSION`, which is local/DPU control (`homer_control_abi.h:25`). Both already exist and signal; the
   close arm needs no new WR.
3. **C4 abort arm — scaffolding exists, nothing end-to-end.** `CITUS_REMOTE_BASEBACKUP_OBJECT_ERROR`
   (`homer_basebackup_abi.h:32`) and `HOMER_PAYLOAD_CLOSE_FLAG_ABORT_RESET`
   (`remote_execution_peer_control_protocol.h:41`) are defined but **no producer emits either**, the handler
   **rejects** `ABORT_RESET` (`T:29832`), and receivers treat **only** `OBJECT_END` as terminal (`T:32203`,
   `T:37242`, `T:34567`). Stage 1 must pick a mechanism, emit it signalled, and teach receivers.
4. **C2 four-class uniformity is FALSE.** Only **command** (`T:1623`/`T:1666`, FIFO + `NextClientSqlCommandWrite
   PostOrdinal`) and **peer-client-completion** (`T:1644`/`T:1260`, `qpPostOrdinal`) have FIFOs/heads/ordinals.
   **Control** has owners (`R:564`/`R:579`) but **no** FIFO/head/ordinal. **Peer-command-completion** has **no
   owner at all** (only a per-session depth-1 scratch, `T:1569`) — and is dead (see #1). ⇒ C2's stamping +
   head-index applies to command + peer-client-completion + control; peer-command-completion is out.
5. **Bootstrap biases send accounting by +1 forever.** Bootstrap accounts its signalled WR (`R:6073`) but its CQE
   is consumed by a special poll (`R:3525`) that never calls the retirement handler, so every QP carries `+1`
   posted-not-retired in the scalar accounting. The interval decision (`postedSendWrs - lastCheckpointFrontier`)
   must correct for or tolerate this.
6. **FB-2 de-alias mirrors `dpuDmaFeedbacks[]` (`T:4037`) but its map uses a RUNTIME bijection validator**
   `HomerServiceValidateDpuFeedbackSlotMap` (`T:3957`), **not** a `_Static_assert`. Use the same pattern.

---

## Stage 1 — terminal flush (close + abort). ALSO fixes the basebackup abort-hang.

**Goal:** guarantee every unit's last WR is signalled at close AND abort, so coalescing's unsignalled tail is
always eventually retired (the cold-path leak that got P0-i reverted). Independent of any interval — testable
now.

- **Close:** `CLIENT_SQL_SESSION_CLOSE` (`T:22759`) and `CLOSE_SINK` (`R:8651`) already signal; confirm each
  stamps the frontier via `TupleSinkServiceAccountPostedSendChainRdma` (`R:10925`/`:10928`). No new WR.
- **Abort — the real work + a DECISION:** wire the single-session-abort-on-a-surviving-connection notification.
  Candidate mechanisms (⚠ **open decision**): (a) `OBJECT_ERROR` (basebackup record-level), (b)
  `CLOSE_SINK | ABORT_RESET` (transport close-level), (c) a combination. Producer emits it **signalled**;
  receivers must treat it as terminal. Sites: producer `bbsink_homer_error` (`basebackup_homer.c:76`) + receiver
  `RunHomerReceiveConsume` (`pg_basebackup.c:2421`); the connection-surviving abandonment frontier is
  `TupleSinkServiceResetSession` (`T:24995`). Full-reset abort paths (`T:32802`/`:33464`/`:34283`/`:37223`/
  `:28983`) destroy the QP and need nothing (S1c).
- **Acceptance (independent):** a mid-transfer `pg_basebackup` abort stops hanging both ends. No coalescing
  interval need be enabled.

## Stage 2 — re-arm the interval (C1 decision + C2 stamping + C3 total-unit admission).

**Goal:** stop signalling every WR; signal on the 3-term rule; retire by QP-wide frontier.

- **C1 decision point** — replace the always-true `ShouldSignal` funnels (command `T:7115`, peer-client-completion
  `T:5960`) and the hardcoded control signal (`R:8651`/flag conversion `R:11067`) with:
  `signalled = (postedSendWrs - lastCheckpointFrontier >= SQ/2)  ||  (this substrate's pool free < watermark)  ||  (terminal WR)`.
  Transport owns the frontier; substrate exposes its pool free (command 63 via `acceptedEpoch-retiredEpoch`
  `T:22637`; peer-client-completion `16 - reservedCount` `T:1274`; control `outstandingControlOps` `R:916`).
- **C2 QP-wide frontier** — stamp `postedSendWrs` on every owner of command + peer-client-completion + control;
  add a per-class **head** so a checkpoint retires the covered prefix (control needs a head added — it has none
  today); **delete** the class-local ordinal machinery (`FindClientSqlCommandWriteLaneFifoOffset` `T:6537` +
  siblings). Retirement handlers: command `HomerServiceHandleCommandSendCqeFromDrain` (`T:15869`),
  peer-client-completion (`T:15893`), control `TupleSinkServiceRetireControlSendCompletion` (`R:4301`).
- **C3 unit admission** — perform one non-mutating, all-or-nothing SOFTWARE preflight for the unit's TOTAL WRs
  before reserving its CQ owner or issuing its first post. The two live multi-post units are command (whole-message
  1 WR or body + ready/WIMM 2 WRs) and peer-client-completion (completion WIMM 1 WR or descriptor + WIMM 2 WRs).
  The single-threaded service posts immediately after a successful preflight; no durable
  "reserved-but-not-posted" WQE state exists. Existing per-post admission remains a defensive backstop.
- **Bootstrap +1** (#5) — correct/tolerate in the frontier math.
- **Flip the P0-i tripwires** `*_SIGNAL_INTERVAL` (`T:423`, `T:442`) from `#error` to the new decision — but
  ONLY after Stage 1, or this re-ships P0-i's cold-path leak (the tripwire enforces that ordering at compile
  time).
- **Acceptance:** gate + **4-role basebackup (the wrap test, ~44k laps — the only workload that exercises the
  byte-ring wrap)** both pass; at a clean close `postedSendWrs == retiredSendWrs` (no leaked WR).

## Stage 3 — uniform coalesced send-CQ handling + FB-2 de-alias + measurement.

**Goal:** the "uniform coalesced CQ polling" — de-schedule the drain, resolve FB-2, measure the cost.

- **Drain de-schedule** — retire the scheduled `PEER_SEND_CQ` collector (`T:2747`, wiring `T:17143`, exec
  `T:49533`); the mandatory admission-forced drain (`R:10863` → reap → `retiredSendWrs` `R:10939`/`:4437`) plus a
  cheap **poll-one after each checkpoint post** replace it. (See the design's FB-2 analysis — the drain makes no
  scheduling decision under coalescing.)
- **FB-2 de-alias** — with the drain de-scheduled, the two posters `DPU_PEER_COMMAND_EGRESS` /
  `DPU_PEER_COMPLETION_EGRESS` (currently aliasing `peerSendCqFeedback` `T:4023`, map `T:11126`, readers
  `T:15025`/`T:43147`) get their own slots — a `peerSendCqFeedbacks[]` array mirroring `dpuDmaFeedbacks[]`
  (`T:4037`) with the runtime bijection validator (`T:3957`), NOT a `_Static_assert` (#6).
- **Measurement** (owner request) — instrument the coalesced CQ polling/handling cost (a stats macro, off by
  default). A before/after vs the old scheduled drain is nice-to-have, not required.
- **Acceptance:** gate + basebackup pass; FB-2 own-age ≥ `MAX_SKIP_GRANTS` is 0 after; coalesced-polling cost
  measured.

---

---

## CONCRETE DESIGN (2026-07-15) — SETTLED. Two adversarial refutation rounds + owner review.

Round 1 found 7 objections (all verified, all folded — the "(refutation round 1)" blocks); round 2 attacked
the corrections and found 4 more defects (the synthetic-close backstop died — see D-S2.4; the sweep
validation gained a cross-connection arm; FAILED needs a distinct result). The first owner review selected a
4096/1024 global size-out; the 2026-07-16 owner pivot first narrowed that to a scoped one-QP proof, then deliberately
generalized it without restoring the cross-product: D-S2.1 now allocates one fixed-depth owner shard per live
owner-bearing QP during cold connection establishment. The stage list above says *what*; this section says *how*,
with the mechanism nailed to code. `T` =
`tuple_sink_service_process.c`, `R` = `remote_execution_peer_transport_rdma.c`.

### D-S2.0 The retirement REDESIGN (why this is not "re-arm what exists")

The dormant per-lane machinery (`postOrdinal` + lane FIFO + retire-through-a-NAMED-index) implements
**class-local** retirement: a CQE retires only owners of its own class, located by searching the lane FIFO for
the WR-ID's `outstandingIndex` (`TupleSinkServiceFindClientSqlCommandWriteLaneFifoOffset` `T:6537`, the
shifting removal `T:6508`). Under coalescing a checkpoint must retire **across classes** on the same QP
(command + peer-client-completion + control multiplex `CRITICAL_CONTROL`), so the ordinal/named-index model is
**replaced**, not fed:

- **Stamp:** every owner record gains `uint64_t frontierMark`, set at post time to `postedSendWrs` *after*
  `TupleSinkServiceAccountPostedSendChainRdma` (`R:10925`). One QP-wide number; class-local ordinals deleted.
- **Sweep:** on every **successful** send CQE, after the scalar retire (`TupleSinkServiceRetireSendChainRdma`
  `R:10939`, called at `R:4437`) yields the new frontier `F = retiredSendWrs`, walk each sweep-class FIFO for
  that connection **from the head, popping while `frontierMark <= F`**, invoking that class's existing
  per-entry release logic. O(retired), no search.
- **Sweep classes: command, peer-client-completion, control.** Payload and receiver-head-ack lanes keep their
  own machinery and their current signalling (payload already amortizes in-chain — one signalled tail per
  1–33-WR chain; head-acks have granularity 16) — but their CQEs still advance `F` through the common funnel,
  so a payload checkpoint retires control/command owners on the same QP *for free* (relevant on payload
  connections, which also carry CLOSE_SINK control traffic — the close handler REQUIRES the stream-bound
  connection, `T:29847`). ⚠ Standing constraint (round 2): **if payload-tail signalling is ever coalesced
  later, payload owners must gain frontier marks and JOIN the sweep** — the abort convergence proof in D-S1
  and this scope split both lean on payload tails staying signalled.
- **In-order safety:** CQEs on one CQ arrive in QP order, so a sweep at `F` can never run ahead of an
  undrained earlier signalled CQE; a signalled owner is popped by its own CQE (it is at the head with
  `mark <= F`) or by any later one — the mark comparison makes the sweep idempotent. Untagged CQEs (blocking
  waiters) also advance `F` and also sweep.
- **WR-ID becomes validation-only:** after the sweep for a typed CQE, the CQE's own named owner MUST be
  inactive; if still live → ALARM (mis-ordering/corruption). The WR-ID keeps its class+index encoding.
- **The post-ORDER survives; the FIFO ARRAYS do not.** The per-connection per-class ordering the sweep pops
  is an **intrusive linked list through the owner entries** (`nextIndex` per entry + `{head, tail}` per
  connection per class — see the sizing note in D-S2.1 for why the array-of-arrays representation dies).
  **DELETE**: `postOrdinal`/`NextClientSqlCommandWritePostOrdinal` (`T:1674`),
  `FindClientSqlCommandWriteLaneFifoOffset` (`T:6537`), the array lane-FIFOs and their O(n) shifting
  mid-queue removal (`T:6508-6531`), and the peer-client-completion siblings. Scenario-E cancellation
  becomes clear-in-place (`active=false`); the sweep lazily skips inactive entries at pop time.
- **Control gains a head:** a per-connection post-order FIFO of `{isResponsePublish, slotIndex, generation}`
  (depth `CITUS_REMOTE_EXEC_PEER_CONTROL_OP_SLOTS + ..._RESPONSE_PUBLISH_SLOTS`), swept like the other two;
  entries release via the existing `TupleSinkServiceRetireControlSendCompletion` logic (`R:4301`) refactored to
  take the FIFO entry instead of a decoded WR-ID.
- **(refutation round 1) The sweep rides the PERSISTENT callback table, and EVERY CQ consumer must carry it.**
  The lane FIFOs are service-file globals unreachable from the transport (`T:1635`, handler signature
  `R:4413`), so the sweep is a new entry on `TupleSinkServicePeerSendCompletionCallbacks` — and the refutation
  found the premise "every consumer can dispatch every CQE" is FALSE today: (a)
  `TupleSinkServiceDrainTaggedSendCompletions` (`R:~4925`) passes **NULL callbacks** and is called from the
  control-response-slot exhaustion path (`R:9040`); on `CRITICAL_CONTROL` that can dequeue a typed
  command/completion CQE and destroy its only retirement event — a LATENT hazard at HEAD (rare only because
  every WR signals and the scheduled drain keeps the CQ short), aggravated by coalescing; (b) the peer-pump
  SEND_CQ phase (`R:13118`) also polls with NULL callbacks; (c) the blocking waiter's precondition **exempts
  `CRITICAL_CONTROL`** from requiring callbacks (`R:4652`) — exactly the QP that carries typed CQEs. Stage 2a
  fix: all three route through the persistent `transportState->sendCompletionCallbacks`, and the exemption is
  removed (inverted: the shared QP is the one that must NEVER be polled without dispatch).
- **Sweep validation must tolerate index reuse — ACROSS CONNECTIONS (round 2 correction):**
  command/completion WR-IDs carry a 16-bit owner index and **no generation** (`R:4010`/`R:4044`), and the
  owner tables are GLOBAL while frontier coordinates are PER-CONNECTION — so a cancelled index can be reused
  by a *different* connection whose numerically-smaller mark is incomparable with this CQE's `F`. The
  validation is therefore three-arm: named owner **inactive**, OR `owner.connectionHandle != CQE's
  connection`, OR (same connection) `frontierMark > F`. Same-connection monotonicity is sound
  (`R:10925` advances only on successful posts; reset destroys the QP/CQs before zeroing, `R:6568`).

### D-S2.1 The decision function (C1), concretely

- New per-connection scalar `lastSignalledPostFrontier`, updated inside `AccountPostedSendChainRdma` when
  `containsSignalledWr` (it equals the mark just stamped).
- Transport term (one helper): `postedSendWrs + chainWrs - lastSignalledPostFrontier >= grantedSendWr/2`.
- Lane funnels become real: command `ShouldSignal` (`T:7115`): transport-term ‖ pool-term
  (`acceptedEpoch - retiredEpoch >= (CITUS_REMOTE_EXEC_LOCAL_COMMAND_MAILBOX_SLOTS-1)/2`, gate at `T:22637`) ‖
  terminal (`SESSION_CLOSE` commandKind). Peer-client-completion (`T:5960`): transport-term ‖ ring outstanding
  `>= 16/2` ‖ terminal (completion of a `SESSION_CLOSE`). Control (`R:8651`, currently hardcoded `true`):
  transport-term ‖ `outstandingControlOps >= OP_SLOTS/2` (resp. response slots) ‖ terminal (CLOSE/ABORT-class
  ops always signal).
- Signalling is a **local-CQE-only** change: WIMM receiver-side doorbells are unaffected, so nothing about
  receiver notification, RQ credits, or client-visible latency changes. What grows is *retention* of sender
  source slots, bounded by the pool terms.
- **Owner-table capacity — FINAL OWNER PIVOT (2026-07-16): DYNAMIC PER-QP SHARDS OF 256/256; the earlier
  4096/1024 global size-out and the temporary single-QP fail-stop are SUPERSEDED.** Replace the service-global
  `ClientSqlCommandWriteCompletions[]`, `PeerClientCompletionPublishCompletions[]`, and their arrays of sweep lanes
  (`T:1707-1724`) with one service-owned `HomerServiceSendOwnerShard` allocated for each local, ready
  `CRITICAL_CONTROL` QP generation. A shard contains 256 command owners, 256 peer-client-completion owners, one
  intrusive `{head,tail}` lane and next-reservation cursor per class, the class counters/peaks, and its exact
  `HomerPeerConnectionIdentity`. Multiple sessions sharing an endpoint/class-matched QP share its shard
  (`TupleSinkServiceFindOutgoingPeerConnection`, `R:7934-7998`); a new peer/QP gets another shard. Payload QPs do
  not allocate one because payload ownership remains outside this command/completion shard. Control owners remain
  transport-owned in each `TupleSinkServicePeerConnectionState` and continue to sweep before the service callback
  (`TupleSinkServiceHandleTaggedSendCompletion`, `R:4628-4651`); they are not duplicated into the service shard.
  On each shard's QP:
  - every **LINKED** command/completion owner covers at least one software-unretired WR (active or Scenario-E
    inactive does not matter; `linked` and retained CQ demand survive until sweep/purge);
  - command + completion linked owners therefore cannot exceed
    `postedSendWrs - retiredSendWrs`, and admission clamps that quantity to 256 (`admittedSendWr`);
  - Stage 2b's total-unit preflight runs **BEFORE owner-table reservation**. If at least one WR is admissible,
    software outstanding is at most 255, so the one transient `active && !linked` owner also has a free slot.
    There is no cross-product by session count or peer count and no global table-pressure signalling term.

  **Cold allocation and exact lifetime:** extend `HomerPeerConnectionLifecycleObserver` (`R.h:671`) with a
  fallible ready callback. `TupleSinkServiceFinishPeerConnectionSetup` (`R:7215`) invokes it after bootstrap
  completion and notification-RECV posting, but **before** publishing `setupPhase=READY`, canonical recv-CQ
  ownership, or `bootstrapComplete` (`R:7265-7270`). For `CRITICAL_CONTROL`, the service `calloc`s and initializes
  the shard and returns it as a generic opaque per-connection lifecycle context; for other traffic classes it
  returns success with NULL. Allocation/initialization failure leaves the connection non-ready and enters the
  existing setup-failure reset path, so no owner-bearing post can observe a ready QP without a shard. The transport
  stores only the opaque pointer and exposes a generation-checked lookup; service owner reserve/link/sweep resolves
  the exact shard from the CQ/post callback's connection handle before interpreting the shard-local WR-ID index.
  This keeps the 16-bit WR-ID owner index unchanged at `0..255`; connection identity already arrives out-of-band
  on every typed send-CQE callback (`TupleSinkServicePeerSendCheckpointSweepCallback`, `R.h:427-430`).

  The opaque pointer is the O(1) post/CQE lookup, but it does **not** replace scheduler enumeration. Stage 2a's
  `HomerServiceExecuteCqDrainProgressPlan` currently discovers command/completion send CQs by scanning the two
  global sweep-lane arrays (`T:16942-17003`), and the generic transport pump does not otherwise guarantee incoming
  send-CQ drain. Maintain a service-owned intrusive active-shard list, appended only after ready allocation succeeds
  and unlinked during reset-complete. The CQ-drain executor walks it from a round-robin cursor, bounded by the
  existing grant/seen-connection limit, and drains shards with signalled demand; this prevents a stable list head
  from starving shard 65+ if the existing 64-grant cap is reached. Retain the existing global active/signalled owner
  counters as aggregate scheduler-readiness facts (`T:7605-7659`, `T:16241-16329`), while each shard carries the
  class-local counters used for exact enumeration and capacity diagnostics. Neither list nor aggregate is a quota.

  **Ready-callback ownership contract:** transport initializes a local `connectionContext = NULL`, rejects a
  duplicate ready callback or a pre-existing stored context, and invokes the observer exactly once. The callback
  returns either (a) `false` with the out-context still NULL, having freed any partial shard and undone any list
  insertion itself, or (b) `true` with a fully initialized, already-listed shard for `CRITICAL_CONTROL` (NULL is an
  invariant failure) and NULL for non-owner-capable traffic classes. Transport stores the local pointer only after
  successful return. Thus reset of a pre-ready/failed connection sees no half-owned shard. Every ready
  `CRITICAL_CONTROL` QP is treated as **owner-capable**, even if it happens to carry generic control only; that may
  cold-overallocate one unused shard, but it cannot miss a current command/completion owner path.

  Reset-begin may snapshot the context but never frees it. Reset-complete runs after QP/CQ destruction
  (`TupleSinkServiceResetPeerConnectionInternal`, `R:6911-6918`). Transport first **detaches** the opaque pointer
  from `connectionState` into a local, then passes that local to the observer, so no accessor can return a pointer
  that the callback may free. The service exact-purges all `active || linked` entries, rearms reset continuations,
  unlinks the shard, asserts it is empty, and frees it. This purge/rearm/free sequence must precede
  `HomerServiceMarkClientSqlPeerSessionsResetComplete`, because that helper may immediately call
  `TupleSinkServiceResetSession` (`T:32180-32198`) and must observe QP-death owner proof, not the old shard. The fixed
  connection slot is memset/reused only after the observer returns (`R:6919`). A connection that fails
  before the ready callback has NULL context and needs no shard cleanup. Service shutdown must drive the same reset
  lifecycle for every active connection and assert zero live shards. The existing
  `TupleSinkServiceDestroyPeerTransportState` loop already resets every active outgoing and incoming connection
  (`R:10529-10549`); no parallel service-side free loop is needed.

  This is the **multi-peer theorem**: every local owner-bearing QP has a disjoint capacity domain whose owner slots
  are bounded by that same QP's admitted WQEs. It naturally covers multiple nodes and simultaneous incoming/outgoing
  owner-bearing QPs up to the transport's existing 64+64 connection-slot limits (`R:70-71`), while allocating heavy
  tables only for active `CRITICAL_CONTROL` QPs. It deliberately adds no separate peer-count cap or startup
  preallocation. Memory-allocation failure is an exceptional cold-path setup failure, consistent with the research
  prototype's fail-loud policy.

  **Rejected designs retained for history:** a global watermark cannot free a quiet different QP; a single global
  256/256 table needs quotas or a second-QP rejection; preallocating 128 heavy shards wastes memory for inactive
  slots; and the 4096/1024 global size-out still aggregates incomparable per-QP frontiers and merely postpones
  exhaustion. Per-QP dynamic shards preserve linear memory in active QPs and O(retired) sweeping: one `nextIndex`
  per owner and one `{head,tail}` lane per class per shard.

  **On "why would a CQE ever sit undrained?"** — under the de-scheduled design a parked CQE on a quiet
  connection is possible by design. The half-SQ term bounds continuing traffic; it does NOT guarantee a later
  post if traffic stops below the interval. Normal close/abort has the Stage-1 signalled terminal flush. Abnormal
  abandonment without a terminal may retain a bounded quiet tail; D-S2.4 keeps owners/MRs and defers reset until
  another checkpoint or QP destruction supplies proof.
- Control op vs response-source slots retire differently (request CQE only sets `sendCompletionRetired`
  while the op stays `WAIT_RESPONSE`, `R:4383`; response slots free directly on CQE, `R:4357`) — the control
  pressure term counts them separately.

### D-S2.2 Unit reservation (C3)

**Final Stage-2b API contract (owner clarification, 2026-07-16): reservation is an immediate SOFTWARE verdict,
not durable credit state.** Add a transport preflight such as
`TupleSinkServicePreflightSendUnitRdma(connectionHandle, unitWrCount, unitContainsSignalledWr, postResult)` which
performs the existing cumulative admission arithmetic for the unit's exact TOTAL WR count and mutates nothing:

`outstanding + unitWrCount <= admittedSendWr - (unitContainsSignalledWr ? 0 : CHECKPOINT_RESERVE_WRS)`.

The service is single-threaded and the unit posts immediately after a successful preflight. No CQ poll, callback,
alternate send, or scheduler yield may occur between preflight and the unit's final post. There is therefore no
`reserved-but-not-posted` counter/token and nothing to roll back if execution stops before the first post. Existing
per-post admission remains as a defensive assertion/backstop; the total preflight proves each prefix passes:
for a signalled N-WR unit, every earlier unsignalled prefix is at most `admittedSendWr - 1`, and the final
signalled post reaches at most `admittedSendWr`; a wholly unsignalled unit leaves the checkpoint reserve intact.

The preflight runs **before CQ-owner reservation**, which is load-bearing for the 256-entry proof in D-S2.1.
Compute `unitWrCount` once and reuse the same branch decision for posting:

- command (`TupleSinkServicePostRemoteClientSqlCommandRecord`, current `T:23255-23502`): 1 WR for the
  whole-message branch, otherwise body + ready/WIMM = 2;
- peer-client-completion (`TupleSinkServicePublishPeerClientCommandCompletion`, current `T:20670-21138`):
  completion WIMM alone = 1, descriptor body + completion WIMM = 2.

Preflight WOULD_BLOCK is normal retryable transport pressure: the command path returns its existing BLOCKED/local
resource result; completion publication gains a distinct `...BLOCKED_TRANSPORT_CREDIT` result carrying
`HOMER_PROGRESS_REASON_LOCAL_SEND_RESOURCE_PRESSURE` (do not mislabel it source-credit exhaustion). Zero-WR verbs
failure after preflight changes no reservation state and follows the existing retry/reset classification. A body
accepted followed by tail post failure is NOT rollbackable; software admission can no longer cause it, so the
research-prototype policy is a loud ALARM + fail-stop/connection teardown rather than partial-unit recovery. Apply
that policy consistently to command and completion.

The existing one-WQE checkpoint reserve remains a per-post defensive backstop and headroom for a standalone
one-WR checkpoint-bearing post. It does **not** claim that an arbitrary multi-WR terminal unit fits in the last WQE;
the total-unit preflight is the no-torn-unit proof.

(refutation round 1) One more 2-post unit exists — the DEAD `TupleSinkServicePublishPeerCommandCompletion`
(unsignalled record `T:20667` + signalled epoch `T:20678`, epoch helper blocks synchronously `R:11737`).
**Exempt it explicitly** (it keeps relying on the 1-WQE reserve) rather than building reservation for a path
pending S7 deletion. Head-acks (ordinary `T:34943`, final `T:37569`) are single signalled WIMMs and stay
always-signalled — no reservation needed.

### D-S2.3 The raw-CQE-consumer skew — WORSE than the recorded "+1", and load-bearing under coalescing

Correction #5 understated the defect. The bootstrap send is accounted signalled (`R:6073`) but its CQE is
consumed by a raw poll (`R:3525`) that never calls `RetireSendChainRdma` — so the ordinal pairing
(`signalledRetiredOrdinal` ↔ `signalledPostOrdinal`) is **permanently shifted**: every subsequent CQE retires
the PREVIOUS checkpoint's mark. At HEAD (every WR signalled) the lag is ~1 WR — invisible. Under coalescing
the lag is **one full interval (~SQ/2)**: admission sees up to `SQ/2` phantom outstanding, halving usable SQ
and spuriously WOULD_BLOCKing. Fix: EVERY consumer of a successful send CQE on an accounted QP must retire
(bootstrap poll gets `connectionState`; inventory of other raw consumers pending the trace). The bootstrap
comment `R:6064-6072` currently CLAIMS the common funnel retires it — a lying comment; fix it with the code.
Acceptance invariant: at clean close/reset, `postedSendWrs == retiredSendWrs`, else ALARM.

- ⚠ **VERIFIED gap (latent at HEAD, not coalescing-specific):** `grantedSendWr` stores the provider's
  written-back grant (`R:5531-5534`) and the setup check (`R:5536-5539`) guards it against the payload
  STAGING POOLS only — **not** against the 256-entry `signalledFrontierMark` table (`R:1045`, sized
  `CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH`) nor the send CQ (created with 256 entries, `R:5437`). Admission's
  ceiling is the raw grant (`R:10867`), so a provider granting >256 plus >256 outstanding signalled posts
  overwrites unconsumed marks — PROVEN; a send-CQ overrun is *plausible but unproven* (the provider-granted
  CQ size is never inspected either). **FIXED EARLY in D-S0** (`f45556838`): the stored admission ceiling is
  clamped to `min(granted, CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH)`; Stage 2b consumes `admittedSendWr` and has
  no remaining clamp work.
- Bootstrap fix details (refutation round 1): both special-poll call sites have `connectionState` in scope
  (`R:7140`/`R:7336` currently pass only the CQ); there is exactly **one accounted-signalled bootstrap send
  per local QP** (outgoing `R:7120`, incoming `R:7312`, both account via `R:6055-6073`); the special poll must
  also validate the CQE's WR-ID is the bootstrap buffer's before retiring (`R:3525` checks only
  status+opcode today); lane traffic before `bootstrapComplete` is structurally impossible (`R:8591` rejects),
  so a non-bootstrap CQE there is an invariant violation → ALARM, not accounting.

### D-S2.4 ⚠ Surviving-QP session reset (refutation round 1 — the biggest hole the review found)

`TupleSinkServiceRetireSessionSendCompletions` drains for at most ~2 s (`T:6954`/`T:7032`), then Scenario-E
**cancels** the session's owners (`T:7080-7108`) — and the caller then **deregisters the session's source
MRs** (`T:25092`/`T:25103`). Today that path is a rare ALARM because every WR signals and CQEs converge fast.
**Normal coalesced close/abort does NOT make this expected:** Stage 1 force-signals the terminal unit. But an
abnormal abandonment can stop below both the half-SQ and source-pressure thresholds with no terminal successor.
On that scoped path, cancelling owners + deregistering MRs without CQE proof releases memory the NIC may still be
reading. The fix is structural, not a longer timeout:

**(REWRITTEN after refutation round 2 — the synthetic-close backstop is OUT of Stage 2.)** Round 2 killed it
three ways, all VERIFIED: (a) there is **no reset-context producer** — the command funnel posts FROM the
frontend-built mailbox MR (`T:22927-22967`, MR registered at `T:39833`), so a service-fabricated close needs
its own registered source that does not exist; (b) a synthetic `acceptedEpoch+1` can collide with a pending
frontend-written slot (raw-readiness is a separate predicate, `T:23628`) and with the terminal ticket
double-mark (`T:19276` vs `T:22807`); (c) node B's fenced landing consumes-and-REJECTS even `SESSION_CLOSE`
after `dpuBackendTeardownStarted` (`T:44726`, its own comment assigning reclamation to "the existing
peer-close/lifetime machinery") — so the message is not a reliable teardown signal in exactly the
abandonment states that need it. The Stage-2 rule is therefore **DEFER-only, split by role**:

1. **Node A (command-lane owners):** at session-reset entry on a surviving connection with unretired session
   WRs, set per-connection `forceNextSignalCheckpoint` (the next post from ANY session signals — QP-wide,
   cheap) and **DEFER the reset** (the §48a outcome pattern, instrument line included) until a checkpoint's
   sweep retires the session's owners or the connection resets. Scenario-E cancellation + MR deregistration
   run ONLY when the QP is dying (reset flushes everything).
2. **Node B (completion-lane owners):** its terminal is the already-posted signalled close-completion WIMM
   (`T:20394`) — the reset drain converges on that CQE; if it has not been drained yet, drain, else defer the
   same way. Node B never needs to manufacture a WR.
3. **The synthetic signalled `CLIENT_SQL_SESSION_CLOSE` moves wholly into §37/P2-L** (abandonment reclaim),
   now carrying a concrete spec from this round: a dedicated service-owned REGISTERED close-record source, the
   raw-mailbox collision predicate, terminal-ticket handling, and a decision on node-B fenced-landing
   semantics. Until P2-L lands, an abandoned session on a quiet live QP stays deferred — the same bounded
   retention the abandonment leak already has today, now with proof instead of a use-after-free.

**Stage-2b continuation mechanics (settled 2026-07-16):**

- Preserve the existing `TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister` guard as the first
  post-NULL operation in `TupleSinkServiceResetSession`; its refusal remains a PURE DEFER with no mutation.
  Immediately below it, before clearing scheduler state or deregistering any MR, run the send-owner proof.
- Change `TupleSinkServiceRetireSessionSendCompletions` from "drain then abandon" to an outcome-producing proof.
  If no owners remain, reset proceeds. If the exact QP/CQs were destroyed, purge the exact
  `{connectionHandle,generation}` lanes (retiring active bookkeeping) and proceed. If the same QP survives with
  owners, do **not** Scenario-E-abandon and do **not** deregister: latch the session reset as pending and return
  `TUPLE_SINK_SESSION_RESET_DEFERRED`.
- Node A command owners set per-connection `forceNextSignalCheckpoint`; the next successful signalled post on
  that QP clears it in `TupleSinkServiceAccountPostedSendChainRdma`. Stage 2b still signals every unit, so this
  is plumbing; Stage 2c adds the flag as the first term in command/completion/control signalling decisions.
  A fully quiet QP may retain the session until connection reset — bounded retention, not false completion.
- Node B may rely on the existing close-completion WIMM only when `peerRemoteWriteTerminalPublished` proves that
  final WIMM was successfully posted (`T:21075-21081`). Descriptor-body success followed by WIMM failure uses
  the D-S2.2 fail-stop policy; it is not a terminal proof.
- Add `resetOwnedSessions` / `resetRunnableSessions` bitmaps to `HomerServiceSessionPendingState` plus a
  per-session pending-reset latch/identity. `ResetSession` arms owned on live-QP deferral. Owner sweep marks runnable
  only after that pending session's command/completion/partial-post owner count reaches ZERO; one retired owner is
  not enough. The connection reset-complete path must purge exact lanes **after QP/CQ
  destruction** and rearm both Node-A and Node-B pending resets (the current reset-complete session scan covers
  only `clientSqlPeerReceiver`). A new top-level `RETRY_SESSION_RESET` action consumes runnable bits and calls
  `TupleSinkServiceResetSession`; send-CQ callbacks only arm the bit and remain RETIRE-ONLY. Successful reset clears
  both bitmaps/latch through `HomerServiceClearSessionPendingState`. Duplicate arms are idempotent and bounded.
- **Bind generation, not merely a recyclable handle.** Incoming client-SQL receiver setup already captures
  `clientSqlPeerConnectionGeneration` beside the handle (`T:43738-43740`), but outgoing command-session bind stores
  only `clientSqlPeerConnectionHandle` (`T:41068-41070`). Stage 2b must snapshot the outgoing generation at that
  successful bind too. Owner-shard lookup at reserve/post/CQE uses the stored expected generation; it must not read
  "the current generation" from a possibly reused stale handle and thereby validate the wrong incarnation.
- **Shutdown must consume the QP-death continuation.** Current shutdown resets sessions once, unmaps control, then
  destroys the peer transport (`T:51824-51835`). A live-QP owner can make that first reset DEFER; transport destroy
  then supplies QP-death proof and rearms the reset, but there is no scheduler pass afterward. Reorder the bounded
  shutdown sequence to: initial session-reset sweep -> `TupleSinkServiceDestroyPeerTransportState` (callbacks
  detach/purge/rearm/free shards) -> consume runnable pending resets or run one dedicated post-destroy reset sweep ->
  assert zero active sessions/pending reset bits/live shards -> unmap control. Transport reset already invalidates
  and NULLs each MR binding before the later session deregistration, so the final reset frees the handle wrapper
  without re-deregistering the provider MR (`R:5343-5356`, deregistration `R:10689-10721`).

This continuation is deliberately indexed/O(1) in steady state; do not add a broad per-pass session scan. QP reset
is cold and may perform the existing bounded lane/session reconciliation. Generation mismatch alone is not the
reset-complete proof until the connection state is memset; the explicit "QP/CQs destroyed" point owns the purge.

### D-S0 Standalone hardening commit — ✅ IMPLEMENTED + VALIDATED (2026-07-15; audit §50). The clamp was
### LOAD-BEARING: the provider grants 409/511 against the 256 request (see §50.3).

The two latent HEAD bugs the refutation surfaced are P0-doctrine violations on their own and ship as a
separate commit ahead of everything else, so the Stage-2a premise ("every CQ consumer can dispatch/sweep
every CQE") is already true before the refactor starts:

1. **NULL-callback CQ consumers** — `TupleSinkServiceDrainTaggedSendCompletions` (`R:4925`, sole caller the
   control-response exhaustion drain `R:9040`) and the peer-pump SEND_CQ phase (`R:~13132`) fetch the
   persistent `transportState->sendCompletionCallbacks/-Context` (the blocking waiter's source,
   `R:4695-4699`); the stale `CRITICAL_CONTROL` exemption (`R:4652`) is DELETED — it predates the
   command-plane migration that put typed traffic on that very QP. Implementation check: the `R:9040` drain
   runs mid-control-publish, so callbacks must stay retire-only there (F7 design; `HomerServiceSendCqDrainDepth`
   guard backstops).
2. **`grantedSendWr` clamp** — store `min(granted, CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH)` (`R:5531`), and
   log the raw granted caps at setup (today the grant is only visible in error messages).

KB in the same commit: audit entries (retire-before-release items) + CONTRACTS.md (the "every consumer
dispatches" rule keyed on the drain symbols; the clamp rule on `grantedSendWr`). Validation: gate + 4-role
basebackup unchanged (behavior-neutral except where today's behavior is the RETIREMENT-EVENT-LOST alarm).

### D-S2.5 Ordering inside Stage 2 (each sub-step commit+validate)

(a) stamping + sweep with signalling still always-on — a pure refactor whose sweep degenerates to per-CQE
pops (its "every consumer can sweep" prerequisite is already true after D-S0); gate + 4-role basebackup must
be unchanged. **⚠ AMENDED at 2a implementation (2026-07-16): the D-S2.3 bootstrap-retire fix MOVES FROM (b)
INTO (a).** It is not an optimization there — it is a *correctness precondition* for the sweep itself: the
bootstrap skew makes every subsequent scalar retire yield the PREVIOUS checkpoint's mark, so under
sweep-only release the LAST owner of every burst has `mark > F` and is never popped. The last command of a
session is `SESSION_CLOSE` itself ⇒ its owner survives ⇒ `TupleSinkServiceRetireSessionSendCompletions`
polls an already-empty CQ for 2 s, then Scenario-E ALARMs — **on every session close**. "(a) is
behavior-identical" is therefore only satisfiable with the bootstrap retire in the same commit (full
argument + defense-in-depth in the D-S2a map below). (b) non-mutating all-or-nothing unit preflight + the
D-S2.4 surviving-QP reset continuation (bootstrap fix moved out, see above); each dynamically allocated QP shard
keeps 256/256 owner slots under its local admission proof. (c) flip the decision function to
`forceNextSignalCheckpoint || per-QP-half-SQ || source-pool-pressure || terminal` (tripwires `T:423`/`T:442`
replaced in the same commit; Stage 1 must already be landed). There is no global owner-table pressure term:
preflight-before-owner-reserve plus each shard's QP-local admission ceiling makes normal exhaustion impossible;
additional owner-bearing QPs allocate independent shards during cold connection establishment.

### D-S2a IMPLEMENTATION MAP (2026-07-16, anchors re-derived at HEAD = citus 4a5d18930; T shifted ~+600 vs the pre-Stage-1 anchors above)

Re-derived anchors: ordinal/FIFO structs+globals `T:1623-1685`; completion-lane FIFO machinery
`T:6011-6420`; command-lane `T:6462-6975` (reserve `T:6635`, per-entry retire `T:6719`, range-retire
`T:6834`, clear `T:6927`); Scenario-E cancel inside `TupleSinkServiceRetireSessionSendCompletions`
`T:7100-7153`; drain callbacks `T:15922`/`T:15946`; persistent callback table `T:16255`; demand scan
`T:16373-16410`; command post site `T:22772-22864`; completion post site `T:20406-20497`; dispatch funnel
`R:4424` (scalar retire `R:4448-4451`, class arms `R:4534`/`R:4570`/`R:4596`); control retire `R:4311`;
drain loop `R:4808+` (errored CQEs rejected BEFORE dispatch, `R:4925`); accounting `R:11017-11052`;
control post funnel: encode sites `R:9263`/`R:9337` (response, generation ALWAYS 0)/`R:12330` (request) →
`TupleSinkServicePublishControlMessage` `R:8657` → sole post `R:11122` (Account at `R:11181`); connection
reset = full memset `R:6675`, generation reassigned fresh at (re)establish `R:6881`/`R:7565`; accessor
`TupleSinkServicePeerConnectionGenerationRdma` already exported (`rdma.h:920`).

Micro-decisions SETTLED at implementation (each is a deviation-from-letter or a gap the plan text did not
cover; attack these first in review):

1. **The bootstrap-retire fix ships IN 2a** (see the D-S2.5 amendment above for the forcing trace). Per
   D-S2.3's own spec: both special-poll sites get `connectionState`, the poll validates the CQE's WR-ID is
   the bootstrap buffer's before retiring, a non-bootstrap CQE there is ALARM. The lying comment
   `R:6064-6072` is rewritten with the code.
2. **Defense-in-depth for any REMAINING raw consumer (the D-S2.3 "inventory pending" risk): the named-CQE
   validator self-heals.** A typed CQE whose own owner survives the sweep with `mark > F` on the SAME
   connection means the mark pairing has skewed (impossible after fix 1 — RC ordering pairs the k-th
   signalled CQE with the k-th mark, so for the CQE's own entry `mark == F`). The validator then ALARMs
   **and force-sweeps through the named entry's own mark** (its WR completed, so its mark IS a proven
   frontier) instead of leaking the owner. Turns an unknown raw consumer into a diagnostic instead of a
   slow leak. Same rule transport-side for a CONTROL CQE still in the control FIFO after its sweep.
3. **List append moves to POST-SUCCESS time (today: lane-FIFO enqueue at RESERVE time, `T:6695`/`T:6237`).**
   The mark is only known after the post; appending after the unit's final post keeps the per-connection
   list mark-sorted for free (single-threaded service; no CQ drain between reserve and post on this path).
   Post-failure paths (`T:22784`/`T:22805`/`T:22819`, `T:20421`/`T:20439`/`T:20471`) then clear an entry
   that was NEVER linked — plain memset, and the O(n) mid-queue lane-FIFO removal dies entirely (nothing
   unposted is ever in a list; nothing posted is ever removed except by sweep pop or purge).
4. **Owner entries gain `linked` + `connectionGeneration`; reserve skips `active || linked`.** A
   Scenario-E-cancelled entry stays LINKED (inactive) until swept — its table slot must not be re-reserved
   or the intrusive list corrupts.
5. **Generation-checked lane PURGE — a lifecycle hole the plan's lazy-skip did not cover.** At HEAD there
   is NO connection-level owner cleanup: Scenario-E *physically removes* entries from the lane FIFO, so a
   dead connection's owners vanish with their sessions. Lazy clear-in-place breaks that: entries on a QP
   that died get NO CQEs, are never swept, and their slots leak. Fix: each lane records
   `{connectionHandle, connectionGeneration}`; any lookup (reserve-append, sweep, Scenario-E) that finds
   the handle's CURRENT generation ≠ the lane's stored one purges the whole list before rebinding. Sound
   because reset destroys QP+CQs before the memset (`R:6675`) and a reused slot gets a FRESH generation
   (`R:6881`/`R:7565`) — once the generation differs, no CQE from the old QP can ever arrive.
   **AMENDED (codex round 1, D1): the purge RETIRES active entries** (per-entry retire, session
   credits/epochs/outstanding counts advanced) **instead of blind-memsetting them.** A blind memset
   stranded the OWNING SESSION's bookkeeping when a sender session outlives the shared CRITICAL_CONTROL
   connection (control connections are ownership-exempt and shared; reset-complete marks only
   `clientSqlPeerReceiver` sessions) — the credit gate could then block that session permanently. QP
   destruction is the transport proof that retiring is P0-clean, and the OLD array-FIFO code produced the
   same effect by accident: the reused lane's first new CQE range-retired the stale prefix. Abandoned
   entries just free (their accounting was released at abandon).
6. **Validator verdicts follow the `R:4359-4366` precedent: ALARM-and-keep-draining**, not
   dispatch-failure (which would reset the connection and turn an accounting diagnostic into a run-killer).
7. **Sweep callback shape:** ONE new member `sendCheckpointSweep(context, connectionHandle, retiredFrontier,
   err, errBytes)` on `TupleSinkServicePeerSendCompletionCallbacks` (`rdma.h:395`), invoked in
   `HandleTaggedSendCompletion` inside the SUCCESS block right after the scalar retire — i.e. for EVERY
   successful CQE of EVERY class including UNTAGGED (the arm at `R:4467` returns before dispatch but after
   retire+sweep) — after the transport-internal control-FIFO sweep. The service callback must no-op for
   non-`CRITICAL_CONTROL` QPs before resolving lifecycle context: callback universality spans traffic-class QPs,
   whereas command/completion shards intentionally do not. The existing `commandCompletion` /
   `peerClientCompletionPublishCompletion` callbacks change signature to `(context, connectionHandle,
   retiredFrontier, outstandingIndex, ...)` and become the 3-arm VALIDATORS. Payload callbacks unchanged.
   RETIRE-ONLY contract (`T:16244`) holds: the sweep releases owners and merges deltas, never posts/polls.
8. **Control FIFO is transport-internal**, embedded in `connectionState` (zeroed by the reset memset for
   free): entries `{responsePublish, slotIndex, slotGeneration, frontierMark}`, ring of depth
   `OP_SLOTS + RESPONSE_PUBLISH_SLOTS` = 128, appended inside `TupleSinkServicePublishControlMessage` after
   its post succeeds (it decodes its own WR-ID — the transport owns the layout; all three encode sites
   funnel there). FIFO overflow or failure to decode that just-encoded CONTROL ID is structurally impossible
   and **fail-stops** after the ALARM; continuing would leave a posted source untracked. Process exit destroys
   the QP and is the deliberately simple research-prototype failure fence. `TupleSinkServiceRetireControlSendCompletion`
   (`R:4311`) splits: decode-free core
   `...ReleaseControlSendOwner(connectionState, responsePublish, slotIndex, generation)` called by the
   sweep pop; the CONTROL arm of the dispatch becomes validation (+ rule 2's force-sweep).
9. **New accessor** `TupleSinkServicePeerConnectionPostedSendWrsRdma(handle)` for the service-side stamp
   (mark = value AFTER the unit's last post; both command branches and the completion publish stamp at
   their existing `*commandWrCount`-known points).
10. **`qpPostOrdinal` is WRITE-ONLY at HEAD** (field `T:1267`; writers `T:5894` from the ring's own
    counter and `T:6244` from the owner entry's `postOrdinal`; zero readers). The `T:6244` write dies with
    `postOrdinal`; the field itself + `T:5894` stay (out of 2a scope, flagged for later deletion).
11. **Demand-scan predicates** (`T:16382-16394` + completion sibling; per-lane `HasSignaledOwner`
    `T:6607`/`T:6137`) re-implement as intrusive-list walks over the new lane table — the PEER_SEND_CQ
    collector itself is untouched until D-S3.
12. **Deleted outright:** `postOrdinal` fields, `NextClientSqlCommandWritePostOrdinal` (`T:1674`) +
    `NextPeerClientCompletionPublishPostOrdinal` (`T:1683`), both LaneFifo structs + global arrays
    (`T:1635-1642`, `T:1657-1664`, `T:1675-1685`), Reset/Find/Enqueue/Remove/FindOffset lane helpers,
    both range-retire functions (`T:6834`, `T:6364`) and the `allowAlreadyRetiredCheckpoint` concept
    (`T:6827`), the completion-entry inactive-tolerance arm (`T:6325-6333` — the sweep pops each entry
    exactly once; the named-CQE-already-swept case is what the validator now expresses).

Codex adversarial review outcomes folded in (round 1: 4 defects, 3 concerns, 2 nits; round 2 verified every
round-1 correction and found 2 NEW defects — items 19–20 below; round 3 verified those):

13. **D1 → purge retires (see the rule-5 amendment).**
14. **D4 → SUPERSEDED by round 4 after owner review.** The proposed posted-but-unlinked recovery was unsafe:
    Scenario-E treated every unlinked entry as unposted and could reuse its index before the old CQE arrived;
    merely retaining the orphan was also incomplete because CQ-demand execution enumerates sweep lanes, so an
    unlinked owner does not identify a QP to drain. The owner explicitly chose the research-prototype policy:
    lane allocation is structurally infallible (lanes are owner-table-sized), and violation is **ALARM +
    `exit(1)` immediately after post success**. The same fail-stop rule replaces the CONTROL dropped-entry
    fallback for impossible FIFO overflow / local WR-ID decode failure. Normal execution therefore has no
    posted-but-unlinked or dropped-control owner; validators handle only sweep/skew diagnostics.
15. **D3 → self-heal widened but deliberately NOT fully QP-wide at 2a:** service validators heal BOTH
    service classes through the proven mark; the CONTROL arm's heal also invokes `sendCheckpointSweep`.
    TWO residual gaps, both **2c items** (the skew ALARM is unreachable at 2a — always-on signalling + the
    bootstrap retire make the pairing exact): (a) a service-side heal cannot sweep the transport control
    FIFO; (b) NO heal repairs the scalar accounting itself (`retiredSendWrs` /
    `signalledRetiredOrdinal` are only advanced by the scalar retire), so a real skew would re-ALARM on
    every later CQE and leave SQ admission seeing phantom outstanding WRs — the heal releases owners, it
    does not re-pair the frontier. Also the heal is BEST-EFFORT: sweeping through a mark cannot pop past a
    larger-marked lane head (safe retention until a covering checkpoint), which the ALARM text now states.
16. **D2 adjudicated ACCEPTED-AS-DOCUMENTED:** abandoning owners drops the lane's CQ-drain demand on a
    quiet QP, but the OLD Scenario-E dropped it identically (Clear decremented the same signalled counts)
    *and* turned the parked CQE's eventual consumption into a "stale checkpoint" drain failure; the sweep
    consumes it cleanly. D-S3's pressure-drain triggers own the quiet-QP window. Not a 2a regression.
17. **Monotonicity-violation policy adjudicated ALARM-and-append (not reset):** a smaller mark appended
    behind a larger one can only be popped LATE (the sweep stops at the larger head first), never early —
    delayed release is safe, so failing/resetting would trade an accounting anomaly for a leak/run-killer.
18. **Round-1 nits fixed:** completion Abandon decrements the stats gauge; the stale `rdma.h`
    "callback-absent drains are control-only" comment rewritten to the D-S0 refusal rule; control
    pop-before-release documented as terminal-for-drain (matches the old dispatch-driven retire).
19. **Round-2 defect A → THREE purge triggers, not one.** The generation check originally ran only from
    same-handle lookups (link/sweep/Scenario-E) — so a table FILLED by owners stranded on dead connections
    blocked the very reservations whose lookups would have purged them. Added:
    `TupleSinkServicePurgeStale{ClientSqlCommandWrite,PeerClientCompletionPublish}SweepLanes()` invoked (a)
    on reservation table-full, with ONE retry if anything was reclaimed, and (b) from the demand scan,
    which generation-checks each CANDIDATE lane (one that passed `HasSignaledOwner`) and purges instead of
    handing a dead/reused connection's CQ to the drain. ⚠ Round-3 scope note: at 2a every owner is
    signalled, so every nonempty stale lane IS a candidate and gets purged there; under 2c's selective
    signalling an all-unsignalled stale lane would only be reclaimed by the table-full trigger — **2c must
    either move the generation check ahead of the demand predicate or accept the table-full trigger as the
    backstop.**
20. **Round-2 defect B → the signalled-demand LIFECYCLE moved to consumption.** Abandoning an owner used to
    decrement the signalled counts — dropping the only CQ-drain demand that would ever consume the parked
    CQE and free the linked slot (the owner-table capacity bound stopped being 1:1). Now: the signalled
    count is released at CQE CONSUMPTION (sweep pop, purge) or when the WR provably never existed
    (unlinked clear) — NEVER at abandonment; the per-lane demand predicates drop their `active` gate so
    abandoned owners keep their lane hot; helpers
    `TupleSinkServiceRelease{ClientSqlCommandWrite,PeerClientCompletionPublish}SignaledDemand` carry the
    rule. This also strictly supersedes round-1's D2 adjudication: demand is no longer dropped at all.

### D-S2a acceptance — PASS (2026-07-16, corrected final binary; Citus `bdcb7eb70`)

Post-implementation review found one HIGH exceptional-path defect in the first implementation: the validator
claimed it could recover a posted-but-unlinked owner, but Scenario-E treated every unlinked entry as unposted and
could reuse the index before the old CQE arrived. Retaining the orphan was also incomplete because CQ-demand
execution enumerates sweep lanes, so the orphan did not identify a QP to drain. The owner explicitly chose the
research-prototype resolution in item 14: structurally impossible service-lane allocation and control-owner
insertion/decode failures ALARM and fail-stop; process exit/QP teardown is the failure fence. A fresh adversarial
pass verified that correction and found no remaining ownership or normal-path defect.

Final full acceptance rebuilt/installed both trees, checksum-synced farnet0, rebuilt both DPU services, proved the
new fail-stop literal in all deployed binaries, and ran from a hard clean baseline with host services absent:

- debug gate: correct `homer-dpu-command` path, five distinct decoded `abalance` values, 5/5 processed, DPU backend
  spawn `COMPLETED`;
- warmed gate `-c 1 -j 1 -t 2000`: **303.395 / 298.298 / 295.362 TPS**, 2000/2000 and zero failures each — inside
  the accepted current/Stage-1 neighborhood, no directional regression;
- measured 4-role basebackup: sender/consumer exit 0, **23,254,330,999 bytes (21.6573 GiB)** in 7.96 s, clean
  `stream complete` + `CLOSE_ACK`; 512-KiB geometry forces **44,354 full ring laps plus 61,047 bytes**;
- both DPUs: global alarms = 0; Stage-2a sweep/skew/orphan/self-heal/purge/Scenario-E diagnostics = 0; supervised
  exit status 0; peer-control `live=0`, abandonment 0/0; head-mirror `live=0`; DMA drain 0 tasks,
  `free_slots=3072/3072`, `fatal=false`.

**Honest evidence boundary:** the implementation does not print final per-connection `postedSendWrs` and
`retiredSendWrs`, so exact numerical equality was not directly observed. Acceptance instead proves clean semantic
closure, no named-CQE/sweep recovery diagnostics, zero live retirement ledgers, zero DMA tasks, and clean service
exits. Node A also retained the pre-existing clean gate result-relay teardown shape (host-consumer detach → reason-4
connection reset → clean orphan reclaim, with `response_slots=1` and all control-op state counts zero); it carried no
Stage-2a alarm/recovery diagnostic and is not reclassified as a Stage-2a fallback.

### D-S2b IMPLEMENTATION MAP — ✅ IMPLEMENTED + VALIDATED (2026-07-16)

**Stage boundary:** behavior-neutral substrate only. Command, peer-client-completion, and control signalling remain
unconditionally ON. Do not flip `ShouldSignal`, de-schedule `PEER_SEND_CQ`, or implement FB-2 here.

A fresh review refuted the first draft's transient-owner wording and early reset rearm. That correction remains:
preflight precedes owner reservation, and reset becomes runnable only at zero session owners or exact
QP-destruction proof. Owner direction on 2026-07-16 then replaced the temporary single-owner-QP restriction with
the D-S2.1 dynamic per-QP shard design so multi-node support preserves the proof by construction. The final
adversarial pass verified the per-shard capacity theorem, opaque-context lifecycle, fair active-shard enumeration,
generation discipline, and shutdown continuation. The first runtime gate then found a missing traffic-class
boundary in the universal checkpoint callback; that localized defect is recorded below and does not change the
architecture.

**Implementation review corrections:** the first diff pass found and fixed three non-cosmetic gaps before build:
(1) pre-ready/setup-allocation reset now carries `lifecycleReadyNotified` so an expected NULL context is not confused
with a ready QP that lost its shard; (2) completion transport-credit pressure now owns an indexed blocked bitmap,
enters `WAIT_CQ`, is rearmed only by a successful CQE on the exact QP generation, and is cancelled rather than
retried on exact QP death; (3) a per-connection admission-refusal serial makes any per-post backstop refusal after
successful unit preflight a distinct ALARM; (4) impossible per-QP owner-shard full and both torn-unit arms now
fail-stop with explicit ALARMs. The same pass removed stale global-lane comments and the final formatter/diff check
is clean. The source builds and the corrected runtime re-validation is green; evidence follows the acceptance list.

**First acceptance failure and correction (2026-07-16):** the all-target build initially caught one missing local
`HomerServiceSendOwnerShard *shard` declaration in command-owner reservation; the one-line correction compiled and
was independently reviewed. The clean debug gate then completed 5/5 and both teardown ledgers converged, but the
farnet1 DPU emitted six repetitions of each command/completion sweep/lane lookup ALARM for outgoing generation 2.
The ordered evidence showed that QP was `traffic_class=2` (payload), while service shards are intentionally
allocated only for `CRITICAL_CONTROL` (`traffic_class=1`). `HomerServiceHandleSendCheckpointSweepFromDrain` was
universally invoked for every successful CQE and attempted service-lane resolution before checking the QP class.
The correction returns immediately for non-`CRITICAL_CONTROL` QPs after the transport has already advanced its
scalar/control frontier. This preserves "every WR class on an owner-bearing QP sweeps" without inventing shards for
payload QPs.

1. **Transport unit preflight API** (`remote_execution_peer_transport_rdma.{h,c}`): export the non-mutating
   total-unit admission helper from D-S2.2. It uses the clamped `admittedSendWr`, `postedSendWrs-retiredSendWrs`,
   exact `unitWrCount`, and whether the unit contains a signalled WR. Existing single-post/chain admission remains
   unchanged as a backstop. Document and ALARM if a backstop refuses after a successful preflight — that means the
   no-interleaving contract was violated.
2. **Command conversion** (`TupleSinkServicePostRemoteClientSqlCommandRecord`, current `T:23255`): decide
   whole-message vs split once, preflight 1-or-2 before `TupleSinkServiceReserveClientSqlCommandWriteCompletion`,
   then execute that same branch. Preflight refusal is BLOCKED/local transport pressure. No poll/post/scheduler
   handoff may intervene. At outgoing command-session bind (`T:41068-41070`), snapshot the connection generation
   beside the handle; shard access uses that expected generation rather than rereading a reused handle's generation.
3. **Completion conversion** (`TupleSinkServicePublishPeerClientCommandCompletion`, current `T:20670`): build
   enough of the source to know `descriptorPublishRequired`, preflight 1-or-2 before
   `TupleSinkServiceReservePeerClientCompletionPublishCompletion`, release the source slot on refusal, and return
   the new retryable `...BLOCKED_TRANSPORT_CREDIT` + `HOMER_PROGRESS_REASON_LOCAL_SEND_RESOURCE_PRESSURE`.
   Thread that result through deferred-completion readiness; send-CQ progress rearms it.
4. **Exceptional torn-unit policy:** zero-WR failure keeps ordinary classification; body-accepted/tail-failed
   ALARMS and fail-stops for both live units. No durable reservation rollback or partial-unit recovery. Keep the
   dead `TupleSinkServicePublishPeerCommandCompletion` path explicitly exempt pending S7 deletion.
5. **Dynamic per-QP owner shards and lifecycle bridge:** introduce service-owned `HomerServiceSendOwnerShard`
   with 256 command owners, 256 completion owners, one intrusive lane/cursor/counter set per class, and exact
   connection identity. Extend `HomerPeerConnectionLifecycleObserver` with a fallible ready callback invoked by
   `TupleSinkServiceFinishPeerConnectionSetup` before READY/canonical/bootstrap visibility. It allocates a shard for
   every `CRITICAL_CONTROL` QP and returns an opaque connection lifecycle context; non-owner traffic classes return
   NULL success. A generation-checked transport accessor lets command/completion reserve, link, sweep, named-CQE
   validation, and purge resolve the shard before using the WR-ID's shard-local 0..255 index. Allocation failure
   fails setup before posting; no hot-path allocation, hash/linear peer lookup, global quota, or WR-ID ABI change.
   Specify the ready callback as atomic ownership transfer: context-out starts NULL; failure must undo/free all
   partial state and leave NULL; success must return a fully initialized/listed non-NULL shard for
   `CRITICAL_CONTROL`; duplicate ready or pre-existing context ALARMS. Allocating for a control-only-but-capable QP
   is an accepted cold-path over-allocation.
6. **Shard enumeration, teardown, and capacity proof:** append ready shards to a service-owned intrusive active
   list; the CQ-drain executor walks it round-robin under the existing 64-connection bound, using per-shard demand
   while retaining global active/signalled counters only for aggregate readiness. On reset-complete, transport
   detaches the opaque pointer before invoking the observer. The service exact-purges all `active || linked` owners,
   rearms affected pending resets, unlinks/verifies/frees the shard, and only then runs helpers that may call
   `TupleSinkServiceResetSession`. Shutdown uses the same reset lifecycle and asserts zero shards. Per shard, assert
   `linked command + completion owners <= postedSendWrs-retiredSendWrs <= admittedSendWr <= 256`;
   preflight-before-reserve supplies the transient-owner free-slot proof. Reservation full after exact
   stale-generation purge is an invariant failure, not normal backpressure. Track active/peak shard count plus
   per-shard class/combined peak occupancy; multiple nodes and directions are accepted, not rejected. Active-list
   removal must advance/repair a round-robin cursor that names the removed shard. Every reserve, clear, sweep,
   inactive pop, zero-post failure, and QP purge updates per-shard and aggregate counters exactly once.
7. **Force-checkpoint plumbing** (`TupleSinkServicePeerConnectionState`): add `forceNextSignalCheckpoint`, a
   service-visible set/query API, and clear-on-successful-signalled-accounting in
   `TupleSinkServiceAccountPostedSendChainRdma`. Stage 2b's always-signalled predicates do not read it yet; Stage 2c
   will make it the first decision term. Reset clears it with the connection state.
8. **Reset proof outcome** (`HomerServiceDrainSessionSendCompletions` /
   `TupleSinkServiceRetireSessionSendCompletions` / `TupleSinkServiceResetSession`): preserve the existing first
   pure guard, then prove owners retired or QP/CQs destroyed before destructive reset. Live-QP nonconvergence arms
   pending reset (+ force checkpoint for Node A), retains owners/MRs, and returns DEFERRED. Remove live-QP
   Scenario-E abandonment; QP-death purge keeps the Stage-2a retire-not-memset rule.
9. **Indexed reset continuation:** extend `HomerServiceSessionPendingState` with reset owned/runnable bitmaps and
   add the per-session latch. Ordinary sweep rearms only when the pending session's total command + completion +
   partial-post owner count reaches zero; exact QP-death purge may rearm from its destruction proof. Add a top-level
   `RETRY_SESSION_RESET` action. The send-CQE callback only updates retirement/latch state. Audit every
   `TupleSinkServiceResetSession` caller: only the allocator may treat COMPLETED as reclaimed; accepted close keeps
   its existing success-on-DEFER semantics. Reorder shutdown to initial reset sweep -> destroy peer transport ->
   consume rearmed resets/dedicated final sweep -> assert no active sessions/pending reset bits/shards -> unmap
   control; do not end immediately after QP destruction rearms a deferred reset.
10. **Direct closure diagnostics:** add a cold teardown diagnostic for each connection's
   `postedSendWrs`, `retiredSendWrs`, signalled post/retired ordinals, and force flag. At a quiescent validation
   boundary with no owners left on that QP's shard, the relevant values must match; one session's semantic close is
   not sufficient while other sessions may still use the QP. On QP destruction, report the flushed delta rather
   than pretending it retired by CQE. Add reset-pending owned/runnable/live counts and shard active/peak/allocation-
   failure counts to the service teardown ledger.
11. **Comments + KB in the implementation commit:** update low-level admission comments (remove drifted line
    references and the claim that one WQE makes any multi-WR terminal unit postable), posting-function block
    comments, reset/Scenario-E comments, this plan's status, audit milestone, and current transport contracts.

**Validation / acceptance:**

- static: exact post-site inventory, `git diff --check`, repository formatter, all Citus/Homer smoke targets build,
  no stats/verbose literals in the measured binaries;
- normal path: fresh install/sync/DPU rebuild and landed proof, clean baseline, DPU command gate debug proof plus
  complete warmed repeat set, then 4-role basebackup with wrap proof — both unchanged from Stage 2a;
- accounting: no unit-preflight/backstop disagreement, torn-unit fail-stop, shard-allocation/table-full,
  named-CQE/sweep, generation/context, or reset-pending alarm; every ready `CRITICAL_CONTROL` QP has exactly one
  generation-matched shard, every other QP has none, and each shard's combined linked peak is within its QP's
  admission bound; clean connections report `postedSendWrs == retiredSendWrs`; teardown reset-pending and live-shard
  counts are zero;
- multi-peer evidence boundary: the design accepts simultaneous owner-capable QPs and must fail loudly if a future
  command/completion owner appears on a non-`CRITICAL_CONTROL` QP, but the current gate/basebackup topology does not
  prove real simultaneous multi-peer traffic. Record that arm as design/code-reviewed-only unless a bounded
  multi-peer exerciser is available; do not mislabel the existing two-node workload as multi-peer coverage;
- deferred-reset arm: preferably use one bounded diagnostic injection that makes the first session send drain
  report nonconvergence, then prove `DEFERRED -> covering CQE/purge -> reset runnable -> top-level COMPLETED` without
  MR deregistration before proof. If no deterministic hook is added, label this exceptional arm code-reviewed-only
  rather than claiming runtime coverage; the research-prototype scope permits that limitation.

**Acceptance PASS (run-validation, 2026-07-16, after the traffic-class correction):** clean all-target Citus build,
Citus install, PostgreSQL relink/install, farnet0 checksum parity, and forced rebuild/landed proof on both DPUs all
passed. The intended-path debug gate completed 5/5; stripped `-t 2000` warmup was 297.990 TPS, followed by complete
warmed repeats **300.149 / 298.033 / 294.586 TPS**, all 2000/2000 with zero failures and no directional regression
from Stage 2a. Four-role basebackup delivered **23,254,799,975 bytes (21.657720 GiB)** in **7.06 s**, sender and
consumer exit 0, `stream complete` + `CLOSE_ACK`; the 524,288-byte geometry proves **44,355 full ring laps plus
5,735 bytes**. Both DPUs had zero global/Stage-2b alarms, including zero recurrence of all four false shard-lookup
alarms. Clean QPs reported exact posted=retired; the few detach QPs reported a one-WR delta explicitly as
`flushed=1`. Both final ledgers were `reset_owned=0`, `reset_runnable=0`, `completion_transport_blocked=0`,
`active_shards=0`, `peak_shards=1`, `shard_alloc_failures=0`; control/head-mirror live counts were zero and DMA
ended `free_slots=3072/3072`, `fatal=false`. Both services stopped gracefully. True simultaneous multi-peer traffic
and the injected deferred-reset transition remain honestly code-reviewed-only, as permitted above.

**Commit boundary:** one Citus Stage-2b code commit plus the matching PostgreSQL KB commit after independent diff
review and full acceptance. No push. **Acceptance is complete; next: Stage 2c policy flip only.**

### D-S3 Drain de-schedule: the pull points that replace the PEER_SEND_CQ collector

1. **Post-clocked:** after any post that carried a signalled WR, bounded nonblocking poll (catches the
   *previous* checkpoint's CQE; its own arrives later).
2. **Pressure-forced:** on source-credit BLOCKED (`T:22637`→`blockedOnSourceCredit`), completion-ring
   reservation failure, control mailbox/op exhaustion (`R:8613`), or admission WOULD_BLOCK — drain that
   connection's send CQ synchronously, then retry once. ⚠ The credit gate at `T:22637` returns BLOCKED
   *before* touching the CQ today; the drain must be inserted on that path or the retry loop spins without
   ever freeing credit.
3. **Terminal:** session close/reset drains until converged or, after Stage 2b, proof-backed DEFER retains the
   session until a covering checkpoint/QP destruction rearms the indexed reset continuation.
Then delete the `PEER_SEND_CQ` collector (`T:2747`/`T:17143`/`T:49533`), de-alias FB-2 posters, add the
coalesced-poll stats macro.

### D-S1 Stage 1 — ✅ IMPLEMENTED + VALIDATED (3 validation rounds, 5 codex review rounds; 2026-07-16; audit §51)

> **ACCEPTANCE MET (round 3, §51.6):** (i) consumer Ctrl-C → sender bounded error via the F3 FAILED credit
> terminal (the 99%-CPU spin is dead); (ii) sender cancel → consumer bounded ERROR `stream is TRUNCATED`;
> (iii) the truncation probe held three ways — an aborted sender NEVER yielded consumer success. The F2
> backstop line was observed verbatim on the client-kill probe. Graceful basebackup + gate unregressed
> (gate 292/294/298 tps in-band, round 2). Open caveats: probe-C's distinct hook detail string unobserved
> (bounded exit proven mechanistically — §51.6); zero-frontier FAILED path code-reviewed-only.

*(History of the two failed validation rounds and their fixes: §51.4–§51.6 and the S1-FIX block below.)*

> **Status:** the full diff is in the working tree of both repos (control ABI **v34** — both DPUs must be
> rebuilt; round-1 validation already synced+rebuilt farnet0 and both DPUs). Review: 2 codex rounds settled.
> **Validation round 1 (2026-07-15, audit §51.4): the REGRESSION NET IS GREEN** — gate 4/4 proofs 5/5 tx,
> graceful 4-role basebackup clean ×2 (21.66 GiB, CLOSE_ACK), full alarm battery empty on both DPUs through
> every probe (`refusing to mark` empty ⇒ no new exit(1) arm ever fired; both DPU services survived all
> three aborts), and the post-abort reuse run proves **aborted runs leak no byte-ring slots/bindings**.
> **But none of the three abort probes reached the new abort path.** Root causes + decided fixes: S1-FIX
> below. Gate perf note: repeats 290.1/281.4/225.9 tps — monotone decline under a multi-hour validation
> session's load, no baseline row for this SHA; re-measure at re-validation before reading it as regression.

### S1-FIX (DECIDED with owner, 2026-07-15): three fixes + corrected probe stimuli. The diff so far is kept; these are additive.

> **Status: IMPLEMENTED; validation round 2 FAILED on an F2 REGISTRATION BUG (fixed; round 3 pending).**
>
> **Round-2 result (2026-07-15):** the gate is a clean in-band PASS (292.3/294.4/298.0 tps vs the 291–304
> band — round 1's 225.9 outlier was indeed machine load), but **EVERY basebackup died at end-of-backup**
> with `ERROR: before_shmem_exit callback ... is not the latest entry`: F2's backstop registered inside
> `bbsink_homer_begin_backup`, which runs INSIDE basebackup.c's `PG_ENSURE_ERROR_CLEANUP(do_pg_abort_backup)`
> window (`basebackup.c:280→318→403`); `PG_END_ENSURE`'s `cancel_before_shmem_exit` demands ITS callback be
> newest and found ours on top. The very LIFO hazard F2's own design note dodged for OUR cancel, inflicted
> on PostgreSQL's. **Fix: register in `bbsink_homer_new`** (sink construction precedes
> `perform_base_backup`, `basebackup.c:1067`, hence precedes the window); arming stays in begin_backup.
> LIFO now: on FATAL, `do_pg_abort_backup` runs first (shared backup state), then our backstop.
> **Silver lining, recorded honestly:** the crash fired the sender-initiated abort chain END TO END on every
> run — `local ABORT_RESET close armed` → `accepted peer ABORT_RESET close` → `peer ABORT_RESET close
> cleared sink` + `armed/published FAILED credit terminal` → consumer exits nonzero `stream is TRUNCATED`,
> `refusing to mark` empty, both DPU services alive — i.e. probe A's full mechanism worked under an
> unplanned trigger; round 3 must still prove it under the DELIBERATE stimuli (attribution) and prove F2's
> backstop line (never reached — the natural ERROR always preempted the client-kill).
> - The F3 trace resolved the mystery: the smoke-named function IS the production Loop-1 credit submitter
>   (name rot — renamed to `HomerDpuDmaSubmitByteRingConsumedHeadPublication`), reached via the
>   `DPU_CONSUMED_HEAD_PUBLISH` collector; `entryState` rides the credit BODY ahead of the epoch gate word.
> - F3 shape as landed: `creditEntryState` flavor on the submitter (ACTIVE strict-advance / FAILED
>   equal-frontier-and-zero allowed); ring-runtime latches `byteRingFailedCreditArmed/Published` (published
>   suppresses ALL later ACTIVE publication); `HomerDpuDmaArmFailedCreditForServiceSink` resolves by the
>   RUNTIME binding (D5) and reuses the ordinary pending bit + per-class counter for scheduling; armed from
>   `BeginPayloadAbortReset` and the reset-complete sender real-failure arm; client checks `entryState`
>   in `StillOpenForReserve`. Import teardown drops the arm (moot) but keeps the published latch.
> - **Round-3 catch (would have been round-1-probe-B all over again):** the submitter's argument guard
>   rejected `consumedHead == 0` — the NEVER-credited abort shape — leaving the pending item armed forever.
>   Fixed: zero allowed only under FAILED. Round 4 verified the corrected walk-through end-to-end.
> - **Recorded coverage gap:** the zero-frontier FAILED path (never-credited stream) is code-reviewed-only —
>   the runtime probes abort mid-flight (credit has flowed). A dedicated smoke leg (arm FAILED at
>   released==published==0, prove the host sees FAILED on epoch 1) is deferred work.

**Round-1 findings, each localized to a mechanism (evidence: audit §51.4):**

- **F1 — probe stimuli were MIS-AIMED (validation-spec error, not code).** SIGINT went to the `pg_basebackup`
  CLIENT. Under Homer, archive bytes never cross libpq, so the walsender does not notice a dead client until
  it next touches the socket: early SIGINTs died in protocol I/O as **`FATAL: connection to client lost`**
  (see F2), later ones let the backup run to natural completion (5/6 attempts). The designed entry point —
  backend **ERROR** → `PG_FINALLY` → cleanup abort-close — was never stimulated.
- **F2 — REAL GAP: the FATAL path skips the abort entirely.** `FATAL` does not unwind `PG_TRY`/`PG_FINALLY`
  (straight to `proc_exit`), so `bbsink_homer_cleanup` never runs on client-connection-lost; farnet1's DPU
  saw only the mmap teardown (`grouped-control read lost host export`), and the peer was never told.
- **F3 — REAL GAP (the big one): a selected-DPU SENDER has no failure signal at all.** Probe-B's walsender
  spun at 99% CPU >138 s after the consumer aborted and the connection reset, because:
  (a) `HomerClientBaseBackupStreamStillOpenForReserve` (homer_client.c:307) polls host-memory
  `byteRingControl->flags`/`terminalStatus` — a **host-service-era contract; nothing DPU-side ever writes
  them** for a selected-DPU sender (its own comment "Transport reset publishes FAILED" describes the dead
  architecture);
  (b) `HomerClientDpuRefreshBaseBackupCredit` (homer_client.c:2362) — polled EVERY reserve iteration — reads
  epoch/generation/ringIndex/consumedHead from `dpuPayloadCreditLine` but **never `entryState`**, which the
  credit line carries (`HomerDpuBridgeDpuCreditLine.entryState`, DPU-owned).
- **F4 — the RECEIVE abort arm targeted the wrong stream entry.** The consumer's `CLOSE_SESSION(abort)`
  names ITS OWN sink id; the relay stream is a SEPARATE entry created by the far sender's peer OPEN, so
  `peerBindingActive && !locallyInitiated` never held on the named entry and the arm no-op'd. Practically
  masked: consumer exit → HOST_DETACHED → `terminating DPU relay after host consumer detached` →
  `requesting exact reset for transport cutoff` achieves the identical connection reset (observed in the
  run). The arm as written is dead code.

**The fixes (work list, in order):**

1. **F3 trace (BEFORE design freeze): locate the PRODUCTION writer of the sender's credit line.** The only
   visible submitter of the `BYTE_RING_CREDIT_BODY/CREDIT_PUBLISH` two-task chain (D:3704-3718, which stamps
   `entryState=ACTIVE` at D:3701) is `HomerDpuDmaSubmitByteRingConsumedHeadSmokePublication` — a SMOKE-named
   function with no other callers found by grep, yet production credit demonstrably flows (21.6 GiB through
   a 512 KiB ring). Either name rot (it IS the production path) or the credit line is written by another
   mechanism entirely. **Do not design F3's publisher until this is traced** (codex-explore task).
2. **F3 fix — sender-side FAILED credit publication + client check.** DPU side: on sender-stream
   reset/abort teardown, publish `entryState=FAILED` on the SEND stream's credit line via (whatever step 1
   finds) — mirror of leg 3's receiver FAILED terminal, pointed at the other host. Client side:
   `HomerClientDpuRefreshBaseBackupCredit` (and/or `StillOpenForReserve`) checks credit-line
   `entryState==FAILED` → reserve/submit paths fail with a distinct "stream failed by peer/service" error →
   bbsink ERROR → cleanup abort-close (or plain exit if already closing). Also REWRITE the stale
   host-service-era comment on `StillOpenForReserve` (it states the refuted contract as fact).
3. **F2 fix — `before_shmem_exit` backstop in the bbsink.** Registered at `begin_backup` (after the stream
   opens), disarmed after the graceful close in `end_backup` AND in `cleanup`; if it fires with
   `stream_open` still true → best-effort `HomerClientAbortBaseBackupStream`. Runs on FATAL/proc_exit,
   which `PG_FINALLY` does not. Must be re-entrancy-safe with cleanup (both may run; abort-close is
   idempotent client-side — verify, else latch).
4. **F4 fix — delete the dead RECEIVE arm, keep the semantics via HOST_DETACHED.** The existing
   detach path already performs the connection reset the arm intended; re-targeting the arm would duplicate
   it. Delete the arm + its log line; keep `closeFlags` accepted on RECEIVE (no error), and comment WHY the
   consumer-initiated abort rides HOST_DETACHED (the abort-close's real work on RECEIVE is prompt process
   exit; F3 is what unsticks the sender). ⚠ If F3's fix needs the reset to carry "abort" vs "failure"
   flavor, revisit — but round-1 evidence shows the sender-side DPU already marks streams ABORTING on the
   reset, which is sufficient input for F3's FAILED publication.
5. **Corrected acceptance probes (re-validation spec):**
   - **A (truncation probe): cancel the WALSENDER** — `pg_cancel_backend(<walsender pid>)` or SIGINT to the
     backend pid mid-transfer → ERROR → PG_FINALLY → abort-close → `ABORT_RESET` → consumer exits nonzero
     "terminated FAILED by sender abort", never "stream complete".
   - **A2 (NEW, exercises F2): kill the CLIENT mid-transfer** (SIGKILL pg_basebackup client) → walsender
     FATAL at next socket touch → before_shmem_exit backstop → same ABORT_RESET chain as A.
   - **B (consumer Ctrl-C): unchanged stimulus**, but the sender-side pass criterion becomes: walsender
     exits bounded via F3's FAILED credit signal (no 99%-CPU survivor).
   - **C (interrupt hook): SIGSTOP consumer → backpressure → `pg_cancel_backend(<walsender pid>)`** (NOT
     client SIGINT) → reserve loop's interrupt check fires → bounded ERROR ("interrupted while waiting for
     ring space") → abort chain.
6. Codex re-review of the S1-fix delta, then re-validate (regression net + A, A2, B, C + reuse run), then
   commit code+KB as one Stage-1 commit set.

**⚠ DEVIATIONS AND DECISIONS RECORDED AT IMPLEMENTATION (read before Stage 2):**

1. **The abort POST keeps the `finalPayloadSendRetired` gate** (the plan text below says "bypasses ... for
   the POST"). The gate is RNIC-local — an RDMA write completes once the remote HCA acks, no consumer
   involvement — so it stays bounded with a wedged consumer, and it preserves the receiver-ring lifetime
   guarantee identically to graceful close. What the abort actually bypasses: the caller's source-drain
   gate (staged-but-unposted bytes are discarded), the tuple EOS wait, the remote-head wait, and the
   QUIESCED retry loop.
2. **Leg 4 needed NO new code**: the ALARM + Scenario-E cancellation already sits at the exact choke point
   (`TupleSinkServiceRetireSessionSendCompletions`, `T:7100-7153`, called from `ResetSession` before the
   MR deregs). The map's "add a check" is satisfied by the existing instrument.
3. **The real implementation work beyond the map was SCHEDULER COVERAGE** — three forever-wedges found by
   tracing + one by review, each invisible until an abort actually ran:
   - `HomerServicePayloadCloseActionReady`: abort arm returns `relayTerminalPublished` BEFORE the
     receive-drain gate (an abort discard is never "drained" — consumedHead stops by design);
   - `HomerServicePayloadMustProgressBeforeClose`: early-false under either abort flag (both its
     predicates hold forever in the stuck-consumer case — the exact case the abort exists for);
   - the ABORT_RESET accept arm latches a **synthetic `receivePayloadDoorbellPending`** (graceful rides
     the final payload doorbell to keep the relay pump hot; an abort can arrive with the pump COLD and no
     doorbell ever coming);
   - **(review) family gate**: ABORT_RESET is accepted ONLY for `BASE_BACKUP_STREAM` — the tuple-view
     relay has no discard implementation and would wedge unscheduled; rejected loudly instead.
4. **Tombstone repeats demand the EXACT abort shape** (`closeFlags == ABORT_RESET`): the tombstone check
   runs BEFORE the live path's flag-shape validation, so it must validate shape itself.
5. **KNOWN LIMITATION — PRE-EXISTING, verified against HEAD `f45556838` by both review rounds:** a
   connection reset before the FAILED terminal is published still strands the selected-DPU consumer
   (`MarkPayloadStreamReceivePeerFailed` no-ops when neither POSIX receiveQueue control exists,
   `T:28325-28329`; reset-begin parks the stream at `ABORTING`, descheduled). Pre-diff there was NO
   FAILED publisher at all, so the window is unchanged — Stage 1 strictly shrinks the hang set. The
   named future fix (**§37/P2-L**): publish the role-7 FAILED terminal from reset-complete for
   remotely-initiated selected-DPU relay streams. Reset paths never call
   `MarkPayloadCloseReclaimable`, so the new abort `exit(1)` fact-arms are unreachable mid-reset.
6. **Freeze-order rule (load-bearing):** the sender pump gate (`abortLocalSendDiscard`) is set BEFORE the
   close continuation freezes the final tail — `TupleSinkServiceBeginPayloadAbortReset` owns that order;
   the freeze guard `exit(1)`s on a moved tail. `HomerServicePumpOutgoingPayloadStream` was verified
   (review C2) to be the SINGLE posting funnel, so the one gate freezes the tail.

*(Original finalized design below, unchanged except where superseded by the deviations above.)*

**Mechanism: (b) `CLOSE_SINK | ABORT_RESET`** — transport close-level, posted **signalled** on the
**stream-bound connection** (`T:29847` requires that connection anyway; the graceful close is already posted
signalled there via `R:8651-8654`/`:11062-11090`, ordered behind the final payload WIMM by the
exact-connection helper `R:12408-12468`), which makes the abort notification and the terminal checkpoint the
same WR (C4). Mechanism, stated precisely: payload chain tails are signalled (and remain so — payload is out
of the coalescing scope), so every earlier payload chain's OWN CQE precedes the abort WR's CQE in the same
CQ; **draining until the abort CQE appears consumes those payload CQEs first, each freeing its owners via its
own callback — the abort CQE is the CONVERGENCE MARKER, not the retirement event for payload owners.** Rejected: `OBJECT_ERROR`-as-flush — an **in-band payload record**: (i) needs
byte-ring space, unavailable exactly in the stuck-consumer abort case, (ii) basebackup-specific, (iii) wrong
layer for the QP checkpoint. (`OBJECT_ERROR` submit support exists but is unwired scaffolding —
`homer_client.c:5580`.)

**What the trace established (the bug is BIGGER than "both ends hang"):**
- `bbsink_homer_error` (`basebackup_homer.c:76`) is only `ereport(ERROR)` — no abort message exists anywhere.
- **Silent truncation, live today:** a CAUGHT sender error runs `PG_FINALLY` cleanup
  (`basebackup.c:1065-1072` → `bbsink_homer_cleanup` `basebackup_homer.c:404`) which does a **normal
  drain-close**; the consumer completes on the transport EOS flag and **never checks `objectKind`**
  (`HomerClientBaseBackupDeliverReceiveObject` `homer_client.c:4492`, EOS from flags `:4510-4529`) — an
  aborted backup can be reported as consumer SUCCESS.
- **The producer hang is a non-interruptible spin:** the reserve loop
  (`HomerClientReserveBaseBackupRecordForObjectPayload` `homer_client.c:5211`, loop `:5266-5392`) checks only
  Homer terminal flags — no interrupt hook — so a backpressured producer (dead consumer) cannot even be
  cancelled; only `kill -9` (which then leaks the whole stream).
- **Nothing detects a dead consumer**: setup TCP is ephemeral (`homer_client.c:2409-2432`), `ownerPid` is only
  validated nonzero (`homer_service_dpu_dma.c:14093`), teardown fires only on EXPLICIT close
  (`HOST_DETACHED`, `homer_service_dpu_dma.c:10413-10445` → relay terminate `T:35465-35481`).

**The legs:**
1. **Producer side:** (a) `bbsink_homer_cleanup`'s close defaults to **ABORT** unless `OBJECT_END` was
   submitted (`bbsink_homer_end_backup` `basebackup_homer.c:383-388` sets a clean-end flag) — this kills the
   silent truncation; (b) add an interrupt hook to the reserve loop (`:5266`) so a backpressured backend can
   be cancelled at all (backend: `CHECK_FOR_INTERRUPTS`-driven; the loop is client-lib code, so a
   caller-supplied callback), after which cancellation flows into (a).
2. **Service→peer:** the abort-close **bypasses** the sender's retirement-proof gate
   (`TupleSinkServicePeerCloseSinkBestEffort` refuses to send until payload sends retired, `T:28902-28945`)
   and the final-tail freeze — abort has no drain contract; receiver accept path replaces the reject at
   `T:29832-29838` with **discard semantics**: skip the drain predicates (`T:28505-28529`) and the final-head
   signalled WIMM (`T:37520-37587`); tear down + tombstone; the response echoes `ABORT_RESET` and is
   **distinguishable from `QUIESCED`** (`QUIESCED` = drained-to-final-tail + final-head posted,
   `T:29902-29915` — must not be conflated).
   *(refutation round 1 — both halves sharpened:)* **Sender:** the bypass licenses only the POST; the
   sender's owners/MRs stay live until the abort WR's **own CQE** is consumed and swept — the peer's RESPONSE
   is not that proof (a close response can precede the request's send CQE; that is what `RETIRING` exists
   for, `R:12718`). The gate's `finalPayloadSendRetired` guards exactly this reclamation (`T:28923`,
   `T:27223`) and the abort path re-proves it via the frontier instead. **Receiver:** "discard" is STAGED,
   not instant — the relay submits DOCA DMA asynchronously (`T:35528`) and reclamation already waits for
   `writtenTail == publishedProducedTail && !byteRingWriteInFlight` (`homer_service_dpu_dma.c:10524`); QP
   ordering orders the peer's RDMA writes before the abort WIMM but says NOTHING about receiver-local DMA
   in flight. Sequence: stop new relay submissions → latch the discard frontier → retire/cancel the current
   DMA chain → publish FAILED (leg 3) → only then reclaim the binding. The existing payload `ABORTING` state
   is NOT reusable (its clear-proof requires QP destruction, `T:27263`); `ABORT_RESET` gets its own proof
   state.
3. **Receiver-service→consumer relay** *(reshaped by refutation round 1 — the leg as first drafted was
   IMPOSSIBLE):* two verified blockers: (a) the consumer's `CLOSED || FAILED` check sits INSIDE the
   `consumedHeadAfter >= producedTail` drain gate (`homer_client.c:4679-4688`), and the head only advances
   across COMPLETE objects (`:4608`) — so a torn final object blocks exit forever, drain-gated FAILED is
   useless for abort; (b) **no receiving-DPU FAILED publisher exists** — the only terminal publisher
   hardcodes `HOMER_DPU_BRIDGE_ENTRY_CLOSED` (`homer_service_dpu_dma.c:4238`), and
   `HomerServicePublishLocalPayloadFailure` (`T:28338`) writes local queue controls, not the imported role-7
   credit line. Fix: parameterize the credit-line terminal publisher (`CLOSED`→`state` arg) so the abort path
   publishes `FAILED` on a fresh credit epoch; on the consumer, make `FAILED` terminal **independent of
   drain** — deliver complete objects already in the ring, then exit with error even if a torn tail remains.
   *(Round 2 additions, VERIFIED:)* the zero-epoch window is safe (an abort before any payload publishes
   terminal on epoch 1, observable — `homer_service_dpu_dma.c:4213`/`:4240-4263`, consumer records it at
   `homer_client.c:4580-4601`), **but FAILED must carry a DISTINCT result**: today both `CLOSED` and `FAILED`
   set `streamComplete=true` and the consumer exits SUCCESS (`homer_client.c:4684-4690` →
   `pg_basebackup.c:2431-2436`) — moving FAILED outside the drain gate without a distinct status would still
   report an aborted backup as successful. Edge recorded: an abort BEFORE the role-7 import is active is
   outside this mechanism (publisher rejects an unimported descriptor, `homer_service_dpu_dma.c:4171`); that
   window is covered by the consumer's open/poll error paths, not the credit line.
   Also: a SIGINT handler in `--homer-receive` doing a best-effort abort-close (today SIGINT exits with no
   close at all — only the poll-failure path closes, `pg_basebackup.c:2421-2426`).
4. **SQL-session abandonment (command lane) — a CONTRACT, not new machinery.** Trace-VERIFIED: after abrupt
   client death node A posts **no successor WR** for that session (staging only from real client commands,
   `T:44315-44420`; `RETIRING` is local source retirement, `R:5108-5131`/`R:12565`, posts nothing). Under
   coalescing this is safe-but-lazy on the shared `CRITICAL_CONTROL` QP: ANY later session's checkpoint
   retires the dead tail (QP-wide frontier); a fully-quiet QP holds it, uncontended — no deadlock, bounded
   staleness, no leak beyond the already-leaked session. The binding rule: **a session reset on a SURVIVING
   connection with unretired session WRs must have checkpoint PROOF before owners are cancelled or MRs
   deregistered — absent proof, it DEFERS** (D-S2.4; round 2 removed the synthetic-close shortcut from this
   stage — the close needs a registered source and node-B semantics that only §37/P2-L can build). Stage 1
   adds only a defensive check+ALARM at `TupleSinkServiceResetSession`.

**Out of scope (recorded, not fixed):** endpoint deaths with no EOF-able channel (`kill -9`) remain
undetectable until connection reset/service restart — Stage 1 shrinks the hang set to those, not to zero.
Liveness detection is §37/P2-L territory.

**Acceptance (no coalescing interval needed):** (i) Ctrl-C the consumer mid-transfer → sender `pg_basebackup`
exits with a bounded error (no reserve-loop spin); (ii) cancel the sender mid-transfer → consumer exits with
**ERROR**, bounded; (iii) the truncation probe: a sender aborted after N bytes must NEVER yield consumer
success — this is a NEW check no run ever made before.

### D-S1 IMPLEMENTATION MAP (2026-07-15, pre-diff site enumeration — all sites re-read at HEAD)

Micro-decisions SETTLED at implementation:
- **Interrupt-hook shape: a registered callback** (`homer_client.c` is client-lib code linked into both the
  `postgres` executable and frontend tools — it cannot reference backend-only symbols). The bbsink registers a
  non-throwing `INTERRUPTS_PENDING_CONDITION()` wrapper at begin_backup; the reserve loop polls it on its
  existing `terminalCheckCountdown` cadence and returns false ("interrupted") — the abort then flows through
  the normal bbsink ERROR → `PG_FINALLY` → cleanup-abort path. The callback must NOT throw (no longjmp out of
  lib code).
- **FAILED drain-independence mechanics: poll returns FALSE with a distinct error.** In
  `HomerClientPollBaseBackupReceive`, `entryState == FAILED` (honored only once `dpuLastCreditEpoch != 0`) is
  checked AFTER the drain pass but OUTSIDE the `consumedHead >= producedTail` gate: deliver what drained,
  then return false with a "sender aborted / truncated" error. The consumer's existing poll-false path
  (best-effort close + `pg_fatal`) becomes the abort exit lane — structurally impossible to report SUCCESS.
  `CLOSED` keeps the drain-gated success arm unchanged.
- **Direction split confirmed by code**: consumer-initiated abort rides the EXISTING
  `HomerServiceRequestBoundPayloadProtocolReset` (`T:5060` → connection reset → sender-side transport reset
  publishes FAILED into the byte-ring flags `StillOpenForReserve` polls, `homer_client.c:299-316`). Only the
  sender-initiated abort needs the new `CLOSE_SINK|ABORT_RESET` on the surviving connection.

Concrete edits (`T` = tuple_sink_service_process.c, `H` = homer_client.c, `D` = homer_service_dpu_dma.c):

1. **ABI** — `homer_control_abi.h`: `CitusRemoteExecCloseSessionRequest` gains `uint32_t closeFlags` (+pad) +
   `CITUS_REMOTE_EXEC_CLOSE_SESSION_FLAG_ABORT_RESET`; **bump `CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION`**
   (host↔DPU lockstep rebuild — both DPUs). `HomerDpuDmaSubmitByteRingWriteTerminal` (`D:4121`, hardcoded
   CLOSED at `D:4247`) gains an `entryState` arg; existing callers (`T:35668`, `T:36500`) pass CLOSED.
2. **Client** — `HomerClientCloseBaseBackupStream` → internal `...Internal(stream, abortReset, err, bytes)`;
   new public `HomerClientAbortBaseBackupStream`. Abort skips the tail flush and stamps
   `closeFlags=ABORT_RESET` into the `CLOSE_SESSION` request (`H:5677-5707`). Interrupt hook: global callback +
   setter, polled in the reserve loop's countdown branch (`H:5380-5391`).
3. **bbsink** — `bbsink_homer_cleanup` (`basebackup_homer.c:404`): `stream_open==true` at cleanup ⇒
   end_backup never completed ⇒ **abort-close** (this IS the truncation-kill: no new flag needed — end_backup
   already sets `stream_open=false` only after its graceful close). `bbsink_homer_begin_backup` registers the
   interrupt callback.
4. **Consumer** — FAILED arm per micro-decision above; SIGINT/SIGTERM handler in `RunHomerReceiveConsume`
   (volatile sig_atomic_t; loop check → abort-close → `pg_fatal`); poll-false path switches to abort-close.
5. **Sender service** — `TupleSinkServiceHandleCloseSession` (`T:41527`) threads `closeFlags`; SEND abort ⇒
   abort variant of `TupleSinkServicePeerCloseSinkBestEffort` (`T:28873`): gate the outgoing payload pump
   FIRST (new stream flag; the freeze guard `T:27208` exit(1)s on a moved tail), freeze at the now-stable
   posted tail, `closeState->abortReset=true`, skip the EOS-wait (`T:28905`) and `finalPayloadSendRetired`
   (`T:28945`) gates for the POST only. Close continuation (`T:28983-29112`): abort builds the request with
   `ABORT_RESET` (`T:27394`), response validation (`T:27400`) gains the abort-echo arm (`peerBindingStillActive
   ==0` + `ABORT_RESET` flag, ≠ QUIESCED), the final-head gate (`T:29101`) is skipped under abort.
   **`HomerServiceMarkPayloadCloseReclaimable` (`T:27223`) gets a distinct abort fact-arm** — its existing arms
   exit(1) on unproven facts and must NOT be satisfied by faking `peerQuiesced`/`finalHeadObserved`; the abort
   arm requires `finalPayloadSendRetired` (owner counters zero — the frontier proof; all WRs signalled at HEAD)
   + the abort response (or connection death). MRs/owners live until then.
6. **Receiver service** — `TupleSinkServiceHandlePeerCloseSinkRequest` reject (`T:29843`) becomes the
   ABORT_RESET accept arm: same identity checks (tokens/connection/direction), then STAGED discard: set
   discard flag; relay pump (`T:35528+`) stops new submissions; terminal publish arm (`T:35663`) under abort
   drops the `writtenTail == publishedTail` requirement (waits only `!writeInFlight`) and publishes **FAILED**;
   response echoes ABORT_RESET with `peerBindingStillActive=0`. Receiver reclaim arm in `T:27223` (abort):
   FAILED terminal published + response posted; skips `finalHeadWimm*` (the final-head WIMM `T:37520` is
   normal-close-only). NOT reusing payload `ABORTING` (`T:27263` clear-proof requires QP destruction).
7. **RECEIVE-direction abort** (consumer-initiated) — `HandleCloseSession` RECEIVE + ABORT flag ⇒
   `HomerServiceRequestBoundPayloadProtocolReset` per bound stream; teardown then converges via the existing
   reset machinery (S1c full-reset path).
8. **Leg 4** — defensive check+ALARM at `TupleSinkServiceResetSession` (`T:25006`): unretired session send WRs
   on a surviving connection ⇒ ALARM (instrument only; D-S2.4 DEFER rule arrives with Stage 2b).

---

## Open decisions
- **Stage 1 abort mechanism** — **DECIDED: `CLOSE_SINK|ABORT_RESET`** (D-S1; trace-confirmed the graceful
  close already rides the stream-bound QP signalled, so the abort variant is the same WR shape).
- **Stage 2 control retirement-by-frontier** — **DECIDED: add a control post-order FIFO head**, swept like
  the two lane FIFOs (D-S2.0).
- Still open (small, settle at implementation): the FAILED-state drain-independence in the consumer completion
  predicate (`homer_client.c:4679-4688`, D-S1 leg 3); the interrupt-hook shape for the reserve loop (callback
  vs direct backend check, D-S1 leg 1b).

## Separate, NOT gating this work
- **The 235.4µs per-command control latency cause is UNATTRIBUTED** (C5's attribution was dead code). Measure at
  the gate/S6 to localize it; it is not what this coalescing fixes.

## Related
- Design: [`../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`](../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md)
- §29(b) accounting + the P0-i revert: [`resource_retirement_contract_audit.md`](./resource_retirement_contract_audit.md)
- The migration this sits inside (S6 follows): [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md)
