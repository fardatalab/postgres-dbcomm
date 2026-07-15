# Send-CQE coalescing — implementation plan

<!-- kb-summary: Ordered stage plan for re-arming send-CQE coalescing on the peer RDMA transport, with the 2026-07-15 site-enumeration corrections to the design. -->

**Status: PLANNING (2026-07-15). No code yet.** Graduated from the design
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

## Stage 2 — re-arm the interval (C1 decision + C2 stamping + C3 unit reservation).

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
- **C3 unit reservation** — reserve the unit's TOTAL WRs before the first post (replaces the per-post 1-WQE
  reserve `R:10883`, which works only because every unit happens to have a 1-WR tail). Five send funnels
  (`R:10837`); units: command 1-or-2, peer-client-completion 1-or-2, control 1, payload chains 1–33.
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

## CONCRETE DESIGN (2026-07-15) — settled through TWO adversarial refutation rounds; ⚠ pending owner
## ratification of ONE choice (the owner-table strategy, option A vs B in D-S2.1) before implementation.

Round 1 found 7 objections (all verified, all folded — the "(refutation round 1)" blocks); round 2 attacked
the corrections and found 4 more defects (the synthetic-close backstop died — see D-S2.4; the sweep
validation gained a cross-connection arm; FAILED needs a distinct result; the global-table watermark needed
the option-A redesign). The stage list above says *what*; this section says *how*, with the mechanism nailed
to code. `T` = `tuple_sink_service_process.c`, `R` = `remote_execution_peer_transport_rdma.c`.

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
- **KEEP** the lane FIFO arrays and the arbitrary-removal path (`T:6508`) — the latter is the Scenario-E
  abandonment cancellation, which is orthogonal. **DELETE** `postOrdinal`/`NextClientSqlCommandWritePostOrdinal`
  (`T:1674`), `FindClientSqlCommandWriteLaneFifoOffset` (`T:6537`) and the peer-client-completion siblings.
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
- **(refutation rounds 1+2) A FOURTH pressure input: the GLOBAL owner tables — and a global watermark alone
  is NOT the fix.** The per-session terms do NOT cover the two service-global 256-entry CQ-owner tables
  (`ClientSqlCommandWriteCompletions` / `PeerClientCompletionPublishCompletions`, capacities
  `remote_execution_peer_transport_rdma.h:219-220`, full branches `T:6673`/`T:6215`): ~4 unretired owners ×
  64 sessions exhausts a table with every per-session predicate false. Round 2 then broke the naive fix: a
  GLOBAL ActiveCount watermark forces signals only on connections that POST, whose connection-local sweeps
  **cannot retire owners belonging to QUIET connections** — the table can fill with quiet-QP owners and
  drain-retry on the posting connection frees nothing (today's scheduled collector avoids this by scanning
  EVERY owner-bearing FIFO, `T:16309`/`T:16333` — exactly what D-S3 de-schedules). **DECIDED fix (option A;
  ratify at review):** (i) per-connection active-owner counts, with the forced-signal term keyed on the
  CONNECTION's share (`capacity / (2 × live connections)`, recomputed on connect/disconnect — cold path);
  (ii) a small checkpoint-only RESERVE of table entries (mirror of the WQE reserve `R:10905`: a forced
  signalled post must always be able to reserve its owner entry, or the checkpoint that frees the table
  cannot be posted — the same deadlock one level up); (iii) the table-watermark / reservation-failure handler
  drains EVERY distinct owner-bearing connection (pull-style port of the collector's scan), not just the
  poster's. Owner-table reservation failure (today `PUBLISH_FAILED`, `T:20349-20365`, not retryable) becomes
  drain-then-retry-once. *Recorded fallback (option B): partition the owner tables per connection — the
  structurally cleanest answer (the global tables are an all-signalled-era sizing assumption) but a larger
  refactor; adopt if (i)-(iii) prove fiddly in implementation.*
- Control op vs response-source slots retire differently (request CQE only sets `sendCompletionRetired`
  while the op stays `WAIT_RESPONSE`, `R:4383`; response slots free directly on CQE, `R:4357`) — the control
  pressure term counts them separately.

### D-S2.2 Unit reservation (C3)

The 2-post units (command body+readySeq `T:22750`/`T:22759`; completion descriptor-body+WIMM `T:20369`/`T:20394`
— NB the admission comment `R:10887-10889` still cites the drifted `:19410/:19435`, fix it in the same commit;
the completion unit's terminal term can reuse the close-is-last latch condition at `T:20427-20431`)
get atomically admitted: admit the unit's TOTAL WRs at the first post (a per-connection credit the later posts
draw down, with rollback on unit failure), replacing reliance on the 1-WQE checkpoint reserve
(`R:10905`) — which stays, but reverts to its real job: guaranteeing an **emergency terminal flush is always
postable on a full queue**. Note the current tail-failure-after-body path is `exit(1)` (`T:22789`); unit
reservation makes that unreachable-by-construction from admission causes.

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
  CQ size is never inspected either). Fix in S2(b): clamp the stored admission ceiling to
  `min(granted, CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH)` — using fewer WQEs than granted is always safe, and
  the clamp bounds both hazards at once.
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
**Under coalescing, a quiet unsignalled tail makes the timeout the EXPECTED path** — and cancelling owners +
deregistering MRs without CQE proof is releasing memory the NIC may still be reading: the P0 violation, made
routine. The fix is structural, not a longer timeout:

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

### D-S0 Standalone hardening commit — FIRST, before Stage 1 (owner-confirmed 2026-07-15)

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
be unchanged. (b) bootstrap retire fix + unit reservation + the D-S2.4 surviving-QP reset backstop. (c) flip
the decision function, including the global-owner-table pressure terms (tripwires `T:423`/`T:442` replaced in
the same commit; Stage 1 must already be landed).

### D-S3 Drain de-schedule: the pull points that replace the PEER_SEND_CQ collector

1. **Post-clocked:** after any post that carried a signalled WR, bounded nonblocking poll (catches the
   *previous* checkpoint's CQE; its own arrives later).
2. **Pressure-forced:** on source-credit BLOCKED (`T:22637`→`blockedOnSourceCredit`), completion-ring
   reservation failure, control mailbox/op exhaustion (`R:8613`), or admission WOULD_BLOCK — drain that
   connection's send CQ synchronously, then retry once. ⚠ The credit gate at `T:22637` returns BLOCKED
   *before* touching the CQ today; the drain must be inserted on that path or the retry loop spins without
   ever freeing credit.
3. **Terminal:** session close/reset drain-until-converged (exists, `TupleSinkServiceRetireSessionSendCompletions`).
Then delete the `PEER_SEND_CQ` collector (`T:2747`/`T:17143`/`T:49533`), de-alias FB-2 posters, add the
coalesced-poll stats macro.

### D-S1 Stage 1 — FINALIZED against the 2026-07-15 abort-path trace (all sites VERIFIED by codex + spot-checked)

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
