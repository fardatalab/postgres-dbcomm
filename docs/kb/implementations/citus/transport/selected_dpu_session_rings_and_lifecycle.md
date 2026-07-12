# Selected-DPU sessions: identities, rings, exports, and lifecycle

## Scope

- **What this doc explains**: the CURRENT architecture of record for the selected-DPU path — what a
  session *is*, the four distinct identities and which one answers which question, the ring roles, how
  rings reach the DPU (exports/imports), and the full open→command→close→teardown lifecycle across both
  nodes.
- **Why it exists**: the July-3 `homer_current_implementation_checkpoint.md` predates the command-plane
  migration (S1–S4), P1–P5, and P6. Its code pointers and its model of the session are both out of date.
  Treat that doc as milestone history, not as current truth.
- **Doc type**: `implementation`
- **Companion plan/history**: `dpu_crossnode_command_completion_bridges_design.md` (the running design +
  execution record; §17–§21 are the most recent).

---

## 1. THE CONTRACT

> **Homer's user has DB semantics. It knows, AT OPEN, what rings a session of a given kind needs.
> So a session BINDS EVERY RING IT NEEDS, IN ONE GO, IN ONE EXPORT.**
>
> The **SESSION KIND** determines the **RING SET**. There is never a reason to discover a session's rings
> later — and therefore never a reason for a second export, a second setup handshake, or a rendezvous
> protocol to glue them back together.

Vocabulary. Conflating these is what produced a multi-day bug (see §6):

| term | meaning |
|---|---|
| **EXPORT** | a DOCA mmap export of ONE host memory **region** + its ring descriptors, shipped to the DPU over the TCP setup socket (port 9727). The DPU **IMPORTS** it. |
| **BIND** | the **session ↔ ring** relationship. A session binds all the rings it needs. |
| **RING** | one descriptor inside an export: a control slot, a mailbox, a byte ring, or an event line. |

The postmaster's **frontend arena** already obeys the contract: **one region, 49 rings, one import**
(16 slots × 3 rings + 1 spawn ring). The client did not, until P6.

---

## 2. THE FOUR IDENTITIES — and which question each one answers

Getting these confused is the single most expensive category of bug in this subsystem.

| identity | scope | unique? | answers |
|---|---|---|---|
| `sessionKey` (`CitusRemoteExecSessionKey`) | DB-semantic | **NO — deliberately not** | *"Is this pooled session **reusable** for a new request?"* (db, user, node, access/tx/freshness/ownership policies). It is a **REUSE key**. |
| `serviceSessionId` | one service process | yes | *"Which session, in **this** process?"* Allocated from `NextTupleSinkServiceSessionId`. |
| `sessionUID` | cross-node | yes | *"Which session, **anywhere**?"* Minted client-side; threaded through the control and peer protocols. **This is the identity.** |
| `bridgeGeneration` | one export | yes | *"Which **export/import**?"* Per-export, not per-session. |

**THE RULE: ownership is IDENTITY, never policy.** If you need to know *which session owns this ring/stream*,
the answer is `sessionUID` (or `serviceSessionId` locally). It is **never** `sessionKey`, and it is never
the output of a reusability predicate. Asking `TupleSinkServiceSessionAllowsReuse()` who owns something is a
category error — it answers *"is this idle session safe to hand to a **new** client?"*, which is a different
question with a different answer.

That exact mistake minted a duplicate "peer compatibility session" for every client (fixed in P5.d, citus
`b19e0d663`).

---

## 3. RING ROLES

`HomerDpuBridgeDescriptorRole`, `src/include/distributed/homer/homer_dpu_bridge_abi.h`:

| role | name | direction | who exports it |
|---|---|---|---|
| 1 | `FRONTEND_CONTROL_SLOT` | host→DPU | the client (per session) |
| 2 | `BACKEND_COMMAND_MAILBOX` | host↔DPU | the postmaster (arena slot) |
| 3 | `BACKEND_COMPLETION_MAILBOX` | host↔DPU | the postmaster (arena slot) |
| 4 | `PAYLOAD_BYTE_RING` | host→DPU | the client (basebackup) |
| 5 | `SQL_RESULT_BYTE_RING` | host→DPU | the postmaster (arena slot) — the backend's result producer ring |
| 6 | `FRONTEND_COMPLETION_EVENT` | DPU→host | the client (per session) |
| 7 | `PAYLOAD_BYTE_RING_DPU_TO_HOST` | DPU→host | the client (per session) — decoded tuples land here |
| 8 | `BACKEND_SPAWN_REGION` | host↔DPU | the postmaster (once) |

`HOMER_DPU_BRIDGE_PROTOCOL_VERSION` = **5**. `ringCount` / `descriptorCount` are **runtime** fields of
`HomerDpuBridgeControlBlockHeader` — an export may carry any number of rings (the arena carries 49). **Adding
a ring to an export therefore needs NO protocol bump.**

### Who exports what (post-P6)

| exporter | region | rings |
|---|---|---|
| **selected-DPU SQL client** (e.g. `pgbench --homer --homer-dpu-command`) | one per session | **1 + 6 + 7** — ONE export |
| **basebackup client** | one per stream | 1 + 4 |
| **postmaster** (`citus.enable_homer_dpu_frontend_agent=on`) | `/dev/shm/citus_homer_frontend_arena_v3`, ~57 MiB | 16 slots × (2, 3, 5) = 48, **plus** 8 (spawn) = **49** — ONE export, two mmap imports |

---

## 4. TOPOLOGY

For `pgbench --homer --homer-dpu-command` (the gate workload):

```
farnet0 HOST            farnet0 DPU              farnet1 DPU            farnet1 HOST
  pgbench      ──TCP──▶   "node A"   ──RDMA──▶    "node B"    ──DMA──▶   postgres
  (client)      setup     10.10.1.200   :9717     10.10.1.201            socketless
                :9727                                                    backend
       ◀──── role 7 (decoded tuples, DMA) ────────────┘
```

- **node A** hosts the client's session: its role-1 control slot, role-6 completion line, role-7 result
  ring. It runs the two-ring deform relay that DMAs decoded tuples into role 7.
- **node B** owns the DPU↔backend side: it spawns the socketless backend, binds an **arena slot**
  (roles 2/3/5), and runs the P2.T teardown handshake.
- **farnet1's HOST service is DOWN** during validation — that is the anti-fallback guard. If the farnet0
  HOST service log shows anything beyond its startup banner, the workload silently fell back.

---

## 5. LIFECYCLE

### 5.1 Open

`HomerClientOpenSqlSessionSelectedDpu` — **one** TCP setup + **one** DOCA mmap export carrying the session's
whole ring set (1, 6, 7). The DPU imports it. `OPEN_SESSION opKind=COMMAND_SESSION` over the control slot
creates the service session and **stamps its `sessionUID`**.

> ⚠ A **remote** command-session open is rejected by the SYNC control-slot handler
> (*"must use async local-control path"*) and is served by the **async** path. Both paths must stamp the
> `sessionUID`. The async one did not — for months — which is the root of the P5.d bug family.

### 5.2 Commands

`CLIENT_SQL_TX_BEGIN → n × SQL_EXECUTE → TX_COMMIT`. Completions come back on **role 6** (DMA-published by
the DPU), not through a host mailbox. Results stream backend → node B → RDMA → node A → **role 7**.

`postCommandState` on a completion: `UNSPECIFIED=0`, `REUSABLE_IDLE=1`, `DO_NOT_REUSE=2`, `FAILED=3`. Only
`CLIENT_SQL_SESSION_CLOSE` (and a real failure) is terminal; `TX_COMMIT`/`TX_ABORT` on a client-SQL session
are `REUSABLE_IDLE`.

### 5.3 Close — THE ORDER IS AN INVARIANT

`HomerClientCloseSqlSessionSelectedDpu`, and every step is load-bearing:

1. **SEMANTIC CLOSE** — publish `CLIENT_SQL_SESSION_CLOSE` and wait (bounded) for the backend's completion.
   This is what makes the backend leave its command loop and exit, and what makes node B run its P2.T
   teardown. The backend answers `DO_NOT_REUSE`, which latches the client's `sqlSessionTerminal` — **that is
   correct, and it must NOT be allowed to gate the rest of the close** (it did; see §6).
2. **LIFECYCLE CLOSE** — `CLOSE_SESSION` over the control slot. Answered by the **DPU SERVICE**, which is
   alive. This releases the session **and the sinks bound to it** (initiating their peer close), and stamps
   node A's clean-teardown flag. *This is node A's analogue of node B's T4.*
3. **SETUP CLOSE + DOCA teardown.**

> Step 2 must precede any export detach. A host-consumer **detach** is reported by node A as a
> payload-protocol **FAILURE** — correctly, for a client that *vanishes mid-stream*. Detaching before the
> close means a clean close runs the entire failure machinery.

### 5.4 Teardown (node B): the P2.T handshake

`T1 quiesce → T2 drain → T3 staged BACKEND_SLOT_RELEASE → T4 released/unbind`. T4 stamps
`teardownCompletedCleanly`, which is what tells the abort path a subsequent peer drop is *expected*.

### 5.5 Clean close vs. real failure

A clean close and a real peer failure **must never be indistinguishable**. The machinery:

`teardownCompletedCleanly` (session) → `peerResetAfterCleanClose` (**latched on the stream**) →
reset-COMPLETE takes the **clean-reclaim** branch and publishes **no failure**.

> ⚠ `peerResetAfterCleanClose` is a **LATCH**, not a derived value. It used to be recomputed at
> peer-reset-begin by looking the owner session up — but by then the close has **destroyed** that session, so
> `ownerSession == NULL` read as *"NOT a clean close"* when it means *"the owner already left"*. Cleanliness
> is decided when the owner **asks** to close. A later lookup may set it; it may never clear it.

---

## 6. THE BUG FAMILY — one sentence, five costumes

Everything expensive in this subsystem has been the same defect:

> **A missing thing read as a definite negative.**

| costume | what it looked like |
|---|---|
| a structurally-zero word | `publishedTail` on the DPU-mirror path is never written by anyone → a guard read `0` and rejected every command after the first |
| a zeroed struct | a memset entry read as a *valid* source kind instead of an invalid one |
| an un-armable collector | a scheduler action whose only ready-bit had no producer — **identical, in the logs, to a healthy idle one** |
| a guard with no `else` | a close that never ran looked exactly like a close that ran fine |
| `ownerSession == NULL` | *"the owner already left"* read as *"this was not a clean close"* |

**The countermeasures, and they are cheap:**

- *A zeroed struct lies; a NULL pointer confesses.* Make the invalid state **unrepresentable or fatal**,
  never plausible. Enum `INVALID = 0`.
- *Silence must not be a valid state.* Every skip logs. A guard that ANDs N conditions must report **which**
  one failed, with values.
- *Name what you measured, not just the value.* `polled_tail_field_addr`, not `addr`.
- *A criterion checked on one end of a two-ended path is not checked.* Name the **node** on every acceptance
  row. (P5 was declared closed on one node's logs, and was not closed.)
- *Absence of a log line is not absence of the event.*
- *Verifying that an outcome happened is not verifying it happened through the path you built.*

---

## 7. KNOWN GAPS (as of July 12, 2026)

| # | gap | state |
|---|---|---|
| 1 | **The result peer-open takes ~2.1 s to arm.** This is the whole of the observed `latency average = 431 ms` (one stall averaged over five transactions, not five slow ones); all results then flush in **4 ms**. It is a **one-time rendezvous stall**, *not* a per-command grant stall in the DMA engine. | top perf item; `p3trace`/`p2diag` must be stripped before measuring |
| 2 | **Mirror-path truth-source** (P4.1): the DPU-mirror path still maps a DPU-local producer shm whose data region is dead and whose `publishedTail` is producer-stale by construction. Six sites gate on the mapping *existing*. | fully designed (design doc §17.3), not started |
| 3 | **The producer byte-ring mapping still exists** (P4.2b) — its full removal *is* the host-service local-byte-ring retirement. | gated on that owner decision, not on anything technical |
| 4 | **Basebackup does not yet obey the one-export contract** (it exports roles 1 + 4 separately per stream). | follow-up to P6 |

---

## 8. Stale docs — do not trust these for current code

- `homer_current_implementation_checkpoint.md` (July 3) — predates the command-plane migration, P1–P6.
  **Milestone history only.**
- Any note describing SQL's command spine as living "entirely in the HOST service on both ends" — the
  command-plane migration moved it **onto the DPU**. A comment carrying that premise justified the
  duplicate-session bug for months.
