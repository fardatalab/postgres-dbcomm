# Citus Transport Implementations

<!-- kb-summary: Current implementation checkpoints, plans, and high-signal contracts for the Homer transport stack. -->

## Purpose

Track implementation checkpoints for Homer transport-layer prototype work in
Citus, including RDMA substrate changes that are separate from the higher-level
remote execution session semantics.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [CONTRACTS.md](CONTRACTS.md) - **READ BEFORE CHANGING THIS IMPLEMENTATION AREA.** Current high-signal ownership, identity, lifecycle, geometry, and teardown contracts for Homer transport.
- [byte_ring_payload_stream_checkpoint.md](byte_ring_payload_stream_checkpoint.md) - Current checkpoint for the service-to-service byte-ring RDMA substrate used by Homer basebackup streams.
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) - Regression analysis separating synthetic byte-ring geometry from negotiated record capacity and the unrelated broken COPY path.
- [copy_path_revival_contract.md](copy_path_revival_contract.md) - Coverage, invariants, refuted suspects, and prerequisites for eventually reviving backend-to-backend COPY.
- [cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md) - Implementation checkpoint for migrating Homer command, completion, and payload paths across the two-host DPU topology.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) - Design and staged plan for per-session DPU byte-ring resources and their lifetime protocol.
- [dpu_collector_admission_starvation_mixed_workload.md](dpu_collector_admission_starvation_mixed_workload.md) - The mixed pgbench+basebackup hang: the control-mailbox fix (citus 6392a1853) was REFUTED as the cause by a reverted-build causation run (both pass, zero starvation); the passes are procedural (pre-arming) and the real cause — a connection-lifecycle/reuse interaction — is OPEN again. Records eight refuted causes and three defects fixed along the way.
- [dpu_collector_feedback_aliasing_defect_b.md](dpu_collector_feedback_aliasing_defect_b.md) - Active diagnosis and remediation plan for DPU collector feedback aliasing and starvation suppression.
- [dpu_command_plane_migration_plan.md](dpu_command_plane_migration_plan.md) - Detailed staged plan and decisions for moving the Homer SQL command plane onto the DPUs.
- [dpu_crossnode_command_completion_bridges_design.md](dpu_crossnode_command_completion_bridges_design.md) - Design and implementation steps for selected-DPU cross-node command and completion bridges.
- [dpu_discovery_latency_measurement.md](dpu_discovery_latency_measurement.md) - S6 Track B measurement of DPU per-command discovery latency D (~38µs, cadence-limited): method, the two lifetime bugs found, results, and the still-open bottleneck question.
- [dpu_dma_backend_homer_service_implementation_checkpoint.md](dpu_dma_backend_homer_service_implementation_checkpoint.md) - Stage-by-stage implementation checkpoint for the DPU-pull Homer service migration.
- [dpu_gate_concurrency_limits.md](dpu_gate_concurrency_limits.md) - Current DPU gate concurrency limits imposed by the byte-ring pool and related fixed-capacity resources.
- [dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) - Current DPU payload byte-ring wrap protocol, mirror geometry, egress parsing, and invariants.
- [homer_current_implementation_checkpoint.md](homer_current_implementation_checkpoint.md) - Historical Homer implementation checkpoint predating the selected-DPU command-plane migration.
- [payload_doorbell_token_v15_checkpoint.md](payload_doorbell_token_v15_checkpoint.md) - Landed checkpoint for payload doorbell tokens and fixed service-owned payload-ready queues.
- [peer_client_completion_v27_checkpoint.md](peer_client_completion_v27_checkpoint.md) - Landed checkpoint for peer-client completion mailbox v27 and full-slot publication.
- [peer_recv_dispatcher_v14_checkpoint.md](peer_recv_dispatcher_v14_checkpoint.md) - Landed checkpoint for peer protocol v14 immediate-data ABI and permanent receive-CQ dispatch.
- [post_gate_basebackup_open_stall.md](post_gate_basebackup_open_stall.md) - Diagnosis and resolution record for the basebackup open stall discovered after the DPU gate.
- [resource_retirement_contract_audit.md](resource_retirement_contract_audit.md) - Ordered Homer transport resource-retirement audit, implementation decisions, evidence, and validation history.
- [s6_native_homer_dpu_and_latency_plan.md](s6_native_homer_dpu_and_latency_plan.md) - Two-track S6 plan: fold native `--homer-dpu` onto the selected-DPU command path (no host service), and attribute per-command latency now that the synchronous START round trip is already gone.
- [s7_host_service_retirement_plan.md](s7_host_service_retirement_plan.md) - The three-bucket (DELETE / KEEP / GUT-THE-ARM) map of the host-service Homer surface that S7 retires, verified against code, with per-sub-item checklists, boundary hazards, and the one open scoping decision.
- [selected_dpu_session_rings_and_lifecycle.md](selected_dpu_session_rings_and_lifecycle.md) - Architecture of record for selected-DPU session identities, ring roles, exports, and lifecycle.
- [send_cqe_coalescing_implementation_plan.md](send_cqe_coalescing_implementation_plan.md) - Ordered stage plan for re-arming send-CQE coalescing on the peer RDMA transport, with the 2026-07-15 site-enumeration corrections to the design.
- [tuple_deform_two_ring_relay_overview.md](tuple_deform_two_ring_relay_overview.md) - End-to-end overview of tuple deformation and the two-ring selected-DPU SQL-result relay.
<!-- kb-docs:end -->

## Related

- `../../../operations/`: **How to actually run and validate any of this.** The farnet runbook's backing
  store: every check that lies (`farnet_operational_hazards.md`) and the diagnostic instruments +
  measurement history (`farnet_diagnostics_and_baselines.md`). Most hazards there were discovered while
  validating the docs in *this* directory.
- `../../../future-directions/citus/transport/`: Transport scheduler, payload
  stream, and RDMA publication design notes.
- `../../../future-directions/citus/transport/dpu_readiness_collectors_and_continuation_edges.md`:
  Root cause behind several decisions in `dpu_command_plane_migration_plan.md` (D5 reverse cookie,
  D6 forward index, D8 arena publish-line reservation). Read before changing them.
- `../../../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`: Ranked map of the
  ten sites where the DPU scheduler arms from exact demand and executes by linear scan. Finding 1 is on
  the basebackup egress path and is unscheduled.
- `../../../implementations/postgres/replication/`: PostgreSQL basebackup target
  integration notes that feed this transport path.
- `selected_dpu_session_rings_and_lifecycle.md`: **Start here** for the selected-DPU
  path. `homer_current_implementation_checkpoint.md` and the other checkpoint docs are
  milestone history; several of their code pointers and premises predate the
  command-plane migration.
