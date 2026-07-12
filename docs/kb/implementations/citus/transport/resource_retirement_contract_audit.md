# The resource-retirement contract: audit and remediation plan

**Status:** OPEN. Audit complete for DOCA DMA; RDMA sweep in progress.
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

## 3. AUDIT — RDMA

*(sweep in progress; this section will be completed from it)*

Known before the sweep:

| resource | verdict | evidence |
|---|---|---|
| payload DATA send | **HOLDS** | `CLOSE_SINK` gated on `payloadSendOwnerCount == 0 && payloadSendOwnerOutstandingWrs == 0` (`tuple_sink_service_process.c:27070`) |
| receiver-head ACK | **HOLDS** | reclaim refuses while `receiverHeadAckOutstandingCount > 0` (`:35605`) |
| sender-head mirror MR | **HOLDS** | deliberately left registered; deregistering would `REM_ACCESS_ERR` and poison the retained QP (`:25063`) |
| **control REQUEST op** | **VIOLATED** | released at response-match while its send CQE may be unpolled; the memset also wipes `requestMessage`, the **registered RDMA source buffer** (`remote_execution_peer_transport_rdma.c:10990`). Safe today only by an **unwritten RC-ordering argument**. |
| **its "guard"** | **DEAD CODE** | `:3699` — **both branches `return true`**. It guards a mutation that does not exist, and its comment blames a blocking-adapter timeout path that **has no caller**. It read as a considered design for months. |
| control RESPONSE slot | **BY-ACCIDENT** | freed only by its own CQE (`:3695`) — but only because the allocator happens to skip `inUse` slots. Nothing enforces it; the generation is decoded and **discarded**. |

Open question for the sweep: **every `ibv_dereg_mr` call site.** Deregistering an MR while a local WR still
reads it, or while a remote peer can still write into it, is corruption or a poisoned QP — the sharpest shape
this contract prevents.

---

## 4. REMEDIATION PLAN

Ordered. **P0 lands before P7b.1's reuse**, because reuse is what makes these reachable.

| # | item | why it is where it is |
|---|---|---|
| **P7b.0** | **Control op: release on PHYSICAL retirement.** Add `sendCompletionRetired`; release on whichever of {response consumed, send CQE retired} comes **LAST** (the CQE usually arrives FIRST, so it cannot simply move to the CQE handler). The dead guard at `:3699` becomes the real retirement site **plus a LOUD generation assertion**. Delete its decoy comment. Closes the RESPONSE-slot hazard **by construction**, by the same rule. | Smallest, fully understood, and it is the exact resource P7b will start reusing. |
| **P7b.0b** | **V2 — forced arena unbind must not reset with tasks in flight.** Either drain, or refuse and retry, or (if a hard deadline must win) *fence the ring* so a late completion cannot write back. Today it warns and proceeds. | Reachable, and it corrupts *tenancy* state — the thing P7b multiplies. |
| **P7b.0c** | **V1 — dead-owner arena reaper needs a DPU handshake.** A host-side reaper may not free a slot the DPU may still be DMA-ing into. Needs the DPU to confirm `inFlightTaskCount == 0` for that slot's rings (the count already exists — it is simply never asked). | **Worst blast radius: cross-session host-memory corruption.** But needs a protocol addition, so it is not a one-liner. |
| **P7b.1** | clean host-export detach RELEASES the connection instead of resetting it | the original P7b goal; now safe to land on top of the above |
| **P7b.2 / .3** | pooled connections at startup; create-on-demand fallback | the owner's directive; sized against **concurrency**, not latency (P7 made a connect ~10 ms) |
| **P2** | **V3 — `HomerDpuDmaDestroy` drain on clean exit.** | Clean-shutdown-only; does not affect steady state. Lowest urgency, but it is a known hole that says so in its own comment. |
| **P2** | **A1 — grouped-control reads outliving tenancy.** Leave as-is, but **write the invariant down** ("safe only while the staging buffer is private and never pooled") and add a tripwire if it is ever pooled. | Correct today; the danger is a future change, so a stated invariant + tripwire is the proportionate response. |

---

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
