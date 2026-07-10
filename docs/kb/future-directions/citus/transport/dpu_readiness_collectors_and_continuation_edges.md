# Readiness collectors, cookies, and the two missing edges

> **Status: future direction / design grounding.** Written July 10, 2026, against citus `c2ec17852`.
> Nothing here is implemented.
>
> This is the **why** for [`homer_continuation_graph_scheduler_plan.md`](homer_continuation_graph_scheduler_plan.md)
> (the design) and the **root cause** behind
> [`dpu_scheduler_arm_execute_mismatch.md`](dpu_scheduler_arm_execute_mismatch.md) (the symptom map).
> Read this one first when returning to the async-continuation work.
>
> **Cost claims below are analytic, read off loop bounds. Nothing here has been profiled.**

---

## 0. TL;DR

The DPU service already contains a hand-rolled completion queue for host-published readiness — the
**ready queue**. It is missing the two things a real completion queue has:

1. **Aggregation.** It polls one memory location per ring per pass, where the ABI was shaped to poll all
   of them in one DMA. The grouping is *parameterized*, and the parameter is pinned to 1.
2. **A cookie slot.** Its entries name the *source address* (`importIndex`, `ringIndex`) and never the
   *waiter*. So every consumer re-derives "who was waiting" by scanning. That single missing field is the
   whole of the arm/execute mismatch, repeated ten times.

The continuation runtime needs **two edges** that do not exist today, in opposite directions:

| Edge | Direction | Needed by | Missing today | Scheduled as |
|---|---|---|---|---|
| **Forward index** | waiter → resource | *pushers* ("I have a session; which ring do I DMA into?") | yes — `FindDescriptorRef` scans, per transaction | **D6**, command-plane S3.3b / S4.0 |
| **Reverse cookie** | resource → waiter | *collectors* ("ring 37 advanced; who was waiting?") | yes — `FindSelectedSession` scans | *unscheduled;* this doc |

`HomerDependencyResolve()` in the plan is exactly the moment the **reverse cookie** is dereferenced.

---

## 1. The question this doc exists to answer

An earlier draft of the analysis proposed three classes of dependency, the third being *"remotely mutated,
with no publication — nothing to register a wait on."* That taxonomy was **wrong**, and it nearly went into
the plan. It is recorded here because the correction is the most useful thing in this document.

> *"How come there is a Class C? I thought everything that mutated, whether RDMA or DMA, will be published.
> Granted, for DMA, unlike RDMA, there's no recv-CQ, but both need to be polled — recv-CQ, DOCA PE, or
> memory."*

Correct. Everything is published, and **everything is polled**.

---

## 2. "Events versus scanning" is a false dichotomy

All three of Homer's readiness sources are polls:

- **recv-CQ** — `ibv_poll_cq`, drained per pass.
- **DOCA PE** — `doca_pe_progress()`, drained inside the granted DMA continuation.
- **host memory** — a DMA read of a control block.

The continuation plan never claimed otherwise. Its own noun is **collector**
([`plan:825` external resolvers](homer_continuation_graph_scheduler_plan.md)), and *a collector is a poll
that demultiplexes*. Any statement of the form "readiness arrives as events rather than being discovered by
scanning" is sloppy and should be struck.

---

## 3. What a completion queue actually gives you

Two properties, and only two. Neither is "avoids polling."

| | recv-CQ | DOCA PE | host memory word |
|---|---|---|---|
| **Aggregation** — one poll covers many sources | yes | yes | **no** — one read per location |
| **Cookie slot** — the poll hands back the identity you registered | `wr_id`, immediate data | `doca_data` | **no** — you get an address |
| **Poll cost scales with** | #events | #events | **#locations you choose to read** |

**Homer already pays real money for property 1+2 on the RDMA path, deliberately.** Bulk payload writes are
silent `IBV_WR_RDMA_WRITE`; the **last** write of a batch is upgraded to `IBV_WR_RDMA_WRITE_WITH_IMM`
(`remote_execution_peer_transport_rdma.c:9463`, `:9698`, `:9266`), purely so that the arrival lands in the
receiver's recv-CQ as `IBV_WC_RECV_RDMA_WITH_IMM` (`:3163`, `:6669`). `WRITE_WITH_IMM` is the slower opcode.
It is bought for the announcement.

**And DOCA task completions self-demultiplex.** Every submit site does `taskUserData.ptr = slot`
(`homer_service_dpu_dma.c:8657`, `:8829`, `:8965`, `:9096`, `:9263`) so the completion callback recovers its
`HomerDpuDmaTaskOwner` in O(1). The codebase knows the cookie pattern. It applies it wherever the hardware
offers a slot to put one in.

**A host CPU store into DOCA-exported memory has neither.** There is no `WRITE_WITH_IMM` for a PCIe memory
write; nothing lands in any DPU queue. The DPU's only option is to go read the location. That — not "no
publication" — is the *entire* asymmetry between the RDMA path and the DMA path.

---

## 4. The ready queue is a cookie-less completion queue

```c
/* homer_service_dpu_dma.c:238 */
typedef struct HomerDpuDmaReadyRef {
    uint32_t importIndex, ringIndex;      /* the SOURCE ADDRESS                        */
    uint32_t workloadClass, descriptorRole;/* the class, for queue selection            */
    uint64_t bridgeGeneration;            /* staleness guard, checked at pop            */
    uint64_t acceptedPublishedEpoch;      /* <-- this is, literally, immediate data     */
} HomerDpuDmaReadyRef;                    /*      ...and there is no wr_id.             */
```

Mechanics, all verified:

- **Seven queues, one per `HomerDpuDmaReadyQueueKind`** — `readyQueues[KIND_COUNT]` on the engine
  (`homer_service_dpu_dma.c:612`), each a ring buffer with compaction on full.
- **Set semantics, not stream semantics.** `ringRuntime->readyRefQueued` is a per-ring bit; enqueue is a
  no-op if already queued (`:6061`); pop clears it. A ring appears at most once. This is `readyBits` from
  the plan's `HomerReadyCatalog`, in embryo.
- **Pop revalidates** against `bridgeGeneration` and import liveness, so a ref that outlives its import is
  discarded rather than dangling. This is the plan's `registrationGeneration`, in embryo.
- **One producer of readiness:** the *completion handler of a grouped-control DMA read* (`:9895`-`:9905`).
  If `line->publishedTail > acceptedPublishedTail && entryState == ACTIVE`, it sets `discoveredReady`, maps
  role → queue kind, and enqueues. A second site (`:6226`) re-arms a ring that still has un-pulled bytes.
- **Two consumers:** command pull (`:1195`) and payload pull (`:3748`).

So structurally the ready queue **is** the plan's ready catalog: durable across passes, deduplicated,
generation-revalidated, per-class. It differs in exactly one respect, and it is the important one:

> **A ready *ref* says "ring 7 has bytes." A ready *continuation* says "this strand can run."**
> Today the consumer must derive the second from the first, by scanning.

Every finding in [`dpu_scheduler_arm_execute_mismatch.md`](dpu_scheduler_arm_execute_mismatch.md) is a
consequence of that one sentence.

---

## 5. Enrolment in discovery is a *typing* accident, not a semantic property

> ### ⚠ CORRECTION (July 10, 2026) — the *mechanism* is a hard-coded role whitelist
>
> An earlier version of this section said "a ring is enrolled iff its `hostControlOffset` points at a
> 64-byte `HomerDpuBridgeHostPublishLine`." That is the **rationale**, not the **code**. The code is:
>
> ```c
> return descriptorRole == FRONTEND_CONTROL_SLOT ||
>        descriptorRole == PAYLOAD_BYTE_RING ||
>        descriptorRole == PAYLOAD_BYTE_RING_DPU_TO_HOST;   /* homer_service_dpu_dma.c:5645 */
> ```
>
> Three consequences, all good:
> - Laying out a publish-line array in a region and rebasing its descriptors **cannot accidentally enrol**
>   roles 2/3/5. Enrolment is a deliberate, separate edit to that list. (D8 relies on this.)
> - Promotion is therefore "allocate the lines, point `hostControlOffset` at them, add three enum values",
>   not "fix the types" — a much smaller change than the typing story implies.
> - Conversely, the whitelist is invisible to the scheduler, which armed discovery from
>   `importedRingCount`. That mismatch was a real, permanent wedge; see §0 of
>   [`dpu_scheduler_arm_execute_mismatch.md`](dpu_scheduler_arm_execute_mismatch.md).
>
> The table below remains correct as an explanation of *why* the whitelist has the members it has.

Which rings get discovered at all is decided by `HomerDpuDmaDescriptorUsesHostPublishLine()`
(`homer_service_dpu_dma.c:5645`), which returns true for roles **1, 4, 7** only. The list has the members
it has because of a struct:

> **A ring is enrollable iff its `hostControlOffset` can point at a 64-byte
> `HomerDpuBridgeHostPublishLine`.**

Because `HomerDpuDmaSubmitOneGroupedControlRead` reads exactly `sizeof(HomerDpuBridgeHostPublishLine)` bytes
from `hostRingAddress + hostControlOffset` and casts them to that type (`:8430` requires
`controlBytes >= 64`). And so:

| Role | Its control block | Publication word | Enrolled? |
|---|---|---|---|
| 1 `FRONTEND_CONTROL_SLOT` | a `HostPublishLine` in the client's contiguous array | `publishedEpoch` @ **0** | ✅ |
| 4 `PAYLOAD_BYTE_RING` | a `HostPublishLine` in the same array | `publishedEpoch` @ 0 | ✅ |
| 7 `..._DPU_TO_HOST` | a `HostPublishLine` (carries the host consumer's `consumedHead`) | `publishedEpoch` @ 0 | ✅ |
| 3 `BACKEND_COMPLETION_MAILBOX` | the mailbox's own 24-byte header | `publishedEpoch` @ **8** | ❌ |
| 5 `SQL_RESULT_BYTE_RING` | a `CitusHomerPayloadByteRingControl` — **also 64 bytes!** | `publishedTail`, **no epoch** | ❌ |
| 2 `BACKEND_COMMAND_MAILBOX` | the mailbox's own 24-byte header (DPU→host anyway) | — | ❌ |

**Roles 4 and 5 are both host-produced byte rings, and one is enrolled and the other is not.** Nothing
semantic separates them. Role 4's descriptor happens to point into the client's publish-line array; role 5's
points at its own byte-ring control block.

So the excluded rings are not unschedulable. **They are unregistered.** Nobody ever allocated them a line.

`HomerDpuDmaReadyQueueKindForDescriptor` (`:5848`) already maps role 3 → `HOST_TO_DPU_BACKEND_COMPLETION`
and role 2 → `DPU_TO_HOST_BACKEND_COMMAND_PUBLISH`. Somebody wrote the mapping. It is unreachable because
`discoveredReady` never fires for those rings, because they have no line for the reader to type-pun.

---

## 6. Grouped-control is parameterized, and the parameter is 1

Three sites, and they contradict each other.

**Task-slot init** (`homer_service_dpu_dma.c:10520`-`:10527`) is written for the grouped design:

```c
slot->owner.ringIndex = HOMER_DPU_BRIDGE_INVALID_RING_INDEX;   /* "this task covers MANY rings" */
slot->owner.u.groupedControl.controlFirstIndex = 0;
slot->owner.u.groupedControl.controlCount =
    engine->config.groupedControlBufferBytes / HOMER_DPU_BRIDGE_CACHE_LINE_BYTES;  /* capacity, in LINES */
```

**Submit** (`:8613`-`:8619`) overrides both back to a scalar:

```c
slot->owner.ringIndex = ringIndex;                        /* a single ring after all */
slot->owner.u.groupedControl.controlFirstIndex = ringIndex;
slot->owner.u.groupedControl.controlCount = 1;
```

**Config** (`:839`): `groupedControlBufferBytes = HOMER_DPU_BRIDGE_CACHE_LINE_BYTES` — the per-task buffer
is exactly **one** cache line, so even the capacity formula evaluates to 1.

**Neither `controlFirstIndex` nor `controlCount` is ever read.** The accept path uses the scalar
`slot->owner.ringIndex` (`:8108`) and interprets the buffer as one publish line. They are write-only fields,
computed by two mutually contradictory formulas.

### The sibling arrays, and which one survived

- `bridgeHeader.dpuCreditOffset` — lines the DPU **writes** — **is** read, and index-addressed:
  `creditOffset = dpuCreditOffset + ringIndex * sizeof(HomerDpuBridgeDpuCreditLine)` (`:9656`).
- `bridgeHeader.hostPublishOffset` — lines the DPU must **read**, where batching is the whole point — is
  **never read**. Every enrolled descriptor carries its own `hostControlOffset` instead.

*The array abstraction survived exactly where it bought nothing and was abandoned exactly where it was the
point.* (This corrects fact **F2** in
[`dpu_command_plane_migration_plan.md`](../../../implementations/citus/transport/dpu_command_plane_migration_plan.md),
which claimed both offsets were unread host-side bookkeeping.)

### What a real grouped read is

Four changes; no ABI change, because the client already lays its publish lines out contiguously at
`hostPublishOffset`, one cache line per ring (`homer_client.c:2790`, `:3129`, `:3504`).

1. `groupedControlBufferBytes = N * 64` — it is already the capacity denominator.
2. Submit reads from the **array**: source `hostRingAddress + bridgeHeader.hostPublishOffset + first*64`,
   length `count*64`. This is why `hostPublishOffset` exists.
3. Accept loops `i ∈ [first, first+count)`, casts `buffer + (i-first)*64` to a `HostPublishLine`, and runs
   the existing per-line logic with `ringIndex = i`.
4. `owner.ringIndex` returns to `HOMER_DPU_BRIDGE_INVALID_RING_INDEX` — the sentinel the init code already
   sets.

**Publication semantics are untouched.** Each line still self-validates through its own `publishedEpoch`,
which `_Static_assert`s to offset 0 of the line (`homer_dpu_bridge_abi.h`) precisely so that the publication
word is the first thing a reader sees. Grouping N lines into one DMA does not weaken the per-line protocol;
it only amortises the task slot and the completion.

**This is the plan's "shared bitmap collector"** (`plan:834`), and it is a *prerequisite* for the
continuation runtime, not an optimisation — see §8.

---

## 7. The two missing edges

### Forward index: `waiter → resource`

Needed by **pushers**. `HomerServiceDpuStageOneBackendCommandForPublish` has a session and needs its
command-mailbox ring, so it calls `HomerDpuDmaFindDescriptorRef`
(`tuple_sink_service_process.c:39260` → `homer_service_dpu_dma.c:5188`), which scans all imports × rings
comparing session id + role. **Once per `START_COMMAND`, i.e. per transaction.**

That is **D6**: cache `backendCommandMailboxRef` / `backendCompletionMailboxRef` / `resultByteRingRef` on
`HomerServiceDpuSelectedSessionState` at bind time, which is the one moment both identities are in hand.
Kills survey findings 1, 6, 7.

### Reverse cookie: `resource → waiter`

Needed by **collectors**. A grouped read (or a completion pull) learns "ring 37 advanced." It must then find
the waiter. Today: `HomerServiceDpuFindSelectedSession(descriptorRef.serviceSessionId)`
(`tuple_sink_service_process.c:38761`), a 64-entry scan of the selected-session table.

**D5's `boundServiceSessionId` is an accidental reverse cookie** — but a *cookie by value* (a session id
still requiring a table lookup) rather than a *cookie by index + generation* (`wr_id`-style, O(1)).

Nobody has scheduled the reverse cookie. It is the direct precondition for `HomerDependencyResolve`.

---

## 8. How `HomerDependencyResolve()` actually lands

The plan already prescribes the right home (`plan:796`-`:815`):

> But there should not be one global dynamically searched observer list.
> Use **specialized storage at each dependency owner**:
> `payload frontier owner: usually one required-frontier waiter`

**`HomerDpuDmaRingRuntime` *is* the frontier owner.** It already holds every scrap of frontier state —
`acceptedPublishedEpoch`, `acceptedPublishedTail`, `completedByteTail`, `discoveredReady`, `readyRefQueued`,
`byteRingCreditPublishPending`, `backendCompletionStagedValid`, and (since D5) `boundServiceSessionId`. It
holds **no waiter**.

Add one:

```c
typedef struct HomerDpuDmaRingRuntime {
    ...
    HomerWaitRegistration waiter;   /* the cookie the hardware would have carried */
} HomerDpuDmaRingRuntime;
```

and the acceptance handler at `homer_service_dpu_dma.c:9895` — today four lines that set `discoveredReady`
and enqueue a `(importIndex, ringIndex)` — becomes:

```c
if (line->publishedTail > ringRuntime->acceptedPublishedTail && line->entryState == ACTIVE) {
    ringRuntime->acceptedPublishedTail = line->publishedTail;
    HomerDependencyResolve(runtime, &ringRuntime->waiter);   /* <- the whole observer pattern */
}
```

That is the entire firing half of the observer pattern, at one site, with no global list — exactly as the
plan demands. The **registration** half is `HomerContinuationWaitForFrontier` (`plan:867`), which installs
`ringRuntime->waiter` and rechecks.

### Two consequences worth writing down now

**(a) Register-and-recheck collapses, but only because of the single service thread.**
The plan says so (`plan:894`): for service-local state updated by the one thread, registration and resolution
are serialised. For a *host-published* frontier the "concurrent writer" is the host — but the DPU never reads
host memory directly; it reads a **DPU-local cache** (`acceptedPublishedTail`) updated only by the grouped-
control accept, which is also the one thread. So the recheck is against local state, and it is free. There is
no lost-wakeup hazard here. Do not import a fence you do not need.

**(b) The wake latency of a host-published dependency is floored by the discovery period, not by the host's
store.** This is the one place where the memory path is *strictly* worse than a CQ, and no scheduler design
can fix it. What a scheduler design *can* fix is how much a discovery pass costs — which is why grouped
control is a prerequisite (§6). If discovery costs one 64-byte DMA per ring per pass, an event system built
on top of it has simply moved the poll, not removed it.

### Cookie inventory, per collector

| Collector | Aggregates? | Cookie carried by | Reverse map needed? |
|---|---|---|---|
| recv-CQ | yes | `wr_id` + immediate data | no |
| send-CQ | yes | `wr_id` | no |
| DOCA PE | yes (own tasks only) | `doca_data` → `HomerDpuDmaTaskSlot` → `HomerDpuDmaTaskOwner` | no |
| CM | yes | connection id | no |
| timer | yes | heap entry | no |
| **host publish lines** | **not today** (one DMA per ring) | **nothing** | **yes → `ringRuntime->waiter`** |

The DOCA PE row is the template. `doca_data` is the field that host memory does not have, and
`ringRuntime->waiter` is where we would keep it ourselves.

---

## 8b. ⚠ Two things this doc does NOT say

Both were learned by getting them wrong, on July 10, 2026, within an hour of writing the doc.

### (i) "Prefer a channel over a poll" is about HOT, HIGH-FAN-IN readiness. It is not a general rule.

The argument in §3–§7 is: *do not poll N sources every pass when an aggregating channel exists.* Its force
comes entirely from **N × per-pass**. Applied to a **cold, demand-armed, single event**, it inverts into bad
advice.

Worked example — the backend-spawn response (command-plane S3.3). It is one event, once per session open,
on a path immediately followed by `fork()` plus a database attach. `pendingSpawnCount` is 0 or 1. Polling it
by DMA is **exactly the D6 discipline** — poll the waiter, not the world. Routing it over the doorbell
socket instead would be "self-announcing", would remove a poll that costs nothing, and would **insert the
frontend agent between "the postmaster forked successfully" and "the DPU knows"** — producing a
forked-but-orphaned backend from a session the client saw fail. The poll has no such failure mode.

**Rule of thumb:** the channel wins when the poll's cost scales with *sources*. The poll wins when it scales
with *waiters* and the waiters are few. Check which one you have before quoting this doc.

### (ii) A DMA read is a `memcpy`, not a snapshot — and the publish line depends on an unstated assumption

PCIe guarantees **no atomicity for ordinary Memory Read/Write TLPs**; it added dedicated AtomicOp TLPs
precisely because they are not atomic. DOCA documents `doca_dma` as a memcpy and claims nothing about
atomicity or ordering of the source read. Reliable single-copy atomicity extends only to **naturally-aligned
≤ 8-byte** accesses.

Yet grouped-control discovery reads all 64 bytes of a `HomerDpuBridgeHostPublishLine` in one transfer
(`homer_service_dpu_dma.c:8699`) and then trusts `publishedEpoch`, `publishedTail`, `generation` and
`entryState` **together** (`:9971`).

That is sound, but not for the reason the code's `_Static_assert` used to give:

- `publishedEpoch` is at **offset 0**. Under an **ascending-order fetch** it is read *before* the body, so a
  torn read can only pair a **stale epoch with a fresh body**. The accept path sees
  `epoch == acceptedPublishedEpoch`, returns early, and re-reads next pass. Missing a publication is free —
  discovery is a perpetual poll.
- The opposite pairing (**fresh epoch, stale tail**) would be **fatal and silent**:
  `acceptedPublishedEpoch` is stored *unconditionally* (`:9941`) before the tail is examined, and every later
  pass early-returns on epoch equality (`:9882`), so the tail is dropped and the ring stalls forever.
  `HomerDpuBridgeFrontierMonotonic` is `observed >= previous` and a one-publication-stale tail **equals** the
  accepted tail — it does not fire.

**Per-field atomicity is necessary but not sufficient** (every field is an aligned ≤8-byte store; the hazard
is *cross-field*). The load-bearing assumption is **ascending fetch order**. It has ~23 GB per validated
basebackup run behind it and the frontier check has never fired. A trailing epoch echo would not remove it:
under a descending fetch a matching head/tail pair can still bracket a body from the next publication.

Two consequences for this doc's programme:

- **Grouping (§6) does not make this worse or better.** Reading N lines in one transfer still validates each
  line independently by its own epoch — it relies on the same assumption, N times per transfer instead of
  once. Say so when building it.
- **Where the body is immutable after publication, assume nothing.** Poll the publication word *alone*, then
  fetch the body in a **second, ordered** transfer. The backend-spawn slot does exactly that (command-plane
  D9b). It is the read-side mirror of the discipline the DPU already uses when it *writes*: body DMA writes
  first, then a **separate one-word publication write** on an ordered context
  (see `HomerDpuBridgeDpuCreditLine`'s own comment). The write side has always been right; the read side
  was never told.

## 9. Cost model (analytic, unmeasured)

Constants: `hostMmapImportCapacity = 1024` (`homer_service_dpu_dma.c:841`); a frontend-agent arena import
carries 49 rings; per-session client exports carry 1–2.

| Strategy | Discovery cost per pass | Demultiplex cost per event |
|---|---|---|
| today (per-ring poll + role scan) | 1 × 64 B DMA **per enrolled ring** | O(imports × rings) scan |
| **D6** (demand-driven poll of waiters) | 1 DMA **per waiting session** | O(1) from cached ref, O(64) for the session table |
| **grouped control** | **1 DMA of `N × 64` B**, all enrolled rings | unchanged |
| grouped + reverse cookie | 1 DMA | **O(1)** |

Grouping trades 48 task slots + 48 completions for 1 of each. That is a task-slot-pressure argument, not a
bandwidth one; **measure it at S6 before quoting a factor.**

---

## 10. Migration order, and why promotion-before-grouping is a regression

Roles 2/3/5 can be **promoted** into discovery by giving them a `HostPublishLine` (a discovery *hint*;
correctness still comes from the mailbox epoch / byte-ring control — exactly the split role 4 already has).

- **Promote without grouping** ⇒ discovery submits one 64-byte DMA per arena ring per pass, perpetually and
  **demand-independently**. For a 16-slot arena that is 48 DMAs per pass whether or not any session is
  waiting. **Strictly worse than D6's demand-driven poll.**
- **Group, then promote** ⇒ one DMA covers all of them, and demand-independence stops mattering.

So:

```
D6 (forward index)  ──┐  orthogonal; needed either way
                      ├──►  grouped control  ──►  promote roles 2/3/5  ──►  ready queue carries continuations
reverse cookie       ─┘
```

D6 and the reverse cookie are the two edges; grouping and promotion are the collector. They are independent
axes and the diagram is not a strict sequence — only *promote after group* is load-bearing.

---

## 11. Cheap moves that keep the door open

Ordered by (door opened) ÷ (cost). The first has a **closing window**.

1. **Reserve the arena's publish-line array now**, while the arena ABI is `v1` and only S2 has shipped
   against it. `HomerDpuBridgeHostPublishLine hostPublishLines[HOMER_FRONTEND_ARENA_RING_COUNT]` at the
   arena head, with `bridgeHeader.hostPublishOffset` pointing at it. **48 × 64 B = 3 KiB on a 57 MiB
   region.** Nothing writes them, nothing reads them. Later, backends stamp their line beside the mailbox
   epoch and the DPU groups all 48 into one read — **with no ABI bump and no coordinated two-DPU redeploy.**
   Tracked as **D8** in the command-plane plan.
2. **Make `controlCount` honest.** It is a range field, always 1, never read, and its two writers disagree.
   Assert and comment, naming the shared-bitmap collector. Five dead ready-queue kinds were born of exactly
   this kind of silence.
3. **Name the reverse cookie.** Keep `boundServiceSessionId` on `HomerDpuDmaRingRuntime` (D5 put it in the
   right struct by accident) but document it as the `resource → waiter` edge, so widening it to a
   `HomerContinuationRef` is later a type change and not a redesign.
4. **Correct F2** in the command-plane plan (`dpuCreditOffset` *is* read and index-addressed).

---

## 12. Code pointer index

| What | Where |
|---|---|
| ready-queue kinds (5 of 7 dead) | `homer_service_dpu_dma.c:110`-`:127` |
| `HomerDpuDmaReadyRef` | `:238` |
| `readyQueues[]` on the engine | `:612` |
| dedup bit / enqueue | `:6045`, `:6061` |
| pop + revalidate | `:6105` |
| the only readiness producer | `:9895`-`:9905` |
| re-arm site | `:6226` |
| the two live consumers | `:1195` (command pull), `:3748` (payload pull) |
| role → queue kind (maps 4 roles; 2 unreachable) | `:5848` |
| enrolment predicate | `:639` (body ~`:5593`) |
| grouped-control submit (`controlCount = 1`) | `:8613`-`:8619` |
| grouped-control init (`controlCount = capacity`, `ringIndex = INVALID`) | `:10520`-`:10527` |
| `groupedControlBufferBytes = 64` | `:839` |
| grouped-control accept (one line) | `:8108`, `:9820`-`:9905` |
| `dpuCreditOffset` read + index-addressed | `:9656` |
| `hostPublishOffset` — never read | (absence) |
| `FindDescriptorRef` — reverse lookup, per START | `:5188`; called `tuple_sink_service_process.c:39260` |
| `FindSelectedSession` — 64-entry scan | `tuple_sink_service_process.c:38761` |
| RDMA `WRITE_WITH_IMM` (bought announcement) | `remote_execution_peer_transport_rdma.c:9266`, `:9463`, `:9698` |
| RDMA recv-CQ demux | `:3163`, `:6669` |
| DOCA task cookie | `homer_service_dpu_dma.c:8657` (`taskUserData.ptr = slot`) |
| client's contiguous publish-line array | `homer_client.c:2790`, `:3129`, `:3504` |

## Related

Reading order, when resuming the async-continuation work:

1. **this doc** — the *why*: what a collector is, what a cookie is, and which of the two edges is missing
   where.
2. [`dpu_scheduler_arm_execute_mismatch.md`](dpu_scheduler_arm_execute_mismatch.md) — the *where*: ten sites
   in the live engine where the missing edges surface as linear scans, ranked, with false positives named.
3. [`homer_continuation_graph_scheduler_plan.md`](homer_continuation_graph_scheduler_plan.md) — the *what*:
   `HomerDependencyKind`, `HomerWaitRegistration`, `HomerReadyCatalog`, collectors, gates, the migration
   sequence. Its §"DPU-offloaded grounding" now carries a summary of this doc; §"Dependency resolution
   model" is where `HomerDependencyResolve()` is specified.
4. [`dpu_dma_backend_homer_service_current_scheduler_design.md`](dpu_dma_backend_homer_service_current_scheduler_design.md)
   — the *today*: how progress collectors / actions / grants actually fit together. Read it to see that the
   **arming** half is already demand-driven and sound; only the execution half scans.

Landed work that this analysis came out of, and that must stay consistent with it:

- [`dpu_command_plane_migration_plan.md`](../../../implementations/citus/transport/dpu_command_plane_migration_plan.md)
  — **D5** (`boundServiceSessionId` — the reverse cookie, by value; §7), **D6** (the forward index, i.e.
  continuation-graph edge #1; §7), **D7** (spawn-slot producer partition — unrelated, but the same file),
  **D8 / S3.0** (reserve the arena's `hostPublishLines[]` while the ABI is still `v1`; §6, §11). Its fact
  **F2** was corrected by §6 of this doc.
