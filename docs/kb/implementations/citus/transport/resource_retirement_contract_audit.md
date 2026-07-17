# The resource-retirement contract: audit and remediation plan
<!-- kb-summary: Ordered Homer transport resource-retirement audit, implementation decisions, evidence, and validation history. -->

> ## ⚠⚠ READ THIS BOX BEFORE ANY OTHER SECTION OF THIS FILE
>
> **THE P0 SET IS COMPLETE. `§4` IS SUPERSEDED AND ITS "OPEN" MARKERS ARE STALE — DO NOT READ §4 AS STATUS.**
> The plan of record is **§9** (the merged protocol); the per-item outcomes are **§14–§29**, each with a SHA.
>
> | item | state | commit |
> |---|---|---|
> | **P0-a** | ✅ LANDED + VALIDATED (§20) | citus `71b523757` |
> | **P0-b** (= P7b.0) | ✅ LANDED + VALIDATED (§22.10) | citus `81593cdf9` |
> | **P0-c** | ✅ LANDED + VALIDATED (§28.8) | citus `2836bc426` |
> | **P0-d** | ✅ LANDED + VALIDATED (§15.5) | citus `9a6f68257` |
> | **P0-e** | ⛔ **DROPPED — THE BUG WAS REFUTED** (§19) | — |
> | **P0-f** | ✅ LANDED + VALIDATED (§14.7) | citus `64050e0dd` |
> | **P0-i** | ✅ LANDED + VALIDATED (§21.12) | citus `67ebc0167` |
> | **§24** WR-ID contract | ✅ LANDED + VALIDATED (§24.8) | citus `1d379b333` |
> | **§25 / F7** | ✅ LANDED + VALIDATED (§25.6) | citus `16e4bdf3e` |
> | **§29a / §29b** | ✅ LANDED | citus `c3e3f3e56` / `e85abe531` |
> | **P1-f** | ✅ LANDED + VALIDATED (§33.11) | citus `91482c840` |
> | **send-CQE Stage 2a** | ✅ **LANDED + VALIDATED** | citus `bdcb7eb70` · **§52** |
>
> **⇒ P7b IS UNBLOCKED.** P7b.0 *is* P0-b, already landed. P7b.1 is small (§26.6).
>
> **STILL OPEN — and P2-i is the LAST CODE ITEM:**
>
> | item | state | where |
> |---|---|---|
> | **P2-i** — `HomerDpuDmaDestroy` drain on clean exit | ✅ **LANDED + VALIDATED** — citus **`2b37e8701`**. **AND IT IS A REAL BUG FIX:** `entered with 3 outstanding task(s)` when SIGTERM'd **mid-transfer**. | **§34.12** (the measurement) · §34 (spec) · §34.7 (my two self-refutations) · §34.9/§34.10 (3 review rounds, 8 defects, 6 mine) |
> | ~~🔴 an exit path that reaches `HomerDpuDmaDestroy` NEVER~~ → ✅ **FIXED + VALIDATED (§48a = §36 PART 2), 2026-07-15** | The **backend-side** DPU service used to **exit(1)** out of the session-reset path (*"refusing to reset peer CLIENT_SQL_SESSION..."*) and never drain the engine. **Now:** guard moved to the top of `TupleSinkServiceResetSession`, `exit(1)` replaced by a pure `DEFERRED` return, so the shutdown sweep reaches `HomerDpuDmaDestroy`. §36 PART 1 (already landed) makes a *graceful* close quiesce the session; this backstop covers the un-graceful SIGTERM-mid-command case that survived PART 1. | **§36.10** (impl) · §34.12 · §48 |
> | **P2-j** — grouped-control reads outliving tenancy | ✅ accepted, **no action** | §4/P2-j |
> | **§31** — dead DPU shm rings | 🧹 planned **cleanup**, not a bug | §31 |
> | **§22.10.1 / P2-o** — P0-b's `RETIRING` branch + abandonment | ✅ **IMPLEMENTED + VALIDATED 2026-07-14 (instrument-and-observe, NOT fatal)** | **§49.** Teardown ledger counts both abandonment triggers — partial-publish (`:12163`) and stale-async recycle (`tuple_sink_service_process.c:41036`) — plus RETIRING entered/released/discarded. ⚠ RETIRING is NOT only abandonment: `:12617` is a **NORMAL** entry (response consumed before our send CQE reaped). VALIDATION: both DPUs printed `abandoned_*=0 live=0`, no ALARM → **abandonment 0/0 confirmed EMPIRICALLY**; `retiring_entered=0` (dead-in-practice, so the RETIRING counters are validated by construction, NOT by execution). Stale-async race benign-ness stays UNVERIFIED but un-triggered. |
> | 🟡 **The 4-role basebackup stall** | **NOT P2-i** (controlled A/B: 2/2 pass on *both* binaries). Environmental; leading hypothesis is the DPU TCP smoke server binding **9727**, the DPU service's own setup port — and the documented DPU reap recipe **cannot see** the smoke binary. **INFERRED.** | **§34.13** |
>
> ⛔ **This box PREVIOUSLY said P2-i's hole was "`fatalError` ⇒ the drain can never converge."** That claim is
> **REFUTED (§34.7) and it is BACKWARDS**: `fatalError` is a software *"do not start new work"* latch, not a dead
> device; already-submitted tasks still retire and **MUST** be harvested (`harvestEvenIfFatal=true`). Refusing to
> drain under fatal is *itself* the hang — the code says so at `homer_service_dpu_dma.c:6535-6538`. **Do not
> re-adopt the struck claim from this table's history.**
>
> ### ⚠ THE HAZARD THIS BOX EXISTS TO PREVENT — and it fired, on 2026-07-13
>
> This file **appended** its outcomes (§14…§29) and **never updated its header or §4**. A reader — including
> its own author, after a context compaction — reconstructed the status from the header and §4, because those
> are the parts that *look* like a summary. The result: a full turn spent proposing P0-b as "the next stage,"
> a day after it landed, with a confident rationale attached.
>
> > **A long-lived document that appends its outcomes instead of updating its header will eventually lie to its
> > own author.** Status belongs at the TOP and must be edited in place. An append-only log is a history, not a
> > state. *(Owner + Claude, July 13, 2026.)*

**Status:** **P0 COMPLETE** (see the box). Audit COMPLETE (RDMA + DOCA DMA). Remaining work is P1/P2 — see the box.
**Opened:** July 12, 2026. **Owner-driven** (the contract below is an owner statement, not a derived rule).
**Code baseline (at open):** citus `d33f6ded3`, postgres `0021633a44a`. **Last updated:** July 16, 2026 (Stage 2a
landed as citus `bdcb7eb70`).

---

## 0. THE CONTRACT

> **A resource may be released, freed, reused, reset, deregistered or detached ONLY when its own outstanding
> physical operations have been retired. You do not leave until you have handled your own work. Nothing is
> left behind for a later occupant to trip over.**

**Corollary — you may not call something "stale" until you can NAME WHO WAS WAITING FOR IT.** A work request
exists *because* something waits on its completion: a send CQE retires a WR slot and frees its staging buffer;
an ACK advances credit; a DOCA task completion releases a `doca_buf` and advances a frontier. Dropping such a
completion does not make it harmless — it leaks the slot, or wedges the credit, or corrupts a frontier.
**"Stale ⇒ discard" is never available.** If a completion genuinely has no consumer, that is a question about
why it is posted, not a licence to drop it.

Applies to **every** transport in this system: RDMA (CQEs) and DOCA DMA (task completion callbacks) alike.

## 0.1 Why this is a standing contract and not a bug report

The audit's central finding is structural:

> **The contract is upheld EXACTLY where someone was previously burned, and nowhere else.**
> That is scar tissue, not design.

Every place that upholds it carries a comment explaining the bug that forced it. Every place that violates it
was simply never hit. So the violations are not a list of mistakes — they are the *complement* of the list of
past incidents, which means enumerating them requires an audit, not a memory.

## 0.2 Why it is URGENT now

**P7b makes resources long-lived and POOLED (reused across sessions).** Every safety argument of the form
*"this cannot happen because the object gets destroyed anyway"* **stops holding the moment we pool.** Several
such arguments are currently load-bearing, unwritten, and correct-by-luck. Pooling is precisely the change
that converts them into silent corruption.

**Sequencing consequence: the P0 items below land BEFORE P7b.1's reuse.**

---

## 0.3 THE MODEL — four scenarios, three mechanisms (READ THIS FIRST)

There are not fifteen problems. There are **four scenarios**, distinguished by exactly two questions:

> **1. Who can still touch this after I let go?** (my NIC / the remote peer / the DPU engine)
> **2. Can *I* observe their completion?**

### Scenario A — I posted work touching MY memory, and I CAN see the completion

**Involved:** source/target buffer + MR, the WR, my send CQ, the slot that owns them.
**Contract:** do not free / overwrite / deregister / reuse until **my own CQE is retired**.
**Efficient solution: an OWNER + a COUNT.** Reserve at post, release at CQE, gate release on `count == 0`.
O(1), no blocking, no allocation, control path only.

| instance | status |
|---|---|
| payload DATA send (`payloadSendOwnerOutstandingWrs`) | **HOLDS** |
| receiver-head ACK (`receiverHeadAckOutstandingCount`) | **HOLDS** |
| client SQL command scratch MR (`:6550`) | **HOLDS — THE EXEMPLAR** |
| peer-client completion publish (`:6604`) | **VIOLATED** — clears an ACTIVE owner, then deregisters the MR |
| control request op (`:7825`) | **VIOLATED** — memsets the registered SOURCE BUFFER before its CQE is polled |
| `payloadPublishTailBuffers` / `payloadFragmentHeaderBuffers` | **VIOLATED — no owner exists at all** |

For the two staging pools the cheap fix is not per-slot owners but **two cursors**: reserve against
`posted - completed < POOL_SIZE`, advance `completed` at the signaled checkpoint. (The fragment headers are
unsignaled but chained to a signaled tail WIMM, so the frontier advances in bulk.) No per-slot state at all.

### Scenario B — a slot IDENTITY is reused and a late completion could be MISATTRIBUTED

**Involved:** the slot, a generation/token, the completion still naming the old occupant.
**Contract:** either do not reuse until retired (best), or make the late completion **ATTRIBUTABLE** —
**never droppable**.
**Efficient solution: reuse-after-retire** — i.e. Scenario A's counter again. A generation is the *fallback*
for when you genuinely cannot wait, and it must **route the completion to its original owner**, not discard it.

Instances: control op slot (**VIOLATED**); payload stream slot + doorbell token (generation works, but a late
event punishes with a **connection-wide reset** — fine today, unacceptable on a SHARED pooled connection);
control response slot (**BY ACCIDENT**).

### Scenario C — the REMOTE PEER writes into my memory. I CANNOT see their completion.  ← the hard one

**Involved:** an MR I registered as a remote-write target (command mailbox, completion mailbox, receive byte
ring), the peer's WR, and **the peer's CQ, which I cannot poll**.
**Contract:** do not deregister/reuse until **no further remote write can arrive** — which I **cannot prove
locally**. Only two sources of proof exist:
  **(a)** the peer TELLS me — but its ack must mean **"my writes are PHYSICALLY RETIRED"**, not "I stopped
  issuing"; or
  **(b)** I destroy a fence it cannot write through — **the QP**.

**Today we lean on (b), implicitly.** The gate is `peerWritersQuiesced || connectionResetComplete`, and
`peerWritersQuiesced` is set when the **close command is OBSERVED** (`:23945`) — *logical*, not *physical*.
**Pooling deletes option (b).**

**Efficient solution — and it is the elegant one: this scenario is NOT special.** The peer CAN see its own
CQEs. So make the peer uphold **Scenario A for its own writes**, and make its "quiesced" ack MEAN
physically-retired. The `CLOSE_SINK` quiesced response already exists; it merely has to promise the right thing.

> **Every actor retires its own work; cross-actor safety then follows for free, because an ack means
> "physically retired", not "logically done".**

Nobody ever has to reason about a completion they cannot see. *(Interim, without a protocol change: DEFER
deregistration to connection reset — exactly what the sender-head mirror MR already does deliberately,
`:25071`.)*

### Scenario D — CROSS-MACHINE. The party releasing is not the party acting.

**Involved:** a host arena slot; the DPU's import + per-ring `inFlightTaskCount`; DOCA tasks the host **cannot
see at all**.
**Contract:** the HOST may not zero/free a slot the DPU may still be DMA-ing into.
**Efficient solution: the release must be a HANDSHAKE, not a local decision.** The count already exists on the
DPU — the host simply has to ask. **This is Scenario C's principle again: the actor who can SEE the completions
is the one who must CERTIFY them.**

| instance | status |
|---|---|
| arena T1-T4 normal release | **HOLDS — this is the model** (quiesce -> all three ring task counts zero -> RELEASE -> re-check -> unbind) |
| forced-teardown deadline (`:43569` / `homer_service_dpu_dma.c:6353`) | **VIOLATED** — reads the count, PRINTS the danger, resets anyway |
| dead-PID reaper (`homer_frontend_agent.c:689`) | **VIOLATED, worst blast radius** — host zeroes + frees with zero DPU knowledge |

On the **crash** path there is **nobody to hand-shake with**. So the efficient answer is not a protocol but
**QUARANTINE: do not reuse the slot until the DPU confirms zero.** Costs one arena slot, not a hot-path
round-trip — and converts silent corruption into a slot you can see.

### Scenario E — destroying the ENGINE itself

**Involved:** the QP + CQs; the DOCA DMA engine + progress engine.
**Contract:** destruction **IS** a legitimate fence — it makes further completions *impossible*.
**Efficient solution: keep it exactly as-is for FAILURE, and never use it for anything else.**

- violent QP teardown on failure — **CORRECT, do not touch**
- using it as the mechanism for **clean** release — **exactly what P7b.1 must stop doing**
- `HomerDpuDmaDestroy` on clean exit — no drain, and it says so itself

### THE WHOLE MESS IN ONE LINE

| | mechanism | cost |
|---|---|---|
| **A + B** (I can see the completion) | **owner + count + zero-gate** (or two cursors for a ring) | O(1), control path only |
| **C + D** (someone else acts) | **they certify their OWN retirement** — their ack must mean *physically* retired | one field's meaning, or one handshake; **QUARANTINE** where there is nobody to ask |
| **E** (the fence) | violent teardown, **for FAILURE ONLY** | free |

> **Scenario E has been silently standing in for A, B, C and D.** QP destruction annihilates every outstanding
> completion, so nothing else *had* to be right. **Every violation in this document is Scenario E leaning on
> the others' behalf.** Pooling removes it — which is why this audit is a PRECONDITION for P7b.1, not a
> follow-up, and why all the fixes look the same.

---

## 1. THE THREE FAILURE SHAPES

1. **Storage reused under a live engine.** Free/overwrite/zero a buffer the NIC or the DMA engine is still
   reading or writing. Silent corruption; no error.
2. **A completion applied to the WRONG TENANT.** A slot is reused by a new owner, then the old owner's
   completion lands and advances *its* frontier or frees *its* buffer.
3. **A completion whose consumer has left.** Leaked slot, wedged credit, or a failure status nobody reads.

---

## 2. AUDIT — DOCA DMA (host ↔ DPU over PCIe)

Verdicts: **HOLDS** / **VIOLATED** / **BY-ACCIDENT** (emergent invariant, nothing enforcing it).

### 2.1 What HOLDS (verified — do not "fix" these)

Genuinely well-guarded. Recorded so nobody re-audits them:

| resource | proof |
|---|---|
| **Normal mmap detach/reclaim** — the `host-detached` path that fires on EVERY clean run | Per-import `inflightTaskCount == 0` enforced before `doca_mmap_destroy` (`homer_service_dpu_dma.c:9478`); teardown moves ACTIVE→CLOSING and `HomerDpuDmaImportCanSubmit` refuses new work on non-ACTIVE (`:9070`). Reclaim additionally checks mirror/write frontiers and in-flight flags (`:9563`, `:9569`). |
| **`doca_buf` + task lifecycle** | Callbacks validate pointer, SUBMITTED state, and slot generation (`:14521`, `:14574`); retirement frees the task, drops both buffer refs, bumps the generation, then marks the slot free (`:10895`). Task slots are pooled; DOCA task objects are per-operation. |
| **Mirror / landing byte ranges** | Pulls are bounded against `releasedByteTail` so unreleased borrowed bytes cannot be overwritten (`:3091`); release validates the active claim before advancing (`:5273`). |
| **Host export destroy** (client side) | Gated on the DPU's `CLOSE_ACK` (`homer_client.c:2452`); basebackup refuses to destroy without it (`:5551`). |
| **Prepared body→publish task chains** | Only the terminal publish releases the shared control cell and advances the published frontier (`:10768`). |

> **This refutes a hypothesis I held going in.** I expected the normal host-detach path to be the weak point,
> because it fires on every clean run. It is in fact one of the best-guarded paths in the system.

### 2.2 P0 — VIOLATED and REACHABLE

#### V1. Dead-owner arena reaper: a HOST-side free with NO knowledge of DPU DMA  ⚠ WORST

`HomerFrontendAgentReapDeadArenaSlots` (`homer_frontend_agent.c:688`) walks the arena, finds BOUND slots whose
owning backend pid is gone (`kill(pid,0) → ESRCH`), and releases them via
`HomerFrontendAgentReleaseArenaSlot` (`:666`), which **zeroes the slot bodies** (`HomerFrontendAgentInitArenaSlot`)
and publishes `FREE` with a release store.

**This is a HOST-side decision made with ZERO knowledge of the DPU's in-flight DMA.** There is no handshake and
no consultation of the DPU's per-ring `inFlightTaskCount`. The binder's CAS proves only that the *host* state is
FREE — it explicitly does **not** establish DMA ordering.

The safety argument in the comment (`:658`) is:

> "A hard crash (SIGKILL/SIGSEGV) skips [on_proc_exit] and leaves the slot BOUND — but a backend crash already
> drives a postmaster crash-restart, which kills every other backend."

**That argument does not cover the DPU.** The DPU service is a **different machine**. A postmaster crash-restart
does not stop it, and the arena `/dev/shm` object is **not unlinked** — so the DPU's import stays valid and its
in-flight DMA can still land in those pages *after* the host has zeroed the slot and handed it to a new backend.

**Failure:** cross-session **host-memory corruption** and silent wrong results.

**Reachability — this is not hypothetical.** Our own runbook (CLAUDE.md) *mandates* `kill -9` on surviving
`postgres: remote exec backend` processes after every `--homer-dpu-command` run, because socketless backends do
not respond to `pg_ctl stop`. The reaper exists precisely to clean up after that.

#### V2. Forced arena unbind ignores its own in-flight count

`homer_service_dpu_dma.c:6353`. The unbind loop reads `import->ringRuntime[...].inFlightTaskCount != 0`, prints

> `"arena slot %u ring %u unbound with %u task(s) in flight (forced teardown; a late completion can re-poison
> the tenancy reset)"`

…and then **continues and resets the tenancy anyway** (`:6394`). Its own comment states the danger exactly:
*"A task still in flight will, on completion, write the OLD tenancy's frontier back into the runtime we are
about to reset — which is the very poisoning this reset exists to prevent."*

It is reached from a **30-second forced-teardown deadline** (`tuple_sink_service_process.c:43581`) that calls
`TupleSinkServiceReleaseDpuArenaBinding` **without checking tasks**. The P2.T T1-quiesce/T2-drain that would make
the count zero runs on the *clean* path only.

**This is the family's signature move, again: the code COMPUTES the right thing, PRINTS it, and does the wrong
thing anyway** — identical in shape to `clean_close=1` being computed, printed, and then ignored by the reset
(§25.2 of the plan doc).

**Failure:** poisoned tenancy state → the grouped-control validator can kill the DMA engine, or accept a stale
frontier. **Reachable on the failure/timeout path.**

#### V3. `HomerDpuDmaDestroy` has no drain — and says so

`homer_service_dpu_dma.c:1178` explicitly records that drain-before-destroy is **unimplemented**, then destroys
contexts, imports, inventories and the progress engine (`:14271`, `:14291`, `:14300`, `:14426`). Service exit calls
it (`tuple_sink_service_process.c:47985`).

Violent teardown on a *failure* exit is acceptable. **Clean service exit has no proof that no tasks are
outstanding.** A callback against a freed engine/import/inventory is a crash or corruption.

### 2.3 P1 — BY-ACCIDENT (works today; nothing enforces it)

#### A1. Grouped-control reads deliberately outlive their tenancy

Unbind bumps `tenancyGeneration` while **preserving** in-flight state (`:4580`); the grouped-control completion
compares its stamped generation and discards stale semantics (`:12644`); these reads are **explicitly excluded**
from per-ring accounting (`:4533`).

It works **today** only because the read lands in **private DPU staging** and its stale result is rejected. But
the old operation is still left for a later tenancy to retire — a direct contract violation. **If the staging
buffer is ever pooled or retargeted, this becomes wrong control state.** Flagged because P7b pools things.

---

## 3. AUDIT — RDMA (sweep complete)

### 3.0 THE ORGANIZING INSIGHT — read this first

> **Today, QP DESTRUCTION is the universal physical fence.** Many paths release/deregister/reuse without any
> completion proof of their own, and are safe only because the connection is eventually torn down violently,
> which destroys the QP and CQs and thereby annihilates every outstanding completion.
>
> **Pooling REMOVES that fence.** Once a QP is long-lived, only resources with their OWN explicit owner/CQE or
> task-count gate remain contract-compliant. **Semantic close alone is not a proof.**

That single sentence explains why this audit had to precede P7b.1 rather than follow it — and why the fix list
is longer than "one bug".

### 3.1 What HOLDS (verified — do not re-audit)

| resource | proof |
|---|---|
| payload DATA send owners/MRs | `CLOSE_SINK` gated on `payloadSendOwnerCount == 0 && payloadSendOwnerOutstandingWrs == 0` (`tuple_sink_service_process.c:27062`) |
| receiver-head ACK slots/MR | reclaim refuses while `receiverHeadAckOutstandingCount != 0 \|\| completedHead < postedTail` (`:35605`) |
| **client SQL command scratch MR** | **THE EXEMPLAR — see §3.2.** Scans owners; refuses to reset while any remain (`:6550`, `:6570`) |
| sender-head mirror MR | deliberately deferred deregistration; a late consumed-head ACK would otherwise `REM_ACCESS_ERR` and poison the QP (`:25071`, `:26325`) |
| DPU mirror byte-range claim | release only the completed prefix (`homer_service_dpu_dma.c:5266`) |
| detached-import reclaim | import task count zero + both semantic closes + frontier checks (`:9526`, `:9574`, `:9592`) |
| arena slot, normal T1–T4 release | quiesce, then **all three** arena-ring task counts zero, then RELEASE, then re-check `inFlight==0` before unbind (`:43638`, `:43689`) |
| `outgoingPublishedTailBuffer` | current helpers post and synchronously await completion (`remote_execution_peer_transport_rdma.c:10185`) |
| notification RECV slots | reposted per consumption; connection-lifetime, not per-tenant (`:6990`) |

### 3.2 ⚠ THE SMOKING GUN — two siblings, fifty lines apart, one correct

**`TupleSinkServiceClearClientSqlCommandWriteCompletionsForSession` (`:6550`) — CORRECT.** Scans the owner
table; if any owner for this session is still `active`, it **refuses to reset and `exit(1)`**, with a comment
that states the contract exactly:

> *"A posted command owner means the RNIC may still read the command mailbox. Resetting the session would
> deregister/unmap that source memory and reuse the session slot under an in-flight WR. Treat this as
> reset-required rather than silently manufacturing a use-after-free."*

**`TupleSinkServiceClearPeerClientCompletionPublishCompletionsForSession` (`:6604`) — VIOLATED.** Same job,
sibling resource, fifty lines below:

```c
if (entry->active && entry->serviceSessionId == sessionState->serviceSessionId)
{
    TupleSinkServiceClearPeerClientCompletionPublishCompletionIndex(completionIndex);   /* BLINDLY CLEARS */
}
```

It finds an **active** owner — the RNIC may still be reading that source — and simply erases it. No check, no
refusal, no warning. Then session reset **deregisters that source MR** (`:23617`).

**Failure:** RNIC reads deregistered/reused source memory -> local-protection CQE, **poisoned QP**, or wrong
completion bytes delivered to a *later* session. **Reachable** whenever a clean session reset races completion-CQ
polling; pooling removes the accidental connection-destruction fence that hides it.

**This is the whole thesis in one file:** we do not need to invent what "correct" looks like — it is fifty lines
above, with the comment explaining the exact use-after-free its sibling manufactures.

### 3.3 VIOLATED — no completion ownership AT ALL

| resource | site | why it is bad |
|---|---|---|
| **`payloadPublishTailBuffers[128]`** | unconditional **modulo** reservation, `remote_execution_peer_transport_rdma.c:743` | Sourced by DATA-tail **and** receiver-head-ACK WRs (`:10054`, `:10249`). **No per-slot `inUse`, no generation, no completion frontier.** The comment relies on "bounded outstanding discipline" — but the bounds are **per stream, not per connection**. A cursor lap overwrites a source word the RNIC is still reading -> **corrupt payload tail or consumed-head CREDIT**. |
| **`payloadFragmentHeaderBuffers[1024]`** | unconditional modulo reservation, `:760` | Registered header overwritten immediately before an unsignaled RDMA write (`:10008`). Pool size is the *only* protection. -> corrupt transport header, malformed payload, protocol reset. |
| **control REQUEST op / source buffer** | `:7825` (reached from `:10975`) | memsets the registered `requestMessage` **source** before its own send CQE is necessarily polled. Masked today only by an **unwritten RC-ordering argument**. Its "guard" (`:3707`) is **dead code — both branches return true**. |

Concurrency-dependent, so **inferred** today — but a pooled lane shared across time and sessions is exactly the
condition that makes a modulo cursor lap while a WR is outstanding.

### 3.4 BY-ACCIDENT — safe only via RC ordering or allocator shape

- **Remote-writable session MRs** (command mailbox `:23615`, completion mailbox `:23613`, peer completion ring
  `:23618`): the reset gate accepts `peerWritersQuiesced || connectionResetComplete` (`:23960`), and
  `peerWritersQuiesced` is set when the *close command is observed* (`:23945`) — which does **not** prove the
  **remote** writer's local CQE retired. Memory safety rests on RC write-before-doorbell ordering. **A late
  remote write into a deregistered MR poisons a POOLED QP** — where today it merely kills a dying connection.
- **Local receive payload/byte-ring MRs** (`:25065`): node A cannot observe retirement of node B's physical DATA
  CQE. The protocol makes it *likely* safe; nothing makes it *provably* safe.
- **Control RESPONSE slots** (`:3684`): freed only by their own CQE — but only because the allocator skips
  `inUse` (`:7950`). Emergent, with the generation decoded and discarded.
- **Payload stream slot + doorbell token** (`:26321`): the token generation DOES live outside resettable stream
  storage (`:3783`) and dispatch validates token + binding + connection generation + class (`:29647`), so a late
  event **cannot corrupt the new tenant**. But release does not prove old WIMM CQEs drained, and the punishment
  for a late event is a **connection-wide reset** (`:29671`) — which on a POOLED connection aborts unrelated
  sessions' work.

### 3.5 Latent API landmines (no production caller today)

- `TupleSinkServiceDeregisterPeerMemoryRegionRdma` (`:9010`) — `ibv_dereg_mr()` with **no** owner/quiescence
  check. Safety wholly delegated to callers.
- `TupleSinkServiceEnsureInlineWriteBuffer` (`:4420`) — growth deregisters and frees the old shared source with
  **no completion fence** (`:4458`).
- `HomerDpuByteRingUnbind` (`homer_dpu_byte_ring_pool.c:250`) — clears ownership with no in-flight/frontier
  check. Current callers happen to be safe; the API is not.

### 3.6 Violent-by-design (correct — do not "fix")

`TupleSinkServiceResetPeerConnectionInternal` destroys QP+CQs **before** deregistering MRs (`:5530`), and QP
destruction is explicitly the cutoff for outstanding one-sided WRs (`:4687`). This is **correct containment for
a real failure**. It is a **landmine only if used as the mechanism for clean pooled release** — which is exactly
what P7b.1 must avoid.

---

## 4. REMEDIATION PLAN — precise, per issue

> ## ⛔ SUPERSEDED. DO NOT READ THIS SECTION AS STATUS.
>
> Every "OPEN" below is **stale**. §4 was the FIRST draft of the plan; it was superseded by §7 (adversarial
> review), then §9 (the merged protocol of record), then by the per-item implementation specs and outcomes in
> §14–§29. **The P0 set is COMPLETE — see the status box at the top of this file.**
>
> §4 is kept because the *reasoning* is still instructive (three of these items were later refuted or
> redesigned, and the wrong turn is worth reading). It is a historical record, not a work list.

**Performance rule for every fix below: NO new round-trips, and NO new work on the hot data path.** Every
mechanism here is either (a) a counter/flag already touched on that path, or (b) a change to *when an existing
message is sent*, never a new message.

Ordering: **all P0 lands before P7b.1**, because P7b.1 removes the QP-destruction fence that hides them.

---

### P0-a — Peer-client completion publish: the owner is cleared while its WR may still be reading

**Scenario A.** `TupleSinkServiceClearPeerClientCompletionPublishCompletionsForSession`
(`tuple_sink_service_process.c:6604`) finds an `active` owner and clears it; session reset then deregisters the
source MR (`:23617`). Sibling `TupleSinkServiceClearClientSqlCommandWriteCompletionsForSession` (`:6550`) does
this correctly: it scans, and refuses while any owner is active.

**⚠ THE NON-OBVIOUS PART — "copy the exemplar" ALONE IS WRONG AND WOULD HANG THE SERVICE.**
`TupleSinkServicePeerClientCompletionPublishShouldSignal` (`:5560`) signals only on **ring pressure**
(`reservedSourceCount >= RING_SLOTS - 1`) and a **signal interval**. **It has NO terminal/close case.** So the
LAST completion publish of a session can be **unsignaled — no CQE is ever coming for it.** Sibling A can afford
`exit(1)` only because its close write is FORCE-SIGNALED (`:6638-6645`, comment: *"Force a CQE checkpoint so
cleanup-heavy paths do not leave only unsignaled command writes outstanding when the frontend command mailbox
is reset"*). **The missing forced signal IS the root cause of B's blind clear**, and it is why B "had" to
cheat. (Unsignaled != complete: the NIC still reads the source, so deregistering the MR is a real
use-after-free.)

**FIX — two parts, in this order:**

1. **`TupleSinkServicePeerClientCompletionPublishShouldSignal` (`:5560`): force a signal on the TERMINAL
   completion.** Mirror `TupleSinkServiceClientSqlCommandWriteShouldSignal`'s close case (`:6638`): if the
   completion being published is terminal for the session (`TupleSinkServiceCommandStateIsTerminal`, or kind ==
   `CLIENT_SQL_SESSION_CLOSE`), return `true`. **This creates the CQE that part 2 waits for.**
2. **`...ClearPeerClientCompletionPublishCompletionsForSession` (`:6604`): scan-and-refuse, like `:6570`.**
   Before the check, drain the send CQ (`TupleSinkServiceDrainTaggedSendCompletions` on the owner's
   `connectionHandle` — the entry already carries it). If any owner for this session is still `active`, do NOT
   clear it: **log each one and refuse the reset.**

**Refuse HOW?** Sibling A `exit(1)`s. Do the same here **only after part 1 makes retirement guaranteed** — with
part 1 in place, a still-active owner at reset means a real bug, which is exactly what we want to catch loudly.
**Do NOT ship part 2 without part 1**, or the service exits on every clean run.

**Fields:** none new. `TupleSinkServicePeerClientCompletionPublishCompletion` already has
`active`, `serviceSessionId`, `sourceSlotIndex`, `connectionHandle` (`:1592`).
**Perf:** one extra signaled WR **per session close** (cold path). Zero hot-path cost.
**Validation:** a clean pgbench run must show no `refusing to reset` line and no `exit(1)`; the terminal
completion must produce a CQE.

---

### P0-b — Control op: release on PHYSICAL retirement (this is P7b.0)

**Scenario A + B.** `TupleSinkServiceReleasePeerControlOp` (`remote_execution_peer_transport_rdma.c:7825`)
memsets the op slot — **including `requestMessage`, the registered RDMA SOURCE BUFFER** — at
**response-match** (`:10990`), while the request's own send CQE may be unpolled.

**FIX:**
- **New field** on `TupleSinkServicePeerControlOpState` (`:263`): `bool sendCompletionRetired;`
- **New phase**: `CITUS_REMOTE_EXEC_PEER_CONTROL_OP_RETIRING`.
- **Release requires BOTH, in whichever order they arrive** — the CQE usually arrives FIRST, so this cannot
  simply move to the CQE handler:
  - **response consumed** (`:10990`): if `sendCompletionRetired` -> memset (release). Else -> `phase = RETIRING`.
  - **send CQE** (`TupleSinkServiceRetireControlSendCompletion`, `:3699`): set `sendCompletionRetired = true`;
    if `phase == RETIRING` -> memset (release).
- **The allocator (`:7790`) already takes only `UNUSED` slots**, so a slot can never be reused with a
  completion outstanding. **No other change needed.**
- **The dead guard becomes real.** Today both branches `return true`. With the above, a generation mismatch
  means a CQE arrived for a slot already freed by someone else — **a genuine bug.** Make it a LOUD error, not
  `return true`. **DELETE the decoy comment** (it blames a blocking-adapter timeout path that has **no
  caller**).
- **Post-failure release (`:10644`) is unchanged**: `WOULD_BLOCK` posted no WR; `PARTIAL` resets the
  connection.

**This closes the control-RESPONSE-slot hazard (§3.4) BY CONSTRUCTION** — same rule, no separate generation
plumbing.
**Perf:** two branches on a control path. Zero hot-path cost.
**Validation:** the new LOUD assertion must never fire across the full workload set.

---

### P0-c — The staging pools have no ownership at all

**Scenario A.** `TupleSinkServiceReservePayloadPublishTailSlot` (`:743`) and
`...ReservePayloadFragmentHeaderSlot` (`:760`) hand out slots by **blind modulo cursor**
(`payloadPublishTailNextSlot`), with no `inUse`, no generation, no completion frontier. The comment admits it:
*"relies on the existing bounded outstanding-WR discipline before the ring wraps"* — **but those bounds are
PER STREAM, not per connection**, and the pool is per connection. Enough concurrent streams -> a lap -> the
cursor hands out a slot whose WR is still being read by the RNIC. **Corrupts a payload tail or a consumed-head
CREDIT.**

**FIX — and it needs NO new CQE plumbing, because these WRs ALREADY HAVE OWNERS THAT RETIRE:**

- the DATA-tail WIMM slot belongs to a **payload send owner** (`HomerServiceReservePayloadSendOwner`,
  `tuple_sink_service_process.c:16297`), which is retired at its CQE (`:16579`);
- the receiver-head ACK slot belongs to a **receiver-head ACK owner** (`:32463`), retired at `:32550`;
- the fragment headers are **unsignaled** but are chained to the batch's **signaled tail WIMM**, so they retire
  with the *same* owner.

So:
1. Add `bool inUse;` per staging slot (both pools).
2. **Record the slot index (and, for fragment headers, the first-index + count of the batch) IN THE EXISTING
   OWNER.** The owner structs already carry token/state/WR-count.
3. **Clear `inUse` when that owner retires** — at the CQE sites that already run (`:16579`, `:32550`).
4. **Reservation:** scan for a free slot; if none, **drain the send CQ and retry; if still none, LOUD ERROR.**
   This is exactly the pattern `controlResponsePublishSlots` already uses (`:7955`) — reuse it, do not invent.

**Perf:** one flag write on reserve, one on retire — both already-touched cache lines. **No allocation, no
round-trip, no extra WR.** The scan is over 128 / 1024 entries only when the pool is full, i.e. never in steady
state.
**Validation:** the "pool full" error must never fire; add a high-water-mark counter under the stats macro.

---

### P0-d — Forced arena unbind resets tenancy with DMA in flight

**Scenario D.** The 30-second forced-teardown deadline (`tuple_sink_service_process.c:43569`) calls
`TupleSinkServiceReleaseDpuArenaBinding` **without the T2 drain**. The DPU unbind
(`homer_service_dpu_dma.c:6353`) then READS `inFlightTaskCount != 0`, **PRINTS** *"a late completion can
re-poison the tenancy reset"*, and **resets anyway** (`:6394`).

The clean path already does it right: **T2 DRAIN** gates on
`HomerDpuDmaArenaSlotTasksInFlight(engine, bridgeGeneration, slotIndex, &inFlight)` **and**
`completionEventCount == 0` (`:43638`). **The predicate exists. The forced path just skips it.**

**FIX — two parts:**
1. **PRIMARY (owner: "fail fatally to catch our own bugs"):** the forced-teardown deadline must **not** bypass
   T2. If the drain has not converged by the deadline, that is **a bug in our drain, not a condition to paper
   over** -> **FATAL**, naming the slot, the ring, and the in-flight count. A hang we can see beats corruption
   we cannot.
2. **STRUCTURAL BACKSTOP (needed anyway for the crash path):** stamp the ring's **`tenancyGeneration`** into
   each arena DMA task. On completion, if the stamped generation is stale: **still retire the task's resources
   (free the `doca_buf`, decrement `inFlightTaskCount`) but DO NOT apply its frontier.** That is
   *attribution*, not *discard* — the completion is still fully handled by its rightful owner; only the
   write-back into a new tenancy is suppressed. **The mechanism already exists** for grouped-control reads
   (`homer_service_dpu_dma.c:4580` bumps it; `:12644` compares it) — extend it to arena ring tasks.

**Perf:** one `uint32` compare in the completion callback. Zero hot-path cost.

---

### P0-e — Dead-PID arena reaper: a HOST free with zero knowledge of DPU DMA  ⚠ worst blast radius

**Scenario D, crash variant — and the one place a handshake is IMPOSSIBLE: the backend is dead, there is
nobody to ask.** `HomerFrontendAgentReapDeadArenaSlots` (`homer_frontend_agent.c:689`) sees `kill(pid,0) ==
ESRCH`, **zeroes the slot bodies and publishes FREE** (`:666`) — while the DPU may still be DMA-ing into those
pages. Its safety argument (*"a backend crash drives a postmaster crash-restart, which kills every other
backend"*) is airtight **on the host** and **never mentions the DPU** — a separate machine, not restarted,
whose import of the `/dev/shm` arena stays valid.

**FIX — QUARANTINE, not free. Fail-SAFE (leak a slot) rather than fail-SILENT (corrupt memory):**

1. **New slot state `HOMER_FRONTEND_ARENA_SLOT_QUARANTINED`** (alongside FREE / BOUND).
2. The reaper **must not zero the bodies and must not publish FREE.** It publishes **QUARANTINED** and logs
   loudly (pid, slot, session). **A QUARANTINED slot is never handed out.**
3. **Only the DPU may certify it back to FREE**, because the DPU is the only party that can see
   `inFlightTaskCount`. It already computes exactly this via `HomerDpuDmaArenaSlotTasksInFlight`. The
   certification rides the **existing** DPU->host channel — **no new round-trip on any hot path** (this is a
   crash-recovery path that runs at most once per dead backend).
4. **FATAL if QUARANTINED slots exhaust the arena** (16 slots). That is the point at which it *is* our bug, and
   it fails loudly instead of quietly.

**Why not FATAL in the reaper itself?** Because our own runbook `kill -9`s surviving `remote exec backend`
processes after every `--homer-dpu-command` run — the reaper is on the ROUTINE cleanup path. Making it fatal
would crash the postmaster on every run. **Quarantine gives us a visible, bounded, non-corrupting failure;
FATAL is reserved for the point where quarantine actually runs out.**

**Open design point (needs the owner):** the exact DPU->host certification carrier. Candidates: the existing
spawn doorbell socket; a field in the arena the DPU already DMAs; or a setup-socket message. **Pick one before
implementing.**

---

### P1-f — Remote-writable MRs: an ack that means "I stopped issuing", not "my writes retired"

**Scenario C.** `TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister` (`:23958`) accepts
`peerWritersQuiesced || connectionResetComplete`, and `peerWritersQuiesced` is set when the **close command is
OBSERVED terminal** (`:23951`) — i.e. **logical**, not physical.

**The command mailbox is actually SAFE today, and it is worth saying WHY so nobody "fixes" it wrongly:** node
A's command writes to that MR all travel one RC QP, RDMA writes are delivered **in order** on a QP, and the
close is the **last** command — so observing the close proves every earlier write landed, and no further write
will be posted. **That is a real architectural guarantee, not luck. It is merely UNWRITTEN.** -> **Write it
down as an invariant + assert that nothing writes to that MR outside the ordered command stream.**

**The completion mailbox (`:23613`) and peer command-completion ring (`:23618`) are NOT covered by that
argument** — they are written by the **opposite** party, on the **opposite** direction, so "I sent my close"
proves nothing about the peer's in-flight completion writes. **Today only `connectionResetComplete` (the QP
fence) saves them. P7b.1 removes it.**

**FIX — change WHAT AN EXISTING MESSAGE MEANS. No new message, no new round-trip:**

> The peer's close **response** must not be sent until the peer's **own send CQEs for writes into my MRs are
> retired.** The peer CAN see its own CQEs (Scenario A). So `peerWritersQuiesced` becomes a *received* fact
> ("the peer certified physical retirement"), not a *locally inferred* one.

This is **Scenario C collapsing into Scenario A**: every actor retires its own work, and the ack then means
*physically retired*. **The CLOSE_SINK / close response already exists** — we are changing when it is sent, not
adding a message. **Zero extra round-trips.**

**Note the failure direction is SAFE:** a session that dies without a close leaves `peerWritersQuiesced` false,
so the gate **refuses to deregister** -> the MR **leaks** until connection reset. Leak, not corruption. Under
pooling that leak becomes unbounded, so it needs a bounded-quarantine escape hatch — **but it fails closed,
which is the right direction.**

---

### P1-g — A late payload WIMM must not reset a SHARED connection

**Scenario B.** The doorbell-token generation correctly prevents wrong-tenant mutation (`:29647`), but a
mismatch requests a **connection-wide reset** (`:29671`). On a per-session connection that only kills a dying
connection. **On a POOLED connection it aborts other sessions' live work.**
**FIX:** a stale-but-well-formed token (correct connection, known-stale generation) must be **counted and
dropped at the stream level**, not escalated to a connection reset. A *malformed* token stays a reset.
**Perf:** one branch. Zero cost.

---

### P1-h — Three latent API landmines

None have production callers today; **pooling adds callers.**
- `TupleSinkServiceDeregisterPeerMemoryRegionRdma` (`:9010`) — `ibv_dereg_mr()` with **no** owner check.
  **FIX:** assert no send owners reference the MR, or take an explicit "caller certifies quiescence" argument
  so the contract is at the signature.
- `TupleSinkServiceEnsureInlineWriteBuffer` (`:4420`) — growth deregisters/frees the shared source with **no
  fence** (`:4458`). **FIX:** drain the send CQ before the swap, or refuse to grow while any unsignaled write
  sources from it.
- `HomerDpuByteRingUnbind` (`homer_dpu_byte_ring_pool.c:250`) — clears ownership with no in-flight check.
  **FIX:** assert the stream's reclaim facts (which its current callers already prove) at the API, not in the
  callers.

---

### P2-i — `HomerDpuDmaDestroy`: drain on CLEAN exit  (Scenario E)

`homer_service_dpu_dma.c:1179` says drain-before-destroy is **unimplemented**, and it is called on **clean**
service exit (`tuple_sink_service_process.c:47985`).
**FIX:** on clean exit, progress the PE until the **global** outstanding-task count is zero (bounded, with a
FATAL on non-convergence), then destroy. **Violent teardown on a FAILURE exit stays exactly as it is — that is
Scenario E used correctly.**

### P2-j — Grouped-control reads outliving tenancy  (Scenario B, accepted)

Correct today: the read lands in **private DPU staging** and its stale result is rejected by generation
(`:12644`). **FIX: none.** **Write the invariant down** — *"safe only while the staging buffer is private and
never pooled"* — and add a **tripwire** that fires if that buffer is ever shared or pooled.

---

### 4.1 What P7b.1 may then do

Only after P0: a clean host-export detach **RELEASES** the connection (owner -> 0, QP intact) instead of
resetting it. **It must NOT use `TupleSinkServiceResetPeerConnection` as the release mechanism** — that is
Scenario E (violent, correct for failure only), and using it for clean release is precisely the substitution
this whole audit exists to end.

## 5. RULES EARNED (all owner-driven)

- **Retire before you release.** "My response came back" is not "my work is done" — the completion for the
  request *you* posted is still yours. Own it to the end.
- **Before you may call something stale, NAME WHO WAS WAITING FOR IT.** This rule did not merely prevent a bug;
  it *found the drain that already existed* (`payloadSendOwnerOutstandingWrs`, `receiverHeadAckOutstandingCount`)
  and stopped us bolting a weaker, redundant mechanism on top of two correct ones.
- **Remove the category, don't guard the case.** A guard makes the wrong thing *safe*; an invariant makes it
  *unrepresentable*. A guard is somewhere a future change can walk past; an impossibility is not.
- **A guard that lies is worse than none** — the twin of CLAUDE.md's rule about diagnostics. **Check that a
  guard's branches actually DIFFER** before trusting it as evidence of a considered design.
- **A quiescence predicate must be REACHABLE.** "Outstanding WRs == 0" is *unreachable* on a connection with
  permanently-posted receives (depth 64, reposted on every consumption). An unreachable gate is a hang, and it
  looks exactly like a slow one.
- **"Is it drained?" is ambiguous in a layered transport.** Three different claims: (1) the byte ring is empty
  (`publishedTail == consumedHead` — all `TupleSinkServiceQueueIsDrained` actually proves); (2) the stream is
  semantically closed; (3) the transport has no outstanding tenant WRs. **Name which one you mean, every time.**
- **Distinguish a live BUG from a live TRAP, and say which you have.** Overclaiming harm to justify a change is
  its own failure; the honest justification here is stronger because it survives scrutiny.
- **An invariant that holds only because of how the code currently happens to be arranged is a coincidence, not
  an invariant** — especially when a change is about to extend an object's lifetime.
- **A safety argument must cover every machine in the system.** V1's argument ("a backend crash drives a
  postmaster crash-restart, which kills every other backend") is true *on the host* and simply does not mention
  the DPU — which is a separate machine, is not restarted, and whose import of the host's `/dev/shm` arena
  remains valid. **The scariest safety arguments are the ones that are locally airtight.**

---

## 6. CORRECTIONS TO OTHER DOCS (found during this audit)

- **CLAUDE.md says the frontend arena is `citus_homer_frontend_arena_v2`. It is `_v3`**
  (`homer_frontend_agent.h:96`). Fix before it misleads a clean-baseline procedure.
- The starvation probe's `inflight=` counter is a **class-wide total**
  (`tuple_sink_service_process.c:43809`), **not** the reclaim predicate. Reclaim uses the relevant import's own
  `inflightTaskCount` (`homer_service_dpu_dma.c:9563`). Do not read one as the other.

---

## Related

- `dpu_crossnode_command_completion_bridges_design.md` §25–§29 — the derivation, the consumer inventory, and
  the control-op finding that opened this audit.
- `selected_dpu_session_rings_and_lifecycle.md` §6b — the contract as a standing rule (architecture of record).

---

## 7. ADVERSARIAL REVIEW — NINE DEFECTS IN THE PLAN ABOVE (July 12, 2026)

The plan in §4 was reviewed adversarially and **it was wrong in nine places**, two of them severe enough to have
broken correct runs. Corrections below **SUPERSEDE** the corresponding text above. Both sides are kept, per KB
convention: the reasoning that produced the error is as instructive as the fix.

### 7.1 THE MODEL (§0.3) — three corrections

**(a) "Four scenarios" is five, and D is not distinct from C.** Both C and D answer *"can I observe their
completion?"* with **no**. The real axis is **completion OWNERSHIP**, not the machine boundary. D is C with the
owner on another machine — an *operational* difference (who certifies, and can I reach them), not a taxonomic
one. Keep the labels, but stop treating "cross-machine" as its own failure mechanism.

**(b) SCENARIO E IS NOT RETIREMENT — IT IS CANCELLATION.** My claim that destruction *"makes further
completions impossible"* is **too strong and materially misleading**. Reset destroys the QP/CQs and then
**synthetically ABORTS software owners** — `TupleSinkServiceAbortConnectionSoftwareOwnersOnReset`
(`remote_execution_peer_transport_rdma.c:4278`) **explicitly records the completed/waiting operations it
DISCARDED** (`:4290`). So E does not *satisfy* the contract; it is a **failure-containment EXCEPTION** to it:
cancel, cut off, reconcile. Successful completions are **lost**, not retired.

**(c) "EVERY violation is E standing in for the others" IS FALSE.** Several violations occur **while the QP and
engine are fully alive** and have nothing to do with pooling:
- the staging-pool lap (`:743`, `:760`) — **reachable TODAY with enough concurrent streams**;
- control-op release — masked by **RC ordering**, not by QP destruction;
- forced arena unbind (`homer_service_dpu_dma.c:6353`) — the engine stays live;
- the dead-PID reaper (`homer_frontend_agent.c:689`) — the DPU stays live.

**This RAISES the urgency:** some of these are **live bugs now**, not merely pooling landmines. The honest claim
is: *E has been MASKING several lifetime bugs (chiefly the MR deregistrations and remote-writer cases), and
pooling removes that mask — but it is not the universal cause.*

**(d) The model omits ABORT/CANCELLATION ownership.** A local send CQE can report **failure**
(`remote_execution_peer_transport_rdma.c:4044`), which is neither normal A-retirement nor E. It is
**A transitioning into explicit abort reconciliation**, and the model must name it.

### 7.2 P0-a — TWO SERIOUS ERRORS. Do NOT implement as written.

**(a) MY PREMISE WAS TOO STRONG — an existing retirement mechanism was omitted.** A per-lane FIFO
(`TupleSinkServiceRetirePeerClientCompletionPublishCompletionsThrough`, `tuple_sink_service_process.c:5934`)
lets a **later signaled** publication **cumulatively retire all preceding owners** on that lane. So *"no CQE is
ever coming"* holds **only** when no later signaled publication occurs — i.e. for a **short or final** session.
The defect is real; my description of it was not.

**(b) ⚠ THE PROPOSED PREDICATE IS A HOT-PATH REGRESSION.** I wrote "terminal for the session
(`TupleSinkServiceCommandStateIsTerminal`...)". **`TupleSinkServiceCommandStateIsTerminal` means `COMPLETED ||
FAILED` (`:17927`) — i.e. EVERY completed SQL command**, not session close. Using it would force a signaled WR
**on every command** — in a plan whose own rule is "no new hot-path work".
**CORRECTION: key the forced signal ONLY on `commandKind == CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`**,
exactly as the command-write path does (`:6638`).

**(c) ⚠⚠ `exit(1)` IS NOT SAFE, EVEN WITH THE FORCED SIGNAL. IT WOULD KILL CORRECT RUNS.** A **signaled** post
can legitimately still be `active` at reset — **the CQE simply has not been polled yet.** CQ retirement is
asynchronous; the lane FIFO exists *because* of that. A single non-blocking drain followed by `exit(1)` will
therefore crash a **healthy** run.
**CORRECTION: DEFER, do not die.** If any owner is still active: **do not clear, do not reset — retry on a
later pass.** FATAL **only** after a bounded deadline (non-convergence is then a real bug). This is the same
trap I warned about and then walked into.

**(d) Omission:** a **partial post** deliberately leaves a source slot `FAILED_OR_INFLIGHT` because no final
WIMM CQE will retire it (`:19043`). **A terminal checkpoint does NOT repair an already-partial chain** — that
case needs its own treatment.

### 7.3 P0-b — the "closes the response-slot hazard by construction" claim is WRONG

`controlOps[]` and `controlResponsePublishSlots[]` are **independent source pools with separate CQE tags and
separate retirement branches** (`:3664`). **Fixing request-op lifetime does not touch response-slot lifetime.**
The response slot needs its **own** treatment (either the same retire-before-reuse discipline, or the
generation check §27 proposed). My "by construction" was a conflation.

**Also:** the reset's abort classifier (`:4290`-`:4315`) does not know a `RETIRING` phase and would file it
under a default/unknown-phase branch. It must be **taught** the new phase, so "post-failure behaviour is
unchanged" is not true as written. (The good news, verified: the abort path *does* clear every non-`UNUSED`
phase, so a `RETIRING` op does **not** leak on connection failure.)

### 7.4 P0-c — the premise holds, but the plumbing does not exist

**Verified good news:** every *current production* poster of a tail slot or fragment-header slot **does** go
through a tracked owner. No control, smoke, EOS-only, or unowned caller exists today. The premise stands.

**But the proposed implementation cannot be written as described:**
- **Slot selection happens INSIDE the transport helpers, AFTER the service owner is reserved, and the helpers
  DO NOT RETURN the slot indices** (`:9676`, `:9890`). So "record the slot index in the existing owner" is
  **impossible without changing that interface.** The helpers must return the indices (or take an owner handle).
- `TupleSinkServicePostPeerUint64WithImmediateResultRdma()` **publicly supports `signaled=false`**, in which
  case the tail slot has **no tagged owner** at all (`:10214`, `:10265`). No production caller today — but the
  *general* ownership rule I asserted is **false for the API as written**.
- **`ibv_post_send()` can PARTIALLY accept a chain** (`HomerPeerPostResult`, `:10080`). Any slot-owner scheme
  **must keep ownership of the ACCEPTED prefix's slots** until the connection-reset cutoff. Clearing all
  recorded slots as though the batch never posted **recreates the original violation.**

### 7.5 P0-d — MY SPEC WAS BADLY INSUFFICIENT (the largest substantive defect)

I wrote "retire the task's resources but do not apply its frontier". **Arena task retirement has far more
tenant-sensitive side effects than a frontier**, verified at `homer_service_dpu_dma.c:10565`ff:
- it clears **per-ring in-flight FLAGS** — `controlReadInFlight`, `commandPullInFlight`, `byteRingPullInFlight`,
  `byteRingCreditInFlight`, `byteRingWriteInFlight` — which after a tenancy flip **belong to the NEW tenant**;
- it sets `controlSnapshotValid`, `lastCompletedTaskGeneration`, `lastCommandBufferIndex`, publication epochs;
- the **success callback can ARM A SUCCESSOR TASK** (`HomerDpuDmaSubmitPreparedDpuToHostPublish`, `:14500`);
- and `HomerDpuDmaResetRingTenancyState` **preserves `inFlightTaskCount`** while bumping `tenancyGeneration`
  (`:4521`).

**CORRECTED SPEC — partition the retirement side effects into THREE classes:**

| class | example | on a STALE generation |
|---|---|---|
| **ENGINE-OWNED** (the task's own resources) | `doca_buf`, `groupedControlBufferInUse[i]`, `inFlightTaskCount` | **ALWAYS apply** — this IS "retire your own work" |
| **TENANT-SCOPED** (belongs to whoever owns the ring NOW) | `*InFlight` flags, `controlSnapshotValid`, `lastCompletedTaskGeneration`, accepted frontiers/epochs | **NEVER apply** — would corrupt the new tenant |
| **SUCCESSOR ARMING** | `SubmitPreparedDpuToHostPublish` | **SUPPRESS** — never arm an old chain into a new tenancy |

That partition is the actual spec. "Suppress the frontier" was one third of one row of it.

### 7.6 P0-e — framing error

*"The backend is dead, so there is nobody to hand-shake with"* is **FALSE**. The **completion-owning actor is
the DPU, and the DPU is alive.** The problem is **not** that the actor is gone — it is that **no certification
path from the DPU to the host reaper exists**. (The plan then relies on the DPU certifying, which quietly
contradicts my own framing.) The QUARANTINE design stands; the justification was wrong. And because the carrier
is still unpicked, **the "no new round-trip" claim for P0-e is UNVERIFIED**.

### 7.7 P1-f — I named the WRONG MESSAGE

**Verified good:** the command-mailbox "safe today" claim **holds**. Only
`TupleSinkServicePostRemoteClientSqlCommandRecord` (`:21143`) writes through that descriptor; all variants use
the session's single `clientSqlPeerConnectionHandle`; no credit/completion/payload/alternate-class writer
targets it. RC in-order delivery + close-is-last is a real guarantee.

**But:** `CLOSE_SINK` is the **payload-stream** protocol, while the session command/completion MRs live on the
**client-SQL critical-control** lifetime. **Those are two different lifetimes and I conflated them.** The
existing session-level message that can certify the opposite-direction completion writers **has not been
identified**. That must be done before P1-f is implementable.

### 7.8 P1-g — MY FIX CONTRADICTS THE CONTRACT, in the document that states it

I proposed that a known-stale payload WIMM be *"counted and dropped at the stream level"*. **That is exactly the
prohibited operation.** The WIMM's consumer advances **sender credit and close state**
(`HomerServiceApplyPayloadSenderCreditDoorbell`, `:29572`) — **someone may still be waiting on it.** Calling it
"known stale" and dropping it **abandons its consumer**, which is the precise thing §0's corollary forbids.

**CORRECTED FIX — ATTRIBUTE, never discard:** on a stale-but-well-formed token, **look up the OLD stream**. If
it still exists, **apply the credit to IT**. Only if the old stream is **provably fully reclaimed** (i.e. it is
proven that nobody awaits that credit) may it be counted and dropped — and that proof is precisely *"name who
was waiting for it"*. A **malformed** token remains a connection reset.

### 7.9 Omission — the sender-head mirror MR LEAKS under pooling

Marked HOLDS (correctly, for *safety*): deregistration is deliberately deferred because a late consumed-head ACK
can still arrive (`:25071`). **But stream reset then memsets the handle away while the MR stays linked to the
connection** (`:25130`, `remote_execution_peer_transport_rdma.c:4204`). On a **long-lived pooled QP**,
registrations and storage **accumulate until connection reset**. Safe, but not lifecycle-complete — and pooling
is what makes it unbounded.

### 7.10 "HOLDS" entries needing qualification

- **Host export destroy:** the client's `CLOSE_ACK` proves the DPU reached its close *point*; the DPU's own
  reclaim separately checks import/task/frontier state (`homer_service_dpu_dma.c:9526`). **The ACK alone is not
  the complete physical-retirement proof** — the pair is.
- **Notification RECV slots:** hold for the *connection's* lifetime, but they do **not** make *stream* reuse
  safe — a late old WIMM still hits the mismatch/reset path (`:29671`). See §7.8.
- **Grouped-control reads (P2-j):** safe because the task's private staging is retired by the **engine** after
  generation rejection. **The correct classification is that the ARENA TENANCY IS NOT THE OWNER — the DPU ENGINE
  IS.** Stated that way it is not an exception to the contract at all; stated my way it was one.

### 7.11 Rules earned

- **Ask for disagreement explicitly, and tell the reviewer not to defer to your framing.** This review found a
  proposed fix that would have **crashed correct runs** (`exit(1)`), one that would have added a **signaled WR
  per command** on the hot path, and one that **violated the contract the very document defines**. None of the
  three would have been caught by a reviewer trying to be agreeable.
- **A predicate's NAME is not its MEANING.** `CommandStateIsTerminal` sounds like "the session ended"; it means
  `COMPLETED || FAILED` — every command. **Read the body before you build a plan on the name.**
- **When you propose "suppress X on a stale generation", ENUMERATE EVERY SIDE EFFECT OF THAT PATH FIRST.** A
  retirement callback is rarely one assignment; here it was flags, buffers, epochs, and a successor submission.
- **Destruction is CANCELLATION, not retirement.** A teardown that abandons owners and *records what it
  discarded* is not satisfying the contract — it is declaring an exception to it. Never let it stand in as proof.

---

## 8. P0-d SUPERSEDED — the "until" condition was MISSING, and the guard was the wrong layer

**Owner, on the three-way partition (§7.5):** *"I don't see the 'until condition' of 'don't leave until you have
retired your own work'. If a tenant truly only leaves until it retires all its own work, we will never be in a
situation where we see a stale generation, no? The point is, we should NOT see a stale thingy. The whole fix
here is to uphold this principle?"*

**Yes. And that invalidates BOTH my §7.5 partition AND the code's existing generation guard.** Both are
mechanisms to *survive* a stale completion. Under the contract, a stale completion is **impossible**. I was
building a better guard for a category I should have been deleting — the exact anti-pattern §0.3 and §5 name.

### 8.1 The code already tells the whole story — including the wrong turn

`homer_service_dpu_dma.c:4533`:

> *"⚠ THE RESET ALONE IS NOT ENOUGH, and run 41k proved it. A grouped-control READ that was already in flight
> when we reset completed 5 ms later carrying the OLD tenant's line, and wrote accepted_epoch=5 straight back
> into the runtime we had just cleared -- after which the (correctly zeroed) host line read as a REGRESSION and
> killed the engine. **Forgetting is not sufficient when something can still remind you.** Hence the tenancy
> generation: we bump it here, every read stamps it, and a completion whose stamp no longer matches is
> **DROPPED**."*
>
> *"The old note here claimed T1 quiesce + T2 drain made in-flight tasks impossible. **That was WRONG -- they
> quiesce the ring's own pull/publish work, not the engine's grouped-control discovery reads, and the unbind's
> `inFlightTaskCount` warning never fired because those reads are NOT COUNTED against the ring.**"*

**We hit this exact bug (run 41k), diagnosed it correctly, and then fixed the WRONG LAYER.** The comment states
the root cause outright: **the drain does not count all the work.**

### 8.2 THE MISSING "UNTIL" CONDITION

`HomerDpuDmaArenaSlotTasksInFlight` (`:6246`) sums `ringRuntime[...].inFlightTaskCount`. **Grouped-control
discovery reads are never counted into it** (`:4533`). So a tenant can PASS the drain and LEAVE while work
touching its ring is still in flight. **"Retire all your own work" was never actually enforced — the accounting
could not see the work.**

### 8.3 THE REAL FIX (supersedes §7.5 and §4/P0-d)

1. **T1 quiesce must stop EVERY source of new work on the ring** — including the **engine's grouped-control
   discovery**, not merely the ring's own pull/publish. (Today discovery ignores the quiesce, so new reads keep
   being armed and the count could never settle.)
2. **`inFlightTaskCount` must COUNT grouped-control reads**, so T2's drain actually covers them.

With both, the drain **converges** (nothing new is armed; in-flight DOCA tasks always complete, success or
error, because the ENGINE is alive and independent of the backend), the tenancy flips with **ZERO outstanding**,
and **a stale generation becomes IMPOSSIBLE**.

Consequences — all subtractive:

- **The tenancy generation STAYS, but becomes a LOUD ASSERTION that must never fire**, not a mechanism. If it
  fires, the contract was broken, and it names the ring.
- **The §7.5 three-way partition EVAPORATES.** It existed only to survive stale completions.
- **The `DROPPED` path goes away** — and good riddance: it was never even complete. A "dropped" grouped-control
  completion still cleared the **NEW** tenant's `controlReadInFlight` flag (`:10565`). The guard did not
  actually guard.

### 8.4 This also resolves the forced-teardown deadline cleanly

The 30 s deadline exists because **T3/T4 wait on the BACKEND** to ack `BACKEND_SLOT_RELEASE` — and a dead
backend never will. But **DMA tasks do not depend on the backend at all**; they complete regardless, because
the **engine** is alive.

> **The deadline may legitimately ABANDON THE BACKEND HANDSHAKE. It may NEVER abandon the DMA DRAIN.**
> The drain is an **unconditional precondition of unbind, on every path**. FATAL only if the DRAIN ITSELF fails
> to converge — which would mean the DOCA engine is broken, i.e. a real bug we want to see.

**Same logic fixes P0-e:** the DPU's tasks drain fine even with the backend dead, so QUARANTINE -> DPU drains ->
DPU certifies zero -> slot goes FREE. No stale completion can ever reach a new tenant, because **the slot is not
handed out until the drain says zero.**

### 8.5 The rule this earns

> **When you find yourself designing a mechanism to SURVIVE a stale reference, the real bug is almost always
> that SOME WORK IS NOT BEING COUNTED.**

Run 41k produced a *correct diagnosis* — *"forgetting is not sufficient when something can still remind you"* —
and then reached for a **generation stamp** instead of asking **why the drain let us leave in the first place**.
The answer was **one uncounted task class**. The guard cost a generation field, a stamp on every read, a compare
on every completion, and a `DROPPED` path that was itself incomplete — **and it left the category alive**, to be
rediscovered here.

**Corollary:** a quiesce that does not stop *every* producer of new work on a resource is not a quiesce, and a
drain that does not count *every* consumer is not a drain. **Enumerate the producers and the consumers, or the
gate is theatre.**

---

## 9. THE PROTOCOL — the merged, step-by-step plan of record

**Owner, July 12, 2026 — this supersedes the per-item ad-hoc fixes in §4/§7 and is the plan of record:**

> **1. DECIDE** — realize I need to leave.
> **2. QUIESCE** — stop new work: **mine AND everyone else's**. *Every source of new work.*
> **3. DRAIN** — finish and drain what remains, **until there is none left**.
> **4. RELEASE** — only now: final cleanup, and actually leave.
>
> **THE ALARM: if we find ourselves needing to handle STALENESS, that is a BUG in phases 2-3, not a case to
> handle.** A stale reference is only reachable if someone left while work was still outstanding.

### 9.1 THE ALARM RUN BACKWARDS — it is an AUDIT TOOL, and it finds exactly our list

Every existing staleness handler in the codebase should point at a missing phase. It does:

| existing staleness handler | the missing phase |
|---|---|
| arena **tenancyGeneration** + `DROPPED` (`homer_service_dpu_dma.c:4533`, `:12644`) | **2 and 3** — quiesce does not stop grouped-control discovery; the drain does not COUNT it |
| payload **doorbell-token generation** -> connection reset (`tuple_sink_service_process.c:29647`, `:29671`) | **3** — the peer never certifies its writes are retired |
| control-op **generation guard** (`remote_execution_peer_transport_rdma.c:3699`, **dead code**) | **3** — the send CQE is never waited for |
| staging pools — **no handler at all** (`:743`, `:760`) | **3** — nothing is counted, so nothing can be waited for |
| **sender-head mirror MR** kept registered "because a late ACK can still arrive" (`:25071`) | **3** — the SAME missing certification as the doorbell token |
| control-**RESPONSE** slot, freed only by its own CQE (`:3684`, `:7950`) | **none — this one ALREADY follows the protocol** |

**The rule finds exactly the audit's list and nothing else.** It also shows the response slot is **already
correct**, so **§27's proposed generation check was solving a non-problem** — the right action there is an
ASSERT that nothing else may clear `inUse`, not a generation.

**Two items COLLAPSE:** the doorbell-token staleness and the mirror-MR leak are the **same** missing peer
certification. Fix phase 3 for the peer once and **both disappear**.

### 9.2 A key distinction the protocol forces (and which we had blurred)

**What phase 3 must prove depends on what the resource IS to the actor:**

- **A SOURCE buffer** (the NIC reads it): I need **"my own CQE is retired"**. Only I can see it.
- **A TARGET MR** (someone writes into it): I need **"no further write will ARRIVE"**. I do **not** need the
  writer's CQE — that is the writer's own phase 3, for the writer's own source.

This is why the **command mailbox is genuinely safe today**: RC delivery is in-order on one QP and the close is
the last command, so observing the close **proves no further write will arrive**. That is a real phase-3 proof,
not luck. **Write it down; do not "fix" it.**

### 9.3 THE PER-ITEM MERGED PLAN

---

#### P0-a — peer-client completion publish (source MR deregistered under a live WR)

| phase | what |
|---|---|
| **1 DECIDE** | session close observed (exists) |
| **2 QUIESCE** | **NEW:** set `completionPublishQuiesced` on the session; refuse any new completion publish |
| **3 DRAIN** | **The blocker: the last publish may be UNSIGNALED, so no CQE is coming.** A lane FIFO (`:5934`) retires cumulatively **only** when a LATER signaled publish occurs — and after quiesce there is none. **So the drain must FINISH the work, not merely wait for it** (the owner's phase 3 is "finish AND drain"): **force a signal on the terminal publish**, keyed **ONLY** on `commandKind == CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE` — **NOT** `TupleSinkServiceCommandStateIsTerminal` (`:17927`), which means `COMPLETED\|\|FAILED`, i.e. **every command**, and would put a signaled WR on the hot path. Then poll the send CQ and **wait for `active == 0`, retrying across passes.** |
| **4 RELEASE** | clear owners, deregister the MR, reset the session |

**No `exit(1)`** — a signaled post can legitimately still be un-polled at reset (CQ retirement is async).
**FATAL only on bounded non-convergence.** The blind clear at `:6604` disappears.
**Also:** a **partial post** leaves a slot `FAILED_OR_INFLIGHT` (`:19043`) that a terminal checkpoint does NOT
repair — that slot is owned until the connection-reset cutoff (Scenario E), and phase 3 must say so.

---

#### P0-b — control op (source buffer memset before its CQE)

| phase | what |
|---|---|
| **1 DECIDE** | the response is matched |
| **2 QUIESCE** | n/a — a single WR; there is no new work to stop |
| **3 DRAIN** | **wait for the request's own send CQE.** It is always signaled (`:9602`), so it always arrives (or the connection fails -> Scenario E). |
| **4 RELEASE** | memset the slot |

**Fields:** `bool sendCompletionRetired;` + phase `..._OP_RETIRING` on `TupleSinkServicePeerControlOpState`
(`:263`). Release fires from **whichever of {response consumed, CQE retired} happens LAST** — the CQE usually
arrives FIRST, so it cannot simply move to the CQE handler. The allocator (`:7790`) already takes only `UNUSED`.
**The dead guard (`:3699`) becomes the real retirement site + a LOUD assertion.** Delete its decoy comment.
**TEACH the reset abort classifier (`:4290`-`:4315`) about `RETIRING`** (verified: it clears every non-`UNUSED`
phase, so no leak — but it would misfile it as unknown).
**NOT closed by this:** the response slot is an independent pool. Per §9.1 it **already follows the protocol** —
so **ASSERT** that only its own CQE may clear `inUse`. No generation needed.

---

#### P0-c — staging pools (no accounting at all).  ⚠ LIVE BUG TODAY, not just a pooling landmine

Reuse **is** "someone left": the previous WR must have retired before the slot is handed out.

| phase | what |
|---|---|
| **1 DECIDE** | a reservation is requested |
| **2 QUIESCE** | n/a |
| **3 DRAIN** | **the reservation must not hand out a slot whose WR has not completed.** Add per-slot `inUse`. If the pool is full: **drain the send CQ, retry, and LOUD ERROR if still full** — exactly the pattern `controlResponsePublishSlots` already uses (`:7955`). Reuse it; do not invent. |
| **4 RELEASE** | clear `inUse` **at the owner's existing retirement site** (`:16579` payload send, `:32550` ACK) |

**IMPLEMENTATION GAP (from the review):** the helpers **select the slot INSIDE and do not return the index**
(`:9676`, `:9890`). **The API must change** — the helper takes/returns the owner so it can record the slot(s).
For fragment headers, record **first-index + count** (they are unsignaled but chained to the batch's signaled
tail WIMM, so they retire with the same owner).
**Two edges the review caught:** `PostPeerUint64WithImmediateResultRdma()` supports `signaled=false` (tail slot
with **no owner** — no production caller, but the API allows it: **forbid it or give it an owner**); and
`ibv_post_send()` can **partially accept** a chain (`:10080`) — the **accepted prefix keeps its slots owned**
until the reset cutoff. Clearing them as if nothing posted **recreates the original bug**.

---

#### P0-d — arena tenancy (SUPERSEDES §7.5's three-way partition entirely)

| phase | what |
|---|---|
| **1 DECIDE** | teardown begins (T1) |
| **2 QUIESCE** | **THE MISSING PIECE.** Today T1 quiesces the ring's own pull/publish but **NOT the engine's grouped-control discovery reads** (`:4533`). **Quiesce must stop EVERY producer on that ring.** |
| **3 DRAIN** | **`inFlightTaskCount` must COUNT grouped-control reads** — today they are excluded (`:4533`), which is exactly why the drain could pass with work outstanding. Then wait for zero. **It converges**: nothing new is armed, and in-flight DOCA tasks always complete (success or error) because the **engine** is alive and independent of the backend. |
| **4 RELEASE** | unbind, reset tenancy |

**Consequences, all subtractive:** stale becomes **impossible**; `tenancyGeneration` survives **only as a LOUD
ASSERTION that must never fire**; the three-way partition **evaporates**; the `DROPPED` path goes away — and it
never worked anyway (a "dropped" completion still cleared the **NEW** tenant's `controlReadInFlight`, `:10565`).

**The 30 s deadline:** it exists because **T3/T4 wait on the BACKEND**, which may be dead. **DMA tasks do not
depend on the backend.** So the deadline **MAY abandon the backend handshake; it MAY NEVER abandon the DMA
drain.** The drain is an **unconditional precondition of unbind on every path.** FATAL only if the DRAIN fails
to converge — which would mean the DOCA engine is broken, i.e. a real bug we want to see.

---

#### P0-e — dead-PID arena reaper

This is simply **phases 2-4 executed by the DPU, with the host WAITING.** The backend is dead, but **the DPU is
ALIVE and its tasks drain fine** — the actor exists; there is merely no certification path.

| phase | what |
|---|---|
| **1 DECIDE** | the host reaper sees `kill(pid,0) == ESRCH` |
| **2 QUIESCE** | the host **cannot** quiesce the DPU — so it must **NOT PROCEED**. It publishes **`QUARANTINED`** (new slot state) and **does not zero the bodies**. A QUARANTINED slot is **never handed out**. |
| **3 DRAIN** | the **DPU** quiesces and drains that slot's rings (its own P0-d machinery), then **CERTIFIES zero** |
| **4 RELEASE** | only on that certification does the slot become `FREE` |

**FATAL only when QUARANTINED slots exhaust the arena** (16) — that is the point at which it *is* our bug.
**Not fatal in the reaper**: it is on the ROUTINE cleanup path (our own runbook `kill -9`s backends every run),
so a FATAL there would crash the postmaster on every run. **Fail-SAFE (leak a slot), never fail-SILENT.**
**OPEN — needs a decision:** the DPU->host certification carrier (spawn doorbell / an arena field the DPU
already DMAs / the setup socket). **Until picked, the "no new round-trip" claim is UNVERIFIED.**

---

#### P0-f — THE CERTIFIER CANNOT SAY "I DON'T KNOW".  ⚠ MANDATORY, independent of every other item

**This is not part of P0-e — it is a live defect in a safety primitive, and P0-e (Option A) does not even use
it. Every OTHER caller does.**

`HomerDpuDmaArenaSlotTasksInFlight()` (`homer_service_dpu_dma.c:6268`) is the primitive that answers *"is this
arena slot's DMA drained?"* — i.e. it is the **phase-3 certifier** for Scenario D. It returns
`bool` + `*inFlightOut`, and:

- `HomerDpuDmaFindArenaSlotImport()` (`:5894`) requires an **ACTIVE import whose `bridgeGeneration` matches
  EXACTLY** (`:5917`);
- when that lookup **FAILS**, `ArenaSlotTasksInFlight` **returns `true` with `*inFlightOut = 0`** (`:6280`).

> **"NOT FOUND" is therefore INDISTINGUISHABLE from "FOUND AND DRAINED".**

After an agent restart mints a new `bridgeGeneration` (`homer_frontend_agent.c:1529`) while old BOUND slot state
survives in the shared arena, the query **misses the old import and silently reports DRAINED** — certifying as
safe a slot whose DMA state is **unknown**.

`HomerDpuDmaUnbindRingSession()` (`:6303`) has the **same shape**: a missing import is treated as idempotent
success (`:6327`).

**This is THE BUG FAMILY — a missing thing read as a definite negative — sitting INSIDE the primitive whose
entire job is to certify safety.** It is the same defect as `ownerSession == NULL` meaning "not clean" (§20.1)
and `clean_close=1` being computed and ignored (§25.2), except here the false negative **grants permission to
reuse host memory**.

**THE FIX — make the unknown state REPRESENTABLE, then force every caller to answer for it:**

```c
typedef enum
{
    HOMER_DPU_ARENA_SLOT_DRAINED = 0,   /* found, inFlight == 0  -- SAFE */
    HOMER_DPU_ARENA_SLOT_IN_FLIGHT,     /* found, inFlight  > 0  -- NOT SAFE */
    HOMER_DPU_ARENA_SLOT_NOT_FOUND      /* lookup failed         -- WE DO NOT KNOW. NEVER "safe". */
} HomerDpuArenaSlotDrainState;
```

- **Every caller must handle `NOT_FOUND` EXPLICITLY at the call site.**
- A caller doing **idempotent cleanup** may still choose to proceed on `NOT_FOUND` — but it must say so, in a
  comment, deliberately.
- A caller using it as a **SAFETY GATE** must treat `NOT_FOUND` as **UNSAFE**. There is no third option.
- **`bool` is the wrong return type for a question with three answers.** That is how the bug got in.

**Caller enumeration is in flight** (adversarial review) — the open question is whether this is **already
biting us** somewhere unrelated to P0-e. Fold the result in before implementing.

**Rule:** *a predicate that cannot express "I don't know" will lie, and it will lie in the direction of
"yes" — because `0` is the value a failed lookup leaves behind.*

#### P1-f — remote-writable MRs (and it SUBSUMES P1-g and the mirror-MR leak)

**Command mailbox: ALREADY CORRECT** (§9.2) — RC in-order on one QP + close-is-last proves *"no further write
will arrive"*, which is the right phase-3 proof for a **target** MR. **Verified single writer** (`:21143`), one
connection handle, no credit/completion/payload/alternate-class writer. **Write the invariant down + assert it.
Do not change it.**

**Completion mailbox (`:23613`) and peer completion ring (`:23618`): NOT covered** — they are written by the
**opposite** party in the **opposite** direction, so "I sent my close" proves nothing.

| phase | what |
|---|---|
| **1 DECIDE** | session close |
| **2 QUIESCE** | the peer stops issuing writes (it observes the close) |
| **3 DRAIN** | **THE PEER drains ITS OWN send CQEs** (only it can see them) **and then CERTIFIES**. `peerWritersQuiesced` must become a **RECEIVED FACT**, not a locally-inferred one (today it is set merely on OBSERVING the close, `:23945` — logical, not physical). |
| **4 RELEASE** | I deregister only after that certification |

**⚡ THE EFFICIENT ANSWER TO CHECK FIRST — it may cost NOTHING:** *if the peer's close RESPONSE travels the SAME
QP as its completion writes, then RC in-order delivery ALREADY proves every prior completion write landed* —
exactly the command-mailbox argument, in reverse. **Then there is no protocol change at all, only an invariant
to write down and assert.** **VERIFY THIS BEFORE DESIGNING ANY CERTIFICATION.** Only if the response rides a
different QP/lane do we need to change what an existing message means (still **zero new round-trips** — we
change *when* it is sent, never add one).
**OPEN:** I previously named `CLOSE_SINK` — **wrong lifetime** (that is payload-stream; these MRs are
client-SQL critical-control). The correct session-level certifier must be identified.

**P1-g DISSOLVES.** My "count and drop a stale payload WIMM" **contradicted the contract** (its consumer
advances **sender credit** — someone was waiting). Under the protocol the stream is not released until the peer
certifies, so **the stale WIMM cannot exist**. The doorbell-token generation becomes a **LOUD ASSERTION**.
(If a stale one somehow arrives: **ATTRIBUTE it to the old stream**, never drop; and it must **never** reset a
SHARED connection. A **malformed** token stays a reset.)

**The sender-head mirror MR leak DISSOLVES too** — it is kept registered *"because a late ACK can still
arrive"*, which is the same missing certification. With phase 3 in place it can be deregistered at stream close.

---

#### P2-i — `HomerDpuDmaDestroy` on CLEAN exit

1 DECIDE: shutdown. 2 QUIESCE: stop arming. 3 DRAIN: progress the PE until the **global** outstanding count is
zero (bounded; FATAL on non-convergence). 4 RELEASE: destroy. **Violent teardown on a FAILURE exit is
unchanged** — see §9.4.

---

### 9.4 THE ONE LEGITIMATE EXCEPTION — and it must be named as such

**Scenario E (violent teardown) is CANCELLATION, not phase 3.** It is what we do when **we CANNOT drain** — the
QP or engine is broken, so the work will never complete. It destroys the CQ/QP and then **ABANDONS software
owners, explicitly recording what it discarded** (`:4278`, `:4290`).

- **CORRECT for a real failure. Keep it exactly as it is.**
- **NEVER a substitute for a clean leave.** Using it as the release mechanism is precisely the substitution this
  whole audit exists to end — and it is what P7b.1 must stop doing.
- **Any resource whose phase 3 cannot converge must route HERE explicitly**, not silently (e.g. the partial-post
  slot in P0-a).

### 9.5 SEQUENCING (current, after three review rounds)

| item | state | note |
|---|---|---|
| **P0-a** | ✅ **LANDED + VALIDATED** (citus `71b523757`) | §20. The blind clear was a SYMPTOM of a signal policy with no terminal case. **But the fix is PER-SITE — see §21 (P0-i), which generalizes it into a substrate invariant and may have found a LIVE HANG in the payload lane.** |
| **P0-i** | ✅ **LANDED + VALIDATED** (citus `67ebc0167`), §21.12-21.15 | the payload HANG was **REFUTED** (I inferred a policy from a stats counter). The payload + credit lanes **already fence themselves** — every batch ends with a forced-signalled WR; **they are the EXEMPLAR.** Only the 2 runtime-selective lanes lack it, and only on the **ABORT** path (no close message ⇒ nothing to force-signal ⇒ P0-a's Scenario E clear = a LOUD use-after-free). Fence must carry its **own lane's WR-ID class** — CQ dispatch is per-class, so a cross-lane CQE does **not** retire it. |
| **P0-b** | ready | self-contained |
| **P0-c** | ready | needs the transport-helper API change (return the slot indices) + WOULD_BLOCK backpressure |
| **P0-d** | ✅ **LANDED + VALIDATED** (citus `9a6f68257`) | §15. NOT what §8 thought: two premises were refuted, and the real hole was the `ResetSession` FUNNEL releasing with no drain (reachable from shutdown). Fixed by enforcing the drain IN the funnel — every path correct by construction. |
| **P0-e** | ⛔ **DROPPED** (§19) | the bug was REFUTED (the role-3 header is self-describing: it carries `consumedEpoch` too, so a cleanly-torn-down slot reads CREDITED, not stale) **and** the design was unbuildable (the DPU cannot address role-5 control; the backend legitimately owns it). Crash paths dropped by owner decision (§18). **What survives: §19.3's premature-re-nomination race (real, unlikely, loud — recorded not fixed) and §19.4's optional header-only-memset perf item.** |
| **P0-f** | ✅ **LANDED + VALIDATED** (citus `64050e0dd`) | the certifier's `bool` could not express NOT_FOUND. §14. Was load-bearing for P0-d/P0-e; both are now unblocked. |
| **P1-f** | ready, **FREE** | **TWO invariants, not one** (§11.1) — one per resource, each with its own proof |
| **P7b.1/.2/.3** | **BLOCKED on P0** | and P7b.1 must NOT use `ResetPeerConnection` as its release mechanism — that is CANCELLATION (§9.4) |

**DISSOLVED by the protocol** (do not implement these — they were guards, and the guards deleted themselves):
P1-g; the sender-head mirror-MR leak; §27's response-slot generation check; §7.5's three-way partition.

### 9.6 The rule

> **A quiesce that does not stop EVERY producer is not a quiesce. A drain that does not count EVERY consumer is
> not a drain. And if you are writing code to handle a stale reference, you have found a missing phase 2 or 3 —
> go fix that instead.**

---

## 10. SECOND REVIEW — SETTLED, with four corrections (and one of them is mine, badly)

### 10.1 ⚠ P0-d: I BUILT §8 ON A STALE COMMENT. The fix is MOSTLY ALREADY IMPLEMENTED.

**VERIFIED against the code, not the comment:**
- grouped-control discovery **DOES honour `ringRuntime->pollQuiesced`** (`homer_service_dpu_dma.c:1408`);
- grouped-control submission **DOES count into the ring** via `HomerDpuDmaRingTaskSubmitted(import, ringIndex)`
  (`:11462`, with its own comment: *"Grouped discovery may address an enrolled arena role-5 result ring"*);
- T1 sets `pollQuiesced` (`:6188`); T2 sums `inFlightTaskCount` across all three arena rings (`:6246`);
- every other arena-ring task builder also calls the same per-ring accounting (`:12071`, `:12291`, `:12582`).

**THE COMMENT AT `:4533` IS STALE.** It asserts *"those reads are not counted against the ring"* — which **was**
true and **has since been fixed**. **I built §8's entire root cause on it.**

**In a document whose central thesis is that this codebase's comments lie, I trusted a comment over the code.**
The comment even ends *"Do not restore that reasoning"* — and I restored a *conclusion drawn from* it. This is
the §29.1 decoy-guard failure in a new costume, and it cost a whole section.
**ACTION: fix that comment as part of P0-d — it is actively misleading and it has now claimed a victim.**

**WHAT ACTUALLY REMAINS FOR P0-d (small):**
1. **The forced-teardown deadline still bypasses the drain** (`tuple_sink_service_process.c:43569`). That part of
   §8 stands: **the deadline may abandon the BACKEND HANDSHAKE; it may NEVER abandon the DMA DRAIN.**
2. `tenancyGeneration` can then become a **LOUD ASSERTION**.
3. **Open (inferred-safe, verify):** a spawn / arena-publish-line-clear task family is tracked by **spawn
   runtime**, not ring `inFlightTaskCount` (`:6814`). Grouped discovery refuses to read while
   `tenancyBaselinePending` is set (`:11328`, `:10459`), so admission ordering *appears* to prevent it being
   outstanding at T1. **Verify rather than assume.**
4. **Note the role-2 command ring is DELIBERATELY not quiesced at T1** — teardown publishes the RELEASE command
   *through* it. The service drains once before RELEASE and re-checks after (`:43638`, `:43689`). **This is
   correct; do not "fix" it.**

### 10.2 ⚠ THE ALARM RULE IS TOO BROAD AS I STATED IT — narrow it

My claim that the alarm "finds exactly our list and nothing else" is **FALSE**. There are legitimate
generation/epoch/stale checks that are **NOT** missing drains, and my rule would have wrongly condemned them:

| legitimate check | why it is NOT a missing drain |
|---|---|
| **Scheduler action snapshots** (`remote_execution_peer_transport_rdma.c:497`, `tuple_sink_service_process.c:46945`) | a PLAN can go stale between planning and execution. Optimistic scheduler validation, not an unretired WR. |
| **Connection generation after cancellation** (`:1060`, `:2276`) | after Scenario-E cancellation, external handles legitimately outlive the destroyed connection. **Needed even with perfect clean drains.** |
| **DPU bridge/process restart identity** (`tuple_sink_service_process.c:43451`) | a restarted agent is not a clean tenant release; phases 2-3 cannot make this impossible. |
| **Task-slot callback integrity** (`homer_service_dpu_dma.c:14500`) | an internal ownership ASSERTION against double-callback/corruption. |
| **Protocol epochs / duplicate detection** (`:42748`) | idempotency against protocol re-observation, not physical retirement. |

**CORRECTED RULE — narrow, and now true:**

> **The alarm applies when a staleness check protects a RELEASED PHYSICAL RESOURCE from an OUTSTANDING PHYSICAL
> OPERATION.** *That* is always a missing phase 2 or 3.
> It does **NOT** apply to plan staleness, restart identity, idempotency/duplicate detection, or
> post-cancellation handle validation. Those are legitimate and must be kept.

### 10.3 ✅ P1-f IS SETTLED — AND IT IS FREE. No new message.

**VERIFIED:**
- every cross-node client completion is published by `TupleSinkServicePublishPeerClientCommandCompletion`
  (`:18684`), and **all** descriptor/body/WIMM writes go through `sessionState->clientSqlPeerConnectionHandle`
  + `clientSqlPeerCompletionMailboxDescriptor` (`:18744`, `:19011`, `:19036`) — **one QP**;
- the **backend-FAILURE cleanup path SYNTHESIZES a `CLIENT_SQL_SESSION_CLOSE` completion** through that same
  publisher (`:24052`, `:24150`) — **so even the failure path produces the terminal completion**;
- completion publication is held until earlier tuple-result EOS source WRs have retired (`:18603`);
- session close is **rejected while another command/completion publication is in flight** (`:39518`).

**THE CERTIFIER IS THE TERMINAL `CLIENT_SQL_SESSION_CLOSE` COMPLETION.** When node A observes that WIMM, RC
in-order delivery **on that same QP** proves every earlier completion-mailbox write has arrived, and it is the
"no future completion writes" boundary.

**And it confirms the SOURCE-vs-TARGET distinction (§9.2):** node A does **not** need the sender's CQE.
**Delivery proves arrival** — that is the right proof for a **TARGET** MR. The sender's local CQE is **P0-a's
source-buffer obligation**, a different thing entirely.

**P1-f is therefore: write the invariant down + ASSERT that no publication is permitted after the terminal
close completion.** Zero protocol change, zero round-trips. **`CLOSE_SINK` was the wrong message; this is the
right one.**

### 10.4 P0-a — the abort-without-close path is still not convergent. FIX IT GENERICALLY.

Force-signalling the `CLIENT_SQL_SESSION_CLOSE` completion converges the clean path (and, per §10.3, the
backend-failure path synthesizes one). **But a teardown that goes straight to reset without a close completion
still leaves unsignaled owners with no guaranteed checkpoint.**

**CORRECTED FIX — do not key convergence on a close completion existing.** Phase 3 is *"finish AND drain"*, so:

> **At QUIESCE, if any unsignaled owner is outstanding on the lane, POST A SIGNALED FENCE.**

The lane FIFO retires cumulatively through a later signaled checkpoint (`:5934`), so **one** signaled WR flushes
everything before it. This converges on **every** path, not just those that happen to emit a close. Keying the
signal on `CLIENT_SQL_SESSION_CLOSE` remains a good *optimization* (it usually IS the last publish), but the
**fence is the correctness mechanism**.

**Still routes to Scenario E (correct):** a **partial post** leaves the source `FAILED_OR_INFLIGHT` (`:19043`)
and **no CQE can identify it** — it is owned until the QP cutoff. It **cannot** be part of an ordinary
live-QP drain.
**Not a problem (verified):** a close checkpoint also retires *other sessions'* older unsignaled owners on the
shared lane. That is correct RC-lane ordering, not a wedge.

### 10.5 P0-c — my drain is at the WRONG LAYER (it would not even work)

Convergence is fine: every current staging-pool WR **is** covered by an already-signaled checkpoint (`:9828`,
`:10054`; both production receiver-head publishers pass `signaled=true`, `:32802`, `:35370`). **So the pool can
never fill with an unsignaled prefix awaiting a fence we are refusing to post.** My deadlock worry was unfounded.

**But "the reservation drains the send CQ and retries" CANNOT be implemented where reservation happens.**
Reservation is inside the **transport** helpers (`:9782`, `:10055`), and **payload CQEs require a SERVICE-owned
callback** — the transport drain **rejects** a payload CQE when no callback is supplied (`:3730`). Owner
retirement updates service stream state, mirror/source ranges and credit owners (`:16579`, `:32550`), which the
transport allocator **cannot** do.

**CORRECTED FIX: reservation must report BACKPRESSURE, not poll.** Return `WOULD_BLOCK` (the existing
`HomerPeerPostResult` idiom, `:10080`) and let the **service** layer — which owns the callbacks — drain and
retry. **Copying the response-slot allocator pattern was wrong-layered**; that pool's CQEs are transport-owned,
these are not.

### 10.6 SCENARIO E IS NOT THE ONLY NON-DRAINING OUTCOME — classify before you cancel

Bounded non-convergence must be **CLASSIFIED**, not blanket-cancelled:

| cannot drain | correct handling |
|---|---|
| accepted **partial post**, no signaled checkpoint (`:19043`) | owned until **QP cutoff** -> E |
| **peer/QP failure** (failed send CQE, `:4044`) | reset **cancels and reconciles** -> E |
| **remote semantic non-progress** — sender close waits on `senderVisibleRemoteConsumedHead >= finalTail` (`:27086`); a **live but stuck** peer blocks it | bounded wait -> classify as **peer failure** -> E |
| **peer dies before certifying** a target MR | connection cutoff -> E |
| **DOCA engine failure/hang** | **FATAL** (this is the engine-broken boundary) |
| **persistent notification RECV WQEs** (`:5000`) | **NEVER include in a tenant "zero outstanding" predicate** — they are connection-lifetime by design |

### 10.7 Rules earned

- **A stale comment is a decoy that outlives its author.** `:4533` asserted a fact that had since been FIXED,
  and I built a whole section on it — in the very document arguing that this codebase's comments cannot be
  trusted. **Verify a comment's claim against the code EVERY time, especially when it is the load-bearing
  premise of your fix.** And **fix the comment when you catch it**, or it will claim another victim.
- **A rule that condemns everything explains nothing.** My alarm was too broad; narrowing it to *"a stale check
  guarding a RELEASED PHYSICAL RESOURCE against an OUTSTANDING PHYSICAL OPERATION"* makes it true AND keeps it
  sharp. Plan staleness, restart identity, and idempotency are legitimate and must survive.
- **Convergence must not depend on a message that a failure path might not send.** Key phase 3 on a **fence you
  can always post**, not on an event you merely usually get.
- **A drain must live at the layer that owns the completion callbacks.** Otherwise it cannot retire what it
  polls.

---

## 11. THIRD REVIEW — P1-f SETTLED (with a split); P0-e NOT SETTLED (the carrier does not exist)

### 11.1 ✅ P1-f — SETTLED, but it is TWO invariants, not one

**Client completion mailbox — FREE. Verified:**
- The QP is **stable for the session**: the handle is assigned **once** (`tuple_sink_service_process.c:37682`
  requester; `:40283` responder) and **never re-pointed**. Connection reset matches the exact old
  handle/generation and marks the peer session reset-complete (`:29122`) — **failure ENDS the session, it does
  not migrate it.** No reconnect/rebind, no traffic-class migration (`:37620` pins CRITICAL_CONTROL).
  **So RC ordering is not broken by a hidden connection swap.**
- **The terminal close completion IS the last remote write.** `TupleSinkServicePublishPeerClientCommandCompletion`
  (`:18684`) is the **sole** remote publisher through `clientSqlPeerCompletionMailboxDescriptor`. The sender
  **does not** write `readyEpochSlots`/`readyVersion` — the RECEIVER's CPU publishes those after WIMM validation
  (`:18980`), so they are **local stores, not later remote writes**. Session close is refused while any command
  or completion publication is outstanding (`:39518`); the backend-FAILURE path synthesizes the terminal close
  completion through the same machinery (`:24052`, `:24150`); duplicate publication is suppressed (`:18729`).
- **The ASSERT has a real home:** every remote publication passes through `:18684`, which sees the session's
  command kind/state before reserving or posting. **A genuine single enforcement point** for *"no publication
  after the terminal session-close publication."*

**⚠ BUT THE PEER COMMAND-COMPLETION RING IS **NOT** COVERED BY THAT ARGUMENT — I had lumped them together.**
It is a **different op** (`CITUS_REMOTE_EXEC_OP_SQL_COMMAND`, not `CLIENT_SQL_SESSION`), a **different connection
handle** (`peerCommandCompletionConnectionHandle`, `:40421`), a **different descriptor**
(`peerCommandCompletionRingDescriptor`, `:19258`), and its publisher **explicitly excludes client-SQL sessions**
(`:19231`).

**It is ALSO free — but for its OWN reason:** its publication is an unsignaled record write followed by a
**SYNCHRONOUS signaled `publishedEpoch` write that WAITS for its own CQE** (`:19292` ->
`TupleSinkServiceWritePeerUint64PreparedRdma`, which blocks on the CQE at
`remote_execution_peer_transport_rdma.c:10195`). **It fences its own source synchronously.**

> **P1-f is TWO invariants, each with its own proof. Write them separately.** One says *"delivery of the
> terminal close completion orders everything before it on this QP."* The other says *"each publication
> synchronously fences its own source."* Merging them would have documented a proof that does not apply to
> half the thing it claims to cover.

### 11.2 ❌ P0-e — NOT SETTLED. My carrier does not exist, and my design skips phase 2.

Three verified blockers:

**(a) The doorbell socket is ONE-WAY in steady state.** After the one-time `DOORBELL_ATTACH`/`_ACK` handshake
(`homer_frontend_agent.c:1315`, `:1326`), the agent **"never parses"** steady-state frames — it drains all bytes
to `EAGAIN`, **discards them**, and signals the postmaster (`:62`, `:1367`). Its event loop waits only for
readability; there is **no request state and no response parser** (`:1675`). The DPU side reads only the attach
frame and has a single outbound `SPAWN_DOORBELL` slot (`homer_service_dpu_doorbell.c:446`, `:878`), and the ABI
defines only those three message kinds (`homer_dpu_comch_abi.h:65`, `:186`).
**"Use the existing doorbell socket" = a NEW bidirectional framed protocol + changes to BOTH event loops.**

**(b) The setup socket is NOT persistent.** The agent's setup exchange completes **before** it enters the
doorbell loop (`:1576`, `:1614`); no setup connection is retained for later queries. A **fresh cold setup-port
request/response** is structurally closest to the design — but the query message does not exist.

**(c) ⚠ NOTHING INITIATES THE DPU-SIDE QUIESCE FOR A DEAD BACKEND — my design SKIPS PHASE 2.**
`HomerDpuDmaQuiesceArenaCompletionPolling` (`homer_service_dpu_dma.c:6190`) is **session-driven** — it needs
`bridgeGeneration` + `arenaSlotIndex` + `serviceSessionId` and validates the rings are still bound to **that
session**. The service invokes T1/T2 only from the **selected-session teardown state machine** (`:43638`).
**With the backend gone, nothing runs T1.** So repeated *"is it drained?"* queries can be **false forever**,
because producers keep arming work. **Adding a QUERY does not execute PHASE 2.** The request must **INITIATE**
the quiesce, not merely ask about it. This is the deepest error in my design: I assumed the DPU would drain on
its own. **It will not.**

**(d) ⚠ `HomerDpuDmaArenaSlotTasksInFlight()` CAN SILENTLY FALSE-CERTIFY — and this is a BUG IN ITS OWN RIGHT.**
`HomerDpuDmaFindArenaSlotImport` (`:5894`) requires an ACTIVE import with an **exactly matching**
`bridgeGeneration` (`:5917`). When lookup FAILS, `HomerDpuDmaArenaSlotTasksInFlight` (`:6268`) **returns `true`
with `*inFlightOut = 0`** (`:6280`) — **"NOT FOUND" is indistinguishable from "FOUND AND DRAINED."** After an
agent restart mints a new generation (`homer_frontend_agent.c:1529`) while old BOUND slot state survives in the
shared arena, the query **misses the old import and reports DRAINED.**

**That is the bug family again — a missing thing read as a definite negative — in the very primitive meant to
CERTIFY safety.** `HomerDpuDmaUnbindRingSession` (`:6303`) has the same shape (`:6327` treats a missing import
as idempotent success). **FIX THIS REGARDLESS OF WHICH P0-e OPTION WE TAKE:** a certifier must distinguish
`DRAINED` / `IN_FLIGHT` / `NOT_FOUND`, and **`NOT_FOUND` must NEVER be treated as safe for host memory reuse.**

### 11.3 P0-e — the two real options

| | option | cost | risk |
|---|---|---|---|
| **A** | **QUARANTINE until postmaster restart.** The reaper marks the slot `QUARANTINED` and it is **never reused for this postmaster's lifetime**. **FATAL** if quarantine exhausts the 16 slots. | **ZERO new protocol, ZERO carrier, ZERO false-certification surface** | leaks a slot per crashed backend |
| **B** | **DPU handshake.** A new bidirectional request that **INITIATES** quiesce+drain on the DPU (phase 2!) and ACKs `DRAINED` / `IN_FLIGHT` / `NOT_FOUND`. | a new framed protocol on **both** event loops, **plus** the (d) fix | correct and reusable |

**RECOMMEND A, with B as the documented upgrade path.** A clean postmaster start **zeroes the whole arena**
(`homer_frontend_agent.c:295`), and our workflow restarts between runs — **so the leak never accumulates in
practice.** A is **fail-safe, loud, and impossible to fool**; B is the proper fix and should be taken when slot
pressure or a long-lived postmaster demands it. **Either way, (d) is fixed.**

### 11.4 Rules earned

- **One invariant per proof.** I nearly wrote a single P1-f invariant covering two resources whose safety rests
  on **completely different mechanisms** (RC delivery ordering vs a synchronous per-publication fence). A
  correct-sounding invariant applied to something it does not actually cover is worse than none.
- **A query is not a phase.** "Ask if it is drained" does not DRAIN it. If phase 2 (quiesce) has no initiator on
  the far side, the query loops forever. **Name who executes each phase, not just who observes it.**
- **A certifier that cannot say "I DON'T KNOW" is not a certifier.** `ArenaSlotTasksInFlight` collapses
  NOT-FOUND into ZERO-IN-FLIGHT. The safety primitive itself had the bug family in it.

---

## 12. P0-e DECIDED — Option A (quarantine until postmaster restart). Owner, July 12, 2026.

**Owner:** *"P0-e is for a crash-recovery case, meaning we don't expect to hit it normally, correct? If so,
let's do option A and we don't care for option B really (this is a research project)."*

**Confirmed, with one nuance that matters for sizing:**

- A backend that exits **cleanly** — **including `ereport(FATAL)`** — releases its own arena slot via
  `on_proc_exit` -> `HomerFrontendAgentReleaseArenaSlot` (`homer_frontend_agent.c:658`). **The reaper NEVER runs
  in normal operation.**
- It fires **only** for a backend that dies without running `on_proc_exit`: **SIGKILL / SIGSEGV**.
- **⚠ But our own runbook hits it deliberately.** CLAUDE.md mandates `kill -9` on surviving
  `postgres: remote exec backend` processes after every `--homer-dpu-command` run (socketless backends do not
  answer `pg_ctl stop`), and that `kill -9` drives a postmaster **crash-restart**, which does **not** re-run
  `_PG_init` — so the arena keeps its stale BOUND state and the reaper exists precisely to clean it up.
  **The APPLICATION never hits this path; our DEV LOOP does, at teardown.**

**Why Option A is nevertheless safe here — and this is the sizing argument, not a hand-wave:**

- Quarantine can only accumulate **within a single postmaster lifetime**.
- Our validation runs 3-6 pgbench runs per postmaster with **CLEAN session closes** between them (the `kill -9`
  happens at the clean baseline, when everything is torn down anyway).
- A clean postmaster start re-runs `_PG_init` -> `HomerFrontendAgentCreateArena` -> **zeroes the whole arena**
  (`homer_frontend_agent.c:295`). **So the leak resets every cycle.**
- **FATAL at 16 quarantined slots** therefore only fires if something genuinely pathological is happening —
  which is exactly when we want it to.

### 12.1 THE DESIGN (Option A)

1. **New slot state `HOMER_FRONTEND_ARENA_SLOT_QUARANTINED`** (today the enum is only `FREE=0`, `BOUND=1`,
   `homer_frontend_agent.h:157`).
2. **`HomerFrontendAgentReapDeadArenaSlots` (`:689`) must NOT zero the bodies and must NOT publish FREE.** It
   publishes **QUARANTINED**, and **logs loudly** (pid, slot, session).
3. **A QUARANTINED slot is NEVER handed out.** The binder takes only `FREE`.
4. **FATAL when QUARANTINED slots exhaust the arena (16).** Not fatal in the reaper itself — it is on the
   ROUTINE teardown path of our own dev loop, and a FATAL there would crash the postmaster on every run.
5. **The slot returns to FREE only via the whole-arena zero on the next clean `_PG_init`.**

**This is fail-SAFE (leak a slot) rather than fail-SILENT (corrupt host memory), and it is impossible to fool:
there is no certification primitive to get wrong, because there is no certification.**

**Option B (a bidirectional DPU request that INITIATES quiesce+drain and ACKs DRAINED/IN_FLIGHT/NOT_FOUND) is
recorded as the upgrade path** should a long-lived postmaster or real slot pressure ever demand it. **Owner has
explicitly deprioritized it: this is a research prototype.**

### 12.2 STILL MANDATORY — the false-certification bug is fixed REGARDLESS

**`HomerDpuDmaArenaSlotTasksInFlight()` (`homer_service_dpu_dma.c:6268`) returns `true` with `*inFlightOut = 0`
when the import lookup FAILS (`:6280`) — "NOT FOUND" is indistinguishable from "FOUND AND DRAINED".**
`HomerDpuDmaUnbindRingSession` (`:6303`, `:6327`) has the same shape.

**This is the bug family INSIDE the primitive whose entire job is to certify safety**, and it is a live defect
independent of P0-e. **FIX IT:** it must distinguish **`DRAINED` / `IN_FLIGHT` / `NOT_FOUND`**, and
**`NOT_FOUND` must NEVER be treated as safe for host memory reuse.**

> **A certifier that cannot say "I DON'T KNOW" is not a certifier.**

Option A does not *use* this primitive — but every other caller does, and the next person to reach for it as a
safety gate would be silently misled. Fix it now, while we know.

---

## 13. P0-e REVISED to Option A′ — the review broke Option A, and handed us the certifier

### 13.1 Option A as specified is memory-safe but OPERATIONALLY UNSOUND (verified)

1. **The DPU keeps polling the quarantined slot.** Host quarantine changes only
   `HomerFrontendArenaSlot.state`. The DPU's `boundServiceSessionId` / `pollQuiesced` / import are **untouched**,
   and grouped-control discovery is **perpetual while the import is ACTIVE**
   (`homer_service_dpu_dma.c:1334`, `:1418`). A dead backend does **not** set `pollQuiesced` — that is
   session-teardown driven (`:6190`). So Option A traded host corruption for a **host-slot leak AND a DPU
   polling/slot leak.**
2. **The DPU can RE-NOMINATE the quarantined index** after its own unbind, so the backend FATALs on the
   `FREE -> BOUND` CAS (`homer_frontend_agent.c:552`, `remote_execution_backend_bridge.c:2833`) — **repeatedly**,
   because the DPU cannot see host quarantine state. **No graceful capacity degradation.**
3. **Quarantines survive crash-restart.** Only a fresh postmaster **process** re-runs `_PG_init` ->
   `CreateArena` -> whole-arena memset (`:299`, `:338`). **16 legitimate crashes in a long-running postmaster
   is NOT pathological**, so my sizing argument was wrong.
4. **FATAL-at-16 was broken anyway.** The reaper runs in a **background worker**; `ereport(FATAL)` there
   restarts the **worker**, not the postmaster (`:1404`) — a restart/log loop with the 16 slots still
   quarantined.

### 13.2 THE FIX FALLS OUT OF OBJECTION 2 — the nomination IS the certificate

**VERIFIED:** `HomerDpuDmaBindRingSession` (`homer_service_dpu_dma.c:5940`) counts rings whose
`boundServiceSessionId != 0` (`:6009`) and **REFUSES a slot whose rings still hold a session binding** (`:6015`).

> **Therefore a DPU NOMINATION of slot N is a CERTIFICATE that the DPU has UNBOUND slot N's rings — and once
> P0-d lands (unbind requires a completed drain), unbind ⇒ DRAINED.**
>
> **The certifier I said did not exist was there all along, riding an EXISTING message: the spawn request's
> slot index.** Zero protocol, zero round-trip, zero new carrier.

### 13.3 OPTION A′ (this replaces §12.1)

```
1. Reaper: BOUND -> QUARANTINED.  Do NOT zero the bodies.  Log loudly (pid, slot, session).
2. The HOST never hands out a QUARANTINED slot on its own initiative.
3. A DPU NOMINATION of that index IS the certificate.  The backend's bind may then CAS
   QUARANTINED -> BOUND -- zeroing the bodies FIRST (the FREE-state ABI init the reaper deferred).
4. NO FATAL-at-16 needed: slots RECYCLE.  Keep the loud log + a quarantine counter.
```

**The key realization: EVERY bind is already DPU-nominated** (`tuple_sink_service_process.c:20113` ->
`:19880` -> `remote_execution_backend_bridge.c:2829`). So quarantine is **not** "never reuse" — it is
**"DEFER THE ZEROING until the party that can SEE the DMA certifies it is drained."**

**That is exactly the Scenario-D rule (§9.2): the actor who can see the completions is the one who must certify
them.** The host cannot see DOCA task completions; the DPU can; so the DPU certifies — and it already does, by
the act of nominating.

**Answers all four objections:** DPU polling is bounded by session teardown (peer close, or the forced deadline)
rather than forever; **divergence BECOMES the certificate**; accumulation **does not happen**; and the broken
FATAL-at-16 is **no longer needed**.

### 13.4 ⚠ NEW DEPENDENCY: P0-e now REQUIRES P0-d

**Without "unbind requires a completed drain", a nomination certifies NOTHING.** Sequence P0-d **before** P0-e.
Also carry forward the review's caveat: the agent's clean shutdown destroys host exports even if the DPU close
FAILS (`homer_frontend_agent.c:1740`) — so *"a fresh `_PG_init` zeroed the arena"* is only physically safe if
the old DPU import was **actually** cut off. **Do not treat a fresh host start as proof the DPU forgot the old
mapping.**

### 13.5 ✅ P0-f IS CONFIRMED A LIVE BUG (not merely latent)

Both executable callers of `HomerDpuDmaArenaSlotTasksInFlight()` are in `HomerServiceDpuAdvanceArenaTeardowns`:

| caller | not-found behaviour | verdict |
|---|---|---|
| **T2 drain query** (`tuple_sink_service_process.c:43640`) | not-found returns success with `inFlight=0`; if `completionEventCount==0`, **teardown ADVANCES to RELEASE publication** | **LIVE BUG.** A wrong/stale generation is **indistinguishable from safely drained**, so teardown can falsely advance while the service still holds ring refs. |
| **T4 release-write retirement** (`:43706`) | not-found + no queued RELEASE is accepted as **physically retired** | **falsely finalizes teardown** on an identity mismatch |

And `HomerDpuDmaUnbindRingSession()` (one caller, `TupleSinkServiceReleaseDpuArenaBinding`, `:19971`): on
not-found it returns success, and the service then **unconditionally clears its cached binding** (`:19982`) —
so on an identity mismatch **the service forgets its ownership while the DPU rings remain bound FOREVER.**

**P0-f is therefore mandatory and load-bearing for P0-d/P0-e both.** Fix it FIRST.

### 13.6 Rules earned

- **A review that breaks your design often contains the better one.** Objection 2 — *"the DPU may re-nominate a
  quarantined slot"* — read as a defect. It is the **certificate**. The fact that the DPU refuses to nominate a
  slot whose rings are still bound is exactly the proof the host needed, and it was already on the wire.
- **When you cannot find a carrier, ask what the OTHER SIDE ALREADY TELLS YOU.** I went looking for a new
  message. The existing message already carried the fact — I just had not asked what its precondition PROVED.
- **`FATAL` in a restartable background worker is not a fatal.** It restarts the worker and loops, leaving the
  condition intact. Know which process your `FATAL` actually kills.

---

## 14. P0-f — IMPLEMENTATION SPEC (the certifier cannot say "I DON'T KNOW")

**Status: IN PROGRESS.** This is the concrete, call-site-by-call-site spec. It supersedes the sketch in
§9.3/P0-f, which it refines in three ways (all recorded below): a **FOURTH defective primitive** the audit
never enumerated; a **mandatory RENAME** (a type change alone is not enough — see §14.2); and a **four-state**
enum rather than three, with the SAFE state deliberately **not** zero (§14.3).

### 14.1 The complete site table (caller set is CLOSED — verified by grep over both trees)

All four primitives are built on `HomerDpuDmaFindArenaSlotImport()` (`homer_service_dpu_dma.c:5894`), which
requires an import that `HomerDpuDmaImportCanSubmit()` AND whose `bridgeGeneration` matches **exactly**
(`:5918`). The lookup therefore fails when the arena export is torn down (frontend-agent restart), when the
import is detached-awaiting-reclaim, or on any generation mismatch.

| primitive | phase | call site | not-found TODAY | verdict |
|---|---|---|---|---|
| `HomerDpuDmaBindRingSession` (`:5980`) | allocate | `tuple_sink_service_process.c:20115` | **loud error, `false`** | ✅ **already correct** — and it is the *only* one, which is more evidence for §3.0 (the contract is upheld exactly where someone was previously burned) |
| `HomerDpuDmaQuiesceArenaCompletionPolling` (`:6228`) | **2 QUIESCE** | T1 `:42812` | `return true` — *"the import and its DPU-private polling state are already gone"* | ⚠ **NEW FINDING, not in the audit.** We stopped **no** poller and we cannot prove one is not running. A phase-2 gate that cannot see its producer must not claim it stopped it. |
| `HomerDpuDmaArenaSlotTasksInFlight` (`:6280`) | **3 DRAIN** | T2 `:43640` | `true` + `inFlight=0` → **ADVANCES to RELEASE_PUBLISH** | 🔴 **LIVE BUG** (§13.5) |
| `HomerDpuDmaArenaSlotTasksInFlight` | **3 DRAIN** | T4 `:43706` | `true` + `inFlight=0` → **FINALIZES teardown** | 🔴 **LIVE BUG** (§13.5) |
| `HomerDpuDmaUnbindRingSession` (`:6327`) | **4 RELEASE** | `:19971` | `true` ("idempotent"), caller then clears its cached binding (`:19979`) | 🔴 the DPU-side `boundServiceSessionId` **and** the role-5 result-sink stamp (header: *"cleared by HomerDpuDmaUnbindRingSession"*) **LEAK FOREVER**; the slot is refused at the next bind |

### 14.2 ⚠ THE RENAME IS LOAD-BEARING, NOT COSMETIC

Changing `HomerDpuDmaArenaSlotTasksInFlight`'s return type from `bool` to the enum **while keeping the name**
would still **compile**, and the bug would survive **verbatim**:

```c
if (!HomerDpuDmaArenaSlotTasksInFlight(engine, gen, slot, &inFlight))   /* !enum is legal C */
    return false;                       /* not taken for NOT_FOUND (== 1), just as before */
if (completionEventCount == 0 && inFlight == 0)                          /* inFlight is STILL 0 */
    phase = RELEASE_PUBLISH;                                             /* STILL falsely advances */
```

So: **rename to `HomerDpuDmaQueryArenaSlotDrainState()`.** The rename is what makes the compiler enumerate
every call site. *Rule: when you fix a predicate by widening its answer set, rename it — otherwise the callers
that ignored the old answer will ignore the new one, silently.*

### 14.3 The type encodes the rule

```c
typedef enum
{
    HOMER_DPU_ARENA_SLOT_QUERY_INVALID = 0, /* bad args / malformed import -- WE DO NOT KNOW */
    HOMER_DPU_ARENA_SLOT_NOT_FOUND,         /* no import for (generation, slot) -- WE DO NOT KNOW */
    HOMER_DPU_ARENA_SLOT_IN_FLIGHT,         /* found, inFlight > 0  -- NOT safe, retry */
    HOMER_DPU_ARENA_SLOT_DRAINED            /* found, inFlight == 0 -- the ONLY safe state */
} HomerDpuArenaSlotDrainState;
```

**The numbering is the fix.** `0` is *"we do not know"*, never *"safe"* — so a zero-initialized variable, a
`memset`, or a forgotten assignment can never accidentally read as DRAINED. That directly encodes the rule the
bug taught us: *a predicate that cannot express "I don't know" will lie, and it will lie in the direction of
"yes", because `0` is the value a failed lookup leaves behind.* Four states, not three: the existing function
already has **two distinct** `false` returns (bad args; malformed import) and collapsing them would destroy
information — the exact sin being fixed. Every call site `switch`es with **no `default:` arm**, so `-Wswitch`
makes a future fifth state a compile error (AGENTS.md: a `default:` arm turns `-Wswitch` off).

### 14.4 What each call site does — and WHY that answer and not another

**Common ground: all four NOT_FOUND cases mean the same physical thing** — *the frontend agent's arena export
vanished under us*. It is not transient: the session's `dpuArenaBridgeGeneration` is frozen at bind time, and
an import never comes back under an old generation. **So waiting for the 30 s deadline can never resolve it**
— burning the deadline is pure latency with zero chance of convergence.

- **T1 QUIESCE (`:42812`)** — pass a mandatory `bool *importFoundOut`. On not-found: **log LOUDLY and continue
  to DRAIN anyway.** Do NOT `return false` (that is the service's completion-acceptance path; a hard failure
  there kills the whole service including healthy sessions on other generations). Do NOT abandon inline: we
  have just enqueued a completion event into `selectedSession` at `:42796` that the client still has to see,
  and the abandon helper `memset`s that struct. **T2 will hit the same NOT_FOUND one pass later, at a safe
  point, and abandon there.** One abandon site, not two.
- **T2 DRAIN (`:43640`)** and **T4 RETIRE (`:43706`)** — full `switch`, no `default:`.
  `DRAINED` → the existing advance/finalize. `IN_FLIGHT` → stay (existing behavior). `NOT_FOUND` /
  `QUERY_INVALID` → **`HomerServiceDpuAbandonArenaTeardown(reason)`**.
  This is **§9.4 applied literally**: *"any resource whose phase 3 cannot converge must route to Scenario E
  EXPLICITLY, not silently."* Abandon-and-record — not service-fatal, because the engine is fine and only this
  session's import is gone; other sessions must survive.
- **`:19971` UNBIND** — pass a mandatory `bool *importFoundOut`. On not-found: **log LOUDLY**, naming the slot,
  the generation, and the consequence (*the DPU-side ring binding and result-sink stamp are LEAKED; the slot
  will be refused at the next `BindRingSession`*), then still clear the cached binding — it has nowhere else to
  point. This is the sanctioned case from §9.3/P0-f: *"a caller doing idempotent cleanup may proceed on
  NOT_FOUND — but it must SAY SO, deliberately."* Fail-**SAFE** (leak a slot), never fail-**SILENT**.

### 14.5 Refactor: ONE abandon path

Extract the body of the 30 s deadline block (`:43582`-`:43635`: scrub queued publish slots → release the arena
binding → `memset` the selected session → decrement the count) into

```c
static void HomerServiceDpuAbandonArenaTeardown(HomerServiceDpuDmaSchedulerState *dpuDmaState,
                                                HomerServiceDpuSelectedSessionState *selectedSession,
                                                TupleSinkServiceSessionState *serviceSession,
                                                const char *reason);
```

called from **three** sites: the deadline (behavior identical — a pure extraction), T2 NOT_FOUND, T4 NOT_FOUND.
`reason` goes in the log line, so the three are distinguishable in a trace. **This extraction is also exactly
what P0-d needs next** (P0-d must change what the forced path may and may not skip — it may abandon the backend
handshake, never the DMA drain), so it is paid for twice.

### 14.6 Non-goals for P0-f (do NOT do these here)

- **Do not** try to make `FindArenaSlotImport` resolve across generations. That is arena-aliasing, and it is
  P0-e's problem.
- **Do not** change the 30 s deadline's *policy*. That is P0-d. P0-f only extracts its body.
- **Do not** touch `HomerDpuDmaBindRingSession` — it is already correct, and it is P0-e's certificate.

### 14.7 ✅ P0-f LANDED AND VALIDATED (citus `64050e0dd`, July 12, 2026)

**Three consecutive pgbench runs on the full four-role DPU stack, one postmaster, one pair of services, no
restarts.** 5/5 tx and five decoded result rows per run, empty error streams, both nodes. Both DPU service
logs: `ALARM` = **0**, `ABANDONING teardown` = **0**, and the clean `T1 → T2 → T3 → T4` teardown fires once
per session with `importFound=1` every time. All seven smoke targets build; deform + DMA smokes pass.

**An unplanned, stronger proof fell out of the run: all three sessions bound `slot=0`.** The slot RECYCLES
across sessions — which can only happen if the unbind genuinely cleared the DPU-side `boundServiceSessionId`,
because `HomerDpuDmaBindRingSession` **refuses a slot whose rings are still bound**. The clean release path
demonstrated itself. *(This is the same refusal that P0-e turns into its certificate — §13.3.)*

### 14.8 Corrections to the §14 spec, made during implementation

1. **`inFlightOut` became OPTIONAL, not mandatory.** The spec made it mandatory by analogy with
   `importFoundOut`. Wrong analogy: for the drain query the **return value IS the answer**, so forcing an
   out-param the caller never reads just breeds a dead variable — and T2/T4 promptly grew one. It is now
   NULL-able, and the count is read exactly where it *means* something: the **cancellation path**, which must
   record what it discarded. `importFoundOut` on Quiesce/Unbind stays **mandatory**, because those still
   return `bool` and the out-param is the ONLY place the caller can learn the unknown state. *The rule is not
   "make out-params mandatory"; it is "the caller must be unable to miss the unknown state" — and where the
   return value already carries it, a second channel is noise.*
2. **The abandon path now RECORDS WHAT IT DISCARDED** (`drain=%s in_flight_dma_tasks=%u`), taken **before**
   `TupleSinkServiceReleaseDpuArenaBinding()` clears the (generation, slot) the query needs. §9.4 always
   demanded this ("abandons owners, **explicitly recording what it discarded**"); the deadline path never did
   it. It is also **precisely the diagnostic P0-d needs** to substantiate its claim that the DMA drain always
   converges.
3. **`ALARM` is the log token**, not the literal word "LOUD". It is greppable and it is the audit's own word
   for *"we are handling staleness — that is a bug, not a feature."* Acceptance now greps for `ALARM = 0`.
4. **T1's log line was lying.** It printed `teardown T1 quiesced arena slot` unconditionally — including when
   it had quiesced **nothing**. Now: `teardown T1 advanced to DRAIN … importFound=%u`.

### 14.9 Rules earned

- **When you fix a predicate by widening its answer set, RENAME it.** A `bool` → `enum` return-type change
  **compiles** at every `if (!Fn(...))` call site and preserves the bug **verbatim** — `NOT_FOUND == 1` is
  truthy, so the error branch is still skipped and the out-param is still `0`. The rename is what makes the
  compiler enumerate the callers. Silent survival of a bug through its own fix is the worst outcome available.
- **Put the SAFE state at a NONZERO enum value.** Zero is what a `memset`, a zero-init, a forgotten
  assignment, and a failed lookup all leave behind. If "safe" is `0`, every one of those failures reads as
  permission. *Encode the invariant in the type, not in the comment above it.*
- **Verify that the new UNSAFE branch is UNREACHABLE on the healthy path — in the code, before running.** The
  whole risk of this change was turning clean closes into abandons. The proof was cheap: every transition out
  of submit-capable state is keyed on `(bridgeGeneration, clientInstanceId)` — a whole **agent-export** close
  or detach — or on a DOCA *lost-host-export* error. **None is per-session.** One grep for the writers of
  `lifecycleState` / `staleTeardownPending` settled it.
- **A sibling with the same shape is a sibling with the same bug.** The audit enumerated three sites; the
  fourth (`QuiesceArenaCompletionPolling`, a **phase-2** gate) was found only by asking *"who else is built on
  this lookup?"* rather than *"who else did the audit name?"* **Enumerate by MECHANISM, not by memory.**

---

## 15. P0-d — INVESTIGATION FIRST, AND IT MOVED THE FIX

**Status: IN PROGRESS.** Three claims in §8/§10.1 were **inferred**. I verified all three before designing.
**Two were wrong, and the one real hole is somewhere I never looked.**

### 15.1 What the verification found

| claim (from §8 / §10.1 / my own suspicion) | verdict |
|---|---|
| *"The `DROPPED` grouped-control path was never even complete — it still cleared the NEW tenant's `controlReadInFlight`"* (§8.3) | ❌ **REFUTED.** `HomerDpuDmaResetRingTenancyState` **explicitly PRESERVES** `inFlightTaskCount`, `controlReadInFlight`, `commandPullInFlight` and every other per-ring in-flight counter across its `memset` (`homer_service_dpu_dma.c:4585-4600`, restored `:4616-4625`). It does **not** forget outstanding physical work — *that is the contract being UPHELD.* And because the flag survives as `true`, the **next** tenant **cannot arm** a grouped-control read (`:11387` refuses) until the old physical read retires. **The guard is complete.** Clearing the flag in the completion (`:10634`) is correct accounting, not a leak. |
| *"`HomerServiceDpuSpawnBegin` may arm the `ARENA_PUBLISH_LINE_CLEAR` DMA and then fail, so the undo at `:20178` releases a slot with an uncounted write in flight"* (my leading suspicion) | ❌ **REFUTED.** `SpawnBegin` submits **NO DMA** — it only reserves software state and sets phase `CLEAR_ARENA_LINES` (`homer_service_dpu_spawn.c:221-251`). **Every** spawn-family submission happens in `HomerServiceDpuSpawnAdvanceOne` (`:320`, `:347`, `:443`, `:461`, `:506`). All four undo sites (`:20140/60/70/78`) are therefore **vacuously safe**. |
| *"The spawn / arena-publish-line-clear family is tracked by spawn runtime, not ring `inFlightTaskCount` — verify rather than assume"* (§10.1 item 3) | ✅ **CONFIRMED as a real accounting gap, but UNREACHABLE on the normal path.** `ARENA_PUBLISH_LINE_CLEAR` is the **ONLY** uncounted task kind that touches arena-slot host memory or its `ringRuntime` (it zeroes the 3 publish lines, `:11218`, and clears `tenancyBaselinePending` in its completion, `:10528`). It cannot be outstanding at T1, because the phase machine will not spawn the backend until `arenaLinesCleared` (`homer_service_dpu_spawn.c:335-342`), and T1 only begins on a **terminal backend completion** — so the backend ran, so the CLEAR retired. |

### 15.2 🔴 THE REAL HOLE — and it is bigger than the deadline

**`TupleSinkServiceResetSession` (`tuple_sink_service_process.c:23664`) releases the arena binding with NO
drain check, NO T1, NO T2 — and it is THE FUNNEL.** Reachable from session close, client disconnect, peer
failure, `TupleSinkServiceHandleCloseSession` (`:39590`), and — worst — **service shutdown**, which resets
every session (`:48060-48067`) and only *then* destroys the DMA engine (`:48072`).

Its own comment is the tell:

> *"ResetSession being the single teardown funnel is **exactly what makes this the one correct unbind site**."*

**Being the single funnel makes it the right PLACE. It says nothing about the right TIME.** Release-site
uniqueness and release-time correctness are different properties, and the comment silently conflates them —
which is how *"we unbind in exactly one place"* came to feel like *"we unbind safely."*

**Full classification of every arena-release path:**

| path | drains first? |
|---|---|
| T4 clean teardown (`:43792`) | ✅ yes — `DRAINED` + no queued RELEASE |
| P2.T abandon / 30 s deadline (`:43645`) | ❌ no — deliberate Scenario E (now at least *records* what it discards, P0-f) |
| **`ResetSession` (`:23664`) — THE FUNNEL** | ❌ **NO. Reachable from shutdown.** |
| spawn undo (`:20140/60/70/78`) | ✅ vacuously (no DMA armed yet — VERIFIED, §15.1) |
| `:40473` deferred-response failure | ⚠️ safe **by accident** — relies on no scheduler pump running between `SpawnBegin` and the release |
| `:45901` async spawn failure | ✅ gated by spawn-slot `opInFlight` |

### 15.3 THE FIX — enforce the drain IN THE FUNNEL, not in each caller

The hole and the fix arrive together: **there is exactly one unbind funnel, so putting the drain inside it
covers every path by construction — including the ones I never enumerated.** That is the whole point of the
contract being a property of the RESOURCE, not of each caller's discipline.

1. **New engine primitive `HomerDpuDmaDrainArenaSlot()`** — query `HomerDpuDmaQueryArenaSlotDrainState()`, and
   while `IN_FLIGHT`, pump the existing bounded `HomerDpuDmaDrainPeInternal()` (`:5395`) and re-query, up to a
   total budget. Returns the tri-state.
   - ⚠ **`harvestEvenIfFatal = true` is MANDATORY here.** Its comment records that refusing to harvest under a
     latched `fatalError` **strands every submitted task forever and deadlocked the setup close-drain**. A
     teardown drain that will not harvest under fatal is a hang, not a drain.
   - **Re-entrancy is safe (VERIFIED):** the DOCA completion callback `HomerDpuDmaTaskMemcpyComplete`
     (`:14626`) never calls into service code — it returns task/buf ownership and at most arms an
     already-prepared publication task. So the funnel can never be reached from inside a callback, and a
     synchronous pump there cannot recurse into `doca_pe_progress()`.
   - This deliberately **deviates from the scheduler's "a drain grant is bounded and never spins"** invariant.
     That invariant governs *scheduler grants on the hot loop*. This is a **control-path** synchronous drain
     that runs once per session close. Documented at the definition.
2. **`TupleSinkServiceReleaseDpuArenaBinding` drains before unbinding.** One edit; every path covered.
3. **`HomerDpuDmaUnbindRingSession` gains an explicit `bool force`.** `force = false` (the contract path)
   **REFUSES** while `inFlightTaskCount != 0`. `force = true` is **Scenario E only** and proceeds, recording
   what it discarded. Today that site merely *warns* (`:6422`, *"a late completion can re-poison the tenancy
   reset"*) **and resets anyway** — a textbook decoy: it detects the violation, names it exactly, and proceeds.
   Making the exception a NAMED PARAMETER turns an implicit violation into a greppable, deliberate one.
4. **`tenancyGeneration` stays — as the Scenario-E backstop, with an ALARM.** It is NOT deleted: on a broken
   engine (`fatalError`, tasks that never retire) the drain legitimately cannot converge, and the generation
   guard is what keeps that from poisoning the next tenant. But on the contract path it must **never fire**, so
   the drop now prints an ALARM naming the broken drain. *Do not delete a safety net in the same change that
   makes it unnecessary.*
5. **The ABANDON_DRAIN phase I had designed EVAPORATES.** Once the funnel drains, the abandon path drains too,
   for free. The deadline keeps abandoning the **backend handshake** (correct — the backend may be dead and can
   never ack) and now never abandons the **DMA drain** (which converges, because the engine is independent of
   the backend). **§8.4 satisfied without a new state.**

### 15.4 Rules earned

- **A "single funnel" comment is about PLACE, not TIME.** One release site is a precondition for correctness,
  never a proof of it. When a comment argues for its own correctness from uniqueness, check what it is unique
  *about*.
- **Enforce a contract at the RESOURCE, not at the CALLER.** I had enumerated two violating call paths and was
  about to fix both. Verification found a third (the funnel), which I would have missed — and putting the drain
  *in the funnel* fixes all of them plus the two I'd rated "safe by accident". **If a contract needs every
  caller to remember it, it will be forgotten; if it lives in the primitive, forgetting is impossible.**
- **Verify inferred claims BEFORE designing on them, not after.** Two of the three premises I was about to
  build on were false, and one of them was mine. This is the *third* time an inferred claim about this file was
  refuted by the code (see §10.1, §14.8). **In this codebase, "I reasoned it must be so" has a losing record.**

### 15.5 ✅ P0-d LANDED AND VALIDATED (citus `9a6f68257`, July 12, 2026)

Three consecutive pgbench runs on the four-role DPU stack (one postmaster, one pair of services, no restarts):
5/5 tx and five decoded rows each, empty error streams. **Plus a NEW validation step that exercises the very
path P0-d closes** — a clean SIGTERM shutdown of both DPU services, i.e. `ResetSession → the funnel`, which
previously unbound arena slots with **zero** drain.

Both DPU logs, across the runs **and** the shutdown:

| counter | result |
|---|---|
| `ALARM` | **0** |
| `arena release waited for DMA` | **0** |
| `phase 3 not complete` (an unbind refusal) | **0** |
| `dropped grouped-control snapshot` (the Scenario-E backstop) | **0** |
| `drain timed out` | **0** |
| clean `T1 → T2 → T3 → T4` | **3× each**, one per session |

**The new drain found every slot ALREADY DRAINED and pumped ZERO rounds.** That is the predicted healthy-run
behavior *and* the proof it costs nothing on the normal path: the clean T4 teardown had already drained, so the
funnel's drain is a single query and no pump. Services exited cleanly; no hang.

### 15.6 ⚠ NEW OBSERVATION from the shutdown exercise — belongs to P0-a / P1-f, not P0-d

The SIGTERM shutdown printed, once:

```
tuple-sink service: refusing to reset peer CLIENT_SQL_SESSION before command-mailbox writers
quiesce session=1 index=0 current_sequence=39 current_kind=8 ... reset_complete=0
```

**A DIFFERENT resource — the peer command mailbox — correctly REFUSED to reset because its writers had not
quiesced, and the service then exited anyway.** That is exactly the shape P0-a and P1-f exist to fix (a guard
that refuses, followed by a leave that ignores the refusal), and it is the **first time we have seen it fire**.
It is not a P0-d regression — P0-d covers the arena slot, not the peer mailbox. **Fold this log line into P0-a's
acceptance criteria: after P0-a, a clean shutdown must not print it.**

*Rule earned: exercising the SHUTDOWN path found a live contract violation in a subsystem the change did not
touch. Shutdown is where every "we'll clean it up later" comes due at once — put it in the acceptance criteria,
not the follow-ups.*

---

## 16. ⛔ P0-e IS BLOCKED — Option A′ CONTRADICTS A DOCUMENTED, RUN-8-VALIDATED INVARIANT

**STOP. This needs a decision, not an improvisation.**

### 16.1 Option A′ step 3 is exactly the thing the code forbids

Option A′ (§13.3) says: *"the backend's bind may CAS `QUARANTINED → BOUND` — **zeroing the bodies FIRST** (the
FREE-state ABI init the reaper deferred)."*

`HomerFrontendAgentBindArenaSlot` (`homer_frontend_agent.c:568-595`) says, in a comment written after a
four-run hunt:

> *"**⚠⚠ THE P1 BLOCKER LIVED EXACTLY HERE** (root-caused by run 8, July 10, 2026). This site used to call
> `HomerFrontendAgentInitArenaSlot(slot)`… by this point **the DPU is already polling that mailbox with
> back-to-back CONTROL_READ DMAs** (run 8 counted ~100 successful proto=15 reads during backend startup, then
> ONE all-zero read). A DMA sampling inside the memset→restamp window reads an all-zero header, fails the DPU's
> snapshot protocol validation, and **permanently fatals the engine**… **do not "fix" a verification failure by
> resurrecting the bind-time reset.**"*

**Option A′ removes the reaper's re-init and then resurrects the bind-time reset to compensate — the precise
move that comment forbids.** I proposed it without reading the bind site.

### 16.2 There is NO safe HOST-side window to re-initialize a slot. None.

| candidate re-init site | why it is unsafe |
|---|---|
| **the reaper** (`ReapDeadArenaSlots:708` → `ReleaseArenaSlot:666` → `InitArenaSlot:269`) | the DPU may still have **counted, in-flight DMA writing into the slot** (`BACKEND_COMMAND_BODY`, `..._READY_SEQ`, `..._PUBLISHED_EPOCH`, `BACKEND_COMPLETION_CONSUMED_EPOCH`, byte-ring credit). The host **cannot see** the DPU's drain state. A late write lands after the re-init → the slot is FREE but **not** ABI-initialized → every future bind FATALs on the verification → **the slot is permanently poisoned.** |
| **the backend at bind** | **run 8**: the DPU is already grouped-control-polling this slot; the memset's transient-zero window fatals the engine. **AND run 9**: the DPU is the role-2 **producer** and *"may publish commands into a BOUND slot before this backend finishes starting up"* — so the memset would also **destroy a command the DPU already published**, leaving the DPU's bookkeeping claiming an in-flight command the backend will never see. |

**Both host-side windows are closed, for two independent reasons each, both already discovered the hard way.**

### 16.3 ⚠ AND THE SAME RE-INIT RUNS ON THE **CLEAN** PATH — this looks like a LIVE latent bug

`HomerFrontendAgentReleaseArenaSlot` runs from **`on_proc_exit`** and calls the same `InitArenaSlot`, which
memsets **`arena->exports[...].hostPublishLines[ringIndex .. +2]`** — *the very lines the DPU reads with
grouped-control `CONTROL_READ`* (`homer_service_dpu_dma.c:11604`: the read source is
`descriptor->hostRingAddress + descriptor->hostControlOffset`, a `HomerDpuBridgeHostPublishLine`).

And T1's quiesce covers **only** `localBase+1` (role-3 completion) and `localBase+2` (role-5 result)
(`homer_service_dpu_dma.c:6246-6272`). **`localBase+0` — the role-2 command mailbox — is DELIBERATELY left
polled**, because teardown publishes the `BACKEND_SLOT_RELEASE` command through it (§10.1 item 4).

> **So from T1 to T4 the DPU keeps grouped-control-polling role-2's publish line, while an exiting backend may
> memset exactly that line.** That is the run-8 shape, on the clean path.

Whether it fires depends on a race we have **not** proven either way: T4 unbinds and calls
`ResetRingTenancyState` (which sets `tenancyBaselinePending` and thereby **suppresses** discovery), and the
backend exits only after it *reads* the RELEASE command. If T4's unbind always wins, the memset lands on a
suppressed ring and is harmless. But T4 fires when the RELEASE **write retires** (a service-side CQE), while
the backend exits when it **polls and sees the bytes** — those are concurrent, and the backend's exit is one
scheduler pass away from beating T4. **NOT PROVEN SAFE. Treat as a live latent bug.**

### 16.4 The only party with a safe window is the DPU — and it is already standing in it

The DPU's own allocation sequence is:

```
DPU unbinds rings (⇒ post-P0-d, DRAINED)   ->  nominates slot N  ->  binds rings
   ->  submits ARENA_PUBLISH_LINE_CLEAR    ->  its COMPLETION clears tenancyBaselinePending
   ->  discovery RE-ARMS  ->  doorbell  ->  postmaster forks backend  ->  backend binds slot
```

Everything between *unbind* and *discovery re-arms* is **inside the DPU's control, with its own DMA provably
drained and no poller running**. **`ARENA_PUBLISH_LINE_CLEAR` already lives exactly there** — its completion
comment even says: *"the zeros are now IN HOST MEMORY, so this slot's rings finally have a baseline **the DPU
established itself**… Clearing the flag here, in the COMPLETION and not at submit, is the whole point."*

**The DPU already owns the re-initialization of the DPU-visible part of a slot. The host's copy of that work is
redundant AND racy.**

### 16.5 THE OPTIONS (needs an owner decision)

- **A — the DPU owns the whole FREE-state re-init (RECOMMENDED).** Extend `ARENA_PUBLISH_LINE_CLEAR` into a full
  slot re-init (publish lines **+ mailbox headers + result-ring control**, with the ABI stamps), and **DELETE**
  the host-side `InitArenaSlot` call from **both** `ReleaseArenaSlot` and the reaper. Then there is **exactly
  one writer** of the FREE-state, in a **provably drained, pre-production window**, and the host's bind-time
  verification becomes exactly what it should be: *the check that the DPU did its job.* The invariant upgrades
  from *"FREE implies initialized"* to *"NOMINATED implies initialized"* — asserted by the only party that can
  prove it. **Also fixes §16.3 for free.** Cost: the DPU must carry the slot ABI stamps
  (`protocolVersion`/`slotCount`/`ringBytes`), i.e. the ABI init logic exists on the DPU side; one larger DMA
  write instead of one small one. **No new protocol, no new round-trip, no new carrier.**
- **B — keep the host re-init, gate it on a DPU certificate.** Requires a NEW host-visible *"the DPU has
  unbound slot N"* fact. That is a new carrier — the thing §13.2 was so pleased to have avoided. **Rejected
  unless A is impossible.**
- **C — narrow scope: quarantine only, DPU re-init only for quarantined slots.** Leaves **two** re-init owners
  and does not fix §16.3. **Worst of both.**

### 16.6 Rules earned

- **A plan item that says "just zero it first" must name WHO zeroes, WHEN, and WHO IS READING IT AT THAT
  MOMENT.** Option A′ passed four adversarial review rounds with none of us asking the third question.
- **The invariant you need may already be enforced by the other side.** I spent this whole item looking for a
  host-side window. There is none — and the DPU has been standing in the only safe one all along, doing half
  the job already.
- **Read the site you are about to change BEFORE you design the change.** The bind site's comment forbids
  Option A′ by name. It cost nothing to read and would have saved the whole design.

### 16.7 ⚠ CORRECTION TO §16.3 — I NAMED THE WRONG BYTES (owner caught the framing error)

**Owner:** *"It seems that this is about the crash scenario, where the Homer user (PG backend) is gone; then why
would it be memset by the backend?"* — **Correct, and it exposed that I had conflated two actors.**

`InitArenaSlot` has **TWO** callers, with **different** hazards:

| caller | when | hazard |
|---|---|---|
| the **exiting backend**, via `on_proc_exit` → `ReleaseArenaSlot:666` | **CLEAN** exit | this is what §16.3 was about |
| the **reaper** `ReapDeadArenaSlots:708` → the same `ReleaseArenaSlot` | **CRASH** (hard kill; `on_proc_exit` skipped) | **this is P0-e** |

**§16.3 IS NOT ESTABLISHED. I named the wrong bytes.** `homer_frontend_agent.c:180-190`:

> *"Since S4.0b the 16 **ROLE-5** lines are live: each **role-5** descriptor points its `hostControlOffset` at
> `hostPublishLines[ringIndex]`… which enrols it in grouped-control discovery. **The roles-2/3 lines remain
> unused reservations**."*

So the **role-2 publish line I built §16.3 on is DEAD MEMORY.** What the DPU actually reads from a slot is
role-3's `completionMailbox` header (via `BACKEND_COMPLETION_CONTROL_READ` — *those* are run 8's "proto=15"
reads) and role-5's control (via grouped control). **T1 quiesces EXACTLY those two** (`localBase+1`,
`localBase+2`, `homer_service_dpu_dma.c:6246-6272`). **The clean path is therefore plausibly safe, for a reason
I had missed** — role-2 is left unquiesced precisely because the DPU only *writes* there.

**This is the FOURTH inferred claim about this file to be refuted by the code** (see §10.1, §14.8, §15.1).
**In this codebase, "I reasoned it must be so" has a losing record. Read the descriptor, not the struct comment
— and note the struct comment in `homer_frontend_agent.h:160-175` still describes the PRE-S4 state and says so
itself: *"Read the note there before assuming this comment still describes the end state."***

**P0-e is UNAFFECTED and still real.** In the crash case there is **no terminal completion, therefore NO T1
quiesce at all** — the DPU is still actively reading role-3/role-5 and writing role-2 — and the **reaper**
memsets all of it, with no way to know. That is the hazard, and it stands.

### 16.8 ✅ DECISION: OPTION A (owner-approved). And it does NOT touch the hot path.

**The DPU re-init must rewrite only the ABI-STAMPED CONTROL HEADERS, never the bodies:**

| region | size | DPU rewrites? |
|---|---|---|
| `commandMailbox` | **3.2 MB** | **NO** — only its ~64 B epoch header (proto, slotCount, publishedEpoch=0, consumedEpoch=0) |
| `completionMailbox` | **270 KB** | **NO** — only its ~64 B epoch header |
| `resultRingControl` | 64 B | yes (whole struct: proto, ringBytes) |
| `hostPublishLines[3]` | 192 B | yes — **already done today** by `ARENA_PUBLISH_LINE_CLEAR` |
| `resultRingStorage` | **10 MiB** | **NO** |

**The bodies never need zeroing.** The command mailbox is **epoch-indexed** and the result ring is
**frontier-based**, so nothing beyond the published frontier is ever read; a stale body under a fresh
`publishedEpoch = 0` is unreachable. The host's `InitArenaSlot` zeroes all 13.7 MB purely as **hygiene** —
cheap for a local CPU `memset`, **absurd as a PCIe DMA**.

> **Total DPU re-init write: ~400 bytes, ONCE per backend spawn, on the spawn phase machine's control path —
> before the backend even exists. It is not on the hot data path, and it does not add a round-trip.**

*Rule earned: when a fix moves work from a CPU memset to a DMA, price it by what must ACTUALLY be written, not
by what the old code happened to touch. The old code zeroed 13.7 MB because zeroing was free; naively porting
that to the DPU would have made a ~400-byte correctness fix into a 13.7 MB per-session transfer.*

---

## 17. P0-e — THE REFUTATION BROKE OPTION A. Three defects, one of them self-inflicted by P0-d.

I asked for refutation, not review. It delivered. **C1 survives; C2, C5 and C6 do not.**

### 17.1 ✅ C1 SURVIVES — bodies genuinely never need zeroing (the load-bearing claim)

Independently confirmed on **four** readers, all frontier-gated:

- backend command reader waits for `publishedEpoch == expectedCommandSequence` before copying the record
  (`remote_execution_backend_bridge.c:420-481`);
- completion producer derives `nextEpoch` from the **reset** header and publishes only after the body is ready
  (`:1828-1855`, `:1906-1908`);
- the DPU reads the role-3 control header first and only then submits the slot read for the **discovered nonzero**
  epoch (`homer_service_dpu_dma.c:12325-12357`);
- `PollCitusTupleSinkRecord` compares `consumedHead`/`publishedTail` **before touching storage**, and wrap/trailer
  handling never speculatively parses beyond the published frontier
  (`homer_tuple_queue_frontend.c:2070-2149`);
- DPU byte-ring pulls clamp to `acceptedPublishedTail` and independently reject ranges beyond it
  (`homer_service_dpu_dma.c:3088-3130`, `:4089-4207`).

> **Stale bodies are unreachable through every protocol reader once the control frontiers are reset. The
> ~400-byte header-only re-init is CORRECT; the 13.7 MB body zero is data-remanence hygiene, not correctness.**

### 17.2 ❌ C2 REFUTED — `tenancyBaselinePending` is NOT a universal arena gate

It is checked by **grouped-control submission ONLY** (`homer_service_dpu_dma.c:11528-11535`).

- **Role-3 completion polling does NOT check it** — it gates on binding + `pollQuiesced` + own in-flight
  (`:1661-1679`) and can submit a control read at `:1733`. At re-init time the rings are **freshly bound** and
  `pollQuiesced` is **false**, so **role-3 polling is ARMED while my re-init DMA would be writing that very
  header.** A real race, introduced by my own design.
- **Role-2 command publication has NO baseline/spawn/backend-live gate at all**
  (`HomerDpuDmaSubmitBackendCommandPublication`, `:2168-2299`; executor `tuple_sink_service_process.c:46452-46482`).
  Ordering is currently enforced only *higher up* (peer command landing waits for `backendLoopActive`,
  `:42604-42617`) — **not at the arena/DMA layer.**

**FIX:** `tenancyBaselinePending` must become the **UNIVERSAL** *"this ring has no trustworthy baseline yet — do
not touch it"* gate, checked by **role-3 polling and role-2 publication as well**. Cost: one bool read on a cache
line already being touched, on each path. Negligible.

**Also:** extending CLEAR from one contiguous 192 B write to **four non-contiguous** ones requires an
**AGGREGATE completion condition** — `arenaLinesCleared` must go true only after **ALL** writes physically
retire, or a partially-initialized slot becomes observable.

### 17.3 ❌ C5 REFUTED — the crash path has a LIVENESS leak, and P0-e does not fix it

**The 30 s teardown deadline is set at T1** (`tuple_sink_service_process.c:42903-42906`), and **T1 only fires on a
terminal backend completion** (`:42815-42889`). A hard-crashed backend produces **no terminal completion**, so:

> **T1 never runs ⇒ the 30 s deadline NEVER STARTS ⇒ the DPU keeps polling the dead backend's slot
> INDEFINITELY.** The host reaper edits **host** arena state only (`homer_frontend_agent.c:722-742`) and **sends
> the DPU nothing.** There is **no DPU-visible signal that a backend died.**

The slot is only reclaimed when some *other* lifecycle path fires (client disconnect → peer close → session reset
→ the funnel). **So DPU-owned re-init fixes CORRUPTION but not AVAILABILITY: the slot is never re-nominated
because the DPU never unbinds.** This is a **separate defect (call it P0-g)** and it is arguably the more
serious of the two.

### 17.4 ❌ C6 REFUTED — and P0-d ITSELF invalidated the certificate

> **"A DPU nomination is a certificate that the slot was DRAINED" rests on "unbind requires a completed drain".
> P0-d made that FALSE — by design.** `TupleSinkServiceReleaseDpuArenaBinding` falls back to
> **`force = true` (Scenario E cancellation)** on `IN_FLIGHT` / `QUERY_INVALID`
> (`tuple_sink_service_process.c:20005-20034`), which unbinds **with DMA still in flight**.
>
> **A forced unbind therefore leaves a slot that the DPU may RE-NOMINATE, and the nomination would certify a
> drain that never happened.** The escape hatch I added in P0-d punched a hole in the certificate P0-e depends on.

**FIX — a DPU-SIDE quarantine (the right side, at last):** the DPU must **refuse to re-nominate a slot whose
unbind was FORCED**, until its rings actually drain. The party that knows is the party that enforces. **No
protocol, no host involvement** — and it is the natural symmetry we kept missing: the quarantine belongs where
the knowledge is.

### 17.5 Other findings (smaller, all real)

- **A THIRD caller of `ReleaseArenaSlot` I never enumerated:** the backend applies `BACKEND_SLOT_RELEASE`
  **inside its command loop** (`remote_execution_backend_bridge.c:2660-2667`, from `:3074`, `:3201`, `:3252`) —
  not only at `on_proc_exit` (`:2584`). My "two callers" was wrong.
- **`BindArenaSlot` publishes `BOUND` BEFORE storing `ownerPid`** (`homer_frontend_agent.c:551` vs `:641`). A
  crash in that window leaves a `BOUND` slot with `ownerPid == 0`, which the reaper **skips forever**
  (`:726-729`). An unreapable slot, unrelated to the memset.
- **The reaper logs success unconditionally** (`:739-742`) even when the checked release silently no-ops on an
  owner mismatch (`:675-679`). *A diagnostic that lies.*
- **C4 partly refuted:** the DPU also reads the role-3 completion **slot body** and role-5 result **storage** —
  not merely the headers. (Does not break C1: those reads are frontier-gated.)

### 17.6 Rules earned

- **An escape hatch you add in one stage can invalidate a proof you rely on in the next.** P0-d's `force` was
  correct *for P0-d*. It silently falsified P0-e's central argument, and only an adversarial pass caught it.
  **When you add an exception, go re-check every invariant that was true before it.**
- **"X gates Y" must be checked at the layer that PERFORMS Y, not the layer that usually calls it.** Role-2
  publication is ordered correctly today by a caller several frames up. That is not a gate; it is a coincidence
  with good manners.
- **A fix for corruption is not a fix for availability.** I set out to stop a slot being poisoned and produced a
  design under which the slot is never reused at all.

---

## 18. P0-e′ — RESCOPED. The crash work is DEAD; the stage is not.

**Owner decision (July 12, 2026):** *"our backend really shouldn't normally just die… if it does that's a bug
from us. So P0-g is separate but also unnecessary… Perhaps P0-e is also unnecessary?"*

**Agreed on the premise, and it kills two items outright:**

| item | verdict |
|---|---|
| **P0-g** (crash liveness leak — no DPU-visible signal that a backend died, so the 30 s deadline never starts) | **DROPPED.** Crash-only. |
| **P0-e as framed** (the reaper memsets while DPU DMA is in flight) | **DROPPED.** The reaper only fires on `kill(pid,0)==ESRCH` — a backend that died *without* releasing. Hard-crash only. |
| **The DPU-side quarantine** (refuse to re-nominate a force-unbound slot, §17.4) | **DROPPED.** Only reachable once the DOCA engine is *already* fatally broken; P0-d already **ALARM**s there. Recorded, not built. |

### 18.1 ⚠ BUT: verifying the owner's premise surfaced a NON-CRASH race, and it is the real bug

**VERIFIED:** role-3 completion polling gates on `boundServiceSessionId != 0`, `!pollQuiesced`, and its own
in-flight flags (`homer_service_dpu_dma.c:1661-1683`). **It does NOT check `tenancyBaselinePending`.**

And `ResetRingTenancyState` — the unbind that *sets* that flag — says (`:4610-4617`):

> *"The outgoing tenant's bytes are still sitting in the host publish line right now — **the backend clears them
> ASYNCHRONOUSLY**, and the next tenant's DPU-side clear has not run yet. So this ring has **NO trustworthy
> baseline** until that clear lands: suppress its **grouped-control** reads until then."*

**It names the hazard exactly — and then guards only grouped-control.** Role-3's dedicated poll path is open:

```
T4:  DPU unbinds slot N's rings   (fires when the RELEASE *write* RETIRES — i.e. BEFORE the
                                   backend has even READ it)
     ... the backend is still alive and will re-init the slot ASYNCHRONOUSLY, whenever it
         gets round to processing BACKEND_SLOT_RELEASE ...
     a new session arrives -> HomerDpuDmaBindRingSession picks slot N
                              (its rings are unbound; it NEVER consults the HOST slot state)
     -> role-3 polling ARMS at once
     -> reads the DEAD tenant's completion header: publishedEpoch = 39
     -> ringRuntime->acceptedPublishedEpoch is 0  =>  "39 completions to pull"
     -> the PREVIOUS session's completions are delivered to the NEW one
```

**No crash anywhere in that story.** It is invisible today only because validation is **sequential** — one
pgbench at a time, so the host wins the race by milliseconds. **Concurrent multi-client sessions are exactly
where it bites, and that is on the roadmap.**

### 18.2 THE DESIGN (P0-e′) — Option A's core, minus everything crash-related

1. **`ARENA_PUBLISH_LINE_CLEAR` → `ARENA_SLOT_REINIT`.** The DPU writes the slot's **ABI-stamped control headers**
   as well as the publish lines, in the window it already occupies (post-unbind ⇒ drained; pre-re-arm;
   pre-doorbell, so no backend exists yet):
   - role-2 `commandMailbox` epoch header (proto, slotCount, publishedEpoch=0, consumedEpoch=0)
   - role-3 `completionMailbox` epoch header (proto, publishedEpoch=0, consumedEpoch=0)
   - role-5 `resultRingControl` (proto, ringBytes) — 64 B
   - `hostPublishLines[3]` — 192 B, **already written today**

   **~400 B, 4 NON-CONTIGUOUS writes** (the headers are megabytes apart inside the slot), so it needs an
   **AGGREGATE completion condition**: `arenaLinesCleared` goes true only when **ALL FOUR** physically retire, or
   a half-initialized slot becomes observable (§17.2).

   **The BODIES are NOT written** — 13.7 MB, and provably unreachable: the command mailbox is **epoch-indexed**
   and the result ring **frontier-based**; C1 confirmed this on four independent readers (§17.1).

2. **`tenancyBaselinePending` becomes the UNIVERSAL gate** — checked by **role-2 publication** and **role-3
   polling** as well as grouped-control discovery. One bool read on an already-hot cache line, on each path.
   *"X gates Y" must be checked at the layer that PERFORMS Y* (§17.6).

3. **Delete `HomerFrontendAgentInitArenaSlot` from `HomerFrontendAgentReleaseArenaSlot`** — a *consequence*, not
   extra work. `ReleaseArenaSlot` then only publishes `FREE`. **NOTE: it has THREE callers, not two** (§17.5):
   the reaper (`homer_frontend_agent.c:739`), `on_proc_exit` (`remote_execution_backend_bridge.c:2584`), and —
   the one I missed — the backend applying `BACKEND_SLOT_RELEASE` **inside its command loop** (`:2660`, from
   `:3074`/`:3201`/`:3252`).

   **`HomerFrontendAgentCreateArena`'s whole-arena zero + per-slot init STAYS** (postmaster start, fresh process).

4. **`HomerFrontendAgentBindArenaSlot`'s verification stays EXACTLY as is** — it becomes what it should always
   have been: **the check that the DPU did its job.** The invariant upgrades from *"FREE implies initialized"* to
   *"NOMINATED implies initialized"*, asserted by the only party that can prove it.

### 18.3 It pays for itself twice

- **Correctness:** kills the §18.1 concurrency race **by construction** — there is no host/DPU re-init ordering
  left to lose. (And it happens to fix the dropped crash case for free.)
- **Latency:** `InitArenaSlot` memsets **13.7 MB on the backend's session-close path** — order **1–3 ms of pure
  `memset`**, every session close, delaying the slot's return to `FREE`. Deleting it is a real win on a stack
  whose steady state is 3.14 ms/tx.

### 18.4 Rule earned

> **Interrogating a "this is out of scope" premise is not wasted work.** The owner was right that the crash paths
> do not matter — and checking *why* they did not matter is what exposed the race that does. **The scoping
> question and the bug hunt are the same question asked twice.**

### 18.5 ⚠ SELF-CORRECTION — I OVERSTATED THE BUG. It is a LIVENESS failure, not data corruption.

**Before the reviewer could, I traced §18.1's link L7 to its consumer and it does not hold.**
`HomerServiceDpuAcceptOneBackendCompletion` guards at `tuple_sink_service_process.c:42784-42795`:

```c
if (!selectedSession->commandInFlight ||
    selectedSession->inFlightCommandSequence != stagedCompletion.completion.commandSequence)
{
    ... return false;   /* a service ERROR -- not a silent acceptance */
}
```

A brand-new session has `commandInFlight == false`, so a stale record carrying the dead tenant's
`commandSequence = 39` is **REJECTED**. The completion record carries **no session id**
(`homer_completion_abi.h:35-55`) — `commandSequence` + `commandInFlight` is what saves us. **There are two more
guards behind it**: `commandKind` must match the in-flight kind (`:42796`), and the completion epoch must exceed
`lastAcceptedBackendCompletionEpoch` (`:42816`).

> **So the previous session's completions are NOT delivered to the new one. My "silent data corruption" framing
> was WRONG.**

**But the race does not evaporate — it changes shape.** The DPU still reads the stale `publishedEpoch = 39`,
still submits a slot read, still stages the dead tenant's record — and the guard then **fails the entire
progress action** (`return false`), killing a **legitimate new session** with
`"backend completion does not match selected-DPU in-flight command"`.

| | before | after |
|---|---|---|
| **severity** | silent wrong-data delivery | **spurious HARD FAILURE of a legitimate session, under concurrency** |
| **detectability** | silent | **loud** (a specific error string) |
| **the fix** | unchanged | unchanged — a stale header must not be READABLE at all |
| **acceptance** | — | **that error string must never appear in a concurrent multi-session run** |

**Rule earned (the fifth time this subsystem has caught me):** *I keep reasoning FORWARD from a mechanism and
forgetting to ask who is checking DOWNSTREAM. The guard that saves us here is three functions away from the code
I was reading.* **Trace to the CONSUMER before you claim a producer's mistake reaches anyone.**

**This does NOT change the P0-e′ design** (§18.2): the DPU owning the re-init still removes the stale-header read
by construction, still deletes the 13.7 MB session-close `memset`, and the universal `tenancyBaselinePending`
gate is still what closes the window. **It changes the JUSTIFICATION (liveness, not integrity) and the
ACCEPTANCE CRITERIA (a concurrent run must not produce that error).**

---

## 19. ⛔ P0-e′ IS DROPPED. The bug was REFUTED and the design was broken. What survives is better.

### 19.1 The bug does not exist — and my error is precise

**REFUTED (L7, the load-bearing link):** the role-3 control read takes **BOTH `publishedEpoch` AND
`consumedEpoch`** from the host header in ONE snapshot (`homer_service_dpu_dma.c:12325-12344`), and the polling
loop computes `nextCompletionEpoch = consumedEpoch + 1`; if `publishedEpoch < nextCompletionEpoch` it
**discards the snapshot with no slot read** (`:1697-1712`). And **every accepted completion publishes
`consumedEpoch` back** (`tuple_sink_service_process.c:42831-42845`), a role-3-counted write that **P0-d's drain
now guarantees retired before unbind**.

> **A cleanly-torn-down slot's header therefore reads `publishedEpoch == consumedEpoch` — CREDITED, not
> "stale with 39 unread completions". A fresh control read sees nothing to pull. THE BUG IS NOT REAL.**

**MY ERROR, EXACTLY: I compared a HOST field against a DPU field.** I assumed the DPU checks the host's
`publishedEpoch` against its own freshly-reset `ringRuntime->acceptedPublishedEpoch`. It does not — it checks
the host's `publishedEpoch` against the **host's own `consumedEpoch`**, both from the same snapshot.
**The header is SELF-DESCRIBING.** I invented a cross-reset comparison the code never makes.

**L6 also refuted:** binding makes role-3 *eligible* in the engine but does not *schedule* it. The collector
takes demand only from a selected session with `commandInFlight` (`tuple_sink_service_process.c:41765-41793`,
`:44099-44136`). A freshly-bound, still-spawning session has none.

### 19.2 The design was broken independently (so it would have failed even if the bug were real)

- **The DPU CANNOT ADDRESS role-5's `resultRingControl`.** The role-5 descriptor is rebased to
  `HomerFrontendArenaExport`: its `hostControlOffset` addresses the **publish line**, and its `hostRingOffset`
  points at **`resultRingStorage`** (`homer_frontend_agent.c:949-992`). The control struct has **no descriptor
  address at all**. The DPU could only reconstruct it by implicit layout arithmetic — **ABI-dishonest**.
- **The backend still WRITES role-5 control**, at startup (`remote_execution_backend_bridge.c:3027-3033` →
  `RemoteExecResetResultQueueControl`, `:633-641`) **and again on tuple-shape changes** (`:1110-1129`). The
  latter is **semantic**, not initialization. **The DPU could never have been the sole owner.**
- My field list was incomplete: role-5 control also needs `flags = CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PRODUCER_OWNED`.

### 19.3 ⚠ THE REAL RACE — and it is the SOURCE-vs-TARGET rule (§0.3) at a new layer

**VERIFIED:** T4 unbinds the DPU-side rings once the RELEASE **WRITE RETIRES**
(`tuple_sink_service_process.c:43818-43842`) — **not** once the backend has **CONSUMED** it.
`HomerDpuDmaBindRingSession` then selects a slot purely from DPU ring state and **never reads the host
`slot->state`** (`homer_service_dpu_dma.c:5947-6080`).

> **So the DPU can RE-NOMINATE a slot before the old backend has published host `FREE`. The newly spawned
> backend's `FREE → BOUND` CAS then FAILS and it FATALs** (`homer_frontend_agent.c:552-557`,
> *"arena slot %u is already bound"*).

**This is §0.3's SOURCE-vs-TARGET distinction, exactly:** *"my CQE retired" proves the bytes LANDED — it does
NOT prove the peer ACTED on them.* The DPU released the slot on the strength of **its own** retirement, when
what it needed was the **backend's acknowledgement**. **The contract caught it; my bug hunt did not.**

**Reachability: REAL but UNLIKELY.** The new backend must be forked *and started* before the old one drains its
mailbox — fork + backend startup is **milliseconds**, the old backend's poll loop is **microseconds**. The old
backend wins almost always. **And it fails LOUDLY** (`FATAL`, named slot), never silently.

**NOT FIXED, recorded.** The cheap fix, if it ever fires: the DPU must not nominate a slot until it has the
backend's host-`FREE` acknowledgement (a `slot->state` DMA read on the cold spawn path, or T4 deferring unbind).
**A ~400-byte re-init does not close it** — the missing fact is *"the backend consumed RELEASE and published
FREE"*, not *"my DMA writes retired"*.

### 19.4 What actually survives: C1, and a cheap perf option

**C1 is the one durable finding** (§17.1, confirmed on four independent readers): the 13.7 MB of slot **bodies**
are **provably unreachable** — the command mailbox is epoch-indexed, the result ring frontier-based.

That licenses a **tiny, safe, architecture-free change** if we ever want it:
**`HomerFrontendAgentInitArenaSlot` need only re-init the ~200 B of ABI headers, not `memset` 13.7 MB of
bodies.** The host keeps ownership; no ABI change; no DPU involvement.

**Payoff, honestly stated:** ~1–3 ms off each **session close**. That is **NOT the hot path** — for `-t 2000`
it is 1–3 ms out of ~6.3 s. It only matters for **session-churn** workloads. **Logged as an optional perf item;
not doing it now.**

### 19.5 Rules earned — the expensive ones

- **Trace to the CONSUMER before you claim a producer's mistake reaches anyone.** (I caught this myself on the
  first pass — see §18.5 — and it *still* was not enough, because the real refutation was one layer further
  down.)
- **When a struct carries both a producer and a consumer frontier, IT IS SELF-DESCRIBING.** Do not reason about
  it by comparing one of its fields to a variable you happen to be holding. **Read what the code compares.**
- **A design can be dead twice.** Even had the bug been real, P0-e′ could not have been built: the DPU cannot
  address role-5 control, and the backend legitimately owns it. **Check that the fix is IMPLEMENTABLE before
  arguing about whether it is NEEDED.**
- **Six refuted inferences in one subsystem is not bad luck — it is a signal about the method.** Every single
  one was a forward chain from a mechanism I had just read, with no check of who was reading downstream. The
  adversarial pass is not a formality here; it is the only thing that has been reliably right.

---

## 20. P0-a — IMPLEMENTATION SPEC (peer-client completion publish: the source MR is released under a live WR)

**Status: IN PROGRESS.** Line numbers below are current (post-P0-d).

### 20.1 The smoking gun, restated from the code

Two sibling functions, **fifty lines apart**, both called from `TupleSinkServiceResetSession` (`:23660`, `:23665`):

| | `ClearClientSqlCommandWriteCompletionsForSession` (`:6550`) | `ClearPeerClientCompletionPublishCompletionsForSession` (`:6604`) |
|---|---|---|
| finds an active owner | **REFUSES** — prints, then `exit(1)`, with a comment naming the exact use-after-free | **BLINDLY CLEARS IT** (`:6619`) |
| why it can | its signal policy has a **TERMINAL CASE** | its signal policy **HAS NONE** |

`TupleSinkServiceClientSqlCommandWriteShouldSignal` (`:6625`) force-signals when
`commandKind == CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE` (`:6638`), **then** falls back to ring
pressure and a signal interval.

`TupleSinkServicePeerClientCompletionPublishShouldSignal` (`:5560`) has **only** ring pressure and the interval.
**It does not even RECEIVE the command kind** — its parameters are `(sessionState, reservedSourceCount)`, so it
*cannot* know the publish is terminal.

> **So the LAST completion publish of a session is typically UNSIGNALLED. No CQE is coming. The lane FIFO
> (`:5935`) retires cumulatively only when a LATER signalled publish arrives — and after close there is none.
> The owner can therefore NEVER retire, which is precisely why `:6604` had to clear it blindly — and that clear
> is what lets session reset deregister the source MR under a live WR.**

**The blind clear is not the bug. It is the SYMPTOM of a signal policy with no terminal case.**

### 20.2 The fix — phase by phase

| phase | what |
|---|---|
| **1 DECIDE** | session close (exists) |
| **2 QUIESCE** | the publish path already stops at close; **assert** it rather than adding a flag (see §20.4) |
| **3 DRAIN** | **(a)** give `PeerClientCompletionPublishShouldSignal` the **TERMINAL CASE** — take the `commandKind`, force-signal on `CLIENT_SQL_SESSION_CLOSE`. **NOT** `TupleSinkServiceCommandStateIsTerminal` (`:17927`), which means `COMPLETED\|\|FAILED` = **EVERY command**, and would put a signalled WR on the **hot path**. **(b)** then **BOUNDED SYNCHRONOUS DRAIN** in the funnel: pump `HomerServiceDrainTypedPeerSendCq` (`:3889`) until this session's active owners reach zero, or a deadline. |
| **4 RELEASE** | only then clear owners / reset the session |

**On non-convergence: ALARM + route to Scenario E** (the connection reset already abandons owners and records
what it discarded, §9.4). **Do NOT silently clear.** This also covers the abort path that never publishes a
close: it will hit the deadline, say so, and cancel honestly rather than pretend.

**`exit(1)` at `:6600` also goes.** A *signalled* post can legitimately still be un-polled at reset (CQ
retirement is async), so the exemplar's refusal is right but its remedy is too violent. **Both families get the
same bounded drain.** On non-convergence: **ALARM + Scenario E, NOT `FATAL`** — the 2 s bounded drain is exactly
what distinguishes *"not yet"* from *"never"*, and once it has expired the honest act is to cancel and say so.

### 20.3 ⚠ RE-ENTRANCY — the same question P0-d had to answer, and it must be answered the same way

`HomerServiceDrainTypedPeerSendCq` is called today from the progress pump (`:6741`, `:14573`). The funnel
(`TupleSinkServiceResetSession`) would now call it too. **If `ResetSession` were ever reachable from INSIDE a
send-CQ drain callback, the pump would recurse.**

The four drain callbacks are retirement handlers (`HomerServiceHandleCommandSendCqeFromDrain`,
`HomerServiceHandlePayloadSendCqeFromDrain`, a diag probe, and the peer-client-completion retire), and no
`ResetSession` call site sits inside them. **But that is an INFERENCE, and inference has a 0-for-6 record in
this subsystem.** So: **guard it by construction.** A `SendCqDrainDepth` counter; the funnel's drain refuses to
pump if depth > 0 and says so **LOUDLY** — re-entry is a bug we want to SEE, not paper over.
*(Contrast P0-d, where re-entrancy was ruled out by PROOF: the DOCA callback never calls service code. Here we
cannot prove it as cheaply, so we assert instead of assuming.)*

### 20.4 Also in scope

- A **partial post** leaves a slot `FAILED_OR_INFLIGHT` (`:19043`) that no CQE can identify. Phase 3 cannot
  retire it. **Route it to Scenario E EXPLICITLY** (§9.4) rather than letting it look drained.
- **ACCEPTANCE (from P0-d's shutdown run, §15.6):** a clean SIGTERM shutdown must **no longer print**
  `refusing to reset peer CLIENT_SQL_SESSION before command-mailbox writers quiesce … reset_complete=0`
  (`:23669`). That line is a guard refusing to release, followed by a leave that ignores the refusal — the exact
  shape this item exists to fix, and P0-d's shutdown exercise is the first time we saw it fire.

### 20.5 ⚠ CORRECTION — §15.6's acceptance criterion belongs to P1-f, NOT P0-a

I attributed the shutdown line to P0-a. **Wrong, and it would have produced a false FAIL.**

```
tuple-sink service: refusing to reset peer CLIENT_SQL_SESSION before command-mailbox writers
quiesce session=1 ... reset_complete=0
```

comes from `TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister` (`:24152`), which gates on
`peerLifetime.peerWritersQuiesced || peerLifetime.connectionResetComplete`.

**That is a DIFFERENT RESOURCE.** It is the **peer COMMAND MAILBOX** — a **remote-writable MR that the PEER
writes into**, i.e. **Scenario C** (*"the remote peer writes into my memory; I cannot see their completions"*).
**P0-a retires OUR OWN send-completion owners** (Scenario A). It cannot possibly clear that guard.

**That line is P1-f's acceptance criterion** (§9.3/P1-f: *"`peerWritersQuiesced` must become a RECEIVED FACT,
not a locally-inferred one — today it is set merely on OBSERVING the close"*). Moved.

**P0-a's own acceptance:**
- the blind clear at `:6604` is gone, and `exit(1)` at `:6600` is gone;
- **`ALARM` count == 0** across runs and a clean shutdown (no non-convergence, no re-entrancy);
- the DPU-command pgbench path still passes 5/5 with five decoded rows (the force-signal must not perturb it);
- **no per-command CQE regression**: the ONLY new signalled WR is one per *session close*.

*Rule earned: two resources whose guards print similar-sounding refusals are still two resources. **Before you
adopt a log line as an acceptance criterion, find the function that emits it and check WHAT it is protecting.***

---

## 21. P0-i — THE UNSIGNALLED-TAIL INVARIANT. A substrate property, not a per-policy habit.

**Owner, July 12, 2026, on P0-a:** *"The optimization is fine; it was adopted without its end-of-stream case…
`'A signalled WR always eventually follows'` — that IS the understated invariant that the RDMA substrate relies
on and should uphold — **a publication gate**."*

**Correct, and it exposes the limit of P0-a's fix.** P0-a is a **per-site** repair: it taught ONE signal policy
about `CLIENT_SQL_SESSION_CLOSE`. **That is the same scar-tissue pattern this audit exists to end** (§3.0) —
the command-write lane had already been fixed the same way, once, by hand.

### 21.1 The invariant, stated

> **A lane that DEFERS retirement to a later signalled WR must GUARANTEE that a later signalled WR exists.**

Selective signalling is safe because RC QPs complete **in order**, so one CQE retires the whole unsignalled tail
behind it (the "lane FIFO"). **The scheme is sound while traffic continues and breaks exactly at the end — which
is also the moment you release the resource.**

### 21.2 Why P0-a's fix does NOT generalize

Force-signalling a **terminal message** works only if the lane HAS one:

- an **ABORT** path may never send a close → **no terminal message to force-signal**;
- a lane that simply **goes quiet** (a payload stream that finishes) has **no "last command" at all**.

### 21.3 THE SUBSTRATE FIX — a signalled FENCE at quiesce

**At quiesce, if a lane has ANY unsignalled WR outstanding, POST A SIGNALLED ZERO-LENGTH WR on that QP.**
RC in-order completion then retires the entire tail.

> **This is phase 3 in its LITERAL form — *"finish AND drain"*. Sometimes draining requires EMITTING something,
> not merely waiting for it.** One fence per lane per close, on the **cold** path. No hot-path cost, no protocol
> change, no new message on the wire that the peer must understand (a zero-length RDMA write to an already-valid
> remote address is invisible to the peer's software).

Then the per-policy terminal cases (P0-a's, and the command-write lane's) become **optimizations**, not
correctness — they merely avoid the fence in the common case. And the invariant becomes an **assertion**: at
release, a lane with unsignalled WRs and no fence posted is an **ALARM**.

### 21.4 ⚠ THE SECOND INSTANCE — and this one may be a LIVE HANG (UNVERIFIED — verify FIRST)

The **payload lane also uses selective signalling** — a **byte-interval** policy
(`payloadBytesPerSignaledCqe`, `payloadSignaledCqes`, `tuple_sink_service_process.c:2225`, `:31105`, `:32411`).

Its close gate (`:25845`, `:27270`) requires `finalPayloadSendRetired`, which requires
`payloadSendOwnerCount == 0 && payloadSendOwnerOutstandingWrs == 0` (§2.1 records this as HOLDS).

**I found NO force-signal on its final payload send.**

> **If the last payload WR lands UNSIGNALLED, its owner never retires, `finalPayloadSendRetired` never goes
> true, and `CLOSE_SINK` NEVER FIRES — the stream close HANGS.**

**Same shape as P0-a — but where P0-a's failure mode was a LEAK (and a blind clear), this one's is a HANG.**

**Why we may never have seen it:** our payload workload is basebackup, where a *byte*-interval policy over
hundreds of MB almost certainly forces a signal close to the end. **A short stream (few KB, one WR) is exactly
where it would bite.** ⚠ **UNVERIFIED. This is an INFERENCE, and inference has a 0-for-6 record in this
subsystem (§19.5). VERIFY BEFORE BUILDING — trace the final payload send and find out whether anything signals
it.**

### 21.5 Scope

1. **AUDIT every lane that posts with a runtime `signaled` flag** and record, per lane, *what guarantees a
   signalled WR follows*. Known sites: `TupleSinkServicePeerClientCompletionPublishShouldSignal` (fixed, P0-a),
   `TupleSinkServiceClientSqlCommandWriteShouldSignal` (fixed long ago, by hand), the **payload lane** (§21.4),
   and every `signalCompletion` parameter through `remote_execution_peer_transport_rdma.c:9258-9427`, `:5116`,
   `:9640`.
2. **Add the fence primitive** to the peer transport: `PostSignalledFence(connectionHandle)`.
3. **Call it at quiesce** from every lane whose tail may be unsignalled.
4. **Assert the invariant at release** — unsignalled tail + no fence = ALARM.

### 21.6 Priority

**Above P0-b and P0-c.** Those are leaks and latent races; **§21.4 may be a live hang.** But the FIRST
deliverable is the **audit**, not the fence: *find out whether the payload lane is actually exposed before
building anything.*

### 21.7 Rule earned

> **Any deferred or cumulative acknowledgement scheme needs a terminal flush.** Batching is safe *while traffic
> continues* and breaks *exactly at the end* — which is precisely when you release the resource. **Whenever you
> see "we'll retire this later, when the next one completes," ask what happens when there is no next one.**

### 21.8 ✅ THE AUDIT LANDED — my payload HANG is REFUTED, and the real gap is ABORT-PATH ONLY

**§21.4 REFUTED (VERIFIED).** `payloadBytesPerSignaledCqe` / `payloadSignaledCqes`
(`tuple_sink_service_process.c:31101`, `:31760`, `:32407`) are **STATISTICS updated after a batch — NOT inputs
to a signalling predicate.** I read a stats counter and inferred a policy. **Seventh refuted inference in this
subsystem — but this time I predicted it and verified BEFORE building. The process worked.**

**The payload lane is safe BY CONSTRUCTION, and it is THE EXEMPLAR.** Its signalling rule is **positional and
unconditional — every batch's LAST WR is forced signalled**:

- fixed-slot: the last payload WR is converted to `WRITE_WITH_IMM | IBV_SEND_SIGNALED`
  (`remote_execution_peer_transport_rdma.c:9811-9838`);
- byte-ring: a **separate signalled tail WIMM is APPENDED** to the batch (`:10039-10077`).

> **That is EXACTLY the fence, already implemented — as a per-batch idiom.** A one-row pgbench stream does not
> hang because its single batch still ends with a forced-signalled WIMM. **The invariant is not undocumented
> luck; it is built into the batch shape.** The fence is not a new idea in this codebase — **it is an existing
> idiom that the two runtime-selective lanes never adopted.**

**Credit ACKs also batch and are also SAFE:** closing forces a below-threshold credit publication and the
dedicated final-head WR is **always** signalled (`tuple_sink_service_process.c:33627-33636`, `:35557-35570`).

**Only TWO lanes choose signalling at runtime** — command-write (`:6744`) and peer-client-completion publish
(`:5566`) — and **both now carry the `CLIENT_SQL_SESSION_CLOSE` terminal case.** The **NORMAL path is covered.**

### 21.9 ⚠ THE REAL GAP — and P0-a's own fallback is the tell

> *"If abnormal teardown bypasses the terminal command, an owner can remain until the reset drain times out; the
> fallback then clears owners despite explicitly warning that source memory may still be read by the RNIC."*

**That is P0-a's Scenario E fallback (`:6709-6736`) — a LOUD use-after-free.** On an **ABORT path there is no
close message**, so nothing force-signals, the tail stays unsignalled, the 2 s drain cannot converge, and we
clear anyway. **I made it LOUD. I did not make it SAFE.**

**The fence closes exactly this**, and then the drain **always converges** — making Scenario E on these two lanes
**unreachable**, which is where it belongs (reserved for a genuinely broken transport, not for a missing flush).

### 21.10 ⚠ A CORRECTION THAT CHANGES THE FENCE'S DESIGN

**My assumption that a signalled WR on a SHARED QP retires every lane's tail is WRONG (VERIFIED).**

- QPs are **not** shared across all four lanes: command + peer-client-completion share the **control** QP;
  payload + ACK/credit share the **stream** QP (`:19138`, `:21485`, `:31033`, `:32996`).
- **And even on a shared QP it would not work:** CQ dispatch is **per-WR-ID CLASS**
  (`remote_execution_peer_transport_rdma.c:3770-3800`, `:3828-3854`). **A command CQE does NOT retire
  peer-completion owners, nor vice versa.** RC ordering makes the *hardware* retire in order; the *software*
  owners are retired by class-dispatched callbacks.

> **So the fence must carry ITS OWN LANE'S WR-ID encoding**, so its CQE dispatches to that lane's cumulative
> retire-through. Cheap — but I would have got it wrong.

### 21.11 P0-i RESCOPED — small, and no longer urgent

| | |
|---|---|
| **scope** | a signalled zero-length **fence** at quiesce, on the **two runtime-selective lanes only**, carrying that lane's WR-ID class |
| **fixes** | the **ABORT-path** unsignalled tail → makes P0-a's Scenario E clear unreachable |
| **does NOT need to touch** | the payload lane or the credit lane — **both already fence themselves** |
| **priority** | **NOT above P0-b/P0-c.** The hang I feared does not exist; the normal path is covered; the gap is abort-only and already **loud** |

**Rule earned (again, and this one is getting expensive):** **a counter named `...PerSignaledCqe` is a STATISTIC
until you find the `if` that reads it.** I inferred a *policy* from a *metric*. **Find the predicate, or you do
not know the policy.**

### 21.12 ✅ P0-i LANDED AND VALIDATED (citus `67ebc0167`) — and the OWNER's rule replaces mine

**Owner, July 12, 2026:** *"I would be more relaxed than this… amortize across units only if you KNOW there are
more units for sure, not just opportunistically and eventually say 'oh there's no more coming, I guess I'll do a
signalled flush'. Though with that said, this flush may also be viable, should we need that for performance."*

**THE RULE OF RECORD (supersedes my "never amortize across units", which was too absolute):**

> **You may defer a WR's retirement to a later signalled WR ONLY IF THAT LATER WR IS GUARANTEED. Never defer on
> the mere EXPECTATION that more work will arrive.**
>
> A successor is guaranteed in exactly two cases:
> **(a)** it is inside the **same logical unit** — a unit always ends, so you may leave every WR of a batch
> unsignalled and force-signal the last one (**this is what the PAYLOAD lane does, and it is the exemplar**); or
> **(b)** you have **already committed** to posting it (N queued, posting all N).
>
> **Amortizing across units you have already queued is FINE. Amortizing on a HOPE is not.**
> **And the FLUSH stays in the toolbox** — if a lane ever needs amortization without a guaranteed successor, a
> signalled fence at quiesce is a legitimate way to buy it.

**My error:** I generalized from *"the payload lane amortizes within a batch"* to *"never amortize across units"*
— banning a safe practice because the one broken instance happened to sit on the other side of that line. **The
defect was never cross-unit amortization; it was amortization against traffic that may simply STOP.**

### 21.13 ⚠ AND THE LAYERING — a substrate/scheduler conflation that helped hide the bug

**Owner:** *"the intervals are more for the scheduler (the heuristics)… not sure we should couple those into the
RDMA substrate contract/principle. We should be more careful here."*

**Right, and the old code HAD coupled them:**

| concern | owner | decides | cost of being wrong |
|---|---|---|---|
| **signalling** | **RDMA substrate** | *"must I ask the HCA for a CQE, or may a successor retire this owner?"* | **CORRECTNESS** — an unsignalled WR with no successor can never retire |
| **CQ poll threshold** | **scheduler** | *"is it worth draining the CQ this pass?"* | **latency only** |

**THREE scheduler poll-readiness predicates were using `SIGNAL_INTERVAL` as their threshold** (`:6822`, `:8609`,
`:14883`). **That is how a substrate constant came to look load-bearing in code that has nothing to do with
signalling — and it is part of why this bug was hard to see.**

**Now separated:** polling has its own `HOMER_SERVICE_PEER_CLIENT_COMPLETION_PUBLISH_POLL_THRESHOLD` at the same
value (scheduler behaviour unchanged), and the signalling predicates read **no scheduler constant at all**.
**Keep them separate.**

### 21.14 What shipped

- **Both control lanes signal EVERY write.** The predicates collapse to `return true` and explain why.
  *(The command-write lane's `ShouldSignal` was ALREADY constant-true — its interval was 1, so its
  `SESSION_CLOSE` branch was **dead code** and the whole predicate a **decoy that looked like a policy**.)*
- **Both signal-interval macros DELETED, replaced by `#error` TRIPWIRES**, so a `-D` flag cannot silently re-arm
  opportunistic amortization — *which is exactly how the original shipped.* The tripwire **GATES rather than
  forbids**: it names the terminal flush you would have to add first. **VERIFIED IT FIRES**
  (`-DHOMER_SERVICE_PEER_CLIENT_COMPLETION_PUBLISH_SIGNAL_INTERVAL=8` now fails the build with that message).
- **P0-a's terminal special case is now redundant** and gone. So is the fence I was about to build.

**PERFORMANCE — measured, not assumed.** The plan feared *"a signalled WR on the hot path"*. **tps
109.9 / 124.4 / 122.4 — squarely inside the 95–128 band** of the three preceding validations of this exact smoke
(P0-f 95.2/125.3/104.1, P0-d 114.6/125.3/121.4, P0-a 107.2/128.3/122.3). **No regression.** The refutation had
been sitting two hundred lines away the whole time: the sibling lane already signalled every write, at the same
rate, on the same QP.

**ALARM = 0** across three runs and a clean shutdown, on both nodes; P0-d's teardown still 3/3/3/3.

### 21.15 Rules earned

- **A tunable default is not an invariant.** The bug shipped as a `#define` someone could set; the fix is a
  `#error` someone must argue with. **If a constant can silently reintroduce a use-after-free, it should not be
  a constant.**
- **Do not generalize a rule from the one instance that broke.** My "never across units" banned safe practice
  because the broken lane happened to fall on that side of the line. **Ask what the broken case actually lacked**
  — here, a *guaranteed* successor, not a *same-unit* one.
- **When a constant is used by two layers, it belongs to neither.** A substrate signalling interval doubling as a
  scheduler poll threshold made both harder to reason about, and made the substrate constant look load-bearing
  where it was not.

---

## §22 — P0-b: the control op releases its slot on RESPONSE MATCH, not on physical retirement

**Status: SPEC (2026-07-12). Supersedes the P0-b entry in §9.3 — the hazard §9.3 stated is REFUTED.**

### 22.1 What §9.3 claimed, and why it is WRONG

§9.3 said: *"`TupleSinkServiceReleasePeerControlOp` memsets the op slot INCLUDING `requestMessage`, the
registered RDMA SOURCE BUFFER, before its send CQE is polled."*

The **first half is true**; the conclusion — that this is a live use-after-free — is **not reachable today**.
Both release sites are safe, each by a proof that is **written nowhere in the code**. That is the actual defect.

This is the **eighth** refutation of one of my own inferences in this audit, and it has the same shape as the
previous seven: a forward chain from a mechanism I had just read (*a memset of registered memory*), with **no
check of what downstream actually guarantees**.

### 22.2 VERIFIED facts (all `remote_execution_peer_transport_rdma.c` unless stated)

| # | fact | evidence |
|---|---|---|
| F1 | The control publish is **exactly ONE WR**: `IBV_WR_RDMA_WRITE_WITH_IMM`, **always `IBV_SEND_SIGNALED`** (`signaled=true` is a literal at the call). A CQE is therefore **guaranteed** for every posted request. | `:7614-7617` → `:9640` |
| F2 | Its source is `opState->requestMessage`, which lives **inside** `connectionState->controlOps[]`, which **is** the registered MR `controlOpsMr`. | `:10634-10636`, `:4624-4627` |
| F3 | `sizeof(TupleSinkServicePeerControlMessage)` = **34,160 B** (DWARF, from the built service binary), vs an mlx5 `max_inline_data` of tens–hundreds of bytes. **`IBV_SEND_INLINE` (`:9641`) can NEVER be taken for a control message.** The source is genuinely live after `ibv_post_send` returns. | `gdb -batch -ex 'print sizeof(...)'` |
| F4 | The publish primitive **states the contract in its own comment**: *"The message source is op-owned or response-slot-owned and remains valid until the tagged send CQE retires it."* | `:7608-7613` |
| F5 | `TupleSinkServiceRetireControlSendCompletion`'s **response** arm honors F4 (`controlResponsePublishSlots[i].inUse = false`). Its **request** arm is a **STUB**: it decodes, range-checks, generation-checks — and then **`return true` in BOTH branches**. | `:3695` vs `:3707-3719` |
| F6 | Exactly **TWO** release call sites: `:10644` (post failed) and `:10990` (response matched). The stub's comment blames *"a blocking adapter may time out and release an op"* — **NO SUCH CALLER EXISTS.** A decoy. | `grep TupleSinkServiceReleasePeerControlOp` |
| F7 | The WR-ID packs `slotIndex` in bits 0–15 and the **full 32-bit** `slotGeneration` in bits 16–47. **No truncation, no aliasing** — a generation check on a control CQE is trustworthy. | `:123-125`, `:3259-3269` |
| F8 | `CITUS_REMOTE_EXEC_PEER_CONTROL_OP_SLOTS` = 64; `sizeof(TupleSinkServicePeerControlOpState)` = **68,304 B** → the op table alone is **4.4 MB** of registered memory per connection. | `..._rdma.h:55`; DWARF |

### 22.3 The two UNWRITTEN proofs that make today's code safe

**Proof A — `:10644`, the post-failure release, always has ZERO WRs in flight.**
`HOMER_PEER_POST_FAILED_PARTIAL` requires `postedWrCount > 0` (`:9587`), but `postedWrCount` comes from
`TupleSinkServiceCountPostedWorkRequests(first, bad, /*maxWorkRequests=*/1)` (`:9545`, called at `:9657`), and on
a **one-WR chain** verbs sets `bad_wr == first_wr`, so the loop breaks at once and returns **0**. Therefore
`FAILED_PARTIAL` is **unreachable on the control lane**, and every failure release happens with nothing posted.
**Safe — because the chain happens to be length 1.** Nothing says so; nothing checks it.

**Proof B — `:10990`, the response-match release, is retiring an already-dead buffer.**
The peer cannot have produced a response carrying `(opIndex, generation, messageSequence, requestKind)` unless
our WIMM **landed in its mailbox** — which means our HCA finished reading `requestMessage`. So the source is
physically retired at the moment the response matches. This is a **real** proof, and it is *stronger* than a
CQE (§0.3: it proves the peer **acted**, not merely that the bytes landed). **It appears nowhere in the code.**

### 22.4 Why P0-b must be done ANYWAY — the proofs are exactly what P7b will break

Both proofs are load-bearing, invisible, and one edit away from false:

- **Proof A dies** the moment the control publish becomes 2 WRs (a tail write, an SGE chain, a batched publish).
  `FAILED_PARTIAL` becomes reachable, `:10644` memsets a **live** source, and **nothing warns**.
- **Proof B dies** the moment any control op is released for a reason *other than a matched response*: a timeout
  (which the decoy comment already claims exists!), a caller abandoning an async op, a fire-and-forget control
  message, or a **pooled connection** recycling its op table on detach.
- Meanwhile the **enforcing function is a stub**, and its comment actively **misdirects** the next reader toward
  a nonexistent timeout path — so a maintainer who wonders "what retires the request source?" reads `:3711-3715`,
  concludes it is handled, and moves on.

**P0-b is therefore rescoped: not "fix a live use-after-free" but "convert two unwritten proofs into one enforced
mechanism, while the cost is zero."** The precondition that makes this free is **F1**: the WIMM is *always*
signalled, so gating release on the CQE can never hang. (That is exactly the substrate rule P0-i just made
explicit — §21.)

### 22.5 The design

Release the op slot on **whichever of {response consumed, send CQE retired} happens LAST.**

1. **New op phase** `CITUS_REMOTE_EXEC_PEER_CONTROL_OP_RETIRING`: *the caller has its response and is gone; the
   slot is now owned solely by the outstanding send CQE.*
2. **New field** `bool sendCompletionRetired` in `TupleSinkServicePeerControlOpState`.
3. **`TupleSinkServiceRetireControlSendCompletion`'s request arm becomes the REAL retirement site** (F5's stub):
   on generation match, set `sendCompletionRetired = true`; if the phase is already `RETIRING`, **release the
   slot now**. Delete the decoy comment.
4. **`TupleSinkServicePollPeerRequestRdma` at `COMPLETED` (`:10987`)**: copy the response out and finish the
   caller **exactly as today — zero added latency**. Then release the slot **only if** `sendCompletionRetired`;
   otherwise move the op to `RETIRING` and leave it. **The caller is never blocked on a CQE; only slot REUSE is.**
5. **`:10644` (post failure)**: keep the immediate release, but **assert `postedWrCount == 0` first**. If a future
   change makes PARTIAL reachable, do **not** release — mark the op `FAILED` and let the reset retire it. This
   turns Proof A into an **enforced check** instead of an accident of chain length.
6. **The generation-mismatch arm becomes a LOUD ALARM, not a benign no-op.** Under this design a slot is *not*
   recycled until its CQE lands, so a control CQE **cannot** find a stale generation. F7 says the check is
   trustworthy. If it ever fires, something posted a WR whose op was released early — the exact class of bug
   this stage exists to prevent.
7. **The reset abort classifier (`:4292`)** gains a `RETIRING` case. An op in `RETIRING` is **not a lost
   operation** — its caller already got its answer — so it must be counted separately (`retiringControlOpsDiscarded`)
   and **must not** inflate `waitingControlOpsAborted`.
8. **`TupleSinkServicePeerControlAsyncOpReady` (`:10904`)** gains the `RETIRING` case. A live async handle can
   never observe it (the handle is memset at COMPLETED), so it is a `-Wswitch` completeness case, not a path.
9. **The response-publish slot already follows the protocol.** Do not change it — just **assert** that only its
   own CQE clears `inUse`.

### 22.6 The one real tradeoff, and why it is acceptable

Deferring slot release to the CQE means an op can sit in `RETIRING` after its caller is done. With 64 slots, a
burst of control ops could in principle exhaust the table before the send CQ is drained.

**Why it converges:** reservation failure returns `HOMER_PEER_POST_WOULD_BLOCK` (`:10624`), the async op is *not*
failed, and `TupleSinkServicePollPeerRequestRdma` retries publication on the next poll (`:10942-10952`) — after
the pump has drained the send CQ. So backpressure resolves itself. **Reservation must NOT drain internally**
(same rule as P0-c): the owner drains, the reserver reports.

**Why `RETIRING` should be RARE:** the send CQE is *generated* when the remote HCA ACKs our WIMM — which happens
**before** the remote CPU has even seen the message, let alone composed a response. So by the time a response is
matched, our CQE is essentially always already sitting in the CQ. Whether it has been *polled* depends only on
the pump's drain order. `RETIRING` is thus a real but short-lived state — **exercised, not dead**, which is what
we want.

### 22.7 Acceptance

- 3 consecutive `pgbench --homer --homer-dpu-command` runs, no restarts, tps in the **95–128** band.
- **ALARM = 0** on both nodes (a control-CQE generation mismatch would print one).
- Clean SIGTERM shutdown on both nodes.
- ⚠ **NOT this stage's criterion:** the `refusing to reset peer CLIENT_SQL_SESSION before command-mailbox writers
  quiesce` line at `tuple_sink_service_process.c:24152` belongs to **P1-f** (a different resource, Scenario C).
  It will still print once. Do not read it as a P0-b failure. (Same trap that nearly produced a false FAIL in P0-a.)

### 22.8 ADVERSARIAL REVIEW OUTCOME (2026-07-12) — the spec was NOT clean

Round 1 of the review (codex, read-only, given falsifiable targets F1–F5 / B1–B4). **Both sides recorded**, per
the audit's own rule: the reasoning that produced the error is as instructive as the fix.

**Held up:** F1 (one WR, always signalled — a CQE is guaranteed; the load-bearing premise of the design),
F5 (Proof B — the reviewer traced the peer's *actual* consumption path and confirmed the response identity is
**echoed from the received 34 KB record**, `:8094` → `:7915`, after an acquire fence and a full-record copy
`:7430-7448`; there is no immediate-only, NAK, partial-record, or locally-synthesized response path — so a
matched response really does prove the WIMM landed), B2 (reset destroys the QP **and both CQs** at `:5515-5530`
*before* the abort classifier memsets any slot — the classifier's comment is honest), B3 (a generation-mismatch
ALARM cannot be triggered by an ordinary QP-error flush: failed CQEs take the failure route before tagged
retirement, and reset destroys the CQ before recycling slots), and B4-aliasing (`controlOpsMr` is registered
`IBV_ACCESS_LOCAL_WRITE` **only**, `:4624` — no part of `controlOps[]` is remote-writable; `requestPublishedTail`
is local bookkeeping, never an SGE).

#### 22.8.1 ⚠ THE REAL FIND: an abandoned async op ORPHANS its transport slot (VERIFIED myself)

**F4 is REFUTED.** §22.2's F6 claimed *"no timeout/abandon path releases an op."* True — and that is exactly the
bug, read the other way round. **A path abandons an op without releasing it.**

- `TupleSinkServiceLocalControlAsyncOp` **embeds** the transport handle:
  `TupleSinkServicePeerControlAsyncOp peerAsyncOp;` — `tuple_sink_service_process.c:37166`.
- `TupleSinkServicePumpLocalControlAsyncOpAt` checks, **at the top of every pump**, whether its SHM control slot
  was recycled under it (`slot->requestSequence != asyncOp->requestSequence`, or the slot is no longer
  `SERVICE_OWNED`) — `tuple_sink_service_process.c:39079`. On a hit it logs *"dropping stale async local-control
  op"* and calls `TupleSinkServiceResetLocalControlAsyncOp` (`:39087`), which **memsets the whole local op —
  including `peerAsyncOp`** (`:37564`).
- The check runs on **every** pump, including pumps where the peer request is **already published and in
  flight**. So the local handle is destroyed while `controlOps[opIndex]` sits in `WAIT_RESPONSE`.
- **Nothing ever releases that slot.** The only two releases (`:10644`, `:10990`) both require a live handle to
  poll. When the peer's response eventually arrives, `TupleSinkServiceCompletePeerControlOp` still matches it
  from the **op state** (which is intact) and flips the phase to `COMPLETED` — where it stays **forever**.

**Consequence: a permanent, silent wedge.** 64 stale drops on one connection exhaust the op table; every
subsequent control op then gets `HOMER_PEER_POST_WOULD_BLOCK` — **no error, no log, no recovery.**

**It is NOT a use-after-free** (the slot is never memset while a WR reads it), so Proof B survives for *memory
safety*. It is a **resource leak of the exact class this audit exists to kill: an owner vanishes without
retiring its resource.** Reachable whenever a client abandons and re-issues on a control slot (client timeout,
Ctrl-C, crash) — rare in a clean run, which is why it has never been seen.

**This is pre-existing and independent of P0-b, but it is P0-b's own resource and P0-b's own contract. In scope.**

#### 22.8.2 B1 — §22.6's liveness claim was TOO STRONG (corrected, not fatal)

§22.6 said *"the pump has drained the send CQ."* **It is not drained unconditionally.** The `PEER_SEND_CQ`
collector is scheduled by the machine-baseline policy via
`HomerServiceMachineBaselinePeerCollectorsDue` → `TupleSinkServiceHasActivePeerWork` → 
`HomerMachineBaselinePolicyPeerControlDue(activePeerWork)` (`tuple_sink_service_process.c:45160-45174`), and
`TupleSinkServiceHasActivePeerWork` (`:29456`) knows only **two** facts: an active session with
`peerCommandEndpointValid`, and an active payload stream with a peer binding. **Outstanding control ops are not
a fact it knows.**

**Verified consequence:** with `activePeerWork == false` the send-CQ collector falls back to the *idle* cooldown
— it is a **blind liveness poll that still runs**, just rarely (the code says so itself: *"policy-owned peer
active/idle cooldown only for blind recv/mailbox/send-CQ liveness"*, `:45181-45184`). **So this is a LATENCY
concern, not a deadlock — the new design cannot hang the service.**

But it is the wrong shape to depend on. The worst case is real: a `CLOSE_SESSION` op whose response is consumed
*after* the session went inactive would sit in `RETIRING` on the idle cadence, holding its slot. **Fix: make
outstanding control ops an authoritative fact** — exactly the direction the file already states for itself
(*"until their authoritative facts are maintained directly"*).

#### 22.8.3 Smaller corrections accepted

- **F2 is not code-enforced.** The QP requests only `CITUS_REMOTE_EXEC_PEER_INLINE_WRITE_BYTES = 64` inline bytes
  (`:106`, `:4534`), but the provider-returned `max_inline_data` is stored with no upper bound (`:4555`), and
  inline is chosen purely by `length <= maxInlineData` (`:9641`). 34,160 B will never inline in practice, but
  **nothing in the source says so.** → Add a `_Static_assert` tripwire. **Note: the new design is correct either
  way** — if the message ever *did* go inline, CQE-gating would merely be redundant, never wrong.
- **F3 has a defensive hole.** `postedWrCount` is 0 on a 1-WR chain only because verbs sets `bad_wr == first_wr`
  (`/usr/include/infiniband/verbs.h:3389`). A **nonconforming provider returning `bad_wr == NULL`** would make
  `TupleSinkServiceCountPostedWorkRequests` count the sole WR as *posted* (`:9552`) and classify it PARTIAL
  (`:9587`). → The design already handles this correctly: **on PARTIAL, do not release.**

### 22.9 THE CORRECTED DESIGN (supersedes §22.5/§22.6)

The release predicate becomes purely local to the transport, and **both conditions must hold**:

1. **`sendCompletionRetired`** — the send CQE landed. *The RDMA substrate is done with the source buffer.*
2. **the owner is done** — either it **consumed** the response (`TupleSinkServicePollPeerRequestRdma` at
   `COMPLETED`) or it **abandoned** the op (new; see below).

An abandoned op **still occupies a mailbox slot at the peer and will still receive a response**, so it must stay
reserved until that response arrives and is discarded. Releasing it early would let a late response hit a
recycled slot, where `TupleSinkServiceCompletePeerControlOp` rejects it (`:7864`) → **connection reset**. So an
abandoned op is a **zombie that self-reaps on {response arrives} ∧ {CQE landed}** — it needs no poller.

| part | change |
|---|---|
| **1. CQE-gated release** | `bool sendCompletionRetired` + phase `..._OP_RETIRING` (*"owner done, awaiting CQE"*). `TupleSinkServiceRetireControlSendCompletion`'s request-arm stub (`:3707`) becomes the **real retirement site**: set the flag; if phase is `RETIRING` **or** (`ownerAbandoned` ∧ response already in), release now. Delete the decoy comment. `PollPeerRequestRdma` at `COMPLETED` (`:10987`): hand the response to the caller **exactly as today — zero added latency** — then release only if `sendCompletionRetired`, else → `RETIRING`. |
| **2. ALARM** | Generation mismatch in the request arm becomes a **LOUD ALARM**. Under this design a slot is not recycled until its CQE lands, so a control CQE **cannot** find a stale generation (F7: the WR-ID carries the full 32-bit generation, no truncation; B3: reset flushes cannot reach here). If it fires, something posted a WR whose op was released early — the exact bug this stage exists to prevent. |
| **3. Abandon API** (§22.8.1) | New `TupleSinkServiceAbandonPeerControlOpRdma(asyncOp)` in the transport. Sets `ownerAbandoned = true`. If the response already arrived (`COMPLETED`) it is equivalent to "owner done" → release now or → `RETIRING`. Called from the stale drop (`tuple_sink_service_process.c:39087`) **before** the local memset, and only when `peerAsyncOp.requestPublished`. |
| **4. Active-work fact** (§22.8.2) | Per-connection `outstandingControlOps` counter (++ at reserve, -- at release). Expose `TupleSinkServicePeerTransportHasOutstandingControlOpsRdma()`; make `TupleSinkServiceHasActivePeerWork` (`:29456`) consult it. **A counter, not a 64-slot scan** — this predicate runs on the service loop. This also reaps a *pre-existing* hazard: today's unpolled control CQEs accumulate in the send CQ whenever the service goes idle. |
| **5. Failure release** | At `:10644`, **assert `postResult->postedWrCount == 0`** before releasing. If PARTIAL is ever reachable (a 2-WR control publish, or a nonconforming `bad_wr == NULL`), **do NOT release** — mark the op `FAILED` and let the CQE or the reset retire it. Turns Proof A from an accident of chain length into an **enforced check**. |
| **6. Classifier** | The reset classifier (`:4292`) gains a `RETIRING` case and counts `retiringControlOpsDiscarded` **separately** — an op in `RETIRING` is **not a lost operation** (its caller already got its answer) and must not inflate `waitingControlOpsAborted`. An abandoned op *is* discarded work and is counted as such. |
| **7. Tripwires** | `_Static_assert` that `sizeof(TupleSinkServicePeerControlMessage) > CITUS_REMOTE_EXEC_PEER_INLINE_WRITE_BYTES` (§22.8.3). Response-publish slots keep their existing (correct) protocol — just **assert** that only their own CQE clears `inUse`. |

**Decision recorded (per the standing rule):** parts 3 and 4 were **not** in the original P0-b scope. They are
included because they are *the same resource* (the 64-slot control op table) and *the same contract* (retire
before release), and because part 1 **depends** on part 4 being right. Splitting them would land a design whose
liveness argument rests on a blind cooldown.

### 22.10 P0-b — LANDED + VALIDATED (2026-07-12)

**Correctness: PASS.** 3 consecutive gate runs (`pgbench --homer --homer-dpu-command`, `-t 2000 -c 1`), no
restarts. `transport: homer-dpu-command (implies dpu result relay)` on all three; `2000/2000` processed, **0
failed**; farnet1 DPU shows `DPU backend spawn begin` → `COMPLETED … launched_pid=N` once per run; the farnet0
host service log holds its **banner and nothing else** (no silent host-relay fallback); farnet1 host service
DOWN. **ALARM = 0** on all four logs across three runs **and** a clean SIGTERM shutdown (T1→T2→T3→T4 teardown
intact, P0-d's counters still 3/3/3/3).

**The new observability fired:** `peer reset-complete discarded control state node=0 reason=4 waiting_lost=0
completed=0 retiring=0 failed=0 response_slots=1`, once per teardown. Those counters had been computed by the
abort classifier since it was written and **printed by nobody**.

#### 22.10.1 ⚠ `retiring=0` — THE NEW BRANCH WAS NEVER EXERCISED

`retiring=0` in every run. §22.6 *predicted* `RETIRING` would be rare (the send CQE is generated when the remote
HCA ACKs, which precedes the peer's CPU even seeing the message, let alone composing a response — so by the time
a response matches, our CQE is essentially always already in the CQ). **Prediction confirmed. But "rare" is not
"tested."** This smoke is single-client with one command in flight; it does not drive the `RETIRING` path at all.

**OPEN:** the RETIRING branch is validated only by construction, not by execution. Driving it needs either a
multi-client run or an artificially delayed send-CQ drain. **Do not claim it is exercised.**

#### 22.10.2 ⚠ THE tps BAND IN THIS DOC IS NOT A BAND — two methodology errors, both mine

The acceptance criterion said *"tps in the 95–128 band"* (P0-f 95.2/125.3/104.1, P0-d 114.6/125.3/121.4,
P0-a 107.2/128.3/122.3, P0-i 109.9/124.4/122.4). This run returned **283–287 tps** — and that is **not a
speedup**, it is a **different measurement**:

1. **The 95–128 band is from `-t 5` runs** (the gate example in CLAUDE.md uses `-t 5`), where the ~40 ms
   per-connection setup dominates five transactions. This run used `-t 2000`, where setup amortizes to nothing.
   **Per-transaction latency, the quantity that is actually comparable, is ~3.5 ms in BOTH.** The tps figures
   were never measuring the same thing.
2. **This run carried `--debug`** — required for gate proof #2 (decoded `abalance` per transaction) — which
   prints ~12,000 lines onto the measured path. CLAUDE.md's like-for-like `-t 2000` steady state (**3.14 ms/tx,
   318 tps**) was taken *"on a fully stripped stack"*, and CLAUDE.md explicitly warns that logging on the
   measured path has produced false regressions in this prototype before.

**Consequence: this stage has NO usable performance number, in either direction.** Do not read 283 tps as a
gain, and do not read 3.50-vs-3.14 ms as a regression. P0-b touches **only the control path** (session
open/close ops — a handful per run); it has **no mechanism** by which it could move per-transaction cost.

**Rejected explanation (recorded so it is not re-proposed):** the validating agent attributed the jump to the P2
stall fix. **Refuted** — P2 (`bf6cf0ea42d`) was already committed *before* P0-f/P0-d/P0-a/P0-i were validated,
so it cannot explain a change that appears only now.

**ACTION for the PERF item:** the reference numbers in this doc must be re-stamped with their `-t` **and** their
logging state, or deleted. A tps figure without both is not a measurement. (This is the same failure the project
already knows about — hazards §8.2, *"stamp every baseline with a commit SHA"* — one level deeper: **stamp it
with its workload shape too.**)

#### 22.10.3 Review corrections applied to the worker's implementation

Reviewed every edit personally. Three defects found and fixed by hand before validation:

- **A new `abort()` path dressed as a safety check.** The worker introduced `#include <assert.h>` — **new to the
  entire homer tree** — and four `assert()` calls. `NDEBUG` is defined nowhere, so those asserts were **live**,
  and a live `assert` calls `abort()`: an invariant violation would have **killed the service**, with a message
  naming neither the peer nor the slot. Two of the four were also vacuous (one restated its own enclosing branch
  predicate). **Replaced with the project's ALARM-and-continue style.**
- **A spurious ALARM on a legitimate path.** `AbandonPeerControlOpRdma` ALARMed when the connection was inactive
  or the slot generation had moved on — but *that is exactly what a prior connection reset leaves behind*: the
  abort classifier has already reaped the slot, so there is nothing wrong. With **ALARM = 0** as the acceptance
  criterion, an ALARM on a benign path both fails validation and devalues every other ALARM. **Downgraded to an
  informational log with full context;** only a NULL handle or an out-of-range index remains an ALARM.
- **Write-only counters.** `retiringControlOpsDiscarded` would have joined three *existing* abort-report counters
  that **nothing ever printed**. Added the reset-complete report (§22.10) — otherwise the classifier change would
  have been unobservable during its own validation.

**Commits:** citus `81593cdf9`, kb `02d220a26d0`.

---

## §23 — P0-c: the RDMA payload staging pools have NO accounting

**Status: SPEC (2026-07-12).** Unlike P0-b, this one **is** a live bug — but the audit's one-line diagnosis
(*"no inUse, no generation, no frontier"*) is right about the symptom and silent about the mechanism. The
mechanism is what makes it exploitable, and it is not where you would look.

### 23.1 The two pools, and why only ONE of them is broken

Both are per-**connection** (i.e. per-**lane**) arrays inside `TupleSinkServicePeerConnectionState`, both are
registered RDMA source memory, and both are handed out by a **blind modulo cursor** with no accounting at all:

- `TupleSinkServiceReservePayloadPublishTailSlot` (`remote_execution_peer_transport_rdma.c:760`) →
  `payloadPublishTailBuffers[128]` (`:704`) — one 64-bit staging word per **batch** (the tail value).
- `TupleSinkServiceReservePayloadFragmentHeaderSlot` (`:784`) → `payloadFragmentHeaderBuffers[1024]` (`:715`) —
  one generated transport header per **payload write**.

Both carry a comment asserting they are safe. **Do the arithmetic** (all VERIFIED):

| constant | value | where |
|---|---|---|
| `CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH` | **256** WRs | `..._rdma.c:101` |
| `CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES` | **32** | `..._rdma.h:54` |
| `payloadWriteCount == 0` | **REJECTED** → a batch is **≥ 2 WRs** (≥1 write + 1 tail) | `..._rdma.c:10028` |
| `HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS` | **63** batches — **PER STREAM** | `tuple_sink_service_process.c:326` |
| `HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS` | **192** WRs — **PER STREAM** | `tuple_sink_service_process.c:338` |

**HEADER pool — SAFE, and P0-c must not churn it.** Live headers ≤ outstanding payload WRs ≤ **256** (the send
queue is the hard cap; `ibv_post_send` returns `ENOMEM` → `WOULD_BLOCK` when the SQ is full). Pool is **1024**.
**4× headroom; the cursor cannot wrap onto a live header.** Its comment claims it is sized `32 batches × 32
writes`; the *real* bound is 4× smaller. Safe — but, once again, **by an argument nobody wrote down.**

**TAIL pool — BROKEN.** One slot per batch. Live batches are bounded by the *per-stream* caps (63 objects,
192 WRs) — but the **pool is per-LANE, shared by every stream bound to it.** Nobody computes the lane-wide sum:

- 1 stream: ≤ 63 live batches. Pool 128. Safe.
- 2 streams: ≤ 126. Safe **by two slots**.
- **3+ streams: the per-stream caps permit 189 live batches against a 128-slot pool.**

The send queue claws that back — 256 WRs ÷ 2 WRs/batch = **128 batches** — which is **exactly the pool size**.
**Zero margin.** The cursor collides on the very next reservation.

### 23.2 THE MECHANISM — the backpressure path is itself the corruption trigger

This is the part the audit's one-liner misses, and it is what turns "zero margin" into a live defect:

```c
tailSlotIndex = TupleSinkServiceReservePayloadPublishTailSlot(connectionState);   /* :10138 -- blind */
tailSource    = &connectionState->payloadPublishTailBuffers[tailSlotIndex];
*tailSource   = tailValue;                                                        /* :10141 -- UNCONDITIONAL */
...
ibv_post_send(...)                                                                /* may fail WOULD_BLOCK */
```

**The staging word is overwritten BEFORE the post is attempted, and the cursor advances even when the post
fails.** So at 128 live batches — precisely the moment the send queue is full — the next publish attempt:

1. reserves slot 0, which is **still live**;
2. **scribbles a much-later tail value into the RDMA source word of an in-flight WR**;
3. *then* posts, and fails `WOULD_BLOCK`.

The already-posted WR for batch 0 then writes **the wrong tail** to the peer. The peer reads that tail as
*"every byte below this is committed"* — for bytes that were never written. **Silent remote data corruption,
triggered by the backpressure path, i.e. exactly under load.** No error, no ALARM, no crash.

### 23.3 THE FIX — a frontier, not an `inUse` bit

Retirement here is **FIFO**, and that is a gift:

- posts are single-threaded (the service loop);
- the RC QP completes **in order**;
- every batch's tail WR is **`IBV_SEND_SIGNALED`** (`:10156`) — a CQE is **guaranteed** (P0-i's substrate rule);
- the batch's unsignalled payload WRs are retired by that same tail CQE (the lane FIFO).

So reservation order == retirement order, and the pools need **no per-slot `inUse` bit and no scan** — they need
**two counters**:

- `payloadPublishTailReserved` (monotonic) and `payloadPublishTailRetired` (monotonic).
- **Reserve:** if `reserved - retired == CAPACITY` → **return `WOULD_BLOCK` BACKPRESSURE. Do NOT drain
  internally** (the owner drains; the reserver reports — same rule as P0-b's op table). Otherwise hand out
  `reserved++ % CAPACITY`, which is now **provably free**, so writing `*tailSource` is safe.
- **Retire:** on the batch's tail CQE, `retired++` (and `retired += headerCount` for the header ring).
- **Roll back on a failed post:** a zero-WR failure must **un-reserve** (`reserved--`), or the frontier never
  advances for a slot that has no WR and the pool bleeds capacity. Safe because nothing was posted and the loop
  is single-threaded.
- **PARTIAL POST:** the tail WR may not have been posted → **its CQE will never come** → the frontier can never
  advance → the lane would wedge. Partial post already forces a connection reset; **the reset must reset both
  frontiers.** (Same shape as P0-b's classifier zeroing `outstandingControlOps`.)

**Apply the frontier to the HEADER ring too**, even though it is safe today at 4× headroom — its safety is an
unwritten argument about the send-queue depth, and P7b pooling plus any change to `SEND_QUEUE_DEPTH` or
`PAYLOAD_BATCH_MAX_WRITES` invalidates it silently. Cost is two more counters on a path that already exists.

### 23.4 Falsifiable claims — REFUTE THESE BEFORE BUILDING

- **F1.** The header pool is safe today (live ≤ 256 vs capacity 1024). *If false, it is a second live bug.*
- **F2.** The tail pool's real bound is per-LANE while its flow control is per-STREAM, so ≥3 concurrent payload
  streams on one lane can drive live batches to the 128-slot boundary. *If false, P0-c is latent, not live.*
- **F3.** `*tailSource = tailValue` executes before `ibv_post_send`, so a **failed** publish still corrupts a
  live slot. *This is the load-bearing mechanism. If false, the bug needs 129 SUCCESSFUL posts and is much
  harder to reach.*
- **F4.** Reservation order == retirement order (single-threaded post + RC in-order + always-signalled tail), so
  a two-counter frontier is sufficient and no per-slot `inUse` is needed. *If false, the whole fix is wrong.*
- **F5.** Consequence is a bogus tail published to the peer → the peer advances its consumed frontier past
  unwritten bytes → **silent data corruption**, not a crash.

### 23.5 ADVERSARIAL REVIEW — §23's DESIGN IS DEAD. Both sides recorded.

Round 1 (codex, read-only, falsifiable targets). **It broke the design.** Every load-bearing claim below was
**re-verified by me in the code** before being accepted (the standing rule: a wrong correction costs exactly as
much as a wrong original).

| my claim | verdict | why |
|---|---|---|
| **F1** header pool is SAFE (4× headroom) | **REFUTED** | I assumed the cursor only advances on **successful** posts. It doesn't. A header is reserved and `*headerSource` **written** at `:9865-9868`, and the very next block can still `return false` (`:9879`), as can the post itself. **Failed attempts lap the 1024-slot cursor independently of send-queue occupancy.** The header pool has the SAME defect as the tail pool. |
| **F2** flow control is per-STREAM, pool is per-LANE | **materially refuted** | Directionally right, but I missed a **second consumer**: the **ACK path also reserves tail slots** (`:10338`), with **64 per-stream ACK owners** (`tuple_sink_service_process.c:393`). My arithmetic was incomplete. |
| **F3** reserve+overwrite happens BEFORE the post, and a FAILED post still corrupts | **VERIFIED** | And it is **worse than I found** — the same shape at `:10138`, `:10171`, `:10338`, `:10356`. No path rolls back. |
| **F4** a two-counter FRONTIER suffices (FIFO retirement) | **REFUTED — THIS KILLS THE DESIGN** | The fixed-slot path (`:9912`) and the byte-ring path (`:10045`) encode the tail WR-ID with the **SAME** kind (`TUPLE_SINK_SERVICE_PEER_PAYLOAD_COMPLETION_DATA`), and the decoder (`:3451`) yields only `(kind, streamIndex, streamGeneration, completionToken)`. **The CQE carries NO discriminator for how many staging slots the batch consumed** — and only the byte-ring path reserves a tail slot at all. A frontier cannot be advanced from a completion that does not say what it is freeing. |
| **F5** consequence is SILENT data corruption | **REFUTED** | The receiver **validates** generation, ordinal, framing, sizes and object semantics (`tuple_sink_service_process.c:33234`, `:34918`, `:34979`). A bogus tail produces a **stall, a reset, or wrong credit** — loud-ish, not proven silent object corruption. **Severity drops.** |
| **F6** reset must zero the frontiers | **already handled** | Reset memsets the **entire** connection state after QP/CQ/MR teardown (`:5566`). Any new counters are covered for free. |

**Lesson (mine):** I computed a capacity bound from the **send-queue depth** and never asked *"what advances
the cursor?"* — the answer is **every attempt, including the ones that never post a WR.** A capacity argument
that only counts successes is not a capacity argument. This is the same failure mode as the previous refutations:
**I reasoned about the mechanism I had just read and not about its callers.**

### 23.6 ⚠ NEW, SEPARATE BUG FOUND BY THE REVIEW — "CQE THEFT" in the blocking wait

**Not P0-c. Independent, and it must be decided on its own.**

`TupleSinkServiceWaitForSendCompletion` (`:3922`) passes **NULL callbacks** to
`TupleSinkServiceHandleTaggedSendCompletion` (`:3962`).

- **Tagged CONTROL CQEs** are retired **internally** — safe (this is what makes P0-a/P0-b sound; I verified it).
- **Tagged PAYLOAD / COMMAND / peer-client CQEs require a service CALLBACK.** With NULL, the handler logs
  *"send-CQ drain received payload completion without payload owner callback"* and **returns false** (`:3796`,
  `:3810`, `:3832`, `:3846`, `:3890`, `:3905`) — **but `ibv_poll_cq` has ALREADY consumed the CQE** (`:3938`).

**The CQE is destroyed. Its owner never receives its retirement event.** The wait then fails → connection reset,
so it is loud rather than silent — but a resource that was waiting on that CQE is retired by **cancellation**
(Scenario E), not by completion.

**Reachable:** tuple/COPY command completion stores the **payload** connection in
`peerCommandCompletionConnectionHandle` (`tuple_sink_service_process.c:27898`, `:38667`), so the blocking epoch
write at `:19461` runs on a QP that **also carries payload batches**. (The SQL-command path uses the
`CRITICAL_CONTROL` lane (`:37991`), whose CQEs *are* retired internally — which is why the DPU gate never sees
this.) **Backend-to-backend COPY is 🔴 already broken at HEAD; this is a candidate contributor.**

Also latent (recorded, no current caller): the singleton `inlineWriteBuffer` has an exported **unsignalled**
posting API (`:9171`), is overwritten at `:9205`, and can be **deregistered and freed during resize** (`:4517`).
That is the P1 `EnsureInlineWriteBuffer` landmine, now with a mechanism.

### 23.7 P0-c REDESIGN — three options, none free

The real constraints, now that F1/F2/F4 are corrected:

1. **Every reservation must be rolled back or accounted, including the ones whose post never happens** (F3, and
   F1's refutation). *This alone fixes a large part of the bug and is cheap.*
2. **A completion CQE does not say what it frees** (F4). Retirement must therefore be attributed some other way.
3. **The tail pool has two producers** (batch tails and ACKs) with different WR shapes (F2).

| option | mechanism | cost |
|---|---|---|
| **A — per-connection FIFO of reservation records** | push `{tailSlots, headerSlots}` at **successful** post; pop one per DATA CQE; advance both frontiers. Retirement is FIFO, so a ring works. | Must push a record for **every** batch that yields a DATA CQE (even zero-slot fixed-slot batches) or the FIFO desynchronizes. **Requires enumerating EVERY producer of a DATA CQE** — including the ACK path. A desync is silent and catastrophic. |
| **B — make the CQE self-describing** | encode the slot counts / a reservation id into the payload WR-ID. | Needs spare WR-ID bits (`kind`, `streamIndex`, `streamGeneration`, `completionToken` are already allocated — **budget unverified**). But it is the P0-e lesson applied: *a self-describing record removes the need for a side table.* |
| **C — block-allocate slots per batch** | reserve a whole fixed block per batch: slot = `(batchIndex % capacity) * MAX_WRITES + writeIndex`. Ownership is **derived** from a single monotonic batch counter; retirement frees the block implicitly. | No side table, no WR-ID change, trivially correct. **But it caps outstanding batches at `1024/32 = 32`** (vs ~128 today) unless the header pool grows. **A throughput ceiling — needs a perf decision.** |

**Recommendation: C, with an enlarged header pool** — it is the only one whose correctness does not depend on
enumerating a set of call sites exhaustively (the exact thing that has burned this audit repeatedly). But it
trades memory for concurrency and that is a **decision for the owner, not an improvisation.**

**STOPPED HERE FOR DISCUSSION.**

---

## §24 — THE WR-ID CONTRACT (canonical). Prerequisite for P0-c.

**Status: SPEC (2026-07-12).** The verbs `wr_id` is the ONLY channel by which a completion tells software what
it completed. Everything in §0's retirement contract ultimately routes through it. It has never been written
down, and it currently encodes couplings that **silently cap tables elsewhere in the tree.**

**A `wr_id` is 64 bits, purely LOCAL — it is never transmitted.** Changing the layout needs **no protocol or ABI
bump**, and no DPU resync. That is what makes §24 cheap.

### 24.1 ⚠ The tag space is NOT disjoint — it is a PRIORITY-DECODED union

This is the most dangerous undocumented fact in the file:

| tag | bit | …but that bit is ALSO |
|---|---|---|
| `PAYLOAD_WR_ID_TAG` | **63** | — |
| `COMMAND_WR_ID_TAG` | **62** | **the payload's `KIND` bit** (`KIND_SHIFT = 62`) |
| `CONTROL_WR_ID_TAG` | **61** | **inside the payload's `STREAM_INDEX` field** (bits 56–61) |
| `CLIENT_COMPLETION_WR_ID_TAG` | **60** | **inside the payload's `STREAM_INDEX` field**, *and* **`CONTROL_WR_ID_RESPONSE_FLAG`** |

It is correct today **only** because the `IsTagged` predicates form an **exclusion chain**:
payload = `bit63`; command = `bit62 && !payload`; control = `bit61 && !payload && !command`;
client-completion = `bit60 && !payload && !command && !control` (`:3244-3256`).

**The decode order is load-bearing and exists nowhere but in the order of four `&&` clauses.** Reorder them,
or add a fifth class, and CQEs silently route to the wrong owner — which, per §0, means a resource is retired by
the wrong actor, or not at all.

**RULE OF RECORD:** the WR-ID is a **priority-decoded tagged union**. A class is chosen FIRST, by the first tag
bit set from 63 downward; only THEN are that class's fields decoded. A class's fields MAY reuse lower tag bits,
**because the class has already been decided.** This must be stated in the code, not just here.

### 24.1.1 The exclusion chain IS correct today (verified) — and it hides a SECOND assumption

**Verified by construction:** no class can be stolen by another, because each class's own FIELDS never set a
HIGHER class's tag bit. Payload (tag 63) occupies bits 0-62 — its `KIND` (62) and `STREAM_INDEX` (56-61) do set
bits 60-62, but payload has already won at bit 63. Command (tag 62) carries only a 16-bit index. Control
(tag 61) carries index bits 0-15 and generation bits 16-47 — it never reaches bit 62. Client-completion (tag 60)
carries only a 16-bit index. **So there is NO live mis-routing bug.** The chain is fragile and undocumented, not
broken.

⚠ **But the UNTAGGED class is defined by ABSENCE — "no tag bit set" — and untagged WR-IDs are raw POINTER
VALUES** (the source-buffer address; see the diag decoder at `:3529`). So CQE routing silently depends on:

> **every registered buffer address has bits 60-63 clear.**

True on Linux x86-64 (canonical user VAs are below 2^47; even 5-level paging stays under bit 57) — but it is an
**assumption about the address space**, it is load-bearing for correct CQE dispatch, and it is written nowhere.
If a pointer ever set bit 60, its completion would be decoded as a *tagged* CQE and delivered to a stranger's
retirement site.

**Fix: one `HOMER_PEER_RDMA_DIAG` check at post time** — assert an untagged WR-ID has no tag bits. Cheap, and it
makes the assumption speak for itself instead of being inferred from the platform.

### 24.1.2 MAKE IT A REAL DISCRIMINATED UNION — but of DECODED FIELDS, not of bits

**Owner's call (2026-07-12), and it is right for a better reason than tidiness.**

**The priority decode currently has NO single home.** It is re-implemented as a hand-ordered `if`-ladder at
THREE dispatch sites (`:3485-3507`, `:3779-3852`, `:4078`) plus two one-off checks (`:9363`, `:9449`).
**Not one of them is a `switch`.** So a fifth WR-ID class needs every ladder edited by hand, in the correct
order, and **nothing tells you if you miss one** — this is CLAUDE.md hazards §5 (*"a new collector needs FOUR
edits; -Wswitch protects nothing"*) sitting in the CQE dispatch path, where a mis-decode means a CQE is
delivered to the wrong owner's retirement site.

⚠ **NOT a union of BITFIELDS.** C bitfield layout is **implementation-defined** — the standard fixes neither
allocation order within a storage unit nor bit numbering. It would *work* on GCC/Linux, but **`_Static_assert`
can check `sizeof` and never a field's bit POSITION.** Expressing the layout as bitfields would therefore
**destroy §24.4**, the compile-time enforcement that is the entire point of this section. Masks and shifts are
integer constant expressions; disjointness, capacity and tag-distinctness stay statically assertable. **The bit
layout MUST remain mask/shift-defined.**

The union belongs one level up — over the **decoded** fields:

```c
typedef enum HomerPeerWrClass          /* the discriminant, explicit at last */
{
    HOMER_PEER_WR_CLASS_UNTAGGED = 0,  /* a raw source-buffer address */
    HOMER_PEER_WR_CLASS_PAYLOAD,
    HOMER_PEER_WR_CLASS_COMMAND,
    HOMER_PEER_WR_CLASS_CONTROL,
    HOMER_PEER_WR_CLASS_CLIENT_COMPLETION,
} HomerPeerWrClass;

typedef struct HomerPeerWrId
{
    HomerPeerWrClass  class;
    union {
        HomerPeerPayloadWrIdFields          payload;
        HomerPeerCommandWrIdFields          command;
        HomerPeerControlWrIdFields          control;
        HomerPeerClientCompletionWrIdFields clientCompletion;
        uintptr_t                           untaggedSourceAddress;
    } u;
} HomerPeerWrId;

bool     HomerPeerDecodeWrId(uint64_t raw, HomerPeerWrId *out);  /* THE ONLY place bits 60-63 are examined */
uint64_t HomerPeerEncodePayloadWrId(const HomerPeerPayloadWrIdFields *fields);
```

Every dispatch site then becomes `switch (id.class)` **with NO `default:` arm**, so `-Wswitch` finally guards a
new class at every site at once.

**What this buys:**
1. The **decode order lives in ONE function**, not in the order of `if`s at three sites.
2. **`-Wswitch` gets teeth.** (Only if there are NO `default:` arms — hazards §5.)
3. **`UNTAGGED` becomes a NAMED state** rather than "fell off the end of the ladder". It is a raw pointer, so
   §24.1.1's address-space assumption now has exactly ONE place to be checked.
4. The bit layout stays statically assertable (§24.4 survives).
5. A `HOMER_PEER_RDMA_DIAG` encode->decode round-trip proves the **shifts**; the static asserts prove the
   **widths**. Between them the layout is pinned.

⚠ **MEASURE, do not assume:** `HomerPeerDecodeWrId` returns a ~16-byte struct on the **send-CQ drain hot path**
(per CQE). It is stack-local and inlinable, so it should stay in registers — but "obviously free" has been wrong
in this subsystem before. **Confirm on the gate, do not assert it.**

### 24.1.3 The decoded object, CONCRETELY — 16 bytes, and asserted

The reviewer's G9 objection was that the decoded object *might* be 24 bytes, and an x86-64 aggregate over 16
bytes stops fitting the two return registers. **Do not argue about it -- PIN it.** (`RESERVED_HEADER_SLOTS` is
gone per §26.3, which is what makes 16 reachable.)

```c
typedef enum HomerPeerSendWrClass          /* the discriminant, explicit at last */
{
    HOMER_PEER_SEND_WR_CLASS_UNTAGGED = 0, /* a raw source-buffer ADDRESS (see 24.1.1) */
    HOMER_PEER_SEND_WR_CLASS_PAYLOAD,
    HOMER_PEER_SEND_WR_CLASS_COMMAND,
    HOMER_PEER_SEND_WR_CLASS_CONTROL,
    HOMER_PEER_SEND_WR_CLASS_CLIENT_COMPLETION,
} HomerPeerSendWrClass;

typedef struct HomerPeerPayloadSendWrIdFields
{
    uint8_t   kind;               /* DATA | RECEIVER_HEAD_ACK                                       */
    uint8_t   streamIndex;        /* -> streamEntries[]                                             */
    uint16_t  streamGeneration;
    uint8_t   ownerSlot;          /* DATA -> payloadSendOwnerTokens[];  ACK -> the ACK owner table.
                                     WHICH table is selected by `kind`.  A HANDLE, never a value.
                                     (see 24.3.1 / 26.5 -- this is what removes the token wrap limit) */
    uint8_t   reservedTailSlots;  /* 0 or 1: what this batch must FREE on retirement (P0-c, 23/26.3) */
} HomerPeerPayloadSendWrIdFields;                          /* 6 used, 8 with padding */

typedef struct HomerPeerControlSendWrIdFields
{
    bool      responsePublish;
    uint16_t  slotIndex;
    uint32_t  slotGeneration;
} HomerPeerControlSendWrIdFields;                          /* 8 bytes */

typedef struct HomerPeerIndexedSendWrIdFields              /* COMMAND and CLIENT_COMPLETION */
{
    uint16_t  index;
} HomerPeerIndexedSendWrIdFields;                          /* 2 bytes */

typedef struct HomerPeerSendWrId
{
    HomerPeerSendWrClass class;                            /* 4 bytes + 4 padding */
    union {                                                /* 8 bytes (widest member: uintptr_t) */
        HomerPeerPayloadSendWrIdFields  payload;
        HomerPeerControlSendWrIdFields  control;
        HomerPeerIndexedSendWrIdFields  command;
        HomerPeerIndexedSendWrIdFields  clientCompletion;
        uintptr_t                       untaggedSourceAddress;
    } u;
} HomerPeerSendWrId;

/* The send-CQ drain decodes one of these PER CQE. If a future field pushes this to 24 bytes it stops
 * fitting the x86-64 return registers -- break the BUILD so that becomes a deliberate decision. */
_Static_assert(sizeof(HomerPeerSendWrId) == 16,
               "decoded send WR-ID must stay 16 bytes (send-CQ drain hot path)");

/* THE priority decode. The ONLY place bits 60-63 are examined. SEND CQEs ONLY -- see 26.7:
 * receive WR-IDs are a SEPARATE namespace (bootstrap = 0, notification = slotIndex + 1) and a
 * "universal" decoder would misclassify those small numeric ids as UNTAGGED SENDS. */
static inline bool HomerPeerDecodeSendWrId(uint64_t raw, HomerPeerSendWrId *out);
```

**`ownerSlot` is ONE field selected by `kind`, not two.** A separate `sendOwnerSlot` and `ackOwnerSlot` would be
honest and wasteful -- they can never both be live, and the second one is what would push the struct to 24.

It is still passed by **out-param**, so in practice the 16 bytes mostly never materialise. The assert means we do
not have to *hope* so. ⚠ Still **MEASURE on the gate** (G9): source inspection cannot prove "free".

### 24.2 The couplings that silently cap tables

A WR-ID field width is a **hard cap on the table it indexes.** One such assert already exists (`:118`) — and it
is the only one:

```c
_Static_assert(CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS <= ..._STREAM_INDEX_MASK + 1U,
               "payload stream table must fit in payload WR id stream-index bits");
```

Somebody had exactly the right instinct once and never extended it. **Every coupling must have one**, or a
future table growth becomes a silent mis-decode instead of a build failure.

### 24.3 The new payload WR-ID layout — BYTE-ALIGNED

Two reasons, and the second is the real one:
1. Byte-aligned extraction is a shift by a multiple of 8 and an `0xff` mask.
2. **A hex-printed WR-ID becomes directly readable in a log.** On a subsystem where the CQE is the only evidence
   of what happened, that is worth more than the cycles.

| byte | bits | field | width | caps |
|---|---|---|---|---|
| 7 | 63 | `PAYLOAD_TAG` | 1 | — (must stay at 63; the exclusion chain depends on it) |
| 7 | 60–62 | `KIND` | 3 | 8 completion kinds (2 used: `DATA`, `RECEIVER_HEAD_ACK`) |
| 7 | 56–59 | *reserved* | 4 | — |
| 6 | 48–55 | `STREAM_INDEX` | **8** | ≤ **256** streams (today `MAX_LOCAL_SINKS`) |
| 4–5 | 32–47 | `STREAM_GENERATION` | 16 | unchanged |
| 3 | 24–31 | **`SEND_OWNER_SLOT`** | **8** | ≤ **256** send owners (today `MAX_INFLIGHT_OBJECTS + 1 = 64`) |
| 2 | 16–23 | **`RESERVED_HEADER_SLOTS`** | **8** | ≤ 255 (today `PAYLOAD_BATCH_MAX_WRITES = 32`) |
| 1 | 8–15 | **`RESERVED_TAIL_SLOTS`** | **8** | 0 or 1 |
| 0 | 0–7 | *spare* | 8 | — |

#### 24.3.1 `SEND_OWNER_SLOT` REPLACES the 40-bit `TOKEN`. This is the important change.

Today the WR-ID carries the **absolute 40-bit `completionToken`**. That is a *value*, and it imposes a **wrap
limit (~1.1e12)** that nothing checks — an implicit ceiling on how much a stream may ever send.

But the service **already** owns the authoritative table:
`payloadSendOwnerTokens[HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS + 1]` (`tuple_sink_service_process.c:1946`),
and `HomerServiceReservePayloadSendOwner` already hands back the `slotIndex` into it.

**So carry the HANDLE, not the VALUE.** The completion callback recovers the exact 64-bit token via
`stream->payloadSendOwnerTokens[slot]`. This:
- **removes the wrap limit entirely** — no absolute counter rides in the WR-ID, so any COPY size works *by
  design*, not by a bound;
- frees **34 bits**, which is what pays for P0-c's reservation fields;
- costs nothing on the wire (WR-IDs are local).

**RULE:** *carry a HANDLE when an authoritative table already exists; carry a VALUE only when it does not.*
Carrying both — as today — is redundant **and** imports the value's range limit into a design that had none.
(This is the mirror of §16's P0-e lesson: there the record was self-describing so the side table was
unnecessary; here the table is authoritative so the value is unnecessary.)

⚠ **`RECEIVER_HEAD_ACK` completions do NOT own a send-owner slot** (`tuple_sink_service_process.c:15072` applies
them as a frontier). The `KIND` field is decoded first, so ACK may keep a value-carrying encoding in the spare
bytes. **Verify before finalising the layout.**

### 24.4 The asserts (this is the deliverable, not decoration)

```c
/* -- capacity <-> width. If a table grows, the BUILD must break, not the decode. -- */
_Static_assert(CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS      <= ..._STREAM_INDEX_MASK      + 1U, "...");
_Static_assert(HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS + 1U<= ..._SEND_OWNER_SLOT_MASK   + 1U, "...");
_Static_assert(CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES<= ..._RESERVED_HEADER_MASK,        "...");
_Static_assert(CITUS_REMOTE_EXEC_PEER_PAYLOAD_TAIL_SLOTS_PER_BATCH <= ..._RESERVED_TAIL_MASK,     "...");
/* -- fields must not overlap each other -- */
_Static_assert(((MASK_A << SHIFT_A) & (MASK_B << SHIFT_B)) == 0, "payload WR id fields overlap");   /* each pair */
/* -- the four class tags are distinct single bits -- */
_Static_assert(PAYLOAD_TAG != COMMAND_TAG && COMMAND_TAG != CONTROL_TAG && ..., "...");
```

⚠ **`HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS` currently lives in `tuple_sink_service_process.c:326`**, where
the transport header cannot see it. **Move it into the transport header.** Once the WR-ID encodes the slot, that
cap **belongs to the WR-ID contract** — it is no longer a service-private tuning knob. Co-locating the constant
with the field width is the whole point.

**Plus a debug-mode encode→decode→compare round-trip check** in each encoder (`HOMER_PEER_RDMA_DIAG`). A static
assert proves the widths fit; only a round-trip proves the *shifts* are right.

---

## §25 — F7: "NO CQE MAY BE DESTROYED." Prerequisite for P0-c.

**Status: SPEC (2026-07-12). Found by adversarial review; verified by me. See §23.6.**

### 25.1 The contract already exists — in the header — and the code violates it

`remote_execution_peer_transport_rdma.h:1094-1100` states it outright:

> *"control/bootstrap helpers may still perform internal control-only drains, but service-owned command, payload,
> and peer-client-completion CQEs **must retire through the typed callback set here**."*

`TupleSinkServiceWaitForSendCompletion` (`:3922`) is one of those "control/bootstrap helpers" — **and it runs on
QPs that are NOT control-only.** It passes **NULL callbacks** into `TupleSinkServiceHandleTaggedSendCompletion`
(`:3962`). Control CQEs are retired internally (safe — this is what makes P0-a/P0-b sound). But a **payload,
command, or peer-client CQE requires a callback**: with NULL, the handler errors out (`:3796`, `:3810`, `:3832`,
`:3846`, `:3890`, `:3905`) — **after `ibv_poll_cq` has already dequeued the CQE** (`:3938`).

**The completion is destroyed. Its owner never retires.** The rule is written down; nothing enforces it.

**Reachable:** tuple/COPY command completion stores the **payload** connection in
`peerCommandCompletionConnectionHandle` (`tuple_sink_service_process.c:27898`, `:38667`), so the blocking epoch
write (`:19461`) runs on a QP that also carries payload batches. (SQL commands use `CRITICAL_CONTROL`
(`:37991`), whose CQEs retire internally — which is exactly why the DPU gate has never seen this.)
**Backend-to-backend COPY is 🔴 broken at HEAD; this is a candidate contributor.**

### 25.2 Why this BLOCKS P0-c

Any retirement scheme that advances a frontier **by a delta the completion reports** is sound only if **every
completion is delivered exactly once.** A single destroyed CQE desyncs such a frontier **permanently and
silently** — the exact failure class this audit exists to eliminate. **So F7 lands first.**

### 25.3 The fix

| # | change | why |
|---|---|---|
| 1 | **Register the send-completion callbacks PERSISTENTLY on `TupleSinkServicePeerTransportState`** — the same pattern as `lifecycleObserver` in `TupleSinkServiceCreatePeerTransportState` (`..._rdma.h:440`). The service already builds the struct (`tuple_sink_service_process.c:15232`). | Then **every** internal drain — including the blocking wait — can retire typed CQEs through their real owners. **No signature churn across the five public wait-backed APIs.** |
| 2 | `WaitForSendCompletion` uses the **registered** callbacks instead of NULL, and **keeps polling** for its own completion after dispatching someone else's. | It becomes a *participant* in the drain instead of a thief. |
| 3 | **Match on `wr_id`, not on opcode.** Pass the expected WR-ID in. (Untagged WR-IDs are already the source-buffer address, `:5231` — a usable identity.) Keep the opcode check as a secondary sanity check. | Today ANY untagged `IBV_WC_RDMA_WRITE` CQE satisfies the wait. That is a latent premature-retirement bug: the caller would believe its source buffer is free while its WR is still in flight. |
| 4 | If a typed CQE arrives and **no callbacks are registered**, that is an **ALARM that NAMES THE LOST EVENT** (`"ALARM ... RETIREMENT EVENT LOST kind=%u stream=%u token=%llu"`) + reset. **Never a quiet `return false`.** | §0: *silence must not be a valid state.* A destroyed retirement event currently reads as a generic error. |
| 5 | **Enforce the header's own rule:** a blocking wait on a connection whose traffic class is not `CRITICAL_CONTROL` is legal **only** if callbacks are registered. | Turns the prose contract into a checked one. |

### 25.4 Follow-up, NOT this stage (PERF)

The blocking wait is also a **serialization point on a hot path** — the comment at
`tuple_sink_service_process.c:19445` admits it *"serializes terminal command completion across all tuple COPY
shard sessions."* Moving that epoch write to the async publish path, or onto the `CRITICAL_CONTROL` lane, is a
**PERF item** (it is a candidate contributor to the ~450 µs/command). **Do not conflate it with the correctness
fix.**

---

## §23 (REVISED) — P0-c, now built on §24 + §25

**Order of work: §24 (WR-ID contract) → §25 (delivery guarantee) → §23 (the staging pools).**

The bug (unchanged, §23.1–23.2): both staging pools are handed out by a **blind modulo cursor**, and
**the cursor counts ATTEMPTS while safety depends on COMPLETIONS.** A slot is reserved and **overwritten before
`ibv_post_send` is even attempted** (`:10141`), and the cursor advances **even when the post fails** — so a
`WOULD_BLOCK` retry storm laps the pool and scribbles over the live RDMA source of an in-flight WR. This is why
**both** pools are broken (§23.5 F1: my "4× headroom" argument for the header pool was worthless — it bounded
*live* slots and never asked *what moves the cursor*).

**The fix, in three parts:**

1. **ROLL BACK any reservation that does not post.** Cheap, local, single-threaded — and **most of the bug on its
   own.** Covers the mid-construction `return false` (`:9879`), the `WOULD_BLOCK`, and the hard post failure.
2. **The completion says what it frees** (§24): `RESERVED_TAIL_SLOTS` + `RESERVED_HEADER_SLOTS` in the WR-ID.
   Retirement advances both frontiers by exactly those counts. Sound **because** §25 guarantees every CQE is
   delivered exactly once.
3. **Reservation returns `WOULD_BLOCK` BACKPRESSURE at capacity, and does NOT drain internally** (the owner
   drains; the reserver reports — the same rule P0-b's op table follows).

**Also:** the **ACK path** (`:10338`) is a **second producer** into the tail pool (§23.5 F2) and gets the same
treatment. Reset already memsets the whole connection state after QP/CQ teardown (`:5566`), so the new frontiers
are reset for free — **verify, do not assume.**

**Severity, corrected (§23.5 F5):** the receiver **validates** framing/generation/sizes, so a bogus tail causes a
**stall, a reset, or wrong credit** — not silent object corruption. Still a real bug; **not** the catastrophe I
first claimed.

---

## §26 — ADVERSARIAL REVIEW ROUND 2 (2026-07-12): §24/§25/§23 CORRECTED. We now agree.

Round 2 of the review. **It broke the F7 fix, re-ordered the work, and SIMPLIFIED P0-c.** Load-bearing claims
re-verified by me in the code before acceptance.

### 26.1 ⚠ THE WORK ORDER IS WRONG — it is §25 → §24 → §23, not §24 → §25 → §23

I claimed §25 (F7) needed §24 (the WR-ID contract) first. **REFUTED.** The synchronous WRs that the blocking
wait cares about already carry an **exact identity** — their source-buffer address (`:5231`). So F7 can match on
`wr_id` **today**, with no layout change at all.

**Land the delivery guarantee FIRST, on the smallest possible diff.** §24 is a *hardening* change (I verified the
exclusion chain is correct today, §24.1.1), so it must not be entangled with a *correctness* fix — otherwise a
validation failure has two candidate causes.

### 26.2 ⚠ §25's FIX IS BROKEN AS DESIGNED — the context is a STACK LOCAL

I proposed *"register the send-completion callbacks persistently, like `lifecycleObserver`."* **REFUTED, and this
was the claim I flagged as least certain.**

`HomerServiceExecuteCqDrainProgressPlan` builds **both** structs as **stack locals**, fresh per call
(`tuple_sink_service_process.c:15218-15219`). **Persisting a pointer to `drainContext` would DANGLE.**

But the *contents* say exactly how to fix it:

| member | lifetime | disposition |
|---|---|---|
| `callbacks.*` (4 fn ptrs) | **static functions — stable forever** | make the struct a **file-scope `static const`**, register once |
| `drainContext.sessionStates` | **long-lived** service table | safe to register |
| `drainContext.streamEntries` | **long-lived** service table | safe to register |
| `drainContext.progressResult` | ⚠ **PER-CALL** — points at the caller's stack | **NEVER register this.** Register a **service-owned fallback** progress result instead. |

**Corrected fix:** register a service-owned, long-lived `HomerServiceCqDrainContext` whose two table pointers are
the real tables and whose `progressResult` points at a **service-owned accumulator**, never at a caller's frame.
A retirement that happens *inside a blocking wait* has no caller progress-result to report into — and that is
fine; it must still RETIRE, which is the whole point.

**Also REFUTED:** `TupleSinkServiceWaitForPeerSendCompletionRdma` (`:10397`) **takes no expected WR-ID**, so
"match on identity" cannot cover it without an API change. Add the parameter.

### 26.3 ✅ P0-c GETS SIMPLER — the HEADER pool needs ROLLBACK ONLY

**The single most useful correction.** Once reservations that never post are **rolled back**, the header pool is
safe **by capacity**: ≤ 256 accepted WRs (the send-queue depth) against **1024** slots. **It needs no frontier,
no WR-ID field, no retirement tracking.**

This retro-fits §23.5's F1 refutation into its proper place: the header pool was never broken *by capacity* — it
was broken **only** by the un-rolled-back reservations. Fix the rollback and the capacity argument becomes true.

**So P0-c splits cleanly:**
- **HEADER pool (`[1024]`) — ROLLBACK ONLY.**
- **TAIL pool (`[128]`) — rollback **AND** capacity/retirement tracking** (128 slots vs up to 128 accepted
  batches: zero margin, so it genuinely needs the frontier + `WOULD_BLOCK`).

**And `RESERVED_HEADER_SLOTS` DISAPPEARS from the WR-ID** (§24.3), which frees a byte.

### 26.4 ⚠ PARTIAL POST MUST NOT ROLL BACK

I said "roll back any reservation that does not post." **Too broad.** On a **PARTIAL** post some WRs *are* live
and *are* reading their staging slots. The existing callers already **retain** partially-posted ownership for the
reset path (`tuple_sink_service_process.c:31111`, `:31652`, `:32427`).

**Corrected:** roll back **only on a ZERO-WR failure.** On PARTIAL, **retain** the reservation and let the
connection reset retire it — exactly the rule P0-b already follows for its control op (§22.9 part 5).

### 26.5 ACK gets a HANDLE too — it has its own authoritative table

I left this open (§24.3.1). **Resolved:** `RECEIVER_HEAD_ACK` completions have their **own 64-slot authoritative
token/tail/state table** (`tuple_sink_service_process.c:1994`). So ACK must **also** carry an **ACK-owner handle
selected by `KIND`**, not a value — uniform with `DATA`, and its 64-bit token likewise stops riding in the WR-ID.

### 26.6 ⚠ THE SWITCH CONVERSION IS **NOT** MECHANICAL — three sites, and one is a trap

| site | why it is not a drop-in |
|---|---|
| `:3485` diag ladder | It deliberately **falls through** to the untagged **pointer-description** arm. The switch needs an explicit `UNTAGGED` case that preserves that. |
| `:3779` `HandleTaggedSendCompletion` | It returns **success with `handled = false`** for untagged IDs (`:3912`). The `UNTAGGED` arm must preserve *"not handled"* — **not** treat untagged as invalid. |
| **`:4078`** | ⚠ **This is FAILURE INSTRUMENTATION, not owner dispatch.** It invokes the payload **probe** *before* the CQE-success check, passing status/vendor error (`:4088`); **failed** CQEs then exit **without retirement** (`:4095`), and only successful ones reach dispatch (`:4106`). **A naive single switch could suppress failure telemetry, or RETIRE FAILED WORK.** Correct shape: **decode once**, probe `PAYLOAD` *regardless of success*, retire **only after** success. |

**And my site inventory was incomplete: there are FOUR posting guards, not two** — command at `:9363`, `:9449`
**plus client-completion at `:9411`, `:9484`.**

### 26.7 ⚠ RECEIVE WR-IDs ARE A SEPARATE NAMESPACE — name the decoder accordingly

**The catch I would have shipped a bug over.** Receive WR-IDs do **not** use the tag scheme at all: bootstrap
receive expects **zero** (`:3234`), and notification receives encode **`slotIndex + 1`** (`:5028`, decoded at
`:6960`).

**A "universal" `HomerPeerDecodeWrId` would classify those small numeric receive IDs as UNTAGGED SENDS.**
The decoder must be named and documented as a **SEND-WR decoder** (`HomerPeerDecodeSendWrId`), and the receive
namespace must be stated as separate and disjoint.

### 26.8 Smaller corrections accepted

- **`HomerPeerDecodeWrId` is NOT known-free (G9).** The decoded object may be **24 bytes**, not 16; an x86-64
  aggregate > 16 bytes commonly uses caller-provided return storage. The sketch already uses an **out-param**,
  which sidesteps it — **but do not claim "free". MEASURE on the gate.**
- **The decoder stays PRIVATE (G10).** Nothing outside `remote_execution_peer_transport_rdma.c` decodes a raw
  WR-ID; the service receives **already-decoded fields** (`tuple_sink_service_process.c:15032`). So masks,
  shifts, layout and asserts remain `static` in the `.c`; only narrow **encoder** APIs stay exported.
- **Control's encoder silently MASKS its fields** (`:3283`) while command's and client's **range-check** theirs
  (`:3380`, `:3422`). **Control needs the range checks + asserts** — a silent mask is how an over-large index
  becomes a mis-routed CQE.
- **The untagged-pointer assumption (§24.1.1) must be enforced ALWAYS, not only under `DIAG`.** Otherwise a
  future *numeric* untagged caller silently enters a typed class. Check it at **every untagged send encoder /
  post helper**.
- **G11 clean:** no production steady-state send WR-ID is a non-pointer untagged value. The assumption holds
  today.

### 26.9 AGREED PLAN OF RECORD

1. **§25 — F7, the delivery guarantee.** Service-owned stable context + `static const` callbacks; the blocking
   wait dispatches typed CQEs to their real owners instead of destroying them; match on `wr_id` (add the missing
   param to `:10397`); ALARM naming any lost retirement event. **Smallest diff, foundational, no layout change.**
2. **§24 — the WR-ID contract.** `HomerPeerDecodeSendWrId` + `HomerPeerWrClass` discriminated union (of decoded
   fields, NOT bitfields); byte-aligned layout; `SEND_OWNER_SLOT`/ACK-owner **handles** replace the 40-bit token
   (removing the wrap limit); the capacity↔width `_Static_assert`s; the three non-mechanical switch sites done
   carefully; control's range checks; the always-on untagged-pointer check.
3. **§23 — P0-c.** Header pool: **rollback only**. Tail pool: rollback (zero-WR failures only) + frontier +
   `WOULD_BLOCK` backpressure, retired by the completion's `RESERVED_TAIL_SLOTS`. ACK path (`:10338`) gets the
   same treatment.

### 25.5 IMPLEMENTATION SPEC (concrete) — §25 / F7

**⚠ The single most important discovery while specifying this.** `TupleSinkServiceWaitForPeerSendCompletionRdma`
(`:10397`) says in its OWN doc comment:

> *"waits for the signaled payload publish checkpoint. Once it returns successfully, all earlier unsignaled WRs
> posted on the same QP are locally complete as well, so **the caller may advance its source-slot reuse
> frontier**."*

**That is P0-c's frontier, and this is the function whose success authorises advancing it — and it matches on
OPCODE ONLY, with no `wr_id`.** A stranger's `RDMA_WRITE` CQE satisfies it, after which the caller frees staging
slots whose WRs are **still in flight**.

**F7 and P0-c are not adjacent bugs. F7 is the mechanism by which a caller is TOLD it is safe to do the thing
P0-c is trying to make safe.** Fixing the pools while this function can lie would be building on sand. This is
the concrete vindication of §26.1's re-ordering.

**It has NO CALLER** (verified). **DELETE IT** — an unused API that authorises a frontier advance on an opcode
match is a landmine the next implementer will reach for **by name**. Deleting is a stronger fix than repairing.

#### The edits

| # | file | change |
|---|---|---|
| 1 | `..._rdma.c` (`:799` struct) + `.h` | Add `const TupleSinkServicePeerSendCompletionCallbacks *sendCompletionCallbacks; void *sendCompletionContext;` to `TupleSinkServicePeerTransportState`. New API `TupleSinkServiceRegisterPeerSendCompletionCallbacksRdma(...)`. **Document that both pointers MUST outlive the transport.** |
| 2 | `tuple_sink_service_process.c` | Make the callback set a **file-scope `static const`** (its 4 members are static function pointers — stable forever; mind the `#ifdef HOMER_DPU_P2_DIAG` probe at `:15234`). Add a **service-owned** `HomerServiceCqDrainContext` whose `sessionStates`/`streamEntries` are the long-lived tables and whose `progressResult` points at a **service-owned accumulator** — ⚠ **NEVER a caller's stack frame** (§26.2). Register both once, after the tables exist. |
| 3 | `tuple_sink_service_process.c` | ⚠ **DO NOT change the normal drain.** `HomerServiceExecuteCqDrainProgressPlan` (`:15212`) keeps its per-call stack structs — it needs the *caller's* `progressResult`. The registered pair is used **only** by internal/blocking drains. |
| 4 | `..._rdma.c:3922` | `TupleSinkServiceWaitForSendCompletion` gains `uint64_t expectedWorkRequestId`. It passes the **registered** callbacks/context to `HandleTaggedSendCompletion` instead of NULL, and **keeps polling** after dispatching someone else's CQE. |
| 5 | `..._rdma.c:3973` | Match on **`wr_id == expectedWorkRequestId`**, not on opcode. Keep the opcode check as a **secondary sanity check that ALARMs on mismatch** (a right-id/wrong-opcode CQE means the WR-ID space is corrupt). |
| 6 | `..._rdma.c:3796`+ | If a typed CQE arrives and **no callbacks are registered**: **ALARM that NAMES THE LOST EVENT** (`"ALARM ... RETIREMENT EVENT LOST class=%u wr_id=0x%llx"`), then fail. **Never a quiet `return false`** — today a destroyed retirement event is indistinguishable from a generic error. |
| 7 | callers `:7756`, `:9573`, `:10279`, `:10465` | Thread the WR-ID they posted (for untagged writes it is the source-buffer address, `:5231`). |
| 8 | `..._rdma.c:10397` + `.h` | **DELETE `TupleSinkServiceWaitForPeerSendCompletionRdma`** (see above). |

#### ⚠ The one thing the implementer MUST verify, not assume

**Re-entrancy.** Dispatching a **payload** callback from inside a blocking wait means the payload completion
handler runs while the caller is somewhere deep in a publish path. P0-a added `HomerServiceSendCqDrainDepth`
(`tuple_sink_service_process.c:3889`), and the review verified it wraps only the **typed service drains** — it
does **NOT** cover the blocking wait's direct polling, so it will neither protect nor block this dispatch.

**So: does `HomerServiceHandlePayloadSendCqeFromDrain` only advance stream frontiers, or can it re-enter the
publisher / mutate state the interrupted caller is mid-way through updating?** If it can, this fix introduces a
re-entrancy bug and must change shape (e.g. queue the CQE for the next real drain instead of dispatching inline).
**VERIFY IN THE CODE. Report the answer either way.**

#### Acceptance

- Builds all targets. 3× gate, `ALARM = 0`, clean SIGTERM (the standard pipeline).
- ⚠ **The gate will NOT exercise this** — the SQL command path uses the `CRITICAL_CONTROL` lane, whose CQEs
  already retire internally (§23.6). **This stage is validated for NO REGRESSION, not for the fix.** Say so.
  The workload that would exercise it is backend-to-backend COPY, which is 🔴 **already broken at HEAD**.

### 25.6 §25 / F7 — LANDED + VALIDATED (citus `16e4bdf3e`, 2026-07-12)

**PASS.** 3× gate, `2000/2000` tx, **0 failed**, **ALARM = 0** on all four logs across three runs and a clean
SIGTERM. All four intended-path proofs held; the farnet0 host service log is banner + the new registration line
and **nothing else**.

**The wiring proof (new acceptance criterion, and the one that mattered):**
`tuple-sink service: registered persistent send-completion owner dispatch callbacks=0x… context=0x…` printed
**exactly once at startup on all three services**, with real pointers. **Its ABSENCE would have meant every
internal blocking drain was still running without owner dispatch — i.e. the bug still live, silently.**

**PERFORMANCE — the first VALID comparison this session.** 288.7 / 284.9 / 283.3 tps against the previous
stage's like-for-like 286.9 / 285.5 / 283.4 (**same `-t 2000`, same `--debug`, same command shape**) — within
~1%. **No regression.** This is what §22.10.2 asked for: a reference stamped with its workload shape.

#### 25.6.1 ⚠ THE GATE DOES NOT EXERCISE THIS FIX — say so, do not let PASS imply otherwise

The SQL command path runs on the **`CRITICAL_CONTROL`** lane, whose CQEs **already** retired internally. The bug
is reachable only on the tuple/COPY path (which shares a QP with payload batches), and **backend-to-backend COPY
is 🔴 broken at HEAD** for unrelated reasons. **This run validated NO REGRESSION + THE MACHINERY IS WIRED UP.
It did not validate the fix.** The fix is validated by construction and by review, not by execution.

#### 25.6.2 Review corrections applied to the worker's implementation

- ⚠ **The worker shipped code that DOES NOT COMPILE** — it invented a `HOMER_PEER_RDMA_LOG` macro that does not
  exist in this file. **Its build was blocked by the sandbox, so it never compiled its own work.** Caught by the
  editor's diagnostics before it reached a build. **This is precisely why the main agent builds.**
- ⚠ **The fix reintroduced its own bug.** A foreign untagged CQE was handled with a **silent `continue`** — i.e.
  the CQE was **destroyed**, in the stage whose one-sentence contract is *"no CQE may be destroyed."* The
  worker's reasoning was sound (untagged sends are serialized by their own blocking wait, so a second untagged WR
  cannot be in flight) — **but that is exactly the argument that made the original bug invisible.** Changed to a
  LOUD ALARM naming the undeliverable event. **"Unreachable" is a reason to ALARM, not a reason to stay quiet.**

#### 25.6.3 Re-entrancy — VERIFIED, not assumed

`HomerServiceHandlePayloadSendCqeFromDrain` (`tuple_sink_service_process.c:15032`) and its callees are
**`HomerServiceApply*Frontier` only** — no publish, no post, no drain, no wait. So dispatching a payload CQE from
inside a blocking wait **cannot recurse into the publisher**. (P0-a's `HomerServiceSendCqDrainDepth` guard wraps
only the typed service drains and would neither protect nor block this dispatch — so this had to be answered on
its own merits, and it was.)

**NEXT: §24 (the WR-ID contract), then §23 (P0-c).**

---

### 24.5 ⚠ DEVIATION FROM THE PLAN — the union becomes DISJOINT, and the priority chain is DELETED

**Decision taken 2026-07-12 while implementing §24, before writing any code.** §24.1's RULE OF RECORD said:
*"the WR-ID is a priority-decoded tagged union … This must be stated in the code, not just here."* **That rule is
now WITHDRAWN.** The code will not state it, because the code will no longer *have* it.

#### The fact that unlocks this (VERIFIED, not inferred)

Every WR-ID bit-layout macro — `..._WR_ID_TAG`, `..._KIND_*`, `..._STREAM_INDEX_*`, `..._GENERATION_*`,
`..._TOKEN_MASK`, `..._INDEX_MASK`, `..._RESPONSE_FLAG` — and every `...WrIdIsTagged` predicate lives **only** in
`remote_execution_peer_transport_rdma.c`. Nothing else in either tree references one:

```
grep -rn 'WR_ID_TAG|WR_ID_KIND|WR_ID_STREAM|WR_ID_GENERATION|WR_ID_TOKEN|WR_ID_INDEX|WR_ID_RESPONSE|WrIdIsTagged' \
     --include=*.c --include=*.h | grep -v remote_execution_peer_transport_rdma.c   ->  EMPTY
```

Outside the transport `.c` a WR-ID is an **opaque `uintptr_t`**: the service calls an exported encoder
(`TupleSinkServiceEncodePeerCommandWrId` at `tuple_sink_service_process.c:21519`,
`TupleSinkServiceEncodePeerClientCompletionPublishWrId` at `:19168`) and hands the result straight back to an
exported post API. **It never inspects a bit.** Combined with §24's standing fact that a WR-ID is never
transmitted, the layout is **entirely private and free to change**.

#### Why the priority decode was never load-bearing — only inherited

§24.1 kept the overlapping tags because it was preserving the *existing* bit assignment. But §24.3 was already
going to **rewrite the payload layout wholesale** (byte-aligned, `OWNER_SLOT` replacing the 40-bit token). Once
the layout is being rewritten anyway, **preserving the exclusion chain buys nothing and costs the one thing §24
exists to remove.**

#### THE NEW LAYOUT — one disjoint 3-bit CLASS field at bits 61-63

```
bits 61-63   CLASS (3 bits, 8 values, 5 used)      <- HomerPeerSendWrClass, the discriminant, ON THE WIRE OF THE ID
               0 = UNTAGGED            (the whole 64-bit value IS a raw source-buffer address)
               1 = PAYLOAD   2 = COMMAND   3 = CONTROL   4 = CLIENT_COMPLETION

PAYLOAD (class 1), bits 0-60:
  bits 56-58   KIND                 3    DATA | RECEIVER_HEAD_ACK (2 of 8 used; was 1 bit)
  bits 59-60   reserved             2
  bits 48-55   STREAM_INDEX         8    caps <= 256 streams   (was 6 bits / 64)
  bits 32-47   STREAM_GENERATION   16    unchanged
  bits 24-31   OWNER_SLOT           8    caps <= 256 owners    (REPLACES the 40-bit token, 24.3.1)
  bits 16-23   reserved             8
  bits  8-15   RESERVED_TAIL_SLOTS  8    0 or 1 (P0-c / 26.3)
  bits  0- 7   reserved             8

CONTROL (class 3), bits 0-60:  bit 60 RESPONSE_PUBLISH; bits 16-47 SLOT_GENERATION (32); bits 0-15 SLOT_INDEX (16)
COMMAND (class 2) / CLIENT_COMPLETION (class 4), bits 0-15: INDEX (16)
UNTAGGED (class 0): the raw source address. REQUIRES bits 61-63 clear -- see below.
```

**What this buys over the priority-decoded version:**
1. **The exclusion chain is GONE.** There is no decode *order* left to get wrong, so there is nothing to
   document, nothing to preserve, and no fifth class can silently steal a fourth class's CQE. §24.1's entire
   hazard evaporates rather than being commented.
2. **The decode is one shift + one mask** — strictly cheaper on the send-CQ drain hot path than four
   short-circuit predicates (which is what G9 was worried about; this makes the worry moot).
3. **`UNTAGGED == 0` is now the NATURAL encoding**, not "fell off the end of the ladder". A pointer has bits
   61-63 clear, so a source address decodes to class 0 by construction.
4. **Class distinctness is trivially assertable** — they are enum values, not bit positions that must not
   collide. The §24.4 tag-distinctness asserts become `_Static_assert(HOMER_PEER_SEND_WR_CLASS_COUNT <= 8)`.
5. **Hex-readable top nibble** (bits 60-63): `0x0`=untagged, `0x2`=payload, `0x4`=command, `0x6`/`0x7`=control
   (low bit = response), `0x8`=client-completion. §24.3's second motivation, delivered.

#### The untagged-pointer assumption is UNCHANGED in kind, and slightly WEAKER in degree

It was *"every registered source address has bits 60-63 clear"*; it is now *"bits 61-63 clear"* — a strictly
easier requirement, still trivially true (canonical user VAs are below 2^57 even with 5-level paging). It is
**enforced at every pointer -> WR-ID conversion**, of which there are exactly **five**, all in the transport `.c`:

| site | the pointer |
|---|---|
| `TupleSinkServicePostWriteBufferInternal` (`:5302`) | `(uintptr_t) buffer` — the generic untagged write |
| payload batch, body WRs (`:9971`) | `(uintptr_t) payloadWrite->buffer` |
| payload byte-ring, body WRs (`:10199`) | `(uintptr_t) payloadWrite->buffer` |
| unsignalled ACK tail (`:10420`) | `(uintptr_t) tailSource` |
| `TupleSinkServicePostSendBuffer` (`:5171`) | `rdma_post_send`'s `context` arg **becomes the `wr_id`** — the bootstrap send is untagged too |

⚠ The 4th and 5th were **not in §24's site inventory.** The bootstrap one is invisible unless you know that
`rdma_post_send(cmId, context, ...)` stores `context` as the `wr_id`.

#### What does NOT change

- **Receive WR-IDs remain a separate, disjoint namespace** (§26.7): bootstrap recv expects `0`, notification
  recv encodes `slotIndex + 1`. The decoder is still named `HomerPeerDecodeSendWrId` and still documents that.
- **The decoded object is still a union of DECODED FIELDS, not bitfields** (§24.1.2) — the reason stands
  verbatim: `_Static_assert` cannot check a bitfield's bit *position*, which would destroy §24.4.
- **`sizeof == 16`** still asserted; the decoder stays `static inline` and private (§26.8 G10).
- **The three non-mechanical switch sites** (§26.6) are unaffected by this change — `:4146` is still failure
  instrumentation and still must probe before the success check and retire only after it.

#### Residual risk, stated honestly

Changing the class encoding changes **every** WR-ID value the process produces. A stale WR-ID *in flight across
the change* is impossible (WR-IDs never leave the process and never outlive it), so there is no mixed-version
hazard — but a **stale binary** on one DPU would produce IDs the other side never decodes... **no: WR-IDs are
purely local to one process's own send CQ.** A DPU running an old binary decodes only its OWN IDs. So even a
partial deploy is safe. **No ABI bump, no DPU resync required** — though both DPUs are rebuilt anyway.

---

### 24.6 ⚠ REVIEW ROUND 3 BROKE §24.5's KEY CLAIM — the bare handle was a REGRESSION. OWNER_INCARNATION added.

**This is the objection I asked for, and it landed.** I gave the reviewer six falsifiable claims and named C3 as the
one I was least sure of. C3 is where it hit.

#### What I claimed (C3), and what survived

> *"OWNER_SLOT as a HANDLE is safe: a send-owner slot cannot be recycled while its WR is still in flight, so
> `payloadSendOwnerTokens[slot]` at CQE time always yields the token that WR was posted with."*

**The LIFETIME half is VERIFIED** — the reviewer checked it end to end and so did I:
- reserve refuses a slot whose owner is `RESERVED`/`POSTED` (`tuple_sink_service_process.c:16513-16542`);
- commit requires the same slot + `RESERVED` + token (`:16617-16651`);
- retirement walks FIFO `POSTED` owners and only then decrements (`:16735-16818`);
- zero-WR rollback frees and rewinds; **PARTIAL post deliberately RETAINS** for reset (`:31111`, `:31652`, `:32427`);
- reset destroys QP+CQ **first** (`remote_execution_peer_transport_rdma.c:5651`), and only then aborts owners
  (`:29442`); stream rebinding cannot proceed until both owner counts are zero (`:25939`);
- the ACK table has the same discipline (`:32759-32903`).

**No legal path frees and re-hands-out a slot while its old QP/CQ can still produce a CQE.** The handle is CORRECT.

#### What was REFUTED — and it is subtle enough that I had waved past it myself

> **"Looking the token up FROM the slot the CQE names makes the token comparison TAUTOLOGICAL."**

Today the WR id carries the **absolute** token, so a stale / duplicated / mis-routed CQE is caught by comparing it
against the owner table (`:16871-16880` for DATA; `:32845-32882` for ACK, which additionally demands FIFO equality).
Resolve `token = table[slot]` and then "compare" it against `table[slot]` and you have compared a value with itself.
**The handle preserves the LIFETIME guarantee and silently DELETES the FAULT DETECTION.**

That is the exact shape of a refactor that looks safe, passes every test, and removes a check nobody realises is
load-bearing until something is already corrupt.

#### THE FIX — `OWNER_INCARNATION`, 8 bits, bits 16-23 (was reserved)

Each owner table now carries a **per-slot incarnation counter**, bumped on every hand-out
(`payloadSendOwnerIncarnations[]`, `receiverHeadAckIncarnations[]`). It rides in the WR id beside the slot, and
`HomerServiceResolvePayloadSendOwnerToken` (`tuple_sink_service_process.c`) refuses a CQE that does not name the
incarnation currently live in that slot. Three checks, all of which the absolute token used to give us for free:

| check | catches |
|---|---|
| slot in range | a corrupt WR id |
| owner state is `POSTED` | a CQE for a FREE or already-RETIRED owner — **stronger than the old token check** |
| **incarnation matches** | a late CQE for a **RECYCLED** slot — *the one a bare handle cannot do* |

**And the downstream checks stay REAL, not tautological**, because they compare against the **FIFO head**, not
against the slot the CQE named: `HomerServiceCompleteTrackedPayload` still requires an exact token match walking
from the head, and `HomerServiceRetireReceiverHeadAckOwners` still demands the head's token equal the resolved one
(which is now, in effect, an assertion that `ownerSlot == headIndex`).

**Why an incarnation beats simply keeping a (shorter) token:** the incarnation only has to DISTINGUISH consecutive
uses of one slot, never to ORDER them. So its 8-bit wrap is not a limit the way the token's 40-bit wrap was: a
wrap costs at worst one missed detection of a fault that cannot occur anyway (verbs does not duplicate CQEs, and
§25/F7 guarantees exactly-once software delivery), whereas the absolute token's wrap was a **correctness ceiling on
how much a stream could ever send.** We keep the wrap-limit removal AND the fault detection.

#### Other review findings, and what was done with each

| finding | verdict | action |
|---|---|---|
| **C1 qualified** — the standalone DOCA validation programs (`postgres-citus/homer/doca_validation/*.c`) have their OWN unrelated WR-ID packing | ACCEPTED, harmless | They never touch the Homer encoders or transport. The *production* layout is still 100% private. No change. |
| **C2** — no sixth pointer→WR-id site exists; and note the payload body WRs are **UNSIGNALLED**, so they normally produce no CQE at all | ACCEPTED | The five sites are all checked. The unsignalled point is *why* it still matters: a **failed/flushed** body WR does produce a CQE, carrying that pointer. Comment says so at the site. |
| **C4** — §24.4 was incomplete: **COMMAND's** 16-bit index caps a **256**-entry table, and **CLIENT_COMPLETION's** caps another | ACCEPTED — real gap | Both constants moved into the transport header and asserted. That is **four** tables now capped by a WR-ID field, not two. |
| **C5** — no ABI bump needed; nothing derived from a WR id is ever transmitted | VERIFIED both ways | No change. |
| **C6** — the `:4146` naive-switch trap is real; **and there is a FOURTH ordering-sensitive CQ consumer**, `TupleSinkServiceWaitForSendCompletion` | ACCEPTED | The drain keeps its 3-block order, now with a comment stating *why* the order is load-bearing. The blocking wait's asymmetry (it does not run the payload failure probe) is recorded below as a KNOWN GAP. |

#### ⚠ KNOWN GAP, deliberately not closed in §24

`TupleSinkServiceWaitForSendCompletion` calls `TupleSinkServiceLogSendCompletionFailure` on a failed CQE but does
**not** invoke `payloadCompletionProbe`, while the async drain does. So a payload CQE that fails *while a blocking
wait is polling* gets the generic failure log but not the P3 owner trace. It is **diagnostic-only** (the probe is
`#ifdef HOMER_DPU_P2_DIAG`) and the CQE is still correctly not retired. Recorded rather than fixed, so it is a
known asymmetry instead of a surprise.

### 24.7 ⚠ THE ASSERTS PROVE WIDTHS. THEY CANNOT PROVE SHIFTS. Hence a startup self-test.

Realised while wiring the asserts, and it is the most useful thing in this section:

> **Transpose two shifts and every single `_Static_assert` still passes** — no width changed. The entire assert
> block is blind to the one error it most looks like it is preventing.

Only an encode→decode round trip catches a wrong offset, and §24.4 put that round trip under
`HOMER_PEER_RDMA_DIAG` — **which is `0`** (`remote_execution_peer_transport_rdma.c:387`). So as specified, the check
that proves the layout **would never have run.**

**`TupleSinkServiceVerifyPeerSendWrIdLayoutRdma`** now round-trips **one WR id per class at its field boundaries,
once at startup, in EVERY build**, and the service **refuses to start** if it fails. It costs microseconds on the
control path and it converts "the shifts are right" from an assumption into something the process proves about
itself before it posts a single WR. Its `PASSED` line is also the landed-proof string for a deploy.

**Rule earned:** *a static assert proves a field FITS; only a round trip proves it is in the right PLACE. If the
round trip is behind a debug flag, the layout is unverified in every build that matters.*

---

### 24.8 ✅ §24 LANDED + VALIDATED (2026-07-12)

**The DPU gate passed** (`pgbench --homer --homer-dpu-command`, 4-role cross-node topology, hard clean baseline).

| acceptance gate | result |
|---|---|
| `send WR-ID layout self-test PASSED (5 classes vs GOLDEN ids, plus field boundaries)` | **exactly 1** in EACH of the 3 service logs (farnet0 host, farnet1 DPU, farnet0 DPU) |
| `ALARM` count, all services | **0** — none of `named a RECYCLED owner slot`, `named a non-POSTED owner`, `WR-ID address-space assumption is broken`, `RETIREMENT EVENT LOST`, `may be corrupt` |
| `self-test FAILED` / service `FATAL` | **0** |
| correctness (`-t 5 --debug`) | `transport: homer-dpu-command (implies dpu result relay)`; **5 distinct abalances**; 5/5, 0 failed |
| DPU spawn | `DPU backend spawn begin` + `COMPLETED … launched_pid=3558654` on the farnet1 DPU |
| anti-fallback | farnet0 host service log = **startup banner only**, zero session/command/payload activity |
| clean SIGTERM | all 3 services, no crash, no hang |
| **tps** (`-t 2000 --debug`, warmed, warmup discarded) | **287.8 / 282.7 / 281.7** vs F7's **288.7 / 284.9 / 283.3** → −0.3% / −0.8% / −0.6%, **inside noise, NO REGRESSION** |

The no-regression result is what §24.1.2's G9 asked for and refused to assume: the decoder is now **one shift +
one mask** per CQE where it used to be up to four short-circuit predicates, and it measures as free.

#### ⚠ THE SELF-TEST WAS PROVEN BY A NEGATIVE TEST, NOT BY PASSING

A self-test that has only ever passed is not evidence. So it was **deliberately broken and re-run**: transposing
`STREAM_INDEX_SHIFT` (48) with `OWNER_SLOT_SHIFT` (24) — two fields of **equal width**, the exact case a
round-trip cannot see.

```
build:      CLEAN. Every _Static_assert passed (no width changed; still disjoint).
startup:    tuple-sink service: FATAL send WR-ID layout self-test FAILED class=payload:
            encoded 0x2104000302000501, expected 0x2102000304000501
            -- a shift or mask does not match the documented layout
exit code:  1  (the service REFUSED TO START)
```

That is the whole §24.7 thesis, demonstrated: **the compiler saw nothing, and only the golden literal saw it.**
Restored and re-validated afterwards.

#### ⚠ WHAT THE GATE DOES **NOT** EXERCISE (same shape as F7's caveat — do not overclaim)

The gate drives the **SQL** path, whose sends are `CRITICAL_CONTROL` and `COMMAND`. It therefore exercises:
- ✅ the CONTROL and COMMAND classes, the decode switch, all four posting guards, the untagged bootstrap send,
  the startup self-test, and the whole no-regression claim;
- ❌ **NOT the payload/ACK OWNER_SLOT + OWNER_INCARNATION path** — those retire on the tuple/COPY payload path,
  and backend-to-backend COPY is **broken at HEAD** (see `byte_ring_slot_capacity_regression.md`).

So the **incarnation check is validated by construction, by review, and by the negative test — NOT by
execution.** `ALARM=0` proves it did not *false-positive*; it does not prove it *fires*. Exactly like P0-b's
`RETIRING` branch (§22.10), this needs the COPY path back, or a fault-injection run.

#### Residual gaps, recorded rather than hidden

1. **Incarnation wrap (review R1).** Widened 8 → **16 bits** (bits 8-23; bits 0-7 were spare, so it was free)
   after the reviewer showed an 8-bit counter aliases after 256 reuses of one slot. **This narrows the window
   256×; it does not eliminate it.** A finite counter always wraps. The honest claim is: *the incarnation
   restores stale/duplicate detection for any fault that can occur under verbs semantics (which do not
   duplicate CQEs), and it does so WITHOUT reintroducing the absolute token's wrap ceiling on how much a stream
   may ever send.* It is **not** "at least" the old token's coverage in the pathological limit, and §24.6's
   first draft wrongly claimed it was.
2. **ACK's one-WR post helper discards `bad_wr`** and records every failure as zero-posted
   (`remote_execution_peer_transport_rdma.c`), so callers roll back. Under a **nonconforming provider** that
   accepts the WR and *then* returns failure with `bad_wr == NULL`, the ACK owner is freed while a CQE is still
   possible. Pre-existing, and the control path already recognises exactly that provider behaviour (P0-b,
   §22.9). **The new incarnation check now CATCHES the resulting stale CQE loudly** instead of mis-retiring —
   so §24 makes this failure mode visible rather than silent. Fixing the helper is P1 work.
3. **CONTROL response retirement ignores the slot generation** (validates only slot + in-use), while the
   request path validates it. Pre-existing; the WR-ID contract must not be read as implying generation protects
   BOTH control tables.
4. **The blocking wait does not run the payload failure probe** while the async drain does (§24.6). Diagnostic-
   only, `HOMER_DPU_P2_DIAG`-gated.

#### Rules earned

- **A static assert proves a field FITS; only a golden-literal round trip proves it is in the right PLACE.**
  And a round trip whose oracle is the implementation's own macros proves *nothing at all* — it shows the
  encoder and decoder agree, which is true for **any** shift assignment.
- **A test vector of all-ones cannot see a transposition.** Give every field a **different** value.
- **A startup gate must run BEFORE anything that can fail.** This self-test was first placed after transport
  init, where a routine `rdma_bind_addr` failure swallowed it — the gate silently never ran and the log looked
  normal. *A gate that only runs when everything else already worked is not a gate.*
- **A review that only confirms is a wasted review.** Round 3 refuted the load-bearing claim (C3), found two
  un-asserted table couplings (C4), and killed the first self-test outright (R4). Every one of those was a real
  defect heading for the tree.

### 24.9 ⚠ CORRECTION to §24.8 — the gate DOES exercise the payload owner-handle path. My caveat was WRONG.

§24.8 claimed *"the gate does NOT exercise the payload/ACK OWNER_SLOT + OWNER_INCARNATION path, because those
retire on the tuple/COPY path, and COPY is broken at HEAD."* **That is false, and it conflated two different
things: the broken COPY WORKLOAD, and the payload MACHINERY that workload happens to use.**

**The DPU result relay is a SECOND consumer of exactly that machinery, and it works.**
`HomerServicePumpOutgoingDpuMirrorByteRingPayload` (`tuple_sink_service_process.c:31394`) —- the farnet1-DPU →
farnet0-DPU result leg —- calls:

- `HomerServiceReservePayloadSendOwner` (`:31817`) → takes a slot, **bumps its incarnation**
- `HomerServiceAttachPayloadSendOwnerDpuMirror` (`:31820`) → `HOMER_PAYLOAD_SEND_OWNER_SOURCE_DPU_MIRROR`
- the payload byte-ring publish, whose **signalled tail WR carries the payload WR id** with `OWNER_SLOT` +
  `OWNER_INCARNATION` (`:31828`)

and the CQE comes back through `HomerServiceHandlePayloadSendCqeFromDrain` →
`HomerServiceResolvePayloadSendOwnerToken` → the slot-range / `POSTED` / incarnation checks →
`HomerServiceApplyTrackedPayloadCompletionFrontier`.

**And it MUST have run:** a DPU-spawned backend has **no host-shm result sink at all**, so its only egress is
the arena role-5 byte ring, drained by the DPU and relayed over peer RDMA. The five distinct `abalance` values
and the 2000/2000 could not have arrived any other way.

**The ACK table too.** The receiving DPU credits consumed-head via `HomerServicePostReceiverHeadAck` (`:33204`)
→ `HomerServiceTrackReceiverHeadAckOwner` (`:33260`), which is the **second** owner table —- the one selected by
the WR id's `KIND` field. So `RECEIVER_HEAD_ACK` WR-ids with an ACK-owner slot + incarnation were produced and
retired on the farnet0 DPU.

**So both owner tables, both handles, and both incarnation checks ran on real traffic across ~2000
transactions, and every payload CQE resolved to a POSTED owner with a matching incarnation.** Had ANY
resolution failed, the drain returns false → bound-payload protocol reset → the run would have ALARMed or lost
rows. It returned 2000/2000, 0 failed, ALARM=0.

**What is ACTUALLY still unexercised** is only the **rejection branches** (`named a RECYCLED owner slot`,
`named a non-POSTED owner`). Those are FAULT DETECTORS: by construction they do not fire in a healthy run, and
exercising them needs fault injection, not a workload. That is a normal and much weaker caveat than the one
§24.8 stated.

⚠ **Do NOT confuse this with P0-b's `retiring=0` gap (§22.10), which is a REAL coverage hole:** that is a
*normal-operation* branch that still never executes. This one is not.

**And the pgbench DPU result path is NOT broken.** What is broken at HEAD is the **Citus backend-to-backend COPY
workload** (`byte_ring_slot_capacity_regression.md`, "Problem 2") -— a different consumer of the same payload
machinery. Two distinct facts; §24.8 fused them into one wrong sentence.

**Rule earned (again, and this is the ninth time in this document):** *a forward chain from a mechanism I had
just read, with no check of what the OTHER callers of that mechanism actually do.* "COPY is broken" was true;
"therefore the payload path is untested" did not follow, and one grep for the other caller would have shown it.

---

## §27 — P0-c IMPLEMENTED (staging pools). Two latent bugs found; the review found a THIRD that was MINE.

**Status: implemented 2026-07-12, reviewed twice, awaiting validation.** Transport-only —- the service already
had correct `WOULD_BLOCK` / `FAILED_PARTIAL` handling, so nothing there needed to change for the mechanism.

### 27.1 What shipped

| pool | treatment |
|---|---|
| **TAIL** (`payloadPublishTailBuffers[128]`) | reserve/retire **frontier** (`payloadPublishTailReserved` / `...Retired`); reserve returns **`WOULD_BLOCK`** at capacity and **does NOT drain internally** (the owner drains; the reserver reports); retired by the CQE's **`RESERVED_TAIL_SLOTS`** — §24 already encodes it, so **the completion says what it frees** and a two-counter frontier suffices. |
| **HEADER** (`payloadFragmentHeaderBuffers[1024]`) | **ROLLBACK ONLY**, no frontier (§26.3). |
| both | roll back **iff ZERO WRs posted**; a **PARTIAL** post **retains** (those WRs are live on those slots) and forces a reset, which zeroes the pools after QP/CQ teardown (**verified**, `:6425`). |

A first-time **one-shot announcement** fires when a lane first hits tail-pool backpressure: before P0-c that
boundary was not a state, it was where the corruption began. *Silence must not be a valid state; spam must not
be the price of saying so.*

### 27.2 ⚠ TWO LATENT BUGS FOUND WHILE IMPLEMENTING — both primed to detonate in THIS stage

1. **The fixed-slot builder reserves NO tail slot** (it re-tags the last payload write as the signalled
   `WRITE_WITH_IMM`) — but **§24 had me thread `TAIL_SLOTS_PER_BATCH = 1` into BOTH encoders.** Its completion
   therefore claimed to free a slot it never took. Harmless *only* while nothing read the field —- and **P0-c is
   what starts reading it**: the frontier would **OVER-ADVANCE** and hand out slots that are still live, which
   is precisely the corruption P0-c exists to prevent. Now encodes **0**.
   **Rule: a WR id must describe the WR that carries it, not the shape of its sibling.**
2. **The unsignalled receiver-head ACK** reserved a tail slot but posted an **UNTAGGED** WR id → **no CQE** →
   the slot could never be retired. Unretirable by construction; also a straight violation of P0-i (an
   unsignalled WR with no guaranteed signalled successor). **Now refused outright.**

### 27.3 ⚠ THE REVIEW FOUND A THIRD, AND IT WAS THE WORST ONE

> **The byte-ring publisher's ZERO-WR `ibv_post_send` failure path had NO ROLLBACK.**

That path is **`ENOMEM` on a full send queue** —- *the exact `WOULD_BLOCK` retry storm P0-c exists to fix.*
Without the rollback the cursors advance with no WR behind them; after 128 such failures the tail pool is
permanently "full" and **the lane wedges forever.**

**P0-c would have shipped a fresh instance of the very bug it was written to remove.**

**Root cause of the miss, and it is the lesson:** my patch script's `.replace()` matched
`"ibv_post_send(payload byte-ring) failed"` while the real literal is `"...byte-ring **publish**) failed"`. Every
*other* replacement in that script asserted its match count. **That one did not.** So it silently no-op'd and
**the build stayed green.** A second script then asserted *after* the writes it had already reported "OK",
aborting before the file was written at all.

> **A CLEAN BUILD IS NOT EVIDENCE THAT YOUR EDIT LANDED.** Every fix in this stage was subsequently **proven by
> reading the code back**, not by a successful compile. (This is the same failure family as the July-12
> `make | tail` script that printed `DPU BUILD OK` over `make: *** Error 1`.)

### 27.4 The review's OTHER findings, all accepted

- ⚠ **The header pool's capacity check was OFF BY ONE BATCH.** I checked `granted_max_send_wr < 1024`. But **a
  batch RESERVES AND WRITES up to `PAYLOAD_BATCH_MAX_WRITES` (32) headers BEFORE it posts**, so the
  in-construction batch is on the cursor too. With **993** accepted-but-unretired WRs against a 1024-slot pool,
  the next batch's 32 headers **wrap onto a live slot and overwrite it** —- *before* the inevitable full-SQ
  failure can roll them back. **A rollback that has not happened yet cannot save a slot that has already been
  scribbled on.** Correct bound, now enforced both statically and at QP creation:
  `granted_send_wr + PAYLOAD_BATCH_MAX_WRITES <= FRAGMENT_HEADER_SLOTS`.
- ⚠ **And the check had to exist at all** because the proof rested on the send-queue depth we **REQUESTED**.
  `rdma_create_qp()` writes the **GRANTED** caps back into `qpInitAttr.cap`, and the code read back only
  `max_inline_data` (`:5410`). A provider may grant **more**. The connection is now **refused** (loud ALARM) if
  the granted depth could outrun the pool. The requested depth (256 + 32 vs 1024) has ample headroom; the check
  exists for the provider that grants more than we asked.
- **The inline-disabled retry reused `qpInitAttr` after a FAILED create.** The struct's contents are only
  guaranteed on success —- and we now make a *correctness* decision out of those fields. All requested caps are
  re-stated before the retry.
- **Rollback no longer depends on `postResult` being non-NULL** (it is an optional out-param; correctness must
  never ride on whether the caller wanted a report). The posted count is recomputed from the same helper
  `MarkPeerPostFailure` uses, and neither the WR chain nor `bad_wr` is mutated in between.
- **`WOULD_BLOCK` is now an EXPECTED state, so it is no longer logged as a publish failure** —- at **six** hot
  publish/credit-ACK sites, not the one I first found. Per-occurrence logging there would spam under legitimate
  saturation and depress the very throughput being measured.

### 27.5 ⚠ A LANDED-PROOF FALSE-NEGATIVE (new operational hazard)

The DPU deploy check **failed on a good binary**. The DPU's system `pg_config` carries **`-flto=auto`**; LTO let
GCC see across translation units, **prove `signaled` is always true, and DELETE the unsignalled-ACK refusal
branch** —- including its strings. Our host build (no LTO) keeps them.

- The compiler thereby **independently confirmed the invariant** I claimed. Nice.
- But **a string-grep landed-proof FALSE-NEGATIVES on code the compiler can prove dead**, and would have blocked
  a correct deploy. **Key landed-proofs on strings the compiler cannot eliminate.**

### 27.6 Rules earned

- **A capacity bound must count what is IN CONSTRUCTION, not only what is ACCEPTED.** The slot is written before
  the post; the rollback comes after it.
- **Never make a correctness decision out of a struct the API only fills in on SUCCESS.**
- **A WR id must describe the WR that carries it, not the shape of its sibling.**
- **A clean build is not evidence your edit landed. Read it back.**

---

## §28 — P0-c v2: THE FRONTIER IS DELETED. Size the pools out of the problem instead.

**Owner's call, 2026-07-12, after v1 was fully built, reviewed twice, and validated on the gate.** v1 is
recorded in §27 and is NOT what shipped. Both sides are here because the reasoning is the point.

### 28.1 The question that killed v1

> *"If we make sure the staging pool is as large as the NIC's send queue, then we won't ever hit a
> staging-pool-full situation — only a NIC-queue-full situation, right?"*

Right in spirit, and it exposed two things.

**First, the numbers were the other way round.** The send queue is **256**; the tail pool was **128** — *half*.
It looked sufficient because a byte-ring batch is **≥ 2 WRs** (≥1 body + 1 signalled tail) but consumes only
**1 tail slot**, so 256 WRs ÷ 2 = 128 batches. Exactly coincident, zero margin.

⚠ **But the ACK path breaks that 1:2 ratio.** A receiver-head ACK is **1 WR consuming 1 tail slot** — **1:1**.
With `A` ACKs and `B` batches: send-queue usage ≥ `A + 2B`, tail usage = `A + B`. At `A = 128, B = 0` **the tail
pool is FULL while the send queue is HALF EMPTY.** ACKs are capped at 64 *per stream*, so two streams sharing a
lane get there. **The pool genuinely could bind before the NIC** — which is exactly what should not happen.

### 28.2 ⚠ THE BOUND — and BOTH of my first two attempts at it were wrong, in OPPOSITE directions

**Attempt 1 (mine): "size the pool to the send-queue depth."** Too small — and for the wrong reason, which I
only found by trying to write the assert. A staging slot is not freed by anything software does; I reasoned
about *observability* instead of *physics* and reached for a CQ term.

**Attempt 2 (mine): "the bound is SQ + CQ, because a slot is live until its CQE is DRAINED."** ⚠ **REFUTED by
the review, and it was wrong twice over:**

- **Nothing retires a staging slot any more.** There is no frontier. The CQE is merely how software would
  *learn* of a completion — **and we never ask.** So "live until drained" describes a mechanism that does not
  exist.
- **If it HAD been true, SQ + CQ would not even be a bound** — one undrained signalled CQE can stand for **32**
  earlier header-bearing unsignalled WRs.
- And it is **actively harmful**: `ibv_create_cq()` may **round the CQ up** far above what we asked, so a
  setup-time check on SQ + CQ would **REFUSE perfectly good providers** for no reason.

**THE CORRECT BOUND — and it needs nothing from the CQ:**

> A slot is unsafe only while the NIC may still be **READING** it, i.e. while its WR has not completed. Slot `S`
> is handed out again only after `POOL` more reservations, and — **thanks to the rollback** — every one of those
> is backed by a **POSTED** WR. You cannot have more than the granted send-queue depth of WRs posted-and-
> uncompleted. So once `POOL > SQ_DEPTH` further WRs have been posted, the WR that was using `S` **must** have
> completed; an RC QP completes **in order**, so it is the oldest ones that have. The NIC is done with `S`.

That argument holds under **every** provider model — it does not care whether a send-queue entry is reclaimed at
completion or only when a later signalled CQE is polled (mlx5 does the latter), because **you cannot POST past
the send queue without earlier WRs having completed**, which is the only thing being claimed.

**THE RULE (compile-time asserted for both pools):**

```
POOL_SLOTS  >  SEND_QUEUE_DEPTH + max_slots_ONE_ATTEMPT_reserves
```
| pool | bound | size |
|---|---|---|
| tail | 256 + **1** = 257 | **512** (grown from 128) |
| header | 256 + **32** = 288 | 1024 (unchanged — it was *accidentally* right) |

⚠ **The last term is real**: an attempt RESERVES AND WRITES its slots **before** it posts. *A rollback that has
not happened yet cannot save a slot that has already been scribbled on.*

⚠ **AND THE PREMISE IS CHECKED, NOT ASSUMED.** `SEND_QUEUE_DEPTH` is what we **REQUEST**. `rdma_create_qp()`
writes the **GRANTED** caps back into `qpInitAttr` — we already read `max_inline_data` out of it and **never
looked at `max_send_wr`**. Connection setup now re-checks the rule against the **granted `cap.max_send_wr`** and
**REFUSES the connection** if a provider hands us a deeper send queue than the pools can cover.

### 28.3 THE DECISIVE ARGUMENT FOR DELETING THE FRONTIER

Not "it is simpler". This:

> **THE FRONTIER CREATES THE VERY FAILURE MODE ITS ALARM GUARDS AGAINST.**

A frontier must be **TOLD** what each completion frees — so the WR id had to carry `RESERVED_TAIL_SLOTS`. **And a
WR id that has to SAY what it frees can LIE about it.** One did: §24 threaded the count into **both** payload
encoders, but the fixed-slot builder **reserves no tail slot at all** (it re-tags the last body write as the
signalled `WRITE_WITH_IMM`). Its completion claimed to free a slot it never took → the frontier would
**OVER-ADVANCE** and hand out **live** slots: *precisely the corruption P0-c exists to prevent.*

Sizing the pools out of the problem deletes, in one move:
- the reserve/retire frontier and its retirement wiring,
- **`RESERVED_TAIL_SLOTS` from the WR id** (bits 0-7 are free again; the golden self-test constants were
  recomputed and still pass),
- the `WOULD_BLOCK` backpressure plumbing through all three publishers,
- the divergence between the two pools — **both are now `rollback + capacity`, one rule**,
- **and that entire bug class.**

**What survives is the part that was always load-bearing: ROLLBACK.** The frontier was scaffolding around it.

**Rule earned:** *the safest field is the one that does not exist. If a mechanism requires a completion to
describe itself, ask first whether the mechanism can be sized away.*

### 28.4 What v1 was still worth

v1 was not wasted — it is what **found** the two latent bugs (§27.2) and the missing byte-ring rollback (§27.3),
and it is what forced the sizing question to be asked precisely enough to be answered. **The bug it exposed in
the fixed-slot encoder is the reason v2 exists.**

### 28.5 The one thing that did NOT survive the change: the runtime self-check

v1's frontier ALARMed on over-advance. v2 has no such runtime check — it has a **compile-time assert** plus a
**setup-time refusal**. That is a deliberate trade: fewer premises to violate, and the two that remain are both
mechanically enforced rather than reasoned about.

### 28.6 ⚠ THE REVIEW FOUND A SHIP-BLOCKER — AND IT WAS A §24 BUG, NOT A P0-c ONE

> **`HomerPeerDecodeSendWrId` truncated the 16-bit `OWNER_INCARNATION` to 8 bits with a stray `(uint8_t)` cast.**

The mask is `0xffff`, the decoded struct member is `uint16_t`, the service's live incarnations are `uint16_t` —
but the decoder cast to `uint8_t`. **From incarnation 256 onward, every legitimate payload CQE decodes an
incarnation of 0**, `HomerServiceResolvePayloadSendOwnerToken` rejects it as *"named a RECYCLED owner slot"*, and
the connection resets. It fires after **256 reuses of any single owner slot** — invisible in the gate's short
runs, fatal in a long COPY or basebackup.

It was introduced when §24.6 widened the incarnation 8 → 16 bits (a `.replace()` that updated the struct, the
mask, and the encoder, but silently missed the decoder's cast).

#### ⚠ AND MY OWN GOLDEN SELF-TEST DID NOT CATCH IT. That is the more useful lesson.

§24.7 claimed the golden-literal self-test proves the layout. **It proved half of it.**

- The **distinct-value** vector encodes *and* decodes — but all its values are small (`1/2/3/4/5`), so an 8-bit
  truncation of a 16-bit field is invisible.
- The **all-max boundary** vector checked **only the ENCODED integer** and **never decoded it**. So a
  **decode-side** truncation walked straight through the test built to prove the layout.

**Fixed:** the boundary vector now decodes the max-field id and verifies **every field**. Proven by the same
negative test as §24: put the `(uint8_t)` cast back, and the build is clean, every `_Static_assert` passes, and
the service **refuses to start** —
`FATAL ... the max-field id 0x27ffbeefffffff00 does not DECODE back to its maximum fields -- a decoder cast or
mask TRUNCATES`.

**RULE EARNED (the second half of §24.7):**
> **A layout test must exercise BOTH DIRECTIONS at the BOUNDARY.** One direction proves half a layout. And a
> distinct-value vector cannot see a truncation that a max-value vector would — you need both, in both
> directions.

### 28.7 The other review findings

- ⚠ **A PARTIAL POST *MUST* be followed by a connection reset — the capacity proof DEPENDS on it.** On a partial
  post the **rejected suffix's** reservations are retained too (we cannot tell which belonged to accepted WRs),
  so the cursor advances for slots **no WR is reading** — the very condition rollback exists to prevent. Bounded
  to one batch, but it **accumulates** if a connection keeps partially posting. Every in-tree caller resets
  (`HomerServiceHandlePayloadBatchPostFailure` maps `FAILED_PARTIAL` → reset). **Now stated in the code as a
  contract rather than left as caller etiquette; enforcing it inside the transport is P1.**
- **Memory.** The pools are embedded per connection, and a transport state holds 64 outgoing + 64 incoming
  connections. The 1024-slot draft would have added ~900 KB per transport state; the corrected bound lets the
  tail pool be **512** (4 KB/connection), halving that. Heap-allocated; not a stack concern.
- **T5 clean:** no reader of `RESERVED_TAIL_SLOTS` remains anywhere in either tree.

### 28.8 ✅ LANDED + VALIDATED — citus `2836bc426` (2026-07-12)

Deployed to both hosts and **both DPUs**, then run through **the DPU gate**
(`pgbench --homer --homer-dpu-command`: farnet0 host → farnet0 DPU → DPU↔DPU RDMA → farnet1 DPU →
DPU-spawned backend). **PASS.**

| criterion | result |
|---|---|
| `ALARM` in ANY service log (host + both DPUs) | **0** — the single most important one: the new rollback/incarnation code ALARMs rather than aborting, so an ALARM is a silent failure only the log shows |
| `RECYCLED owner slot` / `refused` / `connection reset` / `post failed` / `WOULD_BLOCK` in either DPU log | **none** |
| `send WR-ID layout self-test PASSED` | exactly **once per service start**, all 3 services |
| `transport:` line | `homer-dpu-command (implies dpu result relay)` on every run — never a bare `homer` |
| `DPU backend spawn begin` / `COMPLETED … launched_pid=N` on the **farnet1 DPU** | present (5 spawns) — the anti-topology-trap proof |
| farnet0 host service log | startup banner **only**; farnet1 host service **down** |
| transactions | **5/5** and **2000/2000**, **0 failed**, distinct `abalance` values |

⚠ **The `-t 2000` leg is the point of this validation, not a throughput exercise:** it is what drives
`OWNER_INCARNATION` **past 256**, which is exactly where §28.6's truncation bug would have begun rejecting
every legitimate payload CQE as a recycled slot and resetting the connection. A `-t 5` run cannot see it.

**Performance, warmed steady state, `-t 2000 --debug`** (like-for-like against §24's band of
**287.8 / 282.7 / 281.7**; warmup 283.74 discarded):

**280.84 / 281.82 / 280.90 tps** — mean ≈ −1.0%, within noise. **No regression.**

**Two operational findings from the deploy** (folded into `farnet_operational_hazards.md`):
- farnet0's source trees had drifted to `jasonhu`-owned (postgres-citus) and ~1100 mixed-owner files
  (citus-dbcomm), which made the standing rsync recipe fail **code 23** — the exact
  *"leaves files untransferred without saying which"* trap. Normalized with `chown -R dbcomm:dbcomm` and
  re-synced; proven by a per-file md5 diff (**0 differing files**).
- `dbcomm` has **no working SSH key to farnet0**, so the source rsyncs must run as the calling user with
  `--rsync-path='sudo -n -u dbcomm rsync'` (receiving side only) — which is what the runbook recipe already
  says, and now we know *why* it is written that way.

---

## §29 — NEW P0-LEVEL FINDING: the SEND QUEUE has no per-LANE admission control

**Found 2026-07-12 while explaining P0-c's partial-post dependency.**

> ### ✅ BOTH PARTS LANDED (2026-07-12) — this section's "needs an owner decision" is HISTORY, not status.
> - **§29(a)** — the `_Static_assert` pinning the unwritten inequality: citus **`c3e3f3e56`**
>   ("pin two load-bearing premises that nothing was enforcing").
> - **§29(b)** — the per-connection outstanding-WR counter + pre-post admission check, via cumulative
>   `postedSendWrs` / `retiredSendWrs` frontiers: citus **`e85abe531`**.
>   See `remote_execution_peer_transport_rdma.c:1036`-`:1044` (the frontiers), `:10756` (admission),
>   `:10815` (post), `:10843` (retire at the signalled checkpoint).
>
> The owner question *"is (b) P0, or P7b's first step?"* was answered by doing it: **P0.**

### 29.1 The mismatch — and it is the SAME one §23 already found, on a different resource

| | bound | scope |
|---|---|---|
| RDMA send queue | **256 WRs** | **per CONNECTION (per QP / per lane)** |
| the only outstanding-WR cap | `HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS` = **192** (`tuple_sink_service_process.c:335`) | **per STREAM** |
| streams that may share one lane | up to **64** (`CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS`) | — |

**The transport tracks NO per-connection outstanding-WR count.** (Verified: no such counter exists.)

So **two** concurrently-publishing streams on one lane permit `2 x 192 = 384` outstanding WRs against a
**256**-entry send queue.

⚠ **This is precisely the mismatch §23.5's F2 identified for the tail staging pool** — *"the pool is per-LANE
while the flow control that bounds it is per-STREAM"* — **applied to the send queue itself.** We fixed the pool
and left the queue.

### 29.2 Why it matters: the failure mode is a PARTIAL POST, and a partial post costs a CONNECTION RESET

`ibv_post_send()` takes a **chain** (N unsignalled body WRs + 1 signalled tail). The provider pushes WRs into the
send queue one at a time; when it runs out of room **mid-chain** it posts the prefix, sets `bad_wr`, and returns
`ENOMEM`. That is `HOMER_PEER_POST_FAILED_PARTIAL`.

A partial post cannot be undone (verbs has no per-WR cancel), the accepted prefix is live and DMA-reading our
registered source buffers, and the batch is a semantic unit (bytes + the doorbell that commits them). **The only
way to stop the NIC reading is to DESTROY THE QP** — i.e. a full peer-connection reset.

**So under multi-stream load, a routine resource condition triggers a full connection teardown on the hot path.**

⚠ **And P0-c now DEPENDS on that reset** (§28.7): on a partial post the rejected suffix's staging reservations
are retained, advancing the cursor with no WR behind it — bounded to one batch, but it accumulates without the
reset. So the hammer is not optional.

### 29.3 Why it has never bitten — and exactly when it will

Every workload we run today has **ONE** payload stream per lane (the DPU gate; the 4-role basebackup). It becomes
reachable the moment there are **two**:

- **multi-shard COPY** (when it is re-plumbed onto the DPU plane), and
- ⚠ **P7b CONNECTION POOLING — the original goal this entire P0 sequence exists to unblock.** Pooling multiplies
  streams per lane by construction.

### 29.4 The fix (proposed, NOT implemented — owner decision)

**Admission control on the send queue:** track outstanding WRs per connection, and refuse a batch that does not
fit **BEFORE** posting it — return `WOULD_BLOCK` (retry later, no reset) instead of discovering the limit as a
partial post (reset). That makes the send queue an **explicit** backpressure source rather than an implicit
failure mode, which is where backpressure belongs.

**What it needs:**
- a per-connection `outstandingSendWrs` counter, incremented by `postedWrCount` and decremented on CQE
  retirement — ⚠ but unsignalled WRs produce no CQE, so the decrement must be attributed at the **signalled
  checkpoint** (the batch's tail CQE retires the whole chain — P0-i's rule again);
- a pre-post check: `outstanding + chainLength <= grantedSendWr` else `WOULD_BLOCK`;
- the service already handles `WOULD_BLOCK` correctly (retry, no reset), so no service change.

**Cost:** one counter and one compare on the publish path.
**Benefit:** partial posts become (near-)unreachable, which removes a connection reset from the hot path AND
removes P0-c's dependency on that reset.

**Open question for the owner:** is this P0 (it blocks P7b, and P7b is the goal), or is it P7b's own first step?

### 29.5 OWNER CHALLENGE (2026-07-12): *"we have no chain — it's one at a time"*. REFUTED, but it sharpened the finding.

The owner pushed back on §29.2 twice, and both challenges were worth running down.

**Challenge 1 — *"I believe we don't have a chain. It's always one at a time, no?"*** — **HALF RIGHT.**
There are exactly four `ibv_post_send` call sites:

| site | tag | shape |
|---|---|---|
| `remote_execution_peer_transport_rdma.c:6010` | `generic-rdma-write` | **single WR** (scalar `&sendWorkRequest`) |
| `remote_execution_peer_transport_rdma.c:10769` | `control-wimm` | **single WR** |
| `remote_execution_peer_transport_rdma.c:11002` | `payload-batch` (fixed-slot / host-service arm) | **CHAIN**, `totalWorkRequests = payloadWriteCount` (`:10811`), 1..32 |
| `remote_execution_peer_transport_rdma.c:11296` | `payload byte-ring publish` (**DPU arm**) | **CHAIN**, `totalWorkRequests = payloadWriteCount + 1` (`:11091`), **2..33** |

On the **control plane the owner is exactly right**: one WR per post, `bad_wr` can only be the WR handed in,
`HOMER_PEER_POST_FAILED_PARTIAL` is structurally unreachable. **Both payload paths chain** (`:11247` links the
bodies, `:11288` terminates with the signalled tail, `:11296` posts the whole list in ONE call).

**Challenge 2 — *"chaining is a design scar from when a record could wrap the ring; we now leave a gap and
don't wrap, so we're not actively chaining anywhere on the DPU path."*** — **REFUTED. Chaining is active,
unavoidable, and is not what the owner remembers it being.**

1. **The byte-ring publish is a chain of ≥ 2, ALWAYS.** `payloadWriteCount == 0` is an early-out
   (`tuple_sink_service_process.c:31733`), and the tail WR **cannot be merged into the last body write** —
   it targets a *different remote address* (the ring's tail/frontier word at `tailRemoteAddress`, not
   `peerStorage + offset`). So `payloadWriteCount + 1 >= 2` on **every single publish**. It is never one WR.
   *(The fixed-slot arm CAN be a single WR — it re-tags its last body write as the `WRITE_WITH_IMM`. But that
   is the dying host-service arm.)*
2. **The ceiling of 32 is a default, not an accident:** `HomerServiceBuildDefaultPayloadEgressGrant`
   (`tuple_sink_service_process.c:7055`) sets `transportGrant.maxWrCount = CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES`
   = **32**, and the build loop `while (payloadWriteCount < grantMaxWrCount)` (`:31635`) runs to that budget.
3. **The record-wrap protocol change removes NONE of the slicers.** `HomerByteRingRelayNextChunk`
   (`tuple_sink_service_process.c:7796`) takes the **minimum of four** independent limits:
   distance to the **destination ring wrap** · remote credit · the scheduler pacing cap (`grantMaxBytes`) ·
   the substrate per-op cap (`UINT32_MAX` for RDMA). All four are live.

   ⚠ **Why the wrap slice survives "records never wrap":** *this relay is byte-oriented, not record-oriented,
   deliberately.* Its own doc comment (`:7782`) says so: *"There is deliberately NO record/header awareness
   here: whole records (with their transport headers **and any producer wrap-gap padding**) are carried
   byte-identically."* The gap-don't-wrap policy guarantees no single **record** straddles the physical wrap —
   but the relay does not move a record, it moves a **contiguous absolute byte range spanning many records**,
   and *that* crosses the ring boundary routinely. (`ringBytes=1024`; records 900–1000, gap 1000–1024, records
   1024–1100; relaying `[900,1100)` must become two writes, at remote offsets 900 and 0.) **The wrap slice
   fires once per ring lap on every stream that moves more than a ring's worth of bytes — i.e. all of them.**

**What the owner IS right about, and it is the useful part: chaining is NOT semantically load-bearing.**
The call-site comment (`tuple_sink_service_process.c:31615`) claims the chain is what makes "bytes before
doorbell" safe. That is **imprecise**: on an RC QP, WRs execute **in order regardless of how they were
posted** — N separate `ibv_post_send` calls on the same QP give the identical guarantee. The chain is a
**performance** optimization (one doorbell MMIO instead of N), not a correctness mechanism.
⚠ But **unrolling it would not fix §29**: if post #3 of 5 returns `ENOMEM`, the torn state is identical, just
discovered one WR earlier. The fix is the same either way — **check before you post.**

### 29.6 The REAL reason it has never torn: an UNASSERTED ARITHMETIC COINCIDENCE across two files

§29.3 said "one stream per lane". That is true but is not the *mechanism*. The mechanism is three constants in
**two different translation units** with **no static relationship between them**:

```
CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH          256   remote_execution_peer_transport_rdma.c:101
HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS        192   tuple_sink_service_process.c:335       (per STREAM)
CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES   32   remote_execution_peer_transport_rdma.h:54   (+1 tail = 33)
```

**ONE stream:** ≤ 192 outstanding on a 256-deep SQ ⇒ **≥ 64 entries always free**, and 64 ≥ 33 ⇒ **the chain
always fits; partial post is UNREACHABLE.** **TWO streams:** 384 wanted vs 256 ⇒ free space can fall below 33
⇒ **the chain tears.**

> ⚠ **The margin is 64 vs 33 and NOTHING ENFORCES IT. Raise `BATCH_MAX_WRITES` from 32 to 64 and a partial post
> becomes reachable with a SINGLE stream, today, with no other change — and the build stays green.** The
> correctness of the *shipping* path rests on an unwritten inequality that a plausible tuning change silently
> breaks. §29 is therefore not only a latent P7b problem.

### 29.7 The fix is CHEAPER than §29.4 said: it is an EXISTING predicate at the WRONG SCOPE

The per-**stream** path **already performs exactly the admission check §29.4 proposes** —
`tuple_sink_service_process.c:31562`:

```c
streamEntry->stream.payloadSendOwnerOutstandingWrs + grantMaxWrCount + 1 >
    HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS   /* 192 */
```

It correctly reserves for the **whole worst-case chain including the tail** (`+ grantMaxWrCount + 1`), *before*
building the batch. **This is not missing machinery — it is the right predicate at the wrong scope.** §29 is
that same check hoisted to the connection's send queue. That materially strengthens the case for doing it now
rather than deferring to P7b: it is a copy of an existing, already-correct pattern, not new design.

**Two parts, and part (a) should land regardless of the sequencing decision:**

**(a) IMMEDIATE, CHEAP.** Hoist the per-stream cap next to the queue depth and pin the inequality so the
compiler owns it:

```c
_Static_assert(HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS
               + CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES + 1U
               < CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH,
               "one payload stream's outstanding WRs plus one maximal chain must fit the send queue, or "
               "ibv_post_send() tears mid-chain -> PARTIAL POST -> QP destroy -> hot-path connection reset");
```

This converts *"we got lucky"* into *"the compiler enforces it"*, and makes the real fix an explicit,
deliberate relaxation rather than an accident waiting to be re-broken.

**(b) THE REAL FIX** — per-connection outstanding-WR counter + pre-post admission check, exactly as §29.4
describes. ⚠ Decrement attribution is the one subtlety: the body WRs are UNSIGNALLED and produce **no CQE**, so
the counter cannot be decremented per-completion. It must be released at the **signalled checkpoint** — the
chain's tail CQE retires the whole chain's count. That is P0-i's rule again (defer retirement to a later
signalled WR only when that WR is GUARANTEED — and here it is: same chain, same post).

**Still open for the owner:** is (b) P0, or P7b's first step?

---

## §30 — THE 4-ROLE BASEBACKUP WAS BROKEN AT OPEN FOR TWO DAYS. Root cause: the negotiated geometry had no home.

**Regression introduced by citus `4382b65d65` (2026-07-11). Found 2026-07-13. Fixed in citus `ffe2c4ac9`.**

### 30.1 What broke, and why nobody saw it

`4382b65d65` is **the commit that made the DPU gate green** (P3, "SQL result relay GREEN end-to-end"). It added
**three structurally identical byte-ring geometry checks** — peer-open, local-open, async-open — and **each picked
a queue BY NAME.** Two picked `receiveQueue`. A **basebackup stream never provisions `receiveQueue`**
(`HomerServiceCreatePayloadStreamEntry` gates it on `!IsBaseBackup`, `tuple_sink_service_process.c:25758`), so both
compared **ZERO** against the requested `524288` and failed deterministically, on the first open, before a byte
moved — **in BOTH roles**:

| role | check | message |
|---|---|---|
| sender (`pg_basebackup -t 'homer:...'`) | peer-open | `peer open request parameters mismatched existing sink` |
| consumer (`pg_basebackup --homer-receive`) | local RECEIVE open | `open request parameters mismatched existing sink` |

⚠ **It went unnoticed for a DAY because the DPU gate — the only workload re-run — touches neither path.** The
commit's own subject line reads *"one rule, violated in both directions."* It was.

### 30.2 The root cause is NOT "a wrong field". It is that GEOMETRY HAD NO HOME.

The negotiated ring geometry lived **incidentally inside a queue object**, so three checks each had to **guess
which queue held it** — and they guessed by **NAME**, in a scheme where the names describe **DIRECTION** while the
contents describe **OWNERSHIP**:

| object | what it ACTUALLY is |
|---|---|
| `stream.sendQueue` | **PRODUCER-owned** ring. Mapped for **EVERY** byte-ring stream, on the sending **and the receiving** service (`:25732`). On a receiver it carries **no data** — but it does carry the **geometry**. Hence canonical. |
| `stream.receiveQueue` | **RECEIVER-owned**: a ring a **LOCAL BACKEND PROCESS polls**. ⚠ Not created for basebackup, which has no such backend — the DPU relays onward. **ZERO on a basebackup stream.** |
| `stream.localReceiveByteRing` | The **peer-facing inbound RDMA LANDING ring**. A separate object (must live in DPU landing memory, registered as the peer-writable MR). It merely **ALIASES** `receiveQueue` on the host tuple path (`:17762`). **NOT redundant with it.** |

**The model is SOUND and basebackup is NOT missing a ring** (reviewed and confirmed). *The names are the trap.*

### 30.3 The fix: give geometry ONE home, and route EVERY check through it

`HomerServiceStreamNegotiatedByteRingBytes()` (`tuple_sink_service_process.c:4339`) is now the single source of
truth. **All three checks route through it** (`:28115`, `:37318`, `:38783`) — **including the one that was already
correct**, because leaving *any* of them reading a queue member directly is exactly what let the other two drift
apart. Zero direct queue reads remain in any geometry check; the three are now textually identical and cannot get
inconsistent again.

> **A one-line fix was NOT enough, and I nearly shipped one.** I read the *sender's* error, found a real bug, fixed
> it, and stopped — never asking why the *consumer* also failed, **with a different message**. The second check was
> live and on the critical path. **Stopping at the first cause that explains the symptom you happened to look at is
> not root-causing.** Ask what ELSE failed, and whether it failed the same way.

### 30.4 Deeper: the eager allocation is dead storage, and there is a FOURTH geometry-carrier read

- On a **DPU tuple-receive** stream, `receiveQueue` is allocated, *validated as mapped* (`:17735`), and then
  **never used as a data path** — the aliasing is deliberately skipped when the DPU DMA engine is active (`:17760`),
  because the worker backend lives on the **host** and cannot map the **DPU's** shm. Its only surviving purpose is
  `ringBytes = streamEntry->stream.receiveQueue.byteRingBytes;` at **`:17745`** — *a fourth site reading the
  geometry out of whichever queue was handy.* Same disease, one layer down. Safe today only because that branch is
  guarded and basebackup does not reach it. **Route it through the accessor, then the DPU's `receiveQueue`
  allocation is provably dead and can go.**
- **The rename is worth doing** (`sendQueue` → `localProducerSourceQueue`, `receiveQueue` →
  `localConsumerDeliveryQueue`, `localReceiveByteRing` → `peerInboundLandingRing`; ~270 sites) but it is now
  **cosmetic, not load-bearing** — the accessor already removed the ambiguity.
  ⚠ **If you use clangd to do it: clangd indexes ONLY the active build config.** Every reference inside
  `#if HOMER_SERVICE_PAYLOAD_STATS` / `HOMER_DPU_P2_DIAG` will be **silently skipped**, compile fine, and break only
  the *diagnostic* build. Cross-check with a textual grep, and **build the diagnostic configs to prove it**.

### 30.5 ✅ VALIDATED (2026-07-13, citus `ffe2c4ac9`)

4-role DPU-relay basebackup, run **twice**: both **OPEN and COMPLETE**. Zero occurrences of either mismatch string.
`delivered_bytes` = **23,245,076,093** and **23,245,102,717**; both closed with `CLOSE_ACK`.

DPU gate: transport line correct, DPU backend spawn begin/COMPLETED, anti-fallback clean, 5/5 and 2000/2000 with 0
failed. Warmed `-t 2000 --debug`: **292.66 / 286.89 / 285.22** tps vs the like-for-like band 284.51 / 280.97 /
278.65. No regression.

### 30.6 THE VALIDATION LESSON — the gate and the basebackup prove DIFFERENT things

⚠ **A green gate says NOTHING about the ring wrap.** The gate moves one tiny result at a time, so its ready range
is ~one record and it almost certainly **never takes the 2-write wrap path**. The basebackup pushes **21.6 GiB
through a 512 KiB ring = ~44,000 LAPS** — it is the **only** workload that exercises the wrap, and therefore the
only one that can validate a byte-relay change (§28/§31). *That* is why `4382b65d65` could break it invisibly, and
why the byte-relay WR-budget deletion could not be validated until it was fixed. **Run both. Neither substitutes
for the other.**

---

## §31 — IMPLEMENTED + VALIDATED: give the negotiated geometry a real home, and stop allocating two DEAD POSIX SHM RINGS per stream on the DPU

**IMPLEMENTED + VALIDATED 2026-07-15 (gate + 4-role basebackup, both — see §31.8). Scoped 2026-07-13, out of the
§30 post-mortem. Deliberately NOT bundled into §29(b) — it touches stream creation and needs its own gate +
basebackup validation. Field removal (§31.3 step 4) still deferred to S7.1.**

### 31.1 The finding: on the DPU, BOTH shm queues are allocated and NEVER USED

`HomerServiceMapProducerByteRingQueue` really does `shm_open()` (`tuple_sink_service_process.c:23765`).  And on a
**selected-DPU** service, **neither** POSIX shm queue is on any data path — for **either** workload:

- **`sendQueue`** is mapped for **EVERY** byte-ring stream, on the sending **and** the receiving service
  (`:25732`).  But selected-DPU egress is **mirror-only**: `HomerServicePumpOutgoingPayloadStream` forks to
  `HomerServicePumpOutgoingDpuMirrorByteRingPayload` when a DPU DMA engine is present (`:32251`), and its own
  comment says falling through to the `sendQueue` pump would be wrong (`:32275`).  **So even the basebackup
  receiver — carefully exempted from `receiveQueue` — still shm_opens a full `sendQueue` ring nothing reads.**
- **`receiveQueue`** is mapped for every **non-basebackup** byte-ring stream (`:25758`).  On the DPU the aliasing
  is deliberately skipped (`:17760`), so it is never the landing ring either.  Today that means **the pgbench DPU
  SQL-result stream allocates a second dead ring, per stream, on the DPU.**

**What the DPU path ACTUALLY uses** (and neither shm queue appears in it):

| workload | receiver rings |
|---|---|
| **basebackup** — SINGLE-ring, dual purpose | peer RDMA → **landing ring** *(IS the DMA source; no deform)* → DMA → host **role-7** consumer ring |
| **pgbench result** — TWO-ring | peer RDMA → **landing ring** → **deform** → **tuple source ring** → DMA → host **role-5/7** arena ring |

### 31.2 Why they survive: THE GEOMETRY IS ANCHORED TO A DEAD RING

They survive for exactly one reason — **they are the only place the negotiated geometry lives.**  §30's fix
(`HomerServiceStreamNegotiatedByteRingBytes()`) reads `sendQueue.byteRingBytes`.  It is correct and it fixed the
regression, but **it stands on the very object we want to delete.**  There is also still a *fourth* geometry read
straight out of a queue at `:17745` (`ringBytes = streamEntry->stream.receiveQueue.byteRingBytes;`), which sizes the
landing ring.

⚠ **This reframes the `!IsBaseBackup` exemption.  Basebackup was never the weird one — it is the ONLY family that
already got this right.**  The condition that actually matters is *"is there a LOCAL BACKEND PROCESS to poll a
shm ring"*, and on a DPU the answer is always **no** (the worker lives on the host and cannot map the DPU's shm).
The exemption was written against the wrong predicate.

### 31.3 The plan (ordered; each step is safe on its own)

1. **Hoist geometry to scalars on the stream** — `stream.byteRingBytes`, `stream.maxRecordBytes` — set at creation
   from the open request.  Geometry finally owns itself instead of squatting inside a ring.
2. **Point `HomerServiceStreamNegotiatedByteRingBytes()` at the scalar**, and route `:17745` through it too.
   ⚠ This is a ONE-LINE change with no call-site churn — precisely the payoff of §30 having routed all three
   geometry checks through the accessor first.  Do NOT skip step 2's ordering: the accessor is what makes this
   cheap.
3. **Re-gate BOTH queue mappings on "is this a host-service stream"** rather than using `!IsBaseBackup` as the DPU
   distinction. The DPU then stops `shm_open`ing two dead rings per stream. The independent host-service rule that
   basebackup has no local receive backend and therefore omits `receiveQueue` remains in place.
4. When host-service Homer is deleted (see `copy_path_revival_contract.md`), both fields go entirely.

### 31.4 Validation requirement

⚠ **Gate AND 4-role basebackup, both.**  This changes stream *creation*, which is exactly the surface `4382b65d65`
broke (§30) — and the gate cannot see it.  §30.6 applies: a green gate says nothing here.

### 31.5 Related, and NOT the same thing

The **rename** (`sendQueue` → `localProducerSourceQueue`, `receiveQueue` → `localConsumerDeliveryQueue`,
`localReceiveByteRing` → `peerInboundLandingRing`; ~270 sites) is now **cosmetic** — the accessor already removed
the ambiguity that caused §30.  Do it whenever, but do it AFTER §31 (there will be less left to rename).

### 31.6 IMPLEMENTATION SPEC (2026-07-15 — reader-grep done; owner said do it now; hand to codex-worker)

**Reader-grep result:** `.sendQueue` = **126** refs, `.receiveQueue` = **63** refs, almost all LIVE host-service
data-path uses guarded by `!= NULL` (publishedTail/consumedHead reads, mapping validation, the
`receiveQueue → localReceiveByteRing` alias at `tuple_sink_service_process.c:18570`). On a DPU service the rings
are still `shm_open`ed but the `!= NULL` guards skip them; the ONLY DPU-path need is the **geometry**. So this is a
**re-gate + geometry hoist, NOT a deletion.** Field removal (§31.3 step 4) stays with S7.1.

**Creation site — `HomerServiceOpenPayloadStream` region, `tuple_sink_service_process.c:26753-26853`:**
`stream.slotCount` (`:26753`) and `stream.slotCapacityBytes` (`:26754`) are ALREADY stream scalars; geometry is
computed locally — `producerRecordCapacityBytes` (`:26759`/`:26768`) and `producerRingBytes` (`:26777`) — and today
lives ONLY inside `sendQueue`/`receiveQueue`.

- **Step 1 — hoist geometry to stream scalars.** Add `stream.byteRingBytes` (u64) + `stream.maxRecordBytes` (u32) to
  the SAME struct that holds `stream.slotCapacityBytes`/`slotCount`; set them at creation (just after `:26787`,
  byte-ring branch) from `producerRingBytes` / `producerRecordCapacityBytes`. Do NOT re-add slotCapacity/slotCount.
- **Step 2 — reroute geometry reads to the scalars.** `HomerServiceStreamNegotiatedByteRingBytes()` (`:4590`) →
  return `stream.byteRingBytes`; `:18552` (`ringBytes = receiveQueue.byteRingBytes`, sizes the landing ring) → route
  through the accessor/scalar; plus the "fourth geometry read" §31.2 flags near `:17745`.
  ⚠ **CRITICAL / make-or-break:** grep EVERY `sendQueue.byteRingBytes` / `receiveQueue.byteRingBytes` /
  `sendQueue.*maxRecordBytes` / `receiveQueue.*maxRecordBytes` and confirm ALL reads that can run on a **DPU-served**
  stream are rerouted to the scalar. A single missed one reads an UNMAPPED ring after step 3 → crash.
- **Step 3 — re-gate the two mappings.** sendQueue map (`:26803` byte-ring / `:26815` slot) is currently UNGATED
  (every stream); receiveQueue map (`:26830`/`:26843`) is gated `!IsBaseBackup`. Re-gate BOTH on a NEW named helper —
  e.g. `HomerServiceStreamNeedsLocalShmQueues(streamEntry)` — meaning **"is there a LOCAL BACKEND PROCESS to poll a
  shm ring"** = **NOT a DPU service** = `!(HomerServiceDpuDmaSchedulerEnabled(dpuDmaState) && dpuDmaState->engine !=
  NULL)` (dpuDmaState from `HomerServiceDpuDmaSchedulerStateForProgress()`; determinate at creation).
  ⚠ **Do NOT reuse `HomerServicePayloadStreamUsesDpuMirrorSource()`** — its per-stream TUPLE_VIEW_BATCH gate (`:7351`,
  false for non-`dpuRelayResultStream` tuple-view streams) would WRONGLY keep mapping on a DPU service. The §31.2
  predicate is purely service-level. Keep the existing basebackup/tuple-view conditions ANDed in.

**⚠ MUST NOT break the 126/63 host-service refs** — they run on NON-DPU services where the rings stay mapped; the
re-gate only drops the mapping on DPU services.

**Validation (§31.4): gate AND 4-role basebackup, BOTH** (this is stream-creation surface — the §30/`4382b65d65`
surface — and a green gate says nothing here). Then commit code + this KB note.
⚠ **clangd indexes only the ACTIVE build config**: references inside `#if HOMER_SERVICE_PAYLOAD_STATS` /
`HOMER_DPU_P2_DIAG` will be **silently skipped**, compile clean, and break only the *diagnostic* build.  Cross-check
with a textual grep and **build the diagnostic configs to prove it.**

### 31.7 Implementation result (2026-07-15)

Implemented in `citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`:

- `HomerPayloadStreamState` now owns `byteRingBytes` / `maxRecordBytes`, assigned from the already-validated
  `producerRingBytes` / `producerRecordCapacityBytes` in `HomerServiceCreatePayloadStreamEntry()`.
- `HomerServiceStreamNeedsLocalShmQueues()` implements the specified pure service-level predicate. Both byte-ring
  and fixed-slot send/receive mappings are gated by it; the independent basebackup receive exclusion remains.
- All DPU-executable negotiated-geometry reads found by the implementation sweep use the stream scalars: the
  canonical accessor and three strict reopen checks, tuple receive landing sizing, queue-descriptor publication,
  async peer-open equality validation, and the service-owned result peer-open request. The two remaining literal
  `sendQueue.byteRingBytes` reads are inside `TupleSinkServiceEnsureSendQueueMemoryRegion()` and deliberately
  validate an actual host shm mapping rather than discover negotiated geometry.
- The §31.6 inventory was incomplete in current code. The sweep also found and corrected three load-bearing
  dependencies on the now-absent DPU mappings: `HomerServiceFillPayloadStreamQueueDescriptor()` now publishes an
  informational descriptor from stream geometry on a DPU service; `HomerServicePrepareTupleResultStreamForCommand()`
  requires producer queue control only on the legacy host path; and close deferral logging no longer falls through
  from an absent byte-ring control to an absent fixed-slot control.
- Textual cross-config grep found no queue-backed `maxRecordBytes` field reads (that member never existed) and no
  additional queue-control ring geometry hidden in diagnostic branches. `git clang-format` and `git diff --check`
  passed. `sudo -n -u dbcomm make -j8 service-bin CPPFLAGS='-D_GNU_SOURCE'` relinked
  `build/homer/citus_tuple_sink_service` successfully (exit 0); existing warnings remain outside this change.

**Scoped-out mixed mode:** unflagged tuple/COPY streams require a host service without an active DPU DMA engine.
The §31 service-level decision intentionally does not preserve legacy shm queues inside a DPU-enabled service;
the source comments and receive-open diagnostic now state that constraint. The required gate/basebackup workloads
do not exercise this unsupported combination.

### 31.8 Validation (2026-07-15) — PASS (adversarial diff review + gate + 4-role basebackup)

**Diff reviewed adversarially before validation** (the codex-worker diff, against §31.6): predicate correct
(pure service-level, NOT `UsesDpuMirrorSource`); re-gate short-circuits so a DPU service maps NEITHER queue;
reopen-check substitution `stream.maxRecordBytes` for `slotCapacityBytes + sizeof(header)` is **provably
equivalent** (`TupleSinkServiceLocalSendQueueSlotBytes` computes exactly that sum) and now self-consistent with
the published descriptor; the three "extra" dependency fixes (§31.7) are **required** — unguarded `sendQueue`
derefs on the DPU path that would SIGSEGV the gate once the ring is unmapped; the other ~75 ring-control derefs
are all `!= NULL`-guarded and skip on a DPU stream.

**Runtime, on the real hardware** (build + both-DPU deploy; landed-proof: the new
`"unflagged tuple/COPY streams require a host service"` string present in both DPU binaries):
- **No crash, no geometry-miss** (the make-or-break): both DPU services survived to end-of-run (ledgers printed on
  SIGTERM); DPU logs show ZERO `zero stream geometry` / `require a host service` / `no producer byte-ring control` /
  segfault lines — no DPU-path geometry read hit an unmapped ring.
- **Gate:** `homer-dpu-command` path confirmed; 0 failed transactions across 3×2000-tx repeats; DPU backend spawn
  begin→COMPLETED all sessions; anti-fallback clean (no host service).
- **4-role basebackup (the make-or-break — stream-creation surface a green gate can't see):** completed,
  `delivered_bytes=23,252,546,830` (~21.65 GiB, ~44k wrap laps), clean CLOSE_ACK both ends. Confirms stream
  creation + the byte-ring WRAP with the rings unmapped on the DPU.
- **ALARM sweep:** 0 matches on both DPUs. Ledgers healthy (abandonment 0/0, live=0).

⚠ **Gate tps measured 284.6–287.0** (3 repeats), **2–6% below the 291–304 band** — recorded honestly and
attributed to **machine load, NOT §31**. §31 is control-path (re-gates allocation at stream *creation*, once per
stream; the gate runs 2000 tx on one stream), so its per-tx cost is a couple of scalar reads — nanoseconds, not the
~140 µs/tx a 4% drop implies; the one per-command function it touches it made *faster* on the DPU. The box was
heavily multi-tenant (934–990 foreign PIDs), and the same session's earlier (A)+(C) run hit 297.9–300.7 in-band on
this box. **§31 did NOT move the band — do not add a §3.3 band row for it.** (Owner elected to commit on the
mechanism; a controlled A/B was offered and declined.)

---

---

## §32 — MOVED. Send-CQE coalescing has its own plan doc.

The generalized send-CQE coalescing design **outgrew this audit and is a different topic** — this document is
about the *resource-retirement contract* (correctness); coalescing is a *performance* generalization that merely
*depends* on it. It now lives in:

**`docs/kb/future-directions/citus/transport/send_cqe_coalescing_and_pool_depth.md`**

What stays here, because it IS retirement-contract business:

- **§29(b)** — per-connection send-queue admission (`postedSendWrs - retiredSendWrs`). It is the accounting that
  the coalescing plan depends on, and it is a correctness fix in its own right (it replaces a hot-path connection
  reset with a retry).
- ✅ **THE UNSIGNALLED-TAIL LEAK — CLOSED 2026-07-14 by DELETING the inline command fast path** (P2-o batch;
  validated PASS: gate 297.9/299.9/300.7 tps in the 291–304 band, 4-role basebackup wrap 21.66 GiB CLOSE_ACK,
  0 ALARMs on both DPUs, both DPUs' teardown ledgers healthy). The client-SQL command INLINE fast path (the former
  `useInlineCommandPost` branch in `TupleSinkServicePostRemoteClientSqlCommandRecord`,
  `tuple_sink_service_process.c`) posted TWO UNSIGNALLED WRs and never consulted the always-true
  `signalCommandWrite`, so their **send-queue WQEs** had no signalled successor of their own.

  ⚠ **It was LATENT on every real workload, not live** (mechanism confirmed by a read-only Codex trace + own
  byte-math verification, 2026-07-14). The inline branch was taken for any command whose bytes ≤ the negotiated
  inline grant. ⚠ **The grant is NOT the requested 64.** The QP requests `CITUS_REMOTE_EXEC_PEER_INLINE_WRITE_BYTES`
  = 64 but the provider grants MORE — **~124 B on this fabric** (per the command-plane migration plan; Codex's
  read-only trace assumed the *requested* 64 and explicitly flagged the runtime value UNVERIFIED — the migration-plan
  number is the corrected one, and it does not change the conclusion). So the small commands all fit inline:
  END/COMMIT/SESSION_CLOSE (`commandBytes == CITUS_REMOTE_EXEC_LOCAL_COMMAND_HEADER_BYTES == 48`), BEGIN (~96 B), and
  the 8-byte readySeq. The pgbench **UPDATE/SELECT** records EXCEED the grant (~134 B) and take the SIGNALLED
  non-inline path. On the gate (TPC-B) the inline and signalled commands are INTERSPERSED, so each inline command's
  unsignalled tail was retired by a later signalled UPDATE — an RC QP completes in order, so one signalled CQE
  retires the whole unsignalled tail behind it. **That masking is why `-t 2000` never wedged.** The SHARP failure
  needs a **compact-only command stream** — every command ≤ the grant, so NO signalled successor ever appears:
  `outstanding` climbs to the §29(b) unsignalled admission ceiling and the two-post inline pair admits the body then
  FATALLY rejects readySeq (`exit(1)`). No workload produces this.

  **FIX (owner decision, 2026-07-14): DELETE the inline fast path** rather than sign-and-reserve it. VERIFIED the
  non-inline path is a strict superset: it posts the body unsignalled + the readySeq **SIGNALLED and WR-id-tagged**
  (so the send CQE retires the tail through the normal reap `TupleSinkServiceRetireClientSqlCommandWriteCompletionEntry`,
  which advances `retiredEpoch`/`consumedEpoch`), and its posts STILL take `IBV_SEND_INLINE` for small buffers via
  the shared `TupleSinkServicePostWriteScatterGatherInternal` (`remote_execution_peer_transport_rdma.c:6164`) — so
  the data-copy benefit is preserved. The only thing lost is a completion-reservation skip on ~one command per
  transaction (measured: no tps regression). **Leak-closed by construction**: no unsignalled-only command post path
  remains. ⚠ **The gate CANNOT prove leak-closure** (the leak never manifests there); the gate + basebackup were run
  as a REGRESSION check only. *(An alternative "keep inline, signal its terminal WR + reserve a completion" was
  rejected: it converges on exactly what the non-inline path already does, so it is strictly more code for the same
  result — see §49's fix-option record.)*

  ⚠ **P0-i cleaned two lanes and missed this one BECAUSE IT WAS COUNTING THE WRONG RESOURCE.** An INLINE write
  copies its payload into the WQE, so its *source buffer* is free at post time — which is exactly why the path
  looked safe to an audit hunting leaked *source slots*. **It leaked a WQE instead**, and nothing in this system
  counted WQEs until §29(b). *The bug did not appear; the instrument did.*
  This is the same shape as every §0 violation: someone proved the contract for ONE resource and missed a SECOND
  riding the same completion.

---

## §33 — P1-f: IMPLEMENTATION SPEC (write the invariants down, and ASSERT them)

> ## ⛔ §33 AS FIRST WRITTEN IS **WRONG**. See §33.6 for the corrected spec — it is what to implement.
> The sections below are kept because **the reasoning that produced the error is the instructive part**, and
> because the error is a *repeat of a warning this very document already contains*. Read §33.6, then come back
> if you want to know why the obvious design fails.

**Status: SPEC. Ready to implement.** Closes the last substantive item in this audit.
**All line numbers re-derived at citus `22a8f9951e6`** — §9.3's and §11.1's are STALE (thousands of lines
have been inserted since). **Enforcement points are named by FUNCTION, not by line, precisely because of that.**

### 33.1 Why this is worth doing when nothing is broken

§11.1 verified that **all three** remote-writable-MR proofs **hold today**. So P1-f changes no behavior. It is
worth doing anyway, and this document's own thesis is the reason:

> **§0.1: "The contract is upheld EXACTLY where someone was previously burned, and nowhere else. That is scar
> tissue, not design."**

**A proof that is verified but unasserted is scar tissue waiting to happen.** We did the analysis; if we ship
nothing that enforces it, the next person to touch the close path breaks it silently — which is the exact
failure mode this audit exists to document. P1-f converts three proofs into one enforced invariant.

*(Live demonstration, found the same day: the DPU setup-listener starvation produced **three** separate scars —
a private source, `knownExpectedWork`, and a bypass list — each bolted on when someone got burned. The twelve
still-aliased DPU collectors are the un-scarred complement. See
[`dpu_collector_feedback_aliasing_defect_b.md`](./dpu_collector_feedback_aliasing_defect_b.md).)*

### 33.2 THE THREE RESOURCES — three proofs, and they are NOT the same proof

Each is a **remote-writable MR** (Scenario C: the peer writes into my memory; I cannot see their completions).
**Merging them would document a proof that does not apply to half of what it claims to cover** (§11.1).

| # | resource | who writes it | why it is safe TODAY (VERIFIED) |
|---|---|---|---|
| **I1** | peer **COMMAND mailbox** (`clientSqlPeerCommandMailboxDescriptor`, `:1525`) | the **peer**, into my memory | **Single writer**, one connection handle, no credit/completion/payload/alternate-class writer. RC in-order on one QP + **close-is-last** ⇒ *"no further write will arrive."* |
| **I2** | client **COMPLETION mailbox** (`clientSqlPeerCompletionMailboxDescriptor`, `:1526`) | **`TupleSinkServicePublishPeerClientCommandCompletion` (`:19082`) — the SOLE remote publisher** | **The terminal close completion IS the last remote write.** The sender does **not** write `readyEpochSlots`/`readyVersion` — the *receiver's* CPU publishes those after WIMM validation, so they are **local stores, not later remote writes**. The QP is **stable for the session**: failure **ENDS** the session, it does not migrate it — no reconnect, no rebind, no traffic-class migration. So RC ordering is not broken by a hidden connection swap. |
| **I3** | peer **command-completion RING** (`peerCommandCompletionRingDescriptor`, `:1527`) | the peer-command-completion publisher (`:19657`-`:19712`) | **It fences its own source SYNCHRONOUSLY.** An unsignalled record write is followed by a signalled `publishedEpoch` write through `TupleSinkServiceWritePeerUint64PreparedRdma` (`:19709`), which **blocks on its own send CQE** — `TupleSinkServiceWaitForSendCompletion`, `remote_execution_peer_transport_rdma.c:11648`. **(VERIFIED at implementation time, not inherited — §9.3's cited line was stale.)** ⚠ **A DIFFERENT op** from I2: different op kind (`OP_SQL_COMMAND`, not `CLIENT_SQL_SESSION`), different connection handle, different descriptor, and its publisher explicitly **excludes** client-SQL sessions. |

### 33.3 THE FIX — ONE latch enforces "close is last" for all three

**The client has a terminal latch; the SERVICE does not.** `sqlSessionTerminal` lives only in
`homer_client.c` (`:3556`-`:3627`). That asymmetry is the gap: the service can, in principle, publish after
its own terminal close and nothing says otherwise.

**Add one service-side latch** on `TupleSinkServiceSessionState`:

```c
/*
 * P1-f. Latched when the TERMINAL completion for this session has been published
 * into the peer's memory.  After this point the session's remote-writable MRs
 * (command mailbox, completion mailbox, peer command-completion ring) may be
 * deregistered by the OWNER at any time -- so ANY later remote write by us is a
 * use-after-free ACROSS THE FABRIC, into memory the peer is entitled to have
 * reclaimed.  It is not a race we can win by being fast; it is a contract we must
 * not violate.  The three resources are safe today for THREE DIFFERENT reasons
 * (audit S33.2); this latch is the single thing that keeps all three true.
 */
bool peerRemoteWriteTerminalPublished;
```

- **SET** it in `TupleSinkServicePublishPeerClientCommandCompletion` (`:19082`) at the point the **terminal**
  completion is successfully posted (`TupleSinkServiceCommandStateIsTerminal(...)` on a `CLIENT_SQL_SESSION`
  close). ⚠ The **backend-FAILURE** path synthesizes its terminal close completion **through the same
  machinery** — so it latches too, for free. **Verify that; do not assume it.**
- **CHECK** it at the **entry** of every remote publisher — all three, by name:
  `TupleSinkServicePublishPeerClientCommandCompletion` (I2), the peer command-mailbox publisher (I1,
  `:21648`-`:21704`), and the peer command-completion-ring publisher (I3, `:19657`-`:19712`).
  On a hit: **LOUD error, and REFUSE the publication.** Do not publish and warn — publishing is the bug.

### 33.4 ⚠ CONSTRAINTS THE IMPLEMENTER MUST NOT GET WRONG

1. **DO NOT use `assert()`.** `assert.h` is absent in this tree and `NDEBUG` is never set — it would **abort
   the service**. Use `fprintf(stderr, "... ALARM ...")`-and-refuse, matching the existing style.
2. **NO behavior change on the correct path.** If the new branch ever fires in a passing run, the *latch* is
   wrong, not the code it caught. **The acceptance criterion is that it NEVER fires.**
3. **Do not "fix" I1.** §9.3 is explicit: *"ALREADY CORRECT … write the invariant down + assert it. **Do not
   change it.**"*
4. **This is a CONTROL path.** The three publishers are per-command/per-session, not per-tuple. One predicate
   is free. Do not micro-optimize it, and do not put it behind a stats macro — **a guard that can be compiled
   out is not a guard.**
5. **Comment each resource with its OWN proof from §33.2.** The whole point of P1-f is that the next reader
   can see *why* each one is safe. Three near-identical comments would re-create the exact conflation §11.1
   caught.

### 33.5 Acceptance

- The gate (`pgbench --homer --homer-dpu-command`) passes, **and the new ALARM never fires** — across the gate,
  a clean SIGTERM shutdown, and the 4-role basebackup.
- **The 4-role basebackup is mandatory here.** A green gate says nothing about the byte-ring **wrap**; the
  basebackup is the only workload that exercises it (~44,000 laps). See CLAUDE.md.
- **State explicitly that this changed no behavior.** It is an enforcement change. Do not let it collect credit
  for a fix it did not make.


---

### 33.6 ⛔ CORRECTED SPEC — §33.3's "one latch enforces all three" IS WRONG. Two of its three guards are DEAD CODE.

**Found by adversarial review of the implemented diff, 2026-07-13. VERIFIED in code, not inherited.**

#### The error, precisely

§33.3 said *"one latch enforces close-is-last for all three."* **A latch set by one writer cannot gate a
different writer.** And these three writers are **three different session roles — two of them in a different
process, on a different machine** (`tuple_sink_service_process.c:1460`-`:1472`):

> *"`clientSqlRemoteSender` lives on the **frontend node** service … `clientSqlPeerReceiver` lives on the
> **backend node** service."*

The latch's only set-site is inside the **I2** publisher, which runs **only on the backend node**. Therefore:

| | writer's session role | reaches the latch set-site? | guard as specified |
|---|---|---|---|
| I1 | `clientSqlRemoteSender` (frontend node) | **NO** — that publisher runs on the other machine | ☠ **DEAD** |
| I2 | `clientSqlPeerReceiver` (backend node) | yes | ✅ live |
| I3 | an `OP_SQL_COMMAND` session (different op kind) | **NO** | ☠ **DEAD** |

**§29.1 of this document is titled "THE GUARD IS DEAD CODE. Both branches return true."** The spec was about to
ship two more of exactly that.

> ### ⚠ AND IT IS A REPEAT OF A WARNING THIS DOCUMENT ALREADY CONTAINS
> **§11.1:** *"P1-f is TWO invariants, each with its own proof. **Write them separately.** Merging them would
> have documented a proof that does not apply to half the thing it claims to cover."*
> §33 **quoted that**, and then merged them anyway — into one latch.
>
> **The rule, restated so it survives the next author (me):** *when a document tells you two things are
> different, it is not making a stylistic point about documentation. **It is telling you the CODE is
> different**, and any mechanism you build that treats them as one will be dead on arrival for the half you
> were not looking at.*

#### What P1-f actually is (§33.2 had the DIRECTION subtly wrong too)

These MRs are **owned by me and written by the PEER**. The real question is **"when may the OWNER
deregister?"**, and the owner **cannot see the remote writer's send CQEs at all** — they land in the *writer's*
CQ, on the *writer's* machine. There is no RDMA primitive that tells a target "the writes into you have
retired."

**So the owner must infer it from a RECEIVED FACT** — and the enforcement therefore belongs **at each WRITER**,
asserting it upholds the premise the remote owner is silently depending on. That premise is **not the same for
all three**:

| | the premise the remote owner's deregistration RESTS on | so the enforcement is |
|---|---|---|
| **I1** | **inference by ORDERING**: all writes *and* the close ride **one RC QP**, close is **last** ⇒ receiving the close proves every earlier write landed | a **close-is-last latch in ITS OWN writer** + **a same-QP-handle assert** |
| **I2** | same as I1 | same as I1 (**the implemented I2 latch is already correct — keep it**) |
| **I3** | **inference by the WRITER'S OWN FENCE**: it blocks on its own send CQE before writing `publishedEpoch` ⇒ seeing the epoch proves the record write retired. **It never relies on ordering.** | **NOT a latch.** A close-is-last latch is the wrong tool. |

#### ⚠ THE SECOND PREMISE, WHICH NOTHING WATCHES — and it is the more fragile one

I1/I2's whole proof is *"RC in-order **on one QP**."* **If a session's connection handle were ever re-pointed
mid-life, RC ordering would prove nothing** and the owner's deregistration becomes a use-after-free across the
fabric — **silently**. §11.1 verified the handle *"is assigned once and never re-pointed; failure ENDS the
session, it does not migrate it"* — **a verified premise with nothing enforcing it.** Exactly the §0.1 shape.
**Assert it at the writer: the handle used for the close must be the handle recorded at bind.**

#### ⛔ I3 IS DROPPED — and NOT because it is hard. **ITS PATH DOES NOT EXECUTE.**

`TupleSinkServicePublishPeerCommandCompletion` has **one** caller (`:21219`), reached only via the `else` at
`:21214` — i.e. **only when `opKind != CITUS_REMOTE_EXEC_OP_CLIENT_SQL_SESSION`**. That is the
`OP_SQL_COMMAND` / backend-to-backend arm, which is **🔴 BROKEN AT HEAD** (b2b COPY hangs) and is **scheduled
for deletion at S7**. And `TupleSinkServiceWritePeerUint64PreparedRdma` — whose header calls it *"the hot-path
companion"* — has **exactly one call site in the whole tree**: that publisher. **It is on no hot path. It is
on no path.**

**Do nothing to I3.** Not a guard, not a comment-only assert, and above all **not the optimization that was
about to be proposed for it** (§33.7).

#### 33.7 ⚠ THE ANALYSIS THAT WAS *CORRECT* AND *WORTHLESS* — record it so nobody redoes it

While specifying I3's enforcement I established, correctly, that:

- **the fence buys NO ordering.** RC already places two WRITEs on the same QP in order; the record lands before
  the tail for free.
- **what it actually buys is SOURCE-BUFFER REUSE.** `connectionState->outgoingPublishedTailBuffer`
  (`remote_execution_peer_transport_rdma.c:11644`) is a **single per-connection scalar** registered as
  `outgoingTailMr`. The next tail publish overwrites it, so a second write cannot be posted until the first
  retires. **Blocking on the CQE IS the source-retirement mechanism** — Scenario A, exactly as §11.1 said.
- **it is an expensive way to get that.** `TupleSinkServiceWaitForSendCompletion` busy-polls until *this*
  `wr_id` completes; an RDMA-WRITE send CQE arrives when the **remote HCA ACKs**, so it stalls the service's
  **single-threaded** loop for ~a network RTT **per publish** — head-of-line blocking across every session on
  the node.
- **and we already own the fix**: §29(b)'s cumulative `postedSendWrs`/`retiredSendWrs` frontiers. Give the tail
  a small **ring** of slots instead of one scalar, stamp each with its post ordinal, reuse once
  `retiredSendWrs >= stamp` — the same stamp-at-submit / compare-at-retirement pattern as P0-b and the arena
  clear. Fence deleted, invariant preserved.

**Every line of that is true, and every line of it is worthless**, because the code never runs.

> ### 🔑 THE RULE THIS EARNS (it cost three attempts at the same spec)
>
> **"Is this correct?" and "is this fast?" are questions about the LIVE system, and they have no answer until
> you know the code EXECUTES. Ask "does this run?" FIRST — reachability is far cheaper to check than
> correctness, and it dominates it.**
>
> Twice in one hour a fully-verified mechanism turned out to be unreachable: defect B's blind-backoff
> suppression (killed by `knownExpectedWork`, three lines from where I was reading —
> [`dpu_collector_feedback_aliasing_defect_b.md`](./dpu_collector_feedback_aliasing_defect_b.md)), and this
> fence. **A "verified mechanism" on a dead branch is not a finding. It is a very well-researched way to be
> wrong.**

#### 33.8 THE WORK, corrected

1. **I2 — KEEP AS IMPLEMENTED.** The latch + guard in `TupleSinkServicePublishPeerClientCommandCompletion` is
   correct and live. Keep its comment.
2. **I1 — MAKE ITS GUARD LIVE.** Same latch field, but **set it in I1's OWN writer**:
   `TupleSinkServicePostRemoteClientSqlCommandRecord`, when the posted record's `commandKind` is
   `CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE` (the poster already switches on that kind, `:21643`).
   ⚠ Latch **only after the post SUCCEEDS** — a failed post published nothing.
   *(A session is either a sender or a receiver, never both, so ONE field safely serves I1 and I2 — each set by
   its own role's terminal. **That, and not "one latch for all three", is the defensible version of the
   original idea.**)*
3. **I1 + I2 — ADD THE SAME-QP-HANDLE ASSERT.** At each writer, LOUD-ALARM if the peer connection handle in use
   is not the one recorded for the session at bind. This protects the *ordering* premise the remote owner's
   deregistration rests on, which today rests on nothing.
4. **I3 — REMOVE the guard entirely.** Keep a short comment recording (a) its source-fence proof and (b) that
   its path is dead and pending S7 deletion, with a pointer to §33.7 so the optimization is not re-derived.
5. **All three comments stay** — they are the "write the invariants down" half of P1-f, and they are correct.

#### 33.9 Acceptance (unchanged in spirit, sharpened)

- The gate passes, **and the new ALARMs NEVER fire** — across the gate, a clean SIGTERM shutdown, and the
  4-role basebackup (mandatory: the gate never wraps the byte ring; the basebackup does, ~44,000 times).
- **Both surviving guards must be REACHABLE.** Before claiming P1-f complete, state *which node role and which
  workload* executes each set-site. A guard whose latch is never set is not a guard — that is the whole lesson
  of this section.
- **State explicitly that this changed no behavior.** It is enforcement, not a fix.

### 33.10 ⚠ CORRECTION TO §33.6 — "a verified premise with NOTHING enforcing it" was **WRONG**. It WAS enforced. It just had no VOICE.

**Found while reviewing the implemented diff. My third unchecked claim about this file in one day, and all three are the same species.**

§33.6 said the same-QP premise *"rests on nothing."* **False.**
`TupleSinkServicePeerMemoryRegionUsableRdma` (`remote_execution_peer_transport_rdma.c:10190`) **already**
compared `memoryRegionHandle->connectionState == connectionState && ->connectionGeneration ==
connectionState->generation`, and **both** publishers already call it with **exactly** the (MR, connection)
pair the new guard checks — `tuple_sink_service_process.c:21747` (I1) and `:19185` (I2).

**So the guard cannot fire where the old path succeeded.** It is a strict factoring-out of an existing
conjunct. That is also why it is **safe**: no false positive is possible on a path that works today.

#### The REAL justification — and it is stronger than the one I invented

The binding check was one of **six ANDed conjuncts** behind **one** outcome ("path not ready"), and
`connectionState->generation` is assigned **only at connection creation** (`:6699`, `:7383`) — never bumped in
place. **Therefore a binding mismatch is PERMANENT.** And the old code's response to it was to take the
"not ready" branch, bump `remoteCommandPathNotReady++`, **and try again — forever, against a condition that can
never change.**

> **A silent, permanent, retrying stall. Indistinguishable from a healthy idle service.**

The two rules this project already holds say exactly what to do:
> *"A guard that ANDs N conditions must report WHICH one failed. One shared message across N conditions is not
> a diagnostic, it is a decoy."*
> *"Silence must not be a valid state. If a bound thing is neither progressing nor terminal, it should say so
> itself."*

**So the guard's value is not new enforcement — it is giving an existing, permanent, silent failure a NAME and
a terminal outcome.** That is a better reason than the one I gave, and it is the honest one.

#### ⚠ AND STATE THE GUARD'S LIMIT HONESTLY — it is a PROXY

It compares **MR → connection**, not *"this session's connection was never re-pointed."* **If a reconnect
re-registered the MRs onto the new connection, both would agree and the guard would pass** — while the ordering
premise across the reconnect is broken. §11.1 verified that cannot happen today (*"failure ENDS the session, it
does not migrate it"*). **What the guard DOES catch is a session handle re-pointed while its MRs stay bound to
the old connection — which is precisely the shape P7b (pooling) introduces.** That is the case worth watching,
and it is why this belongs in the audit rather than in a cleanup commit.

> ### 🔑 THE RULE (and this is the THIRD time today)
>
> **"Nothing enforces X" is a NEGATIVE claim. It requires a SEARCH, not an intuition.**
>
> Today, in this one file: (1) I claimed defect B suppresses command discovery — refuted by `knownExpectedWork`,
> three lines from where I was reading. (2) I claimed I3's fence needed hardening — refuted by its path not
> executing. (3) I claimed the QP premise was unenforced — refuted by a conjunct inside a predicate the caller
> was already calling. **All three were assertions of ABSENCE, and none of the three cost more than one `grep`
> to check.** Asserting a presence gets checked by the compiler and by review. **Asserting an absence gets
> checked by nobody but you.**

---

## §34 — P2-i: `HomerDpuDmaDestroy` drain on CLEAN exit. THE LAST CODE ITEM OF THIS AUDIT.

**Status: SPEC (2026-07-13).** §9.3's one-paragraph sketch was *directionally* right and had **one hole big
enough to crash every dirty shutdown** (F7 below). Everything here was verified in code first — the discipline
that P1-f's three wrong claims earned.

### 34.1 VERIFIED FACTS — established BEFORE any design

| # | fact | evidence |
|---|---|---|
| **F1** | `HomerDpuDmaDestroy` has **no drain**, and says so | `homer_service_dpu_dma.c:1277-1289`: DestroyDocaLifecycle → FreeStage5Scaffolding → `free()`. Its own block comment (`:1273-1276`) — *"later stages must add explicit drain-before-destroy policy."* |
| **F2** | **Every DOCA return on the teardown path is `(void)`-discarded** | `:14897` `doca_ctx_stop`, `:14905` `doca_dma_destroy`, `:15030/:15033` inventory, `:15038` `doca_pe_destroy`, `:15043` `doca_dev_close`. And `docaContextStarted = false` is set **unconditionally** (`:14898`) — so if `ctx_stop` returned `IN_PROGRESS` (ctx is **STOPPING**, not stopped), **the flag is a lie.** |
| **F3** | **Imports are cleared BEFORE the contexts are stopped** — free-the-target-then-stop-the-engine | `:14885-14888` (clear ALL imports) precedes `:14893-14900` (`ctx_stop`). `HomerDpuDmaClearHostMmapImport` (`:8912`) destroys the DOCA mmap (`:8949`), `free()`s `descriptors`/`ringRuntime` (`:8954-8955`), and **`memset`s the import to zero** (`:8956`) — **without ever consulting `import->inflightTaskCount`.** The `memset` also **destroys the evidence** that anything was in flight. |
| **F4** | the **global** physical-outstanding count **already exists** | `facts->totalInflightTaskCount` (`:1467`), via `HomerDpuDmaGetSchedulerFacts` (`:1295`). P2-i does not need a new counter. |
| **F5** | ✅ **`totalInflightTaskCount` is COMPLETE** — the four sub-counters are strict **SUBSETS** | each is incremented **on the line immediately after** an `inflightTaskCount++`, same function, same path: control-read `:12070`/`:12069`; command-pull `:12249`/`:12248`; backend-cmd-publish `:12680`/`:12679`; backend-completion-pull `:12900`/`:12899`. All five decrements sit in **one block**, `:11439-11471`. ⇒ **`totalInflightTaskCount == 0` ⟹ all four are 0.** The drain predicate is that ONE term, and we can say why. |
| **F6** | `HomerDpuDmaDestroy` runs **only at process exit or Create-failure** | callers: `tuple_sink_service_process.c:48846` (service exit), `homer_service_dpu_dma.c:1256`/`:1263` (Create failure — nothing is in flight yet), and the smoke binaries. **This is exactly why it is P2 and not P0**, and it must be said out loud rather than quietly inflated. |
| **F7** | ⚠ **`engine->fatalError` does NOT exit the service** | set at ~10 sites (`:2093`, `:2273`, `:2475`, `:2511`, …); it only poisons progress. The service **runs on with a dead engine** and reaches the SAME teardown at SIGTERM. (The runbook already records the operational half of this: *"a run can PASS while the engine dies underneath it."*) **⇒ the drain can be entered with an engine whose tasks will NEVER complete.** |
| **F8** | SIGTERM **is** the clean-exit path | `KeepTupleSinkServiceRunning` (`tuple_sink_service_process.c:3861`); SIGINT/SIGTERM installed at `:23565-23566`; the loop exit falls straight through to the teardown. |
| **F9** | **Nothing progresses the PE after the loop exits** | between `:48811` and `:48846` there are only the stream/session resets. P0-d's drain lives in the `ResetSession` funnel and is **per-arena-slot**; engine-level work (grouped-control reads, command pulls, spawn ops) belongs to **no arena slot** and is structurally invisible to it. |
| **F10** | ⚠ a per-import teardown/detach drain gate **already exists, and it already deadlocked once — for exactly the reason P2-i must avoid** | `homer_service_dpu_dma.c:1539-1549` records it verbatim: a **perpetual poller** that kept re-arming through CLOSING pinned `import->inflightTaskCount` above 0 and produced *"an endless teardown-drain-wait spin."* |
| **F11** | ⚠ **A COMPLETION CALLBACK SUBMITS A NEW TASK** | `:10733` — `doca_task_submit_ex(...)`, whose error string is *"DPU-to-host publish failed **from callback**"*. **⇒ `totalInflightTaskCount` is NOT monotonically decreasing under `doca_pe_progress()`.** Draining one task can create one. A bare `while (total > 0) progress();` **has no guaranteed fixed point.** |
| **F12** | there is **NO centralized task-slot acquire** | **8** `freeTaskSlotCount--` sites (`:11855, :12067, :12246, :12379, :12511, :12677, :12897, :13188`) — exactly coinciding with the 8 `inflightTaskCount++` sites — and **one** release (`:11492`). The arm sequence is **duplicated inline 8×**. The accounting invariant therefore holds **by eight repetitions, not by construction.** |
| **F13** | each arming predicate has **TWO definitions** (`#ifdef HOMER_DPU_DMA_WITH_DOCA` / `#else`) | `CanSubmit` `:9547`/`:9686`; `CanDrainHostRing` `:9571`/`:9696`; `CanEgressMirror` `:9586`/`:9701`. **clangd indexes only the active config** — an edit to one copy silently diverges the other build. Touch both. |

### 34.2 ⚠ THE §9.3 SPEC HAS A HOLE, AND IT IS F7

§9.3 said, in full: *"3 DRAIN: progress the PE until the global outstanding count is zero (bounded; **FATAL on
non-convergence**)."*

**Implemented as written, every fatal-engine shutdown now FATALs** — because a dead engine cannot retire its
tasks (F7), so the drain burns its bound and then aborts. That converts a survivable dirty exit into a crash **at
exactly the moment the logs matter most**, and it would have looked like P2-i *introduced* a shutdown bug.

§10.6 of this very document already stated the rule, and I nearly walked straight past it:

> **"SCENARIO E IS NOT THE ONLY NON-DRAINING OUTCOME — CLASSIFY BEFORE YOU CANCEL."**

**The drain must ask "can this engine still complete work?" BEFORE it asks "is it drained?"**

### 34.3 THE DESIGN

`HomerDpuDmaDestroy(engine)` **keeps its signature.** Per F6 every caller is an exit path and none of them holds
a policy to pass; the classification is **derived from the engine's own state**, not supplied.

```
HomerDpuDmaDestroy(engine):

  1 DECIDE   -- classify the exit (F7, §10.6):
       engine->fatalError      -> CANCEL   (Scenario E: the engine CANNOT retire anything. Say so, loudly,
                                            and do NOT drain -- draining a dead engine is the hang.)
       !engine->docaEnabled    -> TRIVIAL  (no DOCA: nothing can be in flight)
       otherwise               -> DRAIN

  2 QUIESCE  -- engine->shuttingDown = true.   *** THIS IS THE LOAD-BEARING PART, NOT THE LOOP (F11). ***
               Without it the drain has no fixed point: a completion callback re-submits (:10733).
               It must stop every PRODUCER -- and NOT the bookkeeping (see the trap below).

  3 DRAIN    -- loop doca_pe_progress(engine->pe) until GetSchedulerFacts().totalInflightTaskCount == 0  (F5).
               Bounded by BOTH an iteration cap AND a wall-clock deadline.
               REPORT UNCONDITIONALLY: entered-with N outstanding, converged in K iters / T us.
                 (N is a number we do not have today and cannot currently guess -- see 34.5.)
               Non-convergence, or fatalError raised MID-drain  -> LOUD (fprintf ALARM; never assert --
               assert() would abort the service) -> then fall through to CANCEL. Do NOT abort.

  4 RELEASE  -- REORDER, which is the fix for F3:
               a. doca_ctx_stop() per started ctx -- *** CHECK THE RETURN ***. DOCA_ERROR_IN_PROGRESS means
                  STOPPING, not stopped: progress the PE until the ctx reports IDLE (bounded). Only then may
                  docaContextStarted be cleared (F2 -- today that flag is set to false regardless, i.e. it lies).
               b. ONLY NOW clear the host mmap imports. They are the DMA TARGETS; today they are destroyed
                  first (F3).
               c. destroy dma ctxs / mmaps / inventory / PE / dev -- check every return, log every failure.
```

#### ⚠ The quiesce trap — do NOT gate all three arming predicates

`HomerDpuDmaImportCanEgressMirror` gates **bookkeeping, not a submit** — it guards
`HomerDpuDmaRequeueDiscoveredReady` (`:8799`), and that function's own comment (`:8806-8810`) says that when it
returns false **"the send-CQ drain caller escalated that to `engine->fatalError`."**

**Gating `CanEgressMirror` on `shuttingDown` would therefore set `fatalError` from inside our own drain** — a
self-inflicted wound, and precisely the "my fix contradicts the contract stated in the same document" shape that
§7.8 already caught once. Quiesce the **submit** paths; leave the **bookkeeping** paths alone.

### 34.4 TWO OPTIONS FOR THE QUIESCE — decision pending the accounting audit

|  | **Option A — gate the predicates** | **Option B — funnel the arm** |
|---|---|---|
| what | `shuttingDown` checked inside `CanSubmit` + `CanDrainHostRing` (both `#ifdef` copies, F13) + the in-callback resubmit (`:10733`) | extract the 8×-duplicated arm sequence (F12) into one `HomerDpuDmaArmTaskSlot()`; check `shuttingDown` **there** |
| size | ~6 edits | 8 call sites + 1 helper |
| correctness | **rests on a NEGATIVE claim**: *"no submit escapes those two predicates."* | **correct by construction**: you cannot submit a task you could not arm |
| bonus | none | makes the F5/F12 accounting invariant (`arm ⇒ inflight++`) **structural** instead of eight coincidences |
| hot path | none | one cold-branch predicate on the DMA arm path |

**DECISION CRITERION (deliberately written down BEFORE the answer is known):** Option A is viable **iff** every
`doca_task_submit*` site is behind `CanSubmit` or `CanDrainHostRing`. That is an **assertion of absence** — the
exact class of claim that produced *all three* of P1-f's wrong calls (§33.10) — so it is being **searched**, not
assumed. **If even one submit escapes, Option B is mandatory.**

### 34.5 ACCEPTANCE — and the number we do not have

P2-i is, like P1-f, primarily an **enforcement** item: on a healthy shutdown it should change nothing.

1. **The drain reports, every run.** `entered-with N outstanding, converged in K iters / T us`.
   **`N` is currently UNKNOWN and unguessable** — nothing progresses the PE after the loop exits (F9), so
   whatever was in flight when SIGTERM landed is still in flight at destroy. **If N is consistently 0**, P2-i
   proves what today merely *happens* to hold, and the fix is free. **If N > 0**, the current code has been
   destroying DOCA mmaps and `free()`ing descriptors out from under live DMA tasks (F3) on every shutdown, and
   P2-i is a real bug fix. **Either way we learn a fact we do not have today.**
2. **Zero non-convergence ALARMs** on a clean (non-fatalError) shutdown.
3. **A fatalError shutdown must NOT hang and must NOT abort** — it must classify as CANCEL, say so, and exit.
4. Gate (`-c 4`) + 4-role basebackup unchanged; both services SIGTERM cleanly.

### 34.6 Rules earned (before writing a line of code)

> **The `(void)` cast is the `catch {}` of C.** Five DOCA calls on this teardown path discard their return, and
> one of them (`ctx_stop`) has a documented "not done yet" return that the code then contradicts by setting
> `docaContextStarted = false` anyway (F2). **A status you do not read is a status that will be wrong, silently,
> in the direction you assumed.**

> **A drain is only as good as its quiesce, and a quiesce is only as good as its enumeration of PRODUCERS.**
> This file has already paid for that lesson once (F10: an "endless teardown-drain-wait spin"), and F11 shows the
> producer set includes **the completion callback itself**. *A loop that waits for a counter to reach zero, while
> something can still increment it, is not a drain — it is a bet.*

> **A stale `file:line` in a comment does not make its claim false; it makes it UNVERIFIED.**
> `tuple_sink_service_process.c:44718-44723` correctly warns that the PE-drain sum double-counts, and cites
> `homer_service_dpu_dma.c:8803-8804` — which today is two local variable declarations in an unrelated function.
> **The claim turned out to be TRUE (F5), and it was checked, not trusted.** Citation repaired as part of P2-i.

### 34.7 ⛔ SELF-REFUTATION — F7 AND F11 ARE BOTH WRONG, AND F7 IS WRONG IN THE DANGEROUS DIRECTION

**Written within the hour, by continuing to read the file I had just claimed to understand.** §34.2 and the F11
row above are preserved verbatim above, not deleted: the reasoning that produced them is the lesson.

#### ⛔ F7 / §34.2 — REFUTED. My "fix" would have RE-INTRODUCED a deadlock the code records already fixing.

I claimed: *"`engine->fatalError` ⇒ the engine cannot retire anything ⇒ draining it hangs ⇒ classify as CANCEL
and do NOT drain."*

**The code says the exact opposite, in a comment written by whoever fixed this before.**

`homer_service_dpu_dma.c:5575-5584` (`HomerDpuDmaDrainPeInternal`, the `harvestEvenIfFatal` parameter):

> *"The normal steady state (false) refuses to progress a fatally-errored engine, because a fatal error means new
> work must not be started. **But `doca_pe_progress()` is the ONLY site that harvests DOCA completion/error
> callbacks, and refusing to call it once `fatalError` is latched strands every already-submitted task as
> `HOMER_DPU_DMA_TASK_SLOT_SUBMITTED` forever** — which pins `import->inflightTaskCount` above zero and
> **deadlocks the setup close-drain** (CLOSE_ACK is never sent)."*

`homer_service_dpu_dma.c:6535-6538` (`HomerDpuDmaDrainArenaSlot`), even blunter:

> *"`harvestEvenIfFatal=true` is **load-bearing**. … **A teardown drain that will not harvest under fatal is
> therefore a hang, not a drain.**"*

**`engine->fatalError` is a SOFTWARE latch meaning "do not START new work." It does NOT mean the DEVICE is
broken.** Already-submitted DMA tasks still complete normally and still MUST be harvested. P2-i's drain therefore
**must pass `harvestEvenIfFatal=true`**, exactly like P0-d's `HomerDpuDmaDrainArenaSlot` already does — and the
"CANCEL on fatalError" branch I specified in §34.3 step 1 **must be deleted**, because it is precisely the hang.

**How I got it wrong:** I inferred the mechanism from the flag's NAME, and from the runbook's operational
phrasing (*"the engine **dies**"*, *"a run can PASS while the engine dies underneath it"*). Both describe the
**semantic** consequence (no new work is accepted; sessions fail). Neither says anything about whether
**already-submitted physical DMA** still retires — and it does.

> **RULE. An operational description of a failure ("the engine dies") is a statement about SERVICE, not about
> PHYSICS. Do not derive a hardware/lifetime conclusion from a phrase that was written to describe a user-visible
> outcome.** The physical question — *"can already-submitted work still complete?"* — has to be asked of the
> code, separately, every time.

#### ⛔ F11 — REFUTED (the count IS monotone). But what replaces it matters MORE.

I claimed the completion callback at `:10733` **submits a new task**, so `totalInflightTaskCount` can grow during
the drain and the loop has no fixed point.

**It submits an ALREADY-ARMED, ALREADY-COUNTED slot.** Proof, in the same function: on submit failure it calls
`HomerDpuDmaRetireTaskSlot(engine, publishSlot, true)` (`:10738`) — **you cannot retire a slot that was never
armed.** The `+2U`/`+3U` admission checks (`:2038`, `:2211`, `:2415`, `:3541`, `:4096`, `:6997`, …) reserve BOTH
slots up front; the **body** is submitted immediately and the **publish** is submitted later, from the body's
completion callback. This is the ordered two-hop DMA (write payload → write tail), and the second hop must not
start until the first retires.

Two consequences, and the second one is the real design driver:

1. **`inflightTaskCount` counts ARMED tasks, not device-resident tasks.** That is *stronger* than I assumed:
   `totalInflightTaskCount == 0` proves nothing is armed AND nothing is awaiting submission. Good.
2. ⚠ **THE DRAIN MUST NOT STOP ON "NO IMMEDIATE PROGRESS".** After the body retires, the publish is submitted but
   may not complete in the same `doca_pe_progress()` round. **`HomerDpuDmaDrainPeInternal` stops as soon as the PE
   reports zero immediate progress** — it says so at `:5570-5573`, and calls that the *scheduler invariant*
   ("a drain grant is bounded and never spins waiting for future completions"). **Used as P2-i's drain it would
   return with the second hop still outstanding, and we would tear down under it.**

#### ✅ THE PRIMITIVE ALREADY EXISTS, AND IT IS THE OTHER ONE

`HomerDpuDmaDrainArenaSlot` (`:6544`) is the exemplar, and its header comment (`:6528-6542`) is the whole design:

- it **deliberately deviates** from the scheduler's bounded-grant invariant, *"because the resource being awaited
  is physical DMA work"*;
- it is bounded by a **wall-clock deadline, not an iteration count**;
- it passes **`harvestEvenIfFatal=true`**;
- and it already documents re-entrancy safety: *"`HomerDpuDmaTaskMemcpyComplete` never calls service code, so this
  release funnel cannot be reached from a completion callback and cannot recurse into `doca_pe_progress()`."*

> **P2-i is therefore: do what `HomerDpuDmaDrainArenaSlot` does, but for the WHOLE ENGINE rather than one arena
> slot** — spin to `totalInflightTaskCount == 0` (F5) under a wall-clock bound, harvesting even under fatal.

**§34.3 step 1 (DECIDE/CANCEL-on-fatal) is DELETED. §34.3 step 2 (QUIESCE) is DEMOTED** from "the load-bearing
part" to a defensive backstop — with arming stopped by the service loop having already exited, the count is
monotone non-increasing. It is still worth having (a `shuttingDown` flag makes that a *fact* rather than a
property of the caller), but it is no longer what the correctness rests on. **What the correctness rests on is
the LOOP SHAPE** — and I had it exactly inverted.

#### The rule, and it is the same rule three times now

> **This is the THIRD time in two days that a claim of mine about this file was an ASSERTION ABOUT CODE I HAD NOT
> READ, dressed as an inference from something I had.** P1-f: "nothing enforces the same-QP premise" (it did).
> P1-f: "I3 needs hardening" (I3 does not execute). P2-i: "a fatal engine cannot drain" (it can, and must).
> **Each cost exactly one `grep` to check. Each was committed before I checked.**
>
> **The pattern is now unmistakable: I write the spec, and THEN discover the mechanism.** Reverse it. The cheap
> question — *"which function already does this, and what does its comment say?"* — found the answer in both
> directions here, and it took ninety seconds.

### 34.8 P2-i — WHAT SHIPPED (7 changes, all in `homer_service_dpu_dma.c`)

| # | change | why |
|---|---|---|
| 1 | `engine->shuttingDown` latch, gated **inside `HomerDpuDmaFindFreeTaskSlot` / `...Excluding`** | that pair is the **single acquire choke point** for all 8 arm sites, so "you cannot submit a task you could not arm" holds **by construction**. Gating the submit *predicates* instead would have rested on the NEGATIVE claim "no submit escapes them" — the class of claim this project keeps getting wrong. |
| 2 | new `HomerDpuDmaQuiesceAndDrainForDestroy()` | phases 2+3. Modelled on **`HomerDpuDmaDrainArenaSlot`**, not on `HomerDpuDmaDrainPeInternal`: wall-clock bound (5 s), **not** an iteration cap; `harvestEvenIfFatal=true`; spins on `totalInflightTaskCount == 0`. |
| 3 | new `HomerDpuDmaLogTaskSlotCensus()` | a timeout that prints *"3 outstanding"* is a number; one that prints *"1× `BYTE_RING_WRITE_PRODUCED_TAIL_PUBLISH` **PREPARED**"* is a diagnosis. `SUBMITTED` ⇒ the device never completed it. `PREPARED` ⇒ armed, counted, never submitted — a successor pin. **DOCA-only** (its two name helpers are inside the `#ifdef`), with a non-DOCA stub. |
| 4 | 🐛 **BUG FIX** at the `:10733` submit-failure branch: abort the prepared **grandchild** before retiring | the byte-ring write is a **3-task** chain; retiring only the middle hop left the terminal publish `PREPARED` **and counted forever** — a permanent silent pin of the very counter the drain waits on. The correct helper (`HomerDpuDmaAbortPreparedDpuToHostPublish`, which already recurses for exactly this chain, `:10868-10872`) existed; this branch just never called it. **Found by the adversarial accounting audit, not by me.** |
| 5 | `HomerDpuDmaDestroy` calls the quiesce+drain before the DOCA teardown | closes V3 (§2.2). Its own block comment used to say the drain policy was missing. |
| 6 | `HomerDpuDmaDestroyDocaLifecycle` **REORDERED**: `doca_ctx_stop` (return **checked**; `IN_PROGRESS` ⇒ pump the PE to `DOCA_CTX_STATE_IDLE`, bounded) now runs **before** the host-mmap-import clear | fixes **F3**. The old order destroyed the DMA **targets** (import mmaps, descriptors, `memset`) *before* stopping the DMA **contexts**. |
| 7 | `doca_buf_inventory_stop/destroy` + `doca_pe_destroy` returns **checked and ALARMed** | fixes **F2**. These two are the **canaries**: the inventory refuses to stop while a `doca_buf` is checked out; the PE refuses to be destroyed while a ctx is still connected. A clean shutdown must see both succeed — and we were discarding the answer. |

**Not fixed, deliberately (recorded, not hidden):** the prepared-successor pins on the **validation-failure** branches (`:10667-10730`, four `fatalError = true; return false;` sites) leak the same way. Different mechanism (validation, not submit failure), and they poison the engine regardless. The drain now **reports** them (census prints `PREPARED`) instead of hanging silently. Fix separately.

#### 34.8.1 First evidence — the host DOCA smoke, BEFORE any DPU deploy

`build/homer/homer_service_dpu_dma_doca_smoke --dev-pci 0000:21:00.0` (host, `-DHOMER_DPU_DMA_WITH_DOCA`):

```
homer DPU DMA: teardown drain COMPLETE: entered with 0 outstanding task(s),
               converged after 0 progress round(s); free_slots=3072/3072 fatal=false
homer DPU DMA: DOCA context state 2 -> 0      <-- RUNNING -> IDLE, x3
homer_service_dpu_dma_smoke: ok
```

What this **does** prove: the drain is wired in and convergent; the slot-array cross-check (`3072/3072`) **agrees** with the class counters (independent of the `> 0` guard at `:11439` that can mask drift); the contexts are **observed** reaching IDLE rather than merely asserted to have; and **all five previously-`(void)`-discarded DOCA statuses returned `DOCA_SUCCESS`** — a fact nobody had ever been in a position to know. Zero ALARMs. The non-DOCA build of the same smoke also passes, so the `#else` stub path compiles *and* runs.

⚠ What it does **NOT** prove, and must not be read as proving:

- **`entered with 0 outstanding`.** The drain drained *nothing*. It is proven harmless, not proven effective. **The number we actually want — N at SIGTERM in the live DPU service — is still unknown** (§34.5).
- **The `IN_PROGRESS` → pump-to-IDLE branch (change 6) never executed**, because with 0 outstanding, `doca_ctx_stop` returns `DOCA_SUCCESS` immediately. That branch is therefore **validated by construction, never by execution** — the same disease §22.10.1 records for P0-b's `RETIRING`, and it must be labelled the same way rather than rounded up to "covered".

### 34.9 ⛔ ADVERSARIAL REVIEW OF THE P2-i DIFF — SIX FINDINGS, FOUR OF THEM MINE

The reviewer **read the DOCA headers** (`/opt/mellanox/doca/include/{doca_ctx,doca_mmap,doca_buf_inventory,doca_pe}.h`),
so its DOCA claims are VERIFIED, not inferred. Every load-bearing one was re-checked by me before acting.
**It caught a hang I was about to ship.**

| # | finding | verdict | whose |
|---|---|---|---|
| **R1** | **Blocking the in-callback resubmit (`:10733`) strands counted `PREPARED` slots** ⇒ `inflightTaskCount` never reaches 0 ⇒ **the drain hangs.** My §34.3 spec said to gate exactly that site. | **the fix would have BEEN the bug** | **mine** |
| **R2** | `doca_ctx_get_state()` **failing** took the same `break` as reaching **IDLE**. | **"I don't know" laundered as "yes"** — the exact disease P0-f exists to cure, in code I wrote *to enforce my own rules* | **mine** |
| **R3** | My comment *"these two functions are THE single choke point … by construction"* is **FALSE**: the byte-ring 3-chain acquires its **third** slot with a **raw `engine->taskSlots[]` scan** inlined at the call site (`:3936-3951`). The quiesce worked only **transitively** (the two gated calls run first and return NULL). | **REFUTED** | **mine** |
| **R4** | My "independent second certificate" (`freeTaskSlotCount == taskSlotCount`) **is not independent** — both counters are maintained in the SAME retirement funnel, so a drift corrupts both in lock-step. It asks the funnel to grade its own homework. | **REFUTED** | **mine** |
| **R5** | I named the **wrong canary**. `doca_buf_inventory_stop` merely *"stops element retrieval"* and *"does not have to be called before destroy"* (`doca_buf_inventory.h:116-132`). **`doca_buf_inventory_destroy`** is the one returning `DOCA_ERROR_IN_USE` *"if not all allocated elements had been returned"* (`:73-90`). | **REFUTED** — checking the wrong call *and believing it* is worse than checking neither | **mine** |
| **R6** | The byte-ring 3-chain's **terminal publish** stays `PREPARED` **and counted forever** when the middle hop's submit fails. Also five (not four) validation-failure branches leak the same way. | **REAL BUG** | pre-existing |
| **R7** | **The failure path still destroyed what it could not prove was safe to destroy.** `doca_mmap_destroy` returns **`NOT_PERMITTED`** while any `doca_buf` points into it (`doca_mmap.h:104-122`); `doca_buf_inventory_destroy` returns **`IN_USE`** while an element is checked out. So the destroys were **rejected by DOCA anyway** — and then we `free()`d the memory. | **REAL** | **mine** |

#### 34.9.1 THE RELEASE GATE — an improvised design decision, recorded as such

**Decision (owner-absent; flagged to the owner in the same turn):** *if any DOCA context cannot be **proven** IDLE,
**abandon the teardown entirely** — destroy nothing, free nothing, ALARM loudly, and let process exit reclaim.*
`HomerDpuDmaDestroyDocaLifecycle` now returns `bool`; on `false`, `HomerDpuDmaDestroy` skips
`HomerDpuDmaFreeStage5Scaffolding()` **and** `free(engine)`, and says so.

**Why "the process is exiting anyway" is NOT a licence — and this is the load-bearing bit:**

> **`free()` IS NOT INERT.** glibc can return a large block to the kernel via `munmap()`, and **a device still
> writing into an unmapped page raises an IOMMU fault, not a harmless scribble.** Leaking at exit costs nothing.
> Destroying under live DMA does.

And it is what §9.4 has always said Scenario E *means* — *"destroys … and then **ABANDONS** software owners,
**explicitly recording what it discarded**"*. So this is read as **plan-consistent, not a deviation**: the previous
code (and my first draft) dressed a cancellation up as a release, which is precisely the substitution this audit
exists to end.

#### 34.9.2 R3's real lesson, and the fix that makes the claim TRUE

The raw scan was **in my own grep output** (`3879: candidate->state == HOMER_DPU_DMA_TASK_SLOT_FREE`) hours before
I wrote the comment claiming there was *"no need to enumerate the submit sites and hope."*

**The fix was NOT to soften the comment.** `HomerDpuDmaFindFreeTaskSlotExcludingTwo()` now exists, gated on
`shuttingDown` like its two siblings, and the raw scan is gone. There are **three** acquisition points and the
claim is now true rather than lucky.

> **RULE. A comment asserting "by construction" while the property actually holds BY ACCIDENT is worse than no
> comment at all** — the next person adds a fourth acquisition site and trusts it. If you cannot make the claim
> true, do not make the claim.

#### 34.9.3 N5 — a hole I found in my OWN fix while briefing the re-review

Writing the re-review brief, I asked: *"could a **STOPPING** context be skipped by the `continue` and thereby be
silently certified idle?"* — and then answered it myself instead of waiting.

The skip condition was `if (ctxs[i] == NULL || !classes[i].docaContextStarted) continue;`. But
`docaContextStarted = (nextState == DOCA_CTX_STATE_RUNNING)` — **so it is false for STOPPING too.** I had deleted
the *comment* that trusted that flag, and left the *code* that trusted it. A STOPPING context would have fallen
through the loop **without being stopped and without clearing `allContextsIdle`** — a false certificate, in the
gate built to prevent false certificates.

**Fix: stop asking the flag; ASK THE CONTEXT.** `doca_ctx_get_state()` distinguishes IDLE / RUNNING / STOPPING
directly. "Never started" (IDLE) now skips honestly; everything else goes through stop-and-prove.

> **RULE. When you discover that a flag lies, `grep` for EVERY reader of it — not just the one you were looking at.**
> Deleting the comment that trusted it is not the fix.

### 34.10 RE-REVIEW OF THE CORRECTED DIFF — N1/N2/N3 pass, and it found ONE MORE, also mine

Corrections introduce new claims, and those are unreviewed. So the corrected diff went back. **N2 and N3 passed
(*"no second raw acquisition remains"* — the three-helper choke point is now real), N1 passed for the current call
graph, and the reviewer confirmed my own N5 self-fix had already landed in the working tree.** Then it found this:

#### ⛔ THE PARTIAL-INIT PUMP CANNOT PUMP — a regression I introduced

**VERIFIED, and it is exactly the shape of every other bug in this stage: a helper that silently declines.**

- `HomerDpuDmaDrainPeInternal` **returns without ever calling `doca_pe_progress()`** whenever
  `!engine->docaEnabled` (`:5704`).
- `engine->docaEnabled = true` is set **only after EVERY context has started** (`:15356`).
- My stop-flush loop pumped **through** `HomerDpuDmaDrainPeInternal`.

⇒ On a **PARTIAL initialisation failure** — precisely the path that reaches teardown with a context in
`STARTING`/`STOPPING` — the pump would **never progress the PE at all**. The loop would burn its full 5 s
deadline, conclude the context could not be proven IDLE, and **abandon (= leak the engine) on what is merely a
config typo at startup.** Conservative rather than unsafe, but a real regression: a bad `HOMER_SERVICE_DOCA_DEV_PCI`
would now cost 5 seconds and leak.

**Fix:** call `doca_pe_progress(engine->pe)` **directly** in the stop-flush loop. The `docaEnabled` gate exists so
we never touch DOCA when DOCA is not configured; but by that point we have already *proven* a context exists and
is stoppable, so the PE exists and pumping it is precisely correct.

> **RULE. When you reuse a helper on a teardown path, re-read its EARLY RETURNS against the state teardown is
> actually in.** Every guard in this codebase was written for the steady state. `!docaEnabled` means "DOCA is not
> configured" during startup and "DOCA never *finished* configuring" during teardown — the same predicate, two
> different meanings, and only one of them is the one the helper was written for.

#### ✅ THE DRAIN'S VERDICT IS NOW A GATE, NOT A DIAGNOSTIC

The reviewer also caught that my slot-disagreement check **ALARMed and then printed `COMPLETE` and proceeded
anyway** — a check that tells you it failed and then shrugs.

It is now an input to the release gate: `HomerDpuDmaQuiesceAndDrainForDestroy` returns `bool`, and the release
requires **`allContextsIdle && drainedCleanly`**. The two are genuinely independent reasons to abandon:

| condition | what it means | why release is unsafe |
|---|---|---|
| `!allContextsIdle` | the **device** may still be executing a task | freeing its target is the **IOMMU-fault** case |
| `!drainedCleanly` | a task slot is stranded (`PREPARED`) | it still **owns its `doca_task` and `doca_buf`s** (released only in the retirement funnel), so `doca_mmap_destroy` → **`NOT_PERMITTED`** and `doca_buf_inventory_destroy` → **`IN_USE`**. The release *cannot* succeed — it can only fail quietly. |

**Every context IDLE is NOT sufficient.** That was the gap: no live DMA does not imply no held buffers.

#### Accepted, not fixed (recorded)

- **`HomerDpuDmaDestroy` is still `void`** — callers cannot distinguish *destroyed* from *abandoned*. Safe today
  because **every** caller is an exit path (verified: 13 call sites — the two `Create` failure paths, five smokes,
  and the service exit), but it is a call-graph property, not an API contract. If anything ever calls
  `HomerDpuDmaDestroy` and continues, this must become a returned status.
- **`releasedCleanly == true` means "no live DMA", NOT "everything was released".** A failed
  `doca_dma_destroy`/`doca_pe_destroy`/`doca_dev_close` is logged and leaks, but does not revoke the certificate —
  correctly, because the certificate's only job is to license the `free()`. Leaks at exit are free; a `free()`
  under live DMA is not.
- **`shuttingDown` is a plain `bool`** and the acquisition helpers return unreserved pointers. Fine while the
  service is single-threaded (it is); it is **not** a thread-safe quiesce, and P7b's pooling must not assume it is.

### 34.11 ⚠ CORRECTION TO §22.10.1 — THE TEST IT PRESCRIBES CANNOT WORK

§22.10.1 records that P0-b's `RETIRING` branch is *"validated by construction, never by execution"* (`retiring=0`
in every run), and prescribes: *"Driving it needs either **a multi-client run** or an artificially delayed send-CQ
drain."*

**The multi-client half is WRONG, and I was about to fold a `-c 4` gate run into P2-i's validation on the strength
of it.**

`remote_execution_peer_transport_rdma.c:8830-8843` — the branch, in full:

```c
opState->phase = CITUS_REMOTE_EXEC_PEER_CONTROL_OP_COMPLETED;
if (opState->ownerAbandoned)                  /* <-- REQUIRES AN ABANDONED OWNER */
{
    if (opState->sendCompletionRetired)
        TupleSinkServiceReleasePeerControlOp(connectionState, incomingMessage->opIndex);
    else
        opState->phase = CITUS_REMOTE_EXEC_PEER_CONTROL_OP_RETIRING;   /* <-- the untested branch */
}
```

**`RETIRING` is gated on `ownerAbandoned`.** It is the ZOMBIE path: the owner gave up — timed out, aborted — and
*then* the response arrived, and the send CQE had not yet retired. It has **nothing to do with concurrency.**

⇒ **A healthy run NEVER abandons a control op, at ANY client count.** `-c 4`, `-c 8`, `-c 16` all report
`retiring=0` **by construction**. Running one and reporting "still `retiring=0`, still unexercised" would be a
TRUE statement that proves NOTHING, produced by a test that **could not have passed** — the same disease as the
runbook's retired gate-proof decoy (a check whose success depends on something that never happens).

**What would ACTUALLY exercise it (both are required):**
1. an **abandoned** control op — a client that aborts or times out mid-command; **and**
2. the response beating the send CQE — the rare race §22.6 predicted is essentially never won.

That is **fault injection, not a workload**: e.g. kill the client mid-command *and* stall the send-CQ drain.
(Note condition 1 is exactly the aborted-gate-run state the runbook already warns leaks arena slots — so the
zombie path is reachable, just not by anything we would call a passing run.)

**§22.10.1 is amended: `-c 4` is NOT a test for this. Do not run it and record the result as evidence.**

> **RULE. Before running a test to exercise branch X, READ THE PREDICATE THAT GUARDS X.** A test that cannot
> reach the branch produces a clean-looking negative result, and a clean-looking negative result is indistinguishable
> from a passing one until someone reads the guard. *(I was one command away from filing exactly that.)*

### 34.12 ✅ P2-i VALIDATED (citus `2b37e8701`) — AND IT IS A REAL BUG FIX, NOT ENFORCEMENT

**The measurement the whole stage existed to produce — and the only way to get it was to stop the service while
it was WORKING:**

```
SIGTERM the DPU service MID-TRANSFER (a 23 GB basebackup in flight):

  homer DPU DMA: teardown drain COMPLETE: entered with 3 outstanding task(s),
                 converged after 1 progress round(s); free_slots=3072/3072 fatal=false
```

**N = 3.** Three DOCA tasks live in the device at teardown. **Before P2-i they were simply abandoned:** the old
path destroyed the import mmaps and `free()`d their descriptors and `memset` the import, *then* called
`doca_ctx_stop` and discarded its `IN_PROGRESS`, *then* `doca_dma_destroy` → `doca_pe_destroy` → `free(engine)`
— with DMA still live, writing into memory being handed back to the allocator. **That is V3, and it is reachable
by SIGTERMing a busy service, which is how this thing is normally stopped.**

Convergence cost: **one** progress round.

> ⚠ **EVERY EARLIER READING SAID `entered with 0`** — the host DOCA smoke, the idle-DPU probe, after the gate,
> after two full basebackups, on both DPUs. All of them were taken **after the workload had finished.** The drain
> looked like a no-op and P2-i looked like an enforcement item (a P1-f-shaped "prove what already holds").
>
> **RULE. To measure what a teardown drains, you must tear down DURING the work, not after it.** A quiescent
> system tells you nothing about a quiesce. *(I recorded "P2-i is an enforcement item, N=0" as a finding, twice,
> before it occurred to me to kill the service mid-flight.)*

#### Full validation set (P2-i round 3, both DPUs)

| check | result |
|---|---|
| mid-flight SIGTERM (23 GB in flight) | **`entered with 3 outstanding, converged after 1 round`**, exit **0**, no alarms |
| DPU gate, `-c 1 -t 2000` ×3 | `2000/2000`, **0 failed**, `transport: homer-dpu-command (implies dpu result relay)` |
| gate tps | **285.6 / 291.0 / 299.1** vs band **291.0 / 295.8 / 304.1**. At/marginally below, with a **monotone within-set decline** (299→291→286) ⇒ machine drift after an hour of 23 GB transfers, not a step change. **Re-measure on a rested machine before quoting.** |
| 4-role basebackup ×2 back-to-back | 23.25 GB each, `CLOSE_ACK`, ~44k byte-ring wraps each |
| ALARM sweep (full pattern) | **zero** hits, both DPUs, every run |
| both `#ifdef` configs | compile; DOCA **and** non-DOCA smokes pass |

#### ⚠ THE tps NUMBER FROM THE FIRST VALIDATION WAS A PHANTOM — AND THE BRIEFING ERROR WAS MINE

The first validation reported **404–424 tps** against the **291–304** band and honestly refused to call it a pass
or a regression. **It was measured at `-c 4`; the band is a `-c 1` band** (P1-f's runs were `2000/2000` = 1×2000;
the `-c 4` runs were `8000/8000`). pgbench reports **aggregate** tps, so `-c 4 → ~420` is **~105 tps/client** —
a 3× per-client fall under 4-way concurrency, i.e. the known command/control **scaling** problem, not a change.

**I asked for `-c 4` in order to exercise `RETIRING` — which §34.11 then proved cannot be exercised that way at
all.** So the client count bought nothing and cost a false signal.

> **RULE. A performance band is only a band AT ITS CLIENT COUNT. Record the client count WITH the band, or the
> next comparison is a coin flip.** The band table in `farnet_diagnostics_and_baselines.md` §3.3 does not state
> it — **fixed there.**

#### ⛔ NOT FIXED, AND IT BOUNDS P2-i: AN EXIT PATH THAT REACHES `HomerDpuDmaDestroy` NEVER

**VERIFIED, pre-existing** (P2-i's diff touches exactly ONE file, `homer_service_dpu_dma.c`; `main()` and
`TupleSinkServiceResetSession` live in `tuple_sink_service_process.c`, untouched):

```
[farnet1 DPU]  RAW EXIT STATUS = 1        teardown drain lines: 0
               LAST LINE: refusing to reset peer CLIENT_SQL_SESSION before
                          command-mailbox writers quiesce  session=1 ... reset_complete=0
[farnet0 DPU]  RAW EXIT STATUS = 0        teardown drain lines: 1   (converged)
```

**The BACKEND-side DPU service exits(1) out of the session-reset path and NEVER REACHES `HomerDpuDmaDestroy`.**
No drain, no `doca_ctx_stop`, no release — **the DMA engine is not torn down at all.** Exit **1**, not a signal:
a deliberate failure exit, not a crash.

**Reproduces when the service is SIGTERM'd shortly after a gate run**, while session 1's command-mailbox writers
are still un-quiesced (`current_sequence=14004` = the warmup's 2000 txns × 7 statements). **Nobody saw it because
nobody SIGTERMs the service right after the gate** — the first validation killed it after the basebackup *and*
the smoke, by which time the sessions had drained, and it saw exit 0.

**This is exactly what this audit exists to find: an exit path that releases nothing.** It also bounds P2-i
honestly — **the drain is correct where it runs, and here it does not run.** Tracked as the next item.

### 34.13 THE BASEBACKUP STALL IS **NOT** P2-i — settled by a controlled A/B

The first validation's most alarming result was a **reproducible basebackup stall** on a clean machine (runs 2
and 3 hung at `waiting for checkpoint to complete`, walsender pinned at 100% CPU, unresponsive even to
`pg_ctl stop -m fast`). Its run 1 had passed. **Bisected:**

| binary | run 1 | run 2 |
|---|---|---|
| pre-P2-i (`91482c840`) | ✅ 7 s, 23.25 GB, CLOSE_ACK | ✅ 7 s, 23.25 GB, CLOSE_ACK |
| **P2-i (round 3)** | ✅ 7 s, 23.25 GB, CLOSE_ACK | ✅ 7 s, 23.25 GB, CLOSE_ACK |

**The stall did not reproduce on either binary under a clean controlled procedure. P2-i is exonerated.**

**⚠ AND THE BISECT SCRIPT'S OWN VERDICT LINE LIED.** After the first arm it printed *"RUN 2 PASSED ON PRE-P2-i
==> P2-i IS IMPLICATED. DO NOT SHIP."* — because that was the branch I wrote before I had the second arm. **Had
I stopped there I would have reverted a correct change.** A verdict line is not a verdict; it is a *guess you
wrote earlier*.

**What the failing environment had that the clean one did not:** the validator **built and ran the DPU TCP
transport smoke between basebackup runs 1 and 2** — and **that smoke server binds port 9727, the DPU service's
own setup-listener port.** It also found the DPU's smoke binary was **x86-64**, i.e. that server had never
actually run there before. **And the documented DPU reap recipe matches only `*/citus_tuple_sink_service*` — a
leftover `homer_dpu_tcp_transport_smoke` holding 9727 is INVISIBLE to it.**

**INFERRED, not verified.** Recorded as the leading hypothesis for a separate investigation, not as a diagnosis.

---

## §35 — P2-k: `TupleSinkServiceResetSession` calls `exit(1)` AND DESTROYS THE ENTIRE TEARDOWN

**Status: PLAN (2026-07-14). Owner-selected as the next item.** Found by P2-i's validation — specifically, by
being the first person to SIGTERM the DPU service *immediately after a gate run*.

### 35.1 THE MECHANISM — VERIFIED, localized to a line

`tuple_sink_service_process.c:24166-24179`, inside **`TupleSinkServiceResetSession`** (`:24120`):

```c
if (!TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister(sessionState))
{
    fprintf(stderr, "tuple-sink service: refusing to reset peer CLIENT_SQL_SESSION before "
                    "command-mailbox writers quiesce session=%llu ... peer_close=%u reset_complete=%u\n", ...);
    exit(1);                                    /* <<<<<< THE BUG */
}
```

The guard (`:24526`) returns **false** exactly when:

> the session is a **backend-side `clientSqlPeerReceiver`**, its **peer binding is open**, its **command-mailbox
> MR exists**, and **NEITHER `peerWritersQuiesced` NOR `connectionResetComplete`** is set.

**⚠ AT SIGTERM THAT IS THE UNIVERSAL STATE FOR ANY STILL-BOUND BACKEND-SIDE SESSION.** The peer never sent a
close and the connection was never reset — **because we are being KILLED, not closing gracefully.** Observed
exactly: `peer_close=0 reset_complete=0`.

⇒ **The NORMAL shutdown of the backend-side DPU service dies at `exit(1)`, and `HomerDpuDmaDestroy` — the P2-i
drain — NEVER RUNS.** No drain, no `doca_ctx_stop`, no release. Confirmed: `RAW EXIT STATUS = 1`,
`teardown drain lines: 0` on the backend-side DPU; `0` / `1` on the frontend-side one.

**SIGTERM is not an exotic path. It is the ONLY way this service is stopped** (`KeepTupleSinkServiceRunning`,
`:3861`; handlers at `:23565-23566`).

### 35.2 WHY IT IS BACKWARDS

| | what it costs |
|---|---|
| **refusing to deregister the MR** — CORRECT (Scenario C: the remote peer may still write into it) | a **harmless leak at process exit** |
| **`exit(1)`** | **the DMA engine is never drained** — the actual use-after-free |

> **It trades a harmless leak for a real one.** A defensive guard that makes the system strictly LESS safe —
> the same shape as the `fatalError` inversion (§34.7), the "I don't know ⇒ IDLE" break (§34.9/R2), and the
> "gate all submits ⇒ deadlock" (§34.9/R1). **Four times in one stage.**

### 35.3 THE FIX — AND THE TEMPLATE IS TWENTY LINES AWAY

`TupleSinkServiceResetPeerOpenFailureIfSafe` (`:24543`) handles **the same predicate** and states the correct
policy in its own comment:

> *"Peer OPEN/spawn failure must not destroy a command mailbox while its remote writer can still use the
> advertised MR. Safe cases retain the historical immediate reset; **unsafe cases fence backend admission,
> publish a loud state, and leave final reset to peer-close/connection-reset lifecycle ownership.**"*

**It does not exit.** One call path already does the right thing; the reset function itself kills the process.

**THE CHANGE — refuse-and-FENCE, not refuse-and-DIE:**

1. keep the guard and the loud message;
2. **DELETE the `exit(1)`**;
3. **fence the session** exactly as `ResetPeerOpenFailureIfSafe` does — `dpuBackendTeardownStarted = true`,
   `backendLoopActive = false`, `currentCommandState/postCommandState = FAILED`,
   `backendReuseState = HOMER_BACKEND_REUSE_FORBIDDEN`;
4. **return WITHOUT resetting.** The session stays `active` and bound, so nothing reuses its slot — which is
   precisely the documented "leave final reset to peer-close/connection-reset lifecycle ownership".

**At shutdown:** the session's MRs leak (free — the process is exiting), the reset loop moves to the next
session, and **`HomerDpuDmaDestroy` runs and drains.**

### 35.4 CALLER AUDIT — the fix needs NO call-site changes (VERIFIED)

All 8 callers of `TupleSinkServiceResetSession` are "reset and move on": `:21047`, `:21257` (post-command
reset), `:24263` (idle-retire scan), `:24437` (idle-retire after peer response), `:24551`
(`ResetPeerOpenFailureIfSafe` — **already checks the same predicate first, so it can never trigger the guard**),
`:27420`, `:27433` (freshness reclaim), and **`:48838` (the SHUTDOWN loop)**.

**None checks a return value** (the function is `void`) and **none does anything afterwards that depends on the
reset having happened.** So refuse-and-fence is safe at every site, with zero call-site edits.

### 35.5 ⚠ THE ONE IMPLEMENTATION TRAP — the refusal message WILL SPAM

`:24263` is a **per-pass idle-retire scan**. Once a session is fenced but still `active`, that scan will call
`ResetSession` on it **every service pass**, and the refusal `fprintf` has **no rate limit**. On the hot loop
that is millions of lines — *"a diagnostic that can fill a disk is a bug"*.

**Two things are required, not one:**
- **latch the message** — log the refusal only on the FIRST refusal per session (`dpuBackendTeardownStarted` is
  the natural one-shot); and
- **make `TupleSinkServiceSessionShouldRetireWhenIdle` return FALSE for a fenced session**, so the scan stops
  re-attempting a reset it can never complete. (Settle this while implementing; it is the difference between a
  fence and a busy-wait.)

### 35.6 ACCEPTANCE — and it is self-proving

> **SIGTERM the backend-side DPU service immediately after a gate run.**
> **Today:** `RAW EXIT STATUS = 1`, `teardown drain lines: 0`.
> **After P2-k:** exit **0**, and a `teardown drain COMPLETE` line — **and we expect `N > 0`**, because the
> still-bound session's DMA is exactly what is outstanding.

**That single test proves BOTH the fix AND P2-i's drain, on the path that matters most.** Note the ordering was
load-bearing: **P2-i had to land first.** Without the drain, continuing past the refusal would tear the DOCA
engine down with tasks in flight — i.e. the "fix" would have turned a loud `exit(1)` into a silent
use-after-free.

### 35.7 Rule earned (and it is the fourth instance today)

> **A GUARD THAT CANNOT PROVE SAFETY MUST REFUSE THE UNSAFE ACT — NOT ABORT THE SAFE ONES AROUND IT.**
> `exit(1)` in the middle of a teardown does not prevent the hazard it names; it prevents every *remaining*
> teardown step, and those steps were the ones actually holding the line.

### 35.8 ⛔ THE OBVIOUS BETTER FIX — A TEARDOWN REORDER — IS **DEAD**, AND THE REASON IS THE REAL FINDING

Before implementing the fence, I asked the question §35 should have asked first: **"what makes this guard PASS,
and does the teardown ever DO that?"**

**It does — and it does it in the wrong order.** The two ways the guard can pass (`:24534`):

| flag | set by | when |
|---|---|---|
| `peerWritersQuiesced` | `TupleSinkServiceApplyClientSqlPeerLifetimeCompletion` (`:24521`) | the peer sent a semantic **`CLIENT_SQL_SESSION_CLOSE`** |
| `connectionResetComplete` | `HomerServiceMarkClientSqlPeerSessionsResetComplete` (`:29705`), from the **`onResetComplete` observer** | the RDMA connection is **physically torn down** — its own log line says *"command mailbox **quiesced by connection reset**"* |

And `TupleSinkServiceDestroyPeerTransportState` (`remote_execution_peer_transport_rdma.c:10034`) calls
`TupleSinkServiceResetPeerConnection(..., force=true, "transport-destroy")` on **every** active connection — which
**fires that very observer** (`NotifyPeerConnectionResetBegin`/`...Complete`, `:6474`/`:6492`).

```
:48832   reset sessions            <-- guard FAILS (writers not quiesced) -> exit(1)
:48843   DESTROY PEER TRANSPORT    <-- *THIS* is what would have quiesced them
:48846   HomerDpuDmaDestroy        <-- never reached
```

**The teardown performs the quiescing step AFTER the step that requires it.** So the obvious fix is a reorder:
destroy the peer transport first, let the observer set `connectionResetComplete`, and the guard passes
**honestly** — MRs properly deregistered, **no leak, no fence.**

#### ⛔ AND IT CANNOT BE DONE, BECAUSE THE TEARDOWN **FUSES** TWO OPERATIONS THAT MUST BE SPLIT

**`remote_execution_peer_transport_rdma.c:5715` — `ibv_dealloc_pd(connectionState->protectionDomain)`.**
**The protection domain is PER-CONNECTION, and it is deallocated INSIDE `TupleSinkServiceResetPeerConnection`** —
the same function whose observer would set the flag.

> **The thing that QUIESCES the writer is the same thing that DESTROYS THE PD THE MRs LIVE IN.**

Reset the connections early and the session-reset loop would then call `ibv_dereg_mr` on MRs whose **PD has
already been deallocated** — trading a loud `exit(1)` for a genuine use-after-free. **The current order is
CORRECT for MR/PD lifetime and WRONG for the quiesce flag, and no ordering satisfies both.**

**⇒ REJECTED. Recorded, not silently dropped.**

#### ✅ WHAT THIS MEANS FOR THE FIX (and it CONFIRMS the fence — for a reason, not by default)

The fence's "leak" is not a wart, it is the **only available outcome**, and it is free:

1. session reset refuses → **fence, return** (MRs stay registered);
2. `DestroyPeerTransportState` → destroys the QPs (**the peer physically cannot write any more**), fires the
   observer, then `ibv_dealloc_pd` → **fails `EBUSY`** because the MRs are still registered → the PD leaks;
3. **`HomerDpuDmaDestroy` RUNS AND DRAINS** ✅;
4. process exits → the kernel reclaims the MR, the PD, everything.

**That is §9.4's Scenario E, exactly: cancel, abandon, and RECORD what was discarded. Leaking at process exit is
free. Skipping the DMA drain is not.**

#### 🔭 THE STRICTLY-CORRECT FIX, recorded as FUTURE WORK (not now)

**Split `TupleSinkServiceResetPeerConnection` into QUIESCE and RELEASE:**
- **quiesce** — destroy the QP (⇒ the remote writer physically cannot write) and fire `onResetComplete`;
- **release** — `ibv_dealloc_pd` and the rest.

Then the teardown becomes: quiesce all connections → reset sessions (**guard passes honestly, MRs deregistered
cleanly**) → release the connections → `HomerDpuDmaDestroy`. **No leak at all.**

This is a real refactor of the RDMA connection teardown and it is **also exactly what P7b (connection pooling)
will need** — pooling must be able to quiesce a connection's writers without destroying its PD. **Do it there,
not here.**

> **RULE. "What makes this guard PASS, and does the system ever DO that?" is the FIRST question, not the last.**
> Asking it here found a better fix in ten minutes — and then found, in ten more, exactly WHY the better fix is
> impossible today. **Both halves are worth more than the patch.** *(Third time this stage that establishing the
> mechanism first turned out to matter more than the fix: §34.7's `fatalError`, defect B's `knownExpectedWork`,
> and now this.)*

### 35.9 ⛔⛔ THE §35 PLAN WAS WRONG IN **SIX** WAYS — AND MY "FIX" WOULD HAVE PERMANENTLY WEDGED THE SESSION

**Adversarial review of the P2-k plan, 2026-07-14. Every finding VERIFIED by me against the code before
acceptance. This is the worst review of the stage and all six defects are mine.**

| # | my claim in §35 | reality |
|---|---|---|
| **1** | *"8 callers of `TupleSinkServiceResetSession`"* (grep) | **26** (clangd). A `grep` count is not a caller audit. |
| **2** | cited `:27420` as a caller | **it is inside a preserved COMMENTED-OUT block** (`:27411`). The live call is `:27433`. |
| **3** | *"no caller assumes the reset succeeded"* | **FALSE.** `TupleSinkServiceAllocateSession` (`:24232`) calls reset at `:24263` and **unconditionally returns that slot** at `:24268`; `CreateSession` then `memset`s it (`:24281`). Safe **today** only because the reclaim candidate must pass `ShouldRetireWhenIdle` (`:24260`), which for peer receivers is **the same quiescence predicate** — a **cross-function invariant, not a contract.** |
| **4** | *"the refusal will SPAM from the per-pass idle-retire scan at `:24263`; fix by making `ShouldRetireWhenIdle` return false for a fenced session"* | **INVENTED, BOTH HALVES.** `:24263` is the **table-full FALLBACK** inside `AllocateSession`, reached only after the free-slot scan fails — **not** a per-pass scan. **And `ShouldRetireWhenIdle` ALREADY returns false for the unsafe state** (`:24366` *is* `peerWritersQuiesced \|\| connectionResetComplete`). **My prescribed change would have BROKEN legitimate final retirement.** |
| **5** | *"fence and **return without resetting**"* | **⚠ THE WORST ONE — see below.** |
| **6** | §35.8: *"the teardown reorder is IMPOSSIBLE — the session reset would `ibv_dereg_mr` against a deallocated PD"* | **FALSE.** `TupleSinkServiceInvalidateRegisteredMemoryRegions` (`remote_execution_peer_transport_rdma.c:5030`) **deregisters every registered MR and NULLs the pointer BEFORE** `ibv_dealloc_pd` (`:5713`), and `DeregisterPeerMemoryRegionRdma` (`:10214`) is **NULL-safe** afterwards (`:10246` frees only the handle). **The reorder is viable. I rejected it on a premise I never checked.** |

#### ⛔ #5 — MY FIX WOULD HAVE CREATED A PERMANENT WEDGE. STRICTLY WORSE THAN THE `exit(1)`.

**`TupleSinkServiceResetSession` MUTATES BEFORE ITS OWN GUARD.** Between the entry (`:24120`) and the guard
(`:24166`) it already:

- clears session pending state (`:24131`),
- clears the deferred peer-client completion + remote-command terminal (`:24150`, `:24151`),
- **retires the session's send completions** (`:24161`),
- **DISABLES `dpuPeerCommandLandingActive`** (`:24164`). ⚠

And `HomerServiceDpuLandOnePeerCommand` (`:43270`) **REQUIRES `dpuPeerCommandLandingActive`** (`:43297`) — its
own comment says it *"must still consume/reject peer mailbox records for the lingering session."*

> **⇒ "Fence and return" would leave the session with peer-command landing DISABLED — so the peer's later
> `CLIENT_SQL_SESSION_CLOSE` COULD NEVER LAND — so `peerWritersQuiesced` could never be set — so the session
> could NEVER quiesce and NEVER be retired. A permanent wedge holding its MRs and its arena slot, forever.**

**It replaces a loud `exit(1)` with a silent, permanent leak.** *Fifth time this stage that a "fix" of mine
would have BEEN the bug.*

#### ✅ THE CORRECTED FIX — and it is now SMALLER, not bigger

1. **MOVE THE GUARD TO THE VERY TOP of `TupleSinkServiceResetSession`, before ANY mutation.** This is not a
   refinement; it is the whole fix. A guard that fires *after* the destructive half of its own function has run
   cannot "refuse" anything — it can only decide how to die.
2. **DELETE the `exit(1)`.** On refusal: log through a **DEDICATED one-shot latch** — **NOT**
   `dpuBackendTeardownStarted`, which ordinary selected-DPU teardown already sets before reset (`:43556`) and
   which would therefore **suppress the very first refusal** — apply the fence, and **return `DEFERRED`**.
3. **Change `TupleSinkServiceResetSession` from `void` to an outcome.** `void` is exactly why defect #3 exists.
4. **Handle the outcome at the two callers that need it:** the **allocator** (`:24263`/`:24268` — it must NOT
   hand out a slot whose reset was deferred) and **close** (`:40399` — it prepares a SUCCESS response at
   `:40393` and must be able to tell a deferred reset from a completed one). **Shutdown may deliberately ignore
   `DEFERRED` and continue** — that is the entire point.
5. **LEAVE `ShouldRetireWhenIdle` ALONE.**

**With the guard at the top, the refusal is now harmless:** landing stays ACTIVE, so the peer's close can still
land, `peerWritersQuiesced` gets set, and the session retires **through the normal lifecycle** — which is
precisely the documented *"leave final reset to peer-close/connection-reset lifecycle ownership."*

#### ✅ AND THE "LEAK" I WORRIED ABOUT DOES NOT EXIST EITHER

At shutdown, after the fence, `TupleSinkServiceDestroyPeerTransportState` (`:48843`) resets every connection,
and **that path already `ibv_dereg_mr`s the session's command-mailbox MR** (it is linked into
`connectionState->registeredMemoryRegions`, `remote_execution_peer_transport_rdma.c:10149`). Only the small
**handle** struct leaks, at process exit. **The fence costs essentially nothing.**

**⇒ The teardown reorder (§35.8) is therefore OPTIONAL, not required.** It would let the session reset complete
*fully* rather than being deferred — nicer, and it is still the right long-term shape (and what P7b needs) — but
it is **not** needed for safety. **Recorded; not in P2-k's scope.**

#### Rules earned

> **A `grep` COUNT IS NOT A CALLER AUDIT.** I reported 8; clangd found 26, and one of my 8 was inside a
> commented-out block. CLAUDE.md already says to prefer clangd for symbol-level questions. I used `grep` because
> it was in my fingers.

> **A GUARD PLACED AFTER THE MUTATIONS IT GUARDS IS NOT A GUARD.** It cannot refuse; it can only choose how to
> die. The `exit(1)` was not an over-reaction to an unsafe state — **it was the ONLY remaining option**, because
> by the time the guard ran, the function had already destroyed the state needed to recover. *Move the guard,
> and the `exit(1)` becomes deletable. Leave the guard where it is, and deleting the `exit(1)` creates a WORSE
> bug.* **The placement was the bug; the `exit(1)` was the symptom.**

> **When a review refutes your plan, re-check the sections you wrote EARLIER too.** §35.8's rejection of the
> reorder was built on a premise (`dereg` after `dealloc_pd`) that I never verified and that is false. A
> refutation of §35.3 does not automatically flag §35.8 — I had to go back and look.

---

## §36 — P2-k **ROOT-CAUSED**: the selected-DPU path NEVER RECORDS THE SESSION CLOSE. The `exit(1)` was a SYMPTOM.

**Settled 2026-07-14 after two adversarial review rounds. The reviewer and I converged independently.**
**This supersedes §35 and §35.3/§35.8/§35.9's fix proposals. The guard was never the bug.**

### 36.1 THE ROOT CAUSE — VERIFIED

`TupleSinkServiceApplyClientSqlPeerLifetimeCompletion` (`:24503`) is the **ONLY** setter of
`peerLifetime.peerCloseCommandObserved` and `peerLifetime.peerWritersQuiesced` (`:24518-24521`) — the two flags
`MayDeregister` consults. **It has exactly TWO call sites:**

| call site | path |
|---|---|
| `:21175` | inside `TupleSinkServiceConsumeCompletionMailbox` — the **ordinary / host-service** completion path |
| `:24700` | the **failed-backend cleanup** path |

**NEITHER IS ON THE SELECTED-DPU PATH.** `HomerServiceDpuEgressOneSelectedCompletionEvent` (~`:43819`) calls
`TupleSinkServicePublishPeerClientCommandCompletion` **directly** and clears the selected-session in-flight state
(`:43853`) **without ever invoking the lifetime helper.**

> ### ⇒ ON THE SELECTED-DPU PATH — **THE GATE, THE ONLY WORKLOAD THAT MATTERS** — A `CLIENT_SQL_SESSION_CLOSE` IS **NEVER RECORDED**. `peerWritersQuiesced` STAYS FALSE FOREVER. **THE SESSION CAN NEVER QUIESCE.**

**And that is precisely what the failing log said: `peer_close=0 reset_complete=0`.**

**⇒ `exit(1)` WAS DOING ITS JOB.** It correctly refused to deregister an MR whose remote writer it could not prove
had quiesced (Scenario C). **The reason it could never prove it is that the DPU path forgot to record the close.**

This is the classic **"the new path dropped a step the old path had"** — exactly what a migration produces. And
the tell was already in the tree: **P1-f** (citus `91482c840`, landed the day before) added
`peerRemoteWriteTerminalPublished` — a latch for **OUR** writes **to** the peer (`:19529`). **The selected-DPU
path has a latch for one direction and NOTHING for the other.**

### 36.2 THE FIX

**PART 1 — THE ROOT CAUSE (this is the actual fix).**
Make the selected-DPU terminal completion **record the peer lifetime**, exactly as the ordinary path does at
`:21175`. Both `TupleSinkServiceConsumeCompletionMailbox` and the DPU egress must go through **one shared
lifetime-update path** so a `CLIENT_SQL_SESSION_CLOSE` sets `peerCloseCommandObserved` / `peerWritersQuiesced`
on **both** arms.

**⇒ Then the guard PASSES at shutdown, `ResetSession` completes normally, and `HomerDpuDmaDestroy` runs and
drains. No `exit(1)`, no fence, no leak, no wedge.**

**PART 2 — THE BACKSTOP (hardening; keep it even though Part 1 makes the refusal unreachable in the normal case).**
- **Move the guard to the TOP of `TupleSinkServiceResetSession`, before ANY mutation.** ✅ VERIFIED SAFE (M1):
  none of the prologue helpers can touch any of the guard's five inputs, so the verdict is unchanged — **but the
  side effects on refusal are.** A guard placed after the mutations it guards cannot refuse; it can only choose
  how to die.
- **DELETE the `exit(1)`**; return a `DEFERRED` outcome instead of `void`.
- **OMIT the one-shot log latch.** ✅ VERIFIED UNNECESSARY (M5): there is no autonomous loop that re-resets a
  fenced session. *(I invented that trap in §35.5, then invented a second reason to keep it. Delete it.)*
- Handle the outcome at the **allocator** (on `DEFERRED`, **continue scanning later candidates** — do not return
  `NULL`) and at **close** (`HandleCloseSession` CAN reach `DEFERRED` — it has already prepared a SUCCESS
  response at `:40393` and performed clean-close side effects at `:40263`, so **do NOT convert a late `DEFERRED`
  to ERROR**; define OK as *"close accepted; reclamation deferred to connection reset"*, which matches what
  clients already do — they discard local session identity during teardown, `homer_client.c:1751`/`:3718`).
- **`main()`'s shutdown loop deliberately IGNORES `DEFERRED` and continues.** That is the entire point.

### 36.3 RECORDED, EXPLICITLY OUT OF SCOPE

- **⛔ THE TEARDOWN REORDER IS DEAD — for a THIRD reason, and this one is fatal.** §35.8 rejected it on a false
  premise; §35.9 revived it. **It is dead anyway:** `TupleSinkServiceDestroyPeerTransportState` **FREES THE
  TRANSPORT OBJECT** (`remote_execution_peer_transport_rdma.c:10088`), while an ordinary `ResetSession` still
  **dereferences stored connection handles** during send-owner and terminal-demand cleanup
  (`tuple_sink_service_process.c:15423`, `:18425`). **Destroy-then-sweep is a use-after-free.** *(Three separate
  analyses of the same idea, three different answers. The first two were wrong.)*
- **Shutdown `activeSinkCount` reconciliation.** `HomerServiceResetPayloadStreamEntry` (`:25598`), used by the
  shutdown stream loop (`:48822`), **does NOT decrement the parent session's `activeSinkCount`** — that decrement
  lives only in the normal reclaim funnel (`:27393`). So connection-reset completion can set
  `connectionResetComplete` and still **decline** the final reset because the stale count is nonzero (`:29714`),
  stranding MR handles, placement history, the arena binding, mappings, FDs, and **named SHM objects** (unlinked
  only at `:19913`/`:19941`/`:19969`/`:19994`). **⇒ the fence leaks MORE than a handle — it leaks /dev/shm
  objects that survive the process.** My §35.9 "the fence costs essentially nothing" is **REFUTED**.
- **Shutdown-aware send-owner cancellation.** Callback-driven final reset can spend up to **2 seconds per
  deferred session** draining send owners (`:6776`) before cancelling (`:6834`), because the QP/CQs are already
  destroyed.

### 36.4 ACCEPTANCE (unchanged, still self-proving)

> **SIGTERM the backend-side DPU service immediately after a gate run.**
> **Today:** `RAW EXIT STATUS = 1`, `teardown drain lines: 0`.
> **After P2-k:** exit **0** + `teardown drain COMPLETE`.
> **And now, with Part 1, we expect the guard to PASS** — so `ResetSession` completes and there should be **no
> refusal line at all**. If the refusal still appears, Part 1 did not take.

### 36.5 Rules earned — and this is the expensive one

> **THE GUARD THAT FIRES IS RARELY THE BUG. IT IS THE ONLY HONEST COMPONENT IN THE STORY.**
> `exit(1)` refused to deregister an MR it could not prove was safe. It was **right**. I spent two review rounds
> trying to make it stop complaining, and produced, in order: a fix that permanently wedges the session, a
> teardown reorder that use-after-frees, and an invented spam trap whose invented cure would have broken
> retirement. **The guard was the only thing in the whole area behaving correctly — and it was the thing I set
> out to change.**
>
> **Ask what the guard is PROTECTING, and then ask why its premise is false.** The premise was
> `peerWritersQuiesced == false`. **The bug was that nothing on the DPU path ever sets it.**

> **A NEW PATH THAT BYPASSES A HELPER HAS SILENTLY DROPPED EVERY INVARIANT THAT HELPER MAINTAINED.**
> The selected-DPU completion egress publishes the completion directly instead of going through
> `ConsumeCompletionMailbox`. It gained speed and lost the peer-lifetime record. **`grep` for the ONE function
> that sets the flag, and count its call sites against the paths that should reach it** — two call sites, three
> paths, and the missing one is the one we actually run.

### 36.6 ⛔ P2-k v3's PART 1 WAS A **NO-OP**. The helper early-returns on the STALE cached state.

**Third review round. VERIFIED, and it is the most valuable catch of the stage** — this would have been built,
deployed to both DPUs, run, SIGTERM'd, and produced **the identical `exit(1)`**, followed by a cycle spent
hunting for why.

```c
static void TupleSinkServiceApplyClientSqlPeerLifetimeCompletion(TupleSinkServiceSessionState *sessionState)
{
	if (sessionState == NULL || !sessionState->clientSqlPeerReceiver ||
		!TupleSinkServiceCommandStateIsTerminal(sessionState->currentCommandState))   /* <<<< EARLY RETURN */
	{
		return;
	}
	...
}
```

**It reads the CACHED `sessionState->currentCommandState`.** Every write site of that field — `:21120` (the
**host** completion snapshot), `:21547` (PENDING), `:22203`/`:22246`/`:22430` (FAILED error paths) — is on the
**host** arm or an error path. **NONE is on the selected-DPU completion path.** Landing copies only
sequence/kind/flags (`:43399`); completion acceptance only validates and enqueues the completion body (`:43461`).

> **⇒ On the DPU arm `currentCommandState` is NEVER advanced to a terminal value, so a bare
> `ApplyClientSqlPeerLifetimeCompletion(sessionState)` at the DPU egress EARLY-RETURNS AND DOES NOTHING.**

**And the codebase already knew the cache was unreliable here** — `:19031` and `:19213` read
`queuedCompletion == NULL ? sessionState->currentCommandState : queuedCompletion->commandState`. The lifetime
helper is one of the places that never got that memo.

### 36.7 P2-k v4 — THE PLAN OF RECORD

**PART 1 — the fix.**
- **REFACTOR `ApplyClientSqlPeerLifetimeCompletion` to take the AUTHORITATIVE COMPLETION** (`commandKind`,
  `commandState`, `postCommandState` — or the `CitusRemoteExecCommandCompletion` itself) **instead of reading
  cached session state.** Host path (`:21175`) and failed-backend cleanup (`:24700`) pass their completion; node
  B passes the queued one.
- **Call it on node B in `HomerServiceDpuEgressOneSelectedCompletionEvent`: AFTER `publishResult == PUBLISHED`
  (`:43834`) ~~and BEFORE `HomerServiceDpuPopSelectedCompletionEvent` (`:43853`)~~.**
  > ⓘ **CORRECTED by S4 (below): the call goes AFTER the CERTIFIED pop, not before it** — recording a lifetime
  > against a completion whose pop was not certified is exactly the "lied in the direction of yes" this audit
  > ends. The LANDED code (verified 2026-07-15, `tuple_sink_service_process.c:45232`) follows S4, not this bullet.
- **Do NOT record on `NOT_READY` / `BLOCKED` / `FAILED`** — the event is not popped and stays retry-owned.
- ⚠ **Do NOT record at ACCEPTANCE time (`:43453`).** VERIFIED unsafe: `TupleSinkServiceCommandStillInFlight`
  (`:22727`) does not know about selected-session `commandInFlight`/`completionEventCount`, so setting
  `peerWritersQuiesced` while publication is still `NOT_READY`/`BLOCKED` would make a **sinkless session
  retireable while its queued CLOSE completion is unpublished.**
- ⚠ **`TX_ABORT` must NOT quiesce writers** — the peer may still legally send CLOSE (documented at `:24494`).

**PART 2 — the backstop (unchanged).** Guard to the TOP of `ResetSession`; delete the `exit(1)`; return
`DEFERRED`; **no log latch**; allocator keeps scanning on `DEFERRED`; close does **not** convert a late
`DEFERRED` to ERROR; shutdown deliberately ignores `DEFERRED`.
**Still necessary** — VERIFIED: a bound peer receiver legitimately stays `false/false` when the client skips or
fails its semantic CLOSE, when SIGTERM lands before terminal acceptance or egress, when egress stays blocked,
when teardown-landing consumes/rejects the CLOSE, or when completion validation fails before enqueue.

**NOT IN P2-k (owner decision, 2026-07-14):**
- **§37 — the selected-DPU terminal bookkeeping gap**, incl. the **reusable-dead-backend** hazard. Split out.
- **The `dpuBackendTeardownStarted` rejection branch (`:43312`).** ⚠ **DELIBERATELY NOT TOUCHED.** It fires only
  when the arena backend is *already released* — and the normal CLOSE arrives BEFORE that (client sends CLOSE →
  backend executes → publishes `COMPLETED/DO_NOT_REUSE` → exits → *then* P2.T releases the arena). **The gate's
  CLOSE never reaches it.** Its comment states the policy: a CLOSE there is *"expected while the service/peer
  session lingers"* and *"the existing peer-close/lifetime machinery remains the sole owner of actual session
  reclamation."* **Part 2 already covers its only consequence.** Recorded as a known gap; **not a bug to fix.**
  *(Owner pushed back on changing this. Correct call — it removed scope.)*

### 36.10 ✅ PART 2 (the backstop) IMPLEMENTED — 2026-07-15 (this is §48a)

*(Numbered 36.10, after the existing §36.8/§36.9 analysis below, though placed here — right after §36.7 the
plan of record — because it records that plan's implementation.)*

**PART 1 was ALREADY IN THE TREE** — verified before touching anything: the three-scalar
`TupleSinkServiceApplyClientSqlPeerLifetimeCompletion` (`tuple_sink_service_process.c:25387`) and its node-B
call in the selected-DPU egress **after certified pop** (`:45232`) are present. So a *graceful* close already
quiesces the session and the guard passes. This commit is **PART 2 only** — the still-necessary backstop for the
un-graceful cases §36.7 enumerates (SIGTERM mid-command, skipped/failed CLOSE, blocked egress). **The `exit(1)`
that §34.12/§48 measured at SIGTERM survived PART 1 and is what this closes.**

**The diff (citus, one file `tuple_sink_service_process.c`), matching §36.7 PART 2 exactly:**
- `TupleSinkServiceResetSession` is now `TupleSinkServiceSessionResetOutcome {COMPLETED, DEFERRED}` (was `void`).
- The `MayDeregister` guard **moved to the very top**, before every mutation (the prologue used to disable
  `dpuPeerCommandLandingActive` *before* the guard — §35.9 defect #5, the wedge). On refusal: log, return
  `DEFERRED`, **mutate nothing** (PURE DEFER — deliberately does NOT set `dpuBackendTeardownStarted`, which would
  trigger the Q3 two-owner CLOSE-collision with the synthetic cleanup path `:25461`/`:45270`).
- `exit(1)` deleted; the in-body guard deleted (it moved up); function returns `COMPLETED` at the end.
- **NO log-rate latch** (§36 M5). The refusal fprintf is unconditional; it is bounded per-*call*, not per-poll.
- Allocator (`TupleSinkServiceAllocateSession`) **continues scanning** on a non-`COMPLETED` outcome instead of
  handing out a still-bound slot (defensive: for a peer receiver `ShouldRetireWhenIdle ⟹ MayDeregister`, so it
  can't actually defer there — but the contract no longer relies on that cross-function invariant).
- `TupleSinkServiceHandleCloseSession` **keeps its already-prepared SUCCESS response** on a late `DEFERRED`
  (`(void)`-ignores the outcome) per §36.2 — does NOT convert to ERROR; `ResetSession` logs the deferral.
- `main()`'s shutdown sweep ignores the outcome and falls through to `HomerDpuDmaDestroy` — the entire point.

**Two adversarial reviews (codex), both against the PLAN OF RECORD, load-bearing claims re-verified by me:**
1. **Caller audit** — refuted my CLAIM 2: the fence branch is **structurally reachable in steady state** via
   `TupleSinkServiceHandleCloseSession` (ungated), though **not by normal in-tree frontend code** (it closes by
   local, not peer, session id), which is why the gate never crashed. Confirmed all fresh-session open-failure
   sites are never fence-eligible (`clientSqlPeerReceiver` set only at the peer-open path, `:42392`+), and the
   allocator/completion reclaim sites are guarded by the same quiescence predicate.
2. **Diff review** — **R1 (the acceptance) VERIFIED**: no remaining fail-stop on the reset-sweep → destroy-transport
   → `HomerDpuDmaDestroy` path; the drain is reached. **R6 VERIFIED**: diff matches §36.7 and PART 1 is present.
   Findings applied: dropped the log latch's "cannot spam" overclaim (a client that RETRIES CLOSE on a deferred
   session logs at *request* rate — an anomaly worth surfacing, ≪ poll rate, no latch warranted); removed a
   redundant second deferral log in the close handler; fixed a "fenced" wording (pure defer sets no fence field).

**⚠ Known limitation (diff-review R5, recorded — consistent with §36.3, NOT a regression):** in the abnormal
`HandleCloseSession`-reaches-`DEFERRED` ordering, that handler has already cleared `backendLoopActive`, so the
DPU landing path (which needs `dpuBackendTeardownStarted` to serve a `backendLoopActive=false` session) will not
land the pending CLOSE — reclamation then waits for **connection reset** rather than peer-close. This is
**bounded** arena-slot/MR retention, only on the violated ordering, and strictly better than the old `exit(1)`
(whole-service kill). Fixing it needs the shutdown `activeSinkCount` reconciliation §36.3 already scopes out.

**Deviation from §35.9 recorded:** §35.9 prescribed a *dedicated one-shot latch*; §36.2/§36.7 (M5) retired it as
unnecessary. I implemented the latch first (reading §35.9), then removed it on reaching §36 — the exact
"re-check earlier sections when the plan evolved" trap §36.5 warns about.

**Also fixed (KB):** §36.7's PART 1 bullet still said *"BEFORE `...PopSelectedCompletionEvent`"* (`:6088`); S4
(`:6274`) superseded that with *"after certified pop"*, and the landed code follows S4. See the ⓘ note there.

**ACCEPTANCE (§36.4) — ✅ VALIDATED PASS (run-validation, 2026-07-15).** The self-proving test: SIGTERM the
farnet1 (backend-side) DPU service **mid-gate** (`-c 4 -j 4 -t 2000`, killed ~2 s in). Observed on the farnet1
DPU log:
- `homer DPU DMA: teardown drain COMPLETE: entered with 3 outstanding task(s), converged after 1 progress
  round(s); ... fatal=false` — **N=3, the drain RAN on outstanding tasks** (the exact work the old `exit(1)`
  skipped);
- `DEFERRING peer CLIENT_SQL_SESSION reset ... session=1..4` — the backstop fired for **all 4** in-flight sessions;
- `refusing to reset peer CLIENT_SQL_SESSION` **ABSENT**; `*** P2-i ALARM ***` and the full ALARM grep **empty**;
  postmaster pid unchanged (no crash-restart).
- **Graceful control (TEST 1):** `teardown drain COMPLETE: entered with 0 outstanding task(s)` with **neither**
  `refusing` **nor** `DEFERRING` — confirming §36 PART 1 quiesces a graceful close so the guard passes on its own.
- **Regression:** 4-role basebackup 23,252,809,978 B (21.66 GiB) clean `CLOSE_ACK`; gate 0 failed / 8000 processed;
  ALARM grep clean on both DPUs.

⚠ **Process note (re-confirms CLAUDE.md hazard §3.2/§3.3):** the first DPU build returned `exit 0` but left the
**STALE** binary on both DPUs (old string present, new `DEFERRING` absent). Only the landed-proof `strings` grep
caught it; the trees were re-rsynced/rebuilt and re-verified (new present, old absent on both) before the tests
ran. A green `make` is NOT proof of deploy.

---

## §37 — P2-L: THE SELECTED-DPU ARM NEVER DOES TERMINAL SERVICE-STATE BOOKKEEPING

**Status: OPEN. Split out of P2-k by owner decision (2026-07-14) — it is a CORRECTNESS bug in its own right and
must not ride along inside a teardown fix.**

### 37.1 THE GAP — three things the host arm does on terminal completion, and the DPU arm does NONE of

| host (`TupleSinkServiceConsumeCompletionMailbox`) | selected-DPU accept/egress |
|---|---|
| `:21119-21120` — records the completion snapshot (`currentCommandState`, `postCommandState`, rows/result/detail) | ❌ |
| `:21175` — applies lifetime / reuse | ❌ *(this is P2-k)* |
| `:21188` — clears **`backendLoopActive`** and **`launchedBackendPid`** on terminal CLOSE or FAILED | ❌ |

### 37.2 ⚠ THE SERIOUS ONE — **A DEAD DPU BACKEND'S SESSION STILL LOOKS REUSABLE**

1. `sessionState->postCommandState` is **initialised to `CITUS_REMOTE_EXEC_POST_COMMAND_STATE_REUSABLE_IDLE`**
   (`:24303`).
2. The socketless backend publishes its **real** post-command state — **`COMPLETED` / `DO_NOT_REUSE`** — inside the
   completion (`remote_execution_backend_bridge.c:3181`) and then **exits**.
3. The **host** arm copies it into the session (`:21120`). **The selected-DPU arm never does.**
4. `TupleSinkServiceSessionAllowsReuse` (`:23498`) gates on
   `TupleSinkServiceSessionPostCommandStateForbidsReuse(sessionState)` (`:22711`) — **which reads that stale field.**

> **⇒ ON THE SELECTED-DPU ARM, A SESSION WHOSE BACKEND PUBLISHED `DO_NOT_REUSE` AND THEN EXITED STILL REPORTS
> `REUSABLE_IDLE`. `SessionAllowsReuse` CAN RETURN TRUE, AND `TupleSinkServiceFindReusableSession` (`:24036`) CAN
> HAND THAT SESSION TO A NEW CLIENT.**

**Note the seam that makes the P2-k/P2-L split clean:** the **reuse gate** reads `postCommandState`, while
`backendReuseState` (which P2-k's refactored helper *does* set) is consumed only by failed-backend cleanup
readiness (`:24587`) — *"selected-DPU admission does not consult `backendReuseState`"* (VERIFIED). **So P2-k fixes
the lifetime and leaves this hazard entirely intact — no half-fix, no inconsistent intermediate state.**

### 37.3 The other two

- **Stale `currentCommandState` (stays `NONE`/`PENDING` forever).** Read by `CommandStillInFlight` (`:22727`) and
  several terminal predicates (`:18716`, `:19715`, `:21194`). Some readers already work around it
  (`:19031`, `:19213` prefer `queuedCompletion->commandState`) — **piecemeal, which is how the bug survived.**
- **Stale `backendLoopActive` / `launchedBackendPid`.** Retains completion-machine ownership
  (`HomerServiceCompletionMachineHasWork`, `:13014`) and leaves a **persistent owned `WAIT_BACKEND` candidate** in
  the scheduler (`:45277`) for **every completed session**. Exact CPU cost is **INFERRED** (runnable filtering may
  reject an empty CPU completion mailbox at `:13037`) — **but it is a standing open question worth checking
  against the unattributed ~10% and the ~223 µs discovery latency.**

### 37.4 EXPLICITLY OUT OF SCOPE FOR P2-L

**Do NOT blindly copy the host path.** VERIFIED constraints:
- **Do NOT import host immediate retirement (`:21247`)** — node-B selected-DPU state **must survive until P2.T T4
  releases the arena and resets the selected state** (`:44527`).
- Do NOT import host mailbox-credit or staged-publication logic.
- **rows / result / detail: probably NOT needed** on this arm (the result reaches the client via the byte ring,
  not the service's cached copy). **Verify what actually reads them before copying anything.**

### 37.5 Rule earned

> **"THE NEW PATH DROPPED A STEP" IS ALMOST NEVER ONE STEP.** The lifetime record was the symptom that shouted.
> Beside it, silently: the reuse gate's input, the in-flight predicate's input, and the scheduler's
> backend-ownership flag. **When you find one invariant a new path skipped, DIFF THE WHOLE FUNNEL — the one you
> noticed is the one that happened to be load-bearing for the bug you were chasing, not the only one.**

### 36.8 ⚠ THE DEFECT IS BIGGER THAN THE `exit(1)`: **SELECTED-DPU SESSIONS NEVER RETIRE**

> # ⛔ PARTIALLY RETRACTED 2026-07-14 BY MEASUREMENT — READ §38 BEFORE YOU BELIEVE THE SEVERITY BOX BELOW.
> **The FACT is confirmed: selected-DPU sessions are never retired. 63 of them sat `active` in a 75-session run.**
> **The CONSEQUENCE stated below is WRONG, and it was mine.** This section claims the leak fills the 64-slot
> session table and then *"every new session open fails, permanently"* via `AllocateSession` returning NULL.
> **`AllocateSession` NEVER RETURNED NULL.** `"peer service ran out of free command sessions"` = **0**, measured
> across 75 sequential sessions, on **both** pre-P2-k and P2-k binaries. **The session table is not the wall.**
>
> **The real wall is the SCHEDULER READY SET** (`HOMER_PROGRESS_PLAN_MAX_GRANTS` = 64, a *different* resource
> that happens to have the *same* capacity): each retained session keeps a `COMPLETION_RING` candidate forever,
> the set saturates at 64, and the **`PAYLOAD_STREAM`** source that would carry the result back is **dropped
> every pass**. The gate then dies at session **64** with a 30 s timeout — not an honest "out of sessions" error.
> **Root cause + measurement: §38. Fix: §37 / P2-L.**

**VERIFIED while checking whether P2-k v4 works standing alone. This reframes the severity.**

`TupleSinkServiceSessionShouldRetireWhenIdle` (`:24366`), peer-receiver branch:

```c
if (sessionState->clientSqlPeerReceiver)
{
	return sessionState->peerLifetime.peerWritersQuiesced ||
	       sessionState->peerLifetime.connectionResetComplete;
}
```

…and the comment directly above it states the contract:

> *"Backend-node peer receivers expose their command mailbox as a remote RDMA target. A failed SQL command or
> backend `DO_NOT_REUSE` state does not prove the peer opener has stopped writing `TX_ABORT` /
> `CLIENT_SQL_SESSION_CLOSE` through the published descriptor. **Retire only after explicit close has quiesced
> peer writers, or after connection reset has destroyed the QP.**"*

`TupleSinkServiceMaybeRetireSessionAfterPeerResponse` (`:24420`) calls `ResetSession` **only** when that predicate
holds.

> ### ⇒ ON THE SELECTED-DPU ARM `peerWritersQuiesced` IS NEVER SET (§36.1) ⇒ `ShouldRetireWhenIdle` ALWAYS RETURNS FALSE ⇒ **THE SESSION IS NEVER RETIRED.**
> ### **EVERY SELECTED-DPU SESSION LEAKS, PERMANENTLY, FOR THE LIFE OF THE SERVICE.**

**And that is EXACTLY the symptom we observed and did not connect:** the failing shutdown refused on **session 1**
— the **warmup's** session, `current_sequence=14004` (= 2000 txns × 7 statements) — **still `active` three gate
runs later.** It was not "lingering". **It could never be retired.**

**⇒ The `exit(1)` at shutdown is a DOWNSTREAM SYMPTOM of a session-retirement leak.** The retirement leak is the
defect; the guard is the alarm.

#### What this changes

- **P2-k Part 1 is not a teardown fix. It restores SESSION RETIREMENT on the selected-DPU arm.** That is the
  actual bug and it is a live, permanent resource leak in the command plane — not a shutdown wart.
- **The acceptance test gets a SECOND, INDEPENDENT observable, and a stronger prediction:**
  after Part 1, sessions should be retired **as they close**, so at SIGTERM there should be **NO active
  peer-receiver sessions at all** ⇒ **NO refusal line whatsoever** (not merely a deferred one), exit **0**, and
  `teardown drain COMPLETE`.
- **Part 2 is confirmed as a BACKSTOP, not the fix.** Good — that is what a backstop should be.

> **RULE. WHEN A GUARD REFUSES, ASK WHAT ELSE READS ITS PREMISE.** `peerWritersQuiesced` is read by TWO
> consumers: `MayDeregister` (the guard that shouted) **and** `ShouldRetireWhenIdle` (which said nothing, and
> silently leaked every session on the DPU path). **The loud consumer is rarely the important one.** I chased the
> `exit(1)` for three review rounds; the retirement leak was sitting on the same flag the whole time.

### 36.9 ROUND 4 — WE AGREE. One hard refutation (S3), one hardening (S4), one wrong rationale (mine).

**Confirmed (VERIFIED):** **S1** P2-k works standing alone — `MayDeregister` reads **none** of §37's stale fields,
and no later P2.T step clears `peerWritersQuiesced`. **S2** the split is coherent — `backendReuseState`'s only
behavioural reader (failed-backend cleanup readiness, `:24570`) treats it as *alternative* evidence and does not
reject disagreement with the stale `postCommandState`; generic reuse reads only `postCommandState`. **P2-k neither
worsens nor masks P2-L.** **S5** the allocator's `DEFERRED` path is bounded, self-excluding, and all **seven**
allocation callers already test `NULL`. **S6 — expect ZERO refusal lines on a clean gate.**

#### ⛔ S3 REFUTED — PASSING THE COMPLETION *POINTER* IS A POST-RELEASE READ RACE

My instinct was to pass the whole `CitusRemoteExecCommandCompletion *`. **It felt cleaner. It is a
use-after-release.**

- **VERIFIED:** on the **host** arm, `completion` points **directly into a mailbox ring slot** (`:21097`).
- **VERIFIED:** the host copies its contents into session state and then **publishes `consumedEpoch`** (`:21119`,
  `:21165`) — **BEFORE** the helper call at `:21175`.
- **VERIFIED:** the backend treats that consumed epoch as **overwrite credit** and **reuses/memsets the slot**
  (`remote_execution_backend_bridge.c:1831`, `:1850`).
- **VERIFIED:** failed-backend cleanup has **no completion at all** — it *derives* kind/state/post-state (`:24672`),
  returns command-ring credit (`:24699`), **then** calls the helper (`:24700`). Its command-record pointer is
  likewise producer-reusable after credit.

**⇒ THE SIGNATURE MUST BE THREE SCALARS, CAPTURED BEFORE CREDIT IS RETURNED:**

```c
TupleSinkServiceApplyClientSqlPeerLifetimeCompletion(sessionState, commandKind, commandState, postCommandState)
```

**VERIFIED: those are the ONLY three facts the helper uses** — no rows, results, flags, sequence, or detail.
A whole pointer is safe **only** on DPU egress before pop, **not uniformly across callers.**

> **RULE. "PASS THE WHOLE OBJECT" IS AN ABSTRACTION CHOICE, AND ABSTRACTION CHOICES HAVE LIFETIMES.** The cleaner
> signature was the unsafe one: the object it names is a **producer-owned ring slot** whose credit has already
> been returned by the time the callee would read it. **Ask what the argument POINTS INTO and who else may write
> it — before asking what looks tidy.**

#### S4 — hardening: certify the pop

**VERIFIED:** `PUBLISHED` is returned only after final-WIMM acceptance; all non-`PUBLISHED` results return before
the proposed call site; SIGTERM cannot interleave (the handler only clears the run flag, and teardown starts after
the current pump returns). **The normal path is safe.**

**But:** `HomerServiceDpuPopSelectedCompletionEvent` can defensively **decline to pop** (count underflow, empty
head) and **returns no status** (`:42653`). Unreachable under normal invariants — **and the caller cannot certify
it.** *That is exactly the P0-f disease.* **Make the pop return `bool`, and record the lifetime only after a
CERTIFIED pop.**

#### ⚠ MY RATIONALE FOR LEAVING THE TEARDOWN-REJECTION BRANCH ALONE WAS WRONG (the decision stands)

I said it fires only when *"the arena backend is already released."* **VERIFIED FALSE:** `dpuBackendTeardownStarted`
is set at **T1**, immediately after completion acceptance (`:43551`, `:43567`); the arena is released **much later,
at T4** (`:44527`).

**The decision is still correct, for a different reason:** the normal CLOSE **has already passed LANDING** before
the completion acceptance sets the flag, so it cannot re-enter the rejection branch. *(Right answer, wrong
reason — corrected here rather than left to look like it was reasoned.)*

#### Follow-up recorded (not a normal-gate blocker)

Generic retirement has **no explicit P2.T check**; today the normal node-B session is protected only because it
retains a reserved result handle and a nonzero `activeSinkCount` until T4 (`:20530`, `:27377`). **A direct node-B
lifecycle close, or a future early result-handle removal, would expose that gap.** Belongs with §37's lifecycle
work.

### 36.10 ▶ RESUME POINT — P2-k **Part 1 LANDED (citus `5a57e1cc9`), NOT YET VALIDATED**

> # ✅ SUPERSEDED 2026-07-14 — Part 1 is now BUILT, DEPLOYED, and VALIDATED. **Its acceptance test passed and
> # THE SYSTEM IS STILL BROKEN.** The live next-step list is **§38.7**. Read §38 first.
>
> **Steps 1–4 below are DONE:** both trees built + installed (landed-proof `0 → 1`, proven in both directions),
> farnet0 synced (md5-identical), **both DPUs redeployed**. The acceptance run passed on **both** DPUs —
> `RAW EXIT STATUS 1 → 0`, `refusing to reset` `1 → 0`, `teardown drain COMPLETE` `0 → 1 line`. Gate band
> **299.6 / 296.1 / 296.3** vs `285.6 / 291.0 / 299.1` — **no regression.**
>
> **AND IT PROVED NOTHING ABOUT THE ACTUAL BUG.** A 75-session loop (the test this plan did *not* ask for)
> dies at session **64** — identically on pre-P2-k binaries. **§38.**
>
> **Steps 5–6 below are REORDERED:** §37 / P2-L is **promoted** (it is the fix for the measured wall);
> P2-k Part 2 (the backstop) is **deferred behind it**.

**Stopped here by owner request (2026-07-14). Everything below is the exact next-step list.**

#### ✅ DONE — Part 1 (builds clean, formatted, committed; **no run yet**)

| # | change | file:site |
|---|---|---|
| 1 | `TupleSinkServiceApplyClientSqlPeerLifetimeCompletion` now takes **three scalars** (`commandKind`, `commandState`, `postCommandState`) instead of reading the session cache | `tuple_sink_service_process.c` (definition + fwd decl) |
| 2 | host caller passes its **already-captured session copies** (written before `consumedEpoch` credit) — **behaviour unchanged** | host consume path |
| 3 | failed-backend cleanup ditto — **behaviour unchanged** | cleanup path |
| 4 | **`HomerServiceDpuPopSelectedCompletionEvent` now returns `bool`** (it was `void` with two silent decline paths) | pop |
| 5 | **THE FIX** — `HomerServiceDpuEgressOneSelectedCompletionEvent`: capture the three facts → only on `PUBLISH_PUBLISHED` → only after a **certified pop** → record the lifetime | DPU egress |

#### ⏭ NEXT — in this order

1. **BUILD + INSTALL + DEPLOY.** citus first (`make` + `install-headers install-service-bin install`), then relink postgres (`ninja -C build`), then `meson install`. Then rsync to farnet0 (verify md5-identical) and **redeploy BOTH DPUs** with the landed-proof grep.
   ⚠ P2-k changes `tuple_sink_service_process.c` — **the DPU service binary. Both DPUs must be rebuilt.**
2. **THE ACCEPTANCE RUN — and it is self-proving.** Clean baseline (both hosts + both DPUs), PG with the frontend agent, **no host services**, both DPU services. Run the gate (`-c 1 -t 2000`), then **SIGTERM the backend-side (farnet1) DPU service IMMEDIATELY AFTER**, capturing the **real exit status** (a vanished `/proc/<pid>` is NOT proof — use the traced start/sigterm scripts).

   | | today (`91482c840`) | expected after Part 1 |
   |---|---|---|
   | `RAW EXIT STATUS` | **1** | **0** |
   | `refusing to reset peer CLIENT_SQL_SESSION` | **1** | **0** — Codex S6 predicts **zero refusal lines on a clean gate** |
   | `teardown drain COMPLETE` | **0 lines** | **1 line** — and **`N > 0` is plausible**, since the still-bound session's DMA is what is outstanding |

   **If the refusal line still appears, Part 1 did not take.**
3. **Re-run the DPU gate band** (`-c 1 -t 2000` ×3) — P2-k is on the command-completion path, so a perf check is mandatory. Current band: **285.6 / 291.0 / 299.1** at citus `2b37e8701` (**`-c 1`** — see the client-count rule).
4. **4-role basebackup** (unaffected in principle, but it is the only byte-ring wrap exerciser).
5. **THEN Part 2** (the backstop) — §36.7. Guard to the TOP of `ResetSession`; delete the `exit(1)`; return `DEFERRED`; **no log latch**; allocator keeps scanning; close does **not** convert a late `DEFERRED` to ERROR; shutdown ignores `DEFERRED`. *(Part 1 should make the refusal unreachable on a clean gate; Part 2 covers the abnormal paths Codex enumerated — client skipped/failed CLOSE, SIGTERM before egress, egress blocked, validation failure, late CLOSE into the teardown-rejection branch.)*
6. **THEN §37 / P2-L** — the terminal service-state bookkeeping gap (**the reusable-dead-backend hazard**, stale `currentCommandState`, stale `backendLoopActive`/`launchedBackendPid`). Split out by owner decision.

#### 🔭 And a hypothesis worth testing once P2-L lands

**Stale `backendLoopActive` leaves a persistent owned `WAIT_BACKEND` scheduler candidate for EVERY completed
session (`:13014` → `:45277`).** With sessions never retiring, that accumulates without bound.
**Check it against the two standing open performance questions** — the unattributed ~10% (318 → ~285) and the
~223 µs DPU discovery latency. **INFERRED, untested — but it is the first mechanism proposed for either that
predicts a monotonically growing cost.**

---

## §38 — 🔴 **THE 64-SESSION CLIFF: MEASURED.** The gate dies at session 64, and it is NOT the session table.

**Status: ROOT-CAUSED to a verified code chain. PRE-EXISTING (not a P2-k regression — measured on both arms).
NOT FIXED. This is the real content of §37 / P2-L, and it is a hard functional wall, not a tidy-up.**

**Found 2026-07-14 by the P2-k Part 1 acceptance run — specifically by the test that the plan did NOT ask for.**

### 38.1 THE MEASUREMENT

75 sequential single-client gate sessions (`-c 1 -t 5`) against **one** DPU service instance. Both arms, same
machine, same hour, clean baseline each time:

| | pre-P2-k (`2b37e8701`) | P2-k Part 1 (`5a57e1cc9`) |
|---|---|---|
| sessions 1–63 | ✅ all pass | ✅ all pass |
| **session 64** | ❌ **FAILS** | ❌ **FAILS** |
| sessions 65–75 | ❌ all fail | ❌ all fail |
| failures / 75 | **12** | **12** |
| `DPU backend spawn begin` / `COMPLETED` | **64 / 64**, then never again | **64 / 64**, then never again |
| `"ran out of free command sessions"` | **0** | **0** |

```
pgbench: error: client 0 timed out after 30 s waiting for Homer completion of sql_execute (kind=6 sequence=5)
pgbench: error: client 0 could not close selected-DPU Homer SQL session: semantic close did not release
                selected-DPU role-1 slot: session=64 outstanding=5 slot_state=2
```

> **⇒ IDENTICAL IN BOTH ARMS. P2-k Part 1 IS NOT A REGRESSION, AND PART 1 STAYS.** The before-arm was run for
> exactly this decision, and it is the only reason the decision is safe to make.

### 38.2 THE DPU NAMES ITS OWN FAILURE — and it is the SCHEDULER, not the session table

```
tuple-sink service: progress ready-set overflow event=1 kept=64 dropped=1
    kind_mask=0x00000a00 overflow_kind_mask=0x00000400
    first_dropped_kind=10 first_dropped_index=0 first_dropped_generation=63
```
**First emitted after spawn #63**, then forever (the event counter reaches 16,777,216 — the probe is
first-N-then-every-Nth, so the output stays bounded; the "always bound the output" rule earning its keep).

Decoded against the enum (`tuple_sink_service_process.c:2577`):

| field | value | meaning |
|---|---|---|
| `kept=64` | | `HOMER_PROGRESS_PLAN_MAX_GRANTS` = **64** (`:2551`). The ready set is **FULL**. |
| `kind_mask=0xa00` | bits 9,11 | KEPT: `COMPLETION_RING` (9) + `CQ_DRAIN` (11) |
| `first_dropped_kind=10` | | DROPPED: **`PAYLOAD_STREAM`** — *the source that carries the result back* |

> **TWO DIFFERENT CAPACITIES, BOTH 64, AND THE COLLISION IS WHY THIS WAS MISREAD.**
> the **session table** = `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS` = 64 (`homer_abi_version.h:50`)
> the **scheduler ready set** = `HOMER_PROGRESS_PLAN_MAX_GRANTS` = 64 (`:2551`)
> They fill in lockstep because each retained session contributes one scheduler candidate. **The cliff looks
> like the session table and is not.** `AllocateSession` never once returned NULL.

### 38.3 THE VERIFIED CAUSAL CHAIN (every link read in the code, not inferred)

1. **The selected-DPU arm never clears `backendLoopActive` on terminal completion.** All six writes of
   `= false` (`:21215`, `:22219`, `:22448`, `:24604`, `:40445`, `:41504`) are on the **host** arm, the
   **host-peer** arm, or inside `ResetSession`. **None is on the selected-DPU arm.**
2. **T4 retains the session and does not clear the flag either** (`:44658`–`:44683`). Its comment is explicit:
   *"Original T4 draft destroyed the service session here: `TupleSinkServiceResetSession(serviceSession);` …
   Retain the fenced service session until that owner closes it."* — and the backend it just released is **gone**.
3. → `HomerServiceCompletionMachineHasWork` (`:13018`) returns **true forever**: `backendLoopActive` is one of
   its disjuncts (`:13026`).
4. → `HomerServiceFinalizeCompletionPendingBit` (`:13481`) clears the session's ready bit **only** when
   `HasWork()` is false. It never is. **The bit is never cleared.**
5. → the ready-set builder (`:13552`–`:13581`) walks `completionRunnableSessions` and appends a
   `COMPLETION_RING` candidate for every session passing `active && completionMailbox != NULL &&
   clientSqlPeerReceiver`. **A retained node-B session passes all three, by construction** — "retained" *means*
   `active`, and the mailbox is the very MR `MayDeregister` guards.
6. → at **64** retained sessions `HomerProgressReadySetAppend` (`:9682`) returns false and the loop `break`s.
   **`PAYLOAD_STREAM` sources are never even reached** — COMPLETION_RING is appended first.
7. → the new session's result never moves. **30 s client timeout. Every session from 64 on.**

> **This is §37.3's FIRST consequence, verbatim** — *"stale `backendLoopActive` … retains completion-machine
> ownership (`HomerServiceCompletionMachineHasWork`, `:13014`)"*. It was recorded as **INFERRED, untested**.
> **It is now MEASURED, and it is not a performance footnote — it is a functional wall at 64 sessions.**

### 38.4 ⛔ RETRACTION — §36.8's SEVERITY CLAIM IS **WRONG**, and it was mine

> # ⛔⛔ THIS RETRACTION IS **ITSELF RETRACTED** (§40.1). I RETRACTED A CORRECT CLAIM.
> **§36.8's severity claim was RIGHT IN OUTCOME.** `AllocateSession`'s table-full reclaim requires
> **`activeSinkCount == 0`** (`:24273`) — and D1's phantom sink pins that at ≥ 1 forever, so **reclaim can
> never fire and session 65 WOULD get NULL.** The session table *is* a wall.
>
> **I never saw `"ran out of free command sessions"` because sessions 65-75 NEVER REACHED node B** — D4's
> setup-TCP wedge stopped them at node A. **I read the silence as "the allocator coped." It meant "the
> allocator was never asked."**
>
> **⇒ THE RULE I DERIVED IN §38.6 AND THEN IMMEDIATELY MISAPPLIED:**
> ## A ZERO COUNT PROVES A LINE DID NOT EXECUTE. IT DOES NOT PROVE **WHY**.
> Absence of a failure message is equally consistent with *"no failure occurred"* and *"the code never got
> there."* **Before you conclude the former, find a line that WOULD have printed on the path you believe ran.**
> Here that line was in my own output the whole time: **`DPU backend spawn begin` stopped dead at 64.**
> Everything below is kept because the reasoning that produced the error is the point.

**§36.8 said:** *"EVERY SELECTED-DPU SESSION LEAKS … hard-capped at 64, after which `AllocateSession`'s
table-full reclaim finds nothing and returns NULL — **every new session open fails, permanently.**"*

**MEASURED: `AllocateSession` NEVER RETURNED NULL.** `"peer service ran out of free command sessions"`
(`:41127`) = **0**, in **both** arms, across 75 sessions. **The session table was never the wall.**
The wall is the scheduler ready set (§38.2). *The sessions-never-retire FACT is real; the CONSEQUENCE I
attached to it was not.* See §36.8's inline retraction.

### 38.5 THE OPEN QUESTION — **WHY is the session retained?** (3 theories, 3 refuted. Do not guess a 4th.)

The chain above explains **the cliff**. It does **not** explain **why no retirement path fires**. Three
retirement paths exist on paper; the log says **none of them ran**:

| path | what it would do | ran? |
|---|---|---|
| `MaybeRetireSessionAfterPeerResponse` (`:24434`) | retire on peer response | **its 5 callers (`:41001`–`:41507`) are ALL on the host-service peer arm.** Not reachable from the DPU arm. |
| sink-release hook (`:27458`–`:27490`) — `activeSinkCount--` then retire | retire when the last sink drops | ❌ **0 lines.** `"reclaiming sink session="` never printed. **`activeSinkCount` was NEVER decremented.** |
| connection-reset (`:29738`–`:29774`) — sets `connectionResetComplete`, then retires | retire on peer disconnect | ❌ **0 lines.** `"command mailbox quiesced by connection reset"` never printed. **`connectionResetComplete` was NEVER set.** |

Meanwhile a **fourth** path *did* run, 63×: `"payload stream marked for CLEAN-CLOSE RECLAIM"` →
`"reclaimed payload stream after a CLEAN CLOSE"`. **It appears to reclaim the stream without decrementing
`activeSinkCount` and without checking retirement.** Leading suspect for why `:29738` skips the session:
its filter requires `peerLifetime.peerBindingOpen`, and the log shows 63× `"marked send byte-ring terminal …
reason=peer-binding-cleared"` — i.e. **the binding may already be cleared by the time the reset callback
runs**. ⚠ **INFERRED. NOT VERIFIED. This is theory #4 and the previous three were wrong.**

### 38.6 Rules earned — and this section is the expensive one

> **⛔ THE ACCEPTANCE TEST THE PLAN ASKED FOR WAS GREEN ON A SYSTEM THAT STILL HAD THE BUG.**
> KB §36.10 specified: *SIGTERM the DPU, expect exit 0 and no refusal line.* It passed — exit 0, zero refusals,
> teardown drain reached. **And the system still dies at 64 sessions.** The shutdown test exercises
> `MayDeregister`, the consumer that SHOUTED. "Zero refusals" is satisfiable **two** ways: *(a)* sessions now
> retire, or *(b)* sessions still leak but now deregister cleanly. **It was (b).** The test could not tell them
> apart — and that was foreseeable *before* running it, from §36.8's own text.
> **WHEN A BUG HAS A LOUD CONSUMER AND A SILENT ONE, AN ACCEPTANCE TEST BUILT ON THE LOUD ONE PROVES NOTHING.
> TEST THE SILENT ONE.**

> **A CAP TEST MUST EXCEED THE CAP.** §36.10's criterion D was *"session-table full = 0 ✅"*. Only **4** sessions
> ran. Of course it was 0. **A ceiling check below the ceiling is decoration**, and it printed a green tick.

> **DIFF THE WHOLE FUNNEL — AND I DID NOT.** §37.5 (written the day before, by me): *"'THE NEW PATH DROPPED A
> STEP' IS ALMOST NEVER ONE STEP … when you find one invariant a new path skipped, DIFF THE WHOLE FUNNEL."*
> The selected-DPU arm dropped **four** host-arm steps. Part 1 fixed **one** and I split the other three out as
> "later". **The KB told me they were there, in a rule I wrote, and I shipped the one anyway.**

> **THREE MECHANISMS, THREE REFUTATIONS, IN ONE HOUR.** (1) "the session table fills and allocation fails
> permanently" — refuted, `AllocateSession` never returned NULL. (2) "§37.3 named the wrong flag (WAIT_BACKEND,
> not COMPLETION_RING)" — **refuted; §37.3 named TWO consequences and I read past the first, which was the
> right one. I 'corrected' a claim that was correct.** (3) "the reclaim path was alive via
> `connectionResetComplete`" — refuted, that flag was never set. **Each story was coherent, evidence-backed,
> and wrong. The only thing that stopped each one was going back to the log for a line that would have to
> exist if the story were true — and finding it absent.**
> **A COUNT OF ZERO ON A LINE YOUR THEORY REQUIRES IS WORTH MORE THAN ANY AMOUNT OF READING.**

### 38.7 ▶ WHAT THIS CHANGES

1. **P2-k Part 1 STAYS.** Validated for what it claims: exit `1`→`0`, refusals `1`→`0`, teardown now reaches
   `HomerDpuDmaDestroy`'s drain, gate band **299.6 / 296.1 / 296.3** vs `285.6 / 291.0 / 299.1` (no regression).
   It is **not** a regression on the cliff (§38.1, measured both arms).
2. **§37 / P2-L is PROMOTED.** It is not follow-on hygiene; it is the fix for a measured functional wall.
3. **P2-k Part 2 (the backstop) is DEFERRED behind it** — it hardens an abnormal path, while P2-L unblocks the
   normal one.
4. **§38.5 must be settled before any code is written.** Three refuted mechanisms is the signal to stop
   theorising and have the diagnosis attacked.

---

## §39 — **SETTLED**: the 64-session cliff is THREE defects, and the one that kills is SILENT

**Status: DIAGNOSIS SETTLED (2026-07-14). Adversarial round with Codex; it refuted me on three load-bearing
claims and I VERIFIED every one of its corrections myself in the code. This section is the agreed ground.**
**§38's chain is SUPERSEDED where it conflicts with this — see §39.5.**

### 39.1 D1 — **THE SINK-ACCOUNTING LEAK.** One line. It is the root of the retention.

`HomerServiceReclaimPayloadStreamAfterPeerReset`, the `peerResetAfterCleanClose` branch
(`tuple_sink_service_process.c:29693-29704`):

```c
if (streamEntry->stream.peerResetAfterCleanClose)
{
	fprintf(stderr, "... reclaimed payload stream after a CLEAN CLOSE (no failure published) ...");
	HomerServiceClearPayloadStreamPeerBinding(streamEntry);
	PayloadBindingsCleanReclaimed++;
	HomerServiceResetPayloadStreamEntry(streamEntry, true);   /* frees the stream */
	return;                                                   /* <<<< AND RETURNS */
}
```

**It frees the stream and returns, bypassing `TupleSinkServiceMaybeReclaimSinkAndSession` (`:27423`) —
so `sessionState->activeSinkCount` is NEVER DECREMENTED and the retirement funnel (`:27458`-`:27490`)
NEVER RUNS.**

> **⇒ `activeSinkCount` IS STUCK ≥ 1 FOREVER. AND EVERY RETIREMENT PATH IS GATED ON `activeSinkCount == 0`.**
> It is **not** that the retirement paths are unreachable. **A phantom sink makes all of them refuse.**
> That single line explains all three of §38.5's zero-counts at once.

⚠ **Directly above this branch is a comment block enumerating what is *"Deliberately NOT called here"* — three
functions, each with a reason.** Someone thought hard about what not to call **and never noticed they were also
skipping the sink accounting.** *A carefully-reasoned exclusion list is silent about the thing it forgot.*

### 39.2 D2 — stale `backendLoopActive` → the **MACHINE** candidate set → payload machines dropped

**§37.3 WAS RIGHT, AND I OVERRODE IT TWICE.** It named the **`WAIT_BACKEND` scheduler candidate**. I "corrected"
it to COMPLETION_RING/ready-set because that is what the **log** showed. The log showed a *companion symptom*.

| link | site | status |
|---|---|---|
| DPU egress never clears `backendLoopActive`/`launchedBackendPid` on terminal | `:43832`-`:43987` (host does it at `:21201`-`:21216`) | **VERIFIED** |
| ⇒ `HomerServiceCompletionMachineHasWork` true (disjunct) | `:13018`-`:13031` | **VERIFIED** |
| ⇒ stale completion **ownership** retained (`completionOwnedSessions`) | set at spawn `:13202`-`:13214`; the finalizer `:13469`-`:13493` is **never called on the DPU arm** (sole exec call `:15803`) | **VERIFIED (Codex)** |
| ⇒ every stale owner becomes an actionless **`WAIT_BACKEND` machine** | `:45414`-`:45450` | **VERIFIED** |
| ⇒ **session** machines are built **BEFORE** payload machines | `:45801` vs `:45806` | **VERIFIED** |
| ⇒ the **MACHINE CANDIDATE SET** (`machines[HOMER_PROGRESS_PLAN_MAX_GRANTS]`, **cap 64**) fills | `:3285`-`:3303` | **VERIFIED** |
| ⇒ payload machines **SILENTLY DROPPED** at `machineCount == 64` | `:9484`-`:9488` | **VERIFIED** |
| ⇒ session 64's result never moves ⇒ **30 s timeout** | — | **MEASURED** |

### 39.3 D3 — **THE SILENT CAP.** This is why it cost a day.

**`candidateSet->droppedMachineCount` is written (`:9487`) and NEVER READ. `candidateSet->overflowed` is set at
SIX sites and NEVER READ.** The *ready set* has `HomerServiceReportReadySetOverflow` (`:9715`, called at
`:48446`/`:48617`). **The machine candidate set has NOTHING.**

> **⇒ THE OVERFLOW THAT ACTUALLY KILLED THE RUN EMITTED ZERO DIAGNOSTICS.** The one I found, chased, and built
> a whole root-cause chain on (`progress ready-set overflow … first_dropped_kind=10`) is the **VISIBLE
> COMPANION** of the invisible one. **I diagnosed the symptom that had a printf.**
>
> **This is the P0-b write-only-counter disease, which THIS KB ALREADY DOCUMENTS** (`:29791`: *"the abort
> classifier's control-op counters used to be WRITE-ONLY — it carefully classified every discarded op and
> nobody ever printed the result"*). **Same bug, same file, a second time.**
> **RULE: NO SILENT CAPS. A bounded set that drops work MUST say what it dropped, or the next reader will
> diagnose whichever neighbouring failure happens to have a printf.**

### 39.4 D4 — **THE SETUP-TCP SINGLETON WEDGE** (independent defect; explains the 12-failure cascade)

`homer_service_dpu_setup_tcp.c` — the DPU setup listener (port 9727) is **CAPACITY ONE**: a single
`int clientFd` (`:48`), and it accepts only when free (`:345`). And `WAITING_CLOSE_DRAIN` has **no deadline and
no EOF escape** (`:697`-`:709`):

```c
if (!drained) { return true; }     /* "keep waiting" -- FOREVER */
```

Session 64's unresolved command ⇒ close certification returns `drained=false` (`:42526`-`:42530`, **INFERRED**)
⇒ the server wedges ⇒ `clientFd` is never released ⇒ **sessions 65-75 CANNOT CONNECT AT ALL.** That is why
`DPU backend spawn begin` is exactly **64** and never increments again.

> **⇒ ANY CLIENT THAT DIES MID-COMMAND PERMANENTLY WEDGES THE DPU SETUP LISTENER FOR EVERY FUTURE CLIENT.**
> This is far broader than the 64-session cliff. **It is a candidate mechanism for the known-but-unexplained
> hazard in CLAUDE.md §8.1** — *"wedged the DPU setup listener for four days without a single log line."*

### 39.5 ⛔ WHAT §38 GOT WRONG (and it was mine)

- **§38.3 CLAIM 5 — REFUTED.** A failed ready-set append breaks only the completion-bitmap helper (`:13577`);
  **coarse construction CONTINUES into payload construction** (`:14137`-`:14165`). And under machine-baseline
  (which DPU mode *requires*, `:48859`-`:48866`) the kept `CQ_DRAIN` still mints a `PAYLOAD_FRONTIER` collector
  (`:46105`-`:46109`) which still requests payload machines. **The dropped flat `PAYLOAD_STREAM` source does not
  by itself starve payload.** The starvation is one layer later, in the machine set (§39.2).
- **§38.3 CLAIM 3 — REFUTED as stated.** `FinalizeCompletionPendingBit` *does* clear the runnable bit when
  `HasWork()` is true but `Runnable()` is false (`:13481`-`:13492`). The bit is stale because **the finalizer is
  never CALLED on the DPU arm** — not because `HasWork()` is true.
- **§38.5 theory #4 — REFUTED.** `"reason=peer-binding-cleared"` is the **payload** field
  `stream.peerBindingActive` (`:26956`), **NOT** the session field `peerLifetime.peerBindingOpen`. Two
  similarly-named fields on different objects; I conflated them. The real skip is the **connection-handle
  comparison** at `:29755`: the session holds the **CRITICAL_CONTROL** connection (`:41156`), while the 63
  disconnects are **FOREGROUND_PAYLOAD** connections. The control connection is **persistent** (log: *"accepted
  persistent incoming RDMA peer transport"* = **1**, for the whole run) and never disconnects. **⇒
  `connectionResetComplete` is STRUCTURALLY UNREACHABLE on this arm.**

### 39.6 ⛔ THE FIX I WAS ABOUT TO WRITE WOULD HAVE BROKEN THE GATE ON COMMAND #2

**§38's CLAIM 6: "clear `backendLoopActive` on terminal completion."** DPU command **landing** rejects a
non-teardown session whose `backendLoopActive` is false (`:43363`-`:43367`):

```c
if (... || ((!sessionState->backendLoopActive || !sessionState->dpuArenaSlotBound) &&
            !sessionState->dpuBackendTeardownStarted) || ...) { continue; }   /* command REJECTED */
```

**The gate runs ~14,000 commands (2000 tx × ~7 statements) on ONE session.** Clearing the flag on *every*
terminal completion would have killed it at command #2. **The host's predicate (`:21201`-`:21216`) clears ONLY
on `CLIENT_SQL_SESSION_CLOSE`, `FAILED`, or `TX_COMMIT`/`TX_ABORT` for non-client-SQL opKinds** — its comment
says it outright: *"For persistent client SQL sessions, COMMIT/ABORT are no longer terminal."*

> **This is the FOURTH proposed fix of mine in this audit that would have been a bug.** (The others: the
> fence-and-return permanent wedge; the teardown reorder use-after-free; the silent no-op helper call.)
> **RULE, again: WHEN A GUARD READS A FLAG, FIND EVERY OTHER READER BEFORE YOU CHANGE WHO WRITES IT.**

### 39.7 ▶ THE P2-L FIX PLAN (ordered; diagnostic FIRST)

**L0 — MAKE THE SILENT CAP LOUD, AND RE-MEASURE BEFORE FIXING ANYTHING.**
Add `HomerServiceReportMachineCandidateOverflow`, mirroring `HomerServiceReportReadySetOverflow` (`:9715`) —
**same first-8-then-powers-of-2 rate limit** (it is on the scheduler pass; an unbounded printf here is a bug).
Report `machineCount`, `droppedMachineCount`, and **the kind/index of the first dropped machine**.
**Then re-run the 75-session loop and SEE the drop.** This converts the fix from a hope into a measured
before/after — and it is the diagnostic whose absence cost a day.

**L1 — terminal bookkeeping on the selected-DPU egress.** In
`HomerServiceDpuEgressOneSelectedCompletionEvent`, right after the **certified pop** — *exactly where P2-k Part 1
already records the lifetime, using exactly the three scalars it already captures* (`commandKind`,
`commandState`, `postCommandState`):
  - apply the **host's predicate verbatim** (`:21201`-`:21216`) to clear `backendLoopActive` + `launchedBackendPid`;
  - record the completion snapshot (`currentCommandState`, `postCommandState`) — **this also kills §37.2's
    reusable-dead-backend hazard**, whose reuse gate reads the stale `postCommandState` (`:22723`, `:23527`);
  - explicitly finalize completion ownership (`:13469`-`:13493`) rather than hoping for an incidental refresh.
  ⚠ **NOT on every terminal completion — §39.6.**

**L2 — fix the sink accounting (D1, the root).** Route the `peerResetAfterCleanClose` branch (`:29702`) through
`TupleSinkServiceMaybeReclaimSinkAndSession` so `activeSinkCount--` and the retirement check run **exactly
once**. ⚠ **It will not work naively:** the funnel's local-handle guard (`:27433`) returns early while the
**synthetic service-owned SEND handle** is open — `SERVICE_RESULT_OPEN` sets `sendHandleOpen=true` (`:39113`) and
**has no frontend response owner** (`:39141`-`:39149`), so nothing ever releases it. **The synthetic owner must
be released/waived first.** *(This is the piece with the most design risk.)*

**L3 — bound `WAITING_CLOSE_DRAIN` (D4).** A deadline + EOF escape so one dead client cannot permanently wedge
the singleton setup listener. **Separate defect, separate commit** — it is not part of the cliff, it is the
cascade, and it is a standing project-wide hazard.

**ACCEPTANCE (and note what the old acceptance could not see):**
1. **75-session loop → 75/75.** *A cap test must exceed the cap; all three caps here are 64.*
2. **`droppedMachineCount` == 0** in the new L0 diagnostic. *This is the real signal.*
3. gate band `-c 1 -t 2000` ×3 — no regression.
4. 4-role basebackup — the only byte-ring wrap exerciser.
5. SIGTERM teardown still exit 0 / zero refusals / drain reached (P2-k Part 1's result must not regress).

---

## §40 — ROUND 2: the fix plan, attacked. **L1(a) as written would have PERMANENTLY WEDGED the session.**

**Second adversarial round with Codex, on the §39.7 FIX PLAN. Every load-bearing claim below was VERIFIED BY ME
in the code before acceptance. It refuted L1(a) twice and reshaped L2 and L3. NO CODE WRITTEN YET.**

### 40.1 ⛔ THE UN-RETRACTION — there are **THREE WALLS**, and I only ever hit the first two

`AllocateSession`'s table-full reclaim requires **`active && activeSinkCount == 0 && ShouldRetireWhenIdle`**
(`:24273`). **VERIFIED.** D1's phantom sink pins `activeSinkCount ≥ 1` forever ⇒ **reclaim can never fire.**

| # | wall | fires at | status |
|---|---|---|---|
| **1** | **machine-candidate saturation** (D2/D3) — stale `WAIT_BACKEND` machines fill the 64-cap set; payload machines silently dropped | **session 64** | **OBSERVED** — the 30 s timeout |
| **2** | **setup-TCP singleton wedge** (D4) — session 64's close never certifies drained; node A's listener never frees `clientFd` | **sessions 65+** | **OBSERVED** — they never reach node B at all |
| **3** | **session-table exhaustion** (D1) — reclaim provably blocked by the phantom sink | *would* fire at session 65 | **LATENT. VERIFIED IN CODE, NEVER OBSERVED — because wall 2 fired first.** |

**Proof that sessions 65-75 never reached node B (by elimination):** had they entered the peer-open handler they
would have produced **either** a `DPU backend spawn begin` **or** `"ran out of free command sessions"`. They
produced **neither**. ⇒ the handler never ran for them.

> ## ⛔ THE RULE, AND I BROKE IT WHILE QUOTING IT
> **§38.6 says: "a count of zero on a line your theory requires is worth more than any amount of reading."
> TRUE — AND I THEN MISAPPLIED IT IN THE SAME BREATH.**
> ### A ZERO COUNT PROVES A LINE DID NOT EXECUTE. IT DOES NOT PROVE **WHY**.
> I read `0 × "ran out of free command sessions"` as **"the allocator coped."** It meant **"the allocator was
> never asked."** Absence of a failure message is equally consistent with *"no failure"* and *"never got there."*
> **Distinguish them by finding a line that WOULD have printed on the path you believe ran.** Mine was sitting
> in my own output: **`DPU backend spawn begin` stopped dead at 64.**

### 40.2 ⛔ L1(a) AS WRITTEN WOULD HAVE PERMANENTLY WEDGED THE SESSION — the **MANUFACTURED FAILED**

**VERIFIED (`:43873`–`:43887`).** The DPU egress **overwrites a COMPLETED completion's `commandState` to
FAILED** when the *result relay's* peer-open failed:

```c
if (sessionState->dpuResultPeerOpenFailed && CommandStateIsTerminal(...) && (resultFlags & ..._EOS) != 0)
{
	completionEvent->completion.commandState = CITUS_REMOTE_EXEC_COMMAND_STATE_FAILED;   /* MANUFACTURED */
	completionEvent->completion.resultFlags  = CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_FAILED;
	...
}
```

**The BACKEND IS ALIVE. Only the RESULT RELAY failed.** This runs *after* the only T1 teardown decision
(`:43622`–`:43660`), so `dpuBackendTeardownStarted` is still **false**.

⇒ Apply the host predicate to *that* `commandState` and you clear `backendLoopActive` with
`dpuBackendTeardownStarted == false` ⇒ DPU **landing excludes the session** (`:43363`–`:43367`) ⇒ **the client's
TX_ABORT / CLOSE can never land** ⇒ **the session can never quiesce.**

> **THAT IS THE EXACT FAILURE SHAPE OF THE §35 "fence-and-return" BUG I ALREADY MADE ONCE.** *Landing disabled,
> so the CLOSE can never arrive, so the thing can never finish.* **Second time. Same file. Same week.**
> **⇒ FIFTH proposed fix of mine in this audit that would have been a bug.**

**THE SAFETY PROPERTY THAT SAVES IT (VERIFIED):** the conversion writes `commandState`, `resultFlags`, and
`detail` — **it does NOT touch `commandKind`.** So keying L1(a) on **`commandKind == CLIENT_SQL_SESSION_CLOSE`**
is *structurally* immune to the manufactured state. **Key on the field that is never manufactured.**

**AND THE GENUINE-FAILED ARM HAS A SECOND, INDEPENDENT COLLISION (VERIFIED).** Clearing on a *real* backend
FAILED makes no-backend cleanup eligible (`:24618`–`:24654`) **while** teardown-fenced DPU landing also consumes
the next mailbox record — *including the CLOSE* (`:43378`–`:43402`). **Two owners for one mailbox record,
resolved by scheduler order.** ⇒ **DEFER the FAILED arm entirely; it is a separate lifecycle design.**

### 40.3 L2 RESHAPED — the reset branch is the ADJUNCT; the **T4 owner-release is the PRIMARY fix**

**VERIFIED:** node B's selected-DPU result takes its `activeSinkCount` increment at the **eager identity
reservation** (`:20563`), **before** the synthetic SEND handle is installed (`:39113`). `SERVICE_RESULT_OPEN`
then *reuses* that already-counted stream (`:38869`–`:38878`), so `:38921` is never reached.

**VERIFIED — the biggest omission in my L2: the NEVER-BOUND `SERVICE_RESULT_OPEN` FAILURE.** If OPEN fails
before binding, **neither** open-failure cleanup (`:38235`–`:38245`, which only funnels streams *it* created —
and here `createdStream == false`) **nor** the clean-reset branch (`:29692`–`:29703`, which needs a
connection-reset callback that may never come) retires the eager count. **⇒ Fixing only `:29702` leaves that
leak intact.**

**THE SHAPE:**
- **Do NOT release the synthetic SEND handle at OPEN** — that would let an ordinary semantic close reach the
  reclaim funnel (`:27863`) **before T4**, reclaiming a stream the DPU mirror may still write into.
- Add `TupleSinkServiceReleaseServiceOwnedDpuResultSinkAfterBackendTeardown`, called **only after T4** has
  released the arena, reset selected state, and stamped `teardownCompletedCleanly` (`:44657`–`:44678`):
  locate `dpuResultServiceSinkId` → validate (active stream, matching parent, `dpuRelayResultStream`, no receive
  handle) → set `sendHandleOpen = false` **idempotently** → call `TupleSinkServiceMaybeReclaimSinkAndSession` →
  **make NO further use of `sessionState`, because the funnel may have reset it** (`:27480`–`:27489`).
- ⚠ **The helper MUST accept `sendHandleOpen == false`** — the count predates the handle.
- ⚠ **The same release is required on the teardown-CANCELLATION path** (`:44510`–`:44523`), or a failed/unbound
  OPEN retains the eager count forever.
- **THEN** `:29702` becomes: clear peer binding → `MaybeReclaimSinkAndSession(...)` → return. **Never decrement
  or reset directly** — the funnel already guards inactive streams (`:27428`), checks underflow (`:27449`), and
  decrements exactly once (`:27458`).

### 40.4 L0 RESHAPED — **TWO** reports, and the second one matters more

**VERIFIED — SEVEN silent overflow counters, not six** (`:9484`, `:9536`, `:9554`, `:9573`, `:9592`, `:9612`,
`:9631`); all seven declared at `:3294`–`:3301`, reset at `:9086`–`:9093`, incremented at the append sites, and
**never read.**

**VERIFIED — but only the MACHINE array can actually overflow today.** Collectors merge by kind/index and are
bounded by the enum; critical-recv adds ≤ 1; control mailboxes are capped at 8; reset/liveness at 64; payload
failures come from a 64-bit bitmap. **So the other six are latent, not live.**

**⇒ VERIFIED — AND THERE IS A SECOND, TIGHTER, ALSO-SILENT BOUNDARY *AFTER* CANDIDATE CONSTRUCTION: POLICY
ADMISSION.** `:182`–`:195`:

```
HOMER_SERVICE_MACHINE_BASELINE_MAX_GRANTS          16
HOMER_SERVICE_MACHINE_BASELINE_MAX_MACHINE_GRANTS  12      <<<< twelve
HOMER_SERVICE_MACHINE_BASELINE_MAX_PAYLOAD_GRANTS   2      <<<< TWO
```
Budget denial only bumps a counter and returns false (`:10600`–`:10625`); those counters are **overwritten per
phase** (`:3502`) and printed **only at service exit** (`:8400`, `:48941`).

> **⇒ THE NEXT SILENT CLIFF IS ALMOST CERTAINLY THE 12/2 POLICY BUDGET, NOT ANOTHER CANDIDATE COUNTER.** And
> **the code says so itself**, at `:11913` — a comment written after this exact class of bug already cost a
> validation cycle:
> > *"⚠ **ARMING A CANDIDATE IS NOT ENOUGH** … armed on every pass and granted on none … the spawn began, the
> > peer response deferred, and the DPU then **sat forever without even a timeout** — because the timeout lives
> > inside the action that never ran. **Nothing warns you** … **the service looks idle.**"*
>
> DPU_SPAWN is **not** on the reserved-lifecycle list (`:10507`–`:10510`) and is ordered **late** (`:11748`+).

**L0 must therefore emit BOTH:** (a) machine-candidate overflow — **per PHASE**, since the set is reset per
phase, not per pass (`:45846`, `:48478`); copy the `HomerProgressMachineFacts` (kind/index/generation + state +
`readyActionMask`/`waitingOnMask`/`blockedReasonMask`, `:3002`–`:3025`), do not keep a pointer; and (b)
**per-phase policy-admission drops.** Reuse the existing first-8-then-powers-of-2 rate limit (`:9715`).

### 40.5 L3 RESHAPED — "deadline + EOF escape" is **NOT a safe contract**

**VERIFIED.** `WAITING_CLOSE_DRAIN` proves, *in sequence*, physical DMA drain → semantic lifetime →
selected-session finalization → host-mmap detach (`homer_service_dpu_setup_tcp.c:646`–`:731`). **EOF proves only
that the TCP owner vanished — it proves none of those.** And merely closing the socket **loses the continuation
identity**: `HomerServiceDpuSetupTcpCloseClient` clears bridge/client identity (`:477`–`:499`) while the import
is already `CLOSING` (`homer_service_dpu_dma.c:10277`).

⇒ **Either fail-stop on the deadline, OR a bounded abandoned-close record** that retains bridge/client identity
and continues drain/cancellation *after* freeing `clientFd`. **Not a bare timeout.**

### 40.6 ▶ THE PLAN OF RECORD (supersedes §39.7)

| step | what | changed by round 2? |
|---|---|---|
| **L0** | **TWO** diagnostics: machine-candidate overflow (**per phase**, copied facts) **+ per-phase policy-admission drops**. Then re-run the 75-loop and SEE both. | **YES** — the policy budget was not in the plan at all |
| **L1** | Clear `backendLoopActive`/`launchedBackendPid` **ONLY on a certified terminal `CLIENT_SQL_SESSION_CLOSE`**, keyed on **`commandKind`** (never manufactured). **DEFER the FAILED arm.** Snapshot `currentCommandState`/`postCommandState`/**`postCommandStateFlags`**. Finalize completion ownership, and make the index-lookup failure **LOUD**. | **YES** — the FAILED arm would have wedged the session |
| **L2** | **PRIMARY: the T4 (and teardown-cancellation) owner-release helper.** ADJUNCT: route `:29702` through the funnel. | **YES** — I had the primary and the adjunct backwards |
| **L3** | Bound the close-drain with **fail-stop or a bounded abandoned-close record** — **not** a bare deadline+EOF. Separate commit. | **YES** |

**ACCEPTANCE:** 75-loop → **75/75**; `droppedMachineCount == 0`; **policy-admission drops == 0 on the gate
path**; gate band ×3; 4-role basebackup; and P2-k Part 1's teardown result (exit 0 / 0 refusals / drain reached)
must not regress.

---

## §41 — **L0 LANDED AND VALIDATED.** The DPU now states the bug in plain English. (citus: this commit)

**L0 was built to give the silent caps a voice. It did — and it REFUTED MY OWN CHAIN TWICE before confirming
its root. That is exactly what "instrument before you patch" is for.**

### 41.1 THE MEASUREMENT — 63 dead sessions crowd out the one live one

75-session loop, node B, phase 3 (SEMANTIC — **the phase that actually grants machines**):

```
PROGRESS MACHINE-CANDIDATE OVERFLOW event=64 phase=3 kept=64/64 dropped_machines=1
  | KEPT[LOCAL_CONTROL_REQUEST=1 COMMAND_SESSION=63]  kept_in_WAIT_BACKEND=63
  | first_dropped=COMMAND_SESSION[63] gen=64 state=WAIT_BACKEND
    ready_actions=0x0000000000000000 waiting_on=0x00000004 blocked=0x00000000
```

| field | reading |
|---|---|
| `kept=64/64` | the machine candidate set is **FULL** |
| `KEPT[… COMMAND_SESSION=63]` | **63 of the 64 slots are session machines.** ⚠ **ZERO `PAYLOAD_STREAM` machines** |
| `kept_in_WAIT_BACKEND=63` | **all 63 are waiting for a backend that EXITED LONG AGO** |
| `first_dropped=COMMAND_SESSION[63] gen=64` | table slot **63**, service session **64** — **THE LIVE SESSION'S OWN MACHINE** |
| `waiting_on=0x4` | `HOMER_PROGRESS_WAIT_BACKEND_COMPLETION_RING` — it is waiting for its backend's completion |

> ## ⇒ SIXTY-THREE ZOMBIE SESSIONS EVICT THE ONE LIVE SESSION FROM THE SCHEDULER, EVERY PASS, FOREVER.
> Session 64 lands in table slot 63, is appended **last** (candidates are built in table-index order), and is
> therefore the one that falls off the end. **That is why the cliff is at exactly 64.** Its command is never
> planned. 30 s timeout. **(Then D4: its close cannot certify drained, node A's setup listener wedges, and
> sessions 65-75 cannot even connect — §39.4.)**

**The root (D2 / §37.3) is now MEASURED, not inferred:** stale `backendLoopActive` ⇒
`CompletionMachineHasWork` true ⇒ a `COMMAND_SESSION` machine in `WAIT_BACKEND` per retained session.
**`kept_in_WAIT_BACKEND=63` is that claim, printed by the machine itself.**

### 41.2 ⛔ AND IT REFUTED §39.2's LAST LINK — the starved machine is NOT the payload machine

**§39.2/§40.6 said: "payload machines are silently dropped."** **WRONG.** There are **no payload machines in the
set at all** — the set fills during *session*-machine construction (`:45801`), which runs **before** payload
construction (`:45806`), so payload machines are **never reached**, not "dropped."

**What is actually starved is the LIVE SESSION'S OWN `COMMAND_SESSION` MACHINE.** The mechanism is more direct
than I thought, and the fix is unchanged — but *"which machine is being starved"* is exactly the kind of detail
a fix gets keyed on, and I had it wrong.

### 41.3 ⛔ TWO DEFECTS **IN THE PROBE I WROTE TO ENFORCE MY OWN RULES**

**(a) A SHARED RATE LIMIT ACROSS PHASES IS A FILTER, NOT A LIMIT.** The first cut used ONE global
first-8-then-powers-of-2 counter. Phase 2 (COLLECTOR) overflows on nearly every pass and **ate the entire
budget**: phase 3 — *the phase that grants machines* — printed **2 lines out of 25**, and its `first_dropped`
was the only line that mattered. Fixed: **per-phase counters.** After the fix: 24 phase-2, **12 phase-3**.

> **RULE: A SHARED RATE LIMIT ACROSS DISTINGUISHABLE EVENT CLASSES SILENTLY SELECTS FOR THE NOISIEST CLASS —
> WHICH IS RARELY THE INTERESTING ONE.**

**(b) "FIRST DROPPED" ANSWERS THE WRONG QUESTION. THE KEPT CENSUS IS THE POINT.** The first cut reported only
the first *dropped* machine — and in phase 2 that is `PEER_CONNECTION`, which is simply *whatever is built last*
and says **nothing** about why the set was full. **I predicted `first_dropped=PAYLOAD_STREAM`; it printed
`PEER_CONNECTION`, and I could not tell whether my model was wrong or my probe was.** Fixed: a **kept-kind
histogram**, computed *inside* the rate limit (zero hot-path cost).

> **RULE: WHEN A BOUNDED SET OVERFLOWS, THE DIAGNOSTIC QUESTION IS "WHAT IS OCCUPYING IT", NOT "WHAT FELL OFF".
> The victim is an accident of ordering. The occupants are the bug.**

### 41.4 L0b — the policy budget is NOT the killer (today), but it is contended

`granted collectors=6 (cap 6), DROPPED collectors=2` in phase 2, every pass; **`machines=0 payload=0` dropped,
and NO phase-3 admission drops at all.** ⇒ **The 12/2 machine/payload budget is not starving anything today.**
The collector budget (6) *is* saturated — noted, not chased. **The next-cliff hypothesis (§40.4) is NOT
confirmed; it is now instrumented, which is what L0 was for.**

### 41.5 ▶ NEXT — the plan is UNCHANGED, and now it has a measured target

**L1** (clear `backendLoopActive`/`launchedBackendPid` on a certified terminal `CLIENT_SQL_SESSION_CLOSE`, keyed
on `commandKind` — never manufactured, §40.2) **directly kills the 63 zombies.**
**Acceptance is now precise and self-proving:** `kept_in_WAIT_BACKEND` must go to **~0**, the machine-candidate
overflow must **disappear**, and the 75-loop must reach **75/75**.

---

## §42 — **L1 LANDED AND VALIDATED. WALL 1 IS DEAD.** And wall 3 stepped forward exactly on cue.

**PREDICTION RECORDED BEFORE THE RUN, AND CONFIRMED IN EVERY PARTICULAR.**

### 42.1 The result

| | before L1 | after L1 |
|---|---|---|
| first failure | session **64** — 30 s timeout (scheduler starvation) | session **65** ✅ |
| failure mode | command never planned | **`peer service ran out of free command sessions`** ✅ |
| `MACHINE-CANDIDATE OVERFLOW` | 35 lines | **0** ✅ |
| flat ready-set overflow | 27 lines | **0** ✅ |
| session 64 | FAILED | **ok** ✅ |
| ALARM sweep | clean | clean |

**Both overflows are ZERO. The 63 zombie `COMMAND_SESSION`/`WAIT_BACKEND` machines are gone.**

### 42.2 The change (citus: this commit)

In `HomerServiceDpuEgressOneSelectedCompletionEvent`, after the certified pop — **exactly where P2-k Part 1
built the mechanism, using exactly the three scalars it already captures**:

- **L1(a)** clear `backendLoopActive` + `launchedBackendPid` — kills the zombie machine at its gate (`:45717`)
- **L1(b)** snapshot `currentCommandState` + `postCommandState` — **also closes §37.2's reusable-dead-backend
  hazard**, whose reuse gate reads that stale field
- **L1(c)** finalize completion ownership (`HomerServiceFinalizeCompletionPendingBit`), because clearing the
  flag makes `HasWork()` *return* false but **nothing on this arm ever ASKS** — with a **loud ALARM** on
  index-lookup failure, since both that lookup and the finalizer silently return on a miss

**KEYED ON `commandKind == CLIENT_SQL_SESSION_CLOSE`, NEVER ON `commandState`** — §40.2. The FAILED arm is
**deliberately not implemented** (the manufactured-FAILED wedge, plus the genuine-FAILED mailbox-ownership
collision). **Key on the field that is never manufactured.**

### 42.3 ✅ THE UN-RETRACTION (§40.1) IS NOW CONFIRMED BY MEASUREMENT

Session 65 failed with **`peer service ran out of free command sessions`** — the string that read **0 in every
previous run**, and which I once took as proof that *"the allocator coped."* **It meant the allocator was never
asked.** The session table **is** a wall; `AllocateSession` **does** return NULL. **§36.8 was right; my §38.4
retraction of it was wrong; §40.1 called it; L1 proved it.**

> **⇒ THE THREE WALLS, NOW ALL THREE OBSERVED:**
> **1.** machine-candidate saturation (session 64) — **KILLED by L1**
> **2.** setup-TCP singleton wedge (65+) — **did not fire** this run: sessions 65-75 failed *fast* with an
> honest error instead of hanging, so no client died mid-command. (D4 remains a latent hazard — out of scope
> by owner decision; fail-stop-on-deadline is the chosen contract when it is taken up.)
> **3.** session-table exhaustion (session 65) — **NOW EXPOSED. This is L2's target.**

### 42.4 ⚠ An instrument gap found by the run

The loop script greps node B's log for `"ran out of free command sessions"` and reads **0** — **but the client
saw it.** `TupleSinkServiceSetPeerErrorResponse` puts that text in the peer *response*; **node B never fprintf's
it.** ⇒ **That DPU-side check was decoration from the day it was written.** The client's error is the evidence.
*(Left as-is: fixing it means adding an fprintf to a cold error path. Recorded so the next reader does not
trust the 0.)*

### 42.5 ▶ NEXT — L2, and the acceptance that follows

**L2** (§40.3): the phantom sink. `activeSinkCount` is never decremented on the clean-close path, and every
retirement path — **including `AllocateSession`'s table-full reclaim (`:24273`)** — is gated on
`activeSinkCount == 0`. **PRIMARY: the T4 / teardown-cancellation owner-release helper. ADJUNCT: route the
`peerResetAfterCleanClose` branch through `TupleSinkServiceMaybeReclaimSinkAndSession`.**

**Full acceptance after L2:** 75-loop → **75/75**; `MACHINE-CANDIDATE OVERFLOW` == 0; gate band `-c 1 -t 2000`
×3 (**still owed — L1 is on the completion path; no perf check has been run yet**); 4-role basebackup; and
P2-k Part 1's teardown result (exit 0 / 0 refusals / drain reached) must not regress.

---

## §43 — **L2 LANDED. THE PHANTOM SINK IS DEAD.** And it uncovered a wall nobody could ever have reached.

**L2's own contract is MET and MEASURED. The 75-loop still does NOT pass — because L2 exposed a LATENT bug
that was unreachable for the entire life of this code. Recorded honestly; NOT yet fixed. ⚠ STOPPED HERE.**

### 43.1 What L2 fixed — measured

| probe | before L2 | after L2 |
|---|---|---|
| `reclaiming sink session=` (the `activeSinkCount--`) | **0** | **64** ✅ |
| `reclaiming idle non-reusable compatibility session` (**the retire**) | **0** | **64** ✅ |
| `DPU backend spawn begin` / `COMPLETED` | 64 / 64 | **75 / 75** ✅ |
| `MACHINE-CANDIDATE OVERFLOW` | 0 (L1) | **0** ✅ |
| my three new L2 ALARMs | — | **0** ✅ |
| ALARM sweep | clean | clean |

> **⇒ SESSIONS NOW RETIRE. `activeSinkCount` reaches zero. The 64-slot session table recycles.**
> **All three original walls are gone** — the machine-candidate saturation (L1), and now the session-table
> exhaustion (L2). **75 sessions allocate, spawn, and run a backend, where 64 was the hard ceiling.**

### 43.2 ⚠ WALL 4 — and it is the FIRST SLOT REUSE IN THIS CODE'S HISTORY

**11 failures remain; the first is session 65.** Node B's own log, side by side:

```
session 63 (healthy, 59 lines):     session 65 (failing, 3 lines):
  reserved DPU result stream          reserved DPU result stream
  DPU backend spawn begin             DPU backend spawn begin
  DPU backend spawn COMPLETED         DPU backend spawn COMPLETED
  landed peer command sequence=1      ...  ...and NOTHING. EVER.
  landed peer command sequence=2..5
  finalized pending DPU result stream
  started service-owned P3 result peer-open
  payload connection allocate ...
```

> ## ⇒ SESSION 65 SPAWNS ITS BACKEND AND THEN **NOT ONE COMMAND EVER LANDS.**

**WHY 65, AND WHY NOW.** Before L2, **sessions never retired, so every session got a FRESH table slot** —
sessions 1-64 took slots 0-63 and **slot reuse was never exercised, ever.** L2 made retirement work.
**Session 65 is the first session in this project's history to be handed a RECYCLED slot.** *L2 did not cause
this bug; it made it reachable.*

### 43.3 The suspects — **NONE VERIFIED. DO NOT ACT ON THESE WITHOUT INSTRUMENTING.**

`HomerServiceDpuLandOnePeerCommand`'s admission test ANDs **six** conditions (`:43826`ff). Exactly one of them
must now be false on a recycled slot. Also in play:

- **`HomerServiceSetPeerCommandLandingActive` (`:24404`)** keeps a per-session **flag** and a global **counter**
  (`peerCommandLandingSessionCount`) in sync, and **guards on the flag** (`if (flag == active) return;`). The
  scheduler arms the command-landing collector **only when that counter is `> 0`** (`:45503`). A flag/counter
  desync across reuse would produce EXACTLY "spawn works, nothing lands".
  ⚠ **BUT I READ IT AND IT LOOKS CORRECT** — `ResetSession` deactivates *before* its memset (`:24477`), and the
  reclaim path routes through `ResetSession` too. **So this suspect is PLAUSIBLE AND PROBABLY WRONG.**
- **`ActiveSessionScanLimit`** (`:624`, shrunk at `:6284`) bounds several hot scans. Retirement now *shrinks*
  it for the first time.
- `TupleSinkServiceCreateSession` memsets (`:24596`); `ResetSession` memsets (`:24535`). Both look ordered.

> ## ⛔ THE RULE THAT STOPS ME HERE, AND IT HAS BEEN PROVEN FIVE TIMES TODAY
> **FIVE mechanisms of mine have been refuted in this one session** (allocator exhaustion; "§37.3 named the
> wrong flag"; `connectionResetComplete` reclaim; "payload machines are dropped"; and the L1(a) FAILED arm that
> would have wedged the session). **Each was coherent, evidence-backed, and wrong.** The suspect above is a
> SIXTH story and it already smells wrong on a first read.
> **DO NOT PATCH. INSTRUMENT.** The probe writes itself: on a recycled slot, print each of the six landing
> conditions and the value of `peerCommandLandingSessionCount` — **the first condition that is false is the
> bug.** That is a one-run answer; a guess is a one-cycle detour.

### 43.4 ▶ STATE AND NEXT STEPS

**LANDED AND VALIDATED:** L0 (the silent caps, given a voice — `e5fd7a210`), L1 (the zombie machines —
`9ef06f224`), **L2 (the phantom sink — this commit).**

**OPEN:**
1. **WALL 4 (§43.2)** — instrument the six landing conditions on a recycled slot. **The next step, and it is a
   probe, not a patch.**
2. **The full acceptance is still owed** — 75/75; **the gate band `-c 1 -t 2000` ×3 (NO PERF CHECK HAS RUN for
   L1 or L2, both of which touch the completion path)**; the 4-role basebackup; and P2-k Part 1's teardown
   result must not regress.
3. **D4 / L3** — the setup-TCP singleton close-drain wedge. **Out of scope by owner decision** (a client dying
   mid-command is not an expected regime); **fail-stop-on-deadline is the chosen contract** when it is taken up.
   ⚠ A bare deadline is *cheap to do badly*: `WAITING_CLOSE_DRAIN` certifies **four** conditions and collapses
   them into one bool, so a useful ALARM needs a reason plumbed out of the callback.
4. **§40.4's "next silent cliff"** — the 16/12/2 policy budget — is **instrumented and NOT firing** (`machines=0
   payload=0` dropped). The collector budget (cap 6) *is* saturated. Watch, do not chase.

---

## §44 — 🔴 **WALL 5 FOUND, AND IT WAS IN THE LOG I NEVER OPENED.** Node A leaks 100% of its selected sessions.

**Status: CAUSE PROVEN (two-directional). NOT FIXED. Out with Codex for the fix design.**
**This supersedes §43.3's suspects — every one of them was wrong. The bug is not on node B at all.**

### 44.1 THE SMOKING GUN — node A's log

**Node B's log for session 65 is three lines and then silence.** So I finally read **node A's**, which I had
never opened. It is **9,422,990 lines**, and **9,416,076 of them are one message**:

```
tuple-sink service: DPU backend-command stage failed: selected-DPU session table is full
```

**THE TWO-DIRECTIONAL PROOF:**

| | **node A** (client-side DPU) | **node B** (backend-side) |
|---|---|---|
| `reset selected-DPU session on close` — **the ONLY release log line** | **0** | **64** |
| `selected-DPU session table is full` | **9,416,076** | **0** |
| `DPU backend spawn begin` | 0 *(correct — node A never spawns)* | 75 |

> ## ⇒ NODE A HAS NEVER RELEASED A SINGLE SELECTED SESSION. NOT ONE. ITS 64-SLOT TABLE FILLS AND STAYS FULL.

### 44.2 The mechanism — VERIFIED

`dpuDmaState->selectedSessions[]` is capped at **64**. `selectedSessionCount++` at `:42960`; *"selected-DPU
session table is full"* at `:42953`. It has **exactly two release sites**:

| site | function | arm |
|---|---|---|
| `:42395` | `HomerServiceDpuResetSelectedSessionForClose` | **node B — called by T4** |
| `:45100` | `HomerServiceDpuAbandonArenaTeardown` | **node B — T4's cancellation twin** |

**BOTH ARE ON THE NODE-B ARENA-TEARDOWN PATH. NODE A HAS NO ARENA BACKEND AND STRUCTURALLY NEVER RUNS EITHER.**
⇒ **Node A's selected-session table leaks 100%, permanently.** Once full, `HomerServiceDpuStageBackendCommand`
cannot stage the client's command, so **the command is never routed to node B** — which is exactly why node B
spawns a backend and then logs `landed peer command` **zero** times.

### 44.3 ⚠ IT WAS PERFECTLY MASKED — **TWO 64-CAPS, ONE BEHIND THE OTHER**

Node B's session table caps at 64 **and so does node A's selected-session table.** Node B's cliff fired at
session **64**, so **in the entire history of this code nobody ever reached session 65 to discover node A's
table was also full.** Kill node B's wall and node A's steps straight into the gap — which is precisely what
was observed.

> **⇒ THIS IS NOW THE SIXTH 64-SIZED RESOURCE IN ONE INVESTIGATION:**
> node-B session table · machine candidate set · flat ready set · `MAX_LOCAL_SINKS` payload streams ·
> peer-transport outgoing connections · **selected-DPU session table (both nodes)**.
> **When every resource shares a magic number, one exhaustion masks the next, and each fix reveals a new
> "regression" that was there all along.**

### 44.4 ⛔ THE RULE I BROKE, AND IT IS WRITTEN IN OUR OWN RUNBOOK

> **CLAUDE.md, verbatim:** *"The client only ever prints the SYMPTOM … **the DPU prints the cause**. When a
> client reports a Homer failure, **READ THE DPU LOG** before touching the source."*
>
> **I read node B's log. I never opened node A's.** I then built a suspect list (§43.3) entirely out of node-B
> code — a flag/counter desync, `ActiveSessionScanLimit`, the memsets — **and every one of them was wrong,
> because the bug is not on node B at all.**
>
> ## RULE: THE GATE HAS **TWO** DPUs. "READ THE DPU LOG" MEANS **BOTH** OF THEM.
> A failure on one node is routinely *caused* on the other. §43.3's suspect list is retracted in full.

### 44.5 ⚠ A SECOND, INDEPENDENT BUG: **9.4 MILLION LINES OF UNBOUNDED ERROR LOG**

That message is printed from a **hot retry loop with no rate limit at all**. It produced **9.4 M identical
lines**, timed out a `cat`, and would eventually fill the disk. **This project's own rule:** *"Always bound the
output. Retry/poll paths run millions of times per second: use first-N-then-every-Nth. A diagnostic that can
fill a disk is a bug."*

**And note the irony: it is simultaneously TOO LOUD and USELESS** — nine million copies of a message nobody was
reading, on the one node nobody was looking at.

### 44.6 ▶ NEXT

**The fix is NOT obvious and I have no plan for it yet** — `HomerServiceDpuResetSelectedSessionForClose`
certifies `commandInFlight`/`completionEventCount` and takes a `bridgeGeneration`, which are **backend/arena**
concepts. **What is node A's semantic equivalent of T4?** Out with Codex (round 3): where node A should release,
whether that function is even the right one, **what ELSE on node A leaks per session** (its log also shows
64× orphaned-stream reclaims, 64× byte-ring receive-queue failures, 64× discarded control state — *all stopping
at 64*), and the unbounded-log fix.

**Still owed regardless:** the gate band (**no perf check has run for L1 or L2**) and the 4-role basebackup.

---

## §45 — ✅ **WALL 5 FIXED. THE 75-SESSION LOOP PASSES 75/75.** It was a ONE-LINE WRONG FIELD READ.

**The 64-session cliff is GONE. Every wall is down. And §44's root-cause claim — mine — was WRONG.**

### 45.1 ⛔ MY §44 CLAIM WAS REFUTED (the sixth of the session, and I asked for it)

**§44 said:** *"Node A has NO release path — both release sites are on the node-B arena-teardown path."*
**REFUTED (Codex round 3, verified by me).** Node A **does** have a release path, registered with **no node-role
condition** (`:49403`):

```c
dpuSetupTcpConfig.closeFinalizeCallback = HomerServiceDpuResetSelectedSessionsForClosingSetup;
```

**It runs on node A. It just never recognizes a single session.**

### 45.2 THE BUG — a wrong field read, forbidden by a comment in its own file

`HomerDpuDmaSetupContainsServiceSession` (`homer_service_dpu_dma.c:10874`) asked:

```c
if (import->descriptors[ringIndex].serviceSessionId == serviceSessionId)     /* WRONG FIELD */
```

**The rule against exactly that is written 10,000 lines above it, on the accessor that exists to replace it**
(`homer_service_dpu_dma.c:757`):

> *"**THE one way** to ask 'which session does this ring serve?'. **Never read `descriptor->serviceSessionId`**
> for that question. The descriptor states what the host DECLARED at cold setup; for the frontend agent's
> shared arena **that is 0 for EVERY ring**, because the arena is exported once at postmaster start and its
> slots are handed to backends later, one at a time. **`ringRuntime` carries what the DPU has actually BOUND.**"*

⇒ The comparison was `0 == serviceSessionId` — **never true for a real session.** The predicate always answered
*"not mine"*, so the finalizer (`tuple_sink_service_process.c:42583`) `continue`d past **every** session and
**never released one**:

```c
HomerDpuDmaSetupContainsServiceSession(..., &setupOwnsSession, ...);
if (!setupOwnsSession) { continue; }                    /* <-- ALWAYS taken */
HomerServiceDpuResetSelectedSessionForClose(...);       /* <-- NEVER reached */
```

**THE FIX (one line):** `import->descriptors[ringIndex].serviceSessionId`
→ `HomerDpuDmaRingBoundSessionId(import, ringIndex)`. It repairs **all three** callers (`:42443`, `:42530`,
`:42583`) — the third being payload setup-drain ownership, broken the same way.

> ⚠ **THE DOC COMMENT NAMED THE WRONG FIELD AS THE CONTRACT** — *"maps a TCP setup-close identity back to
> **descriptor** service sessions"*. **That phrasing is where the bug came from.** Fixed with the code.
> **RULE: A DOC COMMENT THAT NAMES THE WRONG FIELD IS NOT A COMMENT, IT IS AN INSTRUCTION TO WRITE THE BUG.**

### 45.3 THE RESULT — 75/75

| | before | after |
|---|---|---|
| **75-session loop** | 11 failures, first at 65 | ✅ **ALL SESSIONS OK (75/75)** |
| node A `reset selected-DPU session on close` | **0** | **75** |
| node A `selected-DPU session table is full` | **9,416,076** | **0** |
| node A log lines | **9,422,990** | **7,944** *(1,186× smaller)* |
| node B spawn begin / COMPLETED | 64 / 64 | **75 / 75** |
| machine-candidate overflow · ready-set overflow · ALARMs | — | **0 · 0 · 0** |

**GATE BAND (`-c 1 -t 2000` ×3): `295.4 / 296.3 / 296.7`** vs the `2b37e8701` band `285.6 / 291.0 / 299.1`.
**IN BAND — no regression** across L1 + L2 + Wall 5, and unusually tight (1.3 tps spread).
**TEARDOWN: ACCEPT on both DPUs** — exit 0, zero refusals, drain reached. P2-k Part 1's result holds.

**4-ROLE BASEBACKUP: PASS** — sender rc=0, consumer rc=0, **`delivered_bytes=23,256,040,341`** (~21.6 GiB,
**~44,000 byte-ring laps**), `CLOSE_ACK`, **0 ALARMs on both DPUs**. ⚠ **This was NOT optional: L2 changed the
payload-stream reclaim path, and the gate never wraps the ring.** The wrap is clean.

### 45.4 The unbounded log — bounded, but **the spin is NOT fixed**

That `fprintf` (`tuple_sink_service_process.c:48021`) sat in a scheduler retry loop with **no rate limit** and
emitted **9,416,076 identical lines**. Now first-8-then-powers-of-2.

⚠ **THIS FIXES THE FLOOD, NOT THE SPIN.** Codex (VERIFIED): that path marks the failed attempt as *progress*
**without consuming the staged command**, so staged demand re-arms the collector and the service **busy-spins on
a command it can never place**. The real cure is **bounded overload rejection** — reserve selected-session
capacity at OPEN and fail the OPEN honestly, rather than accepting a session whose commands can never be staged.
⚠ **A naive capacity latch DEADLOCKS** (staged selection always returns the first global ring head; an
unreleased head starves every later ring). **Recorded as open work; do not mistake a quiet log for a healthy
service.**

### 45.5 Rules earned — and this is the one that cost the most

> ## ⛔ THE GATE HAS **TWO** DPUs. "READ THE DPU LOG" MEANS **BOTH** OF THEM.
> CLAUDE.md already says *"the DPU prints the cause — read the DPU log before touching the source."* **I read
> node B's and never opened node A's.** Then I built an entire suspect list out of node-B code (§43.3 — a
> flag/counter desync, `ActiveSessionScanLimit`, the memsets) and **every one was wrong, because the bug was not
> on node B at all.** The answer was sitting in 9.4 million lines nobody had ever looked at.
> **A failure observed on one node is routinely CAUSED on the other.**

> **WHEN EVERY RESOURCE SHARES A MAGIC NUMBER, EACH EXHAUSTION MASKS THE NEXT.** This investigation found
> **SEVEN 64-sized resources**: node-B session table · machine candidate set · flat ready set ·
> `MAX_LOCAL_SINKS` payload streams · peer-transport outgoing connections · **selected-DPU session table (both
> nodes)** · and the ready-set/plan grant cap. Node B's cliff at 64 hid node A's cliff at 64 **perfectly**, for
> the entire life of this code. Every fix looked like it caused a regression; every "regression" was already
> there.

> **A ONE-LINE FIX AT THE END OF A SIX-REFUTATION DAY IS NOT LUCK.** It is what instrumenting (L0), fixing what
> the instrument actually showed (L1, L2), and then reading the *other* log produces. **Each wall had to fall
> before the next was visible.**

### 45.6 ▶ OPEN

1. **Bounded overload rejection** for the selected-session table (§45.4) — the spin, not the log.
2. **Codex round 3 found two more leaks I have NOT verified or fixed:**
   - **selected-session allocation rollback** — a new entry is created before materialization/staged-slot
     release (`:43444`-`:43510`); either subsequent failure returns **without rolling back** the newly occupied
     entry. Same shape on the local arm (`:43546`-`:43610`).
   - **sender-stream heap/MR leak** — `localSenderHeadMirror.counters` allocated (`:18280`); reset **skips both
     MR deregistration and `free()`** (`:26004`, `:26035`) before erasing the only pointers (`:26054`). Affects
     node B's result sender.
3. **A clean-close CLASSIFICATION bug** (Codex, VERIFIED): `sessionState == NULL` takes the **failure-producing
   orphan branch** (`:30047`) *before* the `peerResetAfterCleanClose` check (`:30097`) — so a clean orphan is
   reclaimed but **falsely reported as failed**. On the path L2 touched.
4. **Watch the 1,024-entry import table** — verify every host-detached import gets a matching reclaim, or a
   growing delta predicts a **1,024-session wall**.
5. **D4 / L3** — setup-TCP close-drain wedge. Out of scope by owner decision; **fail-stop on deadline** chosen.

---

## §46 — P2-M: the leftovers, triaged. **ONE FIXED. ONE REFUTED. ONE DEFERRED (and it is real).**

**Codex's three round-3 findings, each VERIFIED BY ME IN THE CODE before acting. Its `VERIFIED` label is not
evidence — and one of the three did not survive.**

### 46.1 ✅ FIXED — the clean-close misclassification. **A LYING DIAGNOSTIC ON THE HAPPY PATH.**

`HomerServiceClearAbortedPayloadStreamAfterReset()` took the orphan-**FAILURE** branch on
`sessionState == NULL` **before** consulting `streamEntry->stream.peerResetAfterCleanClose`.

**⇒ EVERY CLEAN CLOSE ON NODE A WAS PUBLISHED AS A REAL PEER FAILURE, ON EVERY SUCCESSFUL RUN.**
A 75-session run produced **64× `abort payload clear could not find owning session`** and **64×
`marked byte-ring receive queue failed … reason=peer-reset-orphan`** — on runs that succeeded end to end.

> ## ⛔ THE RULE WAS ALREADY WRITTEN. ENFORCED AT ONE SITE. VIOLATED AT THE OTHER.
> The **marking** pass (`…MarkPayloadStreamAbortingOnConnectionReset`, `:~29309`) states it verbatim:
> > *"On node A the owner session is GONE by the time the reset runs … `ownerSession == NULL` was therefore read
> > as 'NOT a clean close' when it actually means 'the owner already left'; **the stream was then relabelled a
> > REAL PEER FAILURE on every successful run.** … **A later lookup that cannot find the session must not
> > un-decide it. So: once clean, always clean.**"*
>
> Someone found this exact bug, wrote the rule, and fixed it **there**. The **clearing** pass never got the memo.
> **THIS IS WALL 5'S SHAPE, FOR THE THIRD TIME THIS WEEK.** ⇒ Now a `CONTRACTS.md` entry **with an `ENFORCES`
> list** — *the only mechanism that catches this class.*

**RESULT:** phantom failures **64 → 0**; honest clean reclaims **0 → 75**. Loop **75/75**. ALARMs 0.
**Gate band `298.9 / 299.8 / 303.0`** (band `285.6 / 291.0 / 299.1`) — no regression. **Basebackup PASS**
(23.26 GB, `CLOSE_ACK`, 0 alarms — it is on the payload-reclaim path this fix touches). Teardown ACCEPT ×2.

### 46.2 ⛔ REFUTED — the "selected-session allocation rollback leak". **CODEX WAS WRONG.**

Codex: *"a new entry is created before materialization; either subsequent failure returns without rolling back
the newly occupied entry."* **True as a statement about the code. NOT a leak.**

- **It is `FindOrCreate`.** On a retry the record is **found**, not re-created ⇒ **no per-retry leak.**
- **Release is keyed by SESSION CLOSE** (the setup-close finalizer / T4), **not by staging success** ⇒ there is
  **nothing to roll back.** The record is reclaimed when the session closes, whether or not a command ever staged.
- **The early create is DELIBERATE and documented** (`:~43444`): *"Create it BEFORE any irreversible step … so a
  failure is a clean early return."*
- **⚠ THE RESIDUAL RISK IS REAL BUT IT IS A DIFFERENT LINE:** `HomerServiceDpuResetSelectedSessionForClose()`
  **REFUSES to reset** a session with `commandInFlight` or `completionEventCount > 0` — *that* could strand a
  record. All three post-create failures are **ALARM-grade protocol violations** that never fire on a healthy run.
- **⇒ NOT FIXED. Recorded.** *(And it is **not** the Homer frontend API — all three call sites are inside the DPU
  service's own scheduler: `…StageOneBackendCommandForPublish` and `…LandOnePeerCommand`.)*

### 46.3 🔴 DEFERRED, AND IT IS A REAL LEAK — the sender head-mirror MR/heap leak

**VERIFIED.** `HomerServiceResetPayloadStreamEntry()`:

```c
if (!deferSenderHeadMirrorDeregistration) { DeregisterPeerMemoryRegionRdma(...); handle = NULL; }
if (!deferSenderHeadMirrorDeregistration && counters != NULL) { free(counters); counters = NULL; }
...
memset(streamEntry, 0, sizeof(*streamEntry));      /* <<<< ERASES BOTH POINTERS */
```

**When the defer flag is set, neither the MR nor the `calloc` is released — and the `memset` then destroys the
only pointers to them.** `deferSenderHeadMirrorDeregistration` is **set in ONE place, read in TWO, and NEVER
CLEARED. There is no "later."**

**And its condition is the gate's hot path:** locally-initiated + `TUPLE_VIEW_BATCH` + byte-ring + has an MR —
**that is node B's result sender on EVERY session.** ⇒ **one leaked RDMA memory region per gate session.**

> **⚠ THE FIX IS NOT "MOVE THE `free()`."** The defer is **protective**: it exists because RDMA may still be
> writing into that MR. Deregistering early re-creates precisely the hazard P2-i was about — ***`free()` is not
> inert***. **The fix needs a QUIESCENCE PROOF first** (no outstanding sends against that MR), then dereg + free.
> Same shape as P2-i's drain-before-destroy.
>
> **⇒ A "correct-but-incomplete" defer: it avoids a use-after-free by never freeing.**

**Why deferred:** MRs are finite but plentiful; we restart services between runs, so it never accumulates on this
harness. **Real, unbounded in a long-lived service, non-trivial to fix. Recorded, not urgent.**

### 46.4 Also settled with the owner (2026-07-14)

- **The staging spin / bounded overload rejection — DEFERRED.** Only reachable with **65+ CONCURRENT** sessions;
  **the gate caps at 8** (byte-ring pool). If we need more, **raise the constant**. The 9.4 M-line log flood is
  already bounded.
- **D4 (setup-TCP close-drain wedge) — NOT FIXED, and the reasoning is better than mine.** The thing that should
  be loud is **the client dying mid-command** — the abnormal event — and it already *is* (pgbench errors, the run
  fails). The DPU's silent wedge afterwards is a *consequence*, and the standing rule already covers it: **any
  aborted/timed-out run ⇒ restart both DPU services before the next.** *(If ever taken up: **fail-stop on
  deadline**, and a bare deadline is not enough — `WAITING_CLOSE_DRAIN` ANDs **four** conditions into one bool, so
  a useful ALARM must plumb out **which** one failed.)*

---

## §47 — P2-N: the sender head-mirror MR leak. **FIXED — and the fail-stop I added to keep it honest caught TWO more bugs, one of them a documented, routine crash.**

**Status: LANDED AND VALIDATED** (citus: this commit). Validated on the real 4-machine setup, not argued.

### The leak

`streamEntry->stream.localSenderHeadMirror` is **Scenario C** memory: we own an 8-byte heap counter, register it as
an RDMA MR, and publish its rkey so the **remote consumer** can WRITE its consumed-head into it. We can never see the
peer's send CQEs, so deregistering it needs a *proof* the peer will not write again.

There was a `deferSenderHeadMirrorDeregistration` flag that was supposed to supply that. **It never did.** It was set
in one place, read in two — *both of which merely SKIPPED the cleanup* — and cleared **nowhere**; then the `memset` at
the end of `HomerServiceResetPayloadStreamEntry()` erased the only pointers.

> **It was not a deferred cleanup. It was an ABANDONED one — one RDMA MR + one heap block leaked PER SESSION.**

**Why deferral is not needed at all.** `HomerServiceClearPayloadStreamPeerBinding()` already `exit(1)`s unless
`HomerServicePayloadCloseCanClearBinding()` holds, i.e. either
- `RECLAIMABLE` — the sender has **OBSERVED the peer's FINAL head WRITE**. RDMA WRITEs complete **IN ORDER on a QP**,
  so observing the final head proves every *earlier* head-ACK WRITE already landed — which is exactly the "older ACK
  WR still in flight" case the old comment feared; or
- reconciled `ABORTING` — reset-complete already destroyed the QP/CQs and invalidated every MR.

⇒ **By the time the defer flag was ever set, the peer PROVABLY could not write.** The defer was dead weight guarding an
already-excluded hazard.

### The fail-stop tripwire — and why it earned its keep twice

Because that argument is load-bearing, `HomerServiceResetPayloadStreamEntry()` now **ALARMs and `exit(1)`s** if it ever
meets a still-registered mirror. It immediately found **two** bugs that had been invisible:

**(1) MID-TRANSFER SIGTERM.** Shutdown resets every `active` stream (`:~49963`) *before* destroying the peer transport
(`:~50024`). A stream still OPEN at SIGTERM therefore hit reset with a live mirror and no proof ⇒ `exit(1)`, and the
service **never reached `HomerDpuDmaDestroy`** — re-breaking the very teardown drain this series exists to restore.
*Fixed:* the shutdown loop now retires explicitly under a **named** reason, `SERVICE_EXIT`. Proof-free, and safe **only**
there: the transport (with every QP and MR) dies a few statements later, so a poisoned QP has **no next user**; the
dereg precedes the heap free; and the only thing that still touches the transport afterwards
(`TupleSinkServiceResetSession` → `HomerServiceDrainSessionSendCompletions`) is a **BOUNDED** drain that ALARMs and
cancels its owners on non-convergence rather than blocking.

> ⚠ **A claim I got WRONG and had to correct:** *"nothing uses the QP after the stream reset."* **False** — the session
> reset loop does. The claim survives only in its stronger form: *everything that touches the QP after that point is
> bounded and has an explicit non-convergence escape.* Found by checking, not by asserting.

**(2) 🔴 THE `-c > 8` BYTE-RING REFUSAL — A ROUTINE, DOCUMENTED PATH, AND IT KILLED THE SERVICE.**
Codex (adversarial review) found that the async peer-open registers the mirror at `STREAM_REGISTER_MIRROR` (`:~40062`)
**BEFORE** `peerBindingLocallyInitiated` / `sendHandleOpen` are set (`:~39839` / `:~39864`, only once the peer's response
validates). So in that window the mirror is live while **every "am I the sender?" bit reads false**. The retire's guard
tested exactly that bit — so it **skipped** the window, the reclaim funnel (whose guard is
`LocalHandleCount != 0 || peerBindingActive`, both false here) proceeded to reset, and the tripwire fired.

I first recorded this as "latent, never observed." **It is not latent.** It is the **documented byte-ring-pool
exhaustion**: the pool has 8 slots, so at `-c 10` exactly 2 sessions are refused and the peer REJECTS their
result-relay open. **MEASURED:**

```
[node B] still alive: *** NO -- IT DIED ***   RAW EXIT STATUS = 1
[node B] head-mirror TRIPWIRE lines : 1
ALARM: ... session=9 peerBindingActive=0 peerBindingLocallyInitiated=0 closeState.phase=OPEN
```

The runbook says `-c 10` *partially fails*. With the tripwire it **crashed the DPU service**; before the tripwire it had
been **silently leaking an MR per refused session**.

*Fixed:* **the direction discriminator was simply WRONG.** A receiver never registers a `localSenderHeadMirror` at all
(node A: `registered=0`, measured every run), so the `peerBindingLocallyInitiated` test was **redundant for its stated
purpose AND set later than the thing it guarded**. Dropped it — **the registered handle IS the ownership proof** — and
the reclaim funnel now retires under a third named reason, `OPEN_FAILED`.

> **RESIDUAL RISK, stated honestly (open):** if the peer **ACCEPTED** the open and bound our descriptor and only *our*
> validation of its response then failed, the peer may still write ⇒ `IBV_WC_REM_ACCESS_ERR` ⇒ poisoned QP. That hazard
> is **pre-existing** (the old reset deregistered there too, just after leaking the heap), and it is **not** what the
> `-c 10` case does — a *rejected* open means the peer never bound anything. **The clean fix is to reset the peer
> connection on a post-publication open failure**, which yields proof (b). ⛔ Do **not** "improve" this into a deferral;
> that is what leaked in the first place.

### The instrument: prove it, don't assume it

The tripwire's **silence is a ZERO COUNT**, and a zero count cannot distinguish *"every mirror was retired"* from
*"none was ever registered, so the test was vacuous."* So the service now prints, at teardown:

```
head-mirror MR accounting: registered=N released_on_rebind=N retired_on_clear=N retired_at_exit=N
                           retired_on_open_failure=N live=N
```

- **`live` MUST be 0.** It also catches what the tripwire structurally *cannot*: the shutdown loop only visits `active`
  streams, so an **inactive** entry still holding an MR is invisible to reset — but not to this subtraction.
- ⚠ **`registered == retired` is the WRONG invariant.** `TupleSinkServiceEnsureLocalSenderHeadMirror()` can
  dereg-and-**RE-register** in place when the peer connection changed. That is a *replacement*, not a retirement;
  folding it in would manufacture a phantom leak on every reconnect. Hence `released_on_rebind`.
- ⚠ **PLACEMENT IS LOAD-BEARING:** the line prints **after** the stream loop but **before** the session loop.
  `TupleSinkServiceResetSession` carries its **own, unrelated** fail-stop (open peer command-mailbox MR whose writers
  have not quiesced), which fires on any teardown following an aborted run — and printing after it meant the accounting
  **silently vanished on exactly the runs that needed it** (measured, on the `-c 10` run itself).

### Validation (final binary, both DPUs redeployed with a landed-proof grep verified ABSENT beforehand)

| workload | result |
|---|---|
| 75-session gate loop | **75/75**; node B `registered=75 on_clear=75 at_exit=0 on_open_failure=0 live=0`; ACCEPT both DPUs |
| gate band `-c 1 -t 2000` ×3 | **303.2 / 302.3 / 301.5** vs band 285.6 / 291.0 / 299.1 — **no regression** |
| 4-role basebackup | **PASS**, 23,258,416,190 B, `CLOSE_ACK`, ~44k ring laps, `on_clear=1 live=0`, 0 alarms |
| SIGTERM teardown (clean) | **ACCEPT** both — exit 0, 0 refusals, drain reached |
| **mid-transfer SIGTERM** | **exit 0**, drain reached, `at_exit=1` *reported* (was: exit 1, no drain) |
| **`-c 10` byte-ring exhaustion** | **node B SURVIVES** (was: DIED); `registered=10 on_clear=4 at_exit=4 on_open_failure=2 live=0` |

Node A reports `registered=0` on every run — it is the **receiver**; it holds the *peer's* descriptor, not a local
mirror. **Predicted from code, then confirmed by measurement.**

---

## §48 — teardown ordering. **A mid-COMMAND SIGTERM cannot exit cleanly, and the shutdown order contradicts the transport's own reset contract.**

> **§48a (the `exit(1)` — "cannot exit cleanly") → ✅ FIXED + VALIDATED as §36 PART 2, see §36.10 (2026-07-15).**
> §48a is the same item as §35/§36's P2-k backstop; the mid-command SIGTERM now DEFERS the reset instead of
> `exit(1)`-ing, so `HomerDpuDmaDestroy` runs. **§48b (the shutdown-ORDERING contradiction, below) stays as
> analysis only** — §36.5/§35.9 established the naive reorder is a use-after-free and the fence/defer makes it
> unnecessary; the strictly-correct QUIESCE/RELEASE split is deferred to P7b. The rest of this section is §48b.

> ⚠ **§48a IS NOT A NEW BUG — IT IS §34.12's ALREADY-OPEN ITEM, re-observed in P2-N's tripwire context.**
> The header table's `🔴 exit path that reaches HomerDpuDmaDestroy NEVER` row and §34.12's bottom subsection
> (*"NOT FIXED, AND IT BOUNDS P2-i"*) describe the SAME exit(1): same *"refusing to reset peer
> CLIENT_SQL_SESSION before command-mailbox writers quiesce"*, same never-reaches-`HomerDpuDmaDestroy`, same
> trigger (SIGTERM shortly after a gate run — §34.12 saw `current_sequence=14004`, §48 saw `19904`; both are
> `2000 txns × 7 statements`, un-quiesced). **I filed it as "new" during P2-N — the exact append-vs-update
> hazard this file's header box warns about.** What §48 genuinely ADDS beyond §34.12 is **§48b**: the
> shutdown-ORDERING contradiction (streams→sessions→transport vs the transport's own destroy-QP-first
> contract) and why the naive reorder is a UAF. The header's 🔴 row now points here for that analysis.

**Not caused by §47** — §47 merely stopped masking it (and §34.12 had already recorded it). Bring to the user
before starting; it is a stage, not a patch.

**The symptom (MEASURED).** SIGTERM node B while the gate is mid-command:

```
head-mirror tripwire    : 0        <-- §47 is fine
session-reset fail-stop : 3
  "refusing to reset peer CLIENT_SQL_SESSION before command-mailbox writers quiesce
   session=1 current_sequence=19904 ... peer_close=0 reset_complete=0"
RAW EXIT STATUS = 1     teardown drain = 0 lines
```

The refusal is **honest** — the writers really have not quiesced — and it is **pre-existing** (zero occurrences in the
§47 diff; identical at citus `29aced926`). But it means **the service cannot be stopped cleanly while a command is in
flight**, and it fires on **any** teardown that follows an aborted run.

**The deeper inconsistency.** Shutdown does *streams → sessions → transport*. But the transport's **own** reset code
encodes the **opposite** contract — destroy QP/CQs **first**, *then* invalidate MRs and reconcile owners
(`remote_execution_peer_transport_rdma.c:~6479-6493`).

⚠ **The naive "destroy the transport first" reorder is ALREADY REFUTED** (see §-earlier, and Codex confirmed):
`TupleSinkServiceDestroyPeerTransportState()` frees the transport container, but the subsequent session cleanup still
dereferences stored connection handles ⇒ **use-after-free**.

**Candidate direction (Codex, INFERRED — not yet verified by us):** split transport shutdown into phases —
(1) fence/reset all connections using the existing QP/CQ-destroy → owner-abort → MR-invalidation → reset-complete
sequence, but **retain** the transport/connection-slot allocation; (2) cancel shutdown-owned session/FIFO owners, then
reset streams and sessions while the stored handles still address inactive-but-allocated slots; (3) unmap control state
and only then free the container.

**Also unresolved and worth folding in:** `TupleSinkServiceDeregisterPeerMemoryRegionRdma()` **ignores `ibv_dereg_mr()`'s
return value** and frees the handle regardless (`remote_execution_peer_transport_rdma.c:~10225-10246`). So every
`retired_*` count records an *attempted* retirement, not a *successful* deregistration — and on failure both the
no-scribble proof **and** the accounting are false.

---

## §49 — P2-o: RETIRING / abandonment — DECIDED (owner, this session): **instrument-and-observe, NOT fatal.** And a CORRECTION to "abandonment is expected."

**Supersedes §22.10.1's "needs fault injection" disposition and its "owner timed out, THEN the response landed"
characterization — both were imprecise. See the correction below.**

### The corrected understanding (record both sides)

I earlier called abandonment "expected." **That was overstated, and the owner rightly pushed back.** Corrected:

- **In a HEALTHY run, BOTH abandonment paths should fire ZERO times.** "Handled defensively" ≠ "expected in
  normal operation."
- **There are exactly TWO abandonment triggers, and neither is the "owner timed out" the header row claimed:**
  1. **Partial peer-control publish** (`remote_execution_peer_transport_rdma.c:12163`, sets `ownerAbandoned`,
     ALARMs) — an RDMA multi-WR post that accepted some WRs but not all. A **transport error**. Zero in a
     healthy run.
  2. **Stale async op / slot recycled** (`tuple_sink_service_process.c:41036` → `TupleSinkServiceAbandon`
     `PeerControlOpRdma`, `:12464`, logs *"abandoning peer control op"* at `:12523`; the drop is logged
     *"dropping stale async local-control op"* at `:41028`) — the SHM slot's `requestSequence`/`state` changed
     under an in-flight async op.
- **⚠ UNVERIFIED: whether the stale-async race is genuinely benign.** The code AUTHOR treated it as recoverable
  (the *invalid* owner/slot cases immediately above it — `:41011`, `:41024` — `abort()`; the STALE case merely
  drops-and-continues). **But "the author judged it legitimate" is not proof.** Confirming it means tracing the
  async-op re-entrancy + the full slot lifecycle (FREE→CLIENT_OWNED→REQUEST_READY→SERVICE_OWNED→RESPONSE_READY,
  `homer_shm_channel_abi.h:32-36`) and who can move a slot out from under the service. **NOT DONE.**

### The two real failure modes (what RETIRING is actually about)

RETIRING = abandoned + response terminal + **send WR not yet retired**. The slot IS the registered RDMA source
MR (`remote_execution_peer_transport_rdma.c:12140` comment). So:

1. **LEAK** — a RETIRING op that never exits (send CQE never comes AND the connection never resets). Its slot is
   stuck; finite control-op slots exhaust → new ops cannot allocate. Exits: normal via the send CQE
   (`:4383` → release), or discarded by connection reset (`:5148`, `retiringControlOpsDiscarded++` `:5151`).
2. **EARLY RELEASE** (the opposite) — releasing a RETIRING slot before its send retires ⇒ MR reused under a live
   WR ⇒ corruption. **RETIRING exists precisely to prevent this.**

### THE DECISION (owner)

**(1) Instrument-and-observe. Loud, counted, NOT fatal.**

- ⛔ **NOT fatal on RETIRING entry.** RETIRING is CORRECT cleanup and is legitimately reachable on connection
  reset, where `exit(1)` makes a bad situation worse (kills every other session on the DPU). **Fatal tripwires
  are for CODE-INVARIANT violations (e.g. the FB-1 bijection), never for RUNTIME RECOVERY paths.**
- **Count abandonment on both paths**, tagged distinctly (partial-publish vs stale-async), and **report the
  counts at service teardown.** A healthy gate/basebackup run WANTS `0/0`. Nonzero ⇒ a real finding (either the
  stale race is live, or a transport error is happening quietly) ⇒ investigate then.
- **RETIRING entered-vs-exited accounting** (entered vs released-normally + reset-discarded), reported at
  teardown; a gap ⇒ a RETIRING op leaked. Same shape as the head-mirror MR ledger (§47).
- **The reset-discard path stays a LOG, not an ALARM** — it is legitimate when the connection genuinely died.

**(2) NO deliberate validation run for this.** Owner: *"I doubt it's going to happen."* The counters ride along
on the NEXT validation we do anyway (the (A)+(C) batch, or S6). If abandonment is 0/0 there — as expected — then
"abandonment never happens in a healthy run" is confirmed EMPIRICALLY and RETIRING is proven dead-in-practice,
without a bespoke run. Only if a count comes back nonzero do we open the stale-race investigation.

### Implementation surface (file:line)

- entry counters at the 3 RETIRING transitions: `remote_execution_peer_transport_rdma.c:8841, :12538, :12617`
- abandonment counters at the 2 triggers: `:12163` (partial-publish), `tuple_sink_service_process.c:41036`
  (stale-async)
- normal-release counter at `:4383`; reset-discard already counts (`retiringControlOpsDiscarded`, `:5151`)
- teardown report: alongside the existing service-exit accounting (same site as the head-mirror MR ledger)

### IMPLEMENTED + VALIDATED (2026-07-14, P2-o batch — committed with the §32 inline-path deletion)

Shipped as a file-static lifetime ledger in `remote_execution_peer_transport_rdma.c` (5 counters +
`TupleSinkServiceReportPeerControlRetirementAccountingRdma`), called from the teardown SAFE WINDOW in
`tuple_sink_service_process.c` (right after the head-mirror MR ledger, before the session-reset loop and
`TupleSinkServiceDestroyPeerTransportState` — same placement contract, same reasons: an unrelated fail-stop must not
silence it, and `live` must still see ops the peer teardown has not yet discarded). NON-FATAL; the `ALARM` token
(which the acceptance sweep rejects on) is raised ONLY for an accounting underflow or a nonzero abandonment count. A
nonzero `live` is reported WITHOUT the token — an abrupt/SIGTERM teardown makes it legitimate and there is no clean
at-exit census to subtract, so ALARMing it would make the sweep lie on every SIGTERM run.

**⚠ CORRECTION to this section's own framing — RETIRING is NOT only an abandonment state.** There are THREE entry
sites, and only two involve abandonment:
- `:12617` (`TupleSinkServicePollPeerRequestRdma`) — **NORMAL, no abandonment**: a poll consumed a COMPLETED response
  before our own request send CQE was reaped. Healthy CQ-drain skew. This is why `retiring_entered` *can* be nonzero
  on a clean run, and why the accounting invariant is `entered == released_normally + reset_discarded` (a GAP is the
  leak signal, not a nonzero count).
- `:8841` (response arrived for an already-abandoned op) and `:12538` (stale-async abandon of a terminal op) — the two
  abandonment flavors.
`released_normally` is counted inside `TupleSinkServiceReleasePeerControlOp` guarded on `phase == RETIRING` (the
unique normal exit — verified `:4390` is its only RETIRING-phase caller; reset-discard memsets directly at `:5153`
and never routes through the releaser).

**VALIDATION RESULT** (gate `-c 1 -t 2000` + 4-role basebackup): both DPUs printed
`retiring_entered=0 released_normally=0 reset_discarded=0 live=0 abandoned_partial_publish=0 abandoned_stale_async=0`,
no ALARM. So:
- **Abandonment 0/0 is now confirmed EMPIRICALLY** (the code ran and printed 0) on BOTH nodes — the §49 expectation
  holds. The stale-async race did not fire, so its "is it truly benign?" question stays OPEN but un-triggered (chase
  only if a future run reports a nonzero `abandoned_stale_async`).
- **`retiring_entered=0` — RETIRING was never entered on this workload** (each control-op send CQE is reaped before
  its response is polled), matching the original §22.10.1 observation that it is dead-in-practice. ⚠ **HONEST CAVEAT:**
  the RETIRING counters are therefore validated **by construction (self-review), not by execution** — the very
  "validated by construction, never by execution" caveat §22.10.1 records for this same branch. The abandonment
  counters ARE execution-validated (they ran and printed 0); the RETIRING counters are not (never incremented).

## §50 — D-S0: the two latent CQ-dispatch/admission holes, HARDENED. (The coalescing plan's commit 0.)

**Status: IMPLEMENTED; VALIDATION EVIDENCE BELOW.** Found by the send-CQE-coalescing design refutation
(`send_cqe_coalescing_implementation_plan.md` D-S0), but both are HEAD bugs independent of coalescing — they
are §0-contract violations of the F7/§25 rule ("a dequeued CQE is the ONLY retirement event its owner gets, so
every CQ consumer must be able to dispatch every CQE").

### 50.1 Blind send-CQ drains — the rule existed, the enforcement was an alarm, and three shapes violated the premise

- `TupleSinkServiceDrainTaggedSendCompletions` (R:~4950) passed **NULL callbacks**; its one caller is the
  control-response-slot exhaustion drain (R:~9110). On a QP carrying typed CQEs (CRITICAL_CONTROL, payload
  connections) a typed CQE dequeued there was DESTROYED — "RETIREMENT EVENT LOST" fired, but the event was
  still lost. Rare at HEAD only because every WR signals and the scheduled drain keeps CQs short.
- The peer-pump SEND_CQ phase (R:~13210) polled with NULL callbacks — same shape.
- The blocking waiter's precondition **EXEMPTED `CRITICAL_CONTROL`** from requiring callbacks — written when
  control lanes carried no typed CQEs, silently invalidated when the command-plane migration put the
  command-write and peer-client-completion lanes ON that very QP. A stale exemption aging out under a
  migration: the exact wrong-neighbor shape CONTRACTS.md exists for.

**Fix (one choke point, not three patches):** `TupleSinkServiceDrainTaggedSendCompletionsInternal` now
REFUSES (`callbacks == NULL` → once-latched ALARM + false) before the first poll — no caller can blind-poll,
current or future. The wrapper and the pump pass the persistent registered dispatch
(`HomerServicePersistentSendCompletionCallbacks`, registered at service init before any connection exists);
the CRITICAL_CONTROL exemption is deleted. ⚠ The persistent callbacks acquired a **RETIRE-ONLY contract**
(comment at the struct, `T:~16190`): since D-S0 they also run from mid-operation transport drains that the
`HomerServiceSendCqDrainDepth` recursion guard does NOT bracket — a future callback that posts or polls must
first extend that guard. (Adversarial review confirmed all three current callbacks are retire-only, and that
the exhaustion drain runs BEFORE any response slot is held, so no in-flight publish state is exposed.)

### 50.2 The admission ceiling was the RAW provider grant; the mark table is sized by the REQUEST

`signalledFrontierMark[]` has `CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH` (256) entries, but admission used the
provider-granted `cap.max_send_wr` verbatim — a grant > 256 plus > 256 outstanding signalled WRs would
overwrite unconsumed marks (mark overwrite PROVEN from code; CQ overrun plausible but unproven — the granted
CQ depth was never inspected). The setup-time capacity check guarded the payload staging pools against the
raw grant and nothing guarded the mark table: the capacity proof counted one thing again.

**Fix:** the stored ceiling is clamped to `min(granted, 256)` and the field is **RENAMED
`grantedSendWr` → `admittedSendWr`** so the name cannot re-imply the raw grant (the old field comment had
become a lie the moment the clamp landed). One new cold-path log line per connection —
`peer QP caps host=... requested_send_wr/granted_send_wr/admitted_send_wr/granted_send_cq_entries` — because
until now the grant was visible ONLY inside error messages. (Incoming connections print
`(incoming, pre-established)` for the host: the peer address is resolved only at ESTABLISHED, and an empty
string would read as a lookup failure.) Review also caught: widened `uint64_t` arithmetic in the staging
check, and the correction that mark safety follows from the SQ clamp alone, not from any CQ-size equality.

### 50.3 Validation — PASS, and the §50.2 hazard was LIVE ON THIS HARDWARE, not theoretical

Full battery (2026-07-15): all seven targets built; farnet0 sync verified 0-diff by md5; both DPUs rebuilt
with the exit-1-on-miss landed-proof (`admitted_send_wr=` present in all three deployed binaries); clean
baseline; gate `-t 5 --debug` smoke with ALL FOUR proofs; 3 warmed `-c 1 -t 2000` repeats =
**290.97 / 297.16 / 298.98 tps** vs the 291–304 band (run 1 sits 0.03 below the floor, runs 2–3 in-band — no
directional shift; behavior-neutral as designed); 4-role basebackup **21.66 GiB** (`delivered_bytes=
23253269858`), clean CLOSE_ACK, sender exit 0 in 15.97 s. Alarm battery, `RETIREMENT EVENT LOST`, and the new
`send-CQ drain refused` grep: **all EMPTY on both DPUs**.

**THE HEADLINE — the new `peer QP caps` line, identical on every connection, both DPUs:**
```
requested_send_wr=256 granted_send_wr=409 admitted_send_wr=256 granted_send_cq_entries=511
```
The provider grants **409 send WRs and 511 CQ entries against the 256 request** — so pre-clamp, admission
would have allowed up to 409 outstanding WRs against a 256-entry mark table. §50.2 was a LIVE hazard on this
hardware for any all-signalled workload exceeding 256 outstanding; the clamp is load-bearing, not defensive.

Caveats recorded honestly: the run was on a REPAIRED machine (ownership drift + a broken md5-verification
mode found and fixed — see `farnet_operational_hazards.md` §10); the compiler warning at
`remote_execution_peer_transport_rdma.c:4113` was root-caused during Stage 1 (NOT aarch64-only — any full
recompile emits it): a string literal mangled by `1d379b333` inside the inactive `HOMER_PEER_RDMA_DIAG`
block, i.e. **the DIAG build itself could not compile**; fixed in the Stage-1 commit (§51). Multi-client
gate and the two broken-at-HEAD host workloads were not exercised.

## §51 — Stage 1 terminal flush: the CLOSE_SINK|ABORT_RESET abort path (coalescing plan D-S1). IMPLEMENTED 2026-07-15.

Plan of record: `send_cqe_coalescing_implementation_plan.md` D-S1 + its deviations block (which is the
authoritative decision record; this section carries the audit-side facts and the validation evidence).

### 51.1 What landed (one line per leg)

- **ABI v34**: `CitusRemoteExecCloseSessionRequest` gains `closeFlags` (`ABORT_RESET`); BOTH DPUs rebuild.
- **Producer (bbsink)**: cleanup-close defaults to ABORT unless `end_backup` completed (kills the live
  silent-truncation: a caught sender ERROR used to drain-close and the consumer reported SUCCESS);
  reserve loop gains a registered non-throwing interrupt callback (`INTERRUPTS_PENDING_CONDITION`
  wrapper) — a back-pressured sender is now cancellable at all.
- **Sender service**: `TupleSinkServiceBeginPayloadAbortReset` (pump gate FIRST, then abort mode) +
  the close continuation posts `CLOSE_SINK|ABORT_RESET`, skipping source-drain/EOS/remote-head/QUIESCED
  retry — but KEEPING the RNIC-local `finalPayloadSendRetired` gate for POST and reclaim (deviation #1).
- **Receiver service**: accept arm (identity checks shared with graceful; BASE_BACKUP_STREAM family
  only) → staged discard: relay pump stops, waits out the in-flight DOCA chain, publishes the **FAILED**
  credit-line terminal at the stopped frontier (`HomerDpuDmaSubmitByteRingWriteTerminal` now takes the
  terminal entryState); ABORTED_RESET tombstone answers exact-shape repeats.
- **Consumer**: `entryState==FAILED` is terminal drain-INDEPENDENT and a poll FAILURE with a distinct
  truncation error (CLOSED keeps the drain-gated success arm); SIGINT/SIGTERM → abort-close (RECEIVE
  abort = the existing bound-payload protocol reset, which publishes FAILED into the byte-ring flags the
  sender's reserve loop polls).

### 51.2 The scheduler-coverage traps (each was a silent forever-wedge)

The state machine was the easy half; SCHEDULING the abort work was where the wedges hid. Three
predicates assumed drain-contract progress that an abort explicitly abandons:
`HomerServicePayloadCloseActionReady` (receive-drain gate never satisfied under discard),
`HomerServicePayloadMustProgressBeforeClose` (source-drain / landing-drain hold forever with a stuck
consumer), and the relay pump's wake-up (graceful rides the final payload doorbell; an abort can arrive
with the pump COLD — the accept arm latches a synthetic doorbell). Lesson for Stage 2+: **every new
terminal path must be walked through ready-predicate → ready-set → grant → executor, not just through
its own state machine.**

### 51.3 Review findings and dispositions (2 codex rounds)

- Medium→FIXED: family gate (tuple-view relay has no discard; accept would have wedged unscheduled).
- Low→FIXED: tombstone exact-shape (tombstone check precedes the live flag-shape validation).
- High→RECLASSIFIED PRE-EXISTING (verified vs HEAD `f45556838` by round 2, mechanism verified by owner):
  connection reset before FAILED publication strands the selected-DPU consumer; unchanged by this diff
  (there was no FAILED publisher at all before); future fix named in §37/P2-L — publish role-7 FAILED
  from reset-complete. Reset paths never call `MarkPayloadCloseReclaimable`, so the new abort `exit(1)`
  fact-arms are unreachable mid-reset.
- Drive-by: the `HOMER_PEER_RDMA_DIAG` describe string in `remote_execution_peer_transport_rdma.c`
  (mangled by `1d379b333`; the DIAG config could not compile) fixed; regression net:
  `gcc -fsyntax-only -DHOMER_PEER_RDMA_DIAG=1` on the TU (no sticky object).

### 51.4 Validation round 1 (2026-07-15): regression net GREEN; abort probes FAIL → S1-FIX

**Green (run-validation, full battery):** gate 4/4 proofs (`transport: homer-dpu-command`, 5 distinct
abalance values 5/5, spawn begin+COMPLETED launched_pid=1754709, no host service by /proc/exe); graceful
4-role basebackup clean ×2 (23,253,963,714 / 23,254,233,522 bytes ≈ 21.66 GiB, `stream complete` +
`closed (CLOSE_ACK)`, sender exit 0); **alarm battery empty on both DPUs through every probe** —
`refusing to mark` empty means no new abort exit(1) arm ever fired; **both DPU service pids survived all
three aborted runs**; and the Step-6 reuse run (graceful basebackup with NO service restart after three
aborts) passed with no `pool exhausted` — **aborted runs leak neither byte-ring slots nor bindings.**
Gate perf: warmed 290.1/281.4/225.9 tps — monotone decline under the validation session's own multi-hour
load, no baseline row for this SHA; deferred to re-validation (not read as regression).

**FAIL: none of the three probes reached the new abort path.** Mechanisms (owner-root-caused from the
raw evidence; client-side anchors verified in code):

- **Probe A (client SIGINT):** the walsender only notices a dead client at protocol I/O — early SIGINT ⇒
  **`FATAL: connection to client lost`** (postgresql-2026-07-15_195231.log:1868), and FATAL skips
  `PG_FINALLY`, so `bbsink_homer_cleanup` never ran; farnet1 DPU saw only
  `grouped-control read lost host export; marking stale setup teardown`. Late SIGINT ⇒ the backup ran to
  natural completion (5/6 attempts). The designed ERROR→PG_FINALLY entry point was never stimulated.
- **Probe B (consumer Ctrl-C):** consumer side worked exactly as designed (`interrupted
  (delivered=9512724018); abort-close issued`), the RECEIVE abort arm did NOT fire (wrong stream entry —
  the close names the consumer's own sink; the relay stream is a separate peer-OPEN-created entry), but
  HOST_DETACHED produced the equivalent reset (`terminating DPU relay after host consumer detached` →
  `requesting exact reset for transport cutoff` → both sides `payload stream marked ABORTING`). **The
  sender then spun at 98–99% CPU >60 s (second walsender later found at 99.7%, 138 s)** — the selected-DPU
  sender has NO failure signal: `StillOpenForReserve` polls host-service-era flags nothing DPU-side
  writes, and `HomerClientDpuRefreshBaseBackupCredit` never reads the credit line's `entryState`.
- **Probe C (SIGSTOP consumer + client SIGINT):** mis-aimed like A (the walsender never had an interrupt
  pending — SIGINT went to the client); the interrupt hook was never exercised. Also recorded honestly by
  the validator: its ps-polling loop reported the walsender "gone at t+5s" while a later direct-pid check
  found it alive at 99.7% CPU t+138s — the polling loop's own grep shape was unreliable, the direct-pid
  check is the trustworthy observation.

**Decided fixes (S1-FIX in the plan doc, D-S1 section):** F2 `before_shmem_exit` backstop (FATAL-path
abort-close); F3 sender-side FAILED credit-line publication + client `entryState` check (the missing
failure signal — trace the production credit-line writer FIRST: the only visible `BYTE_RING_CREDIT_*`
submitter is smoke-named); F4 delete the dead RECEIVE arm (HOST_DETACHED already carries the semantics);
corrected probe stimuli (walsender-targeted cancels + a client-kill probe for F2).

### 51.5 Validation round 2 (2026-07-15): gate PASS in-band; basebackup KILLED by the F2 registration

Gate: **292.31 / 294.42 / 297.98 tps** — inside the 291–304 band; round 1's 225.9 outlier was machine
load, not regression. But EVERY basebackup died at end-of-backup with
`ERROR: before_shmem_exit callback ... is not the latest entry`: F2's backstop registered in
`bbsink_homer_begin_backup`, which runs INSIDE `PG_ENSURE_ERROR_CLEANUP(do_pg_abort_backup)`
(`basebackup.c:280→318→403`), and `PG_END_ENSURE`'s `cancel_before_shmem_exit` demands ITS callback be
newest. **Fixed: registration moved to `bbsink_homer_new`** (sink construction precedes
`perform_base_backup`, `basebackup.c:1067`); begin_backup only ARMS. Codex round 5 verified all
constructor paths + the FATAL ordering (`do_pg_abort_backup` first — touches only shared backup state —
then the backstop) + repeated-backup stacking. Silver lining recorded: the crash fired the FULL
sender-abort chain end-to-end on every run (ABORT_RESET armed/accepted/cleared, FAILED credit
armed/published, consumer TRUNCATED nonzero, `refusing to mark` empty, services alive).

### 51.6 Validation round 3 (2026-07-15): **PASS — Stage 1 acceptance met**

- **Graceful basebackup ×2** (same instance, tags 100/101): exit 0, no `before_shmem_exit` anywhere,
  ~21.66 GiB, `stream complete` + CLOSE_ACK, battery empty. **Round-2 blocker resolved.**
- **Probe A (pg_cancel the walsender)**: `ERROR: canceling statement due to user request` (not FATAL);
  `local ABORT_RESET close armed` → `accepted peer ABORT_RESET close` → `peer ABORT_RESET close cleared
  sink`; consumer `terminated FAILED by sender abort ... (stream is TRUNCATED)`, no `stream complete`.
- **Probe A2 (SIGKILL the client — the F2 proof)**: walsender FATAL (`connection to client lost`) **and
  the backstop line** `Homer base backup target: process exit with the stream still open; issuing
  abort-close`; full ABORT_RESET chain; consumer TRUNCATED nonzero. (Timing quirk confirmed as predicted:
  the walsender streamed nearly the whole backup before touching the dead socket.)
- **Probe B (SIGINT the consumer — the F3 proof)**: consumer `interrupted (delivered=4341432050);
  abort-close issued`; node A HOST_DETACHED → relay terminate → reset; **node B `armed FAILED credit
  terminal ... reason=peer-reset-abort` → `published FAILED credit terminal ... (host producer will
  observe entryState=FAILED)`**; sender exited 1 in <60 s with
  `DETAIL: basebackup stream FAILED by the DPU service (peer abort or transport reset): credit_epoch=5804
  ...` — **no 99%-CPU survivor** (round 1's failure mode is dead).
- **Probe C (SIGSTOP consumer + cancel walsender)**: PARTIAL. Sender exited **~1 s after the cancel,
  after ~10 s of backpressure** — and the reserve spin has NO `CHECK_FOR_INTERRUPTS` and no other live
  exit (terminal flags all clear; FAILED credit publishes only post-abort), so the bounded exit is
  mechanistic evidence the interrupt hook fired (INFERRED). **The hook's distinct detail
  ("interrupted while waiting for ring space") did NOT appear** — the log shows the generic
  `canceling statement due to user request` instead (plus the ABORT_RESET chain, clean battery, clean
  reap). OPEN observation item: why the hook's ereport text loses to the cancel ERROR (suspect: the
  pending cancel re-throws during error recovery and supersedes the sink's message). Not an acceptance
  gap — the acceptance criterion is the bounded exit.
- **Post-abort reuse (no service restart)**: graceful ~21.66 GiB PASS, no `pool exhausted` — three
  aborted runs leaked nothing.
- `refusing to mark` empty everywhere across all rounds: **no abort reclaim fact-arm ever fired.** Both
  DPU services survived every probe in every round.

**Stage-1 acceptance (D-S1): (i) consumer Ctrl-C → sender bounded error: PROVEN (probe B). (ii) sender
cancel → consumer bounded ERROR: PROVEN (probe A). (iii) truncation probe: PROVEN three ways (A, A2, B —
an aborted sender NEVER yielded consumer success).** Caveats: probe-C detail string unobserved (above);
zero-frontier FAILED path code-reviewed-only; validator round-3 deviations (self-inflicted postmaster
restarts during probe-timing calibration; `pg_stat_progress_basebackup` never populates for the Homer
sink — unexplained, diagnostic-only).

## §52 — send-CQE coalescing Stage 2a: QP-wide owner-frontier sweep with signalling still always-on. LANDED + VALIDATED 2026-07-16 (citus `bdcb7eb70`).

Plan of record and full implementation map:
`send_cqe_coalescing_implementation_plan.md` D-S2.0 through D-S2a. Stage 2a replaces the class-local ordinal /
array-FIFO retirement of command-write and peer-client-completion owners with per-connection intrusive post-order
lanes stamped by the QP-wide `postedSendWrs` frontier. Every successful accounted send CQE advances the scalar
retirement frontier, sweeps the transport-owned control FIFO, then sweeps both service owner classes; the typed
command/completion/control CQE arms validate that their own named owner is already gone. The bootstrap special poll
now validates and retires its accounted CQE, eliminating the permanent one-checkpoint mark skew. Signalling policy is
unchanged in this stage: command, completion, and control sends remain always-signalled.

### 52.1 Post-implementation review correction — edge recovery was more dangerous than fail-stop

The first implementation attempted to recover a posted-but-unlinked service owner from its typed CQE. Adversarial
review refuted that recovery: Scenario-E treated every unlinked entry as unposted and could reuse the index before
the old CQE arrived; retaining the orphan was still incomplete because the send-CQ demand executor enumerates sweep
lanes, so an unlinked owner did not identify a QP to drain. A later old CQE could therefore inspect a same-connection
replacement owner and force-sweep through a mark it did not prove.

Owner decision: this is a research prototype and these insertion failures are structurally impossible (each lane
table is owner-table-sized; control FIFO depth is exactly op slots + response slots). Do not build a second orphan
scheduler. Service-lane allocation failure, control FIFO overflow, and failure to decode the locally encoded CONTROL
WR-ID now **ALARM + `exit(1)` immediately after post success**. Process exit destroys the QP and is the failure fence.
The unsafe orphan/drop fallback code and promises were removed. A fresh adversarial pass verified the corrected
normal and fail-stop paths and found no remaining ownership defect.

### 52.2 Validation — PASS on the corrected final binary

Full rebuild/install, farnet0 checksum sync, both-DPU rebuild/deploy/landed proof, hard clean baseline, and fresh
service-identity proof all passed. Acceptance evidence:

- debug gate proved `homer-dpu-command`, five distinct decoded results, 5/5 processed, and DPU backend spawn
  `COMPLETED`;
- warmed gate `-c 1 -j 1 -t 2000`: **303.395 / 298.298 / 295.362 TPS**, each 2000/2000 with zero failures — no
  directional regression versus the accepted current/Stage-1 neighborhood;
- measured 4-role basebackup: sender/consumer exit 0, **23,254,330,999 bytes (21.6573 GiB)** in 7.96 s, clean
  `stream complete` and `CLOSE_ACK`; the 512-KiB ring traversed **44,354 full capacities plus 61,047 bytes**;
- both DPUs: global alarms = 0; Stage-2a sweep/skew/orphan/self-heal/purge/Scenario-E diagnostics = 0; supervised
  service exit status 0; peer-control `live=0`, abandonment 0/0; head-mirror `live=0`; DMA drain 0 tasks,
  `free_slots=3072/3072`, `fatal=false`.

**Evidence boundary:** no teardown log prints final per-connection `postedSendWrs` and `retiredSendWrs`, so their
exact numerical equality remains directly unobserved. The acceptance substitute is clean semantic closure, no
named-CQE/sweep recovery diagnostics, zero live retirement ledgers, zero DMA tasks, and clean service exits. Node A
also retained the pre-existing clean gate result-relay teardown shape (host-consumer detach → reason-4 reset → clean
orphan reclaim, `response_slots=1`, all control-op state counts zero); it carried no Stage-2a alarm or recovery and is
not evidence that the Stage-2a fallback ran.

## §53 — send-CQE coalescing Stage 2b plan settlement: stateless unit preflight + per-QP owner shards + proof-backed deferred reset. PLANNED 2026-07-16.

The post-Stage-2a readiness review found that the older D-S2.1/D-S2.2 prose had accumulated mutually exclusive
designs. Owner clarification settled the actual Stage-2b boundary; the canonical implementation map is now
`send_cqe_coalescing_implementation_plan.md` D-S2.1, D-S2.2, D-S2.4, and D-S2b.

Decisions:

- **Unit reservation is stateless software preflight, not durable credit.** Compute the exact 1-or-2-WR unit once,
  admit the total before CQ-owner reservation, then post immediately without an intervening poll/yield. Existing
  per-post admission remains a backstop. There is no reserved-but-not-posted frontier or rollback state. A body
  accepted followed by tail failure is an exceptional non-rollbackable path and fail-stops in this research
  prototype.
- **Keep 256/256 owner capacity, but make it a dynamically allocated per-QP shard.** The earlier 4096/1024 global
  session×pool size-out and the intermediate "reject a second owner-bearing QP" restriction are both superseded.
  Each ready local `CRITICAL_CONTROL` QP generation gets one service-owned shard containing 256 command owners,
  256 peer-client-completion owners, and one intrusive lane/cursor/counter set per class. Every linked owner in a
  shard covers at least one software-unretired WQE on that same QP, bounded by its clamped 256-WQE admission
  ceiling; preflight before owner reservation guarantees room for the one transient unlinked owner. Thus multiple
  nodes and simultaneous directions allocate independent capacity domains without a session×peer cross-product,
  global quota, or WR-ID expansion.
- **Allocate/free only on the cold physical-connection lifecycle.** A new fallible ready observer runs inside
  `TupleSinkServiceFinishPeerConnectionSetup` after bootstrap but before READY/canonical/bootstrap-complete is
  published; it `calloc`s the shard for `CRITICAL_CONTROL` and returns an opaque connection context. Failure leaves
  the QP non-ready and enters setup reset before owner-bearing posts. Reset-begin does not free. Reset-complete runs
  after QP/CQ destruction; transport first detaches the opaque pointer so no accessor can return freed storage, then
  service exact-purges `active || linked` owners, rearms pending session resets, unlinks/verifies/frees the shard,
  and only afterwards runs reset-complete helpers that may call `TupleSinkServiceResetSession`. The existing
  transport-destroy loop resets every active incoming/outgoing connection through this same callback. Multiple
  sessions sharing one endpoint/class QP share its shard; inactive connection slots carry no heavy allocation.
- **Ready allocation is an atomic ownership transfer.** Transport calls ready once with local context NULL and
  stores it only after success. Callback failure frees/undoes every partial allocation/list insertion and leaves
  NULL; `CRITICAL_CONTROL` success requires one fully initialized/listed non-NULL shard. Duplicate ready or a
  pre-existing context is an invariant failure. Every ready `CRITICAL_CONTROL` QP is owner-capable, so a QP that
  ends up carrying generic control only may receive an unused cold shard by design; no current typed-owner path is
  missed.
- **Dynamic lookup still needs fair drain enumeration.** The QP's opaque context gives O(1), generation-checked
  reserve/sweep lookup, but Stage 2a's scheduled CQ drain currently discovers send CQs by scanning the global lane
  arrays. Keep an intrusive active-shard list, cold-mutated at ready/reset, and walk it from a round-robin cursor under
  the 64-grant bound so shard 65+ cannot starve. Preserve global owner/demand counters as aggregate scheduler facts;
  shard-local counters select exact QPs and prove capacity. Neither is a quota.
- **Half-SQ is a bound, not liveness.** Continuing traffic and source pressure force checkpoints; Stage-1 terminal
  close/abort flushes normal tails. An abnormal fully quiet abandonment may retain a bounded unsignalled tail.
  Stage 2b therefore removes live-QP Scenario-E release: owners/MRs stay live and reset defers until a covering CQE
  or QP destruction proves retirement.
- **Deferred reset gets an indexed continuation.** New session reset-owned/reset-runnable bitmaps and a per-session
  latch are rearmed after that session's owner count reaches zero, or by exact QP-death purge/reset-complete. The
  top-level scheduler retries reset; send-CQ
  callbacks remain retire-only. Node A arms `forceNextSignalCheckpoint`; Node B trusts the close WIMM only when its
  successful-post latch is set. Exact lane purge occurs after QP/CQ destruction, not from generation mismatch too
  early in reset-complete.
- **Generation and shutdown close the continuation end to end.** Incoming client-SQL binding already snapshots the
  connection generation; outgoing command binding currently stores only the handle and must snapshot generation too
  so shard lookup cannot bless a reused slot. Shutdown currently resets sessions, unmaps control, then destroys the
  transport; Stage 2b reorders this to initial reset sweep -> transport destroy/QP-death purge -> final runnable-reset
  sweep -> zero-live assertions -> control unmap. Otherwise transport destruction rearms a deferred reset after the
  last scheduler opportunity and shutdown silently leaves the new continuation incomplete.

Stage 2b remains behavior-neutral for signalling; Stage 2c later flips only the decision function. Acceptance keeps
the gate + 4-role basebackup unchanged and adds direct send-frontier, owner-table topology/peak, and deferred-reset
closure evidence. Exceptional defer may be validated with a bounded diagnostic injection or labelled honestly as
code-reviewed-only.

**Final adversarial verdict after the multi-peer revision: READY TO IMPLEMENT.** The review independently verified
the per-shard 256-WQE capacity theorem (including the transient unlinked owner), shard-local WR-ID interpretation,
ready/reset ownership transfer, active-shard drain enumeration, outgoing generation capture, and shutdown
continuation. No high/medium design defect remains. Implementation watchpoints are exact per-shard+aggregate counter
balance at every owner transition, round-robin cursor repair on shard unlink, and a loud invariant failure if any
future command/completion owner appears on a non-`CRITICAL_CONTROL` QP. True simultaneous multi-peer execution is
not covered by today's gate/basebackup and must remain labelled code-reviewed-only unless a dedicated exerciser is
available.

## §54 — send-CQE coalescing Stage 2b implemented and validated after one compile fix and one runtime correction. 2026-07-16.

Stage 2b is now implemented in the three planned Citus/Homer files with signalling still unconditionally on. The
source replaces global command/completion owner tables with cold-allocated per-`CRITICAL_CONTROL`-QP shards, adds
non-mutating total-unit preflight before owner reservation, installs atomic ready/reset lifecycle context transfer,
uses fair active-shard CQ enumeration, captures outgoing generation, and replaces live-QP Scenario-E release with
an indexed proof-backed reset continuation. Shutdown now destroys transport before its final reset sweep.

The independent diff review found three material gaps before build, all corrected in the working tree:

- `lifecycleReadyNotified` distinguishes a legitimate pre-ready/setup-allocation reset with NULL context from a
  ready owner-capable QP that lost its shard;
- `completionTransportCreditBlockedSessions` makes local send-credit pressure a real `WAIT_CQ` dependency and the
  exact QP's next successful CQE clears/rearms only matching generation-bound sessions; exact QP death instead
  cancels the staged publication before reset classification so no dead-generation retry can wedge;
- `sendAdmissionRefusalSerial` distinguishes a genuine zero-WR provider failure from the impossible case where a
  per-post admission backstop refuses after successful unit preflight; the latter ALARMs and fail-stops.
- impossible per-QP owner-shard exhaustion and both body-accepted/tail-failed arms fail-stop with explicit ALARMs.

The repository formatter and `git diff --check` pass. The full all-target build, Citus/Postgres install/relink,
farnet0 parity, and both-DPU rebuild/deploy subsequently passed. The debug gate completed 5/5 through the intended
DPU path, but acceptance stopped before warmed repeats/basebackup because farnet1 emitted 24 owner-shard ALARMs.
The first ALARM immediately followed establishment of outgoing generation 2 as `traffic_class=2` and reported a
NULL lifecycle context while demanding `expected_class=1`; teardown still proved generation 2
`posted=12 retired=12` and the final Stage-2b ledger was all zero.

The defect was a missing boundary, not a missing shard: `sendCheckpointSweep` is transport-invoked for every
successful CQE on every traffic-class QP, but service command/completion shards exist only on
`CRITICAL_CONTROL` QPs. `HomerServiceHandleSendCheckpointSweepFromDrain` therefore tried to resolve service lanes
on a payload QP whose NULL lifecycle context was correct. The callback now returns immediately for
non-`CRITICAL_CONTROL` QPs after the transport scalar/control frontier has advanced. This retains the Stage-2a rule
that every WR class on an owner-bearing QP can retire service owners.

**Corrected re-validation: PASS.** The clean all-target build, Citus install, PostgreSQL relink/install, farnet0
parity, and forced both-DPU rebuild/deploy all passed. The intended-path debug gate completed 5/5 and the four prior
false shard-lookup ALARM classes remained zero on both DPUs. The stripped warmup was 297.990 TPS; complete warmed
repeats were **300.149 / 298.033 / 294.586 TPS**, each 2000/2000 with no failures and no directional Stage-2a
regression. Four-role basebackup delivered **23,254,799,975 bytes (21.657720 GiB)** in **7.06 s**, with both roles
exit 0, `stream complete`, and `CLOSE_ACK`; 524,288-byte ring geometry proves **44,355 full laps + 5,735 bytes**.

Both DPUs ended with zero global/Stage-2b alarms, zero pending reset/credit bits, zero active shards, zero live
control/head-mirror owners, and `free_slots=3072/3072`, `fatal=false`; services stopped gracefully. Clean QPs had
`postedSendWrs == retiredSendWrs`; clean-detach one-WR differences were explicitly reported as `flushed=1`, not
mislabelled retirement. True simultaneous multi-peer execution and a deterministic injected deferred-reset
transition remain code-reviewed-only because the current acceptance topology does not exercise them; this is the
plan-approved research-prototype boundary, not a hidden runtime claim. **Stage 2b is accepted; next is Stage 2c.**

## §55 — send-CQE coalescing Stage 3 implemented and validated: exact indexed demand replaces the broad collector. 2026-07-17.

**Status: IMPLEMENTED + VALIDATED as Citus `c0b9f06bd`.** The canonical design and acceptance record are
`send_cqe_coalescing_implementation_plan.md` D-S3. Stage 3 completes the coalesced send-CQ retirement work without
leaving a quiet signalled tail: the transport owns one direction/class/index demand bitmap and aggregate count,
arms it on the first unretired signalled frontier, and clears it on common scalar retirement or verified reset.
`TupleSinkServiceSetSendCqDemandIndexed` (`remote_execution_peer_transport_rdma.c:1514`) owns bit/count mutation;
`TupleSinkServiceEnumeratePeerSendCqDemandRdma` (`remote_execution_peer_transport_rdma.c:5520`) emits bounded,
generation-bearing exact actions with a retained fair cursor; and `TupleSinkServicePullPeerSendCqRdma`
(`remote_execution_peer_transport_rdma.c:5628`) is the one reason-tagged bounded exact-QP pull. The actual verbs
dequeue is capped by the caller's remaining `maxCqes`, so a poll-one caller cannot dequeue and discard a suffix.

The semantic post tails invoke `POST_CHECKPOINT` only after each retirement owner is fully committed. Local
owner/admission pressure invokes `PRESSURE`, then performs one rescan/retry; remote mailbox consumer credit remains
on recv progress and is not mislabeled as send-CQ pressure. The scheduler's surviving historical
`COMMAND_SEND_CQ` name now means the typed exact-QP drain across every owner and traffic class. The broad
`PEER_SEND_CQ` collector/action and broad peer-pump reachability are deleted. Coarse readiness is phase-specific:
only COLLECTOR mutates/consumes the machine cooldown, SEMANTIC reads pending state without consuming it, and the
non-machine fallback owns a separate once-per-pass cooldown. This correction closes the static-review starvation
counterexample in which the due sample could always land in a phase incapable of granting the collector.

Control sends now classify signalled FIFO owners through per-connection `controlSignalledSendOwnerCount`
(`remote_execution_peer_transport_rdma.c:1177`, insertion/sweep at `:4701-4765`), while QP-wide demand remains the
scheduler truth. FB-2 is de-aliased into exactly two source/feedback slots; the runtime bijection validator
`HomerServiceValidatePeerSendCqFeedbackSlotMap` (`tuple_sink_service_process.c:4299`) proves injective/full coverage
at registry initialization. Diagnostic counters attribute writer ownership and age per slot. Normal close/abort or
continued traffic provides a later checkpoint; accepted abnormal sub-threshold abandonment may retain a bounded
unsignalled tail until another checkpoint or verified QP destruction. Arbitrary corruption and extraordinary
post-success abandonment remain outside this research-prototype proof boundary.

**Runtime acceptance: PASS.** The diagnostic selected-DPU gate completed 4,000/4,000. POST_CHECKPOINT cost was
5,248 polls / 63 CQEs at 1,471.6 ns average and 65.281 us maximum on the direct DPU, and 454 / 32 at 1,991.5 ns
average and 8.970 us maximum on the farnet0 DPU. All 5,124 direct and 422 farnet0 empty post-clock records were
later covered by productive scheduled pulls. The maximum-supported-client shape did not enter `PRESSURE`, so that
arm remains explicitly unexercised rather than inferred green. FB-2 reported completion-egress slot 1 with 26,806
grants and command-egress slot 0 with 23,180 grants, both maximum own-age zero and no owner mismatch. Final indexed
demand and control-signalled-owner counts were zero.

After a discarded warmup, five stripped 2,000-transaction repeats completed without failure at 299.207 / 300.422 /
297.390 / 299.907 / 298.754 TPS (mean 299.136); these are candidate measurements, not a same-lineage regression
verdict. A separately bracketed debug gate passed 5/5. Four-role basebackup delivered 23,255,654,972 bytes in
15.52 seconds with both roles exit zero and `CLOSE_ACK`, proving 44,356 full 524,288-byte laps plus 336,444 bytes.
The following 1/1 gate proved listener reuse. Both DPU alarm batteries were empty, teardown ledgers balanced, every
send frontier satisfied `posted = retired + flushed`, and final process/listener/shared-memory scans were clean.
Evidence is under `/tmp/farnet-validation-stage3-fresh-20260717T124510Z/evidence/`. DPU TCP smoke was not required:
Stage 3 changed no DPU DMA, byte-ring geometry, or bridge ABI.
