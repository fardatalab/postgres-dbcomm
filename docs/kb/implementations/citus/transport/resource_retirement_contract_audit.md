# The resource-retirement contract: audit and remediation plan

**Status:** OPEN. **Audit COMPLETE** (RDMA + DOCA DMA). Remediation plan in section 4; P0 items block P7b.1.
**Opened:** July 12, 2026. **Owner-driven** (the contract below is an owner statement, not a derived rule).
**Code baseline:** citus `d33f6ded3`, postgres `0021633a44a`.

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
