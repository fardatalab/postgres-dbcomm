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
sources rather than only subgrants under one aggregate source. The split should
also remove the current `send-CQ/response` ambiguity before adaptive policies
consume per-source facts: send-CQ retirement and outgoing async response
mailbox consumption are different CPU/liveness work and should not share one
source identity.

- `PEER_CM_SETUP`: cold setup/listener/RDMA-CM transition progress
- `PEER_RECV_CQ`: hot active peer recv-CQ and doorbell polling
- `PEER_REQUEST_MAILBOX`: hot fixed-width incoming peer request dispatch
- `PEER_RESPONSE_MAILBOX`: outgoing async peer response consumption for local
  control continuations
- `PEER_SEND_CQ`: response/control send-CQ retirement and local resource/lifetime
  release
- `PEER_CLOSE_LIFETIME`: low-volume but non-starvable deferred close and cleanup

This split makes hot/cold separation visible to policy. CQ and mailbox work are
hot while peer sessions or streams are active. Setup/listener work is cold.
Close/lifetime progress should have its own non-starvable class because it is
low throughput value but correctness-visible.

The pre-split code mapped `HOMER_PROGRESS_SOURCE_PEER_SEND_CQ` to a combined
send-CQ/response phase, so one grant both drained tagged send completions and
called `TupleSinkServiceProgressPeerControlResponses()`. That ambiguity needed
to be removed before peer-control facts could safely mean either "locally posted
WRs are expected to retire" or "a local async continuation may now complete".

The existing aggregate phase-grant path can remain as a compatibility fallback
or default policy behavior while the split-source facts are introduced.

Implementation progress: the first peer-control executor split now gives the
flat-source substrate separate request-mailbox, response-mailbox, and send-CQ
identities. [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1288)
now has `HOMER_PROGRESS_SOURCE_PEER_REQUEST_MAILBOX` and
`HOMER_PROGRESS_SOURCE_PEER_RESPONSE_MAILBOX`; the service-progress phase mapper
maps them independently in
[`HomerServicePeerControlPhaseMaskForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7046).
At the transport layer,
[`TupleSinkServicePeerPumpPhaseMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:248)
now has separate `TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_SEND_CQ`,
`TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_REQUEST_MAILBOX`, and
`TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_RESPONSE_MAILBOX` bits.
[`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7547)
drains outgoing send CQEs in the send-CQ branch, consumes outgoing async
peer-control responses in the response-mailbox branch, and dispatches accepted
incoming mailbox work in the request-mailbox branch.

Caveat: this split is clean at the scheduler/action boundary, but the physical
inbound peer-control mailbox is still one FIFO ring that can carry both request
and response messages. The request-mailbox branch therefore still completes a
response if that response is the next FIFO message on an accepted lane; otherwise
it could create head-of-line blocking. A fully pure request-vs-response collector
split would need either separate physical mailboxes or a peekable/deferred
mailbox reader. Do not design baseline policy rules that assume the incoming
request-mailbox action can always ignore response messages without consuming
them.

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

## State-Machine Scheduler Pivot

The flat source-plan model above is useful as an executor dispatch substrate, but
it is not the right conceptual boundary for the first-layer scheduler. Homer
service work is mostly asynchronous state-machine progress: one action makes the
next action expected, or leaves the machine known-blocked on a frontier, CQE,
mailbox response, remote credit, or local resource. Treating command rings,
completion rings, payload streams, peer recv-CQ, peer mailboxes, and send-CQs as
independent `cpuClass` sources loses this ordering information.

The target first-layer scheduler should therefore schedule Homer async work state
machines. Source executors remain the low-level nonblocking action primitives,
but policy input should be active machines plus event collectors, not only a
flat ready set of source refs.

### Generic Model

Keep the implementation fixed-table and switch-based. Do not introduce heap
allocated scheduler work items or per-machine vtables on the hot path.

The generic scheduler objects should be:

- `HomerProgressMachineRef`: machine kind, fixed-table index, generation, and
  owner pointer. This is the state-machine analogue of
  [`HomerProgressSourceRef`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1504).
- `HomerProgressMachineFacts`: compact policy-readable facts: current state,
  ready action mask, blocked reason mask, waiting-on mask, unblocks mask,
  CPU/liveness class, traffic class, age/ticks, ready byte/object hints,
  outstanding WR/CQE hints, and recommended action burst.
  Implementation progress: the fixed-table scaffold now defines
  [`HomerProgressMachineKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1383),
  [`HomerProgressCollectorKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1396),
  [`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1412),
  [`HomerProgressMachineFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1531),
  [`HomerProgressCollectorFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1556),
  and the grant/result scaffolding near
  [`HomerProgressActionRef`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1576).
- `HomerProgressActionRef`: small nonblocking action selected for execution. It
  names the action kind, owner machine, source executor kind if one is reused,
  and typed budgets such as max polls, max messages, max objects, max bytes, or
  max WRs.
- `HomerProgressMachineGrant`: policy output for one machine: advance this
  machine for up to N actions, N polls, N objects, N bytes, or until blocked.
- `HomerProgressActionResult`: executor output: made progress, empty poll,
  blocked reason, terminal/failure, updated frontiers, and any newly unblocked
  machine/event collector.
- `HomerProgressTransitionResult`: machine transition output after an action:
  new state, next ready action, waiting-on source/event, unblocks relation, and
  whether the current plan should stop and replan.

The service loop should separate three phases:

1. **Collect events:** schedule event collectors that discover unknown external
   arrivals or local request publication. Collectors update machine facts and may
   activate machines, but they are not themselves the higher-level async work.
2. **Plan machines:** let the policy choose one or more machine grants using the
   maintained machine facts.
3. **Execute actions:** compile each machine grant into small nonblocking actions
   and dispatch existing source executors or refactored action helpers. Each
   action updates the owning machine facts and may unblock another machine.

Event collectors are scheduled work too. The policy may schedule collector
actions and machine actions in the same execution plan, but they have different
roles: collectors discover or publish facts, while machines represent the
higher-level async work being advanced. Collectors should have policy-owned
cooldowns/backoff, and they should be boosted when one or more active machines
are waiting on the event they collect.

The producer/consumer boundary should be explicit:

- fixed collector registries and maintained facts produce collector candidates,
  not collector grants
- active machine registries and maintained facts produce machine candidates, not
  machine grants
- the policy consumes both candidate sets and produces collector grants plus
  machine grants
- the plan executor consumes grants; collector execution refreshes facts and may
  activate machines, while machine execution transitions higher-level async state
  and may add demand for collectors

This avoids treating collectors as autonomous work items. A collector grant is a
scheduling decision made because the collector is due, has known expected work,
or is demanded by one or more blocked/waiting machines.

### Required Machine Kinds

The initial machine registry should cover exactly these current Homer async work
families. These are local to one service process; a cross-node workflow is made
from cooperating machines on each service, not from one global distributed
machine.

1. `LOCAL_CONTROL_REQUEST_MACHINE`: one active local control slot or async local
   control continuation. It covers local open/start/poll requests, including
   remote command/session/stream operations currently handled by
   [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21460)
   and the async phases in
   [`TupleSinkServiceLocalControlAsyncPhase`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18752).
2. `REMOTE_CLIENT_SQL_SENDER_MACHINE`: frontend-node client SQL command
   transport. It consumes local frontend command mailboxes, posts RDMA command
   writes to the peer, drains command-write CQEs when source slots approach wrap,
   and consumes the pushed client completion mailbox. The current write
   completion state is
   [`TupleSinkServiceClientSqlCommandWriteCompletion`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:839).
3. `COMMAND_SESSION_MACHINE`: one service-side command session, whether local
   backend, peer-command worker, or backend-node client SQL receiver. It covers
   backend command execution, backend completion consumption through
   [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10120),
   result payload dependency, and completion publication through
   [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9054)
   or peer command completion publication.
4. `PAYLOAD_STREAM_MACHINE`: one active neutral payload stream in
   [`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:872).
   It covers outgoing producer-byte/object publication, incoming peer-written
   payload consumption, remote-credit publication, send-CQ/source-release
   frontiers, EOS, and close/reclaim. The current aggregate executor is
   [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17772).
5. `PEER_CONTROL_OP_MACHINE`: one outgoing peer-control request/response
   operation or one incoming request dispatch/response publication. It covers
   start peer request, response mailbox wait, incoming mailbox dispatch, and
   response send-CQ retirement. The current primitives are
   [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:215),
   `TupleSinkServicePollPeerRequestRdma()`, and
   [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6826).
6. `PEER_CONNECTION_MACHINE`: one peer connection/lane setup and lifetime
   machine. It covers listener/CM setup, active recv-CQ polling, send-CQ
   retirement, disconnect, close, and reclaim. The current phase-grant executor is
   [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7288).
7. `MAINTENANCE_MACHINE`: service heartbeat, cold cleanup, diagnostics, and
   non-starvable background maintenance. It should stay separate from hot
   connection/command/payload machines so policy can cap it without losing
   liveness.

### Event Collectors

Collectors kickstart machines and refresh facts for unknown arrivals. They are
scheduled actions, but they do not own the higher-level async state. The initial
collector set should be:

- local control slot collector: finds request-ready control slots and either
  handles simple local work or activates `LOCAL_CONTROL_REQUEST_MACHINE`
- local command/completion mailbox collectors: observe frontend command slots,
  backend completion slots, target completion waits, and peer command completion
  rings
- peer recv-CQ collector: drains peer doorbells/events and marks request mailbox,
  payload, client-command, or connection machines likely runnable
- peer request-mailbox collector: dispatches incoming peer requests and activates
  command/session/payload/control machines as needed
- peer response-mailbox collector: consumes outgoing peer-control responses and
  unblocks local-control or peer-control-op machines
- peer send-CQ collector: retires locally posted peer-control WRs and unblocks
  scratch/lifetime/retry state
- payload producer/consumer frontier collectors: read producer published tails,
  local consumed heads, remote consumed-head mirrors, payload doorbells, and
  payload send/ACK CQEs for active `PAYLOAD_STREAM_MACHINE`s
- heartbeat/maintenance collector: marks maintenance due and updates liveness
  diagnostics

The old ready-set construction is a collector implementation detail in this
model. It should no longer be treated as the first-layer scheduler's main input.

Collector facts need their own scheduler-visible vocabulary so the policy can
avoid both starvation and over-polling:

- `candidateDue`: collector is due by maintenance interval, cold liveness, or
  backoff expiry
- `knownExpectedWork`: local state says the collector should find work, such as
  outstanding WRs, published local request slots, known mailbox head/tail
  movement, pending close/reclaim, or command/source ring wrap pressure
- `waitingMachineCount`: number of active machines waiting on this collector
  family
- `oldestWaitingMachineAge`: age of the oldest machine blocked on this collector
- `lastProductiveTick`, `lastEmptyTick`, and `consecutiveEmptyGrants`
- `lastItemsCollected`, `lastPolls`, `lastMessages`, and `lastCqes`
- `collectorCostClass`: cheap exact check, bounded mailbox/CQ poll, or cold
  setup/maintenance scan

Baseline policy rules for collectors:

- Do not starve collectors. If no machine grants are runnable, schedule due
  collectors before sleeping/spinning on nothing.
- Boost collectors with waiting machines, especially when the oldest waiter age
  grows or the wait is on a known prerequisite such as peer response, send CQ, or
  payload credit.
- Prefer exact/known-expected collectors over blind polling.
- Back off collectors with repeated empty grants when there are enough runnable
  machine grants to spend CPU productively.
- Still reserve a small non-starvation budget for unknown external-arrival
  collectors, because they are the only way to discover new work in a polled
  design.
- Cap collector bursts. A productive collector should make machines eligible and
  then yield to machine advancement unless it has a bounded backlog and the
  policy explicitly grants more collection.

### Initial State/Action Inventory

The first implementation should encode states mechanically from current async
logic, then refactor names later if needed. The following state/action sets are
the implementation baseline.

`LOCAL_CONTROL_REQUEST_MACHINE` states:

- `IDLE`
- `REQUEST_READY`
- command-open phases mirroring
  `TUPLE_SINK_SERVICE_LOCAL_CONTROL_ASYNC_COMMAND_CREATE`,
  `COMMAND_ENSURE_CONNECTION`, `COMMAND_REGISTER_MEMORY`,
  `COMMAND_START_PEER_OPEN`, and `COMMAND_WAIT_PEER_OPEN`
- stream-open phases mirroring `STREAM_CREATE`, `STREAM_ENSURE_CONNECTION`,
  `STREAM_REGISTER_MIRROR`, `STREAM_START_PEER_OPEN`, and
  `STREAM_WAIT_PEER_OPEN`
- start-command phases `START_COMMAND_START_PEER` and
  `START_COMMAND_WAIT_PEER`
- poll-completion phases `POLL_COMPLETION_START_PEER` and
  `POLL_COMPLETION_WAIT_PEER`
- `PUBLISH_LOCAL_RESPONSE`, `COMPLETED`, and `FAILED`

Actions: claim/request slot, allocate session/stream, ensure peer connection,
register memory/mirror, start peer request, consume peer response, publish local
response, fail local response. Transitions are currently implemented across
`TupleSinkServicePumpControlSlots()`,
`TupleSinkServiceProgressCommandOpenAsyncOp()`,
`TupleSinkServiceProgressStreamOpenAsyncOp()`, and
`TupleSinkServiceProgressLocalControlAsyncOp()`.

`REMOTE_CLIENT_SQL_SENDER_MACHINE` states:

- `IDLE_WAIT_FRONTEND_COMMAND`
- `COMMAND_RDMA_POST_READY`
- `COMMAND_RDMA_POSTED`
- `COMMAND_SOURCE_SLOT_WAIT_SEND_CQ`
- `WAIT_CLIENT_COMPLETION_MAILBOX`
- `COMPLETION_READY`
- `CLOSE_REQUESTED`
- `FAILED`

Actions: consume frontend command slot, post command RDMA write plus doorbell,
track write completion, drain command send CQ, poll/consume client completion
mailbox, publish frontend-visible completion, close. This is the frontend-node
counterpart to the backend-node `COMMAND_SESSION_MACHINE`.

`COMMAND_SESSION_MACHINE` states:

- `IDLE`
- `COMMAND_READY`
- `BACKEND_RUNNING`
- `BACKEND_COMPLETION_READY`
- `RESULT_SINK_READY`
- `RESULT_PAYLOAD_ACTIVE`
- `COMPLETION_PUBLISH_READY`
- `COMPLETION_BLOCKED_ON_PAYLOAD_EOS`
- `COMPLETION_BLOCKED_ON_RESULT_SEND_CQ`
- `TERMINAL_REUSE_READY`
- `CLOSE_REQUESTED`
- `FAILED`

Actions: consume command mailbox, start backend command, consume backend
completion mailbox, prepare result stream, advance payload stream, publish local
client completion, publish peer-client completion, publish peer-command
completion, retire/reuse/close session. The key transition to encode explicitly
is the current hidden deferral in
`TupleSinkServicePublishPeerClientCommandCompletion()`: `NOT_READY` should move
to `COMPLETION_BLOCKED_ON_PAYLOAD_EOS` or
`COMPLETION_BLOCKED_ON_RESULT_SEND_CQ` instead of returning a success bool.

`PAYLOAD_STREAM_MACHINE` states:

- `INACTIVE`
- `LOCAL_OPEN`
- `PEER_BINDING_WAIT`
- `OUTGOING_READY`
- `OUTGOING_REMOTE_CREDIT_BLOCKED`
- `OUTGOING_SEND_RESOURCE_BLOCKED`
- `OUTGOING_SEND_CQ_PENDING`
- `OUTGOING_EOS_POSTED_WAIT_SOURCE_RELEASE`
- `INCOMING_DOORBELL_PENDING`
- `INCOMING_HEADER_WAIT`
- `INCOMING_LOCAL_CONSUMER_BLOCKED`
- `INCOMING_ACK_SEND_CQ_PENDING`
- `CLOSE_OR_RECLAIM_PENDING`
- `FAILED`

Actions: build egress facts/grant, pump outgoing byte/object payload, track
payload send completion, drain payload send CQ, consume remote credit doorbell,
consume incoming payload, publish receiver head ACK, drain receiver-head ACK CQ,
append tuple-view EOS record, mark close/reclaim. Current code roots include
`HomerServicePumpOutgoingByteRingPayload()`,
`HomerServicePumpIncomingPayloadStream()`,
`HomerServiceTrackPayloadCompletion()`, and
`HomerServiceApplyTrackedPayloadCompletionFrontier()`.

`PEER_CONTROL_OP_MACHINE` states:

- `IDLE`
- `REQUEST_POST_READY`
- `REQUEST_POSTED`
- `WAIT_RESPONSE_MAILBOX`
- `RESPONSE_READY`
- `RESPONSE_SEND_CQ_PENDING`
- `COMPLETED`
- `FAILED`

Actions: allocate peer op slot, post peer request, poll/consume response
mailbox, dispatch incoming request, publish peer response, drain response send
CQ. This machine is why `PEER_RESPONSE_MAILBOX` must be split from
`PEER_SEND_CQ`: response mailbox consumption advances the async op, while
send-CQ retirement releases transport resources.

Implementation prerequisite: the pre-split peer-control phase ambiguity must
stay removed before using peer-control facts in the state-machine scheduler.
`HOMER_PROGRESS_SOURCE_PEER_SEND_CQ`,
`HOMER_PROGRESS_SOURCE_PEER_REQUEST_MAILBOX`, and
`HOMER_PROGRESS_SOURCE_PEER_RESPONSE_MAILBOX` now map to distinct pump phases in
[`HomerServicePeerControlPhaseMaskForSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7046).
The state-machine scheduler should keep these separate action boundaries:

- `DRAIN_PEER_CONTROL_SEND_CQ`: resource/lifetime release for locally posted
  control/response WRs
- `COLLECT_PEER_RESPONSE_MAILBOX`: response arrival collection that unblocks
  `LOCAL_CONTROL_REQUEST_MACHINE` or `PEER_CONTROL_OP_MACHINE`
- `COLLECT_PEER_REQUEST_MAILBOX`: incoming peer request dispatch, replacing the
  generic `PEER_MAILBOX` meaning

Keep this split intact as the state-machine scheduler lands. Otherwise the
machine facts would reintroduce a special case where one action both releases
send resources and completes async response waits, which would weaken collector
demand accounting and baseline policy decisions.

`PEER_CONNECTION_MACHINE` states:

- `INACTIVE`
- `CM_SETUP_PENDING`
- `LISTENER_EVENT_PENDING`
- `ACTIVE_POLLABLE`
- `RECV_CQ_POLLABLE`
- `REQUEST_MAILBOX_READY`
- `RESPONSE_MAILBOX_READY`
- `SEND_CQ_PENDING`
- `DISCONNECT_PENDING`
- `CLOSE_RECLAIM_PENDING`
- `FAILED_RESET`

Actions: poll listener/CM events, establish outgoing/incoming connection, poll
recv CQ, dispatch request mailbox, consume response mailbox, drain send CQ,
handle disconnect, close/reclaim/reset connection.

`MAINTENANCE_MACHINE` states:

- `IDLE`
- `HEARTBEAT_DUE`
- `CLEANUP_DUE`
- `DIAGNOSTIC_DUE`

Actions: publish heartbeat, run bounded cleanup, run bounded diagnostics.

### Current Executor To Action Mapping

The state-machine scheduler should reuse current source executors only where
they are already bounded, nonblocking, and semantically single-purpose. Any
executor that currently combines collection, machine advancement, dependent
retries, or cleanup should be split early. Compatibility wrapping around
aggregate executors is not a design requirement for this milestone; the goal is a
clean state-machine boundary rather than a new name for the old flat-source
behavior.

Current mappings for the first implementation:

- `COLLECT_LOCAL_CONTROL_SLOTS`: split from
  [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21598).
  This collector scans request-ready control slots and activates
  `LOCAL_CONTROL_REQUEST_MACHINE`s. It should not also drive all active async
  continuations in the same action.
  Implementation progress: local-control grants now have an explicit action mask,
  [`HomerLocalControlProgressActionMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1361),
  with `COLLECT_SLOTS` and `ADVANCE_ASYNC` families. Existing flat local-control
  grants still pass zero flags, which
  [`HomerServiceLocalControlActionMaskForGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17866)
  decodes as both families. Nonzero future machine grants can select only the
  collector or only async continuation advancement.
- `ADVANCE_LOCAL_CONTROL_MACHINE`: refactor from
  [`TupleSinkServicePumpLocalControlAsyncOps()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20240)
  and
  [`TupleSinkServiceProgressLocalControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20032).
  One grant advances a bounded number of async local-control machines by one or a
  small exact-ready burst.
- `COLLECT_FRONTEND_COMMAND_RING` and `POST_REMOTE_CLIENT_SQL_COMMAND`: split
  from
  [`HomerServiceExecuteRemoteClientSqlCommandProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6794).
  The collector observes frontend command slots; the machine action posts the
  RDMA command write/doorbell for a selected `REMOTE_CLIENT_SQL_SENDER_MACHINE`.
- `COLLECT_BACKEND_COMPLETION_RING`: use
  [`TupleSinkServiceConsumeCompletionMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10085)
  as the bounded collector/action for backend completion records. A consumed
  completion transitions the owning `COMMAND_SESSION_MACHINE`.
- `PUBLISH_CLIENT_OR_PEER_COMPLETION`: use
  [`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9054)
  and peer command completion publication as machine actions. The client publish
  helper should return `PUBLISHED`, `NOT_READY`, or `FAILED` so blocked
  completion states become scheduler-visible instead of hidden behind a success
  bool.
- `DRAIN_COMMAND_OR_PAYLOAD_SEND_CQ`: reuse
  [`HomerServiceExecuteCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6661)
  for command-send CQ work, and
  `TupleSinkServicePollPeerPayloadSendCompletionRdma()` /
  `HomerServiceApplyTrackedPayloadCompletionFrontier()` for payload send-CQ
  frontiers. These are collector actions when they refresh facts, and machine
  actions when a selected machine is known-blocked on the CQ frontier.
- `ADVANCE_PAYLOAD_STREAM_MACHINE`: split
  [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17772)
  into smaller actions for outgoing publication, incoming consumption,
  send-CQ/source-release retirement, credit publication, EOS, and close/reclaim.
  Do not keep the aggregate helper as a scheduler-visible action in the target
  design.
  Implementation progress: payload grants now have a payload-specific action
  mask, [`HomerPayloadProgressActionMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1344).
  Zero grant flags preserve the old all-actions behavior through
  [`HomerServicePayloadActionMaskForGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17819),
  while selected grants can already name `LOCAL_BLACKHOLE`, `OUTGOING`,
  `INCOMING`, or `CLOSE_RECLAIM`. Close/reclaim logic is split into
  [`HomerServiceProgressPayloadCloseAndReclaim()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17834),
  and [`HomerServicePumpPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:17924)
  dispatches action families by mask. This is not yet a full payload
  state-machine executor: send-CQ/source-release retirement, receiver-credit
  publication, and EOS synthesis are still partly inside the existing outgoing
  and incoming helpers, with payload send CQEs also visible through the shared
  CQ-drain source.
- `COLLECT_PEER_RECV_CQ_OR_CM`: use the recv-CQ/listener parts of
  [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7288)
  and the underlying peer event drain helper. This collector updates
  `PEER_CONNECTION_MACHINE`, peer request-mailbox, payload, and client-command
  facts.
- `COLLECT_PEER_REQUEST_MAILBOX`: split from the mailbox branch of
  `TupleSinkServicePumpPeerRequestsRdma()` and the underlying incoming mailbox
  pump. It dispatches incoming peer requests and activates local command,
  payload, or peer-control machines.
- `COLLECT_PEER_RESPONSE_MAILBOX`: split from
  [`TupleSinkServiceProgressPeerControlResponses()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:6826).
  It consumes outgoing peer-control responses and unblocks
  `LOCAL_CONTROL_REQUEST_MACHINE` or `PEER_CONTROL_OP_MACHINE`.
- `DRAIN_PEER_CONTROL_SEND_CQ`: use the current
  `TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_SEND_CQ` branch in
  `TupleSinkServicePumpPeerRequestsRdma()`. It drains tagged send CQEs but does
  not also consume response mailbox messages.
- `ADVANCE_PEER_CONTROL_OP_MACHINE`: use
  [`TupleSinkServiceStartPeerRequestRdmaForTrafficClass()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:215)
  and `TupleSinkServicePollPeerRequestRdma()` as machine actions for outgoing
  peer-control ops.
- `ADVANCE_PEER_CONNECTION_MACHINE`: use bounded CM setup, disconnect, close, and
  reclaim pieces from `TupleSinkServicePumpPeerRequestsRdma()`. Split request
  mailbox, response mailbox, recv-CQ/CM, send-CQ, and close/lifetime actions
  before wiring peer-control machines into the scheduler.
- `RUN_MAINTENANCE`: use the existing heartbeat due predicate
  [`HomerServiceHeartbeatDueForPump()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3958)
  plus bounded cleanup/diagnostic work as maintenance actions.

### Baseline Machine-Aware Policy

The first policy should be simple, conservative, and measurable. It should be
called something like `machine-baseline` and should preserve fixed-priority
behavior where the machine facts are not yet strong enough to reorder safely.

Policy inputs:

- machine candidates with `readyActionMask`, `blockedReasonMask`,
  `waitingOnMask`, `unblocksMask`, age, traffic class, CPU/liveness class, and
  byte/object/poll hints
- collector candidates with due state, known expected work, waiting machine
  count, oldest waiter age, empty/productive history, and collector cost class
- per-plan fixed budgets: max collector grants, max machine grants, max total
  actions, max payload bytes/objects, and max blind polls

Plan construction:

- choose multiple machine grants per plan, not necessarily one machine
- first include exact/known-expected collectors that unblock waiting foreground
  machines, then collectors that are due for non-starvation
- then include runnable foreground command/client-SQL machines
- then include runnable peer-control/local-control machines whose next action is
  exact-ready
- then include bulk/background payload machines subject to byte/object caps
- then include close/lifetime and maintenance machines under non-starvation
  budgets
- grant each machine a small action burst and stop at blocked/external wait,
  byte/object/poll budget exhaustion, failure, or terminal state
- continue a machine within the burst only while the next action is exact-ready
  and nonblocking
- boost collectors when active machines wait on the event they collect
- back off collectors with repeated empty polls
- prioritize foreground command/client-SQL machines over background payload
  machines, while capping foreground bursts to avoid starving bulk progress
- keep close/lifetime and maintenance non-starvable but budgeted

Suggested first constants, to be tuned by measurement:

- max plan grants: `16`
- max collector grants per plan: `4`
- max machine grants per plan: `12`
- max actions per machine grant: `2` for foreground/control, `1` for cold
  maintenance, and payload-specific byte/object caps for bulk streams
- max blind collector polls per plan: `1` per collector family unless a waiting
  machine boost or recent productive grant is present
- unknown-arrival collector backoff: preserve current periodic behavior as the
  starting policy, then tune by empty/productive feedback

Stop and replan when:

- a collector discovers a new foreground machine or unblocks an old foreground
  waiter
- a machine transitions from blocked to exact-ready due to an action result
- a machine hits blocked/external-wait after consuming less than its grant
- the plan exhausts collector or machine budget
- a terminal failure/reset changes ownership or generation

This makes the service-progress scheduler decide how to spend CPU on Homer
state-machine advancement. The egress scheduler remains responsible for RDMA
traffic-class resource allocation after an action such as payload publication or
peer-control send is selected.

### Implementation Milestone Plan

The state-machine scheduler should be implemented as a new milestone rather than
as another policy tweak on the flat source scheduler. Stage it so each slice has
a correctness/performance gate. Do not keep compatibility with aggregate
source-pump behavior as a requirement: this is a prototype milestone, and the
intended change is to expose async state-machine actions cleanly to the
scheduler.

1. **Split aggregate executors into action executors.** Split peer-control first:
   peer request mailbox, peer response mailbox, recv-CQ/CM, peer-control send-CQ,
   and close/lifetime become separate actions. Then split payload progress into
   outgoing publication, incoming consumption, send-CQ/source-release retirement,
   credit publication, EOS, and close/reclaim actions. Then split local-control
   slot collection from async continuation advancement.
   - Current progress: peer-control send-CQ and outgoing response-mailbox work
     are now separate phase/source identities, and the old combined
     send-CQ/response phase is removed. Request-mailbox remains a bounded action
     over the existing FIFO inbound mailbox, with the caveat above about mixed
     request/response messages. Remaining peer-control split work: separate
     recv-CQ/CM from listener/setup more mechanically if needed for machine
     facts, and keep close/lifetime as an independent non-starvable action when
     the machine executor lands.
   - Validation for this slice: `make -j8 service-bin client-bin` and
     `sudo -n make install-headers install-service-bin install` passed in
     `/data/dbcomm/citus-dbcomm`. Runtime artifacts were synced to farnet0 and
     matched by SHA-256. Focused peer-RDMA checks passed: warmed remote c1 Homer
     pgbench `20000/20000` transactions, `0` failures, `4604.77 TPS`, p99
     `0.238 ms`; remote c4 Homer pgbench `40000/40000` transactions, `0`
     failures, `11045.16 TPS`, p99 `0.591 ms`; remote RDMA basebackup warmed
     run completed in `4.59 s`; backend-to-backend Homer tuple COPY 10M rows
     completed with `count=10000000`, `min=1`, `max=10000000`,
     `sum=500000050000000`, warmed `5.24 s` in
     `/tmp/homer_state_machine_peer_split_copy_1780969094`. Service log tails
     showed normal setup/close/reclaim progress and no reset/error messages from
     the split peer-control branches.
   - Current progress: payload stream execution now has explicit action-mask
     families for local blackhole, outgoing publication, incoming consumption,
     and close/reclaim. The flat payload source still grants all families by
     default, so this is an executor-boundary split rather than a policy behavior
     change. Remaining payload split work: break send-CQ/source-release
     retirement, receiver-credit publication, and tuple-view EOS synthesis into
     smaller action results that machine facts can expose directly.
   - Current progress: per-stream `machine-baseline` payload grants now pass
     nonzero payload action flags for the split families that already exist.
     [`HomerServicePayloadGrantFlagsForActionKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5938)
     maps `PAYLOAD_LOCAL_BLACKHOLE`, `PAYLOAD_OUTGOING`, `PAYLOAD_INCOMING`,
     and `PAYLOAD_CLOSE_RECLAIM` to the existing executor masks consumed by
     [`HomerServicePayloadActionMaskForGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19573).
     It deliberately leaves `PAYLOAD_SEND_CQ`, `PAYLOAD_CREDIT_PUBLISH`, and
     `PAYLOAD_EOS` on the zero-flag compatibility path because those sub-actions
     are still embedded inside outgoing/incoming helpers; selecting them
     precisely before the helpers are split would give the scheduler a false
     boundary.
   - Validation for the payload action-mask slice: `make -j8 service-bin
     client-bin` and `sudo -n make install-headers install-service-bin install`
     passed in `/data/dbcomm/citus-dbcomm`. Runtime artifacts were synced to
     farnet0 and matched by SHA-256. Focused checks passed: warmed remote c1
     Homer pgbench `20000/20000` transactions, `0` failures, `4584.35 TPS`, p99
     `0.239 ms`; remote RDMA basebackup warmed run completed in `4.60 s`;
     backend-to-backend Homer tuple COPY 10M rows completed with
     `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`, warmed
     `5.16 s` in `/tmp/homer_state_machine_payload_action_split_copy_1780969544`.
     Service log tails showed normal setup, basebackup completion, payload
     close, and reclaim progress.
   - Validation for the per-stream payload grant-flag slice: `git clang-format
     HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --cached --check`, `git diff --check`, `sudo -n -u dbcomm make
     -j8 service-bin client-bin`, and `sudo -n make install-headers
     install-service-bin install` passed. Runtime artifacts were synced to
     farnet0 and matched by SHA-256: `citus_tuple_sink_service`
     `07243fd1219d104303eacdcdaa4392c8f7a1982e17f7711cf355f8c191a7734d`,
     `citus.so`
     `474a73bcf3a79408397ab31d08ebf0895c40422900b4ee27395033eeb7230ed8`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused gates passed after service restart and clean preflight: remote c1
     Homer pgbench warm rerun `20000/20000`, `0` failures, `4641.22 TPS`, p99
     `0.235 ms`; remote c4 Homer pgbench `40000/40000`, `0` failures,
     `11062.92 TPS`, p99 `0.608 ms`; remote RDMA basebackup cold/warm repeats
     `6.39` and `4.54 s` in
     `/tmp/homer_state_machine_payload_flags_basebackup_1780976507`;
     backend-to-backend Homer tuple COPY 10M rows completed with
     `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`, warm
     repeats `5.31 s` and `5.09 s` in
     `/tmp/homer_state_machine_payload_flags_copy_1780976529` and
     `/tmp/homer_state_machine_payload_flags_copy_rerun_1780976553`; mixed
     remote c4 pgbench plus remote RDMA basebackup passed with `9131.44 TPS` and
     basebackup `4.94 s` in
     `/tmp/homer_state_machine_payload_flags_concurrent_c4_1780976570`.
     Service-log signature checks and post-run process preflight were clean.
   - Current progress: local-control execution now distinguishes slot collection
     from async continuation advancement through
     [`HomerLocalControlProgressActionMask`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1361).
     [`TupleSinkServicePumpControlSlots()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21598)
     dispatches `ADVANCE_ASYNC` by calling
     [`TupleSinkServicePumpLocalControlAsyncOps()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20240),
     and only scans request-ready slots when `COLLECT_SLOTS` is granted.
     Existing grants still use zero flags and therefore preserve the previous
     aggregate order: advance async continuations first, then collect new slots.
     Remaining local-control split work: represent individual active
     local-control requests as machine facts with waiting-on and next-action
     state, and move the existing periodic async readiness cooldown out of
     [`HomerServiceLocalControlSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19054)
     into the baseline machine-aware policy.
   - Validation for the local-control action-mask slice: `git diff --check`
     passed. `make -j8 service-bin client-bin` hit only the known output-file
     permission issue, then `sudo -n -u dbcomm make -j8 service-bin client-bin`
     passed; `sudo -n make install-headers install-service-bin install` passed.
     Runtime artifacts were synced to farnet0 and matched by SHA-256:
     `citus_tuple_sink_service`
     `21f91ba1011933e3b59e15765cdded694a2b99f5d2cb186dd52a1b168da038f7`,
     `citus.so`
     `9de33407aa41bfa51c090f4336c41d05325d0810ccdbac73602903d45c8e4b77`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused checks passed after clean process preflight: warmed remote c1 Homer
     pgbench `20000/20000` transactions, `0` failures, `4619.30 TPS`, p99
     `0.244 ms`; remote c4 Homer pgbench `40000/40000` transactions, `0`
     failures, `11046.38 TPS`, p99 `0.578 ms`; remote RDMA basebackup warmed
     repeat completed in `4.56 s`; backend-to-backend Homer tuple COPY 10M rows
     completed with `count=10000000`, `min=1`, `max=10000000`,
     `sum=500000050000000`, warmed repeat `5.05 s` in
     `/tmp/homer_state_machine_local_control_split_copy_1780970167`. Post-run
     process preflight showed only the intended PostgreSQL and Homer service
     processes, and service logs had no `error`, `failed`, `reset`, `broken`,
     `stale`, `could not`, or `invalid` lines.
2. **Machine and collector scaffolding.** Add fixed-table
   `HomerProgressMachineRef`, machine facts, collector facts, action refs,
   machine grants, collector grants, action results, and transition results.
   Initially populate facts from existing session, stream, control, and peer
   transport state.
   - Current progress: the behavior-neutral scaffold has landed in
     [`tuple_sink_service_process.c`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1383).
     [`HomerServiceProgressRegistry`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2132)
     now owns fixed machine fact arrays for local-control slots, remote client SQL
     sender sessions, command sessions, payload streams, peer-control ops, plus
     one peer-connection machine and one maintenance machine. Startup
     initialization happens through
     [`HomerServiceInitializeProgressMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7554)
     and
     [`HomerServiceInitializeProgressCollectorFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7577),
     called from
     [`HomerServiceInitializeProgressRegistry()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7622).
     This does not yet change source ready-set behavior or policy decisions; later
     candidate builders must refresh these facts from authoritative service state
     before the `machine-baseline` policy consumes them.
   - Caveat: peer transport still owns private per-connection arrays, so this
     scaffold keeps one aggregate
     [`peerConnectionMachine`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2182)
     tied to the peer request context. Split per-connection machines should wait
     until the peer transport exposes stable connection refs or a compact
     connection-fact iterator; otherwise the scheduler would either duplicate peer
     internals or guess at stale connection identity.
   - Validation for the scaffold slice: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c` was run,
     `git diff --check` passed, `sudo -n -u dbcomm make -j8 service-bin
     client-bin` passed, and `sudo -n make install-headers install-service-bin
     install` passed. Runtime artifacts were synced to farnet0 and matched by
     SHA-256: `citus_tuple_sink_service`
     `bea45295c9d0e38a9f2d5ac5e6694ea935aa92183b7874a66b29df90225ddb8a`,
     `citus.so`
     `9de33407aa41bfa51c090f4336c41d05325d0810ccdbac73602903d45c8e4b77`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused runtime checks passed after clean process preflight: warmed remote c1
     Homer pgbench `20000/20000` transactions, `0` failures, `4641.92 TPS`, p99
     `0.235 ms`; remote c4 Homer pgbench `40000/40000` transactions, `0`
     failures, `11106.28 TPS`, p99 `0.587 ms`; remote RDMA basebackup warmed
     repeat completed in `4.54 s`. Post-run process preflight showed only the
     intended PostgreSQL and Homer service processes, and service logs had no
     `error`, `failed`, `reset`, `broken`, `stale`, `could not`, or `invalid`
     lines.
3. **Collector candidate builder.** Replace the conceptual ready-set entrypoint
   with collector candidates plus machine candidates.
   - Current progress: a behavior-neutral collector candidate bridge now consumes
     the existing coarse ready set in
     [`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22352),
     called from
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22824).
     The bridge intentionally does not re-run local-control or peer-control
     readiness predicates, because those predicates still own cooldown counters
     until the machine-aware policy takes over. Local-control slot counting is
     guarded by the old ready-set signal and performed by
     [`HomerServiceFreshLocalControlRequestSlotCount()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22292)
     so the bridge does not add a fixed control-slot scan to every pump pass.
     Duplicate collector candidates are merged by
     [`HomerProgressMachineCandidateSetAppendCollector()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4590).
   - The bridge adds an explicit command-send-CQ collector,
     [`HOMER_PROGRESS_COLLECTOR_COMMAND_SEND_CQ`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1401),
     because command/payload WR retirement should be visible as collector work
     instead of being hidden under the frontend command-ring collector.
   - Validation status: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --check`, `sudo -n -u dbcomm make -j8 service-bin client-bin`,
     and `sudo -n make install-headers install-service-bin install` passed.
     Runtime artifacts were synced to farnet0 and matched by SHA-256:
     `citus_tuple_sink_service`
     `723d5ea7d58749e9d6541cdb9443c59aef3d0f36bb778f6c8960fb544b3f0521`,
     `citus.so`
     `55dcf6cadef046bbf547bc5f3ff45b4612dae1a180888178fd6a2b79c55a9be5`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused runtime checks passed after clean process preflight: warmed remote c1
     Homer pgbench `20000/20000` transactions, `0` failures, `4689.84 TPS`, p99
     `0.232 ms`; remote c4 Homer pgbench `40000/40000` transactions, `0`
     failures, `11222.19 TPS`, p99 `0.575 ms`; remote RDMA basebackup warmed
     repeat completed in `4.55 s`; backend-to-backend Homer tuple COPY 10M rows
     completed with `count=10000000`, `min=1`, `max=10000000`,
     `sum=500000050000000`, repeats `8.66`, `5.69`, and `5.15 s` in
     `/tmp/homer_state_machine_collector_bridge_copy_1780971500`. The `8.66 s`
     COPY run was treated as warmup and the `5.69 s` repeat was followed by a
     `5.15 s` recheck, so there was no clear collector-bridge regression. Service
     logs had no `error`, `failed`, `reset`, `broken`, `stale`, `could not`, or
     `invalid` lines.
4. **Machine candidate builder.** Register active machines for local control,
   remote client SQL sender, command session, payload stream, peer-control op,
   peer connection, and maintenance. Populate ready action masks, blocked
   reasons, waiting-on masks, unblocks masks, and budget hints.
   - Current progress: a behavior-neutral machine candidate builder now refreshes
     machine fact snapshots in
     [`HomerServiceBuildProgressMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22766),
     called from
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23278)
     immediately after the collector bridge. The candidate set now stores
     [`HomerProgressMachineFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1531)
     snapshots directly instead of only machine refs, and duplicates are merged by
     [`HomerProgressMachineCandidateSetAppendMachine()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4656).
     This avoids forcing future policies to resolve candidate refs through a
     second owner lookup in the hot scheduling path.
   - Implemented machine categories:
     [`HomerServiceBuildLocalControlMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22482)
     exposes active async local-control ops only; fresh control slots remain
     collector candidates until claimed.
     [`HomerServiceBuildSessionMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22516)
     exposes remote client SQL sender machines and command-session completion
     machines.
     [`HomerServiceBuildPayloadMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22676)
     maps payload ready/blocked reason masks into ready actions, wait masks, byte
     and object hints, and WR/CQE hints through
     [`HomerServicePopulatePayloadMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22584).
     [`HomerServiceBuildPeerAndMaintenanceMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22729)
     derives coarse peer-connection and maintenance machine candidates from
     collector candidates.
   - Cost boundary: the machine builder is still a side-channel snapshot and does
     not drive execution yet. To avoid adding unconditional hot-loop scans, session
     and payload machine scans are gated by existing collector candidates, and
     local-control async scanning is gated by `LocalControlAsyncActiveCount`.
     Peer-control remains one aggregate peer-connection machine until the RDMA
     peer transport exposes stable per-connection fact iteration.
   - Validation status: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --check`, `sudo -n -u dbcomm make -j8 service-bin client-bin`,
     and `sudo -n make install-headers install-service-bin install` passed.
     Runtime artifacts were synced to farnet0 and matched by SHA-256:
     `citus_tuple_sink_service`
     `e563060d77ecd7b0b4c551da9c66ef63be412ba3954eddd83aceeab8cdee757b`,
     `citus.so`
     `55dcf6cadef046bbf547bc5f3ff45b4612dae1a180888178fd6a2b79c55a9be5`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused runtime checks passed: warmed remote c1 Homer pgbench `20000/20000`
     transactions, `0` failures, `4628.94 TPS`, p99 `0.236 ms`; remote c4 Homer
     pgbench `40000/40000` transactions, `0` failures, `11132.67 TPS`, p99
     `0.588 ms`; remote RDMA basebackup warmed repeat completed in `4.60 s`;
     backend-to-backend Homer tuple COPY 10M rows completed with `count=10000000`,
     `min=1`, `max=10000000`, `sum=500000050000000`, repeats `8.66`, `5.65`,
     and `5.09 s` in `/tmp/homer_state_machine_machine_candidate_copy_1780972018`.
     The `8.66 s` COPY run was treated as warmup. Service logs had no `error`,
     `failed`, `reset`, `broken`, `stale`, `could not`, or `invalid` lines.
5. **Machine action compiler and executor.** Compile collector and machine grants
   into the small action executors from Step 1. Convert bool-shaped helpers such
   as peer-client completion publication into explicit `PUBLISHED` / `NOT_READY`
   / `FAILED` results where needed.
   - Current progress: execution now passes through a compiled action layer. The
     installed execution plan owns one
     [`HomerProgressCompiledActionGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1758)
     per selected source-plan grant. The source-to-action mapping lives in
     [`HomerServiceProgressActionKindForSourceKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5104),
     and
     [`HomerProgressExecutionPlanCompileActions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5230)
     compiles the source plan into typed action grants during installation.
     [`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23513)
     now iterates `actionGrants`, dispatching through
     [`HomerServiceExecuteProgressActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23253)
     while preserving the old source-plan ordering and per-grant policy feedback.
   - Boundary/caveat: this is a one-action-per-source compatibility slice, not the
     final machine policy. Local-control and payload actions still carry zero
     flags from the old source plan, which their executors decode as "all ready
     action families". This preserves behavior while moving the hot execution
     boundary to typed action grants. The old source dispatcher is marked unused
     as a transitional reference; future slices should either remove it or split
     the remaining source-shaped helpers once `machine-baseline` emits direct
     collector/machine action grants.
   - Validation status: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --check`, `sudo -n -u dbcomm make -j8 service-bin client-bin`,
     and `sudo -n make install-headers install-service-bin install` passed.
     Runtime artifacts were synced to farnet0 and matched by SHA-256:
     `citus_tuple_sink_service`
     `b39f9d26e3ef885c7d9f392e57417d94c6547ed11337813c661d78983a5afe9b`,
     `citus.so`
     `b0afd5aefcb5ad4ddbe2be118b3a6a4fb0696ad55550147e5ff328a3368c0bc7`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Focused runtime checks passed: warmed remote c1 Homer pgbench `20000/20000`
     transactions, `0` failures, `4635.45 TPS`, p99 `0.239 ms`; remote c4 Homer
     pgbench `40000/40000` transactions, `0` failures, `10827.32 TPS`, p99
     `0.631 ms`; remote RDMA basebackup warmed repeat completed in `4.61 s`;
     backend-to-backend Homer tuple COPY 10M rows completed with `count=10000000`,
     `min=1`, `max=10000000`, `sum=500000050000000`, repeats `8.89`, `5.40`,
     and `5.20 s` in `/tmp/homer_state_machine_action_plan_copy_1780972576`.
     The first COPY run was treated as warmup. Service logs had no `error`,
     `failed`, `reset`, `broken`, `stale`, `could not`, or `invalid` lines.
6. **Enable `machine-baseline` policy.** Use collector/machine facts to build
   plans with bounded collector grants, foreground command/client grants,
   peer/local-control grants, capped payload grants, and non-starvable
   maintenance. Validate against remote pgbench c1/c4, remote RDMA basebackup,
   mixed pgbench+basebackup, and Citus tuple COPY.
   - Current progress: the first direct machine-aware policy is now selectable as
     `HOMER_PROGRESS_POLICY=machine-baseline`. The policy interface has a direct
     action-plan hook in
     [`HomerProgressPolicyOps`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1951),
     registered by
     [`HomerMachineBaselineProgressPolicyOps`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6614).
     Existing fixed-priority, round-robin, unblocked-first, cpu-liveness, and
     adaptive-bounded-class policies still use the source-plan hook. The service
     loop selects the direct action-plan path in
     [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:24392)
     only when the selected policy exposes `buildActionPlan`.
   - The direct policy builder
     [`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5856)
     consumes the maintained collector/machine candidate set and emits typed
     action grants through
     [`HomerProgressExecutionPlanAppendExplicitActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5365).
     Its conservative ordering now admits service liveness and fresh
     local-control slots before foreground machine bursts, then drains command
     send-CQ, advances active local-control async machines, posts foreground
     remote-client commands, advances peer-control, publishes target/completion
     results, and finally grants payload progress. This is intentionally a
     baseline machine-aware policy, not the tuned adaptive policy.
   - Boundary/caveats: dense payload COPY/basebackup streams still preserve the
     aggregate payload source when the old ready set contains
     `HOMER_PROGRESS_PAYLOAD_AGGREGATE_INDEX`; otherwise a direct per-stream
     machine plan would expand hot COPY into many grants before the payload egress
     layer can batch real transport work. Per-stream payload machine actions now
     pass exact flags for the already split local-blackhole, outgoing, incoming,
     and close/reclaim families, but still pass zero flags for embedded
     send-CQ/source-release retirement, receiver-credit publication, and tuple
     EOS synthesis. Peer-control uses the aggregate peer-control action when the
     current ready set admits aggregate peer-control, because setup/close
     progress is not yet fully represented by collector facts.
   - Diagnostics: progress stats now record direct action-plan builds through
     [`HomerServiceProgressStatsRecordActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4250),
     so `machine-baseline` does not disappear from plan-build counters when stats
     macros are enabled. Startup env parsing for the new policy is in
     [`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3881).
   - Validation status: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --check`, `git diff --cached --check`,
     `sudo -n -u dbcomm make -j8 service-bin client-bin`, and `sudo -n make
     install-headers install-service-bin install` passed in
     `/data/dbcomm/citus-dbcomm`. Runtime artifacts were synced to farnet0 and
     matched by SHA-256: `citus_tuple_sink_service`
     `03e4bd3a36a426094334aa4684a478fa0ecd3dea91d4c6c3b7f99fa83393a87e`,
     `citus.so`
     `a06feb378a579e37fc2161228dcdcf88bf5d3db63a0e93e1ae204add56b9f25b`,
     and `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
     Both services logged `HOMER_PROGRESS_POLICY=machine-baseline max_grants=64`.
   - Focused runtime checks passed after clean process preflight: warmed remote
     c1 Homer pgbench `20000/20000` transactions, `0` failures, `4517.13 TPS`,
     p99 `0.241 ms`; remote c4 Homer pgbench `40000/40000` transactions, `0`
     failures, `10710.53 TPS`, p99 `0.621 ms`; remote RDMA basebackup warmed
     repeat completed in `4.49 s`; backend-to-backend Homer tuple COPY 10M rows
     completed with `count=10000000`, `min=1`, `max=10000000`,
     `sum=500000050000000`, repeats `8.73`, `5.37`, and `5.11 s` in
     `/tmp/homer_state_machine_machine_baseline_copy_1780973489`. The first COPY
     run was treated as warmup. Mixed remote c4 pgbench plus remote RDMA
     basebackup completed correctly with no failures; repeats were variable:
     pgbench `8013.51`, `8983.49`, and `8387.55 TPS`, with basebackup `4.63`,
     `4.65`, and `4.60 s` in
     `/tmp/homer_state_machine_machine_baseline_concurrent_c4_1780973531`,
     `/tmp/homer_state_machine_machine_baseline_concurrent_c4_repeat_1780973551`,
     and
     `/tmp/homer_state_machine_machine_baseline_concurrent_c4_repeat2_1780973569`.
     Treat the mixed-workload band as a correctness/no-crash gate for this
     baseline policy, not as evidence that adaptive interference policy is tuned.
     Post-run process preflight showed only intended PostgreSQL and Homer service
     processes, and both service logs had no `error`, `failed`, `reset`,
     `broken`, `stale`, `could not`, or `invalid` lines.
7. **Tune and diagnose.** Tune action burst sizes, collector backoff, blind poll
   caps, payload byte/object caps, and stop/replan triggers. Accept the milestone
   only after clean correctness, clean process/log preflight, and no meaningful
   regression versus the current fixed-priority baseline.
   - Current progress: `machine-baseline` now has tunable plan-budget caps with
     defaults `max_plan=16`, `max_collectors=4`, `max_machines=12`,
     `max_payload=2`, and `max_blind_collectors=1`. The defaults live near
     [`HOMER_SERVICE_MACHINE_BASELINE_MAX_GRANTS`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:144),
     env parsing is in
     [`HomerMachineBaselinePolicyInitialize()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6506),
     and the per-plan budget/drop accounting is in
     [`HomerMachineBaselinePlanBudget`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5460)
     and
     [`HomerMachineBaselineBudgetTryReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5508).
   - Important caveat/fix: the first budgeted default-cap build placed
     `HEARTBEAT_MAINTENANCE` and local-control slot collection behind foreground
     machine bursts. Under mixed remote c4 pgbench plus remote RDMA basebackup,
     pgbench succeeded but basebackup close failed with `Homer base backup target
     failed during close stream`; the waiting backend raised the error from
     `WaitForRemoteExecutionControlResponse()` because the service heartbeat did
     not advance. A loose-cap A/B (`max_plan=64`, `max_collectors=16`,
     `max_machines=64`, `max_payload=8`, `max_blind_collectors=4`) passed the
     same mixed shape, confirming the issue was budget starvation rather than
     payload corruption. The accepted fix keeps the modest default caps but
     plans due heartbeat and fresh local-control slots first in
     [`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5890).
     This is a policy ordering rule: service liveness and lifecycle control are
     budgeted, but they are not allowed to disappear behind a full foreground
     command/completion budget.
   - Diagnostics: when `HOMER_SERVICE_PROGRESS_STATS` is enabled,
     [`HomerServiceLogProgressStats()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4314)
     now prints `progress_machine_baseline_stats` with action counts,
     collector/machine/payload counts, budget-drop counters, executed/productive
     grants, and stop reason. Keep these stats off for acceptance performance
     runs.
   - Validation status for the budget/order slice: `git clang-format HEAD --
     src/backend/distributed/utils/homer/tuple_sink_service_process.c`,
     `git diff --cached --check`, `sudo -n -u dbcomm make -j8 service-bin
     client-bin`, and `sudo -n make install-headers install-service-bin install`
     passed in `/data/dbcomm/citus-dbcomm`. The patched runtime artifacts were
     synced to farnet0 with targeted remote-sudo rsync after a broad prefix rsync
     hit expected permission errors on owned logs and prefix metadata. Final
     hashes matched on both hosts: `citus_tuple_sink_service`
     `d7ccbfa244ae9e509e9042d0f31cf21c9f2423376ab9a3128f1f76ae140516c4`,
     `citus.so`
     `f4dab0195962e7f5efe93b28d6ce279e30499c547c05fe25eceac58ac9b930be`, and
     `libhomer_client.a`
     `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
   - Focused runtime checks on the patched default-cap policy passed: remote c1
     Homer pgbench `20000/20000`, `0` failures, `4519.03 TPS`, p99 `0.251 ms`;
     remote c4 Homer pgbench `40000/40000`, `0` failures, `10853.03 TPS`, p99
     `0.607 ms`; remote RDMA basebackup repeats `4.42` and `4.47 s`;
     backend-to-backend Homer tuple COPY 10M rows completed with
     `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`, with
     warm run `5.20 s` in
     `/tmp/homer_state_machine_budget_order_fix_copy_1780974915`. Mixed remote
     c4 pgbench plus remote RDMA basebackup passed after the ordering fix:
     warmup `3657.05 TPS`/`8.84 s`, then warmed `8767.48 TPS` with basebackup
     `4.67 s` in
     `/tmp/homer_state_machine_budget_order_fix_concurrent_c4_warm2_1780974840`.
     Treat the mixed run as a correctness/no-regression gate for this baseline
     policy, not as tuned adaptive interference policy.

## Post-Step-8 Sub-Milestones

Step 8 validated the flat-source scheduler substrate and the adaptive
compatibility baseline. The state-machine scheduler pivot above supersedes the
older source-only design as the long-term target. The two sub-milestones below
remain useful, but should be interpreted as transitional slices toward
state-machine scheduling:

- move periodic collector work into policy-owned cooldowns
- expose dependency/fact edges as machine states, blocked reasons, waiting-on
  masks, and unblocks masks rather than only as source-plan repair rules

### A. Move Periodic Work Into Policy-Owned Cooldowns

Current code has several pre-policy admission gates. Examples include active and
idle peer-control polling in
[`HomerServicePeerControlSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4039),
async local-control continuation polling in
[`HomerServiceLocalControlSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18917),
command send-CQ readiness throttling in
[`HomerServiceCommandSendCqReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6428),
and heartbeat admission in
[`HomerServiceHeartbeatDueForPump()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3958).
Those constants are acceptable as default policy parameters, but the decision to
spend CPU on a pollable source should belong to the service-progress policy, not
to the readiness predicate.

The first implementation slice should keep behavior equivalent by moving the
current intervals into policy-private cooldown state. The fact layer should
expose whether a source is exact-ready, pollable-live, blocked, or
non-starvable-maintenance; the policy then decides whether the source is due.
Executors still re-check authoritative state and report empty/productive/blocked
feedback.

The fact vocabulary should distinguish three readiness shapes:

- **Known expected work:** local state says work should exist or should soon
  exist, such as outstanding signaled WRs, source-ring wrap pressure, a
  request-ready local control slot, outstanding peer response WRs, or a pending
  close/reclaim item.
- **Known blocked work:** work exists but cannot currently progress, such as
  remote-credit blockage, local send-resource pressure, completion publication
  waiting on payload EOS, or source/lifetime reuse waiting on send-CQ retirement.
- **Unknown external arrival:** the remote side may have written or signaled
  work, but the service can only learn that by polling or by consuming a
  notification/doorbell. These sources need policy-owned cooldown, recent
  feedback, and dependency boosts rather than a pretend exact predicate.

This is analogous to an interrupt/poll hybrid policy even though the current
prototype remains polled: repeated empty polls should back off, a non-empty poll
should make near-future work more likely, known outstanding local WRs should make
send-CQ polling due soon, and dependency facts can temporarily override cooldown
when a source is expected to unblock another source.

Facts needed before split peer-control can be used by adaptive policies should
prefer executor-maintained counters/hints over repeated deep scans:

- `PEER_RECV_CQ`: recent non-empty recv-CQ history, CQEs drained in the last
  grant, recent incoming doorbell reason, and a cheap "pollable while active"
  liveness bit for active peer connections where no exact verbs predicate exists.
  Purpose: distinguish unknown external-arrival polling from recently productive
  recv-CQ work, and expose when recv-CQ is expected to unblock mailbox, command,
  or payload processing.
- `PEER_REQUEST_MAILBOX`: local incoming-mailbox head/tail or sequence-visible
  dispatch work, plus a hint that a recv-CQ doorbell just made request dispatch
  likely. Purpose: expose known expected incoming request dispatch instead of
  treating mailbox as a blind peer-control poll.
- `PEER_RESPONSE_MAILBOX`: local outgoing async response mailbox head/tail,
  active response waiters, and recent response consumption count. Purpose:
  expose response arrivals that unblock local-control continuations without
  conflating response progress with send-CQ retirement.
- `PEER_SEND_CQ`: outstanding response/control send WR count, send-CQ completions
  drained recently, and whether retiring CQEs frees command/response scratch or
  lifetime state.
  Purpose: expose known expected CQEs from locally posted control/response WRs
  and known blocked work waiting on send-CQ retirement.
- `PEER_CM_SETUP`: active incomplete outgoing setup, accepted-but-not-bootstrapped
  incoming setup, listener CM event polling due, and recent setup progress.
  Purpose: separate cold setup liveness from hot active peer transport progress.
- `PEER_CLOSE_LIFETIME`: pending disconnect, deferred close/reclaim count,
  retired-but-not-reset connection state, and age of oldest close/lifetime item.
  Purpose: keep cleanup non-starvable without making it look like hot data-path
  progress.
- `LOCAL_CONTROL`: active async continuation count, phase of each active
  continuation family, request-ready slot count, and whether the continuation is
  waiting on peer setup, peer response, or local publication.
  Purpose: distinguish known local control requests from async continuations that
  are blocked on peer transport progress.
- `CQ_DRAIN` / command send-CQ: outstanding signaled command WR count, source-ring
  wrap pressure, and recent CQE/empty-poll feedback.
  Purpose: expose known expected CQEs and source-slot release pressure rather
  than letting command send-CQ polling be only a periodic gate.
- `PAYLOAD_STREAM`: dependent completion count/hint, oldest dependent age,
  payload EOS/source-release frontier, pending payload send-CQ retirement, remote
  credit blockage, and local consumer/resource pressure. Purpose: let the policy
  see when payload work is not only data movement but also the prerequisite that
  will unblock command-completion publication.
- `COMPLETION_RING` / peer-client completion publication: completion publication
  waiting on payload EOS/source-release or prior result send-CQ retirement.
  Purpose: classify the completion source as known-blocked instead of letting a
  policy repeatedly grant it while the payload/source-release prerequisite is not
  satisfied.
- `HEARTBEAT`: maintenance due state should be represented as a cold
  non-starvable source with a policy-owned cooldown.
  Purpose: preserve service liveness diagnostics while keeping heartbeat out of
  hot plans unless due.

A shallow active-state scan may still be needed for table bounds or active
connection counts, but deep "is there work?" probing should stay in the executor
or in maintained facts updated by the executor's last grant.

The adaptive policy design is still open. Candidate policies to discuss and
measure:

- **Parameter-equivalent cooldown policy:** preserve the current `64`/`1024`
  behavior, but store counters in policy state. This is the safest first slice
  because it proves the fact/policy boundary without changing performance goals.
- **Feedback backoff policy:** reduce grants for sources with repeated empty
  polls and restore frequency when exact-ready facts, doorbells, completions,
  request-ready slots, or close/lifetime work appear. This is likely the first
  useful adaptive policy.
- **Class-deficit policy:** maintain policy-local deficits for hot data, hot
  control, close/lifetime, and cold setup/maintenance. Pollable sources consume
  deficit when granted; productive grants replenish or reset backoff. This makes
  "foreground hot work first, but maintenance never starves" explicit.
- **Dependency-boosted cooldown policy:** temporarily lift cooldown for a source
  that is known to unblock another source, such as recv-CQ before mailbox
  dispatch or payload EOS before peer completion publication.

The design question to settle before implementation is how aggressive the first
adaptive policy should be. The recommended sequence is to first land the
parameter-equivalent cooldown policy, validate no regression, then add feedback
backoff and class deficits in separate measured slices.

Implementation status:

- The parameter-equivalent cooldown slice has landed in the Citus/Homer tree.
  `HOMER_PROGRESS_POLICY=machine-baseline` now owns cooldown counters in
  [`HomerMachineBaselinePolicyState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1993)
  for active peer-control polling, idle peer-control polling, async
  local-control continuation polling, heartbeat maintenance, and command
  send-CQ polling. Legacy non-machine policies still use the service-global
  counters documented near
  [`HomerServiceHeartbeatPassCountdown`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2298),
  so this is not yet a full removal of source-shaped readiness gates.
- The policy-owned cooldown helpers are
  [`HomerMachineBaselinePolicyPeerControlDue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4443),
  [`HomerMachineBaselinePolicyLocalControlAsyncDue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4462),
  [`HomerMachineBaselinePolicyHeartbeatDue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4475),
  and
  [`HomerMachineBaselinePolicyCommandSendCqDue()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4481).
  They are called from the existing readiness hooks
  [`HomerServicePeerControlSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4602),
  [`HomerServiceLocalControlSourceReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20696),
  [`HomerServiceHeartbeatDueForPump()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4516),
  and
  [`HomerServiceCommandSendCqReadyForScheduler()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7972).
  This is intentionally a transitional boundary: the policy owns the decision
  state, while the old source-ready construction still asks whether the
  pollable source is due.
- The initial machine-baseline cooldown values are set in
  [`HomerMachineBaselinePolicyInitialize()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6618)
  to preserve the old first-pass cadence. Fresh local-control slots and exact
  payload/frontier facts remain immediate rather than cooldown-gated.
- Validation for this slice passed with stats/logging macros off. Build/install:
  `sudo -n -u dbcomm make -j8 service-bin client-bin`, `sudo -n make
  install-headers install-service-bin install`, targeted artifact sync to
  farnet0, and matching installed hashes on both hosts. Runtime hashes:
  `citus_tuple_sink_service`
  `b75f5083ff2e0713f123af25d54a64a6b25797bac814b5712661dfb1d1bce344`,
  `citus.so`
  `a6916f51cf0ba0d1151bd8fe48d626aeba020a4355497607923b3492189fbf9f`,
  and `libhomer_client.a`
  `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
- Focused runtime gates were clean after service restart and process preflight:
  remote c1 Homer pgbench warm rerun `20000/20000`, `0` failures, `4658.22
  TPS`, p99 `0.234 ms`; remote c4 Homer pgbench `40000/40000`, `0` failures,
  `11104.29 TPS`, p99 `0.599 ms`; remote RDMA basebackup cold/warm repeats
  `6.37` and `4.52 s` in
  `/tmp/homer_state_machine_policy_cooldown_basebackup_1780975525`;
  backend-to-backend Homer tuple COPY 10M rows completed with
  `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`, warm run
  `5.14 s` in `/tmp/homer_state_machine_policy_cooldown_copy_1780975546`;
  mixed remote c4 pgbench plus remote RDMA basebackup passed twice with
  `8271.87 TPS`/`4.52 s` in
  `/tmp/homer_state_machine_policy_cooldown_concurrent_c4_1780975573` and
  `8385.68 TPS`/`4.67 s` in
  `/tmp/homer_state_machine_policy_cooldown_concurrent_c4_rerun_1780975590`.
  The mixed TPS is lower than the prior single warmed reference (`8767.48 TPS`)
  but was correct, error-free, and in the same broad band; keep watching this
  gate when adding adaptive backoff/deficit behavior.

### B. Make Dependency-Aware Planning Explicit

The Step 7 rejection of true class reordering exposed a dependency problem, not
only a starvation problem. The concrete reproduced path was a terminal client-SQL
completion being consumed from the backend completion ring, then intentionally
withheld because payload EOS had not reached the required frontier. The relevant
ordering check is in
[`HomerServicePayloadStreamResultEosPosted()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9009),
called by
[`TupleSinkServicePublishPeerClientCommandCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9054).
Retry currently happens from payload execution through
[`TupleSinkServicePublishPendingPeerClientCommandCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10350)
after the payload source runs in
[`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21787).

The dependency *types* are mostly static and should be encoded once in scheduler
substrate helpers, not rediscovered at runtime and not duplicated by every
policy. The dependency *instances* and readiness state are dynamic: a particular
session may or may not currently have terminal completion waiting on a particular
payload stream, and that stream may be ready, credit-blocked, send-resource
blocked, or already past EOS. Static rules should therefore consume dynamic
facts.

Known static dependency rules to encode, after a full code-path inventory:

- terminal peer-client completion with tuple EOS depends on the corresponding
  payload stream reaching the EOS/source-release frontier before final completion
  publication
- payload progress may need command/response send-CQ retirement before producer
  source bytes can be safely reused
- peer request-mailbox dispatch is often enabled by recv-CQ doorbells and should
  not be treated as an unrelated cold poll
- local-control async continuations waiting on peer responses depend on
  peer-response-mailbox consumption, not on generic peer mailbox polling
- close/lifetime progress may depend on send-CQ retirement and must remain
  non-starvable even when low throughput value
- local-control async continuations for remote open/start/poll depend on peer
  setup/request-mailbox/response-mailbox/send-CQ progress, but should not run as
  unbounded control work

The inventory is itself a subtask. For each dependency, record:

- dependent source and prerequisite source
- static rule type and dynamic instance facts needed to identify it
- whether the prerequisite can block or wait
- whether the dependency is safe to bundle or must be expressed as ordered grants
- whether the rule participates in an acyclic dependency graph
- diagnostics needed to tell whether the policy is avoiding the dependency or
  relying on closure/repair too often

The preferred design is not a large bundled grant. Bundling is only safe when
all sub-work is short and non-blocking. If a prerequisite can be credit-blocked,
resource-blocked, or waiting on external progress, the scheduler should preserve
ordering while allowing other work between attempts.

Likely bundle candidates are small, opportunistic, and bounded:

- after a payload-stream grant, retry pending peer-client completion publication
  for terminal completions whose payload EOS frontier may now be satisfied
- after a recv-CQ grant drains peer-control doorbells, dispatch a small bounded
  peer request-mailbox grant if request dispatch became visible
- after a send-CQ grant retires control/response WRs, run a small bounded
  close/reclaim step if the retired CQEs unblock lifetime state

Likely non-bundle dependencies should be handled as ordered grants instead:

- do not let a completion grant spin payload until EOS
- do not let local-control async spin peer setup/request-mailbox/response-mailbox
  or send-CQ until a remote response arrives
- do not let payload progress spin send CQ until all source bytes retire

Current code already has one implicit executor bundle: after executing a payload
source, the payload executor calls
[`TupleSinkServicePublishPendingPeerClientCommandCompletions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:10350)
from the `PAYLOAD_STREAM` branch of
[`HomerServiceExecuteProgressSource()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21787).
That means a single scheduler payload grant currently covers both payload
progress and the terminal-completion publish retry. This retry is not a blocking
wait: `TupleSinkServicePublishPeerClientCommandCompletion()` returns success
without publishing when the completion is not ready because payload EOS has not
reached the required frontier or because prior result-stream send completions
are still pending. Model this as an async "not ready yet" dependency, not as a
synchronous completion wait.

The target design should make that deferred state scheduler-visible without
turning it into heap-allocated scheduler work. Change the publication helper from
a bool-shaped API to a small result enum, for example `PUBLISHED`, `NOT_READY`,
and `FAILED`, with `NOT_READY` carrying a generic blocked reason such as
`COMPLETION_WAITING_ON_PAYLOAD_EOS` or
`COMPLETION_WAITING_ON_RESULT_SEND_CQ`. The executor can then set the completion
source's blocked reason and set a dependent-work hint on the prerequisite payload
source.

The current active-session retry scan is a pragmatic compatibility shape, not the
desired long-term design. The better design is fixed-table, event-driven
bookkeeping:

- when completion publication first returns `NOT_READY`, mark the session as
  having one deferred peer-client completion and record the prerequisite stream
  index/service stream id plus blocked reason
- add that session to an intrusive pending list, or update a small fixed-table
  pending bit/hint, without heap allocation or locks
- when payload EOS/source-release or result send-CQ retirement advances, retry
  only the pending sessions for the affected stream or for the small global
  deferred-completion list
- clear the pending mark when publication succeeds, fails terminally, or the
  owning session/stream is reset

This keeps the useful payload->completion locality while avoiding an
`ActiveSessionScanLimit` walk on every payload grant. Until measurements show the
scan is visible, the implicit payload retry can remain as a behavior-preserving
transition; the important design point is that the fact vocabulary and result
API should no longer hide `NOT_READY` behind a success bool.

For non-bundled dependencies, add a generic dependency-planning pass between
policy ordering and plan execution:

1. Build current source facts and a ready set.
2. Let the policy choose a ranked, budgeted source plan using facts and feedback.
3. Run a substrate dependency-closure helper that applies static dependency
   rules to the policy plan. It may reorder selected grants or insert a small
   prerequisite grant when the prerequisite source is visible and runnable.
   The rule set should be a DAG so closure is deterministic and bounded rather
   than an unbounded "iterate until fixed" loop.
4. Execute the repaired/closed plan. Executors still report blocked reason masks
   when a dependency was not visible or became blocked after planning.
5. At grant boundaries, stop-and-replan if execution discovers a dependency that
   the closure pass could not satisfy, for example when a grant consumes a
   completion and thereby creates a new dynamic dependency instance. In-place
   insertion/repair at the grant boundary remains a later policy/substrate
   optimization to evaluate against stop-and-replan.

V1 closure should use stop-and-replan, not in-place repair. In-place insertion
may become attractive if measurements show full replanning is too expensive, but
it is future work and must be constrained by substrate helpers so policies do not
own cursor arithmetic, grant-array mutation, or capacity invariants.

This keeps dependency handling out of individual policy implementations while
still letting policies learn from dependency facts. A policy should see that a
payload stream unblocks a completion and rank it earlier on its own; the closure
helper is a correctness guard and a diagnostics source, not the common fast path
we want to rely on forever.

Baseline policy behavior should be dependency-aware but conservative:

- do not grant a known-blocked dependent source repeatedly while its blocked
  reason names an unsatisfied prerequisite
- boost or admit the prerequisite source when it is exact-ready or recently
  productive, subject to normal class/traffic budgeting
- if the prerequisite is an unknown external-arrival source, apply a
  policy-owned cooldown plus a dependency boost rather than spinning it every
  pass
- after a prerequisite grant reports productive progress, make the dependent
  source eligible again or stop-and-replan so the completion/state-machine step
  can run promptly
- record closure/replan counts by dependency reason so a mature policy can be
  judged by how rarely substrate repair is needed on the hot path

Implementation progress:

- The first dependency-aware baseline slice is now implemented as collector
  demand derived from machine wait facts. After machine candidates are built,
  [`HomerServiceAppendCollectorDemandFromMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23752)
  walks the current machine snapshot and
  [`HomerServiceAppendCollectorDemandForMachineWait()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23669)
  maps generic `waitingOnMask` bits to collector candidates through
  [`HomerServiceAppendProgressCollectorDemandCandidate()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23566).
  This is a dependency-fact bridge, not a DB-semantic rule: machines name event
  families such as peer response mailbox, command send-CQ, payload frontier, or
  remote credit; the bridge names the collector that can make that event family
  visible.
- `machine-baseline` now consumes those demand facts before spending the rest of
  the plan on runnable machines. The policy helper
  [`HomerServiceMachineBaselineAppendWaitingCollectorActions()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5867)
  grants collectors with `waitingMachineCount > 0` ahead of local-control,
  frontend-command, completion, and payload machine bursts. Duplicate collector
  grants are suppressed by the `collectorPlanned` bitmap in
  [`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6018),
  so a collector that was already admitted for liveness or exact work does not
  consume another plan slot in the demand pass.
- Local-control async facts were tightened at the same time:
  [`HomerServiceLocalControlAsyncPhaseWaitsForPeerResponse()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23624)
  marks only the `*_WAIT_PEER*` phases as waiting on
  `HOMER_PROGRESS_WAIT_PEER_RESPONSE_MAILBOX`. Earlier scaffolding conservatively
  marked every active async local-control continuation as waiting on both peer
  response mailbox and peer send-CQ, which would have made the new demand bridge
  overpoll peer send-CQ.
- Boundary/caveat: the wait-mask bridge already maps all generic wait families,
  but only collectors with concrete source/action mappings can execute today.
  For example, peer response mailbox and peer send-CQ demand can produce real
  peer-control collector grants, while some broader mappings remain fact-only
  until those source/action executors are split further. This is intentional:
  the generic facts can land before every collector has a fully independent
  action implementation.
- Validation for this slice passed with stats/logging macros off. Formatting and
  build/install: `git clang-format HEAD --
  src/backend/distributed/utils/homer/tuple_sink_service_process.c`, `git diff
  --cached --check`, `git diff --check`, `sudo -n -u dbcomm make -j8
  service-bin client-bin`, and `sudo -n make install-headers install-service-bin
  install`. Runtime artifacts were synced to farnet0 and matched by SHA-256:
  `citus_tuple_sink_service`
  `0b08da35ed1385962eb010f99c306dc252d716b4f5db3501bffc977ef332b157`,
  `citus.so`
  `c6978babd32f117becc281202ab6a4134b02da5854dc18d5017d29f8b9a3cc73`,
  and `libhomer_client.a`
  `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`.
- Focused runtime gates were clean after service restart and process preflight:
  remote c1 Homer pgbench warm rerun `20000/20000`, `0` failures, `4650.50
  TPS`, p99 `0.234 ms`; remote c4 Homer pgbench `40000/40000`, `0` failures,
  `11106.74 TPS`, p99 `0.588 ms`; remote RDMA basebackup cold/warm repeats
  `6.01` and `4.68 s` in
  `/tmp/homer_state_machine_wait_collector_basebackup_1780976149`;
  backend-to-backend Homer tuple COPY 10M rows completed with
  `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`, warm run
  `5.05 s` in `/tmp/homer_state_machine_wait_collector_copy_1780976168`;
  mixed remote c4 pgbench plus remote RDMA basebackup passed with `8535.19 TPS`
  and basebackup `4.65 s` in
  `/tmp/homer_state_machine_wait_collector_concurrent_c4_1780976195`.
  Post-run service logs had no error/reset/stale-owner signatures and process
  preflight showed only the intended PostgreSQL and Homer service processes.

Cheap diagnostics for this sub-milestone:

- dependency-closure insert/reorder count by reason
- stop-and-replan count by dependency reason
- prerequisite grant productive/empty/blocked after closure
- dependent grant blocked count before and after closure
- age of the oldest dependency-blocked operation

These counters are how we verify whether the scheduler learned to avoid repair:
after policy tuning, closure and stop-and-replan counts should fall on the hot
path, while correctness remains protected if a policy experiment misses an edge.

## Implementation Sequence

The implementation should be staged so each slice preserves correctness and has
clear validation:

1. Split peer-control source classes before persistent plans. Add first-class
   progress source refs/facts for setup/listener, recv-CQ, request-mailbox,
   response-mailbox, send-CQ, and close/lifetime while keeping the old aggregate
   path as a fallback/default.
2. Inventory and normalize typed grant units for every source executor. Ensure no
   source can only be represented by an abstract CPU token.
3. Add `HomerProgressExecutionPlan` and `HomerProgressPlanStopState`, but keep
   the initial behavior equivalent to "build one plan and consume it
   immediately" so the struct/lifetime change is behavior-preserving.
4. Add `HomerProgressPolicyOps` and move existing fixed-priority, round-robin,
   unblocked-first, and cpu-liveness policies behind the pluggable interface.
   Verify correctness and performance before proceeding: this step should be
   interface-only, so any regression means the policy wrapper changed behavior or
   added measurable overhead. Treat a failed correctness run or a sustained
   performance regression as a blocking issue to diagnose and fix before Step 5.
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
   the policy as a candidate default. Do not tune around correctness failures or
   accept a policy that regresses the established fixed-priority/default
   baselines without identifying the cause.
8. Run final validation and tuning for the full milestone. Confirm tuple COPY,
   remote pgbench c1/c4, remote RDMA basebackup, mixed pgbench+basebackup, clean
   process preflight, clean service logs, and matching binaries across hosts.
   Tune plan length, bulk caps, empty/blocked thresholds, class weights, and
   preemption thresholds based on these measurements. Any correctness break,
   stale-process contamination, service-log error, binary mismatch, or meaningful
   throughput/latency regression must be resolved before claiming the milestone
   complete.
9. Only after measurement shows replanning overhead matters, enable optional
   constrained in-place repair through `HomerProgressPlanRepairOps`.

## Implementation Progress

### Step 1 Peer-Control Source Split

Started on June 8, 2026. The Citus service now has first-class peer-control
source kinds for setup/listener, recv-CQ, mailbox, send-CQ, and close/lifetime
in
[`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1253).
Each split source has a stable registry core and feedback slot in
`HomerServiceProgressRegistry`, and `HomerServiceBuildPeerControlProgressPlan()`
maps the selected split source back to the existing RDMA peer-pump phase mask.

Important caveat: this first slice deliberately does not add a deep
transport-private per-phase readiness scan. Split sources still share the old
coarse periodic peer-control readiness gate from
`HomerServicePeerControlSourceReadyForScheduler()`. The fixed-priority default
continues to use the aggregate `HOMER_PROGRESS_SOURCE_PEER_CONTROL`; the split
sources are exposed to the `cpu-liveness` policy first, because that policy
already has the hot/cold CPU-class contract needed to use the finer source
identities. This keeps the default path behavior-preserving while giving later
persistent-plan/adaptive policies source-local feedback for peer CQ, mailbox,
send-CQ, setup, and close/lifetime work.

Verification for this slice: `sudo -n -u dbcomm make -j8 service-bin
client-bin` in `/data/dbcomm/citus-dbcomm` completed successfully. No runtime
correctness or performance workload was run yet; the staged workload gates remain
reserved for the policy-interface, persistent-executor, adaptive-policy, and
final-tuning steps below.

### Step 2 Typed Grant Unit Inventory

Started on June 8, 2026. The existing `HomerProgressGrant` fields are sufficient
for the first persistent-plan executor: `maxPolls`, `maxItems`, `maxPumpCalls`,
and source-specific `flags` already cover the CPU-work units currently needed by
CQ drain, command rings, completion rings, local control, split peer-control
phases, payload streams, heartbeat, and target completion. The source-local unit
mapping is now documented beside
[`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1417).

Decision: do not introduce a separate grant-unit enum yet. A common enum would
look cleaner, but the executor still needs typed source-local fields because
poll batches, mailbox messages, completion entries, local control slots, payload
pump calls, and egress bytes/objects/WRs are not interchangeable. If a future
policy wants abstract CPU tokens, it should translate them into these typed grant
fields before installing an execution plan.

Verification for this slice: the same Citus service/client build command
completed successfully after formatting. No runtime workload was needed because
this step only documented/normalized the existing grant contract and did not
change executor behavior beyond the peer-control split from Step 1.

### Step 3 Execution-Plan Scaffold

Started on June 8, 2026. The code now defines
[`HomerProgressPlanStopState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1459)
and
[`HomerProgressExecutionPlan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1474).
`TupleSinkServicePumpOnce()` still builds the existing source plan, installs it
into a stack-local execution plan with
`HomerProgressExecutionPlanInstallSourcePlan()`, and consumes it immediately with
a cursor.

Important caveat: this is only the persistent-plan scaffold. The installed plan
currently contains one default grant per selected source, and source-specific
grant construction still happens inside the selected executor. This preserves the
current behavior while putting the service loop onto the future list+cursor
shape. The later policy-interface and plan-executor slices should move more
typed grant construction into policy/build-plan code and start updating
`HomerProgressPlanStopState` from grant-boundary feedback.

Verification for this slice: `sudo -n -u dbcomm make -j8 service-bin
client-bin` completed successfully after formatting. Runtime correctness and
performance verification should happen after the policy-interface migration and
again after the persistent executor starts changing plan lifetime/early-stop
behavior, as recorded in the staged validation plan.

### Step 4 Policy Ops Interface

Started on June 8, 2026. Existing policies now go through
[`HomerProgressPolicyOps`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1546).
`HomerProgressPolicy` keeps `kind` for environment selection, logs, and stats,
but the source-plan builder now calls `progressPolicy->ops->buildPlan` instead
of switching directly on `kind`. The ops table also includes no-op
`onGrantResult`, `onPlanEnd`, and `onPlanHint` hooks so later persistent-plan and
preemption slices can add behavior without changing the policy object shape
again.

Important caveat: the hint/preemption callback is only part of the interface in
this slice. It is not invoked yet, and all installed policies use no-op
callbacks. This keeps Step 4 interface-only; plan lifetime, stop-and-replan, and
in-place repair remain Step 5+ work.

Implementation detail: `HomerProgressResult` needed a forward declaration for
the callback surface, so it became a tagged struct while preserving the same
fields. `HomerServiceReadProgressPolicyEnv()` now installs both `kind` and `ops`
through `HomerServiceSetProgressPolicy()`.

Verification for this slice:

- `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  was run.
- `sudo -n -u dbcomm make -j8 service-bin client-bin` completed successfully.
- `sudo -n make install-headers install-service-bin install` completed locally.
- Installed `citus_tuple_sink_service` and `citus.so` were synced to `farnet0`
  through a remote staging directory because broad prefix `rsync --delete`
  encountered root-owned log/header/library permission errors. Final hashes
  matched on both hosts:
  `citus_tuple_sink_service=d0bc2c834b3932f6dc0efe220c0ebeca187a2f6d770cd40184c482b1c503228b`,
  `citus.so=52927dafec8e99e8a6bc6136ff6026872a3e8cb3dc32d5d93b953b42a59e98d0`.
- Clean runtime preflight before measurement: after restart, `farnet1` had
  PostgreSQL plus one `citus_tuple_sink_service`; `farnet0` had one
  `citus_tuple_sink_service`.
- Remote Homer pgbench c1, default fixed-priority policy, artifacts in
  `/tmp/homer_step4_pgbench_c1_1780945203`: warmup completed `10000/10000`,
  zero failures, `2675.981222 TPS`; warmed repeats completed `10000/10000`,
  zero failures, `4685.150836 TPS` with p99 `0.243 ms`, then `4247.238552 TPS`
  with p99 `0.285 ms`.
- Remote RDMA basebackup, artifacts in
  `/tmp/homer_step4_basebackup_1780945229`: three successful repeats, `5.98s`
  warmup, then `4.62s` and `4.66s` warmed.
- Post-run preflight showed only PostgreSQL plus one service on `farnet1` and
  one service on `farnet0`. Recent service-log checks found no matching
  failed/error/resetting/broken/timed-out/timeout/invalid/stale signatures.

### Step 5 Generic Execution-Plan Loop

Started on June 8, 2026. The service loop now executes the installed execution
plan through
[`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:21449)
instead of open-coding the cursor loop inside `TupleSinkServicePumpOnce()`.
Grant-boundary accounting is centralized in
`HomerProgressExecutionPlanRecordGrantResult()`, which updates plan-local
`grantsExecuted`, productive, empty, and blocked streak counters. The executor
also calls policy `onGrantResult` after each top-level grant and `onPlanEnd`
when the cursor reaches the end.

Important caveat: this still preserves one-pass behavior. There are no adaptive
early-stop thresholds yet; the only stop reasons recorded in this slice are
cursor exhaustion and executor failure. The `onPlanHint` callback remains part of
the policy interface but is not invoked until the later cheap-hints/preemption
slice.

Implementation detail: the generic executor passes a stack-local
`HomerProgressResult` into `HomerServiceExecuteProgressSource()` for each
top-level grant. Source-specific executors still call `HomerServiceFinishProgressGrant()`
to update source feedback. The generic executor then uses the local result for
plan stop-state and policy callbacks, and merges it once into the outer loop
aggregate. This avoids double-updating per-source feedback while still giving the
policy source-neutral grant feedback.

Verification for this slice:

- `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  was run.
- `sudo -n -u dbcomm make -j8 service-bin client-bin` completed successfully.
- `sudo -n make install-service-bin` completed locally.
- Installed `citus_tuple_sink_service` was synced to `farnet0`; final service
  hash matched on both hosts:
  `09a9464fe23d3c0f902bfc9c3752c017e2ea763d07b4873c9f916d97d808efcc`.
- Remote Homer pgbench c1, default fixed-priority policy, artifacts in
  `/tmp/homer_step5_pgbench_c1_1780945441`: warmup completed `10000/10000`,
  zero failures, `2677.830721 TPS`; warmed repeats completed `10000/10000`,
  zero failures, `4698.157477 TPS` with p99 `0.231 ms`, then `4241.430719 TPS`
  with p99 `0.262 ms`.
- Remote RDMA basebackup, artifacts in
  `/tmp/homer_step5_basebackup_1780945464`: three successful repeats, `6.08s`
  warmup, then `4.54s` and `4.79s` warmed.
- Post-run preflight showed only PostgreSQL plus one service on `farnet1` and
  one service on `farnet0`. Recent service-log checks found no matching
  failed/error/resetting/broken/timed-out/timeout/invalid/stale signatures.

### Step 6 Cheap Feedback And Hint Diagnostics

Started on June 8, 2026. The scheduler feedback vocabulary now records cheap
source-local history in
[`HomerProgressSourceFeedback`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1416):
consecutive empty/productive/blocked grant streaks, the last granted
poll/item/pump-call budgets, last grant flags, `lastBlockedTick`, and
`lastGrantBudgetTick`. These fields are updated at the existing generic
completion boundary rather than inside payload-, command-, or peer-specific
executors.

Implementation detail: `HomerServiceUpdateProgressFeedback()` now updates
source-local streaks and blocked ticks from `HomerProgressResult`, while
[`HomerServiceRecordProgressGrantBudgetFeedback()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5904)
records what the scheduler offered for the same selected top-level grant. This
keeps "work used" and "work granted" comparable without adding heap allocation,
wall-clock reads, or a new source-table scan.

The execution-plan scaffold also records per-plan hint diagnostics in
[`HomerProgressPlanStopState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1478)
and computes `HomerProgressExecutionPlan.classMask` from the selected sources as
grants are appended in
[`HomerProgressExecutionPlanAppendGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4404).
The class mask uses the current dynamic source core when available, so payload
streams can carry the CPU/liveness class derived from traffic class.

Important caveat: this slice is still diagnostics/scaffold only. It does not add
adaptive early-stop thresholds, does not invoke hint/preemption decisions, and
does not change the default fixed-priority behavior. Those belong to the first
adaptive bounded-class policy in Step 7.

Verification for this slice:

- `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  was run.
- `sudo -n -u dbcomm make -j8 service-bin client-bin` completed successfully.
- No runtime performance gate was run for Step 6 because the staged plan only
  requires blocking correctness/performance gates after Steps 4, 5, 7, and 8.

### Step 7 Adaptive Policy Compatibility Baseline

Started on June 8, 2026. The policy enum and ops table now include opt-in
`HOMER_PROGRESS_POLICY=adaptive-bounded-class` through
[`HomerServiceBuildAdaptiveBoundedClassProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4996)
and
[`HomerAdaptiveBoundedClassPolicyPlanEnd()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5139).
This policy currently preserves fixed ready-set order and records policy-local
diagnostics (`lastPlanSourceCount`, `lastPlanClassMask`, executed/productive
grant counts, and stop reason mask).

Important caveat: true class reordering/admission was attempted and rejected by
the Step 7 gate. Several versions that reordered by CPU/liveness class, bounded
hot-control/cold classes, or preserved a local/peer-control front segment still
hung remote c1 pgbench before the first reported result. That is concrete
evidence that the current source facts are not yet sufficient to make arbitrary
first-layer class reordering safe: fixed-priority ready-set order is carrying
hidden liveness dependencies among local control, peer control, command, and
completion progress. The accepted Step 7 state is therefore a compatibility
baseline for the pluggable policy and policy-local diagnostics, not a completed
adaptive bounded-class scheduler.

Diagnostic caveat: `HOMER_PROGRESS_TRACE_PLANS=1` must not be used for acceptance
measurements. An early plan trace version logged every high-frequency
local-control continuation and consumed enough service CPU to make remote c1
pgbench appear hung. The trace filter now ignores single-source heartbeat,
peer-maintenance, and local-control continuation plans and preserves the bounded
trace window for multi-source or hot-data decisions in
[`HomerServiceProgressPlanTraceInteresting()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4382),
but trace runs remain diagnostic-only.

Implementation detail: adaptive policy does not enable split peer-control
sources. `HomerServiceUseSplitPeerControlSources()` keeps split peer-control
limited to the existing `cpu-liveness` policy until peer-control exposes
stronger per-phase readiness/liveness facts. Adaptive uses the aggregate
peer-control source to preserve the old all-phase pump semantics.

Verification for this slice:

- `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
  could not be applied late in the dirty milestone tree because the file already
  had unstaged changes from earlier slices; the targeted C build was used as the
  formatting/compile gate for the final Step 7 edit.
- `sudo -n -u dbcomm make -j8 service-bin client-bin` completed successfully
  after the explicit compatibility fallback.
- Installed `citus_tuple_sink_service` was synced to `farnet0` with remote sudo;
  final service hash matched on both hosts:
  `2e9410ec592b4e46ecd27e2f1fdb2fa1b98bccb47f9f391ba1b3742e853c7ec2`.
- Default fixed-priority sanity check after the Step 7 code changes, artifacts
  in `/tmp/homer_step7_default_pgbench_c1_1780946231`: warmup completed
  `10000/10000`, zero failures, `2672.759846 TPS`; warmed repeats completed
  `10000/10000`, zero failures, `4672.508548 TPS` with p99 `0.232 ms`, then
  `4282.970737 TPS` with p99 `0.257 ms`.
- Default remote RDMA basebackup sanity check, artifacts in
  `/tmp/homer_step7_default_basebackup_1780946241`: three successful repeats,
  `6.09s` warmup, then `4.54s` and `4.72s` warmed.
- Adaptive compatibility smoke, artifacts in
  `/tmp/homer_step7_adaptive_smoke5_1780947021`: `1000/1000`, zero failures,
  p99 `0.333 ms`; the low `550.900750 TPS` was from a short cold run with
  `1774.641 ms` initial connection time.
- Adaptive compatibility remote c1, artifacts in
  `/tmp/homer_step7_adaptive_compat_final_pgbench_c1_1780948757`: `10000/10000`,
  zero failures on all repeats; `2676.079330 TPS` cold/warmup, then
  `4658.549300 TPS` with p99 `0.234 ms`, and `4273.005756 TPS` with p99
  `0.256 ms`.
- Adaptive compatibility remote RDMA basebackup, artifacts in
  `/tmp/homer_step7_adaptive_compat_final_basebackup_1780948781`: three
  successful repeats, `6.35s` warmup, then `4.57s` and `4.58s` warmed.
- Adaptive compatibility mixed c1 pgbench plus background remote RDMA
  basebackup, artifacts in `/tmp/homer_step7_adaptive_compat_final_mixed_c1_1780948813`:
  both commands returned zero; pgbench completed `10000/10000`, zero failures,
  `3612.352657 TPS`, p95 `0.395 ms`, p99 `0.464 ms`; basebackup completed in
  `4.91s`.
- Post-run preflight showed only PostgreSQL plus one service on `farnet1` and
  one service on `farnet0`. Recent service-log checks found no matching
  failed/error/resetting/broken/timed-out/timeout/invalid/stale signatures.

### Step 8 Final Validation And Tuning Gate

Completed on June 8, 2026 for the current implementation state. The validation
ran with `HOMER_PROGRESS_POLICY=adaptive-bounded-class`, but the accepted Step 7
code path is the adaptive compatibility baseline: it uses the pluggable policy
interface and policy-local diagnostics while preserving fixed-priority ready-set
order. True class reordering/admission remains future work because the Step 7
gate exposed remote c1 liveness failures when arbitrary first-layer reordering
was enabled.

Final binary hashes matched on `farnet1` and `farnet0`:

- `citus_tuple_sink_service`:
  `2e9410ec592b4e46ecd27e2f1fdb2fa1b98bccb47f9f391ba1b3742e853c7ec2`
- `citus.so`:
  `52927dafec8e99e8a6bc6136ff6026872a3e8cb3dc32d5d93b953b42a59e98d0`

Remote Homer pgbench artifacts were captured in
`/tmp/homer_step8_pgbench_remote_1780949008`:

- c1: warmup `2678.695797 TPS`, then warmed repeats `4670.448484 TPS`
  with p99 `0.233 ms`, and `4239.300007 TPS` with p99 `0.258 ms`; every run
  completed `10000/10000` transactions with zero failures.
- c4: warmed repeats `11374.642623`, `11418.640474`, and `11199.948480 TPS`;
  every run completed `40000/40000` transactions with zero failures, with p99 in
  the `0.563-0.574 ms` band.

Remote RDMA basebackup artifacts were captured in
`/tmp/homer_step8_basebackup_1780949048`: successful repeats `5.98s` warmup,
then `4.81s`, `4.57s`, and `4.56s` warmed. Each run completed the base backup.

Mixed foreground pgbench plus background remote RDMA basebackup also passed:

- c1 mixed artifacts in `/tmp/homer_step8_mixed_c1_1780949196`: pgbench
  completed `10000/10000` transactions with zero failures at `3596.981269 TPS`,
  p95 `0.387 ms`, p99 `0.451 ms`; basebackup completed in `4.68s`.
- c4 mixed artifacts in `/tmp/homer_step8_mixed_c4_1780949215`: pgbench
  completed `10000/10000` transactions with zero failures at `9433.828767 TPS`,
  p95 `0.561 ms`, p99 `0.679 ms`; basebackup completed in `4.61s`.

Backend-to-backend Citus tuple COPY through Homer passed with artifacts in
`/tmp/homer_step8_b2b_copy_10m_1780949268`. The table was recreated with one
shard placed on `10.10.1.100`. All four repeats copied `10,000,000` rows and
verified `count=10000000`, `min=1`, `max=10000000`, and
`sum=500000050000000`. The first run was cold at `10.60s`; warmed repeats were
`5.25s`, `5.09s`, and `5.12s`, which is in the current accepted Homer/vanilla
Citus parity band. Service logs confirmed this was the Homer service-to-service
byte-ring path with `published_tail=512005120`.

Final process preflight showed no stale `pgbench`, `pg_basebackup`, `psql`,
`walsender`, or `postgres: remote exec backend` processes. The only long-lived
processes were the intended PostgreSQL postmasters and `citus_tuple_sink_service`
instances on the hosts needed by the final tuple COPY run. Recent service-log
error-signature checks found no matching
`failed|error|resetting|broken|timed-out|timeout|invalid|stale` lines.

Acceptance conclusion: the scheduler scaffolding, pluggable policy interface,
generic execution-plan loop, feedback fields, and adaptive compatibility policy
are validated as scheduler-ready scaffolding. The milestone should not be read
as completion of a high-quality adaptive scheduling policy: the next research
step is to add stronger source dependency/liveness facts and then re-enable
bounded class reordering or another adaptive policy with the same Step 7/Step 8
gate discipline.

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
   matching binaries across hosts. The Step 4, Step 5, Step 7, and Step 8 gates
   are blocking gates: if they expose correctness or performance issues, pause
   the implementation sequence, diagnose the specific change that caused the
   issue, fix it, and rerun the relevant gate before moving on.

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
