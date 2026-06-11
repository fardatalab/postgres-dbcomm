# Homer Service-Progress State-Machine Scheduler

## Current Status

The Homer service-progress scheduler milestone is complete as of June 9, 2026.
The default service-progress policy is now the state-machine aware
`machine-baseline` policy, selected at service startup by
[`HomerServiceReadProgressPolicyEnv()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:4207).

The current default startup line is:

```text
tuple-sink service: HOMER_PROGRESS_POLICY=machine-baseline max_plan=16 max_collectors=6 max_machines=12 max_payload=2 max_blind_collectors=2
```

This note is the canonical current-state and future-direction summary. It
replaces the earlier implementation diary that accumulated per-slice validation
records while the milestone was in flight.

## Milestone History And Design Corrections

Keep this section as the compact history of how the current design emerged. It
is intentionally not a turn-by-turn log; it records the steps, issues, and
decisions that matter for future work.

1. **Started from a source-plan scheduler substrate.**
   The earlier scheduler-ready milestone built policy selection, ready sets,
   source refs, typed grants, result/feedback accounting, and payload egress
   grants. That made scheduling pluggable, but the first-layer input was still
   mostly flat sources. It was useful scaffolding, not the right final boundary
   for async Homer work.

2. **Tried policy-level reordering and hit hidden liveness dependencies.**
   Experiments with adaptive/class reordering exposed remote c1 liveness
   failures: local control, peer transport, command send, completion publication,
   and payload progress were not independent sources. Fixed ordering was carrying
   implicit dependency information that the policy could not see. This changed
   the design goal from "smarter source ordering" to "schedule async machines
   plus event collectors."

3. **Pivoted to state machines plus collectors.**
   The new boundary models higher-level async work as machines and models
   discovery/visibility work as collectors. This is why
   [`HomerProgressMachineKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1488),
   [`HomerProgressCollectorKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1501),
   and [`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1519)
   exist separately. The important design correction is that a state machine
   represents work already in the system; collectors are still required to
   discover unknown external arrivals.

4. **Split action families, but kept physical coalescing where performance
   required it.**
   Local control gained separate slot-collection and async-advance action
   families. Payload gained selected action masks for outgoing, incoming,
   send-CQ, credit publish, EOS, and close/reclaim. Peer control gained split
   scheduler identities for setup/listener, recv CQ, request mailbox, response
   mailbox, send CQ, and close/lifetime. A pure physical six-action peer split
   was rejected because it repeated peer connection-table scans and hurt mixed
   workload performance. The accepted design keeps split scheduler facts and
   feedback while coalescing selected peer phases into one physical peer grant.

5. **Moved periodic/cold work toward policy-owned cooldowns.**
   Hardcoded periodic work such as peer polling and local-control async polling
   was reframed as collector due/backoff policy. The first baseline keeps
   conservative cooldowns and cheap feedback, but it makes the ownership visible
   in policy state rather than hiding all periodic work inside readiness
   predicates.

6. **Added dependency-demand collector boosts.**
   Machines expose `waitingOnMask`; the bridge in
   [`HomerServiceAppendCollectorDemandForMachineWait()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25029)
   turns those waits into collector demand. This fixed the core issue where a
   policy could grant a blocked machine while failing to grant the collector that
   would make the machine's next action visible.

7. **Fixed peer setup/listener facts after a real c1 liveness failure.**
   An implementation slice that made peer setup depend only on exact known setup
   facts could starve listener polling. Remote c1 then timed out during outgoing
   RDMA setup because the reverse incoming RDMA-CM event was unknowable until the
   listener was polled. The fix was to maintain exact setup counts in the peer
   transport while still admitting listener polling as due-only blind work.

8. **Rejected misleading "known work" for due-only listener polling.**
   Treating `listenerPollDue` as `knownExpectedWork` would make a speculative
   listener poll bypass blind-collector budgeting and would mislead future
   adaptive policies. The accepted rule is: incomplete setup counts are known
   work; listener polling is due-only work.

9. **Tuned baseline defaults conservatively.**
   `max_payload=1` hurt mixed c4 pgbench. Raising only blind collector budget did
   not materially improve mixed c4. Larger plan admission could improve pgbench
   modestly but sometimes stretched basebackup. The accepted default therefore
   favors basebackup-stable mixed correctness over a slightly higher foreground
   number; adaptive admission is deferred to the next policy milestone.

Decisions that did not survive validation:

- **Arbitrary adaptive/class reordering on flat sources.** Reordering looked like
  the natural next step after the scheduler-ready substrate, but remote c1
  liveness failures showed that flat sources hid dependencies among local
  control, peer control, command send, completion publication, and payload
  progress. This invalidated "just reorder sources better" as the main design.
- **Treating peer setup as exact-only work.** Exact incomplete setup facts are
  useful, but they cannot discover a new incoming RDMA-CM event. Listener polling
  must remain schedulable as unknown-arrival work.
- **Treating due listener polling as known expected work.** This fixed liveness
  superficially but gave speculative listener polls the same policy meaning as
  real incomplete setup, bypassing blind-poll budgets and misleading future
  adaptive policy.
- **A pure physical split for every peer phase.** Separate peer scheduler facts
  survived; separate peer connection-table scans did not. The accepted design
  coalesces selected peer phases into one physical transport grant.
- **Loose static plan admission as the milestone default.** Bigger plans can help
  foreground pgbench in one mixed run, but validation showed basebackup can
  stretch. Static looseness is not the right replacement for an adaptive policy.

## Problem

The older service-progress scheduler treated command rings, completion rings,
payload streams, peer CQs, peer mailboxes, and maintenance as mostly independent
sources. That was enough to build a scheduler substrate, but it hid a more
important structure: most Homer service work is asynchronous state-machine
progress.

One action often creates, blocks, or unblocks the next action. Examples:

- a local-control request starts a peer-control request and then waits for peer
  response visibility
- a command session may wait for backend completion, result payload EOS, or
  send-CQ/source-release before publishing terminal completion
- a payload stream may be blocked on remote credit, local send resources,
  incoming doorbells, send-CQ retirement, or close/reclaim
- peer listener/CM polling discovers incoming connection setup that is otherwise
  unknowable to local state

The state-machine scheduler moves the first-layer scheduling boundary from
"which flat source should be polled now?" toward "which collector or async
machine should be advanced, and with what bounded grant?"

## Design Model

The implementation remains fixed-table and switch-based. The scheduler should
not allocate heap work items or use per-machine vtables in the hot loop.

Core types:

- [`HomerProgressMachineKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1488):
  high-level async work families.
- [`HomerProgressCollectorKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1501):
  event/fact collectors that discover new work or refresh prerequisites.
- [`HomerProgressActionKind`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1519):
  small nonblocking action families that grants can execute.
- [`HomerProgressMachineFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1639):
  policy-readable machine state, ready action mask, blocked/waiting masks, traffic
  class, CPU class, and byte/object/WR hints.
- [`HomerProgressCollectorFacts`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1664):
  policy-readable collector due state, known expected work, dependency waiters,
  recent feedback, and ready-item hints.

The service loop builds a candidate snapshot in two phases:

1. [`HomerServiceBuildProgressCollectorCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25467)
   builds collector candidates from the current coarse ready set plus maintained
   direct facts.
2. [`HomerServiceBuildProgressMachineCandidates()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25396)
   builds machine candidates and dependency-demand collectors.

The `machine-baseline` policy consumes those candidates in
[`HomerServiceBuildMachineBaselineProgressActionPlan()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6671)
and emits typed action grants. The policy is intentionally conservative: it is a
validated baseline for the state-machine boundary, not the final adaptive
scheduling policy.

## Machine And Collector Families

Current machine families:

- local control request/async continuation
- remote client-SQL sender
- command session
- payload stream
- peer control operation
- peer connection/lifetime
- maintenance

Current collector families:

- local control slots
- frontend command ring
- command send CQ
- backend completion ring
- peer CM/setup and listener polling
- peer recv CQ
- peer request mailbox
- peer response mailbox
- peer send CQ
- peer close/lifetime
- payload frontier
- heartbeat/maintenance

Collectors and machines are intentionally distinct. A collector grant is work
the policy chose because it is due, has known expected work, or is demanded by a
blocked/waiting machine. A machine grant advances higher-level async state once
the relevant action is ready.

## Facts, Feedback, And Dependencies

The scheduler distinguishes three shapes of work:

- **Known expected work:** local state says useful work should exist, such as a
  request-ready slot, incomplete peer setup, outstanding WRs, published payload
  bytes, or a ready completion record.
- **Known blocked work:** a machine exists but cannot currently progress, such
  as remote-credit blockage, local send-resource pressure, completion waiting on
  payload EOS, or source release waiting on send-CQ retirement.
- **Unknown external arrival:** work may exist on the remote side, but the
  service can only discover it by polling or consuming a notification/doorbell.
  Peer listener/CM polling is the clearest example.

Dependency demand is represented generically. A machine records `waitingOnMask`;
[`HomerServiceAppendCollectorDemandForMachineWait()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25029)
maps that wait mask to collector demand. The policy then grants demanded
collectors before spending the rest of the plan on runnable machines.

Feedback remains cheap. Collector facts read recent empty/productive state
through
[`HomerServiceProgressCollectorFeedbackForKind()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:24788).
Blind collector backoff is implemented by
[`HomerMachineBaselineCollectorFeedbackBackoffActive()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6061).
It deliberately does not suppress known expected work, dependency-demanded
collectors, or otherwise idle service progress.

## Peer-Control Boundary

Peer control is split semantically but partly coalesced physically.

Split collector identities exist for CM/setup, recv CQ, request mailbox,
response mailbox, send CQ, and close/lifetime. The phase-to-transport mapping is
centralized in
[`HomerServiceMachineBaselinePeerPhaseMaskForCollector()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6375).

When multiple peer phases are selected, the baseline policy coalesces them into
one physical `ADVANCE_PEER_CONNECTION` grant through
[`HomerServiceMachineBaselineAppendPeerCollectorBundle()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6396).
This is intentional. The scheduler boundary still sees split facts and split
feedback, but the executor can walk the peer connection tables once and apply a
phase mask. A pure six-action peer split was cleaner conceptually but repeated
the transport walk and regressed mixed workload performance.

Peer feedback from the bundled grant is fanned back out by
[`HomerServiceFinishPeerControlProgressGrant()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8638),
so the policy can still learn which selected peer phase was empty or productive.

## Peer Setup Facts

Peer setup/listener progress is the main place where exact state-machine facts
are not enough by themselves.

The RDMA transport now maintains scheduler facts through lifecycle transitions:

- [`TupleSinkServiceRegisterPeerConnectionFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:672)
  records newly posted outgoing setup or accepted incoming setup.
- [`TupleSinkServiceMarkPeerConnectionSetupComplete()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:700)
  clears setup-pending counts when bootstrap completes.
- [`TupleSinkServiceUnregisterPeerConnectionFacts()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:728)
  removes active/pending setup facts on reset.
- [`TupleSinkServiceGetPeerSchedulerFactsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:5245)
  exposes active connection count, incomplete setup counts, listener poll due
  state, recent listener events, and currently-zero close/lifetime work.

Important pitfall: `listenerPollDue` is not `knownExpectedWork`. It is due-only
blind work. A new incoming RDMA-CM connect request is unknowable until the
listener is polled. If the scheduler admits `PEER_CM_SETUP` only when exact
incomplete setup already exists, listener polling can starve and reverse setup
can time out. If the scheduler marks every due listener poll as known work, it
bypasses blind-collector budgeting and misleads future adaptive policies. The
current bridge keeps incomplete setup as known work and listener polling as
due-only work.

Close/lifetime has a collector identity, but currently no deferred
close/reclaim queue. `closeLifetimeWorkCount` is zero until a real deferred
teardown state exists.

## Current Hyperparameters

The baseline policy caps each generated plan with startup-configurable
hyperparameters. Defaults are defined near
[`HOMER_SERVICE_MACHINE_BASELINE_MAX_GRANTS`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:147)
and read in
[`HomerMachineBaselinePolicyInitialize()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7298).
They are enforced by
[`HomerMachineBaselineBudgetTryReserve()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6116).

Defaults:

- `max_plan=16`: total action grants per plan.
- `max_collectors=6`: collector grants per plan.
- `max_machines=12`: machine grants per plan.
- `max_payload=2`: payload-machine grants per plan.
- `max_blind_collectors=2`: speculative polling grants where useful work is not
  exactly known.

The second blind collector grant is intentional. Mixed foreground pgbench plus
background basebackup needs room for peer/listener liveness while still admitting
another blind collector in the same decision window.

## Validation

Final default-policy validation used no `HOMER_PROGRESS_POLICY` or
`HOMER_MACHINE_BASELINE_*` environment overrides.

Runtime hashes matched on `farnet1` and `farnet0`:

- `citus_tuple_sink_service`:
  `071d542942ce3c076fb38e4ef54bd2421bedc4720e4b3201670e0a357b25c2e8`
- `citus.so`:
  `858a6c95ba8bd987f906a02eb3757d976a160453631f7cb8dbbac0cb67e0f5b6`
- `libhomer_client.a`:
  `0de53a829c686a6e55dd3fae1a3d67dcd86a053601f58c5c725552a8a97c8ea7`

Build/install/sync gates passed:

- `git clang-format HEAD -- src/backend/distributed/utils/homer/tuple_sink_service_process.c`
- `git diff --check` and `git diff --cached --check`
- `sudo -n -u dbcomm make -j8 service-bin client-bin`
- `sudo -n make install-headers install-service-bin install`
- targeted installed-artifact sync to `farnet0` with matching hashes

Runtime gates:

- Remote Homer pgbench, artifacts in
  `/tmp/homer_machine_default_final_pgbench_1781007328`: c1 cold/setup run
  `3242.75 TPS`, then warmed `4524.51 TPS`, p99 `0.240 ms`; c4 repeats
  `10621.87` and `10551.70 TPS`, p99 `0.619` and `0.625 ms`; zero failures.
- Remote RDMA basebackup, artifacts in
  `/tmp/homer_machine_default_final_basebackup_1781007357`: warmup `5.96 s`,
  then warmed `4.47` and `4.47 s`.
- Mixed remote c4 Homer pgbench plus background remote RDMA basebackup, artifacts
  in `/tmp/homer_machine_default_final_mixed_c4_1781007385`: pgbench completed
  `10000/10000` with zero failures at `8935.90`, `8912.24`, and `8838.18 TPS`;
  basebackup completed in `4.60`, `4.61`, and `4.59 s`.
- Backend-to-backend Citus tuple COPY through Homer, artifacts in
  `/tmp/homer_machine_default_final_copy_10m_1781007621`: four repeats copied and
  verified `10,000,000` rows with `count=10000000`, `min=1`, `max=10000000`,
  and `sum=500000050000000`. The first run was warmup at `8.44 s`; warmed
  repeats were `5.19`, `5.84`, and `5.10 s`. Service logs on `farnet0` confirmed
  the Homer byte-ring service-to-service path with `published_tail=512005120`.

Final process preflight showed only the intended PostgreSQL postmasters and
`citus_tuple_sink_service` instances on both hosts. Final service-log signature
checks found no `error`, `failed`, `reset`, `broken`, `stale`, `could not`,
`invalid`, `timeout`, or `panic` lines.

## Rejected Or Deferred Choices

Rejected during this milestone:

- Marking `listenerPollDue` as `knownExpectedWork`. It made due-only blind
  listener polling bypass blind-collector budgeting.
- `HOMER_MACHINE_BASELINE_MAX_PAYLOAD_GRANTS=1`. In
  `/tmp/homer_machine_payload1_ab_1781006792`, warmed mixed c4 pgbench fell to
  `7550.37 TPS` with p99 `1.136 ms` while basebackup stayed around `4.65 s`.
- Raising only `max_blind_collectors` from `2` to `4`. In
  `/tmp/homer_machine_blind4_mixed_c4_1781007460`, warmed mixed c4 was
  `8957.07 TPS` with basebackup `4.59 s`, not materially better than default.
- Making the current default a loose large-admission shape. A loose diagnostic
  (`max_plan=64`, `max_collectors=16`, `max_machines=64`, `max_payload=8`,
  `max_blind_collectors=4`) reached `9033.59 TPS` with basebackup `4.58 s` in
  `/tmp/homer_machine_loose_budget_mixed_c4_1781007505`, but a narrower
  large-admission shape with `max_payload=2` stretched one warmed basebackup
  repeat to `6.15 s` in `/tmp/homer_machine_large_admit_mixed_c4_1781007556`.

The accepted default therefore stays conservative for basebackup stability.
Larger/adaptive plan admission belongs in the next adaptive-policy research
milestone.

## Future Adaptive Policy Work

The current scheduler is state-machine aware but intentionally not a high-quality
adaptive policy. The next research milestone should focus on policy quality, not
on changing the core abstraction again.

Promising directions:

- adaptive plan admission: choose plan length from current class mix, dependency
  pressure, and recent productivity instead of static caps
- class deficits/credits for foreground command work, hot control work, payload
  data movement, close/lifetime, and cold setup/maintenance
- feedback-driven burst sizing using recent empty/productive streaks and grant
  utilization
- stronger maintained facts for currently blind collectors, especially mailbox
  visibility, recv-CQ productivity, and command/backend-completion timing
- better preemption/stop-and-replan triggers when collectors discover foreground
  work or a dependency unblocks
- eventual constrained in-place plan repair, after stop-and-replan behavior is
  measured and understood

Keep two principles:

1. Unknown external arrivals still require collector policy. State machines
   model async work after it is represented; collectors are how some work enters
   that world.
2. Split scheduler facts do not require physically separate scans. Coalescing is
   acceptable when it preserves the semantic scheduler boundary and avoids
   repeated hot-path walks.

## Related

- [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md):
  earlier scheduler-ready substrate and payload-stream design.
- [homer_payload_control_unification_plan.md](homer_payload_control_unification_plan.md):
  planned payload/control unification work that feeds the scheduler fact model.
- [peer_service_push_completion_implementation_plan.md](peer_service_push_completion_implementation_plan.md):
  peer completion-ring work that reduced poll-based command completion.
- [rdma_publication_visibility_and_doorbells.md](rdma_publication_visibility_and_doorbells.md):
  RDMA publication/doorbell visibility rules that payload and peer collectors
  must preserve.
