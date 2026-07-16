# Homer transport — CONTRACTS & INVARIANTS
<!-- kb-summary: Current high-signal ownership, identity, lifecycle, geometry, and teardown contracts for Homer transport. -->

> **This file is an INDEX, not a narrative.** Everything else in this directory tells a story. This one tells
> you **what a symbol MEANS, what it does NOT mean, and the rule it carries** — so you find out *before* you
> write the line, not after it costs you a day.
>
> **⛔ IT IS A POINTER AND A WARNING — NEVER A SUBSTITUTE FOR READING THE CODE.** A stale entry misdirects *with
> authority*: a lying diagnostic costs more than the silence it replaced. **Go read the code. Verify.**

## ⛔ EVERYTHING HERE IS TRUE **NOW**. NO HISTORY, NO CHANGELOG.

That promise makes this file a focused **starting map for source search**, never a replacement for it. **If a
contract changes, REWRITE the entry** — never add *"used to mean X"*. Two near-identical statements sitting
adjacent, one of them wrong, **is
the exact bug this file exists to prevent** (see the very first entry). History belongs in
[`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) and the commit.

*(A **tempting-wrong-move** warning and a **scoped-out** note are **not** history — they are current facts about
live traps, and they stay. So is a **COST** line: it is calibration — how hard to respect the rule — and it can
never be mistaken for a definition.)*

**Line numbers drift; symbol names do not.** Every entry names the **symbol** as its stable key and gives a line
as a *hint*. Grep the symbol.

**Maintenance is part of the change.** Adding a call site to an `ENFORCES` list belongs in the **same commit** as
the call site. If it is not on the list, **it has not been checked.**

**Repo:** `citus-dbcomm`. Paths below are relative to `src/backend/distributed/utils/homer/`.

---

# SYMBOL ENTRIES

## `HomerPayloadStreamState.byteRingBytes` / `maxRecordBytes` — negotiated geometry survives without local shm queues

- **MEANS:** the immutable byte-ring modulus and maximum record footprint negotiated when
  `HomerServiceCreatePayloadStreamEntry()` creates the stream (`tuple_sink_service_process.c:~26790`).
- **DOES NOT MEAN:** geometry of `sendQueue`, `receiveQueue`, `localReceiveByteRing`, `dpuMirrorRing`, or the
  tuple-source ring. Those are distinct storage/transport objects and the two POSIX shm queues are intentionally
  absent on a DPU service.
- **CONTRACT:** every control-path geometry check/publication that can execute on a DPU-served stream reads these
  stream scalars (using `HomerServiceStreamNegotiatedByteRingBytes()` for the modulus), never a queue mapping.
  Actual host-queue mapping validation may still inspect `TupleSinkServiceQueueMapping.byteRingBytes` because it
  is validating the mapped object rather than discovering negotiated geometry.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** strict peer/local/async reopen checks; tuple receive landing-ring
  sizing in `HomerServiceEnsureLocalReceiveByteRing()`; `HomerServiceFillPayloadStreamQueueDescriptor()`;
  `TupleSinkServiceCompleteStreamOpenAsyncOp()`; and `TupleSinkServiceStartServiceResultPeerOpen()`.

## `HomerServiceStreamNeedsLocalShmQueues()` — service-level local-backend test

- **MEANS:** this service has no active DPU DMA engine and therefore may have a local backend process polling
  `stream.sendQueue` / `stream.receiveQueue`.
- **DOES NOT MEAN:** `HomerServicePayloadStreamUsesDpuMirrorSource()`. That predicate also applies a per-stream
  tuple-view relay discriminator and is not the queue-allocation contract.
- **CONTRACT:** when `HomerServiceDpuDmaSchedulerEnabled(dpuDmaState) && dpuDmaState->engine != NULL`, neither
  POSIX shm queue is mapped for any newly created stream. Host services retain both mapping arms; basebackup still
  independently omits `receiveQueue`. Landing, tuple-source, and DMA-mirror rings are unaffected.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** byte-ring and fixed-slot `sendQueue` mapping arms and byte-ring and
  fixed-slot `receiveQueue` mapping arms in `HomerServiceCreatePayloadStreamEntry()`; informational descriptor
  publication in `HomerServiceFillPayloadStreamQueueDescriptor()`.
- **SCOPED LIMITATION:** unflagged tuple/COPY streams require a host service without an active DPU DMA engine. The
  §31 service-level decision intentionally does not preserve a mixed legacy-shm/DPU-service mode; runtime validation
  in §31 covers selected-DPU SQL results and four-role basebackup, not that unsupported combination.

## `descriptor->serviceSessionId` (`homer_service_dpu_dma.c`) — the host's COLD-SETUP DECLARATION

- **MEANS:** what the host **declared** about a ring when it exported the mmap, once, at cold setup.
- **DOES NOT MEAN:** the session **currently bound** to that ring. For the frontend agent's **shared arena it is
  `0` for EVERY ring** — the arena is exported once at postmaster start and its slots are handed to backends
  *later*, one at a time.
  ⚠ **Not to be confused with `ringRuntime[i].boundServiceSessionId`**, which is what the DPU has actually BOUND.
- **CONTRACT:** ⛔ **NEVER use it to answer "which session owns this ring."** There is exactly **ONE** way to ask:
  **`HomerDpuDmaRingBoundSessionId(import, ringIndex)`** (`homer_service_dpu_dma.c:757`). It returns `0` for an
  unbound ring; callers resolving *by session* never pass `0`.
- **ENFORCES:** `HomerDpuDmaSetupContainsServiceSession()` (`homer_service_dpu_dma.c:~10874`) and its three
  callers in `tuple_sink_service_process.c` (`:~42443` payload setup-drain, `:~42530` setup-close semantic
  drain, `:~42583` **selected-session release**).
- **COST:** violated at `homer_service_dpu_dma.c:10874`, against a rule stated **in its own file, 10,000 lines
  above**. The predicate silently answered *"not mine"* for every session ⇒ the close-finalizer `continue`d past
  all of them ⇒ **node A never released a single selected session**; its 64-slot table filled; it could no longer
  stage commands; node B spawned backends that received **zero** commands. Gate died at session 65. **~1 day.**
  Fixed citus `f12a96112`. Audit §44–§45.
- ⚠ **The function's doc comment said it mapped identities back to "*descriptor* service sessions."**
  **That phrasing is where the bug came from.** *A doc comment that names the wrong field is not a comment — it
  is an instruction to write the bug.*

## `completion.commandState` — **MANUFACTURED on the selected-DPU egress. DO NOT TRUST IT.**

- **MEANS:** the terminal state of a command, *as published to the peer*.
- **DOES NOT MEAN:** what the **backend** actually did. `HomerServiceDpuEgressOneSelectedCompletionEvent()`
  **overwrites an otherwise-COMPLETED completion's `commandState` to `FAILED`** when the **result relay's**
  peer-open failed (`dpuResultPeerOpenFailed`, `tuple_sink_service_process.c:~43873`) — **while the backend is
  still ALIVE**, and while `dpuBackendTeardownStarted` is still `false`.
  ⚠ **Not to be confused with `commandKind`**, which the conversion **never touches** (it writes only
  `commandState`, `resultFlags`, `detail`).
- **CONTRACT:** ⛔ **Never key a backend-lifecycle decision on `commandState`. KEY ON `commandKind`.**
  **RULE, GENERALIZED: key on the field that is never manufactured.**
- **ENFORCES:** the L1 terminal bookkeeping in `HomerServiceDpuEgressOneSelectedCompletionEvent()` keys on
  `commandKind == CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`.
- **COST (avoided):** keying on the synthetic `FAILED` would clear `backendLoopActive` with
  `dpuBackendTeardownStarted == false` ⇒ DPU command **landing** rejects the session
  (`tuple_sink_service_process.c:~43363`) ⇒ the client's `TX_ABORT`/`CLOSE` can **never land** ⇒ **permanent
  wedge**. Same shape as the §35 fence-and-return bug, nearly re-created. Audit §40.2.

## `sessionState->activeSinkCount` — **ONE DECREMENT POINT. GO THROUGH THE FUNNEL.**

- **MEANS:** how many payload sinks a service session still owns.
- **DOES NOT MEAN:** a number you may adjust locally. **It is not a plain refcount you can `--`.**
- **CONTRACT:** **`TupleSinkServiceMaybeReclaimSinkAndSession()`** (`tuple_sink_service_process.c:~27726`) is the
  **single decrement point.** It guards inactive streams, checks underflow, decrements **exactly once**, resets
  the stream, **and retires the session if that was its last sink.**
  ⛔ **Never call `HomerServiceResetPayloadStreamEntry()` directly on a path that owns a sink reference** — that
  frees the stream and leaves the count pinned.
- **⚠ WHY ONE MISS IS CATASTROPHIC:** **every** retirement path is gated on `activeSinkCount == 0` — the
  sink-release hook, the connection-reset retire, **and `AllocateSession`'s table-full reclaim**
  (`tuple_sink_service_process.c:~24273`). **One missed decrement makes ALL of them refuse** — and it presents
  not as a refcount bug but as *three independent retirement paths being mysteriously unreachable*.
- **⚠ THE REFERENCE IS TAKEN EARLIER THAN YOU THINK:** the selected-DPU result stream takes its `++` at **eager
  identity reservation** — *before* `SERVICE_RESULT_OPEN` installs the synthetic `sendHandleOpen`. **Any release
  helper must tolerate `sendHandleOpen == false`**, or it skips exactly the case that leaks (a result-open that
  never bound).
- **ENFORCES:** `TupleSinkServiceReleaseServiceOwnedDpuResultSinkAfterBackendTeardown()` (called from **T4** and
  from **`HomerServiceDpuAbandonArenaTeardown`**); the `peerResetAfterCleanClose` branch
  (`tuple_sink_service_process.c:~30098`).
- **COST:** the clean-close branch used to free the stream and `return`, bypassing the funnel ⇒ node-B sessions
  never retired; the 64-slot session table filled. Fixed citus `494751b7b`. Audit §39.1, §43.
- ⚠ **The comment above that branch carefully listed three functions it was "deliberately NOT calling", each
  with a reason — and never noticed it was also skipping the sink accounting.**
  ***A carefully-reasoned exclusion list is silent about the thing it forgot.***

## `sessionState->backendLoopActive` — **also a SCHEDULER input, not just a backend flag**

- **MEANS:** this session still owns a live socketless backend.
- **DOES NOT MEAN:** merely "a command is in flight". It is a **disjunct of the session-machine gate**
  (`tuple_sink_service_process.c:~45717`), so a **stale `true` mints a `COMMAND_SESSION`/`WAIT_BACKEND`
  scheduler machine FOREVER**, for a backend that exited long ago.
- **CONTRACT — when you may clear it** (the host arm's predicate, `tuple_sink_service_process.c:~21503`): only
  when the command is **terminal** *and* it is `CLIENT_SQL_SESSION_CLOSE`, **or** `FAILED`, **or**
  `TX_COMMIT`/`TX_ABORT` **for a non-`CLIENT_SQL_SESSION` opKind**. Its own comment: *"For persistent client SQL
  sessions, COMMIT/ABORT are no longer terminal."*
  ⛔ **NEVER clear it on every terminal completion.** The gate runs **~14,000 commands on ONE session**; DPU
  landing rejects a non-teardown session whose `backendLoopActive` is false (`:~43363`) ⇒ **the session would
  stop admitting commands at statement #2.**
- **⚠ SCOPED OUT — DELIBERATELY NOT DONE (a CURRENT fact, not history):** the selected-DPU **FAILED arm is not
  implemented.** A genuine backend `FAILED` collides — no-backend cleanup (`:~24618`) vs teardown-fenced landing
  (`:~43378`): **two owners for one mailbox record**, resolved by scheduler order. **Anyone "completing" this
  naively re-creates that race.** It needs a lifecycle design, not a flag write. Audit §40.2.
- **ENFORCES:** L1 in `HomerServiceDpuEgressOneSelectedCompletionEvent()` (CLOSE only); the host arm at `:~21503`.
- **COST:** 63 zombie `WAIT_BACKEND` machines filled the 64-slot machine candidate set and **evicted the one live
  session's own machine** every pass, forever. Fixed citus `9ef06f224`. Audit §41.

## `dpuDmaState->selectedSessions[]` / `selectedSessionCount` — **BOTH DPUs have one**

- **MEANS:** the DPU-side record of a client SQL session selected onto the DPU command plane. **Capacity 64.**
- **DOES NOT MEAN:** a node-B-only structure. **Node A (client-side DPU) allocates these too** — it is where
  commands are staged before being routed to node B.
- **CONTRACT:** released by `HomerServiceDpuResetSelectedSessionForClose()`, reached from **two** owners:
  **node B's T4 arena teardown** *and* **the setup-close finalizer**
  (`HomerServiceDpuResetSelectedSessionsForClosingSetup`, registered **without a node-role condition** —
  **node A's only release path**).
- **⚠ IF NODE A STOPS RELEASING, THE SYMPTOM APPEARS ON NODE B:** node A's table fills ⇒ it cannot stage the
  client's command ⇒ **node B spawns a backend and then lands ZERO commands.** You will debug node B and find
  nothing. See the cross-cutting entry *"Which DPU log?"* below.
- **COST:** the setup-close finalizer was defeated by the `descriptor->serviceSessionId` bug (first entry). Node
  A leaked **100%** of its selected sessions. Audit §44–§45.

## `HOMER_SERVICE_LOG(...)` and the stats macros — **COMPILED OUT of a perf build**

- **MEANS:** verbose development logging. `HOMER_SERVICE_VERBOSE_LOGGING` **defaults to 0**.
- **DOES NOT MEAN:** anything you can rely on in a measured run. Same for
  `TupleSinkServiceLogPeerTransportStatsRdma` (`#if HOMER_SERVICE_PEER_TRANSPORT_STATS`).
- **CONTRACT:** ⛔ **Never anchor a validation proof on a `HOMER_SERVICE_LOG` string.** Use a plain
  `fprintf(stderr, ...)` for anything a *proof* depends on. (Operational hazards §9.)

---

## `signalCommandWrite` / `TupleSinkServiceClientSqlCommandWriteShouldSignal` — **TODAY signals every command write. That is the PRE-COALESCING state, not an eternal rule.**

- **MEANS (today):** `ShouldSignal` returns `true` **unconditionally** (`tuple_sink_service_process.c:7110`), so the
  terminal WR of every client-SQL command post is `IBV_SEND_SIGNALED` + WR-id-tagged and its send CQE retires that
  command's WQEs and (via the reap `TupleSinkServiceRetireClientSqlCommandWriteCompletionEntry`) advances
  `retiredEpoch`/`consumedEpoch`. It signals every write only because **send-CQE coalescing is not yet applied to
  this lane** — NOT because the lane is forbidden from amortizing.
- **THE REAL INVARIANT (this is the violable rule):** ⛔ **no unsignalled command WR may be left without a signalled
  successor.** Signalling every write satisfies it trivially; **coalescing satisfies it too** — a signalled checkpoint
  every `≤ ½·SQ` per-QP retires the unsignalled prefix behind it. That IS the send-CQE coalescing project
  ([`../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`](../../../future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md)),
  and the command lane is **explicitly in scope** (it lists command / peer-client-completion / control /
  peer-command-completion as the four lanes multiplexed onto the shared SQ). Coalescing is SAFE for this lane
  because its drained pool is the 64-slot command mailbox (`CITUS_REMOTE_EXEC_LOCAL_COMMAND_MAILBOX_SLOTS`), so a
  ≤32-deep checkpoint interval never starves source credit — **provided** it also carries a **terminal flush**
  (force-signal the last write at close/quiesce), exactly as the PAYLOAD lane already does.
- **DOES NOT MEAN:** that the command lane must forever signal every write (it is the coalescing project's explicit
  target), nor that inlining precludes signalling — `IBV_SEND_INLINE` (source in WQE) and `IBV_SEND_SIGNALED` (CQE)
  are **orthogonal**; a coalesced lane can inline AND signal-periodically.
- **THE TRAP (learned the hard way):** the deleted `useInlineCommandPost` fast path made BOTH command WRs
  **unsignalled with no successor guarantee at all** (no ≤½ checkpoint, no terminal flush), so on a compact-only
  stream or at quiesce the tail never retired → SQ leak / `exit(1)`. DELETED 2026-07-14 (audit §32); the surviving
  `TupleSinkServicePostRemoteClientSqlCommandRecord` posts an unsignalled body + a **SIGNALLED** readySeq tail, and
  the §29(b) admission reserve (`remote_execution_peer_transport_rdma.c:10898`) guarantees that signalled tail is
  always postable after its unsignalled body. ⚠ **When coalescing lands here it MAY re-introduce unsignalled command
  writes — but ONLY with the reserve + ≤½-pool checkpoint + terminal flush that guarantee the successor.**
  Unsignalled-*without*-successor is the bug; unsignalled-*with-guaranteed*-successor is the goal.

---

# CONTROL-FLOW / LIFECYCLE CONTRACTS

## T4 (arena teardown) — releases the backend, **RETAINS the service session**

- **THE ORDER:** drain the arena slot → `TupleSinkServiceReleaseDpuArenaBinding()` →
  `HomerServiceDpuResetSelectedSessionForClose()` → set `teardownCompletedCleanly` → **release the service-owned
  result sink** → *(session retires later, via the funnel, on the peer disconnect)*.
- **WHAT BREAKS IF REORDERED:** releasing the synthetic SEND owner **at OPEN** instead of at T4 would let an
  ordinary semantic close reach the reclaim funnel **before** teardown — **reclaiming a stream the DPU byte-ring
  mirror may still be writing into.**
- **⚠ THE TEMPTING WRONG MOVE — ALREADY TRIED AND REJECTED (twice), see the commented-out lines:**
  `TupleSinkServiceResetSession(serviceSession)` **at T4**. The peer/session lifetime can legitimately outlive
  its DPU backend; `ResetSession`'s close guards must stay with the lifecycle owner. **Do not re-propose it
  without addressing why it was rejected.**
- **⚠ `ResetSession`'s GUARD IS AT THE TOP — keep it there; add no mutation above it.**
  `TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister` is the FIRST thing after the NULL check
  (`tuple_sink_service_process.c:~25040`), *before* the prologue that disables `dpuPeerCommandLandingActive`,
  retires send completions, and clears scheduler state. On refusal it returns `TUPLE_SINK_SESSION_RESET_DEFERRED`
  and **mutates nothing** (PURE DEFER — in particular does NOT set `dpuBackendTeardownStarted`); on pass it resets
  fully and returns `COMPLETED`. It USED to sit *after* those mutations and `exit(1)` on refusal — a guard placed
  after the mutations it guards is not a guard (it can only choose how to die), and disabling landing first would
  wedge the session; the `exit(1)` killed the teardown before `HomerDpuDmaDestroy` drained. **Any new mutation
  added to this function MUST go below the guard.** (Audit §36 PART 2 / §36.10; §48a.)
- **`TupleSinkServiceResetSession` RETURNS `TupleSinkServiceSessionResetOutcome`, not `void`** — `DEFERRED` means
  "left active/bound; lifecycle owns the reset." **ENFORCES — a caller that acts on the reset having happened MUST
  check for `COMPLETED`:** `TupleSinkServiceAllocateSession` (`:~25194`) **continues scanning** on non-`COMPLETED`
  (never hands out a deferred slot); `TupleSinkServiceHandleCloseSession` (`:~41762`) keeps its SUCCESS response on
  `DEFERRED` (does NOT convert to ERROR — §36.2); `main()`'s shutdown sweep and every other caller may ignore it.

## `streamEntry->stream.peerResetAfterCleanClose` — **ONCE CLEAN, ALWAYS CLEAN**

- **MEANS:** the owner asked for a **clean close**, and stamped that decision **onto the stream itself**.
- **DOES NOT MEAN:** anything about whether the owning *session* still exists.
  ⚠ **`sessionState == NULL` IS NOT EVIDENCE OF FAILURE.** On **node A** the owner session is routinely **gone**
  by the time the reset runs — the client's `CLOSE_SESSION` destroyed it milliseconds earlier. **A missing owner
  is the EXPECTED state here.** The flag lives on the stream *precisely so it survives the owner's departure*.
- **CONTRACT:** **cleanliness is decided the moment the owner asks to close. A later lookup that cannot find the
  session must NOT un-decide it.** ⛔ **Never publish a peer FAILURE on a stream whose `peerResetAfterCleanClose`
  is set** — check it **before** any orphan/failure branch, not after.
- **ENFORCES — every site that resolves an owner and may publish a failure:**
  - `HomerServiceMarkPayloadStreamAbortingOnConnectionReset()` (`tuple_sink_service_process.c:~29309`) — the
    **marking** pass. *Already obeyed it; the rule is stated here.*
  - `HomerServiceClearAbortedPayloadStreamAfterReset()` (`tuple_sink_service_process.c:~30048`) — the
    **clearing** pass. **VIOLATED IT until citus `<this commit>`.**
  - *(A new site that resolves an owning session and can publish a failure belongs on this list.)*
- **COST:** the clearing pass took the orphan-**failure** branch on `sessionState == NULL` *before* consulting the
  flag ⇒ **every clean close on node A was published as a REAL PEER FAILURE, on every successful run** (64× in a
  75-session run: `abort payload clear could not find owning session` + `reason=peer-reset-orphan`).
  **A lying diagnostic, on the happy path, at exactly the place a future debugger looks first.**
- ⚠ **THE SHAPE, FOR THE THIRD TIME THIS WEEK:** *the rule was written down, enforced at ONE site, and violated at
  the other.* Same as Wall 5 (`descriptor->serviceSessionId`). **This entry's `ENFORCES` list is the thing that
  catches it — nothing else does.**

## `streamEntry->stream.localSenderHeadMirror` — **SCENARIO C. Retire it on EVERY teardown route, or reset FAIL-STOPS.**

- **MEANS:** an 8-byte heap counter **we own**, registered as an RDMA MR so the **remote CONSUMER** can WRITE its
  consumed-head into it. We publish its `remoteRkey` + address in the peer OPEN request. **We can never see the
  peer's send CQEs** — so deregistering it needs a *proof* the peer will not write again.
- **DOES NOT MEAN:** the peer's head mirror.
  ⚠ **Not to be confused with `peerSenderHeadMirror`**, which is the *sender's* descriptor as held by a
  **receiver** — not ours to free. A **receiver never registers a `localSenderHeadMirror` at all**: the only
  registration site is `TupleSinkServiceEnsureLocalSenderHeadMirror()`, reached ONLY from the **outgoing** async
  open's `STREAM_REGISTER_MIRROR` phase. *(MEASURED: node A, a pure receiver, reports `registered=0` across 75 gate
  sessions and a 23 GB basebackup.)*
- **CONTRACT:** **the ownership test is "do I hold a registered handle?" — `memoryRegionHandle != NULL`. It is NOT
  `peerBindingLocallyInitiated`.** That bit is set at the peer-open **BIND**, which happens strictly **LATER** than
  the registration it appears to guard, so between those points the mirror is live while every "am I the sender?"
  bit still reads **false**.
  **Every route that tears a stream down must retire the mirror BEFORE
  `HomerServiceResetPayloadStreamEntry()`**, which MEMSETs the descriptor and **fail-stops** on a live handle.
- **ENFORCES — every teardown route, each with its own NAMED reason and counter:**
  - `HomerServiceClearPayloadStreamPeerBinding()` (`tuple_sink_service_process.c:~27485`) —
    `BINDING_CLEAR`. **The only PROOF-BACKED one**: it `exit(1)`s unless `HomerServicePayloadCloseCanClearBinding()`
    holds (RECLAIMABLE ⇒ the sender *observed* the peer's final head WRITE, and RDMA WRITEs complete **in order on a
    QP**, so every earlier head-ACK already landed; or reconciled ABORTING ⇒ the QP/CQs are destroyed).
  - `TupleSinkServiceMaybeReclaimSinkAndSession()` (`tuple_sink_service_process.c:~28068`) — `OPEN_FAILED`.
    Proof-free, but the mirror was **never bound**, so it was never peer-writable.
  - the shutdown stream loop in `main()` (`tuple_sink_service_process.c:~49960`) — `SERVICE_EXIT`. Proof-free; safe
    **only** because the peer transport (and every QP/MR) is destroyed a few statements later, so a poisoned QP has
    **no next user**.
  - `HomerServiceResetPayloadStreamEntry()` (`tuple_sink_service_process.c:~26050`) — the **FAIL-STOP TRIPWIRE**.
  - ⛔ **A NEW TEARDOWN ROUTE BELONGS ON THIS LIST. If the tripwire fires, ADD A RETIREMENT — do not relax it.**
- **⚠ THE TEMPTING WRONG MOVE — *DEFERRING* the dereg.** A comment used to say: *"a receiver may still have an older
  consumed-head ACK WR in flight after the sender reclaimed the stream; keep the mirror valid until the peer
  connection resets, or a late WRITE fails `IBV_WC_REM_ACCESS_ERR` and poisons the QP."* **The hazard was real —
  someone got a poisoned QP.** But the "deferral" was **never implemented**: the flag was set in one place, read in
  two (both merely *skipping* the cleanup), cleared **nowhere**, and then `memset` erased the only pointers. **It
  was not a deferred cleanup, it was an ABANDONED one**, leaking one MR + 8 bytes **per session**. It is excluded
  today by the `BINDING_CLEAR` proof above.
- **COST / EVIDENCE:** the tripwire has caught **two** real bugs, and both looked like "no error" beforehand:
  1. **mid-transfer SIGTERM** — shutdown resets `active` streams *before* destroying the transport, so a stream open
     at SIGTERM hit reset with a live mirror ⇒ `exit(1)`, **teardown never reached `HomerDpuDmaDestroy`**.
  2. **the byte-ring-pool refusal at `-c > 8`** — the documented capacity failure. The refused peer-open landed in
     the reclaim funnel with a registered-but-unbound mirror ⇒ **`-c 10` KILLED the DPU service** instead of
     partially failing. Before the tripwire this had been a **silent MR leak per refused session**.
- **HOW TO PROVE IT, don't assume it:** the service prints, at teardown,
  `head-mirror MR accounting: registered=N released_on_rebind=N retired_on_clear=N retired_at_exit=N
  retired_on_open_failure=N live=N`. **`live` MUST be 0.**
  ⚠ **The tripwire's SILENCE is a ZERO COUNT and proves nothing on its own** — it cannot distinguish *"every mirror
  was retired"* from *"none was ever registered, so the test was vacuous."* Only `registered` vs `retired` can.
  ⚠ **`registered == retired` is the WRONG invariant**: `TupleSinkServiceEnsureLocalSenderHeadMirror()` can
  dereg-and-**RE-register** in place when the peer connection changed (`released_on_rebind`). That is a
  *replacement*, not a retirement; folding it in manufactures a phantom leak on every reconnect.

## The clean-close payload reclaim — **funnel, then check you were not declined**

- **THE ORDER:** clear the peer binding → `TupleSinkServiceMaybeReclaimSinkAndSession()` → **verify the stream is
  no longer `active`**.
- **WHAT BREAKS IF SKIPPED:** the funnel has **three** early-return guards (local handles, peer binding, deferred
  failure). If it **declines**, the stream is never reset and you have swapped a sink leak for a **stream-table
  leak, silently.** The code now ALARMs and **names which guard held** — *a single shared message across three
  conditions is a decoy, not a diagnostic.*

---

# CROSS-CUTTING *(no single symbol owns these)*

## Q: "It failed at 64 — which resource ran out?" — **WRONG QUESTION. SEVEN of them are 64.**

| resource | where |
|---|---|
| node-B **service session table** | `homer_abi_version.h:50` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS` |
| **selected-DPU session table** ⚠ *(BOTH nodes)* | `tuple_sink_service_process.c:~42953` |
| scheduler **machine candidate set** | `HOMER_PROGRESS_PLAN_MAX_GRANTS`, `tuple_sink_service_process.c:2551` |
| scheduler **flat ready set** | same constant |
| **payload stream table** | `homer_abi_version.h:51` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` |
| peer transport **outgoing** connections | `remote_execution_peer_transport_rdma.c:70` |
| peer transport **incoming** connections | `remote_execution_peer_transport_rdma.c:71` |

- **⛔ THE TRAP: ONE EXHAUSTION MASKS THE NEXT.** Node B's session table and node A's selected-session table both
  cap at 64; node B's cliff fired at session **64**, so **in the entire life of this code nobody ever reached 65
  to discover node A's table was full too.** Fix one and the next steps straight into its place — **and looks
  exactly like a regression your fix just caused.** It was there all along.
- **TELL THEM APART BY HOW THEY FAIL:** session table → `"peer service ran out of free command sessions"`
  *(a peer **response** — **never logged on the DPU**; grep the **client**)*. Machine candidate set → a **30 s
  command timeout**. Selected-session table → `"selected-DPU session table is full"` **on node A**.
- **NOT 64:** DPU byte-ring pool = **8** (`homer_service_dpu_dma.h:41`); frontend arena = **16** slots; import
  table = **1024**.

## Q: "The client failed. Which DPU log do I read?" — **BOTH. ALWAYS.**

- **Node A (farnet0 DPU) DRIVES the command plane; node B (farnet1 DPU) EXECUTES it.**
  **A failure OBSERVED on one node is routinely CAUSED on the other.**
- **COST:** node B spawned a backend and landed zero commands. I read node B's log, found nothing, and built an
  entire suspect list out of node-B code — **every suspect wrong, because the bug was not on node B at all.**
  **Node A's log was 9.4 MILLION lines**, 9,416,076 of them `selected-DPU session table is full`. **Nobody had
  ever opened it.**
- **HOW:** node A's log is on **farnet0's** DPU — `ssh farnet0` → `ssh dpu`. **Ship a script, never a double-hop
  one-liner**, and **grep it ON the DPU**: a 9.4-million-line `cat` across a double hop simply times out.

## Q: "Nothing is in the log, so nothing went wrong?" — **NO. THESE FAIL SILENTLY.**

- **`HomerProgressMachineCandidateSet` has SEVEN overflow counters — all were WRITE-ONLY.** The *ready set* had a
  report function; **the machine candidate set had none**, so the drop that actually killed the run emitted
  **nothing**, while its noisy neighbour printed an overflow that produced an entire *wrong* root-cause chain.
  ⇒ now `HomerServiceReportMachineCandidateOverflow()` — **per PHASE**, with a **kept-kind histogram**.
- **DOWNSTREAM POLICY ADMISSION is a tighter, also-silent boundary:** **16 grants / 12 machines / 2 PAYLOAD**
  (`tuple_sink_service_process.c:182`). ⇒ now `HomerServiceReportPolicyAdmissionDrops()`.
  ⚠ The code names this failure itself: *"**ARMING A CANDIDATE IS NOT ENOUGH** … armed on every pass and granted
  on none … **Nothing warns you** … **the service looks idle**."*
- **RULE: NO SILENT CAPS.** A bounded set that drops work must say **what it dropped — and what it KEPT.**
  *The victim is an accident of ordering; the occupants are the bug.*

---

## `TupleSinkServiceDrainTaggedSendCompletionsInternal(callbacks=...)` — **NO CQ CONSUMER MAY POLL WITHOUT OWNER DISPATCH. The choke point enforces it.**

- **MEANS:** the single funnel through which every nonblocking send-CQ drain runs. Since D-S0 it **REFUSES
  (`callbacks == NULL` → once-latched ALARM + false) before the first `ibv_poll_cq`** — dequeuing a CQE is
  destructive (it is the owner's ONLY retirement event, F7/§25), so a blind poll is never a degraded mode,
  it is a lost resource.
- **DOES NOT MEAN** a per-caller convention. It used to be one, and two callers passed NULL (the
  control-response exhaustion drain, the peer-pump SEND_CQ phase) while the blocking waiter EXEMPTED
  `CRITICAL_CONTROL` — an exemption written before the command-plane migration put typed lanes on that QP.
  All three are gone; do not reintroduce a caller-side "we know this CQ is empty of typed CQEs" argument —
  that is exactly the assumption that rotted.
- **CONTRACT — `HomerServicePersistentSendCompletionCallbacks` is RETIRE-ONLY** (`tuple_sink_service_process.c`,
  comment at the struct): these callbacks now run from mid-operation transport drains NOT bracketed by
  `HomerServiceSendCqDrainDepth`. A callback that posts a WR or polls a CQ requires extending that guard to
  every dispatch site FIRST.
- **ENFORCES:** the guard in `...DrainTaggedSendCompletionsInternal` (transport); the wrapper
  `TupleSinkServiceDrainTaggedSendCompletions` and the pump SEND_CQ phase both pass
  `transportState->sendCompletionCallbacks/-Context`; registration happens once in service init
  (`TupleSinkServiceRegisterPeerSendCompletionCallbacksRdma`) before any peer connection exists.

## `connectionState->admittedSendWr` — **the CLAMPED admission ceiling. NOT the provider grant.**

- **MEANS:** `min(provider-granted cap.max_send_wr, CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH)`, stored at QP
  creation. Admission (`TupleSinkServiceAdmitSendChainRdma`) bounds outstanding WRs by THIS, which is what
  keeps the 256-entry `signalledFrontierMark[]` table (sized by the REQUESTED depth) safe from overwrite.
- **DOES NOT MEAN** what the provider granted. The raw grant may be larger; it is visible in the
  once-per-connection `peer QP caps` log line (and the payload staging-pool setup check deliberately still
  tests the RAW grant — stricter, never weaker). The field was **renamed from `grantedSendWr`** precisely so
  the name cannot re-imply the raw grant; if you need the raw number, read the log, not this field.
- **ENFORCES:** the clamp at QP creation (`TupleSinkServiceInitConnectionResources`); every admission /
  diagnostic reader of `admittedSendWr`; mark-table safety follows from this clamp alone (NOT from any
  CQ-size equality — the granted CQ depth may also exceed the request and is only logged).

## `entry->frontierMark` (command-write / completion-publish owners) — **QP-wide retirement coordinate. Class-local ordinals are GONE.**

- **MEANS:** the connection's cumulative `postedSendWrs` as of the owner's unit's LAST (signalled) post,
  stamped at the post site immediately after the unit's final post succeeds
  (`TupleSinkServicePeerConnectionPostedSendWrsRdma`). A later successful CQE whose retired frontier `F`
  reaches the mark proves this WR — and everything posted before it on the same QP — completed; the sweep
  pops per-connection post-order lanes while `mark <= F`, so retirement is CROSS-CLASS (a payload or
  control CQE retires command/completion owners on that QP for free).
- **DOES NOT MEAN** a per-class sequence number (`postOrdinal` and the array lane FIFOs are deleted), and
  `0` is NOT a mark: it flags "reserved, unit not yet posted" — the validators' orphan discriminator.
- **ENFORCES:** stamp+link ONLY after the unit's posts all succeed (command post fn, completion publish fn
  in `tuple_sink_service_process.c`); `TupleSinkServiceAccountPostedSendChainRdma` increments
  `postedSendWrs` BEFORE stamping the mark table, which is what makes the service-side stamp equal the
  signalled WR's own mark; comparisons assume the u64 frontier never wraps.

## `entry->linked` vs `entry->active` — **a wrong-neighbor pair. List membership vs accounting liveness.**

- **`linked` MEANS:** threaded on its connection's post-order sweep lane (`nextIndex` is LIVE list state).
  Reserve MUST skip `active || linked` slots — re-reserving a linked slot threads one table slot into a
  lane twice and corrupts the intrusive list. Only the sweep pop (or a stale-generation purge) unlinks.
- **`active` MEANS:** the entry holds owner accounting. Scenario-E abandonment clears `active` IN PLACE and
  KEEPS `linked` (+ the signalled demand, next entry): the WR's CQE is still in flight and the sweep lazily
  discards the entry when it arrives. `active && !linked && frontierMark != 0` = a posted-but-never-linked
  ORPHAN (link failure), released directly by the CQE validator.
- **ENFORCES:** both reserve probe loops; `Clear...Index` (linked guard: deactivate-in-place + ALARM, never
  memset); `Abandon...OwnerIndex`; the sweep pop unlink-before-retire order (the retire memsets).

## The signalled-demand counts (`...SignaledCompletionActiveCount`) — **released at CQE CONSUMPTION, never at abandonment.**

- **MEANS:** "signalled send CQEs not yet consumed on some lane" — the CQ-drain scheduler's demand gates
  (`...SendCqShouldDrain`, the demand scan, scheduler feedback) key off these.
- **DOES NOT MEAN** "active signalled owners": an abandoned (Scenario-E) owner is inactive but its CQE is
  still in flight and still needs draining — that drain is the ONLY thing that frees the linked slot, so
  releasing the demand at abandonment starves the very consumption that reclaims capacity (codex round 2).
  The per-lane `...SweepLaneHasSignaledOwner` predicates are deliberately NOT gated on `active`.
- **ENFORCES:** release points are exactly: `Clear...Index` UNLINKED path (post-failure, or retire after a
  sweep pop of an ACTIVE entry), the command retire's inline decrement, the sweep-pop/purge INACTIVE arms
  (via `TupleSinkServiceRelease...SignaledDemand`), and unlinked Abandon. The Abandon/Clear LINKED arms
  deliberately do not touch it.

## The sweep lanes' `connectionGeneration` — **dead-QP reclamation has THREE triggers, and purge RETIRES.**

- **MEANS:** the connection generation the lane's owners were posted under. A lookup that finds the
  handle's CURRENT generation differs proves the QP+CQs were destroyed (reset memsets the connection AFTER
  destroying them; re-establish assigns a fresh monotone generation) — no CQE for these owners can ever
  arrive, so the lane is purged.
- **Purge RETIRES active entries** (per-entry retire: session credits/epochs/outstanding counts advance) —
  QP destruction is the P0 proof no WR still reads their sources, and a blind memset would strand a
  sender session's bookkeeping when it outlives the shared CRITICAL_CONTROL connection (reset-complete
  marks only `clientSqlPeerReceiver` sessions). Abandoned entries release their retained demand and free.
- **ENFORCES — all three trigger sites, or a full table blocks the purge that would empty it:** the
  generation check in `Find...SweepLane`; `PurgeStale...SweepLanes()` on reservation table-full (one
  retry); the demand scan's per-lane generation check (purge instead of draining a dead/reused
  connection's CQ).

## `sendCheckpointSweep` / `TupleSinkServiceHandleTaggedSendCompletion` — **every successful CQE on an accounted QP retires and sweeps. NO raw consumers.**

- **MEANS:** the scalar retire → control-FIFO sweep → `sendCheckpointSweep` sequence runs inside the
  dispatch's SUCCESS block for EVERY class including UNTAGGED; the class-typed callbacks afterwards are
  VALIDATORS (their contract comment in `remote_execution_peer_transport_rdma.h`), releasing only the
  orphan/skew anomalies, best-effort.
- **DOES NOT MEAN** the bootstrap poll is exempt: `TupleSinkServicePollBootstrapSendCompletion` consumes an
  accounted-signalled CQE and therefore RETIRES (validated against the stashed
  `bootstrapSendWorkRequestId`). Before that fix the mark pairing was shifted by one for the QP's life and
  the sweep would strand the LAST owner of every burst — `SESSION_CLOSE`'s included (D-S2.3; the reason the
  bootstrap fix moved from 2b into 2a).
- **ENFORCES:** the success block in `HandleTaggedSendCompletion`; both drain paths reject errored CQEs
  before dispatch; the D-S0 choke point guarantees no NULL-callback consumer; the send-CQ poll inventory is
  exactly three (bootstrap poll, blocking waiter, drain loop) — a NEW `ibv_poll_cq` on a send CQ must
  either dispatch through `HandleTaggedSendCompletion` or retire explicitly, else owners leak and the
  validators ALARM.

## `connectionState->controlSendOwners[]` — **the control class's post-order sweep FIFO. Appended ONLY by `TupleSinkServicePublishControlMessage`.**

- **MEANS:** transport-internal ring of in-flight signalled CONTROL sends `{responsePublish, slotIndex,
  slotGeneration(0 for responses), frontierMark}`, swept by `TupleSinkServiceSweepControlSendOwners` on
  every successful CQE; release runs the decode-free core `TupleSinkServiceReleaseControlSendOwner`
  (old per-CQE retire semantics preserved: response slot `inUse=false`; request `sendCompletionRetired` +
  conditional op release).
- **DOES NOT MEAN** a service-visible structure, and the CONTROL dispatch arm does NOT release: it
  validates (entry gone after its own CQE's sweep) with a slot-state fallback whose no-double-release proof
  is TEMPORAL (sweep-before-validator within one dispatch), not identity-based — response WR-IDs all carry
  generation 0.
- **ENFORCES:** the append after the post in `PublishControlMessage` (all three control encode sites funnel
  there); the connection-reset memset zeroes it with the accounting; depth = op+response slot counts, so
  overflow is structurally impossible (ALARM + validator fallback if it ever fires).

---

## `HOMER_PAYLOAD_CLOSE_FLAG_ABORT_RESET` — **staged-discard terminal flush on a SURVIVING connection. NOT the ABORTING failure machinery.**

- **MEANS:** the sender is closing WITHOUT a drain contract (backend ERROR/cancel, cleanup without
  OBJECT_END). Receiver: stop relay submissions, wait out the in-flight DOCA chain, publish the **FAILED**
  credit-line terminal at the stopped frontier, respond with the ABORT_RESET echo (`peerBindingStillActive
  =0`), tombstone `ABORTED_RESET`. Exactly one of {`NORMAL_DRAIN`, `ABORT_RESET`} per request.
- **DOES NOT MEAN** `QUIESCED` (drained-to-final-tail + final head posted — an abort can never claim it;
  the response shapes are mutually exclusive and separately latched). Does NOT mean
  `HOMER_PAYLOAD_CLOSE_ABORTING` / `HomerPayloadFailureState` — that is the CONNECTION-RESET machinery,
  whose clear-proof requires QP destruction; ABORT_RESET completes with the QP alive.
- **ONLY `BASE_BACKUP_STREAM`**: the tuple-view relay has no discard implementation (its terminal is
  gated on in-band EOS); the accept arm rejects other families loudly rather than wedge unscheduled.
- **ENFORCES:** accept arm + family gate in `TupleSinkServiceHandlePeerCloseSinkRequest`; the exact-shape
  tombstone arm in `HomerServiceTryBuildRetiredCloseResponse` (`closeFlags == ABORT_RESET`, because the
  tombstone check runs BEFORE the live flag-shape validation); the abort echo arm in
  `HomerServiceValidatePayloadCloseResponse`; the abort fact-arms in
  `HomerServiceMarkPayloadCloseReclaimable` (exit(1) on unproven facts — never satisfy them by faking
  `peerQuiesced`/`finalHeadObserved`).

## `abortLocalSendDiscard` vs `abortResetDiscardActive` — **a wrong-neighbor pair. Sender pump gate vs receiver relay gate.**

- **`abortLocalSendDiscard` MEANS (SENDER):** the local client abort-closed; the outgoing payload pump
  (`HomerServicePumpOutgoingPayloadStream` — verified the SINGLE posting funnel) posts nothing further, so
  the posted tail is FROZEN. **ORDER IS LOAD-BEARING:** it must be set BEFORE the close continuation
  freezes the final tail (`HomerServiceFreezePayloadCloseFinalTail` exit(1)s on a moved tail);
  `TupleSinkServiceBeginPayloadAbortReset` owns that order — always go through it.
- **`abortResetDiscardActive` MEANS (RECEIVER):** the peer's CLOSE_SINK carried ABORT_RESET; the relay
  pump submits no new DOCA segments and publishes FAILED once the in-flight chain retires. Landed bytes
  beyond the stopped frontier are deliberately discarded, so `writtenTail < publishedTail` can hold
  FOREVER — every drain-shaped predicate needs an abort arm (see the scheduler-trap entry below).
- **DOES NOT MEAN** each other's side, and neither means `closeState->abortReset` (the close MODE flag,
  set on both sides). Both are latches; the stream-entry reset clears them.

## The abort scheduler rule — **a terminal path must be walked through ready-predicate → grant → executor, not just its own state machine**

- Three predicates assumed drain progress an abort explicitly abandons, and each was a silent
  forever-wedge: `HomerServicePayloadCloseActionReady` (receive-drain gate; abort arm returns
  `relayTerminalPublished`), `HomerServicePayloadMustProgressBeforeClose` (early-false under either abort
  flag), and the relay pump wake-up (graceful rides the final payload doorbell; the abort accept arm
  latches a SYNTHETIC `receivePayloadDoorbellPending`).
- **ENFORCES:** any future terminal/close variant (Stage 2's checkpoint flush included) must audit these
  three sites plus `HomerServicePayloadStreamSourceReadyForScheduler`.

## The SENDER failure channel — **`byteRingFailedCreditArmed/Published` + the credit line's `entryState`. The flags channel is DEAD for selected-DPU.**

- **MEANS:** a selected-DPU SENDER learns of stream death ONLY through its credit line: the DPU arms a
  one-shot FAILED credit terminal (`HomerDpuDmaArmFailedCreditForServiceSink`, ring resolved by the
  RUNTIME binding — D5) on abort/reset teardown; the consumed-head publish sweep publishes it (equal or
  ZERO frontier allowed — the stuck/never-credited abort has no new credit); the host's
  `StillOpenForReserve` reads `entryState==FAILED` off the line `HomerClientDpuRefreshBaseBackupCredit`
  already polls every reserve iteration.
- **DOES NOT MEAN** the `byteRingControl->flags`/`terminalStatus` channel: that one is HOST-SERVICE-era —
  `MarkPayloadStreamSendFailed` writes local queue controls a selected-DPU sender never maps; nothing
  DPU-side writes the host's flags. Validation round 1 watched a walsender spin at 99% CPU >138 s on
  exactly that assumption. Do not confuse this SENDER channel with the RECEIVER's FAILED terminal
  (`SubmitByteRingWriteTerminal`, role-7 ring, entry above) — same entryState value, different ring
  direction, different publisher family (`CREDIT_BODY/PUBLISH` vs `WRITE_PRODUCED_TAIL_*`).
- **ONE-SHOT + SUPPRESSION:** `byteRingFailedCreditPublished` permanently suppresses later ACTIVE
  publications (an ACTIVE re-stamp would erase the terminal the producer's exit decision reads).
- **ENFORCES:** `HomerDpuDmaSubmitByteRingConsumedHeadPublication` (renamed from the LYING
  `...SmokePublication` name — it was always the production Loop-1 submitter): ACTIVE keeps
  strict-advance + nonzero; FAILED allows equal/zero. The sweep's FAILED eligibility arm; the ordinary
  arm's `byteRingFailedCreditPublished` refusal; `HomerServiceArmSenderFailedCredit`'s two call sites
  (`BeginPayloadAbortReset`, reset-complete sender real-failure arm).

## `HomerClientPollBaseBackupReceive` terminal split — **FAILED is drain-INDEPENDENT and a poll FAILURE; CLOSED is drain-gated SUCCESS**

- **MEANS:** `entryState==FAILED` (honored only once `dpuLastCreditEpoch != 0`) returns false with a
  distinct truncation error — even with a torn tail holding `consumedHead < producedTail` forever.
  `CLOSED` still requires the full drain before `*streamComplete`.
- **DOES NOT MEAN** both states exit alike: before Stage 1 they both exited SUCCESS, which reported
  aborted backups as good (the silent-truncation bug). Never re-merge them.
- **ENFORCES:** the FAILED arm in `HomerClientPollBaseBackupReceive`;
  `HomerDpuDmaSubmitByteRingWriteTerminal` accepts ONLY CLOSED/FAILED (in-function guard).

---

## Related

- [`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) — the narrative and evidence
  behind every entry here (§34–§45, D-S0 §50, Stage-1 abort §51), including **six refuted mechanisms** and why each was tempting.
- [`selected_dpu_session_rings_and_lifecycle.md`](selected_dpu_session_rings_and_lifecycle.md) — the selected-DPU
  architecture of record.
- `docs/kb/operations/farnet_operational_hazards.md` — the **operational** traps. *This file is the **code** traps.*
