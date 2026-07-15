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

## Open decisions (settle during the stage, record here)
- **Stage 1 abort mechanism** — `OBJECT_ERROR` vs `CLOSE_SINK|ABORT_RESET` vs combination.
- **Stage 2 control retirement-by-frontier** — control has owners but no head; add a head, or retire control
  differently.

## Separate, NOT gating this work
- **The 235.4µs per-command control latency cause is UNATTRIBUTED** (C5's attribution was dead code). Measure at
  the gate/S6 to localize it; it is not what this coalescing fixes.

## Related
- Design: [`../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`](../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md)
- §29(b) accounting + the P0-i revert: [`resource_retirement_contract_audit.md`](./resource_retirement_contract_audit.md)
- The migration this sits inside (S6 follows): [`dpu_command_plane_migration_plan.md`](./dpu_command_plane_migration_plan.md)
