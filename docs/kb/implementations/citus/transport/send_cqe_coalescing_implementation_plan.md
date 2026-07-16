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

## CONCRETE DESIGN (2026-07-15) — SETTLED. Two adversarial refutation rounds + owner review.

Round 1 found 7 objections (all verified, all folded — the "(refutation round 1)" blocks); round 2 attacked
the corrections and found 4 more defects (the synthetic-close backstop died — see D-S2.4; the sweep
validation gained a cross-connection arm; FAILED needs a distinct result); owner review then replaced the
round-2 owner-table machinery with the **size-out** (D-S2.1 — the P0-c move, simpler and structural). The
stage list above says *what*; this section says *how*, with the mechanism nailed to code. `T` =
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
- **(refutation rounds 1+2) A FOURTH pressure input: the GLOBAL owner tables — and a global watermark alone
  is NOT the fix.** The per-session terms do NOT cover the two service-global 256-entry CQ-owner tables
  (`ClientSqlCommandWriteCompletions` / `PeerClientCompletionPublishCompletions`, capacities
  `remote_execution_peer_transport_rdma.h:219-220`, full branches `T:6673`/`T:6215`): ~4 unretired owners ×
  64 sessions exhausts a table with every per-session predicate false. Round 2 then broke the naive fix: a
  GLOBAL ActiveCount watermark forces signals only on connections that POST, whose connection-local sweeps
  **cannot retire owners belonging to QUIET connections** — the table can fill with quiet-QP owners and
  drain-retry on the posting connection frees nothing (today's scheduled collector avoids this by scanning
  EVERY owner-bearing FIFO, `T:16309`/`T:16333` — exactly what D-S3 de-schedules).

  **DECIDED (owner, 2026-07-15): SIZE THE TABLES OUT OF THE PROBLEM — the P0-c move.** Two earlier fix shapes
  are RETIRED, and the reasoning is worth keeping: round 1's global-watermark term died in round 2
  (connection-local sweeps cannot free quiet connections' entries), and round 2's replacement ("option A":
  per-connection share terms + a checkpoint-only table reserve + a drain-all-owner-bearing-connections
  handler) was over-engineering born of treating the 256 capacity as fixed. The owner challenged that with
  P0-c's own precedent (*"sizing the pool out of the problem deleted the frontier, the WR-ID field, the
  backpressure plumbing, and that bug class"*, `R:1020-1026`) — and the bound is airtight:
  - An owner entry exists ONLY while its command/completion holds a per-session SOURCE slot: each table has
    **exactly one reserve caller** (`T:22711` after the credit gate `T:22637`; `T:20349` after the 16-ring
    reservation), and retirement releases the source credit in the same step. Lockstep, 1:1.
  - Ceiling: `64 sessions × 62` = **3968** command owners; `64 × 16` = **1024** completion owners. The WR-ID
    INDEX fields are **16 bits** (`R:300-301`, masks `0xffff`), so sizing command→4096, completion→1024 sits
    an order of magnitude under the encoding ceiling (existing asserts `R:345-350` continue to hold).
  - **Enforce the bound at compile time** (the P0-c pattern):
    `_Static_assert(COMMAND_OUTSTANDING_WRITES >= MAX_LOCAL_SESSIONS × (LOCAL_COMMAND_MAILBOX_SLOTS − 1))`
    and the completion analogue — so raising sessions or pool depths cannot silently reopen the hole.
  - Consequences: **reservation becomes INFALLIBLE** (P0-c language); the watermark term, the table reserve,
    and the multi-connection drain are all **deleted from the design**. The remaining pressure terms are the
    per-session/per-connection SOURCE-pool ones — every one connection-local, every one with a post-time
    drain trigger at its own gate.
  - **The lane-FIFO ARRAYS are deleted with the ordinal machinery (owner correction).** The sweep still
    needs a per-connection per-class POST-ORDER structure (pop-from-head-while-`mark <= F` is O(retired);
    with no ordering the sweep would scan the whole 4096-entry table on EVERY CQE — O(table) on the hot
    path). But the structure is NOT the current array-of-arrays (`LaneFifos[OUTSTANDING]` ×
    `outstandingIndexes[OUTSTANDING]`, quadratic — a naive 4096² would be 64 MiB): it is an **intrusive
    linked list threaded through the owner entries** — one `nextIndex` per entry + `{head, tail}` per
    connection per class. Linear memory, O(1) append at reserve, O(retired) pop at sweep. Scenario-E
    cancellation simplifies with it: clear the owner in place (`active=false`) and the sweep LAZILY SKIPS
    inactive entries when it reaches them — the O(n) shifting mid-queue removal (`T:6508-6531`) dies along
    with the ordinal search.

  **On "why would a CQE ever sit undrained?"** — under the de-scheduled design a parked CQE on a
  quiet connection is POSSIBLE by design (the post-clocked poll runs before that CQE arrives; guaranteeing
  prompt drains everywhere would just re-create the scheduled collector being deleted). The invariant is not
  "no parked CQEs"; it is **"everything a parked CQE defers is bounded and connection/session-local, with a
  drain trigger at the point of need"**: owner entries (sized out, above), the session's source credits (its
  own next publish hits the credit gate → connection-local drain → self-heals), the connection's WQE
  admission (next post WOULD_BLOCK → drain, exists), CQ capacity (outstanding ≤ admission ≤ SQ ≤ CQ). After
  the size-out, no cross-connection coupling remains.
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
