# Homer contracts & invariants — THE INVERTED INDEX

**This doc is not a narrative. It is an INDEX, keyed by the QUESTION you would ask — not by the function that
answers it.** Everything else in this directory tells a story; this tells you *what rule governs the line you
are about to write*, and *where it lives*.

> ## WHY THIS FILE EXISTS
> On 2026-07-14 a rule was stated **perfectly**, in a comment, and violated anyway — **in the same file, a
> thousand lines below its own statement.** Cost: most of a day, six refuted mechanisms, and a leak of every
> selected session on a node whose log nobody had opened.
>
> **A comment lives where the answer is DEFINED. The question arises where the answer is USED.** No amount of
> comment discipline closes that gap. This index closes it, by being keyed by the question.
>
> **It does not spare you from reading the code. It spares you from not knowing there was something to look
> for.** ⇒ **Every entry is a POINTER AND A WARNING. Go read the `file:line`. Verify. A stale index entry is a
> lying diagnostic and costs more than none.**

**Maintenance is mandatory, not optional** — see the pre-commit inspection pass in the global agent rules.
**Adding a call site to any "EVERY SITE" list below is part of the change that adds it.**

---

## Q: "Which session does this ring serve?" — **THERE IS EXACTLY ONE WAY TO ASK**

- **THE RULE:** call **`HomerDpuDmaRingBoundSessionId(import, ringIndex)`**
  (`citus-dbcomm/.../homer/homer_service_dpu_dma.c:757`). It returns what the DPU has actually **BOUND**.
- **⛔ NEVER read `descriptor->serviceSessionId`.** The descriptor states what the host **DECLARED at cold
  setup**, and for the frontend agent's shared arena **that is `0` for EVERY ring** — the arena is exported once
  at postmaster start and its slots are handed to backends *later*, one at a time. Comparing it to a real
  session id is `0 == N`: **never true.** Returns `0` for an unbound ring; callers resolving by session never
  pass `0`.
- **EVERY SITE THAT MUST OBEY IT:**
  - `tuple_sink_service_process.c:42443` — payload setup-drain ownership
  - `tuple_sink_service_process.c:42530` — setup-close semantic drain
  - `tuple_sink_service_process.c:42583` — **selected-session release** (`…ResetSelectedSessionsForClosingSetup`)
  - *(A new caller belongs on this list. If it is not here, it has not been checked.)*
- **WHAT IT COST:** violated at `homer_service_dpu_dma.c:10874`. The ownership predicate silently answered *"not
  mine"* for every session ⇒ the close-finalizer `continue`d past all of them ⇒ **node A never released a single
  selected session**, its 64-slot table filled, it could no longer stage commands, and node B spawned backends
  that received **zero** commands. Gate died at session 65. Fixed citus `f12a96112`. Audit §44–§45.
- ⚠ **The function's own doc comment said it mapped identities back to "*descriptor* service sessions."**
  **That phrasing is where the bug came from.** A doc comment that names the wrong field is not a comment — it
  is an instruction to write the bug.

---

## Q: "This failed at 64 — which resource ran out?" — **WRONG QUESTION. SEVEN OF THEM ARE 64.**

- **THE RULE:** **never assume a "64" failure names the resource you have in mind.** These are all, independently,
  **64**:

  | resource | where |
  |---|---|
  | node-B **service session table** | `homer_abi_version.h:50` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS` |
  | **selected-DPU session table** (⚠ **BOTH nodes have one**) | `tuple_sink_service_process.c:42953` |
  | scheduler **machine candidate set** | `HOMER_PROGRESS_PLAN_MAX_GRANTS`, `tuple_sink_service_process.c:2551` |
  | scheduler **flat ready set** | same constant |
  | **payload stream table** | `homer_abi_version.h:51` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` |
  | peer transport **outgoing connections** | `remote_execution_peer_transport_rdma.c:70` |
  | peer transport **incoming connections** | `remote_execution_peer_transport_rdma.c:71` |

- **⛔ THE TRAP: ONE EXHAUSTION MASKS THE NEXT.** Node B's session table and node A's selected-session table
  both cap at 64. Node B's cliff fired at session 64, so **in the entire life of this code nobody ever reached
  session 65 to discover node A's table was full too.** Fix one and the next steps straight into its place —
  **and looks exactly like a regression your fix just caused.** It was there all along.
- **HOW TO TELL THEM APART:** they fail *differently*. Session table → `"peer service ran out of free command
  sessions"` (a peer *response*, **not** logged on the DPU — see below). Machine candidate set → a 30 s command
  timeout. Selected-session table → `"selected-DPU session table is full"` on **node A**.
- **NOT 64, and worth knowing:** DPU byte-ring pool = **8** (`homer_service_dpu_dma.h:41`, 4 slots × 2 regions);
  frontend arena = **16** slots; import table = **1024**.

---

## Q: "Can I trust `completion.commandState`?" — **NO. THE DPU EGRESS MANUFACTURES IT.**

- **THE RULE:** in `HomerServiceDpuEgressOneSelectedCompletionEvent`, when the **result relay's** peer-open has
  failed (`dpuResultPeerOpenFailed`), the egress **OVERWRITES an otherwise-COMPLETED completion's
  `commandState` to `FAILED`** (`tuple_sink_service_process.c:43873`-ish) — **while the backend is still ALIVE**,
  and while `dpuBackendTeardownStarted` is still `false`.
- **⇒ A `FAILED` `commandState` on this arm does NOT mean the backend died.** It may mean only that the *result
  relay* could not open.
- **⛔ NEVER key backend-lifecycle decisions on `commandState`. KEY ON `commandKind`** — the conversion writes
  `commandState`, `resultFlags`, and `detail`, and **never touches `commandKind`.**
- **WHAT IT WOULD HAVE COST:** clearing `backendLoopActive` on that synthetic `FAILED` makes DPU command
  **landing** reject the session (`tuple_sink_service_process.c:43363`) ⇒ the client's `TX_ABORT`/`CLOSE` can
  **never land** ⇒ the session can never quiesce. **A permanent wedge** — the same shape as the §35
  fence-and-return bug, nearly re-created. Audit §40.2.
- **RULE, GENERALIZED: KEY ON THE FIELD THAT IS NEVER MANUFACTURED.**

---

## Q: "Who decrements `activeSinkCount`?" — **ONE FUNNEL. GO THROUGH IT.**

- **THE RULE:** **`TupleSinkServiceMaybeReclaimSinkAndSession`** (`tuple_sink_service_process.c:27726`) is the
  **single decrement point.** It guards inactive streams, checks underflow, decrements **exactly once**, resets
  the stream, and **retires the session if that was its last sink**.
- **⛔ NEVER call `HomerServiceResetPayloadStreamEntry` directly on a path that owns a sink reference.** Freeing
  the stream without going through the funnel leaves `activeSinkCount` pinned — and **EVERY retirement path is
  gated on `activeSinkCount == 0`**, including `AllocateSession`'s table-full reclaim
  (`tuple_sink_service_process.c:24273`).
- **⇒ ONE MISSED DECREMENT MAKES EVERY RETIREMENT PATH REFUSE.** It does not look like a refcount bug; it looks
  like three independent retirement paths being unreachable.
- **⚠ THE REFERENCE IS TAKEN EARLIER THAN YOU THINK.** The selected-DPU result stream takes its
  `activeSinkCount++` at **eager identity reservation** — *before* `SERVICE_RESULT_OPEN` installs the synthetic
  `sendHandleOpen`. Any release helper **must tolerate `sendHandleOpen == false`**, or it skips exactly the case
  that leaks (a result-open that never bound).
- **WHAT IT COST:** the `peerResetAfterCleanClose` branch used to free the stream and `return`, bypassing the
  funnel. Node-B sessions never retired; the 64-slot table filled. Audit §39.1, §43. Fixed citus `494751b7b`.
- ⚠ **The comment above that branch carefully enumerated three functions it was "deliberately NOT calling",
  each with a reason — and never noticed it was also skipping the sink accounting.**
  **A carefully-reasoned exclusion list is silent about the thing it forgot.**

---

## Q: "When may I clear `backendLoopActive`?" — **ONLY ON CLOSE / FAILED / (COMMIT|ABORT for non-client-SQL).**

- **THE RULE (the host arm's predicate, `tuple_sink_service_process.c:21503`):** clear only when the command is
  **terminal** *and* it is `CLIENT_SQL_SESSION_CLOSE`, **or** `FAILED`, **or** `TX_COMMIT`/`TX_ABORT` **for a
  non-`CLIENT_SQL_SESSION` opKind**. Its own comment: *"For persistent client SQL sessions, COMMIT/ABORT are no
  longer terminal."*
- **⛔ NEVER clear it on every terminal completion.** The gate runs **~14,000 commands on ONE session** (2000 tx
  × ~7 statements). DPU landing rejects a non-teardown session whose `backendLoopActive` is false
  (`:43363`) ⇒ **the session would stop admitting commands at statement #2.**
- **WHY IT MATTERS BEYOND THE BACKEND:** it is a **disjunct of the session-machine gate** (`:45717`), so a
  *stale* `true` mints a `COMMAND_SESSION`/`WAIT_BACKEND` scheduler machine **forever**, for a backend that
  exited long ago. 63 such zombies filled the 64-slot machine candidate set and **evicted the one live
  session's own machine** every pass. Audit §41. Fixed citus `9ef06f224`.
- ⚠ **The selected-DPU FAILED arm is DELIBERATELY NOT IMPLEMENTED** — a genuine backend `FAILED` collides with
  no-backend cleanup (`:24618`) vs teardown-fenced landing (`:43378`): two owners for one mailbox record,
  resolved by scheduler order. Needs a lifecycle design, not a flag write. Audit §40.2.

---

## Q: "Nothing is in the log — so nothing went wrong, right?" — **NO. THESE FAIL SILENTLY.**

- **BOUNDED SETS THAT DROP WORK.** `HomerProgressMachineCandidateSet` has **seven** overflow counters
  (`tuple_sink_service_process.c:3294`-ish) — **all of them were write-only.** The *ready set* had a report
  function; **the machine candidate set had none**, so the drop that actually killed the run emitted **nothing**,
  while its noisy neighbour printed an overflow that produced an entire wrong root-cause chain.
  ⇒ Now reported by `HomerServiceReportMachineCandidateOverflow` (**per PHASE**, with a **kept-kind histogram**).
  Audit §39.3, §41. Fixed citus `e5fd7a210`.
- **DOWNSTREAM POLICY ADMISSION** is a *tighter*, also-silent boundary: **16 grants / 12 machines / 2 PAYLOAD**
  (`tuple_sink_service_process.c:182`). Denial only bumps a counter; counters are **overwritten per phase** and
  were printed **only at service exit**. Now reported by `HomerServiceReportPolicyAdmissionDrops`.
  ⚠ The code names this failure itself at `:11913`: *"**ARMING A CANDIDATE IS NOT ENOUGH** … armed on every pass
  and granted on none … the DPU then sat forever without even a timeout … **Nothing warns you** … **the service
  looks idle.**"*
- **MESSAGES THAT NEVER REACH A LOG.** `"peer service ran out of free command sessions"` and `"service ran out of
  payload stream identities"` go into a **peer error response**, not an `fprintf`. **A `grep` for them on the DPU
  returns 0 whether or not they happened.** Grep the **client**.
- **`HOMER_SERVICE_LOG` AND STATS MACROS ARE COMPILED OUT OF A PERF BUILD.** **Never anchor a validation proof on
  a `HOMER_SERVICE_LOG` string** (hazards §9).
- **RULE: NO SILENT CAPS.** A bounded set that drops work must say **what** it dropped — and **what it KEPT**.
  *The victim is an accident of ordering; the occupants are the bug.*

---

## Q: "The client failed. Which DPU log do I read?" — **BOTH. ALWAYS.**

- **THE RULE:** the gate has **two** DPUs. **Node A (farnet0 DPU) drives the command plane; node B (farnet1 DPU)
  executes it.** A failure **OBSERVED** on one is routinely **CAUSED** on the other.
- **WHAT IT COST:** node B spawned a backend and landed zero commands. I read node B's log, found nothing, and
  built an entire suspect list out of node-B code — **every suspect wrong, because the bug was not on node B at
  all.** Node A's log was **9.4 million lines**, 9,416,076 of them saying `selected-DPU session table is full`.
  Nobody had ever opened it.
- **HOW:** node A's log is on **farnet0's** DPU — `ssh farnet0` → `ssh dpu`. **Ship a script; never a double-hop
  one-liner** (Non-negotiable #5). **Grep it ON the DPU**: a 9.4-million-line `cat` across a double hop simply
  times out.
- ⚠ **`grep -i ALARM` IS NOT ENOUGH.** Use:
  `grep -aiE 'ALARM|semantic validation failed|fatal error state|PE drain failed|pool exhausted|peer-open failed|table is full'`

---

## Related

- [`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) — the full narrative and
  evidence for every entry above (§34–§45), including the six refuted mechanisms and why each was tempting.
- [`selected_dpu_session_rings_and_lifecycle.md`](selected_dpu_session_rings_and_lifecycle.md) — the
  architecture of record for the selected-DPU arm.
- `docs/kb/operations/farnet_operational_hazards.md` — the operational (not code) traps.
