# Deterministic farnet validation runner

<!-- kb-summary: Implementation plan and current status for converting the manual multi-host Homer acceptance procedure into explicit profiles, resumable evidence, and machine-readable verdicts. -->

## Status

**IMPLEMENTATION IN PROGRESS, LIVE EXECUTION FAIL-CLOSED (2026-07-16).** The profile/phase/evidence/parser skeleton
and the forward structured-event API now exist, but `validate.py run --execute` deliberately rejects the invocation.
The current [`farnet_operator_runbook.md`](farnet_operator_runbook.md) remains authoritative until receipt minting,
remote takeover/identity proof, lifecycle cleanup,
negative tests, another adversarial pass, and one known-green live acceptance are complete.

“The runbook remains authoritative” does not mean the runner scripts should sit unused. Every applicable checked-in
parser/checker must run over manually bracketed current-candidate logs; its result determines the named predicate
over that supplied interval. The agent still proves that the interval belongs to the candidate and combines the
result with the remaining manual evidence. What remains disabled is
**verdict-authoritative live orchestration**: the runner cannot yet establish the complete provenance, runtime
identity, candidate-journal, teardown, and cleanup chain needed for its own final `PASS` to replace agent-supervised
manual acceptance.

## Why this exists

The clean Stage-2b acceptance ran from 14:50:25 through 15:10:43 and produced 75 evidence files under
`/tmp/stage2b_validation_rerun_20260716_145025`. The actual basebackup took 7.06 seconds and each warmed gate run
only several seconds; much of the wall time was command translation, SSH/process preflight round trips, repeated log
collection, cleanup, and report assembly. The earlier failed attempt also spent substantial time discovering three
generated-artifact ownership frontiers serially before reaching compilation.

The goal is not to weaken acceptance. It is to make the agent supervise a deterministic state machine instead of
reconstructing the state machine from Markdown for every run.

The runner owns the **known path**, not diagnosis. Expected success and expected-failure predicates should become
concise mechanical results. An unexpected hang, contradictory evidence, missing anchor, or unknown output shape
must stop automation, preserve useful state, and transfer control to the validation agent rather than flattening
the symptom into a generic timeout or blindly cleaning away the evidence.

## Grounding and non-negotiable contracts

- [`Build, install, and artifact proof`](farnet_operator_runbook.md#build-install-and-artifact-proof) names the full
  target set and preserves the Citus-install→PostgreSQL-relink/install order.
- [`Peer-host artifact deployment`](farnet_operator_runbook.md#peer-host-artifact-deployment) rejects broad source
  sync, uses run-scoped DPU snapshots, and requires receipt/checksum proof.
- [`Clean baseline`](farnet_operator_runbook.md#clean-baseline) uses canonical `/proc/PID/exe` identity, includes both
  DPUs, and removes only project-owned shared memory.
- [`Start roles and prepare workload`](farnet_operator_runbook.md#start-roles-and-prepare-workload) proves port 5433,
  the exact data directory, service identity, listener ownership, and clean candidate state.
- [`Selected-DPU command gate`](farnet_operator_runbook.md#selected-dpu-command-gate) requires both host services
  absent, correct local/remote DPU identities, four path proofs, both-DPU evidence, and a both-service restart after
  failure.
- [`Four-role basebackup`](farnet_operator_runbook.md#four-role-basebackup) starts the consumer first, uses matching
  4 x 524,288-byte geometry, and requires both role statuses plus wrap evidence.
- [`Regression sweep`](farnet_operator_runbook.md#regression-sweep) requires gate plus basebackup and adds the DPU TCP
  smoke for DPU DMA, byte-ring, or bridge changes.
- `docs/kb/operations/farnet_operational_hazards.md`: a scripted check must fail loudly when it cannot establish
  identity; unreadable `/proc`, stale DPU services, rsync 23, and pipeline-masked build failure cannot become PASS.

## Alternatives considered

### One large Bash script — rejected

It would reduce terminal round trips, but quoting, double-hop SSH, timeout handling, structured phase state, and
machine-readable evidence are too fragile. Narrow remote helpers may be shell; orchestration is Python.

### Let each validation agent keep translating the runbook — rejected

This preserves flexibility but recreates ordering and quoting risk on every run, produces inconsistent evidence
layouts, and makes failure/resume behavior depend on the individual agent.

### Infer the smallest tier from `git diff` — rejected as an authority decision

Changed-file classification may suggest a profile and explain why, but it must never silently downgrade the profile
selected by the caller or KB plan. Uncertain classification chooses the broader tier.

### Faithful runner first, then measured trimming — selected

Automate the present checks first. Record per-phase duration over known-green runs. Trim only checks shown to be
duplicative while retaining every silent-wrong-result guard.

## Target design

### Project paths

- `tools/farnet_validation/validate.py`: entry point and ordered phase engine.
- `tools/farnet_validation/profiles.py`: explicit profile-to-phase and requirement mapping.
- `tools/farnet_validation/remote_helpers.py`: uploads/invokes bounded checked-in host/DPU helpers by path; it never
  assembles helper bodies or fragile SSH one-liners dynamically.
- `tools/farnet_validation/evidence.py`: atomic manifest/events/summary writing and log-offset scans.
- `tools/farnet_validation/checks.py`: output parsing, correctness anchors, alarm/frontier/teardown extraction.
- `tools/farnet_validation/takeover.py`: bounded unexpected-state capture and the agent takeover control protocol.
- `tools/farnet_validation/remote/`: checked-in `/proc` probe/cleanup, DPU service supervisor, DPU build/deploy, and
  workload-launch helper scripts with argv/env inputs.
- `tools/farnet_validation/tests/`: process-local unit tests and golden-log parser fixtures.

The runner writes only under an explicit evidence directory (default `/tmp/farnet-validation-<run-id>`) plus
documented runtime/build/install destinations selected by the profile.

The configured host prefix is a **logical cross-host path**, currently `/data/dbcomm/pg-citus`. Farnet0 resolves
`/data/dbcomm` through `/home/dbcomm/data-dbcomm`, while farnet1 stores it on the `/data` filesystem. Runtime process
identity therefore canonicalizes both the configured prefix and `/proc/PID/exe` before comparison while retaining
both forms in evidence. The runner does not require equal physical paths. Missing PostgreSQL data is a role-specific
condition: allowed for the current farnet0 peer role only when the helper invocation says so; never silently accepted
for farnet1's intended database role.

### Profiles

| profile | ordered scope |
|---|---|
| `build` | orient, ownership preflight, formatting/static inventory, all-target Citus build |
| `gate-smoke` | build/deploy as requested, clean/start/preflight, debug 5/5 intended-path gate, alarm scan, cleanup |
| `gate-acceptance` | gate-smoke plus stripped warmup and complete warmed repeat set |
| `transport-acceptance` | gate-acceptance plus four-role basebackup, wrap proof, final frontier/shard/reset/DMA ledgers |
| `dma-bridge` | requires a separately fingerprinted pre-change smoke receipt, deploys the candidate, runs the candidate DPU TCP smoke, compares the pair, then runs `transport-acceptance` |

The workload profile and validation subject are separate, explicit choices:

- `--artifact-mode current-source` (default) validates the current workspace closure and requires either
  `--prepare-through dpus` or a complete matching source->build->install->peer/DPU receipt chain;
- `--artifact-mode existing --artifact-receipt PATH` validates only the exact installed/binary hashes named by the
  receipt and makes no claim about the current source tree.

`--prepare-through {build,install,peer,dpus}` is monotone: selecting a later edge executes every earlier edge in the
load-bearing order. Changed-file classification may suggest a profile/preparation edge but never selects or
downgrades one silently.

For current-source mode, source identity is `{postgres HEAD,index,unstaged,relevant explicit untracked;
citus HEAD,index,unstaged,relevant explicit untracked}`. Any untracked file under runtime source/config roots must be
explicitly included or the runner refuses the claim. Receipts form this explicit compatibility chain:

`source identity -> Citus configure/recipe -> Citus outputs -> installed libhomer_client.a + headers + citus.so ->
PostgreSQL Meson/recipe -> relinked postgres + pgbench + pg_basebackup -> installed PostgreSQL consumers ->
peer prefix -> farnet1-DPU configure/toolchain + binary -> farnet0-DPU configure/toolchain + binary`.

Every receipt edge records input/output SHA-256 values and exact command evidence. A mismatch invalidates from the
earliest broken edge: build-output mismatch reruns build, local-prefix mismatch reruns install, peer mismatch reruns
sync, and DPU mismatch reruns deploy. Parity between two equally stale artifacts is never provenance.

Current-source builds never consume mutable live worktrees. The runner fingerprints each live tree before and after
capturing a content-addressed archive of every tracked file plus explicitly authorized untracked runtime input; any
change during capture discards the archive. It expands private mode-0700 run-owned Citus and PostgreSQL build trees
from those immutable archive receipts. Configure/build may create generated files only inside those private trees;
external writers cannot alter the build inputs. Both local and DPU builds derive from the same Citus source archive.

The build receipt also keys exact argv, allowlisted environment, compiler/make/ninja/meson versions, Citus
`config.status`/generated-config hashes, PostgreSQL Meson setup options plus `compile_commands.json`, and negative
instrumentation scan. Citus acceptance uses a forced rebuild with declared performance flags. PostgreSQL uses a
fresh Meson build directory recreated from the recorded active configuration rather than reusing live-tree objects.
This costs more than an incremental relink but makes the first current-source receipt real; later repeated runtime
profiles use `existing-artifacts` only when that exact receipt remains valid.
Source manifests hash content, mode, and symlink target. A successful no-op cannot mint a new current-source receipt
unless every reused output already carries a matching recipe/config/input receipt; otherwise that edge is forced.
Each intermediate receipt has its own invalidation rule, so installing new Citus outputs necessarily invalidates the
PostgreSQL relink and installed-consumer receipts.

### Phase and resume model

`orient -> ownership -> source-snapshot -> citus-config/build -> citus-install -> postgres-config/relink ->
postgres-install -> peer-sync -> dpu-deploy -> clean -> start ->
post-start-preflight -> prepare -> debug-gate -> pre-warmup-preflight -> warmup ->
(pre-repeat-preflight -> repeat) x N -> pre-basebackup-preflight -> basebackup -> live-final-scan ->
postgres-orderly-stop -> host-survivor-cleanup -> DPU-orderly-stop/wait -> collect-teardown-logs ->
teardown-ledger-scan -> shared-memory-cleanup ->
final-process/listener-preflight -> report`

Each phase appends a structured event and records start/end time, command, exit status, output path, and input/output
fingerprints. `run.json` contains repo HEADs, dirty-diff hashes, profile/options, artifact/source hashes, role PIDs and
start times, and phase status. Writes are atomic. Resume is allowed only when the manifest fingerprint still matches:

- source/diff change invalidates build and all later receipt edges;
- build-output mismatch invalidates build; installed-artifact mismatch invalidates install; peer/DPU mismatch
  invalidates only the corresponding receipt edge and later edges;
- any runtime failure/interrupt invalidates clean/start and later phases, and a failed gate additionally requires
  both DPU services to restart;
- measured runs are never individually resumed or cherry-picked. Any contaminated repeat invalidates the entire
  warmed set and returns through clean/start/debug/warmup;
- cross-process resume stops at the artifact receipt boundary. Once `clean` starts, a new invocation always repeats
  clean/start/preflight; runtime PIDs/listeners/log offsets/shared memory are never trusted across invocations.

### Mechanical speedups that preserve proof

- Check all known generated build roots for `dbcomm` writability before starting the build; report the complete
  repair manifest rather than discovering paths one at a time. Repair remains explicit and bounded.
- Keep one SSH control connection per host. Parallelize independent read-only host/DPU preflights and scans only at
  quiescent boundaries; never overlap them with role start/stop, warmup, measured work, or teardown.
- Build a run-scoped Citus source snapshot from every tracked file plus explicitly authorized untracked runtime
  inputs. Archive/hash it locally, transfer it to each DPU, and unpack into a new empty run-ID tree. Re-run DPU-local
  configure, fingerprint generated configuration, and force every profile-selected DPU target: service-bin for all
  DPU profiles, plus dpu-tcp-transport-smoke-bin for `dma-bridge`. This is the authoritative
  repository-source closure; a hand-maintained changed-file manifest is not a dependency closure.
- Synchronize the installed prefix without relying on an empty rsync log as evidence: record exact argv/rc, hash the
  required runtime closure, and prove the peer receipt. Do not use broad source-tree `--delete`.
- Record service-log byte offsets and scan only bytes produced by the current run, while retaining final raw logs.
- Bracket every debug/warmup/repeat/basebackup/teardown candidate with per-role `{device,inode,start_offset}` and
  `{device,inode,end_offset}` receipts. Required anchors may be satisfied only by that candidate's client output and
  role-log intervals; an earlier debug gate can never satisfy a later repeat/basebackup. Log truncation, rotation,
  overlap, or an anchor from the wrong role/candidate is `INCONCLUSIVE`. Stable structured session/connection/
  generation fields provide correlation inside each interval; aggregate alarm scans cover the entire run interval.
- Consolidate correctness parsing and wrap/frontier/ledger arithmetic into deterministic checks that emit JSON.
- Emit a short phase-status line at command boundaries so the supervising agent does not poll blindly.

### Unexpected-state and validation-agent takeover

Known predicates produce ordinary phase results. The following enter `AWAITING_AGENT` instead of being guessed:

- command deadline reached while the process/remote role is still live;
- command exited but required anchors are absent, duplicated, internally contradictory, or unparsable;
- process/listener/log identity changes during a phase;
- an unrecognized event severity/schema version or parser exception;
- progress stops without a profile-defined terminal predicate.

At that boundary the runner launches no later phase. It does not claim that stopping userspace freezes RNIC/DOCA
hardware progress. Instead it records the soft-deadline frontier immediately while the suspect command and roles
remain live, then captures a bounded bundle before any semantic close, signal, restart, or shared-memory cleanup:

- run/profile/source/artifact receipt and exact active command argv/allowlisted env/cwd/start/deadline;
- local SSH/workload PID tree with `/proc/exe`, kernel start time, status, `wchan`, bounded stack when readable, and
  a separate count of kernel-thread/no-exe versus genuinely unreadable user processes;
- role supervisor state, listener ownership/Recv-Q, PostgreSQL data-dir/status, host-service absence, and relevant
  shared-memory names;
- log path device/inode/size/offset plus the last bounded bytes and parsed `HOMER_EVENT` records;
- current frontier/reset/shard/control/head-mirror/DMA counters when already emitted.

No automatic `gdb`, `perf`, core dump, source diagnosis, or unbounded log collection occurs. With
`--unexpected=hold` (the validation-skill default), the runner keeps the suspect command/roles available for a
bounded takeover window and writes `takeover.json`. The runner owns a run-ID lease containing its PID/kernel start
time, random control token, creation/expiry timestamps, suspect PIDs/start times, and `cleanup_required=1`; the
evidence directory is mode 0700 and takeover controls must match the token. A supervising validation agent may use
`validate.py takeover RUN_DIR --capture` for another allowlisted read-only snapshot or `--cleanup` to terminate the
attempt safely. The runner never continues that attempt into acceptance: after a hang/timeout it is diagnostic-only,
and any retry starts from clean/start. If the window expires or `--unexpected=cleanup` was selected, the runner
captures once, cleans up, and returns `INCONCLUSIVE` with `cleanup_required`/`cleanup_completed` explicit.

If the runner dies, a later invocation treats the lease as abandoned takeover state, re-verifies recorded process
identities without trusting them, captures once if possible, and requires explicit cleanup. It never resumes runtime.

The validation agent may interpret evidence, select additional bounded read-only observations, and identify the
first failing/unknown predicate. It still does not patch source or configuration; source-level diagnosis and probe
design remain with the main agent.

### Forward logging/event contract

Validation must not depend indefinitely on English prose. Establish an additive, performance-safe event line:

```text
HOMER_EVENT v=1 severity=<info|warn|alarm> component=<token> event=<stable_token> run=<token|-> key=value ...
```

- The required prefix is `v severity component event run`; fields after it are order-independent. `run` is the
  sanitized `HOMER_VALIDATION_RUN_ID`, or `-` outside a runner. Role/candidate intervals supply external
  correlation; events carry typed object identity where available.
- `component`, `event`, keys, and values are whitespace-free tokens; arbitrary prose remains in a separate legacy
  human line or an escaped `detail` field, never in parser keys.
- Bare `session` is forbidden unless accompanied by `session_kind`; service session, parent session, service stream,
  bridge generation, and other lifetimes stay distinct. Bare `generation` means only exact peer-QP connection
  generation; other generations use typed keys. Connection identity is `in:<index>` or `out:<index>`.
- Required parser anchors use stable event names and typed fields. Field order is irrelevant; unknown fields are
  preserved and ignored unless a profile declares them required. Unknown schema versions are `INCONCLUSIVE`.
- Always-on events are restricted to cold lifecycle, terminal, teardown, and error paths. They use no allocation and
  are never compiled out of performance builds. Hot/retry-loop events use a first-N-then-every-N bounded counter;
  unbounded per-iteration logging is forbidden.
- `HOMER_SERVICE_LOG` remains optional debug prose and must not be an acceptance anchor.
- The runner attaches process logs to a run receipt; a run ID may be emitted at service startup but need not be
  repeated in every line.

The initial adoption is deliberately narrow: add a common Homer event macro/helper and dual-emit structured records
beside the existing human-readable frontend attachment/backend-spawn, QP frontier, Stage-2b teardown, control/
head-mirror, and DMA teardown lines used by acceptance. Keep legacy parsing as a compatibility adapter during
migration. New Homer code that introduces a validation-bearing lifecycle transition, invariant failure, or teardown
counter must use the event convention and document the stable event name in the applicable contract/KB entry. No
exhaustive retrofit is part of this milestone.

The serializer is header-only and bounded: fixed stack buffer, token-safe run ID, one write for stderr sinks, and
`truncated=1`/parser-UNKNOWN on overflow. PostgreSQL code formats the same marker then routes it through
`ereport`/`pg_log_*`; parsers search for the embedded marker rather than column zero. `HOMER_EVENT` is always-on for
cold proof paths; disabled `HOMER_TRACE_EVENT` does not evaluate arguments. Retry warnings use a caller-owned counter
keyed by full event/object identity, emit first 8 then powers of two, and emit a terminal
`observed/emitted/suppressed` summary.

## Verdict and failure policy

- Exit 0 / `PASS`: every selected profile requirement passed on clean state and the artifact receipt matches the
  declared validation subject.
- Exit 1 / `FAIL`: a correctness/intended-path predicate directly disproved the claim, a deterministic source build
  failed, or an ordinary workload failure reproduced after one documented clean restart/retry.
- Exit 2 / `INCONCLUSIVE`: identity, environment cleanliness, evidence, or bounded repair could not be established.
- Exit 3: invalid invocation/profile/manifest; no runtime verdict.

The runner never diagnoses source failures and never edits source/configuration. It may generate temporary helpers
and report a bounded documented repair, but automatic repair is off unless an explicit repair flag authorizes the
specific generated roots. Failures are typed, not inferred from exit code alone. Permission/SSH/timeout/missing-tool,
unreadable identity/checksum, stale listener ownership, missing evidence, unrecognized parser state, and exceptions
fail closed to `INCONCLUSIVE`. Timed-out/interrupted/manually killed attempts are diagnostic-only. An ordinary
non-timeout workload failure may receive one clean retry; only the same clean failure becomes `FAIL`.

DPU services run under a checked-in supervisor that survives SSH, records child PID plus kernel start time and
binary digest, and writes the actual `wait()` status atomically. PID disappearance is not accepted as clean exit.
Cleanup fails if listener ownership remains or cannot be established. Host probes distinguish kernel-thread/no-exe
entries from genuinely unreadable user processes; unreadable user identity makes the run `INCONCLUSIVE`.

Every checked-in runner/helper file is part of the runner receipt. Helpers upload atomically to fresh mode-0700
run-ID directories and remote SHA-256 must match before invocation. Generated executable helper bodies are
forbidden; generated argv/env/data files are permitted and recorded. For every started postmaster, DPU service,
`pgbench`, `pg_basebackup`, DPU TCP smoke server, and DPU TCP smoke client, bind PID + kernel start time + listener
inode where applicable + `/proc/PID/exe` digest to the expected installed/DPU receipt before accepting its output.
Both DPU build receipts include every selected output digest; a service-only receipt cannot satisfy `dma-bridge`.

The runner holds a whole-run exclusive install/deploy lock from the first install mutation through final cleanup;
all project runner invocations honor it, and receipt files are rechecked at every role start and final scan. During
the non-measured debug gate, a bounded identity monitor captures the DPU-spawned backend while live and proves the
mapped `citus.so` device/inode equals the installed receipt object, then verifies that object's digest. Missing or
unreadable maps, an inode/digest mismatch, or prefix drift is `INCONCLUSIVE`. This read-only monitor is allowed only
on the debug proof run, never a measured candidate. System dynamic libraries remain recorded machine inputs rather
than candidate artifacts.

DPU receipts separately record machine dependencies outside the repository snapshot: `/usr/bin/pg_config`
path/output/digest, compiler/version, normalized pkg-config results, relevant DOCA/RDMA/libpq header/library
identities, generated configuration hashes, final `ldd` resolution, and binary digest. Divergent farnet0/farnet1
DPU inputs remain visible rather than being normalized away.

`dma-bridge` requires `--baseline-run PATH`. Its baseline receipt must name distinct baseline source/artifact
identities (unless explicitly selected as a no-change runner sanity test), both endpoint binary digests,
topology/device, exact symmetric six-leg flags, server/client process identities and statuses, both raw logs, and
both `ok` anchors under the same smoke schema version. Validate the baseline completely before candidate deployment;
any mismatch is invalid invocation or `INCONCLUSIVE`, never a comparison PASS.

## Implementation stages

1. **Framework:** profiles, phase engine, atomic evidence manifest, command runner, timeouts, dry-run, resume
   invalidation, verdict codes.
2. **Safe operational helpers:** canonicalized logical-prefix versus `/proc/exe` identity/preflight, role-explicit
   absent-data handling, cleanup, DPU lifecycle, PostgreSQL identity/start, SSH multiplexing and script upload/invoke.
3. **Artifact phases:** complete source identity, run-scoped Citus source snapshot, ownership preflight,
   all-target build/install order, source->build->install->peer/DPU receipt chain, both-DPU configure/forced-build/
   landed proof.
4. **Workloads:** gate debug/warmup/repeats, four-role basebackup, optional DPU TCP smoke.
5. **Evidence and takeover:** actual argv/env/rc/timeout/status per command, incremental log scans, intended-path parser,
   TPS/failure parser, byte/wrap arithmetic,
   alarm/frontier/shard/reset/control/head-mirror/DMA summary, final Markdown/JSON verdict.
6. **Logging contract:** add the shared structured-event helper, dual-emit the narrow initial acceptance anchors,
   add positive/negative parser fixtures, and document the forward rule in `AGENTS.md` plus transport contracts.
7. **Integration:** update `AGENTS.md` and the `farnet-validation` skill to select/invoke the runner while retaining
   the manual commands as fallback and source-of-truth explanation.
8. **Validation:** unit/golden parser tests, `--dry-run` for every profile, adversarial review, then one explicit
   known-green live `transport-acceptance` run before calling the runner authoritative.

## Acceptance criteria

- Process-local tests cover profile expansion, provenance-chain enforcement, phase ordering, timeout/failure
  classification, atomic manifest recovery, artifact-only resume invalidation, log-offset scanning, gate parsing,
  basebackup parsing/wrap math, and final ledgers.
- Negative fixtures fail closed for unreadable `/proc`, stale/wrong listener owner, PID/start-time mismatch, wrong
  DPU peer, unexpected host service, rsync/checksum failure, pipeline-masked build failure, missing/duplicated path
  anchors, truncated/rotated logs, missing teardown ledger, mismatched basebackup geometry/roles, unexplained
  frontier delta, and nonzero/vacuous teardown accounting.
- Path-layout fixtures prove that a logical prefix through a parent symlink matches the canonical `/proc/PID/exe`
  path, a mismatched canonical prefix does not match, missing farnet0 data is accepted only under the explicit peer
  mode, and missing farnet1 data fails closed.
- Hang/takeover tests prove the runner captures before cleanup, never advances an unexpected attempt, accepts only
  allowlisted read-only capture or cleanup controls, times out the hold boundedly, and requires a clean restart for
  retry. Unknown event versions/required fields and contradictory structured-versus-legacy evidence are
  `INCONCLUSIVE` with preserved raw lines.
- Structured logging tests prove the macro is present in performance builds, cold-path records parse independent of
  field order, hot-loop output is bounded, and the selected initial sites retain their human-readable legacy lines.
- Normal teardown tests enforce the green Stage-2b role order: finish workload/live scan -> stop the intended
  PostgreSQL data directory -> reap host socketless backends/clients -> supervised stop/wait of both DPU services ->
  collect full DPU logs -> scan teardown ledgers -> final host/DPU process+listener preflight. Unknown-hang takeover
  branches before this sequence and can never be labelled clean final teardown.
- `--dry-run` emits every state-mutating command in the same order as the runbook and writes no runtime state.
- Shell helpers pass `bash -n`; Python passes compilation and the project-local test suite.
- An adversarial reviewer finds no path that can turn stale artifact provenance, unreadable identity, rsync/build
  pipeline failure, stale
  service, wrong port/topology, missing intended-path proof, failed workload, or missing ledger into PASS.
- A known-green live run matches or strengthens Stage-2b evidence and records phase timings. Only then may the skill
  prefer the runner over manual execution.

## Current next step

Canonical-prefix matching and role-explicit absent-data handling discovered by the farnet0 home-backed layout audit
are now fixed and covered by process-local alias, sibling-tree, identity-drift, and capability tests. Next complete
the provenance receipt writer and runtime identity/preflight/cleanup/takeover implementation while keeping
`LIVE_EXECUTION_READY = False`. Expand role-separated negative fixtures and process-local supervisor tests, then run
another independent diff review. Only after every live false-PASS/contamination path is closed should the main agent
ask for the one known-green `transport-acceptance` run and consider enabling `--execute`.

## Milestone history

### 2026-07-16 — fail-closed framework and logging-contract checkpoint

- Added explicit profiles, ordered phases, command/evidence journals, artifact-receipt validation, candidate log
  intervals, legacy plus v1 structured-event parsers, local soft-timeout takeover primitives, remote helper
  skeletons, and process-local tests under `tools/farnet_validation/`.
- Added `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_event.h` and dual emission at the initial cold
  frontend-attachment/DPU-spawn/frontier/control/head-mirror/Stage-2b/DMA proof sites. Existing human lines remain
  for compatibility.
- An independent post-implementation review found the runner could still overclaim stale artifacts, incomplete
  role logs, weak cleanup/preflight, remote timeout state, and nonzero service exit. Immediate false-PASS surfaces
  were tightened, unsafe remote-shell tokens are rejected, and live execution was hard-disabled rather than
  treating the partial engine as authoritative.
- Development evidence: 44 process-local parser/phase/receipt/takeover tests pass; every checked-in shell helper
  passes `bash -n`; Python compilation and both-repo `git diff --check` pass; standalone header compilation proves
  token sanitization and no disabled-trace argument evaluation; both the Citus service target and full Citus build
  compile/link the structured sites. These are not runtime acceptance. No install, SSH, service start, workload, or
  live validation was performed at this checkpoint.
- The shared repo `AGENTS.md` now makes the runner authority boundary explicit for every validation agent: use
  plan/dry-run surfaces for inspection and parser/checker functions as evidence compressors over manually bracketed
  candidate logs while this note says fail-closed; keep live orchestration on the manual runbook, and eventually split
  deterministic known-path automation from agent-owned unexpected-state takeover.
- A path-layout audit found farnet0's logical `/data/dbcomm` resolves into `/home/dbcomm/data-dbcomm`. The initial
  helpers compared canonical `/proc/PID/exe` values against the logical prefix and asked the peer to stop a data
  directory it intentionally lacks. The helpers now canonicalize and pin the prefix through final cleanup proof,
  reject sibling-tree identity, and require an explicit peer capability for absent data. The full runner suite has
  52 passing process-local tests. Live execution remains closed for the other incomplete receipt/lifecycle surfaces.
- The operator-runbook audit also exposed that fresh second-hop helper installation assumed the farnet0-DPU helper
  directory already existed. `dpu_dispatch.sh prepare` now creates it by file-based second hop, and planner tests
  require prepare to precede every helper install.
