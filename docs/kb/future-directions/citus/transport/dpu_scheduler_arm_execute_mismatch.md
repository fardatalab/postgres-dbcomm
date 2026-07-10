# The DPU scheduler's arm/execute mismatch — a survey

> **Status: FUTURE DIRECTION, except for §0 which is FIXED AND VALIDATED.** One instance
> (finding 2 + 6 + 7) is already scheduled as **D6** in
> [`dpu_command_plane_migration_plan.md`](../../../implementations/citus/transport/dpu_command_plane_migration_plan.md),
> to be built at S3.0/S3.3b and cashed in at S4.0. The rest is a map, deliberately made *before* we trip
> over the instances one at a time.
>
> **Cost models below are ANALYTIC, not measured.** They are loop bounds read out of the source. None of
> this has been profiled. Do not cite a factor from this doc as a benchmark result.

---

## §0 — ⚡ CONFIRMED, AND IT WAS NOT A COST PROBLEM. It was a permanent, silent wedge.

**Found and fixed July 10, 2026 (citus `36a61a5a1`), while validating S3.0.** This is the only entry in
this doc that has been observed on hardware rather than read out of the source. Read it first: it changes
what you should expect the other findings to cost you.

### Symptom

With a live Homer frontend arena import, the DPU service **stops accepting new host setup connections,
forever**. `pg_basebackup` fails with `DPU setup socket exchange failed`; the DPU logs *nothing*; the
service spins at 100% CPU; `ss -ltn` on the DPU shows `LISTEN Recv-Q=1` — a completed connection sitting
in the backlog that is never `accept()`ed. Minutes later it is still there.

Reproduced deterministically: `pg_basebackup` through the 4-role DPU relay **passes** with
`citus.enable_homer_dpu_frontend_agent=off` (19.42 s, 23.2 GB, `CLOSE_ACK`) and **fails at setup** with it
`on`, same binaries, same everything else.

### Mechanism — three independent defects, each of which hid the others

**A. The arming counted rings the executor skips.**
`HOMER_PROGRESS_COLLECTOR_DPU_GROUPED_CONTROL_READ` was armed with
`knownExpectedWork = facts.hostMmapImported` and a ready hint of `facts.importedRingCount`
(`tuple_sink_service_process.c`, `HomerServiceAppendDpuDmaCollectorCandidates`). But the executor,
`HomerDpuDmaSubmitGroupedControlReads` (`homer_service_dpu_dma.c:1163`), skips every ring for which
`HomerDpuDmaDescriptorUsesHostPublishLine` is false — a **role whitelist of {1, 4, 7}**
(`homer_service_dpu_dma.c:5645`).

The arena imports **49 rings — 48 of roles 2/3/5, plus one role-8 spawn region. Zero enrolled.**
So the collector claimed 49 units of known expected work, every pass, forever, and submitted nothing.

> This is the *inverse* of findings 1–10 below. Those execute more work than the arming implies. This one
> **arms more work than the execution performs** — and because the scheduler's fairness accounting is
> driven by the arming, the lie propagated.

**B. The anti-starvation escape hatch is keyed on a SHARED clock.**
`HomerMachineBaselineCollectorFeedbackBackoffActive` suppresses a *blind* collector once
`recentEmptyPolls >= 8` with no productive poll, and only lets it out again when
`grantsSinceLastCollectorTouch >= HOMER_SERVICE_MACHINE_BASELINE_BLIND_BACKOFF_MAX_SKIP_GRANTS` (= 4).
That quantity is `HomerProgressFeedbackTick - collector->lastCollectedTick`, and

```c
collectorFacts->lastCollectedTick = sourceFeedback->lastTouchedTick;   /* :40133 */
```

is copied from the **source**, not the collector. **All five DPU collectors share
`HOMER_PROGRESS_SOURCE_DPU_DMA`.** So a permanently-armed sibling (defect A) keeps resetting every other
DPU collector's starvation clock, and the hatch never opens. The guard that exists precisely to prevent
indefinite suppression could not fire.

**C. Two jobs with different readiness live in one action.**
`HomerServiceDpuSetupTcpServerProgress` — which owns the listener's `accept()` — is called from
**exactly one place**: inside `HOMER_PROGRESS_ACTION_DPU_PE_DRAIN`
(`tuple_sink_service_process.c:41449`). A backoff policy that is *correct* for "don't poll an empty DOCA
progress engine" is *fatal* for "don't stop listening". An `accept()` returning `EAGAIN` is, of course,
an empty poll — so "no arrivals so far" was being used to justify "stop looking", which for a listener is
never valid: **a backlog is not observable without an accept attempt.**

The code already knew this shape. The same predicate hand-exempts `PEER_CM_SETUP` and
`PEER_CLOSE_LIFETIME` with the comment *"cold, but they are correctness-visible lifecycle work … do not
suppress them with blind-poll backoff"*. The DPU setup listener is the same species and was not on the
list — it couldn't be, because it isn't a collector.

### Evidence

A macro-guarded probe (`-DHOMER_DPU_SETUP_STARVE_DIAG=1`, kept in-tree) counts scheduler passes against
actual `DPU_PE_DRAIN` grants:

```
before fix   imported=0 rings=0    pass=1500001  pedrain_grants=750005     <- climbing
             imported=1 rings=49   pass=2000001  pedrain_grants=812042     <- frozen at the import
             imported=1 rings=49   pass=3500001  pedrain_grants=812042     <- still frozen
             [starve-diag] PE_DRAIN dropped: feedback-backoff (count=2500001)

after fix    imported=0 rings=0    pass=500001   pedrain_grants=500000
             imported=1 rings=49   pass=1500001  pedrain_grants=1500000    <- 1:1, no drops
```

### What was fixed, and what was not

| | Fix | Where |
|---|---|---|
| A | Count **enrolled** rings, once, at import: `HomerDpuDmaHostMmapImport.groupedControlRingCount` (`homer_service_dpu_dma.c:5904`), summed into `HomerDpuDmaSchedulerFacts.groupedControlRingCount`. Arm discovery from **that**, never from `importedRingCount`. Precomputed at import because `GetSchedulerFacts` runs on the busy-poll loop. | `homer_service_dpu_dma.{c,h}`, `tuple_sink_service_process.c` |
| C′ | `setupTcpKnownWork = setupTcpFacts.started` (was `.activeConnection`). Takes `DPU_PE_DRAIN` out of the blind class and therefore out of backoff. **A tactical hotfix, not the resting state** — see the corrections below. | `tuple_sink_service_process.c:39897`+ |
| B | **NOT FIXED.** | — |
| C (proper) | **NOT DONE.** | — |
| D | **NOT FIXED** (found after the above shipped). | — |

---

## §0b — CORRECTIONS to §0, and a fourth defect (July 10, 2026, later the same day)

Written after (i) an experiment that reverted C′ on the DPU tree only, and (ii) an independent Codex
review. Every claim below was measured or verified against source; the ones that contradict §0 above are
marked. **§0's narrative stands; three of its supporting claims do not.**

### ❌ CORRECTION 1 — fix A **alone** unwedges the listener. C′ is not load-bearing.

Measured on the DPU with C′ reverted and A kept, arena imported, zero sessions:

```
pass=2500001  pedrain_grants=1250010          <- ~50% of passes (was FROZEN)
[starve-diag] PE_DRAIN dropped: feedback-backoff   <- backoff still fires
ss -ltn: LISTEN Recv-Q=0                       <- listener healthy
```

With grouped-control no longer granted, nothing else advances the shared `lastTouchedTick`, so
`grantsSinceLastCollectorTouch` grows and the 4-skip escape hatch opens as designed.

So the wedge required **A (the trigger) AND B (the amplifier)**. Removing either breaks it. C′ merely
raises the grant rate from ~50% to 100% of passes. §0's *"Fixed by (A) and (C)"* overstates C′.

### ❌ CORRECTION 2 — C′'s cost is not "one `accept4()` per pass".

Three things, verified:
- The listener calls **`accept()`**, not `accept4()` (`homer_service_dpu_setup_tcp.c:369`).
- The same action then calls `HomerDpuDmaDrainPe` → `doca_pe_progress()` on an empty PE.
- The same action then calls `HomerDpuDmaReclaimDetachedImports` **and** `HomerDpuDmaHasDetachedImports`
  (`tuple_sink_service_process.c:41625`, `:41634`), each an **O(1024) import-table scan**.

So C′ buys, per pass while idle: one syscall + one empty PE progress + up to two 1024-slot scans. Not
visible in the 23 GB basebackup band (see the cost note below), because during real work `inflight > 0`
already made `PE_DRAIN` non-blind. The cost is paid **only when idle** — which is also when it is useless.

### ❌ CORRECTION 3 — `knownExpectedWork = true` does **not** guarantee the collector executes.

`HomerServiceMachineBaselineAppendCollectorAction` (`:9850`) reserves budget *after* the backoff gate.
A non-blind collector still competes for the **total collector quota** and loses to **fixed collector
ordering** (command send CQ and grouped control are appended before PE drain). So C′ made the wedge
unlikely, not impossible. **By the same argument, fixing B does not guarantee it either.** The only
construct that guarantees a listener poll is a **reserved admission** that generic collectors cannot
consume. *(Credit: Codex review. Neither the original diagnosis nor the first proposed fix caught this.)*

### 🆕 DEFECT D — `runnableMachineWork` is *always* true, so backoff's idle-service guard is dead

`HomerMachineBaselineCollectorFeedbackBackoffActive` short-circuits to `false` unless `runnableMachineWork`
(`:9612`), and its comment promises *"keeps an otherwise idle service from suppressing unknown-arrival
collectors indefinitely."*

That promise is void. `HomerServiceBuildPeerAndMaintenanceMachineCandidates` (`:40803`) unconditionally
appends a heartbeat machine with `readyActionMask = RUN_MAINTENANCE` (`:40827`) whenever the
`HEARTBEAT_MAINTENANCE` collector is a candidate — which is always. Measured, per plan phase, on a service
with **zero sessions, zero streams, zero in-flight DMA**:

```
plan phase=0 runnable=0 machines=0     (RESET)
plan phase=1 runnable=0 machines=0     (LOCAL_IPC)
plan phase=2 runnable=1 machines=1     (COLLECTOR)   <- the phase that plans DPU collectors
plan phase=3 runnable=0 machines=0     (SEMANTIC)
```

**Blind-poll backoff is permanently armed in the collector phase, on every DPU service.**

D is also, right now, *load-bearing in the other direction*: the escape hatch is denominated in **grants**
(`HomerProgressFeedbackTick` advances only when some grant executes), so if a skipped collector were the
only thing with work, the tick would never move and the skip would be permanent. That cannot happen today
**only because** the always-runnable heartbeat machine yields a grant every pass. Two mechanisms, either
sufficient, neither documented. Any fix to D must pick one on purpose.

### 🔎 THE REAL SHAPE OF B — the DPU family is the only one that is not 1:1 collector→source

Both directions of the feedback mapping collapse for the DPU family, and only for it:

| | mapper | DPU family |
|---|---|---|
| write | `HomerServiceUpdateProgressFeedbackAtTick` (`:13231`) resolves feedback from `sourceRef` | 11 collectors → `dpuDmaSource` (`:9758`) → `dpuDmaFeedback` (`:13219`) |
| read | `HomerServiceProgressCollectorFeedbackForKind` (`:38182`) | 11 collectors → `dpuDmaFeedback` (`:38218`) |

Every other collector is 1:1 with its own source and its own feedback struct (`localControlSource`,
`cqDrainSource`, `peerCmSetupSource`, `peerRecvCqSource`, `peerSendCqSource`, `peerCloseLifetimeSource`).
**The per-source feedback machinery is correct under a 1:1 invariant. The DPU family broke the invariant.**

And it is not only the tick. `HomerServicePopulateCollectorFeedbackFacts` (`:40159`) copies **all three**
backoff inputs from the shared struct:

```c
collectorFacts->recentEmptyPolls      = sourceFeedback->consecutiveEmptyGrants;      /* :40174 */
collectorFacts->recentProductivePolls = sourceFeedback->consecutiveProductiveGrants; /* :40175 */
collectorFacts->lastCollectedTick     = sourceFeedback->lastTouchedTick;             /* :40176 */
```

So a productive grouped-control read can clear `DPU_PE_DRAIN`'s empty streak, and an empty PE drain can
increment grouped control's. **A per-collector `lastGrantedTick` alone does not fix B.**

> *Retracted:* an earlier note claimed `HomerProgressMachineCandidateSetAppendCollector` (`:8697`) sums
> these counters *across* collectors. It does not — the merge is guarded on
> `candidate->kind == collectorFacts->kind && candidate->index == collectorFacts->index`, so it merges
> duplicate records of the **same** collector. Summing a monotone *streak* across duplicate records is
> still wrong, but it is not the aliasing channel.

### ✅ THE HONEST ARMING FOR A LISTENER — and it already exists in this repo

The service cannot learn that the kernel's listen backlog became non-empty without a syscall. There is no
maintained user-space "pending connection" bit. So `knownExpectedWork` for a listener must not assert
*"an item is ready"* — it can't know that. It should assert **"a poll is due."**

That is a maintained, honest, cadence-controlled fact, and the RDMA peer listener already implements it:

| step | code |
|---|---|
| choose the interval | `TupleSinkServiceListenerLivenessInterval` (`remote_execution_peer_transport_rdma.c:1939`) |
| arm the next due pass | `TupleSinkServiceArmListenerLiveness` (`:1954`) |
| raise the durable due bit at pass start | `listenerLivenessDue = true` (`:1987`) |
| scheduler consumes it | `peerSchedulerFacts.listenerPollDue` (`tuple_sink_service_process.c:41081`) |
| clear **and rearm only at actual execution** | `:10803` |

Seen this way, **C′ is the degenerate case of this pattern with `interval = 0`** — "a poll is due, always."
The right version says "a poll is due every N passes": honest, bounded-latency, and it deletes the idle
per-pass syscall and the two 1024-slot scans.

It also explains the two-name exemption list. `PEER_CM_SETUP` has its own source, so its 4-grant bound
*already worked* — yet it was exempted anyway. Because once you have a **due** obligation, "due" must mean
"will run", and backoff could delay a due poll. The exemption is not belt-and-braces; **it is the missing
half of the poll-due contract**, added by hand, for one collector.

### Three latches, all found open

| latch | intent | why it was inert |
|---|---|---|
| "only back off while busy" | idle service polls everything | **D** — the heartbeat machine makes the service permanently "busy" |
| "skip at most 4 grants" | bounded starvation | **B** — the starvation clock is shared by 11 collectors |
| "never back off a listener" | correctness-visible lifecycle work | **C** — the listener is not a collector; it is fused inside one |

Defect **A** merely pushed on a door that three separate latches had quietly stopped holding.

### Revised rules

1. **Never arm a collector from a count its executor will filter.** (Unchanged; this is defect A, and it
   is findings 1–10's disease with the arrow reversed. An over-armed collector does not merely waste
   passes — it **steals fairness credit from its siblings** through the shared feedback struct.)
   *Refinement (Codex, adopted):* store the enrolled ring **index vector / bitset**, not just a count, so
   enrolled membership — rather than a predicate re-evaluated in two places — is the single source of
   truth. And name the fact honestly: it is an **enrolment** count, not an *immediately submittable* count
   (rings with `controlReadInFlight` set are skipped by the executor anyway,
   `homer_service_dpu_dma.c:1202`).
2. ~~*A listener is never blind maintenance; give it its own collector with `knownExpectedWork = started`.*~~
   **SUPERSEDED.** `knownExpectedWork = started` is a tactical hotfix and does not guarantee execution
   (correction 3). The rule is:
   > **A listener gets its own collector, its own action, and its own progress source; it advertises a
   > durable `pollDue` obligation on an interval, cleared and rearmed only when the poll actually runs;
   > and a due listener poll gets RESERVED admission that generic collector budgets cannot consume.**
   ⚠ **S3.2's doorbell listener must be built this way from the start.** Do not fuse it into another
   action, and do not copy C′.
3. **Do not measure performance on aliased scheduler feedback.** B corrupts the empty/productive streaks
   of all eleven DPU collectors, including the data-path ones (grouped control, command pull, payload
   pull, completion push). Fix B before any S6 number is recorded, or the number cannot be explained.

### Cost note

C′ makes `DPU_PE_DRAIN` run on ~100% of passes rather than ~50% **while idle** (during real work
`inflight > 0` already made it non-blind). Measured effect on the 23 GB 4-role DPU-relay basebackup: none
detectable. Warm repeats, agent OFF `19.50 / 18.92 / 22.80 s`; agent ON `20.34 / 22.09 / 19.89 s`. Bands
overlap. (Both arms discard the first run after a service restart.) This is a *no-regression* result on a
streaming workload, **not** evidence that the idle cost is free — see correction 2.

---

> ## 👉 This doc is the SYMPTOM MAP. The root cause is one missing field.
>
> Read [`dpu_readiness_collectors_and_continuation_edges.md`](dpu_readiness_collectors_and_continuation_edges.md)
> **first**, or at least alongside. Every finding below is a consequence of a single fact:
>
> **The ready queue is a hand-rolled completion queue with no cookie slot.** Its entries name the *source*
> (`importIndex`, `ringIndex`) and carry an `acceptedPublishedEpoch` that is literally immediate data — but
> no `wr_id`. So each consumer must re-derive *who was waiting*, by scanning. A recv-CQ hands back a
> `wr_id`; a `doca_pe` task hands back `doca_data`; a host CPU store into DOCA-exported memory hands back
> nothing, because there is no `WRITE_WITH_IMM` across PCIe.
>
> Ten findings, one missing edge, in two directions:
> **forward index** (waiter → resource, for pushers — this is D6) and
> **reverse cookie** (resource → waiter, for collectors — this is where the continuation runtime's
> `HomerDependencyResolve()` will land).
>
> If you fix these one at a time from the table below, you will write the same edge ten times.

## The pattern

The DPU service's progress scheduler is **demand-driven and well informed**. It decides whether to grant an
action from precise per-entity bookkeeping: per-session waiters, per-ring runtime flags, queue depths,
free-slot counts. The *grant* is right.

Then several action handlers **execute by scanning every entity and filtering on a type tag**. The scan's
cost is unrelated to the demand that armed it.

```
   arming:      "one selected session is waiting for a completion"        O(1) fact
   execution:   for each of 1024 import slots, for each of its rings,
                if descriptorRole == BACKEND_COMPLETION_MAILBOX ...       O(1024 + Σrings) probes
```

The information needed to close the gap almost always **already exists**, one layer up, in a per-session or
per-stream struct. What is missing is a *forward index*: a stored handle from the entity that has the demand
to the ring that serves it. See D6 in the migration plan for the canonical fix.

**A full engine sweep is `O(C + Σ rings of ACTIVE imports)`, not `O(C × rings)`** — inactive import slots
are skipped by a single check. With `hostMmapImportCapacity = 1024`
(`homer_service_dpu_dma.c:841`), one full miss is ≈ 1,024 import probes plus the rings of whatever is
imported: 49 for the frontend-agent arena (48 arena + 1 spawn), 1–2 per per-session client export.

## Ranked findings

Ranked by (hot-path × cost gap). "Layering" matters: a function inside the **DMA engine** has no access to
the selected-session table, so from where it sits a role scan is the only thing it *can* do. Those fixes
must move the choice up a layer, not push a condition down one.

| # | Executor | Armed by (exact demand) | Scan cost | Layering | Hot? |
|---|---|---|---|---|---|
| 1 | `HomerDpuDmaMirroredByteRangeReadyForServiceSink` (`homer_service_dpu_dma.c:4392`) + `...ClaimMirroredByteRangeForServiceSink` (`:4556`) | the exact stream pair (`tuple_sink_service_process.c:12523`, `:40615`) | `O(1024+Σrings)` **twice per outgoing payload grant** vs `O(1)` | engine can't see `HomerPayloadStreamState` | **yes — this is the basebackup egress path we measure today** |
| 2 | `HomerDpuDmaSubmitBackendCompletionPulls` (`:1261`) | `selectedBackendCompletionWaitCount` etc. (`tuple_sink_service_process.c:39957`-`:39992`) | `O(1024+Σrings)` vs `O(waiters)` | engine can't see sessions | yes, throughout every selected transaction's wait |
| 3 | `HomerDpuDmaSubmitMirroredByteRingConsumedHeadPublications` (`:3955`) | `facts.totalConsumedHeadPublishPendingCount` (`:40030`) — an exact aggregate | `O(1024+Σrings)` vs `O(pending)` | **none — demand and identity are both engine-local** | yes, per released payload range |
| 4 | `HomerDpuDmaClaimNextStagedBackendCompletion` (`:2516`) | same completion gates | `O(D×(1024+Σrings))` — **each claim restarts at index 0** | staged flag engine-owned | yes, ≥1× per transaction |
| 5 | `HomerDpuDmaCopyNextStagedCommand` (`:5049`) | `facts.totalStagedCommandRequestCount` (`:39929`) | `O(D×(1024+Σrings))` — also restarts at 0 | engine can expose a head without interpreting it | yes, every selected-DPU frontend command |
| 6 | `HomerServiceDpuStageOneBackendCommandForPublish` → `HomerDpuDmaFindDescriptorRef` (`:39260`) | the START's own session | `O(1024+Σrings)` vs `O(1)` **per transaction** | engine can't see sessions | yes |
| 7 | `HomerServiceDpuPublishOneSelectedCompletionEvent` → `FindDescriptorRef` (`:39531`) | `completionPushReadyCount` (`:40021`) | `O(1024+Σrings)` per queued event | engine can't see sessions | yes |
| 8 | `TupleSinkServiceFindOutgoingPeerConnection` (`remote_execution_peer_transport_rdma.c:6332`) | every remote `START_COMMAND` | `O(≤64)` vs `O(1)` — the session **already stores** `clientSqlPeerConnectionHandle` (`tuple_sink_service_process.c:1334`) | service knows; transport resolver doesn't | yes, per remote command |
| 9 | `TupleSinkServicePumpPeerRequestsRdma` (`:10645`) | exact machine wait masks (`:40222`) | `O(Hout+Hin)` ≤ 128 vs `O(demanded lanes)` | transport can't see machine state | hot/periodic; **partly intentional** (blind liveness) |
| 10 | `HomerDpuDmaReclaimDetachedImports` (`:7317`) | in-flight task counts | `O(1024)` outer scan even with **zero** detached imports | engine-local | hot outer, cold inner |

**Findings 2, 6, 7 are exactly D6.** One bind-time forward index kills all three.

**Finding 1 is the biggest and is not yet scheduled.** It sits on the outgoing DPU-mirror payload path —
the path that moved 23 GB in the validated basebackup run — and pays the scan **twice** per grant (readiness,
then claim). `HomerPayloadStreamState` (`tuple_sink_service_process.c:1595`) already stores
`dpuMirrorRingHandle` (`:1683`) and already demonstrates the fix elsewhere with a cached
`relayTargetDescriptorRef` (`:1786`). Fix: store the source-ring `HomerDpuDmaDescriptorRef` on the stream
and give the engine ref-taking readiness/claim entry points.

**Finding 3 is the cheapest win.** Demand and identity are both engine-local — no layering obstacle at all —
and the dead ready-queue kind `HOMER_DPU_DMA_READY_QUEUE_DPU_TO_HOST_CREDIT_PUBLISH` (`:117`) is sitting
there for it. Enqueue the ring ref when `byteRingCreditPublishPending` goes false→true.

## The ready-queue audit — 5 of 7 kinds are dead

The engine has the right machinery and uses it in two places. The enqueue function is
`HomerDpuDmaEnqueueReadyRef` (`homer_service_dpu_dma.c:6045`), with only **two** call sites (`:6226`,
`:9903`); `HomerDpuDmaPopReadyRef` is at `:6105`.

| `HomerDpuDmaReadyQueueKind` | Status |
|---|---|
| `HOST_TO_DPU_FRONTEND_COMMAND` | **live** — enqueued at `:9903`, popped at `:1195` |
| `HOST_TO_DPU_PAYLOAD` | **live** — enqueued `:9903`, re-enqueued `:6226`, popped `:3748` |
| `HOST_TO_DPU_BACKEND_COMPLETION` | dead. Mapped by role at `:5871`, but completion mailboxes are excluded from `HomerDpuDmaDescriptorUsesHostPublishLine` (`:5593`), so nothing ever enqueues |
| `DPU_TO_HOST_BACKEND_COMMAND_PUBLISH` | dead, same reason (`:5890`) |
| `DPU_TO_HOST_FRONTEND_RESPONSE_PUBLISH` | dead — enum only (`:116`) |
| `DPU_TO_HOST_CREDIT_PUBLISH` | dead — enum only (`:117`). **This is finding 3's home.** |
| `DPU_TO_HOST_PAYLOAD` | dead — explicitly reserved scaffolding (`:118`-`:125`) |

**Why the queue does not transfer to backend completion (finding 2).** For command pull and payload pull the
**host announces readiness** by advancing a publish line; grouped-control discovery observes it and pushes a
ref. For a backend completion mailbox the host announces nothing the DPU can see without a DMA read — **the
poll IS the discovery**. A ready queue would have to be refilled every pass with exactly {rings whose session
awaits a completion}: a materialization of the session set, plus a queue. Session iteration *is* that set.
Hence D6 rather than a queue. (The dead enum is evidence someone reached for the queue and stopped.)

## Named false positives, and two corrections to the survey that produced this doc

Both corrections came from spot-checking the survey against the source. Recorded because a survey trusted as
evidence is precisely this project's recurring failure mode.

- **`HomerDpuDmaClaimNextMirroredByteRange` (`:4445`) is NOT dead.** The survey reported "no caller"; it has
  three, all in `src/bin/homer_service_dpu_dma_smoke.c` (`:166`, `:387`, `:442`). The survey was scoped to
  four source files and the smoke was not among them — **my scoping error, not the surveyor's.** Correct
  classification: **Tier 2 — smoke-only, no production caller.**
- **The byte-ring `Bind` on the egress path (`tuple_sink_service_process.c:16804`) is DELIBERATE, not an
  oversight.** The survey flagged it as "Bind called before checking the cached handle." The code
  immediately below explains itself: the freshly bound slot identity is compared against the remembered one
  *precisely so* "a reused stream entry could [not] keep advertising an MR over an older slot." Cost is a
  scan of 8 pool slots. Low priority; a `Find` would serve, since `Bind`'s prefer-own arm already is one.

Genuinely fine, and worth naming so nobody re-flags them:
- `HomerDpuDmaSubmitCommandPulls` (`:1195`) and `HomerDpuDmaSubmitMirroredByteRingPulls` (`:3748`) pop typed
  ready refs. They are the model, not the problem.
- `HomerDpuDmaSubmitGroupedControlReads` (`:1054`) scans on purpose: its arming demand genuinely *is* "all
  imported rings that carry a host publish line," because no host doorbell exists to announce a tail advance.
- `HomerDpuByteRingFind` scans 8 pool slots, but `HomerDpuDmaResolveMirrorSlot` caches the result per ring
  (`:4177`-`:4213`), so it is first-resolution only.
- `HomerDpuDmaFindDpuToHostByteRingRef` (`:5286`) is amortized: the service caches `relayTargetDescriptorRef`
  and only re-scans while unresolved. **This is the forward-index pattern already applied once.**
- Teardown/close/diagnostic scans: `BeginHostMmapImportTeardown` (`:7104`), `MarkImportSenderCloseReleased`
  (`:7428`), `HoldImportForSenderClose` (`:7486`), `HoldImportForReceiverClose` (`:7542`),
  `MarkImportReceiverCloseReleased` (`:7595`), `SetupContainsServiceSession` (`:7665`),
  `ByteRingFrontierForServiceSink` (`:4492`), pool `Unbind`/`UnbindSession`, RDMA release/stats/destroy.
- The lone `spawnRegion->slots[]` scan (`tuple_sink_service_process.c:18909`) is per backend spawn, bounded
  by 32. Its *bugs* are real and fixed in S3 — see **D7** — but its cost is not a finding.
- Service-side scans bounded by 64 sessions (`FindSelectedSession` `:38761`, completion-event counting
  `:38956`, event-owner selection `:38935`) have no layering excuse but rank far below the 1,024-import scans.

## What this suggests, beyond the individual fixes

Four times now this subsystem has turned out to have **anticipated the right design and then taken a
shortcut in the implementation**: the descriptor roles (all seven already existed when the command plane
needed them), the transport-agnostic control dispatcher (one dispatcher, not two), the exporter ≠ reader
prior art in the Tier-2 path, and now five declared-but-dead ready-queue kinds.

The shortcut has a consistent shape: **iterate everything and filter on a type tag, because the tag is
available locally and the identity is not.** The identity is one layer up. The fix is always to carry a
handle down, never to teach the lower layer to guess.

## Related
- [`dpu_readiness_collectors_and_continuation_edges.md`](dpu_readiness_collectors_and_continuation_edges.md)
  — **the root cause of every finding here.** Why everything is polled, what a completion queue actually
  buys (aggregation + a cookie slot), the ready queue as a cookie-less CQ, and the two missing edges.
- [`homer_continuation_graph_scheduler_plan.md`](homer_continuation_graph_scheduler_plan.md) — the runtime
  these edges are for. Its `HomerReadyCatalog` is this engine's ready queue, keyed on a continuation instead
  of a ring.
- [`dpu_command_plane_migration_plan.md`](../../../implementations/citus/transport/dpu_command_plane_migration_plan.md)
  — D5 (`boundServiceSessionId`, the reverse cookie in its weakest form), D6 (the forward index; findings
  2/6/7), D7 (spawn-slot partition), D8 (arena publish-line reservation, S3.0).
- [`dpu_dma_backend_homer_service_current_scheduler_design.md`](dpu_dma_backend_homer_service_current_scheduler_design.md)
  — how the progress collectors/actions/grants fit together. Read it to see that the *arming* half is sound.
