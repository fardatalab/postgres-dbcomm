# Homer Service-Progress Control/Data-Plane Scheduler

## Purpose

Record the next scheduler design after the scheduler-ready milestone and W1-W4
tuple-COPY/peer-completion cleanup. The current code has the right source,
grant, result, and egress-grant substrate, but it still rebuilds and immediately
executes a short source plan every service pass. The next design should make the
service-progress scheduler a real control/data-plane split: a policy builds a
bounded execution plan with an intended lifetime, then the service loop executes
that plan until the plan is spent, clearly unproductive, or cheaply invalidated.

This is a future-direction note. It is not a statement that the current default
policy already implements persistent plans.

## Current Grounding

The current service loop is in
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20893).
Each pass builds a ready set with
[`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4901),
turns it into an ordered source plan through
[`HomerServiceBuildProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4194),
then dispatches each source through
[`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20695).

The first-layer source vocabulary is
[`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1242).
It is service/transport-oriented rather than DB-semantic: tuple COPY, basebackup,
and client-SQL result payloads should remain payload streams to this layer.

The first-layer CPU/liveness class is
[`HomerProgressCpuClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1268).
It is separate from RDMA/egress
[`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:52).
The first layer decides how to spend service-loop CPU; the second layer decides
payload egress bytes, objects, fragments, and WRs after a send-capable source is
selected.

Existing per-source execution budget is expressed as typed grants in
[`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1407):
`maxPolls`, `maxItems`, `maxPumpCalls`, and source-specific `flags`. Exact
payload egress budget remains in
[`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3155).

## Decisions

### Persistent Plan, Not Fixed-N Rebuilds

Plan lifetime should be a policy output, not a fixed rule such as "rebuild every
N passes". A policy should choose how much work to encode in a plan based on
current facts, feedback, class mix, and expected head-of-line risk.

When many ready sources/classes exist, the policy may build a larger plan to
amortize decision cost and encode fairness. It should still cap bulk transfer
grants so foreground work is not trapped behind an overly eager all-work plan.
When little work is ready, the policy should build a smaller plan so the service
loop observes new arrivals quickly.

The plan should look like a list plus cursor. Executing the plan is a hot-loop
dispatch over typed grants. That overhead is acceptable for the first design
because dispatch already happens through source-specific executors, and the
point of a persistent plan is to avoid rebuilding policy state every pass. If
later profiling shows switch/function-call overhead matters, optimize the
dispatch representation after measurement rather than weakening the scheduler
model up front.

### Typed Grants Backed By Optional Abstract Tokens

The policy may reason internally in unitless "CPU tokens" as a rough proxy for
cycles, but the stored plan must contain executable typed grants. Different
sources have different natural CPU-work units:

- peer recv-CQ progress: poll batches and CQEs
- peer mailbox progress: fixed-width mailbox messages
- peer send-CQ/response progress: response completions and send CQEs
- local control: control slots
- command rings: commands, source slots, and command WRs
- completion rings: completion entries
- payload streams: first-layer pump calls, then second-layer byte/object/WR
  grants

Do not force these into one physical unit in the executor. A common token model
can help a policy compare sources, but the plan should translate tokens into
source-local limits before execution.

### Cheap Guards Instead Of Whole-Plan Revalidation

Longer-lived plans make stale source references possible even if they are rare
on the current one-pass path. A source reference carries a compact
[`HomerProgressSourceId`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1310)
plus a direct owner pointer in
[`HomerProgressSourceRef`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1327).
Executors must keep validating kind, index, generation, owner, and active state
before touching authoritative state. This is a safety guard for recycled
session/stream slots, not an expected common hot-path race.

The hot path should not validate the entire plan before every iteration. Instead,
each grant validates only its selected source, then returns typed progress in
[`HomerProgressResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1620).
Plan execution can stop early when grant results show the plan is no longer
useful.

### Early-Stop Conditions

The first persistent-plan executor should support cheap early-stop rules:

- plan budget exhausted
- plan cursor reaches the end
- repeated grants produce no progress
- the selected source reports blocked-on-credit or local-resource pressure
- a source that was expected to be hot returns empty polls repeatedly
- a source drains and no longer reports `stillReady`
- a high-priority arrival hint is observed cheaply enough to justify replanning

These rules are deliberately local and feedback-driven. They should not require a
full owner-table rescan or a full fact rebuild on every executed grant.

Early-stop counters belong to the active plan execution state, not to durable
per-source feedback. For example, a plan may track "this plan has produced three
empty grants in a row" and stop following the plan, but that is different from a
source's longer-lived empty-poll score. The former answers "is this plan stale or
unproductive now?" The latter answers "should a future policy grant this source
less often?"

The persistent plan should therefore split the grant list from plan-stop state:

```c
typedef struct HomerProgressPlanStopState
{
    uint16_t consecutiveEmptyGrants;
    uint16_t consecutiveBlockedGrants;
    uint16_t productiveGrantCount;
    uint16_t grantsExecuted;

    uint32_t stopReasonMask;
    uint64_t preemptEpochSnapshot;
} HomerProgressPlanStopState;

typedef struct HomerProgressExecutionPlan
{
    HomerProgressGrant grants[HOMER_PROGRESS_PLAN_MAX_GRANTS];
    uint16_t grantCount;
    uint16_t cursor;

    uint32_t planEpoch;
    uint32_t remainingCpuTokens;

    uint32_t classMask;
    HomerProgressPlanStopState stopState;
} HomerProgressExecutionPlan;
```

`consecutiveEmptyGrants`, `consecutiveBlockedGrants`, and
`productiveGrantCount` are execution-local counters. They are reset when a new
plan is installed. They may be copied into diagnostic stats, but they should not
replace per-source feedback.

Policy-local state should be owned by the selected policy and managed through a
small pluggable interface. The substrate owner defines the interface, storage
lifetime, and baseline policy. A policy implementer owns the contents of that
policy's private state.

The first implementation should keep policy state service-thread-local and avoid
per-pass heap allocation. A union is explicit and cheap enough for current
research policies:

```c
typedef struct HomerProgressPolicy HomerProgressPolicy;

typedef struct HomerProgressPolicyOps
{
    bool (*buildPlan)(HomerProgressPolicy *policy,
                      const HomerProgressReadySet *readySet,
                      HomerProgressExecutionPlan *plan);
    void (*onGrantResult)(HomerProgressPolicy *policy,
                          const HomerProgressGrant *grant,
                          const HomerProgressResult *result);
    void (*onPlanEnd)(HomerProgressPolicy *policy,
                      const HomerProgressExecutionPlan *plan);
} HomerProgressPolicyOps;

typedef struct HomerAdaptiveClassPolicyState
{
    uint32_t sourceEmptyScore[HOMER_PROGRESS_SOURCE_STATE_CAPACITY];
    uint64_t sourceLastProgressTick[HOMER_PROGRESS_SOURCE_STATE_CAPACITY];
    uint32_t sourceLastBlockedReason[HOMER_PROGRESS_SOURCE_STATE_CAPACITY];
    uint16_t sourceLastGrantSize[HOMER_PROGRESS_SOURCE_STATE_CAPACITY];
    uint16_t sourceLastGrantUsed[HOMER_PROGRESS_SOURCE_STATE_CAPACITY];
    uint32_t classDeficit[HOMER_PROGRESS_CPU_CLASS_COUNT];
} HomerAdaptiveClassPolicyState;

struct HomerProgressPolicy
{
    HomerProgressPolicyKind kind;
    const HomerProgressPolicyOps *ops;
    HomerProgressExecutionPlan activePlan;
    union
    {
        HomerFixedPriorityPolicyState fixed;
        HomerRoundRobinPolicyState roundRobin;
        HomerAdaptiveClassPolicyState adaptiveClass;
    } state;
};
```

The exact arrays should follow the existing fixed source registries rather than
introducing a hash table. Policy state is long-lived for the service process and
is updated from `HomerProgressResult` / `HomerProgressSourceFeedback` after each
grant or plan. The active plan owns only one decision window; policy-private
state owns history across many plans.

## Preemption Hints

Plan preemption should be cooperative and cheap. It should happen only at grant
boundaries, not by interrupting a source executor mid-grant.

The first design should support preemption using source-neutral arrival epochs or
bitmasks that are updated only at already-cheap observation points. Candidate
hints:

- local control ready after the service observes a submitted control slot
- command ring non-empty after command publication/frontier movement is observed
- completion ring backlog after completion publication/frontier movement is
  observed
- peer doorbell pending after peer recv-CQ polling records a sink-local doorbell
- close/lifetime pending after a stream/session close flag is observed
- transport-broken/error state

The policy can snapshot a `preemptEpoch` when installing a plan. If a later
grant observes a higher epoch for a higher-priority class than the current plan
is serving, the policy can choose how to react.

Avoid preemption triggers that require rescanning all sessions, streams, or peer
connections on every grant. If a hint is not already maintained by owner state or
not discovered during normal grant execution, it should wait until the next
ordinary planning pass.

The staged design is:

1. V1 uses cooperative stop-and-replan. A policy sees a hint, returns a stop
   decision from a grant-boundary callback, and the generic service loop builds a
   fresh plan through the ordinary planning path.
2. V2 may add constrained in-place plan repair. A policy can insert, trim, or cap
   remaining grants through substrate-owned helper operations instead of directly
   editing the active plan array.

The substrate should therefore expose a preemption/hint callback now, even if the
only supported action in the first implementation is `STOP_AND_REPLAN`:

```c
typedef enum HomerProgressPlanHintAction
{
    HOMER_PROGRESS_PLAN_HINT_IGNORE = 0,
    HOMER_PROGRESS_PLAN_HINT_STOP_AND_REPLAN,
    HOMER_PROGRESS_PLAN_HINT_REPAIR_IN_PLACE
} HomerProgressPlanHintAction;

typedef struct HomerProgressPlanRepairOps
{
    bool (*insertGrantAfterCursor)(HomerProgressExecutionPlan *plan,
                                   HomerProgressGrant grant);
    bool (*trimRemainingClass)(HomerProgressExecutionPlan *plan,
                               HomerProgressCpuClass cpuClass);
    bool (*capRemainingBudget)(HomerProgressExecutionPlan *plan,
                               uint32_t cpuTokens);
    bool (*moveCursor)(HomerProgressExecutionPlan *plan,
                       uint16_t cursor);
} HomerProgressPlanRepairOps;
```

The policy hook can be shaped so V1 ignores `repairOps`, while V2 uses it:

```c
HomerProgressPlanHintAction (*onPlanHint)(
    HomerProgressPolicy *policy,
    const HomerProgressHintState *hintState,
    HomerProgressExecutionPlan *plan,
    const HomerProgressPlanRepairOps *repairOps);
```

In-place repair is an optimization path, not the initial correctness path. If it
lands later, the substrate should enforce these invariants:

- repair only at grant boundaries
- never mutate the grant currently executing
- keep `cursor <= grantCount`
- never exceed fixed grant capacity
- update remaining budget and stop-state through helper operations
- let all inserted grants pass normal executor source/generation validation
- record a repair/preemption reason for diagnostics

This keeps the pluggable-policy surface practical: policy implementers can first
return `STOP_AND_REPLAN`, then later use constrained repair helpers without
owning cursor arithmetic, capacity checks, or executor invariants.

## Peer-Control Split

Peer control is the main remaining first-layer modeling problem. Today the first
layer sees one aggregate `HOMER_PROGRESS_SOURCE_PEER_CONTROL`, while the lower
transport pump already understands phase bits:
[`TupleSinkServicePeerPumpPhaseMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:237)
selects setup, listener CM, recv CQ, send-CQ/response, mailbox, and
close/lifetime work, and
[`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7288)
applies those grants.

The next design should split peer control into first-class service-progress
sources rather than only subgrants under one aggregate source:

- `PEER_CM_SETUP`: cold setup/listener/RDMA-CM transition progress
- `PEER_RECV_CQ`: hot active peer recv-CQ and doorbell polling
- `PEER_MAILBOX`: hot fixed-width peer request dispatch
- `PEER_SEND_CQ`: response/control send-CQ retirement
- `PEER_CLOSE_LIFETIME`: low-volume but non-starvable deferred close and cleanup

This split makes hot/cold separation visible to policy. CQ and mailbox work are
hot while peer sessions or streams are active. Setup/listener work is cold.
Close/lifetime progress should have its own non-starvable class because it is
low throughput value but correctness-visible.

The existing aggregate phase-grant path can remain as a compatibility fallback
or default policy behavior while the split-source facts are introduced.

## Facts, Feedback, And Planner Inputs

Keep current facts and feedback distinct:

- Facts are pre-execution state used to decide what looks runnable now:
  source kind, CPU class, traffic class, ready reason mask, blocked reason mask,
  ready bytes/objects, credit blockage, local resource pressure, and close/reclaim
  flags. Payload source facts are refreshed by
  [`HomerServiceRefreshPayloadProgressSourceFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4789).
- Feedback is what happened the last time the scheduler granted work:
  empty polls, CQEs drained, items processed, WRs posted, bytes posted, frontier
  movement, and ready/blockage hints. The generic feedback structure is
  [`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1369).

The first-layer planner should consume both. Facts answer "what is probably
runnable now?" Feedback answers "was it worth spending CPU here recently?"

The egress layer has its own ready facts in
[`HomerPayloadEgressReadyFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1553)
and builder
[`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3096).
The service-progress layer should not decide exact byte/WR sizing, but it should
use coarse egress facts to avoid wasting CPU. Examples:

- deprioritize a payload stream that is remote-credit blocked
- shrink or skip a source that has local send resource pressure
- avoid repeatedly polling command/completion sources that have returned empty
  for many grants
- avoid immediate re-polling when the only known in-flight command is likely
  still executing in the DB backend
- raise priority again when a doorbell, completion, command publication, or
  close/lifetime reason becomes visible

These examples are policy behavior, not hard-coded scheduler semantics. The
interface should expose enough facts and feedback to let policies experiment.

These "smarter avoidance" behaviors should guide the interface but should not be
baked into the scheduler substrate as fixed semantics. A policy may learn to back
off from predictably empty sources, prefer work that unblocks other work, preserve
productive-source locality briefly, enforce age/fairness guardrails, or delay
completion polling while a DB backend command is likely still executing. The
substrate only needs cheap facts, typed grants, plan-local stop counters, and
durable source feedback rich enough for those policies to be expressed later.

Feedback tracking must stay cheap. The first implementation should prefer
saturating integer counters and existing service ticks over wall-clock time,
histograms, or per-event allocation. Existing fields such as
`emptyPollScore`, `lastEmptyPolls`, `lastCqesDrained`, `lastItemsProcessed`,
`readyObjectCountHint`, `readyByteCountHint`, `lastTouchedTick`, and
`lastProgressTick` in
[`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1369)
are the right starting point. Add fields only when a policy cannot distinguish a
real case with the existing source/result vocabulary.

Additional cheap feedback fields that are likely useful for adaptive policies:

- consecutive empty/productive streak per source
- last blocked reason and the service tick when that reason was observed
- in-flight command start tick or age bucket for command/completion backoff
- last grant size versus work actually consumed
- plan preemption count/reason for diagnostics and over-eager-replan detection

Class-level deficits/credits should stay in policy-private state rather than in
generic feedback, because different policies may define fairness differently.

## Candidate First Policy

The first policy after this design should be an adaptive bounded class plan, not
a full optimization framework.

Inputs:

- ready-set sources and current source cores
- source feedback from recent grants
- CPU/liveness class and traffic class
- ready/blocked reason masks
- coarse ready bytes/objects and credit/resource pressure
- policy state such as class deficits, empty-poll scores, and last productive
  tick

Outputs:

- a persistent execution plan with cursor
- typed grants for each selected source
- total plan work budget
- early-stop thresholds such as max consecutive empty grants or max blocked
  grants

Simple initial behavior:

- prefer hot data and hot control when both are productive
- include close/lifetime grants regularly even if they are low-volume
- include cold setup/maintenance only when due or when no hot work is productive
- cap bulk payload grants so foreground command/result work is not stuck behind
  a large transfer
- shrink sources with repeated empty polls
- skip or delay sources that are credit/resource blocked until feedback or
  facts show progress is possible again

## Implementation Sequence

The implementation should be staged so each slice preserves correctness and has
clear validation:

1. Split peer-control source classes before persistent plans. Add first-class
   progress source refs/facts for setup/listener, recv-CQ, mailbox, send-CQ, and
   close/lifetime while keeping the old aggregate path as a fallback/default.
2. Inventory and normalize typed grant units for every source executor. Ensure no
   source can only be represented by an abstract CPU token.
3. Add `HomerProgressExecutionPlan` and `HomerProgressPlanStopState`, but keep
   the initial behavior equivalent to "build one plan and consume it
   immediately" so the struct/lifetime change is behavior-preserving.
4. Add `HomerProgressPolicyOps` and move existing fixed-priority, round-robin,
   unblocked-first, and cpu-liveness policies behind the pluggable interface.
   Verify correctness and performance before proceeding: this step should be
   interface-only, so any regression means the policy wrapper changed behavior or
   added measurable overhead.
5. Add generic plan execution with cursor, grant-boundary stop-state updates, and
   policy callbacks. Start with `STOP_AND_REPLAN`; keep in-place repair disabled.
   Verify correctness and performance again. This is the first step that changes
   the execution shape, so regressions should be fixed before adding adaptive
   policy logic.
6. Add cheap feedback extensions and preemption/hint diagnostics. Use saturating
   counters and service ticks; avoid heap allocation and wall-clock reads in the
   hot path.
7. Add the first adaptive bounded class policy using the new interface.
   Verify correctness, performance, and mixed-workload behavior before treating
   the policy as a candidate default.
8. Run final validation and tuning for the full milestone. Confirm tuple COPY,
   remote pgbench c1/c4, remote RDMA basebackup, mixed pgbench+basebackup, clean
   process preflight, clean service logs, and matching binaries across hosts.
   Tune plan length, bulk caps, empty/blocked thresholds, class weights, and
   preemption thresholds based on these measurements.
9. Only after measurement shows replanning overhead matters, enable optional
   constrained in-place repair through `HomerProgressPlanRepairOps`.

## Implementation Details To Settle During Code

1. Confirm exact names and array sizing for `HomerProgressExecutionPlan`,
   `HomerProgressPlanStopState`, `HomerProgressPolicyOps`, and the first
   policy-private state structs.
2. Split peer-control sources before introducing a persistent plan. This makes
   hot/cold/close-lifetime classes visible before the policy starts extending
   plan lifetimes.
3. Inventory every CPU work type and map it to a typed grant unit before the
   persistent plan executor lands. The first mapping can use constants, but no
   source should be executable only through an untyped "CPU token".
4. Define the cheap preemption-hint mechanism. The likely design is source-neutral
   class/arrival epochs observed at grant boundaries; V1 should stop and replan,
   while V2 can optionally repair in place through substrate helpers.
5. Add cheap generic feedback fields for empty/productive streaks, last blocked
   reason/tick, command in-flight age, grant utilization, and preemption
   diagnostics; keep class deficits in policy-private state.
6. Treat validation as staged, not only final. At minimum, run correctness and
   performance gates after the policy-interface migration, after persistent-plan
   execution, after the first adaptive policy, and during final tuning. The core
   workload set is tuple COPY, remote pgbench c1/c4, remote RDMA basebackup,
   mixed pgbench+basebackup, clean process preflight, clean service logs, and
   matching binaries across hosts.

## Related

- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md):
  the longer scheduler-ready design history and milestone checkpoint.
- [homer_payload_control_unification_plan.md](homer_payload_control_unification_plan.md):
  payload/control unification plan that motivated neutral source facts.
- [peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md):
  service-to-service peer completion context; the new scheduler should not treat
  normal command completion polling as the hot path.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md):
  RDMA doorbell/visibility constraints that affect readiness facts.
