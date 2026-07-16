# Operations

<!-- kb-summary: Operational runbooks, hazards, diagnostics, baselines, and deterministic validation tooling for the farnet Homer research rig. -->

## Purpose

The **operational** knowledge for the farnet two-host + two-DPU prototype rig: the canonical operator workflow,
the traps that make a check lie, the diagnostic instruments, and the measurement history.

Repository `AGENTS.md` is the concise auto-loaded policy/router. Exact mutable rig facts and commands live in
`farnet_operator_runbook.md`; the other leaves separate rationale, diagnostics/history, and runner readiness:

- **Why** each of its rules exists, and the incident that proved it. A rule without its incident is a rule the
  next reader will "simplify" back into the bug.
- The **diagnostic** builds and batteries, which are for when something is *already* broken — not routine
  preconditions.
- The **numbers** we have actually measured, each stamped with a date and a commit, several now known stale.
- The deterministic runner's implementation and authority boundary while live execution remains fail-closed.

The organizing idea behind almost everything in `farnet_operational_hazards.md`:

> An **identity check** — "is this the right process / binary / shared-memory object / database / build?" —
> whose negative answer is indistinguishable from "I could not tell." **Prefer checks whose failure mode is
> LOUD.** A check that reports "clean" when it could not look is strictly worse than no check, because it
> manufactures confidence.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [farnet_agent_runbook_split_inventory.md](farnet_agent_runbook_split_inventory.md) - Durable source-to-destination and shell-block inventory used to prove that shrinking AGENTS.md preserves every current farnet procedure, warning, prerequisite, and ordering dependency in the correct canonical document.
- [farnet_agent_runbook_split_plan.md](farnet_agent_runbook_split_plan.md) - Plan and migration record for keeping repository AGENTS.md policy-focused while preserving exact farnet machine, build, deployment, workload, and evidence procedures in a canonical operations runbook.
- [farnet_diagnostics_and_baselines.md](farnet_diagnostics_and_baselines.md) - Diagnostic builds and network batteries, SHA-stamped measurement history, and intentionally non-runnable broken command shapes for the farnet Homer rig.
- [farnet_operational_hazards.md](farnet_operational_hazards.md) - Incident-backed operational hazards for farnet process, database, artifact, shared-memory, tool, and validation identity checks whose failure modes can manufacture a false result.
- [farnet_operator_runbook.md](farnet_operator_runbook.md) - Canonical topology, artifact preparation, role lifecycle, workload, evidence, and teardown procedures for manual operation of the farnet Homer rig while deterministic live runner execution remains disabled.
- [farnet_validation_runner_plan.md](farnet_validation_runner_plan.md) - Implementation plan and current status for converting the manual multi-host Homer acceptance procedure into explicit profiles, resumable evidence, and machine-readable verdicts.
<!-- kb-docs:end -->

## Related

- `../../../AGENTS.md` (`CLAUDE.md` symlinks to it): concise policy, task triggers, acceptance matrix, and routing.
- `../implementations/citus/transport/`: the transport implementation checkpoints. Most hazards here were
  discovered while validating those.
- `../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`: the scheduler arm/execute
  mismatch that `starve-diag` exists to instrument.
