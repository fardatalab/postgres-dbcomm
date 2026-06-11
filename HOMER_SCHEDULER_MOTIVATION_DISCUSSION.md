# Homer Scheduler Motivation Discussion

## Purpose

This note is a discussion artifact for rethinking the Homer scheduler from first
principles. It is intentionally outside `docs/kb`: the goal is not to update the
canonical implementation record, but to give another systems/database expert
enough context to challenge the motivation, baseline, and scheduler boundary.

The central question is not "how do we implement the next scheduler policy?"
The central question is:

> Given Homer's goal, what is the simplest baseline design that exposes the
> communication-stack problem, and what exactly motivates a scheduler rather than
> a simpler fixed progress loop?

## Grand Goal

Homer's high-level goal is a DPU-offloadable external communication stack for the
database. In the target shape, PostgreSQL/Citus should not rely on the vanilla
libpq/TCP path for all database communication. Instead, a Homer service owns a
communication stack that can use RDMA, semantic payload rings, and eventually DPU
placement.

At this level, the motivation is simple:

- move communication work out of database backend threads/processes where useful
- replace TCP/libpq transport with RDMA where useful
- keep database-visible semantics such as SQL command completion, tuple payloads,
  and basebackup payloads correct
- create a communication stack that can later run on a DPU or DPU-like service
  core

Nothing in that statement by itself proves that Homer needs a scheduler. RDMA
rings and semantic object sinks motivate a transport/data-plane redesign. They do
not automatically motivate a two-layer scheduling architecture. The scheduler
motivation needs a separate baseline and problem statement.

## Current Architecture Snapshot

The current Homer standalone service runs a single explicit progress loop. The
loop is in
[`main()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26280),
which repeatedly calls
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26177)
with `TUPLE_SINK_SERVICE_PUMP_MAIN_LOOP`.

`TupleSinkServicePumpOnce()` is the central service progress primitive. It:

- builds a coarse ready set through
  [`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8083)
  at the call site in
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26216)
- bridges that into collector and machine candidates through
  [`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25467)
  and
  [`HomerServiceBuildProgressMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25396)
  at
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26218)
- builds either an action plan or an older source plan depending on the selected
  policy at
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26226)
- begins the payload egress pass budget through
  [`HomerServicePayloadEgressPassBudgetBegin()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3844)
  at
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26265)
- executes the plan through
  [`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26120)
  at
  [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26271)

The currently default first-layer policy is selected in
[`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4207).
With no `HOMER_PROGRESS_POLICY`, it selects the state-machine aware
`machine-baseline` policy at
[`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4216).
The `machine-baseline` policy is initialized by
[`HomerMachineBaselinePolicyInitialize()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7298)
and builds action plans through
[`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6671).

The older flat source policies still exist. The most baseline-like one is
[`HomerServiceBuildFixedPriorityProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6844),
which simply preserves the order produced by
[`HomerServiceBuildCoarseProgressReadySet()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8083).
This is useful as a diagnostic baseline, but not necessarily a clean research
baseline, because it still inherits many current readiness predicates and
post-hoc fixes.

The first-layer scheduler vocabulary is currently service/transport-oriented,
not database-semantic. [`HomerProgressSourceKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1370)
contains sources such as peer control, command rings, completion rings, payload
streams, CQ drain, target completion, and heartbeat. The newer state-machine
model separates machines, collectors, and executable actions with
[`HomerProgressMachineKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1488),
[`HomerProgressCollectorKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1501),
and
[`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1519).

The egress side is represented separately. [`HomerEgressPolicy`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2180)
is selected by
[`HomerServiceReadEgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4138),
and payload egress grants are built by
[`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069).
Transport traffic classes are defined by
[`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:64);
the current one-to-one lane mapping is intended to avoid control traffic being
queued behind bulk payload WRs on the same QP, as described near
[`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:52).

## Why The Scheduler Was Introduced

The strongest first-principles motivation is not "RDMA needs a scheduler." It is:

> Once Homer externalizes communication progress into a user-space service, the
> work that libpq/TCP/backend code used to hide behind blocking calls, kernel
> readiness, per-connection execution, or synchronous call-stack ordering becomes
> explicit service work. The service must decide what to poll or advance now.

That decision can be naive and implicit, or it can be made explicit and
measurable. The scheduler is the attempt to make it explicit.

There are two different resource questions:

1. How should the service spend CPU cycles on polling, CQ drain, command
   forwarding, completion publication, payload stream progress, peer mailbox
   dispatch, setup/teardown, and maintenance?
2. Once a payload/control stream is selected, how should RDMA egress resources be
   sized and shared: bytes, objects, WRs, credits, CQs, doorbells, and lanes?

The current code maps these to a first-layer service-progress policy and a
second-layer egress policy. That split is a design choice, not a theorem. It is
plausible because the resources, units, and time scales differ, but it still
needs a research baseline to show that the separation is useful.

## Scheduler-Migration Guardrail

The peer setup/listener timeout uncovered during scheduler work should be
de-emphasized in the research motivation. It was an implementation bug in
scheduler eligibility, not a fundamental reason Homer needs a scheduler.

A naive hardcoded service loop that periodically pumps every required subsystem
would eventually poll the RDMA-CM listener. The broken intermediate scheduler
instead made listener polling conditional on exact setup facts that listener
polling itself creates:

```text
poll listener only if exact setup work exists
exact incoming setup work exists only after listener polling accepts/registers it
therefore listener polling can be suppressed forever
```

The current bridge in
[`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25467)
fixes that by making listener polling schedulable as blind discovery work while
reserving `knownExpectedWork` for exact setup facts at
[`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25607).

The lesson for future discussion is narrow: unknown-arrival collectors must
remain schedulable even when exact known-work facts are absent. The broader
scheduler motivation should come from measured CPU waste, batching, HOL, and
mixed-workload interference, not from this migration bug.

## The Missing Baseline

The current implementation has already accumulated scheduler fixes, which makes
it harder to tell a clean research story. We still need an explicit baseline that
answers: what would a naive external RDMA communication service do, and where
does it lose?

A useful baseline should be intentionally simple and arguably reasonable:

- one service progress loop
- fixed order over local control, command rings, completion rings, payload
  streams, peer transport, and maintenance
- fixed periodic polling for unknown external arrivals
- no machine/collector dependency model
- no known-work versus blind-poll distinction
- one egress class, or fixed per-payload caps with no cross-stream traffic class
  policy
- no adaptive plan lifetime or feedback-driven admission

The closest current code diagnostic is `HOMER_PROGRESS_POLICY=fixed`, selected
by
[`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4232)
and implemented by
[`HomerServiceBuildFixedPriorityProgressSourcePlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6844).
However, it is not a pure naive baseline because it still runs inside the modern
service, with modern ready-set construction, modern peer facts, and modern
payload/peer fixes.

The baseline should be evaluated on workloads that stress different failure
modes:

- remote c1/c4 client SQL through Homer: command/completion/control sensitivity
- remote RDMA basebackup: bulk payload throughput and background progress
- foreground pgbench plus background basebackup: CPU and link interference
- backend-to-backend Citus tuple COPY: service-to-service payload stream
  throughput
- cold peer setup or reconnect loops: correctness guardrail for unknown-arrival
  discovery, not the main performance motivation

The measurements should include not only TPS or seconds, but also service-loop
empty/productive progress, blind poll counts, blocked-on-credit/resource counts,
payload WR/byte grants, and CQ drain counts. Timeout/stuck cases should be
tracked as correctness regressions in scheduler migration, not counted as the
primary evidence that scheduling is needed.

## What Problems The Baseline Should Expose

### CPU Waste

The service may spend cycles polling sources that are usually empty while known
work is ready elsewhere. This is especially likely for blind collectors: peer
listener polling, recv CQ polling, mailbox polling, and similar unknown-arrival
checks. Current blind backoff lives in
[`HomerMachineBaselineCollectorFeedbackBackoffActive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6061).
A naive loop would either poll these too often, wasting CPU, or too rarely,
hurting discovery latency. It should not be described as a liveness failure
unless the discovery poll can be suppressed indefinitely.

### Dependency Blindness

Some work cannot progress until another collector/action runs. Current machine
facts expose `waitingOnMask` in
[`HomerProgressMachineFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1639),
and collector facts expose `waitingMachineCount` in
[`HomerProgressCollectorFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1664).
The current policy uses that to grant demanded collectors before spending the
rest of the plan on runnable machines in
[`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6736).

A naive flat loop might still eventually work if it polls everything often
enough. The research problem is that "eventually" can mean high CPU burn, long
tail latency, or poor mixed-workload throughput. The state-machine design is a
response to that service-model efficiency problem, while timeout/stuck behavior
is a sign that the scheduler's admission predicates have become incorrect.

### Head-Of-Line Interference

Bulk payloads can occupy service CPU and RDMA resources while foreground command
or completion traffic waits. The code already distinguishes first-layer CPU
classes in
[`HomerProgressCpuClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1408)
and transport traffic classes in
[`HomerTransportTrafficClass`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:64).

The baseline question is whether a one-class RDMA egress path, or a first-layer
policy that ignores egress blockage and ready-byte hints, produces measurable HOL
or throughput loss in mixed workloads.

### Over-Batching Or Under-Batching

For RDMA, tiny writes can waste doorbells, WRs, and CQ processing. But overly
large grants can delay other streams. The egress grant builder
[`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069)
currently has fixed-default and ready-hints modes. Payload facts such as
`readyBytes`, `readyObjects`, `outstandingWrCount`, and credit/resource blockage
are represented in
[`HomerPayloadEgressReadyFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2212).

This is the cleanest motivation for an egress policy, but not necessarily for a
separate egress policy. A monolithic scheduler could in principle choose both
"which stream" and "how much to send" in one decision.

## Why Two Layers Might Still Be Right

The strongest argument for two layers is engineering and experimental
separation, not mathematical necessity:

- service-progress scheduling spends CPU on heterogeneous actions: polling,
  dispatching, CQ drain, completion publication, state-machine advancement, and
  payload pump calls
- egress scheduling spends RDMA resources on sendable payload/control streams:
  bytes, objects, WRs, credits, and traffic classes
- the first layer must sometimes advance work that sends nothing, such as
  polling for unknown arrivals or publishing local completion
- the second layer only matters after a send-capable payload/control action has
  been selected
- the first layer needs summarized egress facts to avoid wasting CPU on streams
  blocked on remote credit or local resources

The current code explicitly connects the layers at
[`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26265):
first-layer planning chooses which source/action executes, then payload execution
consumes the second-layer `payloadEgressPassBudget`.

This is a plausible decomposition, but it should remain open to challenge. A
monolithic policy could produce grants of the form:

```text
advance payload stream S for at most X pump calls, Y WRs, Z bytes
advance command session C for at most N completions
poll peer recv CQ for at most M CQEs
```

That would combine CPU and egress budget in one policy. The cost is a larger,
more coupled policy surface. The benefit is potentially better global decisions.
The current two-layer design is easier to reason about and easier to test, but
the research argument should be validated rather than assumed.

## Discussion Questions For The Next Brainstorm

1. What is the cleanest naive baseline?
   Should it be the existing `fixed` policy, a new deliberately stripped
   baseline, or a paper-level conceptual baseline backed by targeted ablations?

2. What is the strongest measured symptom that motivates a scheduler?
   Candidate symptoms are CPU waste, foreground tail latency under bulk load,
   poor COPY/basebackup throughput, and HOL across traffic classes. Setup
   timeout/liveness belongs in the correctness-guardrail bucket unless we can
   show a naive always-polling baseline also fails.

3. Which symptoms motivate first-layer service-progress scheduling specifically,
   rather than only better egress batching?

4. Which symptoms motivate second-layer egress scheduling specifically, rather
   than only better source ordering?

5. Is the two-layer design actually the right research abstraction, or should
   the policy emit one richer grant that includes both CPU work and egress
   budget?

6. How much should the first layer know about egress?
   Current facts include traffic class, ready bytes, outstanding WR/CQE hints,
   and blocked-resource state in
   [`HomerProgressMachineFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1639).
   Is that enough, too much, or the wrong shape?

7. How much of the state-machine model is essential for the research story?
   It helps make Homer service progress explicit and less wasteful, but the peer
   liveness incident was an implementation bug in scheduler eligibility, not a
   generic RDMA requirement. The note/paper should present state machines as a
   way to manage externalized async progress, not as something libpq forgot to do.

8. What should the DPU-offload story emphasize?
   If the DPU has fewer CPU cycles than the host, first-layer CPU scheduling may
   become more important than host-side results suggest. If the DPU/NIC path has
   different notification mechanisms, the unknown-arrival polling story may
   change.

## Current Working Thesis

The scheduler should be motivated as follows:

1. Homer externalizes database communication progress into a service.
2. That service must explicitly drive heterogeneous asynchronous work that vanilla
   libpq/TCP hides behind blocking calls, kernel readiness, and per-connection
   execution.
3. A naive fixed progress loop can be correct in light load, but under mixed or
   heterogeneous workloads it can waste CPU by polling blindly, delay useful
   dependent progress, under/over-batch RDMA sends, or create
   foreground/background interference.
4. First-layer service-progress scheduling is the mechanism for spending service
   CPU on the most useful next actions.
5. Second-layer egress scheduling is the mechanism for sizing and ordering RDMA
   sends once send-capable work is selected.
6. The two-layer split is a reasonable decomposition because the scarce resources
   and grant units differ, but it should be tested against a richer unified-grant
   alternative.

The key research gap is therefore not lack of implementation. The gap is a clean
baseline and ablation story that shows why explicit scheduling is needed and why
the current decomposition is the right one, or at least a good one.
