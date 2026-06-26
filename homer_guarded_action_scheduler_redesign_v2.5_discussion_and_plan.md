The new plan is not a replacement of `homer_guarded_action_scheduler_redesign_v2.md`; it is more like a **v3 refinement**.

The main change is this:

> **v2 moved the design from “state-machine scheduler + egress split” to “guarded action/grant scheduler.”
> The new version narrows that into “bounded aggregate action candidates, not microscopic logical actions.”**

That is the biggest conceptual delta.

## 1. v2 was still too action-granular in spirit

In v2, the main message was:

```text
state machines stay
collectors stay
egress becomes a first-class action
scheduler chooses action + grant
```

That was correct, but it still left open an implementation path like:

```text
PAYLOAD_OUTGOING(stream)
PAYLOAD_SEND_CQ(stream)
PAYLOAD_CREDIT_PUBLISH(stream)
PAYLOAD_EOS(stream)
PAYLOAD_CLOSE_RECLAIM(stream)
COMMAND_SEND_CQ(session)
PAYLOAD_SEND_CQ(stream)
PEER_RECV_CQ(peer)
PEER_RESPONSE_MAILBOX(peer)
...
```

That is conceptually clean, but probably too granular.

The new plan says:

```text
Do not enumerate every micro-action.
Expose bounded aggregate service visits.
```

So the scheduler-visible unit becomes something like:

```text
ADVANCE_PAYLOAD_STREAM(stream, subactionMask, grantVector)
PUMP_PEER_TRANSPORT(phaseMask, grantVector)
DRAIN_SEND_CQ(lane/cq, grantVector)
```

rather than one candidate per small transition.

This directly addresses the developer’s coalescing point and your concern about candidate explosion.

## 2. `physicalKey/coalesceClass` was demoted

In v2, I suggested every logical candidate could have something like:

```text
physicalKey
coalesceClass
```

so logical actions could later be lowered/coalesced into physical executor calls.

After your pushback, I now think that should **not** be a first-slice hot-path mechanism.

The new stance is:

```text
Use explicit aggregate action kinds instead of generic coalescing metadata.
```

For example:

```text
PUMP_PEER_TRANSPORT(phaseMask)
```

is better than:

```text
six peer collector candidates
each with same physicalKey
later grouped by coalesceClass
```

The benefit is lower policy overhead, fewer candidate records, and less generic indirection.

So the new plan keeps the **principle** of coalescing, but implements it through aggregate actions, not a generic lowering framework.

## 3. Candidate set became candidate slate

v2 talked about a candidate set.

The new plan reframes this as a **bounded candidate slate**.

That is more precise. We do not want to build the universe of legal actions and then choose from it. We want to build a deliberately small scheduler slate:

```text
a few global/control candidates
a few CQ/lane candidates
top K foreground payload streams
top K bulk payload streams
hard-admitted close/resource-relief candidates
a few demanded/speculative observation candidates
```

This is important because it changes the overflow story.

In v2, overflow sounded like a problem to solve after candidates are emitted. In the new plan, the first defense is:

```text
choose aggregate candidates so overflow is rare by construction
then use reservations + spillover + overflow debt
```

So candidate overflow is no longer treated as “we need a sophisticated replacement algorithm.” It is treated as:

```text
avoid explosion first
reserve liveness/resource categories second
only add scoring/replacement later if measurement says we need it
```

## 4. The action taxonomy is now concrete

v2 described the model but did not really settle the list of scheduler-visible aggregate actions.

The new plan proposes a concrete first taxonomy:

```text
COLLECT_LOCAL_CONTROL
ADVANCE_LOCAL_CONTROL
COLLECT_COMMAND_INGRESS
ADVANCE_COMMAND_DISPATCH
COLLECT_BACKEND_COMPLETIONS
PUBLISH_COMMAND_COMPLETIONS
DRAIN_SEND_CQ
PUMP_PEER_TRANSPORT
ADVANCE_PEER_CONTROL_OP
REFRESH_PAYLOAD_FRONTIERS
ADVANCE_PAYLOAD_STREAM
ADVANCE_LIFETIME_RECLAIM
RUN_MAINTENANCE
```

The important change is not the exact names. The important change is the **granularity rule**:

```text
one candidate = one bounded service visit
not one candidate = one tiny transition
```

So `ADVANCE_PAYLOAD_STREAM` can include outgoing bytes, known completion application, credit publish, EOS, and close/reclaim under a subaction mask. `PUMP_PEER_TRANSPORT` can include selected peer phases. `DRAIN_SEND_CQ` is CQ/lane-owned, not per owner.

## 5. CQ drain is now treated as a resource-relief aggregate action

v2 said CQ drain should be first-class.

The new plan makes this more specific:

```text
DRAIN_SEND_CQ(lane/cq)
```

should be owned by the physical completion source, not by command/payload/control owners.

This matters because if one CQ can return mixed completions, scheduling “payload CQ drain” or “command CQ drain” is misleading. The actual action is:

```text
drain this CQ, route whatever tagged completions appear
```

That makes CQ drain a resource-relief/observation action in the same scheduler surface as payload posting and command completion publication.

## 6. The batching knobs are now explicitly separated and moved into grants

v2 said egress grants should become first-class.

The new plan separates four different knobs that were previously easy to conflate:

```text
1. local signaled-completion / CQE-production batching
2. CQ drain batching
3. RDMA posting / WR-descriptor batching
4. remote notification / WRITE_WITH_IMM publication batching
```

The new plan also says:

```text
Expose these in the grant surface, but default them to today’s behavior first.
```

So the first implementation does **not** need to be adaptive. It just makes the hidden fixed policy visible:

```text
maxBytes
maxObjects
maxWrs
maxFragments
maxDoorbells
maxPollBatches
maxCqes
localSignalPolicy
remoteNotifyPolicy
```

That is a major practical refinement over v2.

## 7. The grant object is now more concrete

v2 had the idea of action/grant pairs.

The new plan says the grant surface should unify:

```text
CPU/poll/message/CQE budgets
+
RDMA WR/object/byte/doorbell budgets
+
local signal policy
+
remote notify policy
```

So instead of:

```text
HomerProgressGrant for service progress
HomerTransportGrant for egress
```

the conceptual surface becomes:

```text
HomerActionGrant {
    action kind
    owner
    subaction mask
    HomerGrantVector
}
```

That is the concrete replacement for the two-layer split.

The old egress functions do not disappear. They become:

```text
candidate feasibility helpers
default grant/range helpers
executor-side clamp/revalidation helpers
```

but they stop being an independent second scheduler.

## 8. Plan repair/replan is now staged, not assumed

v2 leaned toward stop-and-replan as the clean reaction mechanism.

After the later discussion, the new plan is more cautious and staged:

```text
Stage A: no repair, just instrument discovered-work delay
Stage B: observation-heavy / observation-only short plans
Stage C: bounded stop-and-replan at grant boundary
Stage D: scheduler-owned hot continuation / repair surface
```

This is a real change.

The new plan recognizes that:

```text
stop-and-replan may be costly
full plan repair may become a hidden second scheduler
but no reaction may add foreground latency
```

So it proposes measurement first, then increasingly powerful mechanisms.

The newest addition is the idea that repair/replan can be treated as a scheduler-owned conditional continuation after observation:

```text
observation grant executes
executor reports discovered hot work
scheduler applies a second-time policy decision
scheduler may prepend/insert a bounded hot continuation grant
```

Executors still do not mutate plans. The scheduler owns the repair.

## 9. v2 was more conceptual; the new plan is implementation-staged

v2 was primarily a design argument:

```text
why state machines stay
why collectors are observation actions
why egress should be first-class
why guarded actions are the right abstraction
```

The new plan is a milestone plan:

```text
Stage 1: scaffolding and compatibility mode
Stage 2: move payload egress into candidate/grant planning
Stage 3: candidate slate + reservations
Stage 4: lane/CQ-owned send-CQ drain
Stage 5: observation-heavy policy mode
Stage 6: stop-and-replan
Stage 7: hot continuations / repair
Stage 8: adaptive batching policy
```

So the main difference is that it turns the abstraction into a migration path the developer can implement incrementally.

## 10. The research framing is sharper now

v2 framed the problem as:

```text
guarded actions over async lifecycles, with observation work and egress grants
```

The new plan ties that more directly to the paper contribution:

```text
avoid HOL for latency-sensitive command/control/completion work
while preserving bulk tuple/basebackup throughput
```

That sharpened the design choices:

```text
bounded bulk grants
early observation of command/completion sources
CQ/resource-relief priority
terminal visibility reservations
hot-discovery reaction mechanisms
traffic-class-aware payload grants
```

So the scheduler is not just a cleaner model. It has a concrete job:

```text
prevent small command/completion work from waiting behind large bulk transfer work,
without killing bulk throughput
```

## Condensed delta

If I had to summarize the delta from `v2` in one paragraph:

> `v2` established the right abstraction: state machines and collectors produce guarded actions, and egress becomes part of unified action/grant scheduling. The updated plan refines that into an implementable substrate: use **aggregate action candidates** rather than microscopic actions, avoid generic `physicalKey` lowering in the first slice, build a bounded **candidate slate** with reservations/spillover/debt, make CQ drains lane/CQ-owned, expose four batching knobs inside the unified grant vector, and stage low-latency reaction from instrumentation to observation-heavy plans to stop/replan to scheduler-owned hot continuations.


# Homer Unified Aggregate-Action Scheduler Plan

Below is the updated plan I would hand to the developer. It assumes the milestone branch already has the current state-machine scheduler baseline, peer phase bundling, payload egress facts/grants, and RDMA payload batching/checkpoint work.

## 0. Working thesis

The next scheduler milestone should move Homer from:

```text
machine/collector facts
    -> first-layer source/action plan
    -> separate payload egress grant decision
    -> executor
```

to:

```text
machine/collector/lane/stream facts
    -> bounded aggregate action candidate slate
    -> unified action+grant plan
    -> executor-side revalidation/clamping
    -> typed feedback
```

The key change is **not** “make every micro-action a candidate.” The key change is:

> Make every scheduler-visible unit a bounded aggregate service visit with a homogeneous grant surface.

That means candidates such as:

```text
PUMP_PEER_TRANSPORT(phaseMask)
DRAIN_SEND_CQ(lane/cq)
ADVANCE_PAYLOAD_STREAM(stream, subactionMask, grantVector)
COLLECT_COMMAND_INGRESS(...)
PUBLISH_COMMAND_COMPLETIONS(...)
```

rather than microscopic candidates like:

```text
PAYLOAD_SEND_CQ(stream)
PAYLOAD_POST_ONE_WR(stream)
PAYLOAD_PUBLISH_CREDIT(stream)
PAYLOAD_EOS(stream)
```

This preserves the guarded-action model while avoiding candidate explosion.

The current code already contains most of the ingredients: state-machine kinds, collector kinds, action kinds, split payload action masks, typed feedback frontiers, bounded grants, peer phase bundling, and egress ready facts.   The main design defect is that egress remains conceptually outside the first-layer grant: the current `HomerProgressGrant` comments say CPU/polling work is first-layer while transport byte/object/fragment grants remain a separate egress-scheduler concern. 

## 1. Milestone goal

The milestone goal should be:

> Build a unified aggregate-action scheduler substrate that makes Homer’s service CPU work, observation work, CQ/resource-relief work, and RDMA egress work schedulable through one bounded action/grant interface, while preserving the existing state-machine/lifecycle correctness model and avoiding hot-path candidate explosion.

The research-aligned performance goal is:

```text
avoid HOL for latency-sensitive command/control/completion work
while preserving high throughput for bulk tuple/basebackup payload transfer
```

This means the scheduler must control both:

```text
1. ordering:
   which aggregate service visits happen before which others

2. non-preemptive grant size:
   how much RDMA/CQ/bulk work is admitted per visit
```

Ordering alone is not enough if a bulk grant posts too much non-preemptive work to a QP/CQ/lane.

## 2. Non-goals for the first slice

Do **not** start by implementing:

```text
- a full POMDP solver
- a generic physicalKey/coalesceClass lowering framework
- one candidate per micro-action
- arbitrary plan insertion at any position
- adaptive basebackup/tuple semantic chunk sizing
- dynamic remote-notification policy changes
- complex bounded replacement scoring for candidate overflow
```

The POMDP framing remains conceptual: hidden work, observation actions, and cheap belief/backoff approximations. Implementation should stay fixed-table, bounded-array, switch-based, and cache-friendly.

## 3. State machines and collectors still stay

The new model should **not** delete state machines or collectors.

State machines/lifecycles remain the correctness owners:

```text
- legal phase transitions
- DB-visible ordering constraints
- stream/session/peer ownership
- close/reclaim preconditions
- source-release and semantic-frontier invariants
```

Collectors remain observation/fact-refresh work:

```text
- discover unknown external arrivals
- drain CQ/mailbox/CM/doorbell sources
- refresh payload frontiers
- unblock machines waiting on visibility
```

The change is that both become **candidate producers** for aggregate actions.

```text
state machine facts -> lifecycle action candidates
collector facts     -> observation/resource-relief candidates
lane facts          -> CQ drain/resource-relief candidates
stream facts        -> payload stream advance candidates
```

Collectors should no longer live in a separate scheduling universe. A collector grant is just an observation/resource-relief action candidate.

## 4. Aggregate action candidates

The first unified scheduler should expose a small set of aggregate action kinds.

### 4.1 `COLLECT_LOCAL_CONTROL`

Purpose:

```text
claim newly published local frontend/backend control slots
```

Why separate:

The current code explicitly separates local slot collection from async advancement to avoid spending a collector grant on all active continuations, or vice versa.  Keep that separation.

Grant dimensions:

```text
maxItems
maxPolls
```

### 4.2 `ADVANCE_LOCAL_CONTROL`

Purpose:

```text
advance already-claimed local control async work
```

Grant dimensions:

```text
maxItems
maxSteps
maxPumpCalls
```

### 4.3 `COLLECT_COMMAND_INGRESS`

Purpose:

```text
observe/claim client SQL command work from command ingress sources
```

Grant dimensions:

```text
maxItems
maxPolls
```

Class:

```text
critical or foreground
```

### 4.4 `ADVANCE_COMMAND_DISPATCH`

Purpose:

```text
advance command/session dispatch after ingress has been collected
```

Grant dimensions:

```text
maxItems
maxSteps
maxWrs, if command publication consumes RDMA send/write resources
```

### 4.5 `COLLECT_BACKEND_COMPLETIONS`

Purpose:

```text
observe backend completion records and update command/session lifecycle state
```

Grant dimensions:

```text
maxItems
maxPolls
```

### 4.6 `PUBLISH_COMMAND_COMPLETIONS`

Purpose:

```text
make terminal command completion visible to frontend/client side
```

Why important:

This is directly on the latency-sensitive path. It should be a hard-reservation class and should not be crowded out by bulk payload.

Grant dimensions:

```text
maxItems
maxSteps
```

### 4.7 `DRAIN_SEND_CQ`

Purpose:

```text
drain a physical send CQ / lane / QP completion source,
route mixed tagged completions,
release local source/WR resources,
advance transport-completion frontiers
```

Why lane/CQ-owned:

If a physical CQ can return command/control/payload completions, the scheduler-visible owner should be the CQ/lane, not the semantic owner. The branch already tags payload and command WR IDs so mixed completions on a shared send CQ can be routed safely.   The RDMA header already exposes a bounded tagged send-CQ drain that can route command and payload CQEs through typed callbacks. 

Grant dimensions:

```text
maxPollBatches
maxCqes
```

Notes:

If command/control and payload are on distinct traffic-class lanes in today’s terminology, this is still the right abstraction: the owner is the actual physical CQ/QP/lane being drained.

### 4.8 `PUMP_PEER_TRANSPORT`

Purpose:

```text
coalesced peer transport progress:
    listener/CM setup
    recv CQ
    request mailbox
    response mailbox
    send CQ, if not split into DRAIN_SEND_CQ
    close/lifetime
```

Why aggregate:

The current code already does this successfully: it keeps peer collectors logically separate, folds selected phases into a phase mask, and executes one peer table walk. The code comment says a pure six-action physical split preserved correctness but repeated connection-table walks and regressed mixed pgbench/basebackup.  The KB states the accepted design keeps split scheduler facts/feedback while coalescing selected peer phases into one physical peer grant. 

Grant dimensions:

```text
phaseMask
maxPollBatches
maxMailboxMessages
maxConnections or scan budget, later
```

### 4.9 `ADVANCE_PEER_CONTROL_OP`

Purpose:

```text
advance higher-level peer-control async operations after peer transport observation made their next transition legal
```

Why separate from peer pump:

Peer pump discovers and processes transport-visible work. Peer-control operations are higher-level lifecycles. Keeping them separate avoids hiding protocol transitions under observation work.

Grant dimensions:

```text
maxOps
maxSteps
maxWrs, if publication is required
```

### 4.10 `REFRESH_PAYLOAD_FRONTIERS`

Purpose:

```text
refresh payload readiness, remote credit, source-release blockage,
and stale stream facts without scanning every stream every pass
```

This is the payload-side observation action. It may later disappear for streams with strong maintained facts, but it is useful in the transition.

Grant dimensions:

```text
maxStreams
maxPolls
```

### 4.11 `ADVANCE_PAYLOAD_STREAM`

Purpose:

```text
perform a bounded visit to one payload stream lifecycle
```

Possible subactions:

```text
OUTGOING
INCOMING
SEND_CQ_APPLY_KNOWN
CREDIT_PUBLISH
EOS
CLOSE_RECLAIM
LOCAL_BLACKHOLE
```

Why aggregate:

Payload streams can simultaneously have multiple legal ready bits. Emitting one candidate per bit would cause candidate explosion. The current baseline often narrows a payload stream to one selected payload action, but the executor boundary already maps action kinds to payload subaction masks.  

Grant dimensions:

```text
subactionMask
maxPumpCalls
maxBytes
maxObjects
maxWrs
maxFragments
maxDoorbells
localSignalPolicy
remoteNotifyPolicy
```

This is where current payload egress facts move upward. The existing fact builder already computes byte-ring use, outstanding completions/WRs, local resource blockage, traffic class, ready bytes/objects, and estimated WR count.  The current grant builder separately clamps object/byte/WR limits.  In the new model, those become candidate feasible ranges and default grant hints.

### 4.12 `ADVANCE_LIFETIME_RECLAIM`

Purpose:

```text
close/reclaim stream/session/peer resources
```

This may initially be represented as a hard class/reason flag on `ADVANCE_PAYLOAD_STREAM` and `PUMP_PEER_TRANSPORT`, rather than a separate executor. But it deserves an explicit scheduler class because starving close/reclaim can become resource exhaustion.

Grant dimensions:

```text
maxItems
maxSteps
```

### 4.13 `RUN_MAINTENANCE`

Purpose:

```text
heartbeat, diagnostics, low-frequency maintenance
```

Grant dimensions:

```text
maxItems
maxSteps
```

## 5. Homogeneous grant surface

The current split between `HomerProgressGrant` and `HomerTransportGrant` should collapse conceptually. `HomerTransportGrant` already has the key egress dimensions: `maxWrCount`, `maxObjectCount`, `maxBytes`.  These should become part of a broader `HomerGrantVector`.

Recommended shape:

```c
typedef enum HomerLocalSignalPolicy
{
    HOMER_LOCAL_SIGNAL_DEFAULT = 0,
    HOMER_LOCAL_SIGNAL_NONE_UNLESS_NEEDED,
    HOMER_LOCAL_SIGNAL_AT_GRANT_END,
    HOMER_LOCAL_SIGNAL_EVERY_N_WRS,
    HOMER_LOCAL_SIGNAL_EVERY_N_BYTES,
    HOMER_LOCAL_SIGNAL_FORCE
} HomerLocalSignalPolicy;

typedef enum HomerRemoteNotifyPolicy
{
    HOMER_REMOTE_NOTIFY_DEFAULT = 0,
    HOMER_REMOTE_NOTIFY_AT_GRANT_END,
    HOMER_REMOTE_NOTIFY_EVERY_N_OBJECTS,
    HOMER_REMOTE_NOTIFY_EVERY_N_BYTES,
    HOMER_REMOTE_NOTIFY_ON_EOS,
    HOMER_REMOTE_NOTIFY_FORCE
} HomerRemoteNotifyPolicy;

typedef struct HomerGrantVector
{
    uint16_t maxPolls;
    uint16_t maxPollBatches;
    uint16_t maxItems;
    uint16_t maxMessages;
    uint16_t maxPumpCalls;
    uint16_t maxSteps;
    uint16_t maxCqes;

    uint32_t maxWrs;
    uint32_t maxObjects;
    uint32_t maxFragments;
    uint32_t maxDoorbells;
    uint64_t maxBytes;

    HomerLocalSignalPolicy localSignalPolicy;
    uint32_t localSignalEveryWrs;
    uint64_t localSignalEveryBytes;

    HomerRemoteNotifyPolicy remoteNotifyPolicy;
    uint32_t remoteNotifyEveryObjects;
    uint64_t remoteNotifyEveryBytes;
} HomerGrantVector;
```

Important implementation rule:

> In the new grant surface, zero should mean zero. Do not preserve “zero means legacy unbounded” for new action grants.

The current code uses zero in some grants to preserve old unbounded behavior. That is dangerous for a scheduler whose purpose is bounding HOL. If an executor needs default behavior, the builder should fill an explicit default. If something must remain unbounded during migration, mark it with an explicit compatibility flag and remove it later.

Action grant:

```c
typedef struct HomerActionGrant
{
    HomerActionKind kind;
    HomerOwnerRef owner;

    /*
     * Aggregate-action-specific mask:
     * - peer phase mask
     * - payload subaction mask
     * - local-control subaction mask
     */
    uint64_t subactionMask;

    HomerGrantVector grant;
    uint32_t flags;
} HomerActionGrant;
```

## 6. Candidate surface

A candidate is a stack-local snapshot, not a heap task and not an owned work item.

Recommended shape:

```c
typedef enum HomerActionClass
{
    HOMER_ACTION_CLASS_CRITICAL_CONTROL = 0,
    HOMER_ACTION_CLASS_TERMINAL_VISIBILITY,
    HOMER_ACTION_CLASS_RESOURCE_RELIEF,
    HOMER_ACTION_CLASS_DEMANDED_OBSERVATION,
    HOMER_ACTION_CLASS_FOREGROUND_PAYLOAD,
    HOMER_ACTION_CLASS_BULK_PAYLOAD,
    HOMER_ACTION_CLASS_CLOSE_LIFETIME,
    HOMER_ACTION_CLASS_SPECULATIVE_OBSERVATION,
    HOMER_ACTION_CLASS_MAINTENANCE
} HomerActionClass;

typedef enum HomerActionCandidateFlag
{
    HOMER_ACTION_CANDIDATE_KNOWN_WORK           = (1U << 0),
    HOMER_ACTION_CANDIDATE_SPECULATIVE         = (1U << 1),
    HOMER_ACTION_CANDIDATE_DEPENDENCY_DEMAND   = (1U << 2),
    HOMER_ACTION_CANDIDATE_RESOURCE_RELIEF     = (1U << 3),
    HOMER_ACTION_CANDIDATE_TERMINAL_VISIBILITY = (1U << 4),
    HOMER_ACTION_CANDIDATE_CLOSE_LIFETIME      = (1U << 5),
    HOMER_ACTION_CANDIDATE_FOREGROUND          = (1U << 6),
    HOMER_ACTION_CANDIDATE_BULK                = (1U << 7)
} HomerActionCandidateFlag;

typedef struct HomerActionCandidate
{
    HomerActionKind kind;
    HomerOwnerRef owner;

    HomerActionClass actionClass;
    HomerTransportTrafficClass trafficClass;
    HomerProgressCpuClass cpuClass;

    uint32_t flags;
    uint32_t blockedReasonMask;
    uint32_t waitingOnMask;
    uint32_t unblocksMask;

    uint64_t subactionMask;

    HomerGrantVector minGrant;
    HomerGrantVector maxGrant;
    HomerGrantVector defaultGrant;

    uint16_t waitingMachineCount;
    uint16_t recentEmptyScore;
    uint16_t recentProductiveScore;

    uint32_t readyObjectsHint;
    uint32_t estimatedWrCountHint;
    uint64_t readyBytesHint;

    uint32_t outstandingWrCount;
    uint32_t outstandingSignaledWrCount;

    uint64_t readySinceTick;
    uint64_t lastProgressTick;
    uint64_t overflowDebt;
} HomerActionCandidate;
```

This can be simplified in code. The important fields for slice one are:

```text
kind
owner
actionClass
trafficClass/cpuClass
flags/reason
subactionMask
defaultGrant/maxGrant
ready bytes/objects/WR hints
waiting/dependency hints
```

Candidate builders should reuse current facts as much as possible. Current `HomerProgressSourceCore` and feedback already distinguish traffic class, CPU class, ready/blocked reasons, signaled-WR pending, resource pressure, source-release blockage, outstanding WR count, byte/object hints, and separate frontiers.  

## 7. Candidate slate and overflow strategy

Do not build the universe. Build a bounded slate.

Separate:

```c
#define HOMER_ACTION_CANDIDATE_MAX 32  /* or 48 */
#define HOMER_ACTION_PLAN_MAX      16
```

The current machine/collector candidate set already tracks dropped machine/collector counts and overflow.  Keep that spirit, but do not let append order decide correctness-sensitive drops.

Candidate-building strategy:

```text
1. Add fixed/global candidates:
   - local control collection
   - command ingress
   - backend completion collection
   - command completion publication
   - peer transport pump
   - maintenance, if due

2. Add per-lane/CQ candidates:
   - send CQ drain where there is known outstanding signaled work,
     resource pressure, or waiters

3. Add payload candidates:
   - top K foreground streams
   - top K bulk streams
   - all hard close/lifetime/resource-relief streams up to a hard cap

4. Add observation candidates:
   - demanded observation first
   - speculative observation with backoff
   - payload frontier refresh by cursor/class
```

Bucket reservations:

```text
hard bucket A: critical/terminal visibility
hard bucket B: resource relief
hard bucket C: close/lifetime/reclaim
hard bucket D: demanded observation
soft bucket E: foreground payload
soft bucket F: bulk payload
soft bucket G: speculative observation / maintenance
```

Rules:

```text
- Hard buckets cannot be silently crowded out by soft buckets.
- Unused reserved slots flow to shared spillover.
- Soft candidates can be dropped.
- Dropped hard candidates set overflow debt and should be considered first next pass.
- Large candidate classes use rotating cursors.
- Record overflow counters by action kind, class, and source family.
```

This should avoid most overflow because candidates are macroscopic. If overflow still happens frequently, then add bounded replacement later.

## 8. Batching knobs in the grant

The first implementation should expose batching knobs without changing behavior aggressively.

Separate four knobs.

### 8.1 Local signaled-completion / CQE-production batching

Question:

```text
how often do local sends generate signaled completions?
```

Current branch already uses signaled checkpoints. The RDMA header says waiting for a signaled payload publish checkpoint also closes all earlier unsignaled WRs on the same QP.  The historical note says the per-object completion wait was the basebackup bottleneck and recommends mostly unsignaled writes with signaled completion every batch/window/close. 

First slice:

```text
add localSignalPolicy fields
default to current behavior
do not yet adapt aggressively
```

### 8.2 CQ drain batching

Question:

```text
once we touch a CQ, how many batches/CQEs do we drain?
```

Expose through:

```text
DRAIN_SEND_CQ.maxPollBatches
DRAIN_SEND_CQ.maxCqes
PUMP_PEER_TRANSPORT.maxPollBatches
```

The current peer pump already takes `maxPollBatches` and `maxMailboxMessages` from the grant. 

### 8.3 RDMA posting / WR batching

Question:

```text
how many bytes/objects/WR descriptors do we post in one payload visit?
```

Expose through:

```text
maxBytes
maxObjects
maxWrs
maxFragments
maxDoorbells
```

The current object-ring and byte-ring senders already respect `HomerTransportGrant` dimensions.  

### 8.4 Remote notification / responder-visible publish batching

Question:

```text
how often do we generate responder-visible WRITE_WITH_IMM notifications?
```

The RDMA visibility doc says the correctness rule is ordinary RDMA writes followed by a final `RDMA_WRITE_WITH_IMM` responder-visible publish/doorbell.  The object-ring sender already posts one responder-visible tail doorbell for a contiguous object range. 

First slice:

```text
add remoteNotifyPolicy fields
default to current behavior
do not yet change notification cadence
```

Do not make semantic basebackup chunk size or tuple producer batch size dynamic in this milestone. Treat those as upstream producer/object-formation knobs.

## 9. Plan execution and repair/replan strategy

There are three levels. Implement them in order.

### Stage A: no repair, but instrument discovery delay

First, implement no mid-plan mutation.

Executors return typed result flags:

```c
typedef enum HomerActionResultFlag
{
    HOMER_ACTION_RESULT_PROGRESS                   = (1U << 0),
    HOMER_ACTION_RESULT_EMPTY_OBSERVATION          = (1U << 1),
    HOMER_ACTION_RESULT_DISCOVERED_COMMAND         = (1U << 2),
    HOMER_ACTION_RESULT_DISCOVERED_COMPLETION      = (1U << 3),
    HOMER_ACTION_RESULT_UNBLOCKED_CRITICAL         = (1U << 4),
    HOMER_ACTION_RESULT_RELIEVED_RESOURCE_PRESSURE = (1U << 5),
    HOMER_ACTION_RESULT_GRANT_CLAMPED              = (1U << 6)
} HomerActionResultFlag;
```

Record:

```text
when high-priority work was discovered
how many grants ran before it was first serviced
how many bulk bytes/WRs ran after discovery before service
discovery-to-service microseconds/ticks
```

This tells us whether repair/replan is actually needed.

### Stage B: observation-heavy / observation-only plans

Before adding plan mutation, add a policy mode:

```text
if foreground-sensitive or cold/uncertain:
    emit short observation-heavy plan
else:
    emit normal mixed plan
```

Example observation-heavy plan:

```text
COLLECT_COMMAND_INGRESS
COLLECT_BACKEND_COMPLETIONS
DRAIN_SEND_CQ(critical/control)
PUMP_PEER_TRANSPORT(RECV_CQ | RESPONSE_MAILBOX, small grant)
REFRESH_PAYLOAD_FRONTIERS(foreground, small grant)
```

Then the next pump pass naturally schedules newly visible work.

This may be sufficient if plans are short and grants are bounded.

### Stage C: stop-and-replan at grant boundary

Use existing scaffolding. The current code already has plan stop state and a transition result field for `stopAndReplan`.  

Policy:

```text
after an observation/resource-relief grant,
if result discovered command/completion/critical unblocking:
    stop at grant boundary
    rebuild slate
    build new plan from current facts
```

Limits:

```text
maxReplansPerPump = 1 initially
only after high-priority discovery
never after every productive bulk grant
```

### Stage D: scheduler-owned hot continuation / repair surface

After measuring stop-and-replan cost, add bounded repair.

The repair itself can be viewed as a scheduler action/subaction conditional on observation result:

```text
OBSERVE_X discovers hot work
    -> scheduler applies continuation policy
    -> inserts/prepends one or two grants into continuation area
```

Do not let executors mutate plans. Executors only report facts. The scheduler owns repair.

Initial repair surface:

```c
typedef struct HomerPlanRepairRequest
{
    uint32_t flags;
    HomerOwnerRef owner;
    HomerActionKind suggestedKind;
    uint64_t suggestedSubactionMask;
    HomerActionClass actionClass;
} HomerPlanRepairRequest;

typedef struct HomerActionExecutionPlan
{
    uint16_t grantCount;
    uint16_t cursor;

    uint16_t hotGrantCount;
    uint16_t hotCursor;

    uint16_t repairsUsed;
    uint16_t maxRepairs;

    HomerActionGrant grants[HOMER_ACTION_PLAN_MAX];
    HomerActionGrant hotGrants[HOMER_ACTION_HOT_CONTINUATION_MAX];
} HomerActionExecutionPlan;
```

Execution rule:

```text
after each normal grant:
    executor returns result
    scheduler may create hot continuation grant
    hotGrants execute before remaining normal grants
```

Limits:

```text
HOMER_ACTION_HOT_CONTINUATION_MAX = 2
max repair depth = 1 initially
only critical/foreground/terminal/resource-relief grants
dedupe by owner/action
continuations consume remaining budget
no arbitrary insert-anywhere in first repair implementation
```

Later, if needed:

```text
insert before first remaining bulk grant
replace a remaining soft bulk grant
```

Do not implement arbitrary middle insertion until there is a concrete measured case.

## 10. Policy v1

The first unified policy should be simple and research-aligned.

Policy order:

```text
1. critical/control and terminal visibility
2. resource relief needed for foreground/control progress
3. demanded observation
4. foreground command/payload progress
5. bounded bulk payload progress
6. speculative observation
7. maintenance
```

But this should be reservations + spillover, not a rigid total order that starves bulk.

Suggested defaults:

```text
candidateMax = 32 or 48
planMax = 16

hard reservations:
    terminal/critical:     3
    resource relief:       3
    demanded observation:  3
    close/lifetime:        1 or 2

soft/spillover:
    foreground payload:    2-4
    bulk payload:          2-4
    speculative/maint:     1-2
```

Bulk payload grants should be throughput-friendly but bounded:

```text
bulk maxBytes > foreground maxBytes
bulk maxObjects > foreground maxObjects
bulk maxWrs > foreground maxWrs
but bulk grant must be small enough for grant-boundary reaction to matter
```

Foreground grants should be small/latency-biased:

```text
small object/byte grants
more frequent scheduling
higher stop/repair sensitivity
```

CQ/resource relief policy:

```text
if outstandingSignaledWrCount high
or sourceReleaseBlocked
or resourcePressure
or waitingMachineCount > 0:
    raise DRAIN_SEND_CQ priority/class
```

Unknown work policy:

```text
known expected work > demanded observation > speculative observation
speculative observation uses empty-poll backoff
idle service still polls normally
```

This preserves the current lesson that due-only listener polling must remain schedulable but should not masquerade as known work. The KB explicitly notes incomplete setup counts are known work while listener polling is due-only blind work. 

## 11. Implementation stages

### Stage 1: scaffolding and compatibility mode

Add new types:

```text
HomerActionKind, or extend/alias current action kind names
HomerActionClass
HomerActionCandidate
HomerGrantVector
HomerActionGrant
HomerActionCandidateSlate
HomerActionExecutionPlan
```

Do not remove old machine-baseline code yet.

Add conversion helpers:

```text
old collector facts -> aggregate observation candidates
old machine facts    -> aggregate lifecycle candidates
old egress facts     -> payload feasible grant ranges
```

Add logging/stats:

```text
candidate counts by class/kind
candidate overflows by class/kind
grant counts by class/kind
bytes/objects/WRs per grant
local signal policy used
remote notify policy used
discovered-hot-work delay
```

Validation goal:

```text
new policy can be compiled but defaults to behavior equivalent to current baseline
```

### Stage 2: payload egress moves into candidate/grant planning

Change:

```text
HomerServiceBuildPayloadEgressReadyFacts()
```

from “input to separate egress policy” into “candidate feasibility/cost helper.”

Change:

```text
HomerServiceBuildPayloadEgressGrant()
```

from “independent grant decision after source selection” into “default grant/range helper.”

Executor rule:

```text
planned grant is maximum
executor revalidates
executor may clamp downward
executor must not expand upward
```

Keep existing RDMA posting helpers where they are.

Validation:

```text
payload-only basebackup unchanged or better
mixed pgbench/basebackup no regression
bytes/objects/WR grant stats sane
```

### Stage 3: aggregate candidate slate and reservations

Implement candidate slate:

```text
fixed/global candidates
per-lane CQ candidates
payload candidates via class cursors
observation candidates via demand/backoff
```

Add reservations, spillover, overflow debt, rotating cursors.

Validation:

```text
candidate overflow rare or zero
hard class overflow zero in normal workloads
no regression on c1 setup/liveness
no regression on mixed workload
```

### Stage 4: lane/CQ-owned send-CQ drain

Introduce `DRAIN_SEND_CQ` candidates for physical CQs/lanes where feasible.

Use existing tagged completion routing support. 

Decide whether peer-control send CQ remains inside `PUMP_PEER_TRANSPORT` or is split into `DRAIN_SEND_CQ` based on current physical CQ ownership. Do not force conceptual purity over code reality.

Validation:

```text
command/control completion retirement remains correct
payload source-release remains correct
mixed tagged completions route correctly
CQ drain feedback by lane/class visible
```

### Stage 5: observation-heavy policy mode

Add policy mode that emits short observation-heavy plans under foreground-sensitive conditions.

Measure:

```text
discovery-to-service delay
bulk bytes/WRs executed after foreground discovery
foreground p99/p999
basebackup/COPY throughput
```

Validation:

```text
foreground tail improves or stays flat
bulk throughput not materially harmed
```

### Stage 6: stop-and-replan

Use grant-boundary stop/replan only for high-value discoveries.

Validation:

```text
number of replans per second
candidate rebuild cost
foreground discovery-to-service delay
bulk throughput impact
```

If replan cost is too high, proceed to Stage 7.

### Stage 7: scheduler-owned hot continuations / repair

Add tiny hot continuation array.

Rules:

```text
only scheduler inserts
executors only report repair requests
max 2 continuations
dedupe owner/action
only critical/foreground/terminal/resource-relief
continuations consume remaining budget
```

Validation:

```text
same correctness gates as stop/replan
lower replan overhead
equal or better foreground tail
no bulk starvation
```

### Stage 8: adaptive batching policy

Only after the unified surface is stable:

```text
adapt maxBytes/maxObjects/maxWrs by class and pressure
adapt localSignalPolicy under resource pressure
adapt remoteNotifyPolicy, if needed
```

Do not change semantic producer chunk size in this milestone.

## 12. Validation matrix

Run the same core gates after each meaningful stage:

```text
foreground-only remote Homer pgbench
bulk-only remote RDMA basebackup
mixed remote c4 pgbench + background RDMA basebackup
backend-to-backend Citus tuple COPY
cold c1 setup/reconnect/liveness
synthetic CQ/resource-pressure test, if available
```

Metrics to add:

```text
candidate slate size by class/kind
candidate overflow by class/kind
plan grants by class/kind
grant execution time
bytes/objects/WRs per payload grant
CQEs drained per CQ grant
empty/productive observation grants
discovered-hot-work to first-service delay
bulk work executed after hot discovery
stop/replan count
hot continuation count
grant clamp reasons
resource-pressure relief latency
```

The final acceptance criterion is not “more scheduler sophistication.” It is:

```text
foreground command/completion p99/p999 improves or stays stable
bulk basebackup/COPY throughput is preserved
service CPU does not grow significantly
candidate overflow is controlled
no setup/liveness regressions
```

## 13. The plan in one sentence

Move Homer to a **unified, aggregate, guarded action scheduler** where state machines and collectors produce bounded candidates, egress is just a parameterized payload action, CQ drains are lane/CQ-owned resource-relief actions, overflow is controlled by reservations and cursors, and low-latency reaction is added in stages: measurement first, observation-heavy plans second, stop/replan third, hot continuations fourth.
