# Send-CQE coalescing and pool depth

**Status: DESIGN. Not implemented.** Prerequisite: §29(b) send-queue admission accounting in
`../../../implementations/citus/transport/resource_retirement_contract_audit.md`.

> ## ⚠⚠ THE IMPLEMENTATION PLAN — with 2026-07-15 corrections — SUPERSEDES PARTS OF THIS DOC
> **[`../../../implementations/citus/transport/send_cqe_coalescing_implementation_plan.md`](../../../implementations/citus/transport/send_cqe_coalescing_implementation_plan.md)** is the plan of record. A site
> enumeration found several assumptions below went stale. **Most important: ⛔ C5 IS DEAD.** Its depth-1
> "~450µs/command blocking fence" target (`TupleSinkServicePublishPeerCommandCompletion`) is dead code pending S7
> deletion, and every LIVE pool is already deep + non-blocking — **there is no live shallow pool to deepen, and
> the 235.4µs latency is NOT this fence** (its cause is unattributed, tracked separately). Also: C4's close
> checkpoint is `CLIENT_SQL_SESSION_CLOSE` / `CLOSE_SINK`, **not** `CLOSE_SESSION` (local control); C4's abort
> notification is unwired scaffolding (`OBJECT_ERROR` / `ABORT_RESET`), not absent; C2's four classes are **not**
> uniform (only two have FIFOs). Read the implementation plan's CORRECTIONS section before trusting C1–C5 below.

**Why this doc exists:** this began as §32 of the resource-retirement audit and outgrew it. That audit is about
**correctness** (a resource may be released only when its outstanding physical operations have retired). This is a
**performance** generalization that *depends* on that contract but is not part of it. Keeping them together was
making the audit unreadable.

**Related**
- `../../../implementations/citus/transport/resource_retirement_contract_audit.md` — §0 the contract, §29 the
  send-queue admission accounting this design requires.
- `../../../implementations/citus/transport/dpu_command_plane_migration_plan.md` — the migration this sits inside.


## The design

**Not implemented. §29(b) is its prerequisite. Recorded now because the reasoning is the valuable part and it
corrected me twice.**

### 1 The one fact everything follows from

> **A send CQE carries exactly ONE bit: "this WR — and every WR posted before it — is done."**
> Everything anyone waits on it for is **RESOURCE RETIREMENT**. There is no second semantic.

Verified against the two places that *looked* like exceptions:

- **Bootstrap** blocks on its CQE — but its own comment says that is *"in order to keep the mailbox-descriptor
  exchange simple and easy to debug"* (`remote_execution_peer_transport_rdma.c:5917`). **Convenience, not
  semantics.**
- **Peer command-completion** blocks on its CQE — to fence *"reuse of the **single** scratch/tail source"*. That
  **is** retirement — of a pool whose depth is **ONE**.

### 2 THE RULE

```
  interval  <=  1/2  x  (depth of the pool that substrate's CQE drains)
```

⚠ **THE "EXCEPTIONS" WERE NOT EXCEPTIONS — THEY WERE DEPTH-1 POOLS.** Depth 1 ⇒ interval 0 ⇒ *must signal every
WR*. That is not a carve-out; **that is the rule, evaluated at depth 1.**

Which inverts the conclusion: the peer command-completion **blocking wait is not a constraint on coalescing — it
is a BUG.** It blocks because someone gave that path **one** scratch buffer. **Deepen the pool ⇒ the frontier
retires a range ⇒ the blocking wait disappears.** And that blocking wait is the prime suspect for the
**~450 µs/command**. *The obstacle to coalescing turns out to be the biggest performance item on the list, and
coalescing is what removes it.*

⚠ **A SINGLE GLOBAL INTERVAL IS WRONG.** Each substrate's CQE drains a *different* pool with a *different* depth,
so each needs its own interval. (And if one CQE drains several pools, the bound is `min(depth)/2`.) The deleted
`RING_SLOTS / 2` (8 of a 16-slot completion-publish ring) was **not an arbitrary constant** — it was exactly this
rule: *never let coalescing hold more than half the pool it drains hostage.*

### 3 The mechanism is SCALARS, not a FIFO

Per QP, two monotonic scalars:

```
  outstanding = postedSendWrs - retiredSendWrs
  postedSendWrs  += chainWrs           on EVERY post (signalled or not)
  retiredSendWrs  = max(retired, mark) on each signalled CQE
```

A signalled WR stamps the cumulative `postedSendWrs` onto **its own outstanding-WR record** — which already
exists, and which its WR ID already points at (that is what §24's `OWNER_SLOT` / `INDEX` fields *are*). `max()`
makes it monotone and idempotent.

Per substrate, retirement is **"free everything at or below frontier F"** — a **RANGE on a ring**, not a queue
walk.

> ⚠ **I PROPOSED A FIFO AND THE OWNER WAS RIGHT TO REFUSE IT.** I had conflated two retirements that ride the
> same CQE: **semantic owner retirement** (release each owner's *distinct* resource — that genuinely walks a
> prefix, which is why the existing per-lane FIFO with `postOrdinal` exists) and **send-queue accounting**
> (*"how many WQEs are outstanding?"* — a **count**). Counts do not need a queue; they need subtraction.
> The frontier mark is a NUMBER CARRIED BY THE CHECKPOINT, and the checkpoint already has a record to carry it.

> ⚠ **AND ON LATENCY, ALSO REFUTED:** I argued control was too latency-sensitive to coalesce.
> **Retirement latency is not operation latency.** A control op takes the *next free* slot from a 64-slot ring;
> when the *old* slot returns to the pool is irrelevant to it. Control coalesces fine.

### 4 The machinery already exists, dormant

`tuple_sink_service_process.c:6568`: *"A send CQE on an RC QP retires all earlier WRs posted to the same QP. The
per-lane FIFO gives the exact source-owner prefix covered by this checkpoint."* Per-lane FIFOs with post-ordinals
and prefix walks exist for the **command** and **peer-client-completion** lanes. They are **never exercised beyond
one entry**, because every post currently signals. **Re-arming the interval feeds code that is already written.**

### 5 THE TERMINAL FLUSH — the thing P0-i could not build, and why it can now

P0-i **removed** the interval (both `*_SIGNAL_INTERVAL` macros are now `#error` tripwires,
`tuple_sink_service_process.c:417` / `:442`) because coalescing leaves an **unsignalled tail at session close —
and on ABORT paths, which have no terminal message to force-signal.** Those WRs' resources are never retired.

**Coalescing is sound while traffic continues** (a later signalled WR always arrives). **It breaks at the END.**
Every argument for it is about the hot path; the bug is entirely in the cold path. *That is why it shipped.*

⚠ **§29(b) IS THE MISSING PIECE.** A terminal flush must ask *"do I have un-retired WRs?"* — and that is exactly
`postedSendWrs - retiredSendWrs`. P0-i chose "only coalesce within a unit of work" because **there was no send-queue
accounting to make the other branch safe.** With accounting, the other branch opens.

⚠ **The hard case is not close — it is ABORT ON A SHARED CONNECTION.** On a full reset the QP is destroyed and every
WR is flushed, so resources are released via the failure path. The leak bites when a **single session** closes or
aborts while the **connection survives** (other sessions still on that QP). The flush must therefore be a
**transport primitive**, not a protocol message. *(Open: a signalled zero-length RDMA_WRITE is the cheap way to
manufacture a checkpoint; being verified.)*

### 6 Order of work

1. **§29(b)** — scalar frontier + admission. Needed regardless; produces `outstanding`.
2. **Terminal flush** — close AND abort. Warrants its own design.
3. **Re-arm the interval per substrate**, each `<= 1/2` its own pool, reusing the dormant retirement code.
   Convert the P0-i tripwires from *"never"* to *"not without the flush."*
4. **Deepen any depth-1 hot-path pool** (peer command-completion scratch) — which is also the ~450 µs/command fix.

⚠ Doing (3) before (2) re-ships P0-i's bug. The tripwire would stop it at compile time — a rather good
advertisement for tripwires.

---

## CORRECTIONS (2026-07-13). Four, all owner-driven. The design got smaller each time.

### C1. The interval is PER-QP, and it is NOT the invariant

**Half the SEND QUEUE, enforced PER-QP by the transport.** Not per-session, not per-pool.

- **Per-SESSION `SQ/2` BREAKS:** two sessions on the shared `CRITICAL_CONTROL` QP each accumulate 128 unsignalled
  WRs → 256 → the queue is full and **no checkpoint can be posted**.
- **Per-QP `SQ/2` WORKS:** at most 128 unsignalled WRs on the wire, 128 WQEs always free. §29(b)'s
  `postedSendWrs` is already per-connection, so the transport can force `signalled = true` whenever
  `postedSendWrs - lastCheckpointFrontier >= SQ/2`, regardless of who posted. **Immune to multi-session by
  construction.**

⚠ **BUT THE INTERVAL ALONE DEADLOCKS.** A session exhausts its **own source pool** (e.g. 16 completion slots) long
before 128 WRs. It stops posting. Nothing forces a checkpoint. Now it needs a CQE to free its slots → a CQE needs
a checkpoint → a checkpoint needs a post → **a post needs a slot it no longer has.** Deadlock, not a leak.

**So you need BOTH, and they do different jobs:**

| | job |
|---|---|
| **per-QP interval (`SQ/2`)** | bounds the **WQEs** — an *optimization* |
| **pressure trigger** — force a checkpoint when any pool this CQE would drain nears full | breaks the **deadlock** — the *invariant* |

`interval <= 1/2 pool` (the earlier framing) is a **headroom heuristic**, not the correctness rule. The **forcing
conditions** are.

**THE ONE DECISION POINT**, evaluated at post time:

```
signalled =  (postedSendWrs - lastCheckpointFrontier >= SQ/2)   /* per-QP WQE pressure  -- TRANSPORT knows this  */
          || (this substrate's source pool free < watermark)     /* per-resource pressure -- SUBSTRATE knows this */
          || (terminal WR of a closing/aborting unit)            /* terminal                                     */
```

That split **is** the API: the transport owns the frontier, the substrate owns its pool.

### C2. ONE QP-WIDE POST-ORDER FRONTIER — not a FIFO per class

⚠ **Only `CRITICAL_CONTROL` is a SHARED QP**; every other traffic class is session-owned. And **FOUR classes
multiplex onto it**: command, peer-client-completion, control request/response, peer command-completion.

A checkpoint CQE proves every earlier WR on that QP is done — **across all four classes** — but today each class
keeps its own **class-local, incomparable ordinals**, so the handler can only retire within the CQE's own class.

**Fix: stamp ONE QP-wide number — `postedSendWrs`, the frontier §29(b) already computes — on every owner of every
class.** Retirement becomes a **comparison**:

> a checkpoint at frontier **F** retires **every owner, of any class, whose mark <= F.**

**What happens to the per-class FIFOs: SIMPLIFY, DO NOT REMOVE.**
- **KEEP** the per-owner records (they hold the resource to release).
- **KEEP** a head index per class (so a checkpoint walks only the covered *prefix*, O(retired), not O(table)).
- **DELETE** the class-local ordinal machinery (`FindClientSqlCommandWriteLaneFifoOffset` and friends).
The "queue" was always just post-order allocation wearing a costume.

### C3. UNIT-LEVEL RESERVATION — reserve the unit's total WRs before the FIRST post

Admission today is **per `ibv_post_send`**. A unit spanning **two posts** (unsignalled body, then signalled tail)
is **not atomically admitted**: if admission takes the body and refuses the tail, the body is on the wire with **no
checkpoint that will ever retire it**, and the lane wedges.

§29(b) currently closes this with a **1-WQE checkpoint reserve** (an unsignalled post may not spend the last WQE).
⚠ **That works today only by arithmetic luck** — every multi-post unit happens to have a **1-WR** tail. Give a unit
a 2-WR tail and it tears again. **A fix that works for the wrong reason.**

**The correct primitive: reserve the unit's TOTAL WRs before the first post**, then let the individual posts draw
down that reservation. Every caller knows its total up front (body + tail = 2; a payload chain is one post). This
makes *"a unit is never torn"* **structural** rather than arithmetic.

### C4. THE TERMINAL FLUSH ALREADY EXISTS IN THE PROTOCOL. No new WR.

Idle detection was proposed and **rejected**: knowing a QP is "idle" needs a timer and races with an incoming post.
**A terminal flush is local, deterministic, and easier to reason about.** Do that.

And it costs **nothing**, because the checkpoint is a message we already send:

| case | the checkpoint | cost |
|---|---|---|
| close known BEFORE the last WR is posted | **signal that WR** | free |
| close known AFTER | **`CLOSE_SESSION` is already a real, typed, signalled control message** — it IS the checkpoint | free |
| **abort** | **an abort notification — WHICH WE ALREADY OWE** | free, and it fixes a bug |
| peer gone / QP broken | connection reset destroys the QP and flushes everything | nothing to do |

> ⚠ **THE TERMINAL FLUSH AND THE MISSING ABORT NOTIFICATION ARE THE SAME WR.** There is a standing robustness gap
> — *"a mid-transfer abort sends NO notification, so both `pg_basebackup` ends hang until killed."* We owe the peer
> that message anyway. Posted **signalled**, it **is** the terminal checkpoint. **No zero-length write, no dummy WR,
> no new WR-ID class — and the abort-hang bug is fixed as a side effect.**

*(This also retires the earlier "zero-length RDMA_WRITE" proposal, which was refuted: no zero-length contract is
established here, the helpers reject empty SGEs, and untagged CQEs are reserved for blocking waiters.)*

### C5. DEEPEN THE SHALLOW POOLS — this is where the performance actually is

⚠ **A depth-1 pool on a hot path is a BUG, not a constraint.** The peer command-completion publish blocks on its
CQE because it has **ONE** scratch buffer. Deepen it to a ring ⇒ the frontier retires a range ⇒ **the blocking wait
disappears** — and that wait is the prime suspect for the **~450 µs/command**.

**The obstacle to coalescing turns out to be the biggest performance item on the list, and coalescing is what
removes it.** Every substrate whose CQE drains a shallow pool wants the same treatment. Scope: every substrate
except bootstrap/utility (cold path, twice per connection, genuinely synchronous).
