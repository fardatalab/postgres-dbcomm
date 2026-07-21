# Homer Unified Aggregate-Action Scheduler Plan

## Scope

- **What this doc explains**: the merged v2/v2.5 guarded-action scheduler redesign, the stronger post-discussion stance on peer-control/CQ ownership prerequisites, and the staged implementation plan for replacing the current machine/collector plus egress split with one aggregate action/grant scheduler.
- **What this doc does NOT cover**: detailed benchmark runbooks, full adaptive policy design, DOCA/DPU offload, or semantic producer chunking for basebackup/tuple materialization.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

## Why this exists

The earlier guarded-action redesign notes reframed Homer from a "state-machine
scheduler" into asynchronous lifecycles that expose guarded action candidates.
That direction is still right, but the first version was too open to
microscopic action enumeration and generic coalescing machinery. The refined
direction is stronger and more implementation-bound:

```text
state machines and collectors remain correctness/fact owners
candidate builders emit bounded aggregate action candidates
the policy chooses action + unified grant vector
executors revalidate and clamp, never expand
typed feedback drives reservations, debt, and later repair/replan
```

The strongest correction from the follow-up discussion is around ownership:
peer-control operation progress and peer-control send-CQ retirement cannot be
split into standalone actions merely because the names look clean. They become
standalone scheduler actions when the code has a clean owner/ref boundary. The
plan below treats that cleanup as a required milestone, not as an optional
future possibility.

## Motivating incidents — the two starvation bugs this design prevents (2026-07-20)

Two mixed-workload hangs in one debugging session were the **same** structural
failure: correctness-critical work starved by the flat, append-order collector
budget (`HOMER_SERVICE_MACHINE_BASELINE_MAX_COLLECTOR_GRANTS = 6`), which the
current scheduler can protect only by bolting on per-site bypasses. They are the
canonical instances of the two failure modes the class model exists to make
*unreachable*. (Full investigation:
[`../../implementations/citus/transport/dpu_collector_admission_starvation_mixed_workload.md`](../../implementations/citus/transport/dpu_collector_admission_starvation_mixed_workload.md).)

**Incident 1 — class-admission starvation (fix citus `6392a1853`).** Peer
control-mailbox actions are enumerated up to 8 but the planner keeps only
`HOMER_CONTROL_MAILBOX_ACTIONS_PER_PASS = 2`, walked in strict traffic-class
priority with no rotation. Over 4 classes, "strict priority" silently meant lower
classes (`FOREGROUND`) *never* ran while `CRITICAL` stayed busy — and the
truncation happened **before** budget accounting, so no drop counter fired.
- Failure mode **(a): a hard class crowded out by a soft one.**
- `primaryClass`: the foreground control mailbox is `FOREGROUND_PAYLOAD` /
  `DEMANDED_OBSERVATION`; it lost to `CRITICAL_CONTROL` purely because admission
  was **order, not class**.
- Patch produced: a per-class rotation cursor + "one action per class per pass"
  (`TupleSinkServiceAppendReadyControlMailboxActionsRdma`).

**Incident 2 — recv-CQ liveness silent drop (fix citus `947b7692a`; the deeper
one).** A gate's `FOREGROUND` service-result peer-open publishes, the peer posts
the OPEN response, but the requester never recv-processes it. The **only** path
that discovers foreground payload recv WIMMs is the recv-CQ liveness action
([`HomerServiceMachineBaselineAppendRecvCqLivenessAction`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c)),
which competes for the same 6-collector budget and is **silently dropped** when
full (`return true`, no diagnostic). A waiting `STREAM_WAIT_PEER_OPEN` op had *no*
demand-driven recv poll — the machine-baseline peer collector bundle is only
`PEER_CM_SETUP` + `PEER_CLOSE_LIFETIME`. `CRITICAL` had an exact-demand recv path;
`FOREGROUND` had none.
- Failure mode **(b): a must-run observation with no demand path.**
- `primaryClass`: a waiting op's recv poll is `DEMANDED_OBSERVATION` — it unblocks
  a waiting machine — and should be reserved by class.
- Patch produced: extend the critical exact-demand bitmap to foreground
  (`payloadOpensAwaitingResponse`) and make the exact append budget-free.

**Why these matter to this design.** Both patches are local answers to *"who else
must bypass the shared cap?"*, invisible to each other — which is why the *N*th
workload finds the *N+1*th starvation. In the target model each is **one line at
candidate construction**: incident 1's mailbox action is a
`DEMANDED_OBSERVATION`/`FOREGROUND_PAYLOAD` candidate with a class reservation;
incident 2's recv poll is a `DEMANDED_OBSERVATION` candidate raised by the waiting
op. Neither can be silently dropped — hard classes are reserved (see "Fixed
unified policy order"), and any refusal is **overflow debt with a counter**, not
`return true`. **Slice 3's concrete acceptance criterion is that both of these
patches dissolve into class reservations** — the reserved-lifecycle admission, the
exact-demand bitmaps, and the mailbox rotation cursor should all *disappear*, not
be ported.

**The diagnostic lesson, made structural.** Incident 2 was invisible for a full
validation run because the drop emitted no counter — it cost a dozen refuted
hypotheses to localize. The design rule *"record overflow counters by action kind
and class from the first implementation"* (see "Candidate slate and overflow
semantics") is not garnish: it is the property that turns a silent multi-day hang
into a one-pass observation. **No budget refusal may be a silent `return true`.**

## Key code pointers

- [`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1519) already names most action families, but still contains finer payload actions and peer-control actions that should be regrouped as aggregate candidates.
- [`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1835) explicitly says current grants bound first-layer CPU/polling work while byte/object/WR egress grants remain separate. This is the two-layer split the redesign removes.
- [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26216) builds the ready set, collector candidates, machine candidates, action plan, then starts a separate payload egress pass budget before execution at [`tuple_sink_service_process.c:26265`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26265).
- [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4010) and [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069) are the current second-layer egress fact/grant helpers. They should become candidate feasible-range/default-grant helpers.
- [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20968) currently asks the egress policy for a transport grant after the payload source has already been selected.
- [`HomerServiceBuildCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9002) already exposes a coarse send-CQ drain plan from outstanding command checkpoints and payload send-CQ hints.
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1764) drains one lane-local send CQ and routes tagged completions through [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1554).
- [`TupleSinkServicePeerSendCompletionCallbacks`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:161) currently routes command and payload completions through callbacks; control send completion is retired internally by [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1631).
- [`TupleSinkServicePeerControlAsyncOpReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:226) is the right non-progressing scheduler fact probe for one peer-control op.
- [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7432) still checks one peer op while also draining connection events, send completions, and peer responses at [`remote_execution_peer_transport_rdma.c:7476`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7476). This is the main ownership blocker for first-class `ADVANCE_PEER_CONTROL_OP`.
- [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7221) also performs broad transport progress before publishing a request at [`remote_execution_peer_transport_rdma.c:7257`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7257).
- [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7536) already accepts a bounded peer pump grant, with `phaseMask`, `maxPollBatches`, and `maxMailboxMessages` sourced from [`TupleSinkServicePeerPumpGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:270).
- [`HomerServiceMachineBaselineAppendPeerCollectorBundle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6396) coalesces peer collector phases into one physical peer transport grant, preserving split scheduler facts without repeated peer table walks.
- [`HomerServicePopulatePayloadMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25209) demonstrates why payload should be aggregate: one stream can expose outgoing, incoming, send-CQ, credit, EOS, and close/reclaim readiness in the same fact pass.

## Current grounded findings

- The current scheduler has useful substrate: explicit action kinds, machine facts, collector facts, wait masks, dependency demand, execution plans, typed feedback, peer phase masks, and payload egress facts.
- The current scheduler still has a real conceptual split: first-layer progress grants decide CPU/poll/item work, while payload byte/object/WR sizing is decided later by an egress helper. The comment on [`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1835) is the clearest code-level evidence.
- Peer transport should remain physically aggregate in the first unified slice. The current policy already maps split peer collectors to a phase mask in [`HomerServiceMachineBaselinePeerPhaseMaskForCollector()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6375) and coalesces them in [`HomerServiceMachineBaselineAppendPeerCollectorBundle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6396).
- Payload frontier refresh should not become a first-slice standalone scanner. Payload readiness facts should be refreshed only for bounded candidate streams, using maintained feedback and cursors.
- `ADVANCE_PEER_CONTROL_OP` is a target action, but not a first-slice action. Today, polling one async peer op still hides transport progress through [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7432), so the executor boundary is not clean yet.
- Peer-control send CQ should initially stay in `PUMP_PEER_TRANSPORT`. Moving it to standalone `DRAIN_SEND_CQ` is a chosen later direction, gated by callback/routing and ownership cleanup.

## Target model

The target scheduler-visible unit is:

```text
aggregate action candidate + unified grant vector
```

not:

```text
machine source selected first
then a separate egress policy sizes payload work
```

The scheduler should build a bounded slate of aggregate candidates:

```text
COLLECT_LOCAL_CONTROL
ADVANCE_LOCAL_CONTROL
COLLECT_COMMAND_INGRESS
ADVANCE_COMMAND_DISPATCH
COLLECT_BACKEND_COMPLETIONS
PUBLISH_COMMAND_COMPLETIONS
PUMP_PEER_TRANSPORT(phaseMask)
DRAIN_SEND_CQ(cq/lane)
ADVANCE_PAYLOAD_STREAM(stream, subactionMask)
RUN_MAINTENANCE
```

The candidate carries:

```text
kind
owner/ref
action class
traffic class / CPU class
reason flags
subaction or phase mask
ready byte/object/WR hints
min/default/max grant vector
waiting/dependency hints
overflow debt and age/debt metadata
```

The grant vector should eventually unify:

```text
CPU/poll/message/CQE budgets
RDMA WR/object/byte/fragment/doorbell budgets
local signaled-completion policy
remote notification / write-with-imm policy
```

For the new grant surface, zero must mean zero. Legacy "zero means unbounded"
behavior can exist only behind an explicit compatibility flag during migration.

## First-slice action set

The first slice should be narrow and map to executor boundaries that already
exist or require only shallow wrapping:

```c
typedef enum HomerActionKind
{
    HOMER_ACTION_INVALID = 0,

    HOMER_ACTION_COLLECT_LOCAL_CONTROL,
    HOMER_ACTION_ADVANCE_LOCAL_CONTROL,

    HOMER_ACTION_COLLECT_COMMAND_INGRESS,
    HOMER_ACTION_ADVANCE_COMMAND_DISPATCH,

    HOMER_ACTION_COLLECT_BACKEND_COMPLETIONS,
    HOMER_ACTION_PUBLISH_COMMAND_COMPLETIONS,

    HOMER_ACTION_PUMP_PEER_TRANSPORT,
    HOMER_ACTION_DRAIN_SEND_CQ,
    HOMER_ACTION_ADVANCE_PAYLOAD_STREAM,

    HOMER_ACTION_RUN_MAINTENANCE
} HomerActionKind;
```

The first slice should not add these as standalone actions:

```text
REFRESH_PAYLOAD_FRONTIERS
ADVANCE_PEER_CONTROL_OP
ADVANCE_LIFETIME_RECLAIM
```

Instead:

- payload frontier refresh is folded into bounded payload candidate construction;
- peer-control op facts can influence candidate metadata, but the schedulable peer transport action remains `PUMP_PEER_TRANSPORT`;
- close/lifetime is represented as `CLOSE_LIFETIME` / `RESOURCE_RELIEF` action class flags on `PUMP_PEER_TRANSPORT` or `ADVANCE_PAYLOAD_STREAM`.

This is not weakening the target. It prevents the first implementation from
creating new ownership ambiguities while the substrate is being moved.

## Required peer-control ownership cleanup

`ADVANCE_PEER_CONTROL_OP` should be tackled after the peer-control ownership
cleanup below. This is a prerequisite, not a vague "only if it happens to be
clean" condition.

The current problem:

- [`TupleSinkServicePeerControlAsyncOpReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:226) is a non-progressing readiness probe and is suitable for candidate facts.
- [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7432) is not just an op-terminal-consume function; it can publish a request through [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7452) and then drains connection events, tagged send completions, and control responses at [`remote_execution_peer_transport_rdma.c:7476`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7476).
- [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7221) also performs broad transport progress before reserving/publishing the request at [`remote_execution_peer_transport_rdma.c:7257`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7257).

Required cleanup before standalone `ADVANCE_PEER_CONTROL_OP`:

1. Keep `TupleSinkServicePeerControlAsyncOpReady()` as the scheduler fact probe.
2. Split broad transport pumping into `PUMP_PEER_TRANSPORT(phaseMask)`.
3. Make peer request publication a bounded op-owned step that either:
   - runs inside `ADVANCE_PEER_CONTROL_OP` only when connection readiness is already known, or
   - remains a peer transport phase until publication ownership is explicit.
4. Narrow `TupleSinkServicePollPeerRequestRdma()` or add a new helper so terminal response consumption/release of one op slot can happen without opportunistically draining unrelated peer transport.
5. Ensure the op-owned executor can say "I advanced this one peer op" without also claiming ownership of listener polling, response mailbox drain, send-CQ retirement, or connection lifetime repair.

Only after those conditions are true should the first-class action be added:

```text
ADVANCE_PEER_CONTROL_OP(op, maxSteps/maxOps/maxWrs)
```

Until then, peer-control progress is represented through:

```text
PUMP_PEER_TRANSPORT(phaseMask)
ADVANCE_LOCAL_CONTROL
ADVANCE_COMMAND_DISPATCH
PUBLISH_COMMAND_COMPLETIONS
```

## Required send-CQ ownership cleanup

The direction is to converge command/payload/control tagged send-CQ retirement
onto lane/CQ-owned `DRAIN_SEND_CQ`. The first slice should not force peer-control
send CQ into that action before the owner/ref and callback model are clean.

Current facts:

- `DRAIN_SEND_CQ` already has a coarse path through [`HomerServiceBuildCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9002) for command checkpoints and payload send-CQ hints.
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1764) is the correct physical-lane primitive: it drains tagged completions from one send CQ.
- [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1554) routes payload completions at [`remote_execution_peer_transport_rdma.c:1562`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1562), command completions at [`remote_execution_peer_transport_rdma.c:1596`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1596), and control completions at [`remote_execution_peer_transport_rdma.c:1631`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1631).
- The callback interface in [`TupleSinkServicePeerSendCompletionCallbacks`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:161) exposes command and payload callbacks, but not a control callback. Control retirement currently stays internal to the RDMA layer.
- Peer pump already handles send-CQ as a bounded phase in [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7795), and the comment there intentionally separates send-CQ retirement from response-mailbox progress.

First-slice decision:

```text
DRAIN_SEND_CQ owns clean command/payload physical CQ drain.
PUMP_PEER_TRANSPORT owns peer-control send-CQ drain for now.
```

Required cleanup before peer-control send CQ moves into standalone
`DRAIN_SEND_CQ`:

1. Define a stable lane/CQ owner reference that can be scheduled without a peer table walk unrelated to that lane/CQ.
2. Add explicit control-completion callback/routing semantics, or document that lane-owned control completion retirement stays fully inside the RDMA layer and is safe to invoke from `DRAIN_SEND_CQ`.
3. Ensure the CQ drain can update peer-control resource state without also consuming response mailbox messages or connection-lifetime work.
4. Ensure peer-control op readiness consumes the results of CQ/resource retirement through facts, not through hidden side effects in op polling.
5. Add feedback counters by lane/class: CQEs drained, command completions, payload completions, control completions, empty polls, and errors.

After that cleanup, the target is:

```text
DRAIN_SEND_CQ(lane/cq, maxPollBatches, maxCqes)
```

for tagged send-CQ retirement across command, payload, and control. Until then,
peer-control send CQ remains a phase of `PUMP_PEER_TRANSPORT`.

## Candidate slate and overflow semantics

The scheduler should build a bounded candidate slate, not the universe of legal
work.

First-slice slate construction:

```text
always consider global/control candidates when useful or due:
    COLLECT_LOCAL_CONTROL
    COLLECT_COMMAND_INGRESS
    COLLECT_BACKEND_COMPLETIONS
    PUBLISH_COMMAND_COMPLETIONS
    PUMP_PEER_TRANSPORT
    RUN_MAINTENANCE

bounded cursors:
    ADVANCE_LOCAL_CONTROL
    ADVANCE_COMMAND_DISPATCH
    ADVANCE_PAYLOAD_STREAM foreground up to K
    ADVANCE_PAYLOAD_STREAM bulk up to K

clean physical lane/CQ:
    DRAIN_SEND_CQ
```

Use one primary action class plus independent reason flags instead of forcing
one enum to carry all scheduling semantics:

```c
candidate.primaryClass = HOMER_ACTION_CLASS_FOREGROUND_PAYLOAD;
candidate.reasonFlags = HOMER_ACTION_REASON_RESOURCE_RELIEF |
                        HOMER_ACTION_REASON_CLOSE_LIFETIME;

candidate.primaryClass = HOMER_ACTION_CLASS_TERMINAL_VISIBILITY;
candidate.reasonFlags = HOMER_ACTION_REASON_CRITICAL_CONTROL |
                        HOMER_ACTION_REASON_DEPENDENCY_UNBLOCK;
```

The primary class is the reservation/admission bucket. Reason flags explain why
the candidate matters and can raise urgency inside or across buckets. This
separation is required because real candidates often have overlapping meaning:

```text
payload work may also be resource relief
peer transport work may also be demanded observation
close/reclaim may also be resource relief
terminal visibility may also be critical control
```

First-slice primary classes:

```text
CRITICAL_CONTROL
TERMINAL_VISIBILITY
RESOURCE_RELIEF
DEMANDED_OBSERVATION
FOREGROUND_PAYLOAD
BULK_PAYLOAD
CLOSE_LIFETIME
SPECULATIVE_OBSERVATION
MAINTENANCE
```

Overflow semantics:

- Hard classes cannot be silently crowded out by soft bulk candidates.
- Unused reserved slots flow into shared spillover.
- Soft candidates can be dropped without correctness impact.
- Dropped hard candidates set overflow debt and move their cursor/class earlier next pass.
- If hard-class overflow occurs, first aggregate more work into existing aggregate candidates before adding replacement/scoring machinery.
- Record overflow counters by action kind and class from the first implementation.

Do not implement complex bounded replacement until measurements show frequent
overflow after aggregate actions, reservations, spillover, and cursors.

## Fixed unified policy order

"Fixed order" here means a deterministic reservation/admission skeleton, not a
rigid total order that starves bulk. The first policy should spend reserved
slots in this order, with unused slots flowing to spillover:

```text
1. critical/control and terminal visibility
2. resource relief needed by control/foreground progress
3. demanded observation that unblocks waiting machines
4. foreground command/payload progress
5. bounded bulk payload progress
6. speculative observation
7. maintenance
```

This is intentionally stronger than "maybe prioritize if needed." Terminal
visibility, resource relief, and demanded observation are correctness/liveness
classes in this prototype; they should have hard reservations from the beginning.

Bulk throughput is preserved by grant sizing and spillover, not by allowing bulk
to crowd out terminal visibility or resource relief.

### Reservation, sizing, and eliminating the dangerous knob (proposed refinement)

"Reservation" means guaranteeing per-pass action slots to a class regardless of
other classes' demand; "sizing" is choosing those counts. The per-pass cap exists
only because the service is a single-threaded busy-poll loop that must **revisit**
all work at bounded latency — a pass cannot do unbounded work or every other class
waits a whole pass. The cap is a revisit budget, not a memory limit.

Reservation *counts* are a dangerous knob: under-size a hard class and it silently
starves (this is exactly what both 2026-07-20 incidents were); over-size and bulk
collapses. **The proposal is to eliminate the reservation-count knob entirely,
using the structure both incidents share** — not to add a smarter tuner for it.

The lever is the `COLLECT`/`OBSERVE` vs `ADVANCE` split this plan already draws
(see "First-slice action set"). Both starvation bugs were on the OBSERVE half — a
recv poll / a mailbox drain that only makes a response *fact* visible:

- **Correctness-critical OBSERVE demand is structurally bounded and cheap.** At
  most one recv poll per *waiting* op (bounded by the 64-entry async-op table),
  one mailbox drain per connection. So do not reserve `N` slots for it — **admit
  all of it exhaustively every pass.** The bound comes from the workload's own
  fixed table sizes, so there is no count to tune.
- **Only unbounded work needs a size clamp.** A `BULK_PAYLOAD`/`ADVANCE` action
  can post arbitrarily many WRs per pass, so it keeps a grant *size* clamp
  (`maxBytes`/`maxWrs`). But that clamp is a **throughput/latency tradeoff whose
  worst case is "slower," never "hung"** — a safe knob, adaptive later (Slice 9).
- **Degenerate safety.** If exhaustive OBSERVE demand ever overflows a pass,
  overflow debt makes it visible and self-correcting (see overflow semantics) —
  never silent.

⇒ The reservation model becomes **"admit all bounded cheap OBSERVE demand
exhaustively; clamp only unbounded BULK by size,"** *not* "reserve N control slots
vs M bulk slots." This deletes reservation-count sizing for exactly the classes
both incidents starved, and leaves only a size clamp that cannot cause a hang.

Preconditions to confirm before adopting this as the reservation rule (each is a
counter, not an assertion):
- Exhaustive OBSERVE cost per pass stays small under maximum concurrency (e.g. all
  64 async-op slots waiting) — measure mostly-empty CQ-poll cost per pass.
- The `COLLECT`/`ADVANCE` boundary is real: an OBSERVE action must only arm facts,
  never do the expensive follow-up work (that is a separate `ADVANCE` candidate
  which *may* be bounded, but with debt-not-silence).
- Any hard class whose per-action cost is genuinely expensive *and* high-count
  still needs a per-pass bound — but with overflow debt, never a silent drop.

## Egress migration

Existing egress functions should become helpers under the unified action/grant
surface:

- [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4010) becomes a candidate feasible-range/fact helper.
- [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069) becomes a default grant/range helper, not an independent second scheduler.
- [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20989) and the underlying verbs-post helpers remain specialized executors.

The executor rule is:

```text
planned grant is an upper bound
executor revalidates current authoritative stream state
executor may clamp downward
executor must never expand beyond the scheduled grant
```

Expose, but initially default, these batching knobs:

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

Do not make semantic basebackup chunk size, tuple producer batch size, or remote
notification cadence adaptive in the first slice.

## Repair/replan strategy

Start with instrumentation, not arbitrary plan mutation.

Stage A:

```text
no repair
add result flags/counters:
    DISCOVERED_COMMAND
    DISCOVERED_COMPLETION
    UNBLOCKED_CRITICAL
    RELIEVED_RESOURCE_PRESSURE
    WOULD_REPAIR_PLAN
measure discovery-to-service delay and bulk work after discovery
```

Stage B:

```text
emit short observation-heavy plans under foreground-sensitive conditions
let the next pump pass schedule newly visible work
```

Stage C:

```text
stop and replan at grant boundary after high-value discovery
maxReplansPerPump = 1 initially
```

The code already has a stop/replan field in
[`HomerProgressTransitionResult.stopAndReplan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1749)
and per-grant policy callbacks in
[`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26150).

Stage D:

```text
scheduler-owned hot continuation array
max 1-2 hot grants
only critical/foreground/terminal/resource-relief
executors report facts, scheduler inserts continuations
```

Executors must not mutate the plan directly.

## Implementation roadmap and checkpoints

This section is the concrete engineering plan. Each slice should be small enough
to validate independently. Do not start the next semantic split until the
previous slice has a correctness checkpoint and at least a rough performance
comparison against the pre-slice branch.

### Slice 0: baseline snapshot and counters

Goal:

```text
establish the current branch as the before/after reference
```

Code touch points:

- [`TupleSinkServicePumpOnce()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26216) for top-level per-pass counters.
- [`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26120) for per-grant counters.
- Existing stats surfaces around progress plans and payload grants.

Deliverables:

- Record baseline candidate counts, grant counts, payload grant sizes, CQ drain counts, empty/productive observation counts, and discovery-to-service delay if it is already observable.
- Keep this slice instrumentation-only. No scheduling behavior change.

Correctness checkpoint:

- Remote c1 setup/reconnect/liveness still passes.
- Foreground-only remote Homer pgbench still completes.
- Remote RDMA basebackup still completes.
- Backend-to-backend Citus tuple COPY still completes with the expected row count/checksum.

Performance checkpoint:

- Compare warmed foreground pgbench, warmed remote RDMA basebackup, mixed pgbench plus basebackup, and backend-to-backend COPY against the immediately previous branch.
- Any regression here is instrumentation overhead; fix or compile-gate it before continuing.

Unlocks:

- A measurement baseline for every later slice.

### Slice 1: compatibility action/grant scaffolding

Goal:

```text
introduce the unified aggregate-action types without changing selected work
```

Code touch points:

- [`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1519), [`HomerProgressGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1835), and [`HomerProgressCompiledActionGrant`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1871).
- [`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6671) as the first compatibility policy producer.
- [`HomerServiceExecuteProgressActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25860) as the compatibility executor bridge.

Deliverables:

- Add `HomerActionClass`, `HomerGrantVector`, and a candidate/grant wrapper or alias that can carry the new grant dimensions.
- Add action-class flags for `TERMINAL_VISIBILITY`, `RESOURCE_RELIEF`, `DEMANDED_OBSERVATION`, `FOREGROUND`, `BULK`, and `CLOSE_LIFETIME`.
- Map existing action grants into the new wrapper with behavior-equivalent defaults.
- Keep legacy zero-as-unbounded behavior only behind an explicit compatibility bit.

Correctness checkpoint:

- The selected action sequence is materially equivalent to the current `machine-baseline` plan for the standard workloads.
- Executor-side generation/owner validation remains authoritative; the new candidate/grant wrapper must not become permission to skip checks.

Performance checkpoint:

- Service CPU and warmed throughput should be statistically flat. If not, the new wrapper is too heavy for the hot loop.

Unlocks:

- The rest of the plan can move one action family at a time without redesigning the execution plan format again.

### Slice 2: payload egress grant becomes scheduler-visible

Goal:

```text
remove the payload two-layer scheduling defect for ADVANCE_PAYLOAD_STREAM
```

Code touch points:

- [`HomerServiceBuildPayloadEgressReadyFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4010).
- [`HomerServiceBuildPayloadEgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4069).
- [`HomerServiceExecutePayloadProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20968).
- [`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:20989).

Deliverables:

- Convert egress ready facts into candidate feasible-range facts for only the payload streams considered by the bounded slate.
- Convert the egress grant builder into a default/max grant helper.
- Carry `maxBytes`, `maxObjects`, `maxWrs`, `maxFragments`, and signal/notify defaults in the scheduler-visible grant.
- Executor revalidates and clamps downward. It must never post more bytes/objects/WRs than the selected grant.

Correctness checkpoint:

- Payload source-release and semantic frontiers remain correct.
- EOS and close/reclaim ordering is unchanged.
- Remote RDMA basebackup and backend-to-backend COPY complete correctly.

Performance checkpoint:

- Bulk-only basebackup/COPY throughput is not materially worse than baseline.
- Mixed foreground/bulk does not show worse foreground p99 from larger non-preemptive payload grants.

Unlocks:

- Real unified scheduling of payload posting against CQ drain, command/completion publication, and peer transport.

### Slice 3: bounded candidate slate with reservations

Goal:

```text
replace append-order candidate admission with bounded aggregate candidates,
reservations, spillover, cursors, and overflow debt
```

Code touch points:

- [`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26218).
- [`HomerServiceBuildProgressMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26219).
- Payload facts from [`HomerServicePopulatePayloadMachineFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25209).
- Peer phase bundling from [`HomerServiceMachineBaselineAppendPeerCollectorBundle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6396).

Deliverables:

- Build a slate, not the universe: global/control candidates, peer transport aggregate, clean CQ candidates, and K foreground plus K bulk payload streams by cursor.
- Add hard reservations for terminal visibility, resource relief, demanded observation, and close/lifetime.
- Add overflow counters and overflow debt by class/kind.
- Fold cheap payload frontier refresh into candidate construction only for considered streams; no full-stream `REFRESH_PAYLOAD_FRONTIERS` action.

Correctness checkpoint:

- Hard-class overflow should be zero under normal workloads. If not, the slate is too small or not aggregated enough.
- No c1 setup/liveness regression.
- No missed terminal completion or stream close/reclaim under mixed workload.

Performance checkpoint:

- Candidate-building CPU does not dominate the service loop.
- Mixed workload foreground tail should improve or stay flat; bulk should not collapse because of reservation mis-sizing.

Unlocks:

- The scheduler now has a controllable bounded work surface and meaningful overflow telemetry.

### Slice 4: command/payload lane-owned `DRAIN_SEND_CQ`

Goal:

```text
represent clean command/payload send-CQ retirement as standalone resource-relief
where the current owner model is already genuinely clean
```

Code touch points:

- [`HomerServiceBuildCqDrainProgressPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:9002).
- [`TupleSinkServiceDrainTaggedSendCompletionsInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1764).
- Completion routing in [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1554).

Deliverables:

- Introduce a lane/CQ owner ref for clean command/payload CQ drain when a current path satisfies the ownership invariant.
- Route command/payload completions through existing callbacks/stash paths where standalone drain is safe.
- Emit feedback: CQEs drained, empty polls, command completions, payload completions, resource pressure relieved.
- Keep peer-control send CQ under `PUMP_PEER_TRANSPORT`.
- If no current command/payload drain path has a clean owner ref, implement only the candidate/action/grant shape and feedback counters, and record exactly why each physical drain path remains inside an existing aggregate executor.

Correctness checkpoint:

- At least one clean physical CQ/lane drain is represented as `DRAIN_SEND_CQ`, or this slice records why no current drain path satisfies the ownership invariant.
- For any path migrated to `DRAIN_SEND_CQ`, command send source slots retire correctly.
- For any path migrated to `DRAIN_SEND_CQ`, payload source-release frontiers advance correctly.
- Mixed tagged completions on a migrated CQ route to the right owner.
- CQ errors still reset/fail the appropriate transport state.

Performance checkpoint:

- Resource-pressure relief latency improves or stays flat.
- CQ drain batching does not add excessive empty polling.

Unlocks:

- The scheduler can prioritize local resource relief before admitting more payload/command posting.

### Slice 5: peer-control ownership cleanup

Goal:

```text
make peer-control op advancement separable from broad peer transport pumping
```

Code touch points:

- [`TupleSinkServicePeerControlAsyncOpReady()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:226).
- [`TupleSinkServicePollPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7432).
- [`TupleSinkServiceTryPublishPeerControlAsyncOp()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7221).
- Local-control peer wait sites such as [`tuple_sink_service_process.c:22391`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22391) and [`tuple_sink_service_process.c:22971`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:22971).

Deliverables:

- Keep `TupleSinkServicePeerControlAsyncOpReady()` as the non-progressing fact probe.
- Move broad transport progress to `PUMP_PEER_TRANSPORT(phaseMask)`.
- Add or narrow an op terminal-consume helper that releases exactly one op slot without draining listener, response mailbox, send CQ, or close/lifetime work.
- Decide whether publication retry is op-owned only after connection readiness is known, or stays under peer transport until that ownership is explicit.

Non-observational helper invariant:

```text
After Slice 5, every helper used by ADVANCE_PEER_CONTROL_OP must be
non-observational with respect to peer transport.
```

Allowed behind `ADVANCE_PEER_CONTROL_OP`:

```text
inspect op state
publish one op request if all prerequisites are already known
consume one already-visible terminal response
release one op slot
update op-local facts
```

Not allowed behind `ADVANCE_PEER_CONTROL_OP`:

```text
poll CM/listener
drain recv CQ
drain send CQ
consume response mailbox
scan peer connection tables
make unrelated peer transport progress
```

If a helper still needs any of the forbidden work, it is not ready to sit behind
`ADVANCE_PEER_CONTROL_OP`; that work must remain under `PUMP_PEER_TRANSPORT` or
another observation/resource-relief action.

Correctness checkpoint:

- Existing local-control peer open/start paths still complete.
- Peer response visibility still comes from the response-mailbox/peer transport path, not hidden op polling.
- No op slot leak across success, failure, reset, and timeout paths.
- Hidden peer-transport progress counters for the op-owned helper path are zero.

Performance checkpoint:

- Peer transport table walks do not increase.
- Foreground c1/c4 remote command latency does not regress from extra op bookkeeping.

Unlocks:

- First-class `ADVANCE_PEER_CONTROL_OP`.

### Slice 6: add standalone `ADVANCE_PEER_CONTROL_OP`

Goal:

```text
schedule op-owned peer-control lifecycle advancement after ownership is clean
```

Code touch points:

- New action mapping beside existing peer/local/command action cases in [`HomerServiceExecuteProgressActionGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25860).
- Peer-control op facts from local-control and command dispatch continuations.

Deliverables:

- Add `ADVANCE_PEER_CONTROL_OP(op, maxSteps/maxOps/maxWrs)` as a standalone aggregate action.
- Keep `PUMP_PEER_TRANSPORT` responsible for listener/CM, recv CQ, request mailbox, response mailbox, send CQ, and close/lifetime until Slice 7.
- Add counters for op advances, terminal consumes, publication retries, and blocked-on-transport facts.

Correctness checkpoint:

- Op action must not make progress unless its guard is true.
- If response visibility is not known, the op candidate must express demanded observation instead of polling transport itself.

Performance checkpoint:

- Foreground command latency should improve or stay flat from avoiding repeated grants to blocked local-control continuations.

Unlocks:

- Cleaner policy control over peer-control continuations versus peer transport observation.

### Slice 7: migrate peer-control send CQ to `DRAIN_SEND_CQ`

Goal:

```text
make all tagged send-CQ retirement lane/CQ-owned
```

Code touch points:

- Peer send-CQ phase in [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:7795).
- Callback interface in [`TupleSinkServicePeerSendCompletionCallbacks`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:161).
- Control completion retirement in [`TupleSinkServiceHandleTaggedSendCompletion()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1631).

Deliverables:

- Add the missing control-completion callback/routing decision, or explicitly keep RDMA-layer internal retirement as the lane-owned behavior.
- Remove peer-control send-CQ work from `PUMP_PEER_TRANSPORT` phase admission.
- `DRAIN_SEND_CQ` reports command/payload/control completion counts separately.

Correctness checkpoint:

- Control send completions retire without op leaks.
- Response mailbox consumption remains separate from send-CQ retirement.
- Failure/reset behavior remains equivalent to the peer pump path.

Performance checkpoint:

- CQ/resource relief improves or stays flat under mixed command/payload traffic.
- Peer pump becomes cheaper when only mailbox/CM work is needed.

Unlocks:

- Clean lane-owned CQ drain across command, payload, and peer control.

### Slice 8: reaction policy

Goal:

```text
reduce foreground discovery-to-service delay only if counters prove it matters
```

Code touch points:

- Result flags in [`HomerProgressActionResult`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1725).
- Stop/replan hook in [`HomerProgressTransitionResult.stopAndReplan`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1749).
- Per-grant policy callback in [`HomerServiceExecuteProgressExecutionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26150).

Deliverables:

- Stage A: add `DISCOVERED_COMMAND`, `DISCOVERED_COMPLETION`, `UNBLOCKED_CRITICAL`, `RELIEVED_RESOURCE_PRESSURE`, and `WOULD_REPAIR_PLAN` counters.
- Stage B: add observation-heavy short plans.
- Stage C: add max-one stop/replan per pump after high-value discovery.
- Stage D: add a tiny scheduler-owned hot continuation array only if stop/replan cost is too high.

Correctness checkpoint:

- Executors never mutate plans.
- Continuations consume remaining budget and are deduped by owner/action.
- No infinite replan/continuation loop.

Performance checkpoint:

- Foreground discovery-to-service delay and p99/p999 improve enough to justify any extra candidate rebuild or hot-continuation overhead.
- Bulk throughput remains within the accepted band.

Unlocks:

- Adaptive grant sizing and signal/notify policy.

### Slice 9: adaptive batching

Goal:

```text
adapt grant sizes and signal/notify policy after the unified surface is stable
```

Deliverables:

- Adapt `maxBytes`, `maxObjects`, and `maxWrs` by action class, resource pressure, and recent foreground discovery delay.
- Adapt local signal policy only when outstanding signaled/completion pressure justifies it.
- Adapt remote notification policy only after correctness-preserving defaults are stable.

Correctness checkpoint:

- No semantic producer chunking changes in this slice.
- Remote publish/visibility rules remain consistent with the RDMA publication note.

Performance checkpoint:

- Bulk throughput improves without worsening foreground p99/p999.
- CQ pressure and source-release delay decrease under mixed load.

Unlocks:

- Later traffic-class and multi-QP policy experiments.

## Decisions that did not survive validation

- **Microscopic action candidates for every payload/peer subtransition.** This looked clean but would explode candidate count and force generic coalescing. Aggregate actions with subaction/phase masks are the chosen direction.
- **Generic `physicalKey/coalesceClass` as first-slice machinery.** The principle of physical coalescing survives, but the implementation should use explicit aggregate actions such as `PUMP_PEER_TRANSPORT(phaseMask)` and `ADVANCE_PAYLOAD_STREAM(subactionMask)`.
- **Standalone `REFRESH_PAYLOAD_FRONTIERS` in the first slice.** It risks becoming a hidden all-stream scanner. First-slice payload fact refresh belongs in bounded candidate construction.
- **Standalone `ADVANCE_PEER_CONTROL_OP` before ownership cleanup.** Current peer-op polling still performs broad transport progress, so the action boundary would be misleading.
- **Moving peer-control send CQ to `DRAIN_SEND_CQ` before routing cleanup.** The direction is to move it, but only after lane/CQ refs and control-completion ownership are explicit.

## Validation and metrics

Run the standard Homer gates after each meaningful stage:

```text
foreground-only remote Homer pgbench
bulk-only remote RDMA basebackup
mixed remote pgbench plus background RDMA basebackup
backend-to-backend Citus tuple COPY
cold c1 setup/reconnect/liveness
synthetic CQ/resource-pressure check, if available
```

Counters to add early:

```text
candidate count by kind/class
candidate overflow by kind/class
hard-class overflow debt
grant count by kind/class
bytes/objects/WRs per payload grant
CQEs drained per lane/CQ grant
control/command/payload completion split
empty/productive observation grants
discovered-hot-work to first-service delay
bulk bytes/WRs executed after hot discovery
stop/replan count
hot continuation count
grant clamp reasons
resource-pressure relief latency
hiddenTransportProgressInPeerOpPoll
hiddenCqDrainInOpHelper
hiddenMailboxDrainInOpHelper
hiddenPayloadGrantExpansion
hiddenFullStreamScan
peerOpPollTransportProgressEvents
peerOpPollSendCqDrains
peerOpPollResponseMailboxConsumes
peerOpPollConnectionEventDrains
```

Acceptance is not "more scheduler sophistication." Acceptance is:

```text
foreground command/completion p99/p999 improves or stays stable
bulk basebackup/COPY throughput is preserved
service CPU does not grow materially
candidate overflow is controlled
no setup/liveness regressions
peer-control ownership boundaries are cleaner after each staged split
```

## Interactions and cross-links

- [`service_progress_control_data_plane_scheduler.md`](service_progress_control_data_plane_scheduler.md) is the canonical current-state note for the existing `machine-baseline` scheduler, including the state-machine plus collector milestone history.
- [`homer_transport_scheduler_and_payload_streams.md`](homer_transport_scheduler_and_payload_streams.md) is the broader transport scheduler and payload-stream future-direction note. This note narrows the next scheduler redesign into the unified aggregate-action milestone.
- [`homer_payload_control_unification_plan.md`](homer_payload_control_unification_plan.md) covers payload/control lifecycle unification that this scheduler plan depends on but does not replace.
- [`peer_client_completion_publication_pipeline_plan.md`](peer_client_completion_publication_pipeline_plan.md) is the narrower prerequisite cleanup for cross-node client-SQL completion publication. It should land before the scheduler migration depends on staged `PUBLISH_COMMAND_COMPLETIONS` and source-credit-driven `DRAIN_SEND_CQ` facts.
- [`rdma_publication_visibility_and_doorbells.md`](rdma_publication_visibility_and_doorbells.md) remains the publication/visibility grounding for local signal and remote notification policy.

## Open questions / TODO

- Choose the first concrete `HomerGrantVector` field names and whether it replaces or wraps `HomerProgressGrant` during migration.
- Decide the initial candidate slate size and hard reservation counts. Start with a small value, but record overflows before tuning. **First evaluate the "eliminate the reservation-count knob" refinement** (see "Reservation, sizing, and eliminating the dangerous knob"): if bounded cheap OBSERVE demand is admitted exhaustively, there may be no hard-reservation *count* to choose at all — only a bulk size clamp. Confirm the per-pass exhaustive-OBSERVE cost first.
- Define lane/CQ owner refs for the first standalone `DRAIN_SEND_CQ` implementation.
- Decide whether peer request publication remains inside peer transport until connection readiness is explicit, or becomes an op-owned bounded step sooner.
- Add a control-completion callback or document why RDMA-layer internal control retirement is safe under lane-owned `DRAIN_SEND_CQ`.
