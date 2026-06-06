# Homer Service-Progress Scheduler Onboarding

This note is for someone adding or evaluating new Homer service-progress
scheduling policies without first learning the whole Homer transport codebase.
The implementation currently lives in the sibling Citus repo at
`/data/dbcomm/citus-dbcomm`, mostly in
`src/backend/distributed/utils/homer/tuple_sink_service_process.c`.

## Scope

Homer has a standalone tuple-sink service process that repeatedly advances
transport/control work for local frontends, peer services, command forwarding,
completion delivery, payload streams, and maintenance. The service loop must
decide how to spend CPU cycles among these progress sources without needing DB
semantic knowledge such as "this is SQL" or "this is basebackup".

There are currently two scheduler layers:

1. Service-progress scheduling chooses which concrete service source gets CPU
   in this loop pass. This is the first layer and is the main focus of this
   note.
2. Transport-egress scheduling chooses byte/object/WR grants after a selected
   payload stream is being executed. This layer uses traffic classes and RDMA
   resource facts.

Keep these separate, but not blind to each other. CPU/liveness classes answer
"what should the service loop poll or execute now?" Transport/egress facts such
as ready bytes, remote-credit blockage, local completion pressure, and
source-release pressure can still inform that first-layer choice through
`HomerProgressSourceCore` and `HomerProgressSourceFeedback`. The second layer
then answers the narrower question: "given that this send-capable source was
selected, how many bytes, objects, fragments, and WRs should be posted?"

## Main Loop Shape

The top-level primitive is
`TupleSinkServicePumpOnce()` in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
Each service pass does this:

1. `HomerServiceBuildCoarseProgressReadySet()` builds a bounded array of ready
   `HomerProgressSourceRef` entries from cheap/coarse facts.
2. `HomerServiceBuildProgressSourcePlan()` dispatches to the selected
   `HomerProgressPolicyKind` and returns an ordered `HomerProgressSourcePlan`.
3. `HomerServicePayloadEgressPassBudgetBegin()` starts any pass-wide egress
   budget state for payload streams.
4. `HomerServiceExecuteProgressSource()` validates each selected source and
   calls the source-specific plan/executor.
5. Executors return a `HomerProgressResult`; `HomerServiceFinishProgressGrant()`
   merges it into source feedback for later policy decisions.

The ready set and source plan are fixed-size stack-local objects. Do not add
per-pass heap allocation to policy code.

## Getting Started

Start in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`.
For a new first-layer service-progress policy, the usual edit set is small:

1. Add a new enum value to `HomerProgressPolicyKind`.
2. Add the user-facing policy name to `HomerServiceProgressPolicyName()`.
3. Parse an opt-in `HOMER_PROGRESS_POLICY=...` spelling in
   `HomerServiceReadProgressPolicyEnv()`.
4. Implement a policy builder with this shape:

   ```c
   static bool
   HomerServiceBuildMyPolicyProgressSourcePlan(
       const HomerProgressReadySet *readySet,
       HomerProgressSourcePlan *sourcePlan);
   ```

   If the policy needs persistent state, take `HomerProgressPolicy *` as the
   first argument, like the round-robin policy does.

5. Add a dispatch case in `HomerServiceBuildProgressSourcePlan()`.

The builder's job is only to turn the bounded ready set into an ordered source
plan. It should normally call `HomerProgressSourcePlanReset()` once, iterate over
`readySet->sources[]`, inspect cheap source facts/feedback as needed, and append
selected sources with `HomerProgressSourcePlanAppend()`. It should not call
source executors directly and should not decide payload byte/object/WR counts.

Existing service-progress policies to read first:

- `HomerServiceBuildFixedPriorityProgressSourcePlan()`: minimal baseline. It
  copies the ready set in the order produced by
  `HomerServiceBuildCoarseProgressReadySet()`.
- `HomerServiceBuildRoundRobinProgressSourcePlan()`: example with persistent
  policy state in `HomerProgressPolicy`.
- `HomerServiceBuildUnblockedFirstProgressSourcePlan()`: example that consumes
  feedback through `HomerServiceProgressSourceFeedbackBlocked()`.
- `HomerServiceBuildCpuLivenessProgressSourcePlan()`: example that consumes
  `HomerProgressCpuClass` and class ranks.

If the experiment is about exact payload send sizing rather than source ordering,
start with the second-layer egress interface instead:

```c
static bool
HomerServiceBuildPayloadEgressGrant(
    const HomerEgressPolicy *egressPolicy,
    const HomerPayloadEgressReadyFacts *readyFacts,
    HomerTransportGrant *transportGrant);
```

That path is selected by `HOMER_EGRESS_POLICY`, not `HOMER_PROGRESS_POLICY`.

## Current Policies

`HOMER_PROGRESS_POLICY` is read once at service startup by
`HomerServiceReadProgressPolicyEnv()`.

Available policies:

- `fixed-priority`: default baseline. It keeps the ready-set order produced by
  `HomerServiceBuildCoarseProgressReadySet()`.
- `round-robin`: simple research policy that rotates over the ready array.
- `unblocked-first`: simple feedback policy that tries unblocked sources first.
- `cpu-liveness`: first policy that explicitly consumes CPU/liveness classes.
  It orders hot data, hot control, close/lifetime, cold setup/maintenance, then
  invalid/unknown sources.

The policy hook is intentionally small:

1. Add a value to `HomerProgressPolicyKind`.
2. Add a name in `HomerServiceProgressPolicyName()`.
3. Parse an env value in `HomerServiceReadProgressPolicyEnv()`.
4. Add a builder and dispatch case in `HomerServiceBuildProgressSourcePlan()`.

If the new policy needs persistent state, add compact service-thread-local state
to `HomerProgressPolicy`. Avoid allocating policy work records per service pass.

## Scheduler Sources

`HomerProgressSourceKind` is the service-progress source vocabulary:

- `RDMA_CM`: low-frequency RDMA connection-manager maintenance.
- `PEER_CONTROL`: aggregate peer-control progress. This includes setup/listener
  work, recv-CQ doorbell polling, peer mailbox dispatch, send-CQ response
  retirement, and close/lifetime progress.
- `LOCAL_CONTROL`: local control-region requests from local frontends/backends.
- `COMMAND_RING`: backend-to-service command publication/forwarding.
- `COMPLETION_RING`: local frontend completion delivery.
- `PAYLOAD_STREAM`: tuple-view or basebackup payload stream progress.
- `CQ_DRAIN`: transport completion retirement for posted work.
- `TARGET_COMPLETION`: synchronous wait target used by restricted pump loops.
- `HEARTBEAT`: service heartbeat/maintenance.

The scheduler sees these as transport/service sources, not DB operations. For
example, tuple-view and basebackup payloads may differ in object format and
egress traffic class, but the first-layer progress scheduler primarily sees a
payload-stream source plus readiness/feedback facts.

## Source Facts

The main source fact types are:

- `HomerProgressSourceId`: compact typed handle: kind, index, generation.
- `HomerProgressSourceRef`: the source id plus the resolved owner pointer.
- `HomerProgressSourceCore`: compact, frequently-read per-source facts.
- `HomerProgressSourceFeedback`: executor-written feedback after a grant.
- `HomerProgressReadySet`: bounded ready-source array for one pass.
- `HomerProgressSourcePlan`: ordered source list selected by a policy.
- `HomerProgressGrant`: bounded CPU work grant passed to a source executor.
- `HomerProgressResult`: source executor result that feeds later feedback.

Important meanings:

- `trafficClass` in `HomerProgressSourceCore` is RDMA/resource treatment. It is
  not itself the first-layer CPU priority, but first-layer policies may use it as
  one scheduling hint when comparing otherwise-ready sources.
- `cpuClass` is the first-layer service-progress CPU/liveness class.
- `ready` is a coarse scheduler hint. Executors must still validate owner,
  generation, frontiers, active bits, and credit before doing real work.
- `creditBlocked`, `resourcePressure`, and `sourceReleaseBlocked` are feedback
  hints. They are useful for policies, but they are not correctness predicates.
- Frontier fields are deliberately split:
  `transportCompletionFrontier`, `sourceReleaseFrontier`,
  `semanticObjectFrontier`, and `remoteCreditFrontier` describe different forms
  of progress and should not be collapsed into one "made progress" bit.

The current ready set is capped by `HOMER_PROGRESS_PLAN_MAX_GRANTS`. Overflow is
reported by `HomerServiceReportReadySetOverflow()` because dropping a ready
source can create starvation or misleading policy conclusions.

## CPU/Liveness Classes

`HomerProgressCpuClass` is the first-layer class vocabulary:

- `HOT_DATA`: payload streams, command rings, completion rings, CQ drains, and
  target completions.
- `HOT_CONTROL`: peer control and local control.
- `CLOSE_LIFETIME`: non-starvable teardown/lifetime work.
- `COLD_SETUP_MAINTENANCE`: RDMA-CM/setup/listener/heartbeat-style work.

`HomerServiceDefaultCpuClassForProgressSource()` assigns conservative defaults.
`HomerServiceProgressSourceCpuClass()` reads the registry core when present and
classifies transient refs directly. New policies can either consume these
classes as-is or derive a private ranking from the same source facts.

## Peer Control

Peer control is still one aggregate first-layer source, but its executor accepts
phase-level subgrants through `TupleSinkServicePeerPumpGrant` in
`remote_execution_peer_transport_rdma.h`.

Phase bits include:

- setup progress
- listener CM polling
- recv-CQ/doorbell polling
- send-CQ/response retirement
- mailbox dispatch
- close/lifetime progress

`fixed-priority` gives peer control an all-phase, unbounded per-pass grant to
preserve the baseline. `cpu-liveness` emits two bounded subgrants: one for cold
setup/listener/close-lifetime work and one for active recv-CQ/send-CQ/mailbox
work. This keeps the transport polling-only for now while letting the
service-progress policy decide CPU budget.

## Egress Layer

Payload stream execution has a second scheduling point:
`HomerServiceBuildPayloadEgressGrant()`.

That policy consumes `HomerPayloadEgressReadyFacts`, not the first-layer ready
set directly. It decides byte/object/WR limits for the selected payload stream.
Some of the same underlying state is visible earlier as coarse source feedback,
for example ready byte/object hints and blocked/resource-pressure flags. Use
those coarse facts to decide whether and when to run a source; do not move exact
payload byte/WR sizing into a service-progress source-order policy.

## Known Non-Goals For A First Policy Experiment

- Do not require event-driven RDMA notifications for peer-control phases. The
  current milestone intentionally keeps all peer-control phases polled and lets
  the scheduler choose how often and how much to poll.
- Do not require DB-semantic awareness in the service-progress scheduler.
  Policies should operate on source kind, CPU/liveness class, traffic class,
  readiness, feedback, and frontier facts.
- Do not treat peer push completions as required for first policy experiments.
  Local frontend push completions exist, and service-to-service peer command
  completions now use a requester-service-owned fixed-size peer completion ring
  on the normal path. `POLL_COMMAND_COMPLETION` remains as a fallback/debug path,
  so policy work should measure whether it is actually exercised before using it
  as an explanation for performance.
- Do not use cold first-run numbers as steady-state evidence. Warmed comparisons
  are the default for this prototype.

## Policy Experiment Checklist

1. Decide which facts the policy needs: source kind, CPU class, traffic class,
   feedback, frontier hints, or per-source owner state.
2. Keep the builder as a pass over `HomerProgressReadySet`; avoid owner rescans
   unless there is a concrete reason and measured benefit.
3. Preserve executor validation. A policy chooses order and budget; executors
   own correctness checks.
4. Keep first-layer CPU work and second-layer egress grants separate.
5. Add diagnostic counters under existing compile-time stats macros if the
   policy needs evidence. Keep normal performance builds clean.
6. Validate with warmed client-SQL and basebackup paths, then mixed workloads if
   the policy changes relative priority between payload and control.

## High-Signal Code Pointers

- `TupleSinkServicePumpOnce()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerProgressSourceKind`, `HomerProgressCpuClass`, `HomerProgressSourceCore`,
  `HomerProgressSourceFeedback`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerServiceBuildCoarseProgressReadySet()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerServiceBuildProgressSourcePlan()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerServiceBuildCpuLivenessProgressSourcePlan()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerServiceExecuteProgressSource()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `HomerServiceBuildPeerControlProgressPlan()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `TupleSinkServicePeerPumpGrant` and `TupleSinkServicePeerPumpProgress`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h`
- `TupleSinkServicePumpPeerRequestsRdma()`:
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c`
- Longer design/history note:
  `docs/kb/future-directions/citus/transport/homer_transport_scheduler_and_payload_streams.md`
