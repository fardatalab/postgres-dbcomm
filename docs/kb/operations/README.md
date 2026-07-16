# Operations

<!-- kb-summary: Operational runbooks, hazards, diagnostics, baselines, and deterministic validation tooling for the farnet Homer research rig. -->

## Purpose

The **operational** knowledge for the farnet two-host + two-DPU prototype rig: the traps that make a check lie
to you, the diagnostic instruments, and the measurement history.

This tree is the **backing store for the repo `CLAUDE.md`/`AGENTS.md`**. That file is the *runbook* — facts and
canonical commands, kept short enough to actually be read. Everything it deliberately leaves out is here:

- **Why** each of its rules exists, and the incident that proved it. A rule without its incident is a rule the
  next reader will "simplify" back into the bug.
- The **diagnostic** builds and batteries, which are for when something is *already* broken — not routine
  preconditions.
- The **numbers** we have actually measured, each stamped with a date and a commit, several now known stale.

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
- [farnet_validation_runner_plan.md](farnet_validation_runner_plan.md) - Implementation plan and current status for the deterministic, phase-based farnet validation runner that converts the manual multi-host Homer acceptance procedure into explicit profiles, resumable evidence, and machine-readable verdicts.
- [farnet_diagnostics_and_baselines.md](farnet_diagnostics_and_baselines.md) - Diagnostic builds (`starve-diag`, the stats macros, `HOMER_REMOTE_EXEC_TRACE`) and how to READ their output; the RDMA/RoCE link battery and the open cross-lane L2-domain failure; the measurement history (P7's 1450ms->10ms RNR fix, the pgbench TPS bands, the STALE June-6 COPY baseline); and the broken command shapes kept out of the runbook so nobody copy-pastes them.
- [farnet_operational_hazards.md](farnet_operational_hazards.md) - Every check in this project that LIES, with the incident that proved it: `pkill -f` matching its own argv; `/proc/<pid>/exe` and the non-optional trailing `*`; the SECOND PostgreSQL on farnet1 (same `dbcomm` user, port 5432) that silently swallows `pgbench -i`; sticky `make -B` stats builds; the shm object whose version is in its NAME; `-Wswitch` protecting nothing; and the subagent that reverted 30 edits.
<!-- kb-docs:end -->

## Related

- `../../../CLAUDE.md` (→ `AGENTS.md`): the runbook these docs back. Every ⚠ rule there points here.
- `../implementations/citus/transport/`: the transport implementation checkpoints. Most hazards here were
  discovered while validating those.
- `../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`: the scheduler arm/execute
  mismatch that `starve-diag` exists to instrument.
