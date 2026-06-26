# Homer Scheduler Redesign Plan: From Machine/Egress Split to Guarded Action Grants

## Audience and purpose

This document is for the Homer implementation team working on the current Citus/Postgres communication-stack prototype. It assumes familiarity with the codebase, current `machine-baseline` scheduler, payload streams, RDMA transport lanes, command/completion paths, and the existing progress/egress policy split.

The purpose is not to recap the code. The purpose is to define the next design direction:

> Move from a scheduler that is conceptually split into machine/collector progress plus a separate payload-egress policy, toward a unified scheduler over **guarded parameterized actions**.

The current state-machine work is not wrong. It solved an important modeling problem: Homer operations are multi-step asynchronous lifecycles, not one-shot queue entries. But the state machine should be treated as the **lifecycle/correctness representation**, not as the fundamental scheduling abstraction. The thing the scheduler should select is not “a machine” or “an egress subpolicy.” The thing the scheduler should select is:

```text
(action, grant)
```

Examples:

```text
poll peer listener once
collect up to 8 local control slots
drain up to 16 CQEs from foreground lane
publish up to 4 client completions
post up to 64 KiB / 4 WRs for foreground payload stream S
post up to 2 MiB / 16 WRs for bulk basebackup stream B
return receiver-head credit for stream R
advance close/reclaim for stream X
```

In this model, egress is not a separate scheduler layer. Egress RDMA posting is just another action family with a heterogeneous resource vector and a parameterized grant.

---

## 1. Design stance

The current code is already halfway to the right abstraction. It has explicit machines, collectors, action kinds, wait masks, blocked reasons, traffic classes, ready-byte/object hints, and grant budgets. These are valuable. But the current conceptual boundary still reflects the migration path:

```text
coarse ready set
  -> collector candidates
  -> machine candidates
  -> machine/collector action plan
  -> execute selected action
  -> payload execution asks second-layer egress policy for byte/object/WR grant
```

This should evolve toward:

```text
lifecycle/transport state
  -> guarded action candidates
  -> unified action/grant plan
  -> bounded executor calls
  -> typed progress feedback
```

The scheduler should see a homogeneous list of candidate actions. Each candidate can have different resource dimensions, but every candidate should have the same scheduler-facing shape:

```text
owner
kind
class
why it is eligible
known vs speculative/demanded
what it may unblock
feasible grant range
resource-cost vector
expected progress/effect hints
age/debt/starvation metadata
```

This is the line of thinking we should use going forward:

> **Homogeneous scheduler item, heterogeneous resource vector.**

---

## 2. Why the state-machine model was useful, and what it should not become

### 2.1 Why we needed lifecycle state

Homer work is multi-step and asynchronous. A client SQL command, a peer command, a tuple/result payload stream, and a basebackup stream all involve multiple stages where different actions become legal only after earlier observations or resource transitions.

A simplified client SQL / result path looks like this:

```text
frontend publishes START_COMMAND
  -> service claims/validates request
  -> service posts command to backend or peer
  -> backend publishes STARTED, possibly with result-sink metadata
  -> payload stream becomes active
  -> tuple/result bytes are produced and transported
  -> backend publishes terminal completion
  -> service waits until payload/EOS/source-release constraints are satisfied
  -> service publishes completion to frontend or peer
  -> service closes/reclaims stream/session state
```

At different moments, the useful next action may be:

```text
collect command slot
post command
poll or drain peer transport
consume backend completion
publish sink-ready metadata
post RDMA payload writes
drain send CQ
process receiver credit
publish terminal completion
close/reclaim resources
```

A plain FIFO queue or “ready source list” does not capture this. The service needs to know which actions are legal and why other actions are blocked. That is why explicit lifecycle state is useful.

### 2.2 The risk of making “state machines” the scheduler abstraction

The problem is that real payload/control objects often have several independent predicates true at once. A payload stream may simultaneously have:

```text
producer bytes ready
send CQ completions pending
remote credit unavailable
local source bytes not yet releasable
receiver ACK needed
EOS pending
close/reclaim pending
```

A textbook finite state machine forces these into either a large cross-product or vague states like `WAIT_CQ`, `BLOCKED`, `ACTIVE`, or `READY`. Those states are not enough for good scheduling. The scheduler needs to know the independent predicates and the legal actions.

Therefore, the better mental model is not a pure FSM. It is:

```text
lifecycle state + independent predicates + wait masks + guarded action candidates
```

The state machine remains valuable as an implementation discipline and correctness guard. But the scheduler should operate over actions derived from that state.

### 2.3 Desired phrasing

Avoid saying:

```text
Homer has a state-machine scheduler.
```

Prefer:

```text
Homer represents long-lived communication operations as asynchronous lifecycles that expose scheduler-visible guarded actions.
```

This is more general, more accurate, and less likely to overfit the paper/story to the current implementation.

---

## 3. The formal model we should use

The closest high-level formalization is:

> **A constrained partially observable polling/scheduling problem over guarded batch actions.**

The pieces are:

1. **Polling system:** one service loop visits many sources/objects.
2. **Partial observability:** some work is hidden until we poll, drain, or read a frontier.
3. **Guarded actions:** actions are legal only when lifecycle/resource guards are true.
4. **Batch service:** many actions have tunable grant size.
5. **Resource vectors:** actions consume different resources: CPU, CQEs, WRs, bytes, doorbells, remote credit, source-buffer lifetime, and future completion budget.

We do not need to solve an exact POMDP. The POMDP framing is useful because it explains why “unknown work” exists and why polling/notification choices are information-gathering actions.

### 3.1 State

For each owner/object `i`, define conceptual state:

```text
x_i = {
    lifecycle phase,
    frontiers,
    known backlog,
    hidden/latent work belief,
    resource state,
    class/priority metadata,
    age/debt/starvation metadata
}
```

Examples of frontiers:

```text
producer-published frontier
transport-posted frontier
transport-completion frontier
source-release frontier
semantic-object frontier
remote-credit frontier
receiver-consumed frontier
```

Examples of resource state:

```text
remote ring credit
local SQ/WQE pressure
CQ completion pressure
registered staging-buffer lifetime
source-ring reuse constraints
traffic-class lane/QP ownership
```

### 3.2 Actions

Each owner exposes action candidates:

```text
A_i(x_i) = { action candidates whose guards may be true }
```

An action candidate is:

```text
alpha = {
    owner,
    action kind,
    guard predicate,
    feasible grant range,
    resource-cost vector,
    expected effect/progress,
    class/urgency metadata
}
```

The scheduler chooses:

```text
plan = [ (action_1, grant_1), ..., (action_k, grant_k) ]
```

subject to per-pass budgets.

### 3.3 Grants as parameterized actions

A grant parameterizes an action:

```text
polls
items
CQEs
WRs
objects
fragments
bytes
signal/checkpoint policy
```

This is the key point for removing the egress split. Egress is not special in the scheduler model:

```text
POST_RDMA_PAYLOAD(stream, bytes=N, wrs=M, objects=K, signal=policy)
```

is just one action/grant pair.

### 3.4 Unknown work

Unknown work is represented as an observation action.

Known work example:

```text
guard: producer_tail > posted_tail
action: post RDMA payload
```

Unknown/speculative work example:

```text
guard: listener_poll_due OR belief(listener_has_event) high enough
action: poll listener once
```

If we later use notifications, the source of uncertainty changes, but scheduling does not disappear. Notification turns:

```text
blind poll to discover work
```

into:

```text
event arrived; choose when and how much to drain/process
```

The scheduler still chooses among event drain, CQ drain, command publication, completion publication, payload egress, credit return, and close/reclaim.


### 3.5 What the POMDP framing means for implementation

The POMDP framing is a **conceptual model**, not a proposal to implement a POMDP solver in the Homer hot path.

We should not implement:

```text
large global state vector
explicit transition probability matrix
belief distribution over all hidden states
dynamic programming / value iteration / online POMDP planning
```

That would be far too expensive, brittle, and unnecessary for this codebase.

Instead, the POMDP-like view tells us how to name and approximate the problem:

```text
observed state        -> current lifecycle/frontier/resource facts
hidden state          -> remote work or completion state not yet observed
observation action    -> poll/read/drain/arm operation that reveals hidden state
belief approximation  -> cooldowns, recent empty/productive counts, due bits,
                         ready-since age, waiting-machine demand, workload hints
policy                -> cheap heuristic over action candidates and grant ranges
```

So the implementation target is still a fixed-array, switch-based service loop. The change is that we make the hidden/known distinction and observation cost explicit in the action metadata.

The practical mapping is:

```text
POMDP state          => lifecycle + frontier + resource facts
POMDP hidden state   => possible unobserved arrivals/completions/remote writes
POMDP observation    => collector/observation action result
POMDP belief         => small per-source feedback counters and due/backoff state
POMDP action         => HomerActionGrant
POMDP reward/cost    => typed progress, latency relief, resource relief, empty-poll cost,
                         HOL risk, grant utilization
```

This gives us a disciplined vocabulary without requiring a heavyweight control-theory implementation.

### 3.6 What happens to state machines and event collectors

State machines and event collectors do **not** disappear in the guarded-action model. They are reinterpreted with cleaner roles.

#### State machines / lifecycles

State machines remain the correctness representation for long-lived async protocol objects. A session, stream, peer op, or command lifecycle owns the authoritative state that determines which actions are legal.

In the target model, a lifecycle does not ask the scheduler to run “the machine” in the abstract. Instead, it exports zero or more guarded action candidates:

```text
lifecycle state + frontiers + resource facts
    -> candidate: POST_RDMA_PAYLOAD(stream, feasible grant range)
    -> candidate: DRAIN_OR_PROCESS_COMPLETION(session, max items)
    -> candidate: PUBLISH_CLIENT_COMPLETION(session, max events)
    -> candidate: ADVANCE_CLOSE_RECLAIM(stream, max steps)
```

The lifecycle is still responsible for correctness:

```text
which transitions are legal
which prerequisites must hold before publication/release
which generation/owner checks must pass
which DB-visible ordering constraints must be preserved
which resource/frontier updates are authoritative
```

The scheduler is responsible for allocation:

```text
which legal action gets a grant
how large the grant is
which class/order/debt rule applies
how to trade foreground latency against bulk throughput and resource relief
```

This is the desired separation:

```text
state machine/lifecycle = legal action producer + correctness owner
scheduler               = action/grant allocator
executor                = guard revalidator + bounded transition applier
```

#### Event collectors / observation actions

Collectors also remain. They become the implementation of **observation actions**.

Some actions advance known work:

```text
producer bytes are known ready -> POST_RDMA_PAYLOAD
completion record is known ready -> PUBLISH_COMPLETION
source-release completion is known pending -> DRAIN_CQ
```

Other actions discover whether work exists:

```text
listener may have incoming setup -> OBSERVE_PEER_LISTENER
peer mailbox may contain remote request -> OBSERVE_PEER_MAILBOX
shared-memory command ring may have new command -> OBSERVE_COMMAND_RING
CQ may contain completions -> DRAIN_CQ / OBSERVE_CQ
```

A collector is therefore not a separate conceptual scheduler object. It is an action candidate with metadata:

```text
kind = OBSERVE_* or DRAIN_CQ
knownWork = true/false
speculativeObservation = true/false
dependencyUnblocker = true/false
waitingMachineCount = number of lifecycles blocked on this observation
recentEmptyCount / recentProductiveCount = cheap belief approximation
candidateDue / cooldown = observation admission control
```

The policy can then compare observation actions against other work using the same plan builder:

```text
poll listener once
vs drain 8 CQEs
vs publish 4 completions
vs post 64 KiB foreground payload
vs post 2 MiB bulk payload
```

This preserves the current collector insight—unknown-arrival work needs explicit service—while removing the artificial boundary that makes collectors feel like a separate first-class world from actions.

### 3.7 The intended implementation stack

The target stack should look like this:

```text
fixed-table lifecycle objects
    own authoritative protocol state and frontiers
    expose guarded candidate actions

collector/observation helpers
    expose candidates that may reveal hidden work
    maintain cheap belief/backoff/due feedback

candidate builder
    creates a bounded homogeneous list of action candidates

unified scheduler policy
    chooses ordered action/grant pairs

specialized executors
    revalidate guards and apply bounded protocol-specific transitions

feedback fold
    updates lifecycle facts, observation feedback, ready bits, age/debt,
    and frontier/resource pressure hints
```

This means the code still contains state machines and collectors. What changes is the scheduler boundary: the scheduler should see actions, not “machines versus collectors versus egress.”

---

## 4. What is not good enough today

This section is intentionally critical. It does not mean the current work was wrong; it means the current boundary is an intermediate implementation shape, not the final model.

### 4.1 Actions are not first-class enough

Current machine facts and collector facts already expose many of the right ingredients: ready action masks, blocked reasons, waiting masks, known expected work, candidate due state, traffic class, and ready byte/object hints. But the policy still largely reasons through machine/collector families and then compiles actions.

The improved model should construct action candidates directly:

```text
candidate: DRAIN_CQ foreground lane up to N CQEs
candidate: POST_RDMA bulk stream B up to M bytes / W WRs
candidate: PUBLISH_COMPLETION session S up to 1 event
candidate: POLL_PEER_LISTENER once, speculative/due
candidate: RETURN_CREDIT stream R up to one ACK publication
```

The machine/collector structures can remain as producers of candidates. They should not be the final policy object.

### 4.2 The egress split causes locally good but globally bad decisions

The current split is:

```text
first layer chooses source/action
payload execution then asks egress policy for byte/object/WR grant
```

This can make the wrong global decision because the egress grant is only considered after the payload action has already been selected. A second-layer egress policy cannot choose to drain a CQ, publish a completion, poll listener, or service another foreground payload first. It can only size the payload work that the first layer admitted.

A unified scheduler should be able to compare:

```text
post 2 MiB bulk payload
vs drain 16 CQEs
vs publish 4 foreground completions
vs post 64 KiB foreground payload
vs poll peer listener once
```

in one plan.

### 4.3 Ordering alone does not solve HOL; grant size matters

It is correct that egress is “just another action” from the service CPU perspective. But it has a special downstream property: once a WR chain is posted to an ordered QP/lane, later work on that same lane cannot jump ahead.

Therefore HOL is controlled by:

```text
which egress action is selected
how large the grant is
which traffic-class lane/QP it maps to
how often the scheduler can re-evaluate
```

This is why egress must be in the unified action model as a parameterized action, not as a binary “run payload source” decision.

### 4.4 CQ drain must become a normal action

Completion handling should not be an incidental side effect of whichever owner happened to call a poll helper. CQ drain should be lane-owned and scheduler-visible:

```text
DRAIN_CQ(lane, max_cqes=N)
```

The result should update owner frontiers directly:

```text
transport completion advanced
source release may now be legal
local WR resources freed
errors observed
blocked egress actions unblocked
```

This avoids hidden resource progress and makes completion pressure visible to the scheduler.

### 4.5 Observation work should be explicit action metadata

Current known/blind/due collector handling is useful but should be represented more uniformly. Observation actions should carry metadata:

```text
knownWork = false
speculativeObservation = true
candidateDue = true
waitingMachineCount = K
recentEmptyPolls = E
recentProductivePolls = P
belief/cooldown score = optional
```

This lets a policy reason about observation work the same way it reasons about other work: cost, expected value, starvation, and dependency unblocking.

### 4.6 “Traffic class” is not enough

Traffic classes and physical lanes reduce some QP-level HOL, but they do not by themselves solve service CPU scheduling, CQ drain ordering, grant sizing, or foreground/bulk fairness.

The scheduler should use traffic class as one input to action choice, not as the whole policy.

---

## 5. Target scheduler interface

### 5.1 Candidate type

A future candidate type should look roughly like this. Names are illustrative; the point is the shape.

```c
typedef enum HomerActionKind
{
    HOMER_ACTION_INVALID = 0,

    HOMER_ACTION_OBSERVE_LOCAL_CONTROL,
    HOMER_ACTION_OBSERVE_COMMAND_RING,
    HOMER_ACTION_OBSERVE_COMPLETION_RING,
    HOMER_ACTION_OBSERVE_PEER_LISTENER,
    HOMER_ACTION_OBSERVE_PEER_MAILBOX,

    HOMER_ACTION_DRAIN_CQ,
    HOMER_ACTION_POST_COMMAND,
    HOMER_ACTION_PUBLISH_COMPLETION,
    HOMER_ACTION_POST_RDMA_PAYLOAD,
    HOMER_ACTION_POST_PEER_CONTROL,
    HOMER_ACTION_RETURN_CREDIT,
    HOMER_ACTION_ADVANCE_CLOSE_RECLAIM,
    HOMER_ACTION_RUN_MAINTENANCE
} HomerActionKind;
```

```c
typedef struct HomerGrantRange
{
    uint16_t minPolls;
    uint16_t maxPolls;
    uint16_t minItems;
    uint16_t maxItems;
    uint16_t minCqes;
    uint16_t maxCqes;
    uint16_t minWrs;
    uint16_t maxWrs;
    uint32_t minObjects;
    uint32_t maxObjects;
    uint32_t minFragments;
    uint32_t maxFragments;
    uint64_t minBytes;
    uint64_t maxBytes;
} HomerGrantRange;
```

```c
typedef enum HomerSignalPolicy
{
    HOMER_SIGNAL_POLICY_NONE = 0,
    HOMER_SIGNAL_POLICY_GRANT_BOUNDARY,
    HOMER_SIGNAL_POLICY_EVERY_N_WRS,
    HOMER_SIGNAL_POLICY_EVERY_N_BYTES,
    HOMER_SIGNAL_POLICY_CLOSE_OR_RECLAIM,
    HOMER_SIGNAL_POLICY_RESOURCE_PRESSURE
} HomerSignalPolicy;
```

```c
typedef struct HomerResourceCostHint
{
    uint32_t cpuCost;
    uint32_t pollCost;
    uint32_t cqDrainCost;
    uint32_t wrCost;
    uint32_t doorbellCost;
    uint32_t futureCompletionCost;
    uint64_t byteCost;
    uint64_t remoteCreditCost;
    uint64_t sourceLifetimeCost;
} HomerResourceCostHint;
```

```c
typedef struct HomerActionCandidate
{
    HomerActionKind kind;
    HomerOwnerRef owner;

    HomerProgressCpuClass cpuClass;
    HomerTransportTrafficClass trafficClass;

    bool guardKnownTrue;
    bool knownWork;
    bool speculativeObservation;
    bool dependencyUnblocker;
    bool resourceRelief;
    bool latencySensitive;
    bool nonPreemptiveAfterPost;

    uint16_t waitingMachineCount;
    uint16_t recentEmptyCount;
    uint16_t recentProductiveCount;

    uint64_t readySinceTick;
    uint64_t lastTouchedTick;
    uint64_t lastProgressTick;
    uint64_t ageOrDebt;

    HomerGrantRange feasible;
    HomerResourceCostHint costHint;
    HomerSignalPolicy signalPolicyHint;

    HomerFrontierSnapshot frontierHint;
} HomerActionCandidate;
```

The candidate should not contain a heap work item or copied payload metadata. It should contain a typed owner reference and cheap facts. The executor must revalidate authoritative state before doing real work.

### 5.2 Grant type

```c
typedef struct HomerActionGrant
{
    HomerActionCandidateRef candidate;

    uint16_t maxPolls;
    uint16_t maxItems;
    uint16_t maxCqes;
    uint16_t maxWrs;
    uint32_t maxObjects;
    uint32_t maxFragments;
    uint64_t maxBytes;

    HomerSignalPolicy signalPolicy;
    uint32_t signalEveryNWrs;
    uint64_t signalEveryNBytes;
} HomerActionGrant;
```

For non-egress actions, unused fields are zero. For egress actions, byte/object/WR/fragment/signal fields are meaningful.

### 5.3 Result type

The executor should return typed progress, not just a boolean.

```c
typedef struct HomerActionResult
{
    bool stillEligible;
    bool guardBecameFalse;
    bool blockedOnCredit;
    bool blockedOnLocalResources;
    bool discoveredWork;
    bool madeSemanticProgress;
    bool madeResourceProgress;
    bool failed;

    uint16_t pollsAttempted;
    uint16_t emptyPolls;
    uint16_t itemsProcessed;
    uint16_t cqesDrained;
    uint16_t wrsPosted;
    uint32_t objectsPosted;
    uint32_t fragmentsPosted;
    uint64_t bytesPosted;

    HomerFrontierDelta frontierDelta;
} HomerActionResult;
```

The distinction between semantic progress and resource progress matters. Draining a CQ may not advance a semantic object frontier, but it can release local resources and unblock future egress. That should count as useful progress.

---

## 6. Unified service-loop shape

The target service pass should look like:

```text
1. Refresh cheap global/lane facts.
2. Build bounded action candidates from lifecycle/transport owners.
3. Policy selects an ordered fixed-size action/grant plan.
4. Execute grants through specialized executors.
5. Fold typed results into owner/lane feedback.
6. Update ready bits, cooldowns, age/debt, and frontier hints.
7. Derive idle/backoff/sleep decision from typed results.
```

Pseudo-code:

```c
static bool
HomerServicePumpOnceUnified(...)
{
    HomerActionCandidateSet candidates;
    HomerActionPlan plan;
    HomerActionPassResult passResult;

    HomerActionCandidateSetReset(&candidates);
    HomerActionPlanReset(&plan);
    HomerActionPassResultReset(&passResult);

    HomerRefreshCheapFacts(...);

    HomerBuildObservationActionCandidates(..., &candidates);
    HomerBuildCommandActionCandidates(..., &candidates);
    HomerBuildCompletionActionCandidates(..., &candidates);
    HomerBuildPeerActionCandidates(..., &candidates);
    HomerBuildPayloadActionCandidates(..., &candidates);
    HomerBuildCqDrainActionCandidates(..., &candidates);
    HomerBuildMaintenanceActionCandidates(..., &candidates);

    HomerActionPolicyBuildPlan(&ActionPolicy, &candidates, &plan);

    for each grant in plan:
        result = HomerExecuteActionGrant(grant);
        HomerFoldActionResult(grant, result);
        HomerActionPassResultAccumulate(&passResult, result);

        if (result.discoveredHighPriorityWork && policy wants stop-and-replan):
            break;

    HomerUpdateIdleBackoff(&passResult);
    return !passResult.failed;
}
```

Specialized executors remain specialized. We are not trying to create one generic command/payload/control executor. The unification is at the scheduler interface.

---

## 7. Migration plan

The migration should be staged so we can preserve correctness and isolate performance changes.

### Milestone 0: Write down invariants and freeze vocabulary

Before changing behavior, define invariants:

```text
I1. Executors revalidate guards and generation before touching owner state.
I2. No action may publish DB-visible completion before lifecycle prerequisites.
I3. No egress action may post beyond remote credit or local resource limits.
I4. No source bytes/slots may be released before required completion/ACK evidence.
I5. Observation actions must be bounded and must not be suppressible forever.
I6. Close/reclaim must not starve behind foreground/bulk work.
I7. Candidate building and planning must not allocate heap memory in the hot pass.
I8. Dropped/overflowed candidates must be counted and diagnosable.
```

Also decide naming:

```text
lifecycle object
owner
candidate
action
grant
resource vector
frontier delta
observation action
known work
speculative observation
dependency-unblocking action
resource-relief action
```

### Milestone 1: Introduce action-candidate structs without behavior change

Add `HomerActionKind`, `HomerActionCandidate`, `HomerActionGrant`, `HomerActionResult`, and candidate/plan arrays. Keep them fixed-size and stack-local.

Do not remove existing machine/collector/source-plan structures yet.

Add a bridge:

```text
current machine/collector candidate set
  -> action candidate set
```

Initially, the bridge can emit action candidates that exactly correspond to current action grants. The selected plan should mimic `machine-baseline` ordering.

Success criterion:

```text
same behavior and same validation results as current default
no measurable overhead beyond noise, or overhead is understood
```

### Milestone 2: Make action plan the common execution format

Currently, older source plans and newer machine action plans coexist. Move toward one execution format:

```text
HomerActionPlan = ordered action/grant array
```

Old source policies can compile to action grants through a compatibility builder. Current machine-baseline can compile through a direct builder.

The purpose is to make all execution go through:

```text
HomerExecuteActionGrant()
```

with a switch on `HomerActionKind`.

### Milestone 3: Convert payload egress to `POST_RDMA_PAYLOAD` action grants

This is the key conceptual change.

Current shape:

```text
first-layer selects payload action
payload executor builds egress ready facts
egress policy builds transport grant
payload executor posts RDMA
```

Target shape:

```text
candidate builder emits POST_RDMA_PAYLOAD with feasible grant range
unified policy chooses POST_RDMA_PAYLOAD and grant size
payload executor revalidates and posts RDMA within grant
```

The old egress policy should become a helper, not an independent decision-maker:

```c
HomerComputePayloadFeasibleGrant(stream, facts, &candidate.feasible, &costHint)
```

The candidate should carry cheap facts only:

```text
traffic class
ready bytes/objects hints
remote credit estimate
outstanding WR/CQE pressure
source-release pressure
object family / byte-ring flag if needed
```

The scheduler should not parse payload object headers. The executor remains responsible for exact record validation and posting.

Success criterion:

```text
fixed action policy with old fixed-default grants reproduces current behavior
ready-hints behavior can be reproduced as a unified policy variant
payload no longer calls a separate scheduler after being selected
```

### Milestone 4: Make CQ drain lane-owned and action-visible

Add candidates:

```text
DRAIN_CQ(critical_control_lane, max_cqes=N)
DRAIN_CQ(foreground_payload_lane, max_cqes=N)
DRAIN_CQ(bulk_payload_lane, max_cqes=N)
```

The drain executor should:

```text
poll up to N CQEs from the lane
parse tagged WR IDs
dispatch completion to owner state
advance transport-completion frontier
release local resource/staging pressure
mark owner candidates/ready bits accordingly
report typed result
```

This removes owner-specific CQ polling from the normal path where possible.

This milestone also prepares for sparse signaled-checkpoint behavior:

```text
most WRs unsignaled
signaled checkpoint at grant boundary or resource threshold
completion token retires cumulative prefix
```

Do not make this mandatory in the same patch unless necessary; lane-owned CQ drain is already a significant boundary cleanup.

### Milestone 5: Represent observation work uniformly

Observation actions include:

```text
poll listener / RDMA-CM
poll peer recv CQ or notification path
poll peer mailbox frontier
scan local control slots
scan completion rings
read shared-memory ready/frontier words
```

Each should be represented as a candidate with:

```text
knownWork = true/false
speculativeObservation = true/false
candidateDue = true/false
waitingMachineCount
recentEmptyCount
recentProductiveCount
cooldown/belief score
```

The policy can then treat observation work using a general rule:

```text
if dependency-unblocking: high priority
else if known work: normal priority by class
else if speculative: admit only within blind/observation budget and cooldown
```

Notifications, if introduced later, become another observation source. They do not change the action model.

### Milestone 6: Implement first unified policies

Start with policies that are easy to reason about.

#### Policy A: fixed bounded action policy

Purpose: behavior preservation.

```text
critical control observation/drain/publish
foreground command/completion
foreground payload
bulk payload
maintenance/setup observation
```

Use conservative per-action grants.

#### Policy B: priority + limited service

Purpose: avoid foreground HOL without starving bulk.

```text
critical control: strict or near-strict priority
foreground: bounded high priority
bulk: large but capped grants
maintenance/setup: bounded, age-protected
```

#### Policy C: deficit over resource vectors

Purpose: fairness and tunable throughput/latency.

Use deficits in one or more units:

```text
CPU/pump calls
bytes
WRs
CQEs
```

Do not pretend one unit captures all costs. Start with bytes/WRs for egress actions and action-count/CQE units for non-egress actions.

#### Policy D: feedback-driven observation backoff

Purpose: reduce empty polling.

Observation candidates with repeated empty results should cool down unless:

```text
known work appears
a waiting machine demands the collector
source age exceeds starvation bound
service is otherwise idle
```

### Milestone 7: Remove the old conceptual split

After the unified action path validates:

```text
remove separate payload-egress policy as a decision layer
keep grant-estimation helpers
simplify machine/collector bridges
rename docs/comments away from two-layer scheduler as conceptual model
keep state-machine/lifecycle code as candidate producers
```

The final architecture should say:

```text
lifecycles expose guarded actions
scheduler selects action/grant plan
executors perform bounded action work
```

---

## 8. Policy rules of thumb

The exact policy can evolve, but these principles should hold.

### 8.1 Dependency-unblocking before dependent work

If a machine is blocked on a collector/action, schedule the unblocker before retrying the blocked action.

Examples:

```text
blocked on send CQ -> drain CQ before posting more or publishing release-dependent completion
blocked on remote credit -> process receiver ACK/credit before retrying egress
blocked on backend completion -> consume completion before publishing client state
blocked on listener/CM event -> observation action must remain admissible
```

### 8.2 Resource-relief actions are useful progress

Draining CQEs, processing ACKs, and reclaiming staging/source resources may not advance DB-visible semantics immediately. They still reduce future blocking and should be schedulable as useful work.

### 8.3 Egress grant size is a scheduling decision

Ordering alone does not prevent HOL. Grant size controls how much non-preemptive work is injected into the transport.

For payload egress, the grant should include:

```text
max bytes
max objects
max fragments
max WRs
signal/checkpoint behavior
```

### 8.4 Unknown work should have bounded but nonzero service

Speculative observation should be budgeted and feedback-driven, but not suppressible forever. Unknown-arrival discovery is part of correctness and tail latency.

### 8.5 Keep the scheduler cheap

Candidate building and policy selection should remain fixed-array, bounded, and switch-based. Avoid:

```text
heap work items per object
per-WR policy callbacks
large scans when ready bits/frontiers can narrow work
function-pointer vtables in hot loops unless measured acceptable
complex global optimization in the hot pass
```

---

## 9. Instrumentation needed for the new model

The unified model is only useful if we can see what it is doing. Add counters at candidate, grant, and result levels.

### 9.1 Candidate counters

```text
candidates built by action kind
candidates built by traffic class
known vs speculative candidates
candidate overflows/drops
candidates skipped due to guard false
candidates skipped due to cooldown
candidates marked dependency-unblocking
```

### 9.2 Grant counters

```text
grants selected by action kind/class
grant bytes/objects/WRs/CQEs/polls/items
grant utilization: requested vs actually used
bulk grant size distribution
foreground grant size distribution
signaled checkpoint frequency
```

### 9.3 Result counters

```text
empty polls
productive polls
CQEs drained
WRs posted
bytes posted
objects posted
completions published
frontier deltas by kind
blocked-on-credit counts
blocked-on-local-resource counts
guard-false revalidations
resource-relief-only progress
semantic progress
```

### 9.4 Latency and interference counters

```text
ready-to-grant delay by class/action
grant-to-execute delay by class/action
foreground completion wait behind bulk grants
time blocked on CQ drain
time blocked on remote credit
time blocked on source release
age/debt at grant time
```

This instrumentation is how we prove whether the unified action model is better than the current split.

---

## 10. Validation plan

Use staged validation. Each milestone should have correctness, no-regression, and targeted stress tests.

### 10.1 Correctness gates

```text
remote c1/c4 Homer pgbench
remote RDMA basebackup
mixed pgbench + basebackup
backend-to-backend Citus tuple COPY
cold setup/reconnect/listener path
forced close/reclaim paths
service log scan for error/timeout/reset/stale/panic patterns
```

### 10.2 Performance gates

```text
foreground-only latency/TPS should not regress unexpectedly
bulk-only basebackup should stay in warmed steady-state band
mixed workload should improve or expose actionable counters
empty polling should decrease or be justified by lower latency
CQ drain should become more predictable and less owner-stashed
bulk grant distributions should show bounded non-preemptive work
```

### 10.3 A/B comparisons

Keep these policy shapes available:

```text
old machine-baseline behavior
unified fixed bounded behavior
unified priority + limited-service behavior
unified deficit/resource-vector behavior
unified observation-backoff behavior
```

The first goal is not to invent the perfect policy. The first goal is to make the policy surface correct: one scheduler selecting action/grant pairs.

---

## 11. How to explain this in the paper or design docs

Do not claim the research contribution is “two scheduler layers.” It is not the right conceptual claim.

A better framing is:

> Homer externalizes database communication progress into a service. Long-lived database communication objects are represented as asynchronous lifecycles that expose guarded, parameterized actions. The scheduler allocates service CPU and RDMA transport resources by selecting bounded action/grant pairs. This lets Homer reason uniformly about known work, speculative observation, CQ/resource relief, command/completion progress, and RDMA egress batching.

State machines become an implementation detail:

> We implement lifecycles as fixed-table state machines with explicit wait masks and ready-action predicates.

The egress split becomes an implementation history:

> Early prototypes factored payload-egress grant sizing from service-progress ordering for experimentation. The final abstraction treats egress as a normal guarded action with a resource-vector grant.

---

## 12. Short checklist for the next design session

Before modifying code, agree on these answers:

1. What is the first concrete `HomerActionCandidate` shape?
2. Which existing machine/collector facts map directly into candidate metadata?
3. Which action kinds are needed for the first no-behavior-change bridge?
4. How does current payload egress grant become a `POST_RDMA_PAYLOAD` grant?
5. Which fields are required for grant sizing but too expensive to compute for every stream?
6. What is the candidate-set bound and overflow behavior?
7. What is the first unified fixed policy order?
8. Which CQ drains can become lane-owned first?
9. Which old egress policy code becomes a helper rather than a scheduler?
10. What counters prove that the new model is behaving as intended?

---

## 13. Bottom line

The current code has the right raw ingredients, but the conceptual scheduler boundary should move.

Keep:

```text
explicit lifecycle state
fixed tables
ready-action masks
blocked/waiting masks
traffic classes
frontier facts
typed executors
bounded grants
```

Change:

```text
from scheduling machines/sources plus a separate egress layer
```

to:

```text
scheduling guarded action/grant pairs over homogeneous candidates with heterogeneous resource vectors
```

This gives us a cleaner theory, a cleaner implementation target, and a better policy surface for foreground latency, bulk throughput, CQ/resource relief, and polling/observation tradeoffs.
