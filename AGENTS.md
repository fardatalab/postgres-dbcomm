# Homer / farnet project policy

This file is the always-loaded policy for the two-host, two-DPU Homer research rig. Exact topology, build,
deployment, workload, evidence, and teardown commands live in
[`docs/kb/operations/farnet_operator_runbook.md`](docs/kb/operations/farnet_operator_runbook.md).

The runbook is authoritative for the current manual workflow. Incident rationale lives in
[`farnet_operational_hazards.md`](docs/kb/operations/farnet_operational_hazards.md), diagnostic instruments and
timestamped measurements in
[`farnet_diagnostics_and_baselines.md`](docs/kb/operations/farnet_diagnostics_and_baselines.md), and deterministic
runner readiness in [`farnet_validation_runner_plan.md`](docs/kb/operations/farnet_validation_runner_plan.md).

## Task trigger matrix

| task | mandatory reading before acting |
|---|---|
| Explain/review Homer code | The narrowest `docs/kb/implementations/**/CONTRACTS.md`, then cited current code |
| Build, install, sync, deploy, start/stop a role, or run a workload | The applicable operator-runbook sections and linked hazard rules |
| Live farnet validation or benchmark | This file, the **entire** operator runbook, hazards, runner-plan **Status** and **Current next step**, plus the task's implementation plan |
| Diagnose a broken run | Hazards first, then the applicable diagnostics and implementation contract; localize the first failing frontier before patching |
| Change a validation-bearing lifecycle, identity, or teardown site | Stable-event policy below and the owning implementation `CONTRACTS.md` |

This matrix applies to main agents and implementation workers, not only dedicated validation agents. The operator
runbook must be reread when mutable topology, paths, or runner readiness may have changed.

## Project boundaries

- PostgreSQL fork: `/data/dbcomm/postgres-citus`.
- Citus/Homer fork: `/data/dbcomm/citus-dbcomm`.
- Stable logical install prefix on both hosts: `/data/dbcomm/pg-citus`; runtime user: `dbcomm`.
- Farnet0's logical `/data/dbcomm` resolves through `/home/dbcomm/data-dbcomm`; farnet1 stores it on the real `/data`
  filesystem. Compare canonical process identities, but keep configuration and commands on the logical path.
- Some branch families use suffixed sibling worktrees. Confirm the active worktrees before using an example path.
- Build/install/runtime artifacts belong to `dbcomm`; source, docs, and Git metadata belong to the human developer.
  Citus in-tree `.deps` and `build-cdc-*` directories are generated artifacts and belong to `dbcomm`.

## Non-negotiable safety rules

1. **Our PostgreSQL is port 5433 and data directory `/data/dbcomm/pg-citus/data`.** Farnet1 also runs another
   project's PostgreSQL on 5432 under the same Unix user. Never stop, query, or initialize against it. Every
   `psql`, `pgbench`, and `pg_basebackup` command must name `-p 5433` and validation must prove the data directory.
2. **Never use `pkill -f`, `pgrep -f`, or `ps | grep` as process identity.** Resolve `/proc/PID/exe`, including the
   `sudo -n readlink` fallback and deleted-executable suffix; unreadable user identity is `INCONCLUSIVE`, not clean.
3. **Never `kill -9` a live `postgres: remote exec backend`.** Stop the intended postmaster first, then reap the
   socketless backend by verified executable identity during teardown.
4. **Write multi-line and second-hop operations to files and invoke them by path.** Do not put a heredoc body inside
   `bash -c`, and never inline `ssh farnet0 ssh dpu ...`.
5. **Never run a write-capable subagent concurrently with another writer in the same tree.** Use an isolated worktree
   or exclusive ownership.
6. **A diagnostic `make -B` build is sticky.** Exit with another forced performance build and prove diagnostic strings
   are absent.
7. **Stamp every recorded performance result with both commit SHA and workload shape.** Client count, transaction
   count, logging state, and warmup state are part of the result.
8. **Use a commit-message file.** Backticks inside `git commit -m "..."` execute shell substitution.
9. **Each host frontend talks to its own local DPU.** Only the DPU-to-DPU leg uses the remote DPU address.
10. **Any bridge/ABI change requires rebuilding and deploying both DPUs.** Source the current protocol version from
    the header; never trust a copied number in prose.
11. **Do not broadly rsync source trees with `--delete`, and do not accept unreadable checksum omissions as parity.**
    Peer deployment is installed-prefix receipt/checksum proof; DPU deployment is a run-scoped source snapshot.
12. **A failed, timed-out, interrupted, or manually cleaned candidate is diagnostic-only.** Restart from a clean
    baseline; after a failed gate, restart both DPU services before any retry.

The incident evidence behind these rules is in `farnet_operational_hazards.md`. Do not simplify a rule without first
reading its incident.

## Build and artifact invariants

- When source changes are in scope, assume both trees must be rebuilt and reinstalled.
- Install Citus first so the new `libhomer_client.a`, headers, and `citus.so` exist; then relink PostgreSQL against
  that archive; only then install PostgreSQL and start it. A new `citus.so` with an old `postgres` fails symbol
  resolution in every backend.
- Build every validation target, not only `service-bin`. A target that was not compiled cannot be covered by a green
  workload.
- Performance candidates use forced non-diagnostic objects and prove stats/trace instrumentation absent.
- Artifact parity means a complete source→build→install→peer→both-DPU receipt chain. Equal stale binaries are not
  provenance.
- Source synchronization and builds must fail loudly on nonzero status. Pipeline output and an empty rsync log are not
  evidence.

## Validation and workload policy

| workload | status | acceptance meaning |
|---|---|---|
| `pgbench --homer --homer-dpu-command` | **THE GATE** | Full farnet0 host→local DPU→peer DPU→farnet1 DPU-spawned backend command/result path |
| Four-role selected-DPU basebackup | **MANDATORY FOR TRANSPORT ACCEPTANCE** | Bulk byte-ring relay and wrap behavior; not covered by the gate |
| DPU TCP transport smoke | **CONDITIONAL MANDATORY** | Single-node host↔DPU DMA regression net before/after DPU DMA, byte-ring, or bridge-ABI changes |
| Local `pgbench --homer` | Diagnostic-only | Local host-service control path; no cross-node/DPU acceptance |
| Remote host-service `pgbench --homer` | Broken; scheduled for removal | Never an acceptance gate |
| Backend-to-backend COPY | Broken; scheduled for removal | Never an acceptance gate; do not revive during validation |
| `pgbench --homer-dpu` without DPU command mode | Never completed end to end | Not the selected-DPU command gate |

If only one observation is possible, lead with the gate; a full transport verdict still requires basebackup. The gate
moves small results and does not validate byte-ring wrap. Host-service paths do not substitute for either.

For any transport change:

1. build the complete target set;
2. run the selected-DPU gate;
3. run four-role basebackup;
4. add the DPU TCP smoke for DPU DMA, byte-ring, or bridge/ABI changes;
5. include at least one workload with the frontend agent enabled and prove the DPU setup listener remains live
   afterwards.

The gate uses no host `citus_tuple_sink_service`. Keep client CPUs, DPU service CPUs, and socketless-backend CPUs
disjoint and sized to the client count. The current byte-ring pool caps the gate at eight clients; prefer four for
headroom. Scan both DPU logs, because the failure can be observed on one node and caused on the other.

## Measurement and verdict rules

- The first run after restart, sync, geometry change, or peer setup is warmup and is never quoted.
- Quote the full warmed repeat set, not the best run; prefer candidates lasting at least about five seconds.
- Keep verbose logging and stats macros off measured paths. Debug proof runs and stripped performance runs are not
  comparable.
- Compare only against a diagnostics row with the same SHA-relevant lineage, client count, transaction count, and
  logging state. Baseline values live only in `farnet_diagnostics_and_baselines.md`.
- Exit zero is not intended-path proof. Use candidate-bracketed role logs, structured events/checkers, correctness
  anchors, and process/artifact identity.
- A clean verdict answers both: **did the intended Homer path run?** and **was the candidate uncontaminated from
  preflight through teardown?**

## Stable validation events

New or materially changed validation-bearing lifecycle, terminal, teardown, and invariant-failure sites use the
shared `HOMER_EVENT(...)` API in
`/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_event.h`:

```text
HOMER_EVENT v=1 severity=<info|warn|alarm> component=<token> event=<stable_token> run=<token|-> key=value ...
```

Events must be allocation-free, bounded, present in performance builds, and absent from hot/retry loops. Retry
observations use `HomerEventShouldEmit()` with a caller-owned counter and terminal observed/emitted/suppressed
summary. Bare `session` requires `session_kind`; bare `generation` means only exact peer-QP generation. Keep the
human line during additive migration and document each stable proof event in the owning implementation
`CONTRACTS.md`. `HOMER_SERVICE_LOG` and disabled `HOMER_TRACE_EVENT` are not acceptance evidence.

## Deterministic runner authority

`tools/farnet_validation/` is **not verdict-authoritative while**
[`farnet_validation_runner_plan.md`](docs/kb/operations/farnet_validation_runner_plan.md) says live execution is
fail-closed. Do not bypass `LIVE_EXECUTION_READY = False`; live `run --execute`, `resume --execute`, and takeover
cleanup execution remain prohibited.

Current allowed/required uses:

- `validate.py plan` and non-executing `run` for exact expansion and journaling;
- process-local tests and golden fixtures;
- every applicable checked-in checker over explicitly bracketed current-candidate role intervals, preserving path,
  device, inode, offsets, raw evidence, and result;
- `summarize` only when a real complete receipt and journal-bound candidate logs exist.

A checker proves only its named predicate over supplied input. The agent still proves candidate provenance, interval
identity, ordering, teardown, and cleanup through the operator runbook.

## Unexpected-state ownership

Automation never owns an unexpected state. A live process after deadline, identity change, contradictory/malformed
evidence, or missing terminal predicate stops later phases before cleanup and transfers control to the validation
agent for bounded observation.

While runner takeover is disabled, retain a mode-0700 `/tmp` manifest with `cleanup_required=1`, exact PID/start/exe
and listener identities, candidate log intervals, and a hold deadline no more than 300 seconds away. Capture only
bounded read-only evidence, then perform identity-checked cleanup and record `cleanup_completed=1`. Leave live state
only when the brief explicitly names an immediate cleanup owner and deadline. Otherwise return `INCONCLUSIVE` with
every remaining identity.

## Scheduler enum and collector changes

When adding a `HomerProgressSourceKind`, `HomerProgressCollectorKind`, or `HomerProgressActionKind`, enumerate every
switch manually; `default:` arms defeat `-Wswitch`. A new collector requires the enum, collector→source map,
collector→action map, **and** explicit placement in `HomerMachineBaselineCompileExecutionPlan`'s phase body. An armed
collector omitted there is granted on no pass and looks like healthy idle state.

## Knowledge routing

| question | canonical document |
|---|---|
| Exact machine/build/deploy/workload commands | `docs/kb/operations/farnet_operator_runbook.md` |
| Why an operational rule exists | `docs/kb/operations/farnet_operational_hazards.md` |
| Diagnostic builds, network battery, historical measurements, broken shapes | `docs/kb/operations/farnet_diagnostics_and_baselines.md` |
| Runner readiness and authority | `docs/kb/operations/farnet_validation_runner_plan.md` |
| Current session/ring/sink identity, ownership, ordering, and capacities | `docs/kb/implementations/citus/transport/CONTRACTS.md` |
| Selected-DPU architecture | `docs/kb/implementations/citus/transport/selected_dpu_session_rings_and_lifecycle.md` |
| Broken host-service paths and removal boundary | `docs/kb/implementations/citus/transport/copy_path_revival_contract.md` |
| Resource retirement | `docs/kb/implementations/citus/transport/resource_retirement_contract_audit.md` |
