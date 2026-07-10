# The DPU scheduler's arm/execute mismatch — a survey

> **Status: FUTURE DIRECTION.** Nothing here is implemented. One instance (finding 2 + 6 + 7) is
> already scheduled as **D6** in
> [`dpu_command_plane_migration_plan.md`](../../../implementations/citus/transport/dpu_command_plane_migration_plan.md),
> to be built at S3.0/S3.3b and cashed in at S4.0. The rest is a map, deliberately made *before* we trip
> over the instances one at a time.
>
> **Cost models below are ANALYTIC, not measured.** They are loop bounds read out of the source. None of
> this has been profiled. Do not cite a factor from this doc as a benchmark result.

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
