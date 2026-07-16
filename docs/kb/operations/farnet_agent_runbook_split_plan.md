# Farnet agent-guide and validation-runbook split

<!-- kb-summary: Plan and migration record for keeping repository AGENTS.md policy-focused while preserving exact farnet machine, build, deployment, workload, and evidence procedures in a canonical operations runbook. -->

## Status

**COMPLETE (2026-07-16).** Repository `AGENTS.md` is now the concise policy/router, while
`farnet_operator_runbook.md` owns mutable facts and exact operator procedure. Personal validation-agent routing,
static verification, and independent review are complete.

## Pre-split baseline grounding

- `AGENTS.md:1-14` declares itself the machine setup and runbook.
- `AGENTS.md:75-484` carries topology, build/install, deployment, cleanup, process preflight, service start, and schema
  preparation details.
- `AGENTS.md:485-1108` carries workload selection, exact workload recipes, evidence interpretation, regression sweep,
  post-run commands, and KB routing.
- `docs/kb/operations/farnet_operational_hazards.md` is already the canonical incident/rationale archive.
- `docs/kb/operations/farnet_diagnostics_and_baselines.md` is already the canonical diagnostic and historical-results
  archive.
- `docs/kb/operations/farnet_validation_runner_plan.md` is already the source of truth for deterministic-runner
  readiness and its live-execution authority boundary.

The inventory found four claims that must be corrected rather than copied:

- `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` is `5` in
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:47`, not the guide's stale `3`.
- A read-only live check on 2026-07-16 found farnet1's `/data/dbcomm/pg-citus/data/postgresql.conf` explicitly
  sets `port = 5433`; farnet0 currently has the installed prefix but no `/data/dbcomm/pg-citus/data` directory. The
  hazards note's claim that the permanent farnet1 setting was unapplied is stale.
- The broad source-tree rsync/md5 recipe is invalidated by the mixed-ownership and unreadable-file incident in
  `farnet_operational_hazards.md` §10 and by the runner plan's prohibition on broad source-tree `--delete`.
- The guide's embedded gate TPS band is older than the current diagnostics row. Baselines belong only in
  `farnet_diagnostics_and_baselines.md`.
- Farnet0's `/data/dbcomm` is a logical compatibility symlink to `/home/dbcomm/data-dbcomm`; farnet1's is a real
  directory on `/data`. Moving farnet1's 23 GiB prefix into `/home` is rejected: `/home` shares a root filesystem
  with only about 22 GiB free, while `/data` has about 467 GiB free. The logical `/data/dbcomm/...` interface remains
  common across hosts.
- The initial runner helpers resolved `/proc/PID/exe` but compared it with the uncanonicalized logical prefix, so
  they could miss farnet0 processes. Stage 1 now canonicalizes and pins the configured prefix through final cleanup
  verification, rejects sibling-tree identities, and permits missing PostgreSQL data only through the explicit peer
  invocation capability. Live runner execution remains disabled for the broader reasons in the runner plan.

## Decisions and rationale

1. Create `docs/kb/operations/farnet_operator_runbook.md` as the canonical home for mutable rig facts and exact
   operator commands. Use the current project guide as the migration inventory, but do not blindly preserve claims
   disproved by the factual audit or copy incident narratives and measurement history owned elsewhere.
2. Rewrite repository `AGENTS.md` as a policy and routing document. Retain concise project/repository identity,
   mandatory safety rules, build/validation ordering invariants, workload-selection rules, stable-event requirements,
   unexpected-state ownership, and explicit documents that must be read for each task class.
3. Do not duplicate detailed commands in `AGENTS.md`. A concise rule may name the invariant and link to the exact
   runbook section; the runbook owns command syntax, topology tables, ports, CPU sets, and log anchors.
4. Keep incident narratives and diagnostic batteries in their existing canonical documents. The new runbook links to
   them rather than absorbing their history.
5. Update the operations index and both Claude/Codex validation-agent instructions so delegated validators read the
   new runbook, not an assumed command-heavy `AGENTS.md`.
6. Add an explicit trigger matrix to `AGENTS.md`: before any farnet build/install/sync/DPU-deploy/service/runtime or
   workload command, read the applicable canonical runbook sections and the marked hazard rules; before live farnet
   validation, read the runbook in full. This applies to main agents and implementation workers, not only the
   dedicated validation role.
7. Remove broad host source-tree synchronization from the canonical procedure. Peer-host deployment synchronizes
   the installed runtime prefix and proves checksum parity. DPU source deployment uses the runner's immutable,
   run-scoped tracked-source snapshot model, transfers to a fresh run-ID tree, reconfigures locally, and force-builds;
   it does not mutate a shared source checkout with broad `--delete`.
8. Treat logical and canonical paths as distinct identities. Commands and cross-host configuration use the stable
   logical `/data/dbcomm/...` path; process probes canonicalize both the configured prefix and `/proc/PID/exe` before
   comparison and report both forms. A missing data directory is accepted only for a role whose invocation explicitly
   permits it.

## Alternatives considered

- **Leave `AGENTS.md` as the runbook:** rejected because every agent pays the context and scanning cost even when it
  is not building, deploying, or validating.
- **Split immediately into many topology/build/workload leaf documents:** rejected for this stage because it would
  multiply routing decisions and increase the chance of losing a load-bearing cross-step ordering rule. One canonical
  runbook plus the existing hazards/diagnostics/runner documents is the lower-risk boundary.
- **Blindly copy everything and trim later:** rejected. Use `farnet_agent_runbook_split_inventory.md` to identify
  the canonical owner before relocation, then move only current operator procedure and replace owned history,
  diagnostics, and baselines with links.

## Preservation inventory

`docs/kb/operations/farnet_agent_runbook_split_inventory.md` is the durable old-section-to-destination map and
normalized fenced-block baseline for `AGENTS.md` at `bc7eb141964`. Its post-migration proof must remain current
through final review before the refactor can be committed.

## Stages

1. **Complete.** Reconcile current facts and runner path handling before migration: source bridge version from the header; record
   the verified farnet1 port state and absent farnet0 data directory; replace broad source-tree sync with snapshot/
   prefix procedures; remove embedded baselines in favor of the diagnostics link; canonicalize process-prefix
   comparisons and explicitly allow missing PostgreSQL data only on the peer role.
2. **Complete.** Create `farnet_operator_runbook.md` from the still-current topology, build/deploy, cleanup, start, workload,
   proof, and teardown procedures. Route incident narratives, diagnostic batteries, stale/broken command shapes, and
   measurement tables to their existing canonical leaves rather than creating a second monolith.
3. **Complete.** Replace `AGENTS.md` with the concise policy/router and trigger matrix; verify that every former heading has a
   canonical destination and every auto-loaded safety invariant retains a concise imperative summary.
4. **Complete.** Update `docs/kb/operations/README.md`, hazards/diagnostics backlinks and metadata, runner-plan
   grounding, the farnet-validation skill, and Claude/Codex validation-agent guidance.
5. **Complete.** Run KB/index checks, Markdown/link/search checks, validation-agent structural checks, and the
   deterministic-runner unit suite. Obtain an independent diff review before committing this stage.

## Acceptance criteria

- `AGENTS.md` contains rules and decision procedures, not full build/start/workload shell recipes or mutable topology
  tables.
- Every former `AGENTS.md` runbook heading and exact recipe remains discoverable in the new canonical runbook or an
  existing explicitly linked operations document, with a recorded old-section to new-section mapping. A normalized
  shell-block inventory confirms that moved commands retain their adjacent warnings, prerequisites, topology, and
  ordering rather than merely remaining searchable.
- Every agent is explicitly required to read the applicable runbook sections before farnet build/install/sync/
  deployment/runtime commands; validation agents must read `AGENTS.md`, `farnet_operator_runbook.md` in full, the
  operational hazards, and the runner plan before live farnet validation.
- The runbook does not carry incident histories, diagnostic batteries, broken recipes, or performance baseline
  tables already owned by the hazards/diagnostics leaves.
- The new runbook has a document-owned `kb-summary`; the operations README index is current.
- Process probes match executables through both hosts' logical/physical prefix layouts, and remote stop treats a
  missing farnet0 data directory as clean only when the caller explicitly selects that role condition.
- Repository-plus-personal-agent searches contain no stale statement that `AGENTS.md` itself holds exact commands.
  The scoped operations-tree KB check introduces no warning beyond a recorded pre-existing repository baseline.
- No source build, install, service start, workload, or remote runtime validation is performed for this documentation
  refactor.

## Current next step

Commit the reviewed stage. Live runner orchestration remains fail-closed; its next implementation work stays in
`farnet_validation_runner_plan.md`.

## Completion evidence

- Repository `AGENTS.md` is 185 lines; the canonical operator runbook is workflow-focused and contains 28
  syntax-valid shell blocks.
- The scoped operations KB sync/check passes with no warning; targeted relative Markdown-link resolution and
  `git diff --check` pass.
- All checked-in runner helpers pass `bash -n`; Python compilation and all 52 process-local runner tests pass.
- The installed farnet-validation skill passes its structural validator; Claude agent frontmatter and Codex role
  structure pass readback checks; every routing surface names `farnet_operator_runbook.md`.
- Independent adversarial review verified install/relink order, second-hop DPU preparation, both-DPU preflight,
  profile-specific role selection, canonical-prefix fail-closed behavior, preservation inventory, and routing. No
  further correction/pass is required.
- No source build, install, service start, workload, or live acceptance run was performed for this stage.
