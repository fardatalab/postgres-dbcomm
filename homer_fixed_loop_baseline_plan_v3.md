# Homer Fixed-Loop Baseline Implementation Plan

## 1. Purpose

This document defines the baseline implementation we want before continuing with the newer guarded-action / state-machine scheduler work.

The goal is to create a credible, simple, non-adaptive Homer service baseline that represents the obvious fixed-loop design for an external RDMA-backed database communication service. It should be correct, reasonably engineered, and tunable through static constants, but it should not already contain the richer scheduler model we want to motivate.

The primary baseline is:

```text
Homer-FixedLoop-ExhaustivePayload
```

The secondary static baseline is:

```text
Homer-FixedLoop-Limited
```

Both should be based on the earlier `homer-client-sql-basebackup-merge` branch, not on the later `homer-better-service-progress-scheduler` branch.

## 2. Why use `homer-client-sql-basebackup-merge` as the base

`homer-client-sql-basebackup-merge` is the better baseline starting point because it still has the direct fixed-loop structure we want to study. Its `TupleSinkServicePumpOnce()` directly visits service phases in a hardcoded order:

```text
1. remote client SQL command pump
2. local control slot pump
3. peer transport pump
4. target completion pump, in restricted wait contexts
5. all completion mailboxes
6. payload stream table scan
7. pending peer client command completions
8. heartbeat
```

That shape is exactly the baseline story: one service thread, fixed phase order, local checks inside each phase, and no explicit scheduler policy object.

By contrast, `homer-better-service-progress-scheduler` is already too advanced for a clean naive baseline. It contains ready sets, source plans, policy hooks, CPU/liveness classes, blocked-source feedback, adaptive bounded-class policy state, and a separate egress policy. Those are useful later as engineering artifacts, but they blur the baseline by already exposing scheduler facts and policy decisions.

## 3. Research role of the baseline

The baseline should answer this question:

> If Homer externalizes communication into a user-space RDMA service, what is the simplest reasonable progress loop a competent systems engineer would build before introducing guarded actions, lifecycle dependency modeling, or adaptive scheduling?

The answer should not be a strawman. The baseline is allowed to:

- perform cheap local readiness checks;
- obey all protocol, RDMA, credit, and resource-lifetime correctness rules;
- use static bounds to prevent infinite loops and resource exhaustion;
- use fixed periodic polling for external arrivals;
- use static payload send/credit/CQ parameters;
- collect diagnostic counters.

The baseline should not:

- construct machine facts;
- construct collector facts;
- distinguish known work from unknown work as a scheduler concept;
- expose observation actions or guarded action candidates to a policy;
- rank work using CPU/liveness class;
- rank service-loop work using traffic class as an adaptive scheduler input;
- rank work using blocked/unblocked feedback;
- apply dependency-demand boosts;
- use adaptive plan length;
- use a separate egress scheduler/policy;
- choose payload grant sizes based on current foreground/bulk mix.

Traffic class is still allowed as static transport layout metadata. The current
baseline keeps separate RDMA transport lanes/QPs for control, foreground payload,
bulk payload, and maintenance-style work so unrelated traffic does not share one
QP ordering domain. That is not the scheduler policy being studied here. The
fixed-loop baseline does not build traffic-class facts, compare ready actions by
traffic class, or choose a payload grant from the current traffic mix.

We explicitly do **not** build a single-QP multiplexed baseline. A single QP
would require a unified transport envelope, receive-side demultiplexing, shared
credit/backpressure, and more complex lifetime dispatch into command, control,
payload, and session-specific sinks. That would study a different transport
design rather than the service-progress question. The baseline uses the existing
static lane/QP substrate and asks how far a fixed service loop gets before an
explicit scheduler is needed.

The baseline should be deliberately static. If a workload performs poorly, the answer should be: the static loop and static quanta cannot adapt to the workload mix. That is the motivation for the guarded-action scheduler.

Two distinct failure modes are expected and both are useful:

```text
1. HOL / over-service failure:
   A stream has a large visible backlog. FixedLoop-ExhaustivePayload spends a long
   non-preemptive visit on that stream, and foreground commands/completions wait.

2. Under-batching / eager-service failure:
   The service visits a stream very frequently. At each visit only one or a few
   tuple/COPY records are visible, so the service immediately posts tiny RDMA
   publications and pays fixed WR/doorbell/CQE costs too often. Bulk throughput
   suffers even though foreground latency may look good.
```

The second failure is not created only by the Limited baseline. It can happen in
FixedLoop-ExhaustivePayload when the producer exposes tuple records gradually and
the service loop is fast enough to catch each small gate. FixedLoop-Limited can
also force this behavior by choosing a small static per-visit cap, but that should
be treated as a static tradeoff ablation rather than the primary way to create the
effect.

## 4. Baseline definitions

### 4.1 Homer-FixedLoop-ExhaustivePayload

This is the primary naive baseline.

At each main service-loop pass, the service runs a fixed sequence of phases. Each phase walks its relevant table or queue in fixed index order. When the payload phase visits an active stream, it sends all payload work that was visible at the start of that stream visit, subject to fixed safety caps and resource limits.

This is a **gated exhaustive** payload discipline:

```text
visible_tail = producer_published_frontier at stream-visit start

while posted_frontier < visible_tail:
    if remote credit exhausted: break
    if local in-flight object/WR cap reached: break
    if descriptor batch is full: post current batch and continue if safe
    post next visible record/range

Do not spin waiting for future producer records.
Do not keep serving newly arriving records beyond visible_tail.
Do not revisit this stream until the next fixed payload phase.
```

This is intentionally throughput-oriented and simple. It is a natural RDMA baseline because it amortizes fixed posting and doorbell cost across all work already visible when the stream is visited. It may hurt foreground tail latency under mixed workloads because a bulk stream can consume a long non-preemptive service interval and post substantial ordered work before the loop returns to commands/completions.

### 4.2 Homer-FixedLoop-Limited

This is the secondary static baseline.

It uses the same fixed phase order and table scans as FixedLoop-ExhaustivePayload, but the payload stream visit has an additional static limit:

```text
max objects per stream visit
max bytes per stream visit
max WRs per stream visit
max pump iterations per stream visit
```

It is still not adaptive. It does not inspect foreground pressure, dependency demand, recent empty polls, age, or traffic mix. It simply uses smaller static grants.

FixedLoop-Limited is useful as a static tuning ablation:

```text
ExhaustivePayload: better bulk-throughput-biased baseline
Limited: better latency-biased static baseline
GuardedAction: dynamic action/grant scheduler that should avoid choosing one static compromise
```

### 4.3 WaitBatch diagnostic mode, not a primary baseline

A third mode is useful for diagnosis but should not be the main baseline:

```text
Homer-FixedLoop-WaitBatchDiagnostic
```

This mode keeps the same fixed loop and same fixed payload table scan, but when a
payload stream first appears ready it waits for a very small bounded number of
`cpu_relax` loops, or until a target number of adjacent records/bytes becomes
visible. Then it posts the now-visible batch.

This diagnostic is used to answer:

```text
Was FixedLoop-ExhaustivePayload under-batching because it served too eagerly?
```

If a tiny wait shifts the batch histogram from mostly size 1 to larger batches and
improves COPY throughput, then the immediate fixed loop is under-batching. If the
same wait hurts foreground pgbench tail latency in a mixed workload, that is the
scheduler motivation: the system needs a policy that can decide when batching is
worth waiting for, not a hardcoded wait on every payload visit.

Do not use WaitBatchDiagnostic as the paper's main baseline. It is an ablation
that demonstrates available batching opportunity and the latency/throughput tradeoff.

## 5. Implementation scope

### 5.1 Repositories and branches

Create matching baseline branches in both repositories:

```text
citus-dbcomm:    homer-unscheduled-baselines
postgres-dbcomm: homer-unscheduled-baselines
```

Use `homer-client-sql-basebackup-merge` as the starting point for both, unless there is a known protocol compatibility reason to pick a later non-scheduler commit.

The Citus branch owns the main service implementation. The Postgres branch should exist so that client binaries, protocol headers, `pgbench --homer`, `pg_basebackup`, and linked Homer client code match the service branch. If the Postgres-side branch needs no code changes beyond branch alignment and build scripts, say that explicitly in the branch note.

### 5.2 Files expected to matter most

Primary Citus file:

```text
src/backend/distributed/utils/homer/tuple_sink_service_process.c
```

Likely supporting files:

```text
src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c
src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h
src/include/distributed/homer/remote_execution_control_protocol.h
src/include/distributed/homer/remote_execution_peer_control_protocol.h
src/include/distributed/homer/tuple_sink_protocol.h
src/bin/homer_client.c
```

Postgres-side files may include the Homer-aware `pgbench` and `pg_basebackup` integration points if protocol or command-line contracts changed between branches.

## 6. Top-level service-loop target

The baseline should keep or restore a direct fixed-loop `TupleSinkServicePumpOnce()` shape. It should not construct a ready set or source plan.

Target skeleton:

```c
static inline bool
TupleSinkServicePumpOnceFixedLoop(TupleSinkServiceControlState *controlState,
                                  TupleSinkServicePeerTransportState *peerTransportState,
                                  TupleSinkServicePeerRequestContext *peerRequestContext,
                                  TupleSinkServiceSessionState *sessionStates,
                                  HomerServicePayloadStreamEntry *streamEntries,
                                  TupleSinkServiceSessionState *targetCompletionSession,
                                  uint32_t pumpFlags)
{
    bool madeProgress = false;

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_REMOTE_CLIENT_COMMANDS) != 0)
        madeProgress |= TupleSinkServicePumpRemoteClientSqlCommandsFixed(sessionStates);

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_LOCAL_CONTROL) != 0)
        madeProgress |= TupleSinkServicePumpControlSlotsFixed(controlState,
                                                              peerTransportState,
                                                              sessionStates,
                                                              streamEntries);

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_PEER_CONTROL) != 0)
        madeProgress |= TupleSinkServicePumpPeerConnectionsFixed(peerTransportState,
                                                                 peerRequestContext);

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_TARGET_COMPLETION) != 0)
        madeProgress |= TupleSinkServicePumpTargetCompletionFixed(targetCompletionSession,
                                                                  streamEntries);

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_ALL_COMPLETIONS) != 0)
        madeProgress |= TupleSinkServiceConsumeAllCompletionMailboxesFixed(sessionStates,
                                                                           streamEntries);

    if ((pumpFlags & HOMER_SERVICE_PUMP_PAYLOAD_STREAMS) != 0)
        madeProgress |= HomerServicePumpPayloadStreamsFixed(peerTransportState,
                                                            sessionStates,
                                                            streamEntries);

    if ((pumpFlags & TUPLE_SINK_SERVICE_PUMP_HEARTBEAT) != 0)
        madeProgress |= TupleSinkServicePumpHeartbeatFixed(controlState);

    return madeProgress;
}
```

The actual code does not need the `Fixed` suffix everywhere if the branch is dedicated to the baseline. The suffix is useful if we want to keep both fixed-loop and later code paths side by side.

## 7. Fixed phase semantics

### 7.1 Remote client SQL command phase

Purpose:

```text
Consume already-published frontend/client command records and forward them to the peer/backend path.
```

Baseline behavior:

- scan session table in fixed index order;
- process commands whose local mailbox/frontier says they are already visible;
- use static per-pass bound, defaulting to effectively exhaustive over visible command work if safe;
- do not rank sessions by age, traffic class, blocked state, or recent productivity;
- do not inspect payload or completion pressure to decide whether to stop early.

Suggested static knobs:

```c
#ifndef HOMER_FIXED_REMOTE_COMMAND_MAX_SESSIONS
#define HOMER_FIXED_REMOTE_COMMAND_MAX_SESSIONS UINT32_MAX
#endif

#ifndef HOMER_FIXED_REMOTE_COMMAND_MAX_ITEMS
#define HOMER_FIXED_REMOTE_COMMAND_MAX_ITEMS UINT32_MAX
#endif
```

For a first implementation, `UINT32_MAX` can mean “scan all active sessions and process visible work according to existing internal command limits.”

### 7.2 Local control phase

Purpose:

```text
Handle local shared-memory control slots: open, close, start command, poll completion, post-command state, and async local-control continuations.
```

Baseline behavior:

- scan control slots in index order;
- process `REQUEST_READY` slots up to a static cap;
- continue using existing async local-control continuation mechanisms if needed for correctness;
- do not rank control slots based on operation kind except where existing correctness requires special handling;
- do not use dependency-demand or collector semantics.

Suggested static knobs:

```c
#ifndef HOMER_FIXED_LOCAL_CONTROL_MAX_SLOTS
#define HOMER_FIXED_LOCAL_CONTROL_MAX_SLOTS UINT32_MAX
#endif

#ifndef HOMER_FIXED_LOCAL_CONTROL_ASYNC_PUMP_INTERVAL
#define HOMER_FIXED_LOCAL_CONTROL_ASYNC_PUMP_INTERVAL 1U
#endif
```

The async continuation interval may be tuned, but it should remain a static periodic mechanism, not a feedback policy.

### 7.3 Peer transport phase

Purpose:

```text
Drive RDMA peer-control setup/listener, recv CQ, mailboxes, send CQ, disconnect, and close/lifetime work.
```

Baseline behavior:

- call the aggregate peer pump in a fixed position in the loop;
- use fixed periodic polling for idle peer state;
- use fixed periodic or every-pass polling for active peer state;
- keep the peer transport’s internal phase order hardcoded;
- do not expose split peer phases as top-level scheduler actions;
- do not distinguish known setup work from unknown listener work at the scheduler level.

Suggested static knobs:

```c
#ifndef HOMER_FIXED_PEER_IDLE_POLL_INTERVAL
#define HOMER_FIXED_PEER_IDLE_POLL_INTERVAL 1024U
#endif

#ifndef HOMER_FIXED_PEER_ACTIVE_POLL_INTERVAL
#define HOMER_FIXED_PEER_ACTIVE_POLL_INTERVAL 1U
#endif
```

A useful default is active peer polling every pass and idle peer polling every 1024 passes. If every-pass active polling is too expensive, sweep active intervals such as 1, 16, 64.

Important: if a later correctness fix showed that active peer polling every 64 passes is required or that every-pass polling is too expensive, keep the static interval but document the default. The baseline’s defining property is static polling, not a specific interval.

### 7.4 Completion phase

Purpose:

```text
Consume backend/local completion mailboxes and publish completion state to the appropriate frontend/peer path.
```

Baseline behavior:

- scan session table in fixed index order;
- consume all currently visible completions per session or up to a static cap;
- do not prioritize terminal foreground completions over bulk/payload-related completions based on age or dependency;
- do not re-enter the scheduler when a completion unblocks payload or close work.

Suggested static knobs:

```c
#ifndef HOMER_FIXED_COMPLETION_MAX_SESSIONS
#define HOMER_FIXED_COMPLETION_MAX_SESSIONS UINT32_MAX
#endif

#ifndef HOMER_FIXED_COMPLETION_MAX_ITEMS
#define HOMER_FIXED_COMPLETION_MAX_ITEMS UINT32_MAX
#endif
```

### 7.5 Payload phase

Purpose:

```text
Move tuple/result/basebackup payload streams and perform stream-local send/receive/CQ/ACK/close progress.
```

Baseline behavior:

- scan payload stream table in index order;
- skip inactive streams;
- for each active stream, call the fixed payload pump;
- after payload scan, publish pending peer client command completions using the existing helper;
- do not create one scheduler source per stream;
- do not create an aggregate payload scheduler source;
- the whole phase is already an aggregate payload table scan.

Suggested static knobs:

```c
#ifndef HOMER_FIXED_PAYLOAD_MAX_STREAMS_PER_PASS
#define HOMER_FIXED_PAYLOAD_MAX_STREAMS_PER_PASS UINT32_MAX
#endif

#ifndef HOMER_FIXED_PAYLOAD_MODE
#define HOMER_FIXED_PAYLOAD_MODE HOMER_FIXED_PAYLOAD_EXHAUSTIVE
#endif
```

For `FixedLoop-ExhaustivePayload`, each stream visit uses gated exhaustive service over work visible at visit start.

For under-batching experiments, keep this immediate behavior and set any ready-spin
or wait-for-more-data diagnostic knobs to zero. The point is to observe whether
the service loop is visiting the stream so eagerly that the visit-start gate is
small.

For `FixedLoop-Limited`, each stream visit uses the same visible-frontier snapshot but stops after fixed caps:

```c
#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_OBJECTS
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_OBJECTS 1U
#endif

#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_BYTES
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_BYTES (256U * 1024U)
#endif

#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_WRS
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_WRS 4U
#endif
```

The exact limited defaults should be treated as experimental. The important point is that they are static.

### 7.6 Heartbeat phase

Purpose:

```text
Publish service liveness to local clients.
```

Baseline behavior:

- keep simple fixed heartbeat increment;
- optionally make heartbeat interval static if every-pass heartbeat adds measurable cost;
- do not treat heartbeat as a schedulable maintenance action.

Suggested static knob:

```c
#ifndef HOMER_FIXED_HEARTBEAT_INTERVAL_PASSES
#define HOMER_FIXED_HEARTBEAT_INTERVAL_PASSES 1U
#endif
```

## 8. Payload implementation details

### 8.1 Gated exhaustive semantics

For each stream visit, snapshot the local producer frontier before posting payload data:

```c
uint64_t visibleTailAtVisitStart = HomerServicePayloadProducerPublishedTail(streamEntry);
```

Then post only up to that visible frontier:

```c
while (HomerServicePayloadPostedFrontier(streamEntry) < visibleTailAtVisitStart)
{
    if (!HomerServicePayloadHasRemoteCredit(streamEntry))
        break;

    if (!HomerServicePayloadHasLocalSendResources(streamEntry))
        break;

    if (fixedMode == HOMER_FIXED_PAYLOAD_LIMITED &&
        HomerFixedPayloadVisitBudgetExhausted(&visitBudget))
        break;

    post next record/range according to existing transport correctness rules;
}
```

Do not spin-wait for future producer work. Existing diagnostic ready-spin code should remain disabled by default.

### 8.2 What “send all available” means

“Send all available” means:

```text
send all records visible at visit start, subject to correctness/resource caps.
```

It does not mean:

```text
wait for future records;
serve new arrivals indefinitely;
ignore remote credit;
ignore local WR/CQ/source lifetime limits;
post unbounded WR chains;
skip send-completion or ACK requirements.
```

### 8.3 Under-batching experiments

Under-batching is a scheduler-visible effect only when the stream visit sees a
small gate. It is not caused by small records alone.

The condition we want to expose is:

```text
stream visit at time t:
    only one or a few records are visible

shortly after t:
    more adjacent records become visible

FixedLoop-ExhaustivePayload-Immediate:
    posts the small visible gate immediately
    pays WR/doorbell/CQE/publication overhead for a tiny batch

better scheduler:
    may defer briefly, revisit sooner, or choose a larger grant when the
    batching opportunity is worth the latency cost
```

This distinction matters for both tuple/COPY and basebackup. Two 32 KiB records
and one 64 KiB record are not meaningfully different to the service-level
scheduling policy if both 32 KiB records are already visible at the stream visit
and the sender coalesces adjacent ranges. Smaller record size only makes
under-batching easier to expose if it interacts with producer cadence so the
service observes small visit-start frontiers.

#### 8.3.1 Natural under-batching with immediate gated service

Use `FixedLoop-ExhaustivePayload` with no ready-spin/waiting:

```text
ready-spin / wait-batch = disabled
payload mode            = gated exhaustive over visit-start visible work
```

The clean natural under-batching signature is:

```text
visit 1: one payload record visible -> post one tiny batch
visit 2: one more record visible   -> post one tiny batch
visit 3: one more record visible   -> post one tiny batch
```

This is not a gimped baseline. It is the natural immediate fixed-loop behavior.
It is simply too eager relative to producer publication cadence.

Candidate workloads:

```text
1. Homer payload microbenchmark
   Best controlled proof. Producer can expose records at a controlled cadence.

2. Homer basebackup with smaller payload capacity or metadata-heavy datasets
   Real workload. Smaller `bytes=` creates more basebackup records, which can
   make small gates more likely if the service loop interleaves with producer
   publication.

3. Citus tuple COPY
   Useful as integration/correctness and possibly as a stressor, but do not
   assume it is transport-bound. If row serialization, routing, COPY parsing, or
   insert dominates, scheduler batching effects may be masked end-to-end.
```

For tuple/COPY, if the existing path does not naturally expose small enough
records, a test-only producer-side or stream-geometry knob may cap tuple-view
payload record size or tuple batch size. That knob should be documented as
workload shaping, not as part of the baseline scheduler.

#### 8.3.2 Basebackup as a real under-batching/HOL workload

Basebackup is a better real workload than full Citus tuple COPY for many
scheduler effects because it has less per-row database work. The Homer basebackup
sink writes archive/manifest bytes directly into a Homer byte-ring record payload
and submits that record to the service. Large regular files may produce large
records when `bytes=` is large, while tar headers, begin/end records, manifest
records, small files, file tails, and padding produce partial records.

Use `bytes=` as a workload-shape axis, not as proof of under-batching by itself:

```text
bytes=32768 or 65536:
    many smaller records; under-batching is easier to expose if the service
    observes one/few records per visit

bytes=1048576:
    middle point

bytes=8388608:
    large records; good for bulk/HOL grant-size experiments, but natural
    under-batching may be weak because each record is already coarse
```

Important interpretation rule:

```text
If two 32 KiB records are both visible at the visit boundary and the sender
coalesces them into one RDMA publication batch, then reducing `bytes=` did not
create a scheduler under-batching case. It mostly changed semantic record count
and header/validation overhead.

If the service sees one 32 KiB record per visit and a short wait-batch diagnostic
would have gained adjacent records, then the fixed loop is under-batching.
```

Therefore the basebackup experiment must report visit/batch evidence, not just
end-to-end elapsed time.

#### 8.3.3 Forced static under-batching with FixedLoop-Limited

`FixedLoop-Limited` can create or amplify under-batching by setting `maxObjects`,
`maxBytes`, or `maxWrs` small. This is useful as a static latency-biased ablation:

```text
FixedLoop-Limited(N=1):  very small payload grants, likely poor batching
FixedLoop-Limited(N=4):  moderate batching
FixedLoop-Limited(N=16): closer to bulk throughput, higher HOL risk
```

This mode should be presented as a tradeoff curve, not as the only proof that the
primary baseline under-batches. It demonstrates that a static hyperparameter is
hard to tune:

```text
small N:
    lower HOL risk and potentially better foreground latency
    worse batching and bulk throughput

large N:
    better batching and bulk throughput
    higher service-loop / QP-ordering HOL risk
```

The guarded-action scheduler should replace this static compromise by choosing a
payload posting grant from the current foreground pressure, batching opportunity,
resource pressure, and age/debt.

Limited-mode caps are configuration constraints, not adaptive flow-control
policy. The implementation should fail loudly on nonsensical cap values such as
zero objects, zero bytes, or zero WRs. For geometry-dependent constraints, the
preferred behavior is to detect an impossible configuration at stream open or on
the first stream visit and emit a clear error that includes the cap values and
stream geometry. If a valid in-progress fragment or record would otherwise make
no forward progress, the implementation may allow exactly one minimal progress
unit and record that as a forced-progress guard. That guard is a correctness
escape hatch for badly chosen static caps; it is not a dynamic scheduling rule
and should not be used to tune performance.

#### 8.3.4 WaitBatch diagnostic to show recoverable batching opportunity

If the branch has diagnostic knobs equivalent to:

```text
HOMER_SERVICE_PAYLOAD_READY_SPIN_LIMIT
HOMER_SERVICE_PAYLOAD_READY_SPIN_TARGET
```

keep them disabled for FixedLoop-ExhaustivePayload and enable them only in a
WaitBatchDiagnostic run. The diagnostic should:

```text
1. observe at least one ready payload record;
2. wait up to K cpu_relax loops;
3. stop early if M records/bytes are visible;
4. post the gated visible batch;
5. record how many objects were gained by waiting.
```

These knobs are not how we create the baseline. They are how we show that a small
amount of waiting could have improved batching. In a mixed foreground/bulk run,
the same wait may hurt foreground tail latency, which is why the real scheduler
should make this decision dynamically.

#### 8.3.5 Required evidence for under-batching

Do not claim under-batching from small `bytes=` or small `N` alone. Report the
mechanism:

```text
payloadSenderBatches
payloadObjectsPosted
payloadBytesPosted
payloadWrsPosted
payloadBatchSizeHistogram[1,2,4,8,16,...]
payloadBytesPerBatch
payloadObjectsPerBatch
payloadBytesPerWr
readySpinAttempts                 only in diagnostic builds
readySpinLoops                    only in diagnostic builds
readySpinGainedObjects            only in diagnostic builds
```

Expected signatures:

```text
Immediate under-batching:
    batch_hist[1] dominates
    objects_per_batch is low
    bytes_per_WR is low
    sender batches per GiB is high

Recoverable batching opportunity:
    WaitBatchDiagnostic has readySpinGainedObjects > 0
    batch histogram shifts to larger batches
    batches per GiB and/or WRs per GiB decreases
    throughput improves or service CPU falls

No scheduler-visible under-batching:
    small records are already visible together
    coalesced batches are common
    readySpinGainedObjects is near zero
```

### 8.4 Fixed completion/CQ behavior

The baseline should retain static completion-polling behavior such as:

```text
poll payload send completions when required by source release / local resource limits;
use static min-inflight or interval constants;
use signaled checkpoint behavior already present in the branch;
```

It should not decide CQ drain frequency based on a scheduler-visible value model. CQ handling is local to the payload or peer phase.

### 8.5 Receiver ACK behavior

Retain static receiver-head ACK thresholds. Receiver ACK coalescing is a protocol/transport efficiency constant, not an adaptive scheduler policy in the baseline.

## 9. What to keep from later branches

Backport only correctness and compatibility fixes that are independent of scheduler policy.

Acceptable backports:

- protocol layout compatibility fixes;
- byte-ring correctness fixes;
- stale service/client protocol version fixes;
- source lifetime and completion tracking correctness fixes;
- peer-open descriptor correctness fixes;
- crash/timeout fixes not caused by scheduler policy;
- build/install/link sanity fixes;
- low-overhead diagnostic counters.

Avoid backporting:

- ready-set/source-plan scheduler substrate;
- machine/collector/action-plan scheduler;
- known/unknown collector facts;
- dependency-demand boosts;
- CPU/liveness class policy;
- blocked/unblocked-first policy;
- adaptive bounded-class policy;
- egress ready-hints policy;
- pass-wide egress budget;
- scheduler-level aggregate payload source.

If a later correctness fix is entangled with scheduler code, extract the minimal correctness change and keep the fixed-loop baseline surface intact.

## 10. Aggregate payload clarification

The later `homer-better-service-progress-scheduler` branch introduced an “aggregate payload” scheduler source to avoid representing many active payload streams as many top-level ready-set entries. This was an implementation tactic for dense payload workloads: if many streams are ready, use one aggregate payload source and let the executor scan the payload table.

For the fixed-loop baseline, do not import that scheduler mechanism.

The baseline already has an aggregate payload phase by construction:

```text
payload phase = scan the payload stream table in index order
```

So the baseline should be described as a fixed payload table scan, not as scheduler-level aggregate payload. This preserves the naive fixed-loop story.

## 11. Instrumentation requirements

The baseline needs enough counters to explain behavior, but counters must be optional or low overhead.

Recommended counters:

```text
serviceLoopPasses
serviceLoopIdlePasses
phaseCalls[phase]
phaseProductive[phase]
phaseItems[phase]
phaseTimeNs[phase]              optional if timing cost acceptable
peerIdlePollSkipped
peerIdlePollExecuted
peerActivePollExecuted
payloadStreamsVisited
payloadStreamsProductive
payloadObjectsPosted
payloadBytesPosted
payloadWrsPosted
payloadVisitBudgetStops
payloadRemoteCreditStops
payloadLocalResourceStops
payloadVisibleTailStops
completionMailboxesVisited
completionsConsumed
remoteCommandsForwarded
localControlSlotsHandled
```

For payload baseline comparison, log enough to distinguish:

```text
visible work exhausted
limited budget exhausted
remote credit blocked
local WR/resource blocked
completion/CQ release bottleneck
```

For under-batching specifically, collect:

```text
payloadSenderBatches
payloadObjectsPosted
payloadBytesPosted
payloadWrsPosted
payloadBatchSizeHistogram[1,2,4,8,16,...]
payloadBytesPerBatch
payloadObjectsPerBatch
payloadBytesPerWr
readySpinAttempts                 only in diagnostic builds
readySpinLoops                    only in diagnostic builds
readySpinGainedObjects            only in diagnostic builds
```

The expected signature of under-batching is a batch-size histogram dominated by
1-object or very small batches, many sender batches per GiB, and low bytes per WR.
The expected signature of recoverable batching opportunity is that a small
WaitBatchDiagnostic run shifts the histogram toward larger batches and improves
COPY throughput.

Do not add per-object hot-path logging. Prefer aggregate counters printed on service exit or when a test ends.

## 12. Build and configuration interface

Expose baseline mode through compile-time constants. Keep runtime environment
selection out of the first baseline implementation so performance runs do not
pay an extra mode branch in the payload hot path.

Implemented compile-time mode in
`src/backend/distributed/utils/homer/tuple_sink_service_process.c`:

```c
#define HOMER_FIXED_PAYLOAD_EXHAUSTIVE 0U
#define HOMER_FIXED_PAYLOAD_LIMITED    1U

#ifndef HOMER_FIXED_PAYLOAD_MODE
#define HOMER_FIXED_PAYLOAD_MODE HOMER_FIXED_PAYLOAD_EXHAUSTIVE
#endif

#ifndef HOMER_FIXED_PAYLOAD_MAX_STREAMS_PER_PASS
#define HOMER_FIXED_PAYLOAD_MAX_STREAMS_PER_PASS UINT32_MAX
#endif

#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_OBJECTS
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_OBJECTS 1U
#endif

#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_BYTES
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_BYTES (256ULL * 1024ULL)
#endif

#ifndef HOMER_FIXED_PAYLOAD_LIMITED_MAX_WRS
#define HOMER_FIXED_PAYLOAD_LIMITED_MAX_WRS 4U
#endif
```

There is intentionally no runtime override yet:

```text
HOMER_FIXED_PAYLOAD_MODE must be selected at build time through CPPFLAGS.
```

The default mode must be `exhaustive` with spin limit zero. The diagnostic mode
may reuse existing `HOMER_SERVICE_PAYLOAD_READY_SPIN_LIMIT` /
`HOMER_SERVICE_PAYLOAD_READY_SPIN_TARGET` names if they already exist on the
branch, but keep their effect disabled unless the diagnostic mode is selected.

Keep the baseline independent of `HOMER_PROGRESS_POLICY` if starting from `homer-client-sql-basebackup-merge`, because that branch should not have a policy object. If we later need to compare from a branch that has policy code, use:

```text
HOMER_PROGRESS_POLICY=fixed-loop
```

but treat that as compatibility only.

## 13. Validation workloads and experiment strategy

### 13.0 Expected baseline tradeoff surface

The first comparison is baseline-versus-baseline, not
baseline-versus-scheduler. The fixed-loop baselines should be credible static
points that expose the tradeoff surface:

```text
FixedLoop-ExhaustivePayload:
    expected to be the strongest bulk-throughput static point when a payload
    stream has visible backlog, because it amortizes RDMA posting, doorbell, and
    CQ costs across all work visible at stream-visit start.

    expected failure mode:
        foreground command/completion tail latency can suffer in mixed workloads
        because a bulk stream visit may consume a long non-preemptive interval.

FixedLoop-Limited-small:
    expected to improve responsiveness in some mixed workloads because payload
    visits return to the fixed service loop sooner.

    expected failure mode:
        lower bulk throughput and worse batching efficiency due to smaller
        batches, more WRs/CQEs/doorbells per GiB, or more fixed loop overhead.

FixedLoop-Limited-large:
    expected to recover some bulk throughput relative to Limited-small.

    expected failure mode:
        moves back toward ExhaustivePayload and can reintroduce foreground
        tail-latency risk.

FixedLoop-WaitBatchDiagnostic:
    expected to improve batching only when the immediate fixed loop is too eager
    relative to producer publication cadence.

    expected failure mode:
        the same fixed wait can hurt foreground latency in mixed workloads,
        showing that batching delay should be conditional rather than hardcoded.
```

The desired evidence is **not** that every baseline is bad. The stronger result
is that no single static point dominates:

```text
Exhaustive wins or is competitive in bulk-only but hurts mixed foreground tail.
Limited-small improves tail but hurts bulk throughput or batching.
Limited-large recovers bulk but loses some tail benefit.
WaitBatch proves recoverable batching opportunity but is harmful when waiting is
not worth the latency.
```

If one static cap wins across all tested workloads, the scheduler motivation is
weak for that workload set. Increase stress before drawing conclusions: use more
foreground clients, larger or multiple bulk streams, smaller basebackup `bytes=`,
more active payload streams, producer pacing/microbenchmarks, or tuple/COPY only
if instrumentation shows transport posting is material. If ExhaustivePayload is
poor even in bulk-only, first suspect implementation, transport, batching, CQ, or
measurement problems rather than treating that as scheduler motivation.

### 13.1 Correctness validation

Minimum correctness validation:

```text
1. remote Homer pgbench c1/j1
2. remote Homer pgbench c4/j4
3. remote RDMA basebackup
4. backend-to-backend Citus tuple COPY through Homer, if supported on this branch
5. mixed remote c4 pgbench + remote RDMA basebackup
6. peer setup/reconnect smoke
```

### 13.2 Do not assume full Citus tuple COPY is transport-bound

Full Citus tuple COPY is useful as integration validation, but it may be a weak
end-to-end workload for proving scheduler batching/HOL effects. It can be
dominated by row production, Citus routing, tuple serialization, COPY parsing,
worker-side insert, WAL, and file/heap work. If those dominate, transport
under-batching can be visible in service counters but hidden in total elapsed
time.

Before using tuple COPY as a headline scheduler result, decompose it with Citus,
Postgres, and Homer counters:

```text
Citus/Postgres timing:
    DoCopyFromLocalTableIntoShards_
    CitusSendTupleToPlacements_
    SerializeAndCopyRow_
    SendCopyDataToPlacement_
    Receiver_CopyFrom
    NextCopyFrom_
    CopyFrom_CopyGetData
    CopyFromInsertIntoTable

Homer service stats:
    senderPostedBatches
    senderPostedObjects
    senderPostedBytes
    senderPostedWriteDescriptors
    senderBatchSizeHistogram
    senderCoalescedObjects
    local/remote waits
    service-loop productive vs idle passes
```

Interpretation:

```text
If SerializeAndCopyRow_ / CopyFromInsertIntoTable dominate:
    tuple COPY is DB CPU / ingest dominated; do not expect large end-to-end
    scheduler gains.

If SendCopyDataToPlacement_ / Homer service posting dominates:
    tuple COPY may expose under-batching and HOL.

If service stats show small batches but COPY elapsed time does not improve:
    the scheduler effect exists but COPY is masking it; use basebackup or a
    microbenchmark for the clean performance proof.
```

### 13.3 Recommended experiment ladder

Run experiments in layers. Do not start by assuming one full DB workload will show
every effect.

```text
Layer 1: payload microbenchmark
    Controlled proof of under-batching and grant-size tradeoff.
    Producer cadence and record size are controllable.

Layer 2: basebackup workload
    Real Homer bulk workload with direct byte-ring record production.
    Use `bytes=` as workload-shape axis, but prove under-batching with batch
    histograms and ready-spin-gained counters.

Layer 3: mixed pgbench + basebackup
    Real foreground/bulk interference test.
    Shows why a static bulk grant that helps throughput can hurt foreground tail.

Layer 4: full Citus tuple COPY
    Integration/correctness and possible stress workload.
    Use as performance proof only if instrumentation shows transport/posting is
    material.
```

### 13.4 Baseline and diagnostic modes to run

Minimum performance comparison:

```text
A. FixedLoop-ExhaustivePayload-Immediate
B. FixedLoop-Limited with 2-3 static cap choices
C. FixedLoop-WaitBatchDiagnostic with 2-3 tiny wait/target settings
D. later guarded-action scheduler / state-machine policy branch
```

For under-batching:

```text
A. Immediate:
   ready spin disabled, gated visible work only

B. Limited(N):
   static small/moderate/large N

C. WaitBatch(K,M):
   wait up to K relax loops for M objects/bytes
```

For basebackup specifically, sweep workload shape independently of scheduling
mode:

```text
bytes=32768 or 65536
bytes=1048576
bytes=8388608
```

Use this sweep to ask whether the fixed loop observes small visit-start gates. Do
not claim that smaller `bytes=` is itself under-batching.

For mixed runs:

```text
foreground pgbench + background basebackup
foreground pgbench + payload microbenchmark, if available
foreground pgbench + tuple COPY, only if COPY transport path is material
```

The desired evidence is:

```text
Immediate or small-N Limited:
    small batch histogram, lower bulk throughput, low bytes/WR

WaitBatchDiagnostic:
    larger batches and higher bulk throughput in bulk-only runs
    possible pgbench p99 regression in mixed workload

GuardedAction scheduler later:
    batch when safe, avoid waiting when foreground pressure is high
```

Important measurement rules:

- do not use cold first-run numbers as steady-state evidence;
- always run warmed repeats;
- record service binary hashes and protocol/client binary hashes;
- record payload geometry: slots, bytes, object size, byte-ring vs fixed-slot mode;
- record CPU pinning for service, backends, and client;
- collect p50/p95/p99/p999 for foreground pgbench when available;
- report basebackup/COPY elapsed time separately from foreground latency;
- log whether service stats were enabled.

## 14. Expected outcomes and interpretation

### 14.1 If FixedLoop-ExhaustivePayload is good for basebackup but hurts pgbench tail

This is the expected and useful result. It shows that static exhaustive bulk service amortizes RDMA overhead but can create foreground interference.

### 14.2 If FixedLoop-Limited improves pgbench tail but hurts basebackup

Also expected. It shows the static grant-size tradeoff.

### 14.3 If FixedLoop-ExhaustivePayload has many tiny payload batches

This is the under-batching failure. It means the fixed loop is visiting payload
streams before enough records accumulate, so it pays RDMA fixed costs too often.
Confirm with batch-size histograms, sender batches per GiB, bytes per WR, and
bulk throughput.

This can occur in tuple/COPY, basebackup, or a microbenchmark, but the evidence
must be scheduler-visible batching evidence. Do not infer under-batching merely
from small `bytes=` or small tuple records.

### 14.4 If smaller basebackup `bytes=` does not change batch histograms

Then smaller payload capacity changed workload geometry but did not create a
scheduler-visible under-batching regime. If multiple small records are already
visible together and coalesced at each visit, the fixed loop is not under-batching
at the service scheduling boundary. Use producer pacing, a microbenchmark, or a
more aggressive Limited cap to expose the tradeoff.

### 14.5 If WaitBatchDiagnostic improves bulk throughput

This is useful evidence. It shows the immediate fixed loop left batching
opportunity on the table. If the diagnostic hurts foreground p99 in a mixed run,
that strengthens the scheduler motivation: batching delay is sometimes valuable
and sometimes harmful, so it should be a policy decision.

### 14.6 If one static point works well for all workloads

Then the richer scheduler needs a stronger motivation or more stressful mixed workload. Try higher concurrency, larger bulk streams, more active payload streams, or synthetic foreground injections during bulk transfer.

### 14.7 If FixedLoop-ExhaustivePayload fails correctness

That indicates either a real transport/protocol bug or that “send all visible work” violated an existing resource invariant. Fix correctness, but do not add adaptive scheduling to repair it.

## 15. Staged implementation and verification plan

The goal of the implementation sequence is to catch protocol, transport,
correctness, and measurement issues before the final baseline sweeps. Each stage
has a narrow deliverable and a verification gate. Do not move to performance
claims until the earlier correctness gates are clean.

### Stage -1: Pre-implementation reference measurements

Status on 2026-06-12: done as a ballpark reference set in
`homer_current_reference_measurements_20260612.md`. The COPY-through-Homer row
is not a valid performance baseline because it currently fails with
`remote execution control response kind mismatch`; keep COPY as optional until
that integration bug is debugged.

Before changing payload scheduling semantics, collect a small reference set on
the unmodified `homer-client-sql-basebackup-merge`-derived branch. These numbers
are not the final baseline results; they are ballpark guardrails. Their purpose
is to catch suspicious regressions, setup mistakes, wrong IP paths, stale
process contamination, unexpected protocol mismatch, and wildly different
runtime behavior after the fixed-loop baseline changes land.

Deliverables:

- build and install the current Postgres and Citus/Homer worktrees with the
  runbook's two-pass Homer order: install current Postgres headers/server,
  rebuild and install Citus/Homer against that `pg_config`, then relink and
  reinstall the Postgres binaries that statically link `libhomer_client.a`;
- use performance-style flags and keep verbose hot-path logging disabled;
- record source commits, branch names, build commands, service binary hash,
  `citus.so` hash, `pgbench` hash, and `pg_basebackup` hash;
- record farnet IP/interface state and confirm the two same-lane RDMA host links
  still pass the quick `ib_read_bw` sanity checks if the machines were rebooted
  or renumbered;
- start from a clean runtime process set on both hosts and record the preflight
  output;
- run warmed repeats for the workloads we expect to use later.

Reference workload set:

```text
1. remote Homer pgbench c1/j1
2. remote Homer pgbench c4/j4
3. local Homer basebackup blackhole, if available
4. remote RDMA basebackup with bytes=8388608
5. remote RDMA basebackup with bytes=1048576 or bytes=65536 if runtime permits
6. mixed remote pgbench c1 plus remote RDMA basebackup
7. mixed remote pgbench c4 plus remote RDMA basebackup, diagnostic-only if too slow
8. backend-to-backend Citus tuple COPY through Homer, if supported on this branch
9. vanilla/libpq comparison for the pgbench and COPY shapes we intend to cite
```

Reference evidence to capture:

- full warmed repeat sets, with run 1 treated as warmup unless explicitly
  studying cold setup;
- foreground `pgbench` TPS and latency percentiles, especially p95/p99/p999;
- basebackup or COPY elapsed time separately from foreground latency;
- payload geometry: slots, bytes, object size, byte-ring versus fixed-slot mode;
- CPU pinning for service, socketless backends, and clients;
- whether diagnostic stats were enabled;
- payload batch histograms, bytes per WR, objects per batch, and stop reasons if
  stats are enabled;
- service logs from both hosts and process preflight after each run group.

Interpretation gate:

- do not tune the new baseline implementation to reproduce these numbers
  exactly; use them as sanity bands;
- if a later run is far outside the reference band, first check build hashes,
  runtime process contamination, IP path, CPU pinning, payload geometry, and
  stats/logging flags before attributing the change to scheduling;
- if the unmodified branch already shows suspicious behavior, fix the run setup
  or document the issue before implementing the fixed-loop changes.

### Stage 0: Branch and runbook alignment

Status on 2026-06-12: done for the paired
`homer-unscheduled-baselines` worktrees. `AGENTS.md` now carries the current
farnet IP/RDMA setup and the corrected Homer build/install order.

Deliverables:

- keep paired worktrees on `homer-unscheduled-baselines`;
- carry the updated `AGENTS.md` runbook into the Postgres worktree;
- document that the baseline starts from `homer-client-sql-basebackup-merge`;
- document static transport lanes/QPs and explicitly reject the single-QP
  multiplexing baseline for this study.

Verification gate:

- `git status --short --branch` in both worktrees shows the expected branch;
- `AGENTS.md` contains the current farnet IP/RDMA caveats;
- no scheduler branch code has been merged just to implement the baseline.

### Stage 1: Fixed-loop structure audit

Status on 2026-06-12: done. The branch already has a direct
`TupleSinkServicePumpOnce()` fixed phase order rather than a scheduler
ready-set/source-plan path. The remaining Stage 1 code change was to make the
payload phase explicitly bounded by static compile-time knobs.

Deliverables:

- preserve the direct `TupleSinkServicePumpOnce()` fixed phase order;
- add or update a top-of-function comment that names the fixed phases in order;
- keep local-control async continuations, peer pump internals, completion scans,
  heartbeat, and payload stream table scan as fixed-position phases;
- avoid ready sets, source plans, collector facts, dependency boosts, and
  scheduler-level aggregate payload sources.

Verification gate:

- build both repos without new payload modes enabled;
- run remote Homer pgbench c1 smoke;
- inspect logs for service heartbeat stalls, protocol mismatch, peer reset, and
  stale process contamination.

### Stage 2: Gated exhaustive payload baseline

Status on 2026-06-12: implemented in the service code and smoke-tested with the
default build. Build checks passed for both default exhaustive mode and
`-DHOMER_FIXED_PAYLOAD_MODE=HOMER_FIXED_PAYLOAD_LIMITED`; the installed runtime
was then rebuilt back to the default exhaustive mode. Remote RDMA basebackup
smoke completed with `rc=0` in
`/tmp/homer_fixed_loop_stage1_smoke_20260612_195725`, and remote Homer pgbench
c1 smoke completed `1000/1000` transactions with no failures in
`/tmp/homer_fixed_loop_stage1_pgbench_smoke_20260612_195741`. These are
correctness smokes only, not warmed baseline performance numbers.

Deliverables:

- implement `HOMER_FIXED_PAYLOAD_EXHAUSTIVE` as the default mode;
- snapshot the producer-visible frontier once at the start of each active stream
  visit;
- for byte-ring and fixed-slot senders, post only through that visit-start
  frontier;
- keep existing RDMA credit, send-completion, WR, CQ, ring-space, EOS, and close
  correctness checks;
- keep ready-spin and wait-for-more disabled in the primary baseline.

Verification gate:

- run local basebackup blackhole smoke if available;
- run remote RDMA basebackup smoke;
- run local and remote Homer pgbench c1/c4 smoke;
- confirm payload stats show successful posting and no transport reset;
- confirm a service visit does not chase newly published producer records beyond
  its visit-start frontier.

### Stage 3: Limited static payload mode

Status on 2026-06-12: initial compile-time mode implemented in the service code,
including payload-stats counters for Limited stop reasons:
`limited_object_budget_stops`, `limited_byte_budget_stops`, and
`limited_wr_budget_stops`. Build checks passed for default mode, Limited mode,
payload-stats default mode, and payload-stats Limited mode. Limited runtime
smoke also passed: remote RDMA basebackup completed with `rc=0` in
`/tmp/homer_fixed_loop_limited_smoke_20260612_200202`, and remote Homer pgbench
c1 completed `1000/1000` transactions with no failures in
`/tmp/homer_fixed_loop_limited_pgbench_smoke_20260612_200218`. A Limited remote
Homer pgbench c4 smoke also completed `10000/10000` transactions with no
failures in
`/tmp/homer_fixed_loop_limited_c4_pgbench_smoke_20260612_200442`. The installed
service was rebuilt and restarted back in default exhaustive mode after the
Limited smokes.

Deliverables:

- implement `HOMER_FIXED_PAYLOAD_LIMITED`;
- add static object, byte, WR, and optional pump-iteration caps;
- reject zero or obviously impossible caps with clear diagnostics;
- keep the forced-progress guard for the first object in a visit so a valid
  object is not permanently blocked by a too-small byte cap;
- record budget-stop counters separately from credit/resource-stop counters
  before treating Limited-mode numbers as final.

Verification gate:

- run Limited with a conservative large cap and confirm it behaves like
  Exhaustive for correctness;
- run Limited with small caps on remote RDMA basebackup and confirm it completes
  rather than spinning or leaking stream state; done for the default small caps
  in the smoke above;
- run local and remote Homer pgbench c1/c4 smoke after Limited changes, because
  payload caps must not break foreground SQL command/completion paths; done for
  remote Homer c1/c4 smokes. Local Homer pgbench remains intentionally out of
  the baseline workload set.

### Stage 4: WaitBatch diagnostic mode

Status on 2026-06-12: implemented as
`HOMER_FIXED_PAYLOAD_WAIT_BATCH_DIAGNOSTIC`. The mode is compile-time only,
requires `HOMER_SERVICE_PAYLOAD_READY_SPIN_LIMIT > 0`, keeps wait/spin disabled
for the default Exhaustive and Limited builds, and extends the diagnostic wait
to byte-ring basebackup with `HOMER_SERVICE_BYTE_RING_READY_SPIN_TARGET_BYTES`.
The byte-ring sender records `ready_spin_gained_bytes` under
`HOMER_SERVICE_PAYLOAD_STATS`; fixed-slot payloads keep using
`ready_spin_gained` as an object counter. This remains an ablation, not a
primary baseline.

Deliverables:

- implement `HOMER_FIXED_PAYLOAD_WAIT_BATCH_DIAGNOSTIC` only as an ablation;
- keep wait/spin disabled unless this mode is selected;
- reuse existing fixed-slot ready-spin counters where applicable;
- add equivalent byte-ring wait-batch accounting if basebackup is used to prove
  recoverable batching opportunity;
- record gained objects/bytes and resulting batch histogram shifts.

Verification gate:

- run a small bulk-only diagnostic and confirm `readySpinGainedObjects` or the
  byte-ring equivalent is nonzero only when waiting actually observes new work;
  done for byte-ring with nonzero `ready_spin_gained_bytes` in the Stage 7
  diagnostic below;
- run a mixed pgbench plus basebackup smoke to confirm the diagnostic does not
  break command completion or close/reclaim paths; done for mixed c4 with
  `10000/10000` transactions and no failures;
- do not use this mode as the primary baseline.

### Stage 5: Instrumentation and result hygiene

Status on 2026-06-12: partially implemented. Limited stop counters are recorded
under `HOMER_SERVICE_PAYLOAD_STATS`, and
`homer_fixed_loop_baseline_harness.sh` now records mode label, workload,
commands, process preflight, branch commits, binary hashes, OIDs, target
geometry, and raw logs. The harness was validated with a preflight-only run in
`/tmp/homer_fixed_loop_fixed-loop-exhaustive_preflight_harness-check_20260612_200701`
and a default-mode remote Homer pgbench c1 smoke in
`/tmp/homer_fixed_loop_fixed-loop-exhaustive_pgbench-c1_harness-check_20260612_200720`.
Full warmed sweeps still need to use the harness after the final correctness
matrix is clean.

Deliverables:

- reuse existing `HOMER_SERVICE_PAYLOAD_STATS`,
  `HOMER_CLIENT_BASEBACKUP_STATS`, `HOMER_SERVICE_CLIENT_SQL_STATS`, and peer
  transport stats where possible;
- add only missing fixed-loop counters needed for interpretation, such as
  visit-start frontier, visible-exhausted stops, limited-budget stops, remote
  credit stops, local resource stops, and forced-progress guard hits;
- print aggregate stats on service exit or stream close, not per object;
- record static mode and cap values in each result directory.

Optional compile-time gate:

```c
#ifndef HOMER_FIXED_LOOP_STATS
#define HOMER_FIXED_LOOP_STATS 0
#endif
```

Verification gate:

- run one diagnostic build and verify the expected stats lines are emitted;
- run one performance-shaped build with verbose hot-path logging disabled;
- record binary hashes for service, `citus.so`, `pgbench`, and `pg_basebackup`
  before comparing runs.

### Stage 6: Correctness matrix

Status on 2026-06-12: core matrix smoke passed for both default
FixedLoop-ExhaustivePayload and FixedLoop-Limited. The harness exercised remote
Homer pgbench c1/c4, local Homer basebackup blackhole, remote RDMA basebackup,
and mixed remote pgbench plus remote RDMA basebackup. COPY-through-Homer remains
outside the core matrix because this branch currently fails that workload with
`remote execution control response kind mismatch`; that is a Citus remote
execution tuple-sink integration issue, not a blocker for the pgbench/basebackup
baseline experiments.

Default FixedLoop-ExhaustivePayload evidence:

```text
/tmp/homer_fixed_loop_fixed-loop-exhaustive_smoke-core_stage6-default_20260612_200957
    local basebackup blackhole: completed, real 3.90 s
    remote RDMA basebackup:     completed, real 5.31 s
    remote pgbench c1:          1000/1000, 0 failed, 3810 TPS, p95 0.266 ms, p99 0.284 ms
    remote pgbench c4:          10000/10000, 0 failed, 8376 TPS, p95 0.622 ms, p99 0.710 ms

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c1-basebackup_stage6-default_20260612_201014
    mixed basebackup:           completed, real 5.55 s
    remote pgbench c1:          1000/1000, 0 failed, 1626 TPS, p95 1.115 ms, p99 1.752 ms

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c4-basebackup_stage6-default_20260612_201025
    mixed basebackup:           completed, real 4.26 s
    remote pgbench c4:          10000/10000, 0 failed, 4702 TPS, p95 1.225 ms, p99 1.475 ms
```

FixedLoop-Limited evidence:

```text
/tmp/homer_fixed_loop_fixed-loop-limited_smoke-core_stage6-limited_20260612_201138
    local basebackup blackhole: completed, real 3.94 s
    remote RDMA basebackup:     completed, real 5.83 s
    remote pgbench c1:          1000/1000, 0 failed; correctness smoke only because
                                latency max 1690.751 ms distorted average/TPS while
                                p95 0.263 ms and p99 0.330 ms stayed low
    remote pgbench c4:          10000/10000, 0 failed, 8440 TPS, p95 0.619 ms, p99 0.719 ms

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c1-basebackup_stage6-limited_20260612_201159
    mixed basebackup:           completed, real 4.08 s
    remote pgbench c1:          1000/1000, 0 failed, 1762 TPS, p95 0.888 ms, p99 1.558 ms

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c4-basebackup_stage6-limited_20260612_201208
    mixed basebackup:           completed, real 4.24 s
    remote pgbench c4:          10000/10000, 0 failed, 4723 TPS, p95 1.225 ms, p99 1.464 ms
```

The installed runtime was restored to the default exhaustive service binary
after the Limited matrix. Both hosts had matching
`/data/dbcomm/pg-citus/bin/citus_tuple_sink_service` hash
`d97a8cb8828ade18dce583c013684af51cce687e4b4bb783f3a40850f6474c85`, and the
process preflight showed only the intended PostgreSQL postmasters and one Homer
service on each host.

Run these before any final baseline experiment:

```text
1. remote Homer pgbench c1/j1
2. remote Homer pgbench c4/j4
3. local Homer basebackup blackhole, if available
4. remote RDMA basebackup
5. mixed remote pgbench + remote RDMA basebackup smoke
6. backend-to-backend Citus tuple COPY through Homer, diagnostic-only until the
   response-kind mismatch is fixed
7. peer setup/reconnect smoke
```

Acceptance gate:

- no stale `pgbench`, `pg_basebackup`, `walsender`, or `remote exec backend`
  processes before measured runs;
- no command completion timeout, protocol mismatch, payload transport reset, or
  service heartbeat stall;
- basebackup and COPY correctness checks pass where those workloads are used;
- failed, interrupted, or manually cleaned runs are marked diagnostic-only.

### Stage 7: Baseline performance sweeps

Status on 2026-06-12: first default FixedLoop-ExhaustivePayload repeat set
captured with the branch harness. These are the first real Stage 7 baseline
bands for the core pgbench/basebackup workload set. Run 1 is treated as warmup
where it shows cold setup effects; warmed bands below use runs 2-4 unless noted.

Default FixedLoop-ExhaustivePayload artifacts:

```text
/tmp/homer_fixed_loop_fixed-loop-exhaustive_pgbench-c1_stage7-default_20260612_202151
    run 1: cold/outlier max 1692.281 ms; correctness passed but exclude from warmed band
    runs 2-4: 1000/1000 each, 0 failed
              TPS 3535-3786, p95 0.268-0.286 ms, p99 0.294-0.321 ms

/tmp/homer_fixed_loop_fixed-loop-exhaustive_pgbench-c4_stage7-default_20260612_202158
    runs 1-4: 10000/10000 each, 0 failed
              TPS 8491-8557, p95 0.612-0.618 ms, p99 0.703-0.711 ms

/tmp/homer_fixed_loop_fixed-loop-exhaustive_basebackup-local-blackhole_stage7-default_20260612_202344
    run 1: 4.04 s warmup
    runs 2-4: completed, real 3.88-3.91 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_basebackup-rdma_stage7-default_20260612_202207
    run 1: 5.32 s warmup
    runs 2-4: completed, real 4.07-4.11 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c1-basebackup_stage7-default_20260612_202227
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 2097-2215, p95 0.904-1.054 ms, p99 1.396-1.641 ms
              basebackup completed, real 4.18-4.22 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c4-basebackup_stage7-default_20260612_202251
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 4526-4734, p95 1.193-1.253 ms, p99 1.418-1.495 ms
              basebackup completed, real 4.19-4.26 s
```

Limited small-cap artifacts:

```text
/tmp/homer_fixed_loop_fixed-loop-limited_pgbench-c1_stage7-limited-small_20260612_202537
    run 1: cold/outlier max 1693.215 ms; correctness passed but exclude from warmed band
    runs 2-4: 1000/1000 each, 0 failed
              TPS 3513-3771, p95 0.270-0.288 ms, p99 0.311-0.321 ms

/tmp/homer_fixed_loop_fixed-loop-limited_pgbench-c4_stage7-limited-small_20260612_202545
    runs 2-4: 10000/10000 each, 0 failed
              TPS 8412-8480, p95 0.612-0.621 ms, p99 0.710-0.715 ms

/tmp/homer_fixed_loop_fixed-loop-limited_basebackup-local-blackhole_stage7-limited-small_20260612_202553
    runs 2-4: completed, real 3.90-3.97 s

/tmp/homer_fixed_loop_fixed-loop-limited_basebackup-rdma_stage7-limited-small_20260612_202612
    run 1: 5.21 s warmup
    runs 2-4: completed, real 4.03-4.04 s

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c1-basebackup_stage7-limited-small_20260612_202632
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 2156-2224, p95 0.823-0.890 ms, p99 1.202-1.403 ms
              basebackup completed, real 4.14-4.20 s

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c4-basebackup_stage7-limited-small_20260612_202656
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 4602-4747, p95 1.215-1.264 ms, p99 1.480-1.630 ms
              basebackup completed, real 4.23-4.28 s
```

The small Limited cap point is correct but does not materially separate from
Exhaustive for the current `bytes=8388608` basebackup shape. That is still a
useful result: this geometry is not enough by itself to expose a Limited versus
Exhaustive static-grant tradeoff. The next Stage 7 runs should sweep
`BASEBACKUP_TARGET` with smaller `bytes=` values, and should include a
payload-stats build for at least one default and one Limited run so we can see
whether the service is actually hitting Limited object/byte/WR budget stops or
mostly stopping on producer visibility, credit, or local send resources.

Immediate interpretation: the first default baseline already shows the expected
mixed-workload interference. Remote pgbench c1 falls from roughly 3.5-3.8k TPS
alone to roughly 2.1-2.2k TPS under background RDMA basebackup, and p95/p99
increase from sub-0.3 ms to roughly 0.9-1.6 ms. Remote pgbench c4 falls from
roughly 8.5k TPS alone to roughly 4.5-4.7k TPS under background RDMA
basebackup, with p95/p99 around 1.2-1.5 ms. Basebackup elapsed time stays near
the warmed remote-RDMA band during mixed runs. This is useful scheduler
motivation, but do not over-interpret until Limited cap points and, later, the
guarded-action scheduler are measured under more payload shapes and matched
geometry.

The installed runtime was rebuilt and restarted back to the default exhaustive
service after the Limited-small sweep. Both hosts had matching
`/data/dbcomm/pg-citus/bin/citus_tuple_sink_service` hash
`d97a8cb8828ade18dce583c013684af51cce687e4b4bb783f3a40850f6474c85`, and the
process preflight showed only the intended PostgreSQL postmasters and one Homer
service on each host.

Smaller-basebackup-shape artifacts:

```text
Default FixedLoop-ExhaustivePayload

/tmp/homer_fixed_loop_fixed-loop-exhaustive_basebackup-rdma_stage7-default-bytes1m_20260612_202959
    run 1: 6.06 s warmup
    runs 2-4: completed, real 3.81-3.83 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_basebackup-rdma_stage7-default-bytes64k_20260612_203019
    runs 2-4: completed, real 4.09-4.49 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c1-basebackup_stage7-default-bytes64k_20260612_203039
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 2576-3262, p95 0.330-0.411 ms, p99 0.344-0.427 ms
              basebackup completed, real 4.28-4.71 s

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c4-basebackup_stage7-default-bytes64k_20260612_203101
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 6354-6666, p95 0.780-0.821 ms, p99 0.911-0.952 ms
              basebackup completed, real 4.18-4.55 s

Limited small-cap

/tmp/homer_fixed_loop_fixed-loop-limited_basebackup-rdma_stage7-limited-small-bytes1m_20260612_203218
    run 1: 5.36 s warmup
    runs 2-4: completed, real 3.77-3.80 s

/tmp/homer_fixed_loop_fixed-loop-limited_basebackup-rdma_stage7-limited-small-bytes64k_20260612_203238
    runs 2-4: completed, real 4.36-4.43 s

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c1-basebackup_stage7-limited-small-bytes64k_20260612_203258
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 3045-3320, p95 0.322-0.396 ms, p99 0.337-0.418 ms
              basebackup completed, real 4.50-4.52 s

/tmp/homer_fixed_loop_fixed-loop-limited_mixed-pgbench-c4-basebackup_stage7-limited-small-bytes64k_20260612_203321
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 6671-6737, p95 0.766-0.778 ms, p99 0.888-0.906 ms
              basebackup completed, real 4.55-4.57 s

Limited medium-cap
    caps: objects=4, bytes=1048576, wrs=8
    diagnostic binary hash:
        1d7b8b2d331b29bfa67396f561529beb0362dcbbc49c78509a0068c53342f395

/tmp/homer_fixed_loop_fixed-loop-limited-medium_basebackup-rdma_stage7-limited-medium-bytes64k_20260612_205012
    run 1: 6.64 s warmup
    runs 2-4: completed, real 4.47-4.51 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c1-basebackup_stage7-limited-medium-bytes64k_20260612_205034
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 3116-3270, p95 0.329-0.342 ms, p99 0.342-0.356 ms
              basebackup completed, real 4.52-4.53 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c4-basebackup_stage7-limited-medium-bytes64k_20260612_205058
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 6603-6725, p95 0.761-0.784 ms, p99 0.899-0.908 ms
              basebackup completed, real 4.53-4.62 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_basebackup-rdma_stage7-limited-medium-bytes1048576_20260612_205654
    run 1: 5.90 s warmup
    runs 2-4: completed, real 3.81-3.82 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c1-basebackup_stage7-limited-medium-bytes1048576_20260612_205714
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 2208-2345, p95 0.844-0.960 ms, p99 1.146-1.232 ms
              basebackup completed, real 3.87-3.88 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c4-basebackup_stage7-limited-medium-bytes1048576_20260612_205739
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 4445-4680, p95 1.137-1.231 ms, p99 1.346-1.880 ms
              basebackup completed, real 3.91-3.93 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_basebackup-rdma_stage7-limited-medium-bytes8388608_20260612_205757
    runs 1-4: completed, real 4.05-4.16 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c1-basebackup_stage7-limited-medium-bytes8388608_20260612_205816
    runs 1-4: pgbench 10000/10000 each, 0 failed
              TPS 2116-2221, p95 0.847-0.933 ms, p99 1.080-1.456 ms
              basebackup completed, real 4.06-4.24 s

/tmp/homer_fixed_loop_fixed-loop-limited-medium_mixed-pgbench-c4-basebackup_stage7-limited-medium-bytes8388608_20260612_205840
    runs 1-3: pgbench 10000/10000 each, 0 failed
              TPS 4310-4343, p95 1.295-1.321 ms, p99 1.507-1.692 ms
              basebackup completed, real 4.14-4.17 s
    run 4: diagnostic-only; basebackup completed in 4.13 s, but remote pgbench
           and four remote exec backends remained stuck until manually killed.
           The run was discarded and the services were restarted before the next
           mode.

Limited large-cap
    caps: objects=16, bytes=4194304, wrs=32
    diagnostic binary hash:
        775eb3e52be9928a70bfda8fe27171f469d7b5d8cce44831c8d40395312a3d22

/tmp/homer_fixed_loop_fixed-loop-limited-large_basebackup-rdma_stage7-limited-large-bytes64k_20260612_205218
    run 1: 5.81 s warmup
    runs 2-4: completed, real 4.47-4.48 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c1-basebackup_stage7-limited-large-bytes64k_20260612_205240
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 3102-3185, p95 0.337-0.344 ms, p99 0.351-0.357 ms
              basebackup completed, real 4.17-4.53 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c4-basebackup_stage7-limited-large-bytes64k_20260612_205303
    runs 2-4: pgbench 10000/10000 each, 0 failed
              TPS 6579-6678, p95 0.779-0.798 ms, p99 0.909-0.931 ms
              basebackup completed, real 4.55-4.58 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_basebackup-rdma_stage7-limited-large-bytes1048576_20260612_210323
    run 1: 5.08 s warmup
    runs 2-3: completed, real 3.81 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c1-basebackup_stage7-limited-large-bytes1048576_20260612_210338
    runs 1-3: pgbench 10000/10000 each, 0 failed
              TPS 2240-2392, p95 0.261-0.900 ms, p99 0.274-1.193 ms
              basebackup completed, real 3.83-3.87 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c4-basebackup_stage7-limited-large-bytes1048576_20260612_210357
    runs 1-3: pgbench 10000/10000 each, 0 failed
              TPS 4478-4535, p95 1.187-1.198 ms, p99 1.535-1.586 ms
              basebackup completed, real 3.90-3.91 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_basebackup-rdma_stage7-limited-large-bytes8388608_20260612_210411
    runs 1-3: completed, real 4.05-4.08 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c1-basebackup_stage7-limited-large-bytes8388608_20260612_210425
    runs 1-3: pgbench 10000/10000 each, 0 failed
              TPS 2116-2220, p95 0.896-1.089 ms, p99 1.336-1.680 ms
              basebackup completed, real 4.16-4.18 s

/tmp/homer_fixed_loop_fixed-loop-limited-large_mixed-pgbench-c4-basebackup_stage7-limited-large-bytes8388608_20260612_210443
    runs 1-3: pgbench 10000/10000 each, 0 failed
              TPS 4489-4759, p95 1.208-1.318 ms, p99 1.456-2.089 ms
              basebackup completed, real 4.24-4.29 s
```

Smaller `bytes=65536` changes the failure shape. Compared with `bytes=8388608`,
foreground pgbench suffers much less under mixed load, because the bulk stream
naturally yields at much smaller payload records. The cost is bulk-side
variability and lower basebackup efficiency. Limited-small improves or
stabilizes foreground pgbench slightly on the `bytes=65536` mixed runs, but all
Limited cap points keep basebackup in roughly the slower `4.5 s` band. Medium
and large are effectively indistinguishable for this small-record shape, which
means the workload is already dominated by producer-visible record cadence and
per-record transport overhead before those larger caps matter.

At `bytes=1048576`, medium and large again overlap closely: warmed bulk-only
basebackup is about `3.81 s`, mixed c1 is about `2.2-2.4k TPS`, and mixed c4 is
about `4.4-4.7k TPS`. At `bytes=8388608`, medium and large recover bulk-only
basebackup to about `4.05-4.16 s`, while mixed foreground performance remains in
the same interference band as Exhaustive and Limited-small. That means the static
cap curve is not producing a single dominant point; larger caps recover bulk-side
efficiency, but they do not solve mixed foreground interference. The medium
`bytes=8388608` c4 stuck fourth repeat is also useful guardrail evidence: a
static loop can leave client/remote-backend cleanup fragile under repeated mixed
runs, so timeout/cleanup handling is required before using that shape as an
automated acceptance test.

Payload-stats diagnostics:

```text
/tmp/homer_fixed_loop_fixed-loop-exhaustive-stats_basebackup-rdma_stage7-stats-default-bytes64k_20260612_203443
    diagnostic-only binary hash:
        88020f8f014f66da0a0588bc5d2adfee9f655aef44256c0d5aa20ad3069af837
    run 2 elapsed: 4.46 s
    sender side, two runs:
        sender_batches 360678 / 360675
        sender_objects 360758 / 360758
        sender_bytes about 23.49 GB
        limited_*_budget_stops all zero
        batch_hist[1] 360644 / 360641
        batch_hist[2] 25 / 24
        batch_hist[8] 4 / 4

/tmp/homer_fixed_loop_fixed-loop-limited-stats_basebackup-rdma_stage7-stats-limited-bytes64k_20260612_203520
    diagnostic-only binary hash:
        917240310e4f35830ac9405957765ebf036a9d6ec1c9545ae9c1d002b2bd5ce4
    run 2 elapsed: 4.49 s
    sender side, two runs:
        sender_batches 360758 / 360758
        sender_objects 360758 / 360758
        sender_bytes about 23.49 GB
        limited_object_budget_stops 123 / 125
        limited_byte_budget_stops 0 / 0
        limited_wr_budget_stops 0 / 0
        batch_hist[1] 360758 / 360758
```

The stats confirm that `bytes=65536` already exposes natural under-batching in
the default exhaustive mode: nearly every visit posts a single object because
that is all the producer has made visible at the visit gate. Limited-small makes
that behavior absolute and records object-budget stops, but only around 125
times across roughly 360k objects. That means this particular workload is driven
more by producer-visible frontier cadence and object size than by a large
backlog that Limited repeatedly chops down. A WaitBatch diagnostic or a
producer-cadence microbenchmark would be the cleaner way to prove recoverable
batching opportunity; the current real workload already proves the static
latency-vs-bulk tradeoff.

WaitBatch diagnostic:

```text
/tmp/homer_fixed_loop_fixed-loop-waitbatch-stats_basebackup-rdma_stage7-waitbatch-bytes64k_20260612_204040
/tmp/homer_fixed_loop_fixed-loop-waitbatch-stats_mixed-pgbench-c4-basebackup_stage7-waitbatch-bytes64k_20260612_204051
    diagnostic-only binary hash:
        a312765529c19fabc12c0b0ca73d1f0e9bf1ccd581977e152143a09d0d6d1bec
    bulk-only basebackup:
        run 1: 5.67 s warmup
        run 2: 4.11 s
    mixed c4:
        run 1: cold/outlier max 1694.417 ms; correctness passed
        run 2: pgbench 10000/10000, 0 failed, 6253 TPS, p95 0.835 ms, p99 0.979 ms
               basebackup completed, real 4.15 s
    service logs:
        copied after both WaitBatch diagnostic commands, so they are cumulative
        for that diagnostic binary. The basebackup-only streams show small byte
        gains, for example ready_spin_gained_bytes 1.2-2.5 MB across about 360k
        ready-spin attempts. The mixed run's basebackup stream shows a larger
        gain, ready_spin_gained_bytes 113.7 MB, with sender_batches 349777 for
        360774 objects and batch_hist[2]=8238, batch_hist[4]=104, batch_hist[8]=8.
```

WaitBatch confirms that recoverable near-ready batching exists, but it is modest
for the bulk-only `bytes=65536` shape and comes from many fixed spin attempts.
The mixed diagnostic also shows why this should remain an ablation: fixed-slot
tuple/result streams reported ready-spin attempts too under this branch's
traffic-class assignment, so a hardcoded wait can spend cycles in places that a
real scheduler should decide about explicitly.

Non-stats WaitBatch spin-limit performance sweep:

```text
Common setup:
    workload: mixed c4 pgbench plus one RDMA basebackup
    basebackup target:
        homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=65536
    ready-spin byte target: 262144
    runs per point: 3
    run 1: excluded as cold/setup warmup for every point

/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c4-basebackup_waitbatch-sweep-limit0-target256k-bytes64k_20260612_231749
    service hash:
        14188b7d91e2a9d4247cb3387d1f910b3a1e988a517ca5fa34475db4a70854f2
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6677-6877, p95 0.755-0.782 ms, p99 0.868-0.915 ms
        basebackup real 4.54-4.57 s

/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit8-target256k_mixed-pgbench-c4-basebackup_waitbatch-sweep-limit8-target256k-bytes64k_20260612_231816
    service hash:
        860ab4ea6ead479ba04ec2cbc90979d193f3fcce5e311299103036dee37ab2da
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6707-6776, p95 0.763-0.781 ms, p99 0.896-0.909 ms
        basebackup real 4.57-4.58 s

/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit32-target256k_mixed-pgbench-c4-basebackup_waitbatch-sweep-limit32-target256k-bytes64k_20260612_231842
    service hash:
        288525710f44e96f1dc6ab97ca53898d4bdf9614f849ab7861653b7f35dab189
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6681-6687, p95 0.780-0.782 ms, p99 0.899-0.903 ms
        basebackup real 4.56-4.57 s

/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit128-target256k_mixed-pgbench-c4-basebackup_waitbatch-sweep-limit128-target256k-bytes64k_20260612_231909
    service hash:
        69eb7e2754d0412d2f61fe96555a6494e4ab9a126f10219fe4137274f36f2dff
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6307-6448, p95 0.815-0.825 ms, p99 0.937-0.952 ms
        basebackup real 4.17-4.55 s

/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit512-target256k_mixed-pgbench-c4-basebackup_waitbatch-sweep-limit512-target256k-bytes64k_20260612_231935
    service hash:
        ead2e8f49d31c9d842db86c72f1f82ea5ae3bf208292d2293de019040dbd56da
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6120-6128, p95 0.845-0.846 ms, p99 0.976-0.982 ms
        basebackup real 4.42-4.58 s
```

The non-stats sweep supports the advisor's narrower paper framing. WaitBatch is
not a standalone "better baseline": tiny spin windows are roughly neutral here,
and a large fixed spin window clearly spends service time in the wrong place for
foreground pgbench. Limit 512 loses about 8-11% foreground TPS relative to the
default warmed range and pushes p99 from about 0.87-0.92 ms to about
0.98 ms. Basebackup does not receive a reliable warmed improvement; the one
4.17 s run at limit 128 is not enough to claim a stable bulk win.

This is enough for a Figure 3b-style tradeoff chart if the caption is careful:
near-ready batching exists, fixed waiting only partially recovers it, and the
post-now versus wait/defer choice should be a scheduler decision. It is not
enough to claim "too much wait makes both foreground and background worse." To
support that stronger claim, run a longer or more stressed second sweep, for
example `ready_spin_limit={0,128,512,2048}` crossed with
`ready_spin_target_bytes={262144,1048576}`, a longer basebackup source, or two
concurrent basebackup streams so background elapsed time is less dominated by
short-run variance.

Aggressive "too much wait" diagnostic attempt:

```text
Control:
/tmp/homer_fixed_loop_fixed-loop-exhaustive_mixed-pgbench-c4-basebackup_waitbatch-too-much-control-limit0-target1m-bytes64k_20260612_232815
    service hash:
        9c2910e4f7883022bca51c97153223527eda4588159f9b3be9e08a976f98f91f
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6377-6390, p95 0.820-0.826 ms, p99 0.958-0.973 ms
        basebackup real 4.15-4.18 s

All-stream WaitBatch, ready_spin_limit=1024, byte target=1 MiB:
/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit1024-target1m_mixed-pgbench-c4-basebackup_waitbatch-too-much-limit1024-target1m-bytes64k_20260612_233021
    run 1 completed but was badly degraded:
        TPS 2748, p95 0.978 ms, p99 1.125 ms, basebackup real 8.27 s
    run 2 failed before a complete measurement:
        tuple result byte-ring record header mismatch at byte_head=80
        pgbench processed only 2500/10000 transactions before aborting
        basebackup completed in 4.13 s

All-stream WaitBatch, ready_spin_limit=4096, byte target=1 MiB:
/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit4096-target1m_mixed-pgbench-c4-basebackup_waitbatch-too-much-limit4096-target1m-bytes64k_20260612_232841
    run 1 completed but was badly degraded:
        TPS 2142, p95 1.455 ms, p99 1.665 ms, basebackup real 13.68 s
    run 2 failed before a complete measurement:
        tuple result byte-ring record header mismatch at byte_head=80
        command-completion mismatches during abort/close
        pgbench processed 0/10000 transactions before aborting
        basebackup completed in 8.92 s

Byte-ring-only WaitBatch attempt, ready_spin_limit=4096, byte target=1 MiB,
with `HOMER_SERVICE_PAYLOAD_READY_SPIN_TARGET=1` to keep fixed-slot payload
streams out of the WaitBatch branch:
/tmp/homer_fixed_loop_fixed-loop-waitbatch-byteonly-limit4096-target1m_mixed-pgbench-c4-basebackup_waitbatch-too-much-byteonly-limit4096-target1m-bytes64k_20260612_233126
    run 1 completed but was badly degraded:
        TPS 2140, p95 1.466 ms, p99 1.668 ms, basebackup real 14.17 s
    run 2 failed before a complete measurement:
        tuple result byte-ring record header mismatch at byte_head=80
        command-completion mismatches during abort/close
        pgbench processed 0/10000 transactions before aborting
        basebackup completed in 8.99 s
```

These aggressive points should stay diagnostic-only, not plotted as clean
performance data. They do answer the qualitative question: yes, making the wait
large enough creates an obvious bad regime, but in the current prototype it hits
foreground protocol/correctness instability before producing a stable warmed
performance band. That instability is itself consistent with the scheduler
motivation: a fixed service-loop wait can delay unrelated foreground result and
completion progress so badly that lifecycle assumptions break. For paper text,
use the correctness-preserving spin-limit sweep above as the main Figure 3b
evidence, and mention this aggressive attempt only if we want a footnote-style
guardrail that unbounded fixed waits are unsafe in this prototype.

June 13 correction: the foreground instability above was a real tuple-result
byte-ring ordering bug, not acceptable WaitBatch behavior. The Citus/Homer
service now gates tuple-result byte posting on command-specific READY
preparation and restricts the WaitBatch byte-ring wait to basebackup/bulk
streams. After that fix, aggressive WaitBatch runs complete correctly and can be
used as diagnostic performance evidence.

Correctness fix validation, small WaitBatch sanity:

```text
/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit128-target256k-fixed_mixed-pgbench-c4-basebackup_waitbatch-fixed-limit128-target256k-bytes64k_20260613_022017
    ready_spin_limit: 128
    ready_spin_target_bytes: 262144
    basebackup target:
        homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=65536
    run 1: warmup after service restart
        pgbench 10000/10000, 0 failed, 3033 TPS, p99 0.950 ms
        basebackup completed, real 8.37 s
    warmed runs 2-3:
        pgbench 10000/10000 each, 0 failed
        TPS 6301-6381, p99 0.944-0.976 ms
        basebackup real 4.11-4.14 s
```

This reproduces the earlier known-good WaitBatch band and shows the correctness
fix did not poison the ordinary small-wait diagnostic point.

Extreme "too much wait" after correctness fix:

```text
/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit65536-target8m-fixed_mixed-pgbench-c4-basebackup_waitbatch-fixed-limit65536-target8m-bytes64k_20260613_021623
    ready_spin_limit: 65536
    ready_spin_target_bytes: 8388608
    basebackup target:
        homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=65536
    runs 1-2:
        pgbench 10000/10000 each, 0 failed
        TPS 414-443, p99 13.089-13.228 ms
        basebackup completed, real 92.68-96.59 s
```

This is correct but too extreme for the main paper figure. It proves that a
large fixed wait can make both foreground and background much worse, but the
numbers are so large that they risk looking like an artificial failure instead
of a useful scheduler-motivation tradeoff.

Middle-ground "too much wait" point after correctness fix:

```text
/tmp/homer_fixed_loop_fixed-loop-waitbatch-limit16384-target8388608-tune_mixed-pgbench-c4-basebackup_waitbatch-tune-limit16384-target8388608-bytes64k_20260613_022714
    ready_spin_limit: 16384
    ready_spin_target_bytes: 8388608
    basebackup target:
        homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=65536
    runs 1-2:
        pgbench 10000/10000 each, 0 failed
        TPS 1268-1634, p99 3.392-3.513 ms
        basebackup completed, real 25.02-28.79 s
```

This is the cleaner diagnostic point for the "too much wait" story. Compared
with the small-wait warmed band above, foreground throughput falls from about
`6.3k TPS` to the `1.3k-1.6k TPS` range, p99 grows from about `0.95 ms` to
about `3.5 ms`, and basebackup grows from about `4.1 s` to `25-29 s`. The
result is still intentionally bad, but it is not the 90-second extreme. The
paper framing should remain modest: hardcoded waiting can expose recoverable
near-ready batching, but once the wait is too large it burns service cycles and
delays unrelated foreground progress. A scheduler should decide when to post
now, wait briefly, or defer based on current competing work rather than baking
one static wait into every bulk payload visit.

After the WaitBatch and full medium/large Limited-cap geometry diagnostics, the
installed runtime was rebuilt, synced to `farnet0`, and restarted back to the
default non-stats exhaustive service. Both hosts had matching
`/data/dbcomm/pg-citus/bin/citus_tuple_sink_service` hash
`14188b7d91e2a9d4247cb3387d1f910b3a1e988a517ca5fa34475db4a70854f2`. The final
preflight artifact is
`/tmp/homer_fixed_loop_fixed-loop-exhaustive_preflight_final-after-full-geometry-sweep-restore_20260612_210547`;
it showed only the intended PostgreSQL postmasters and one Homer service on each
host.

After the later WaitBatch spin-limit sweep, the Citus/Homer service was rebuilt
once with WaitBatch to verify the compile-warning cleanup, then rebuilt with
default `CPPFLAGS='-D_GNU_SOURCE'`, installed, synced to `farnet0`, and restarted
on both hosts. Both hosts then had matching default-service hash
`9c2910e4f7883022bca51c97153223527eda4588159f9b3be9e08a976f98f91f`; process
preflight showed only the intended PostgreSQL postmaster and one Homer service
on each host. The later aggressive "too much wait" diagnostic attempt was also
restored to the same default-service hash on both hosts.

Run warmed repeats only after Stage 6 is clean. Compare each result first
against the Stage -1 reference band to catch fishy numbers before drawing
baseline-versus-scheduler conclusions.

Bulk-only basebackup:

```text
A. FixedLoop-ExhaustivePayload-Immediate
B. FixedLoop-Limited with small, medium, and large static caps
C. FixedLoop-WaitBatchDiagnostic with small wait/target settings
```

Sweep basebackup shape independently:

```text
bytes=65536
bytes=1048576
bytes=8388608
```

Mixed foreground/bulk:

```text
foreground remote pgbench c1/c4 + background remote RDMA basebackup
```

Tuple/COPY:

```text
backend-to-backend Citus tuple COPY through Homer, only after the current
remote-execution control response-kind mismatch is debugged
```

Use tuple/COPY as a performance proof only if service and database counters show
that transport posting/batching is material. Otherwise treat it as integration
and correctness evidence.

Required interpretation artifacts:

- warmed repeat set, not only best run;
- mode and cap values;
- payload geometry: slots, bytes, object size, byte-ring versus fixed-slot;
- CPU pinning for service, backends, and clients;
- p50/p95/p99/p999 for foreground pgbench where available;
- basebackup/COPY elapsed time separate from foreground latency;
- payload batch histograms, bytes per WR, objects per batch, and stop reasons;
- whether diagnostic stats were enabled.

### Stage 8: Later scheduler comparison

After the fixed-loop baseline results are stable, repeat the same workload
geometry, CPU placement, warmup rules, binary-hash recording, and process
preflight against the later guarded-action/state-machine scheduler branch.
Compare only matched runs:

```text
FixedLoop-ExhaustivePayload-Immediate
FixedLoop-Limited static cap points
FixedLoop-WaitBatchDiagnostic ablation
Guarded-action/state-machine scheduler
```

The scheduler result is meaningful only if it improves the static tradeoff:
preserve or recover bulk batching when safe, while avoiding unnecessary
foreground tail-latency damage under mixed load.

## 16. Paper-facing summary

Use this wording:

> FixedLoop is our scheduling baseline for Homer. It uses the same Homer RDMA communication substrate but drives progress with a static service loop. The loop visits command, control, peer, completion, payload, and maintenance phases in a fixed order. External arrivals are discovered by fixed periodic polling. Payload streams use a static gated-exhaustive discipline: when a stream is visited, the service sends all work visible at the start of the visit, subject to fixed resource caps. FixedLoop-Limited uses the same loop but caps each payload visit with static byte/object/WR limits. These baselines are correct and tunable, but they cannot adapt to foreground/bulk interference, dependency-unblocking work, or changing observation value.

Also make clear what the baseline is not:

> FixedLoop is not a single-QP multiplexing experiment. It keeps the existing static RDMA lane/QP separation so control, foreground payload, bulk payload, and maintenance traffic do not all share one QP ordering domain. The absence of explicit scheduling refers to the service-progress loop and payload-grant decisions: the loop does not build ready/action facts, rank work by traffic mix, or adapt grants from foreground/bulk pressure.

Also include the under-batching motivation:

> FixedLoop also exposes the opposite failure mode: when the service loop visits a payload stream too eagerly, only a few records may be visible at each gated visit. The baseline then posts many tiny RDMA publications and loses throughput. This can be studied with a controlled microbenchmark, with basebackup using `bytes=` as a workload-shape axis, or with tuple/COPY if instrumentation shows the transport path is material. A small wait-batch diagnostic can recover batching in bulk-only runs but may hurt foreground latency under mixed load, showing that batching delay must be a policy decision rather than a fixed rule.

Then contrast with the future scheduler:

> The guarded-action scheduler replaces the fixed phase loop with a bounded action/grant plan. It treats polling, CQ drain, command publication, completion publication, RDMA posting, credit return, and close/reclaim as homogeneous action candidates with heterogeneous resource vectors. RDMA posting is a parameterized action: the policy decides whether to post now, defer briefly for batching, or grant a larger/smaller batch based on foreground pressure and resource state.
