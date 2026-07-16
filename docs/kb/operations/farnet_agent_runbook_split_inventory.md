# Farnet AGENTS-to-operator-runbook preservation inventory

<!-- kb-summary: Durable source-to-destination and shell-block inventory used to prove that shrinking AGENTS.md preserves every current farnet procedure, warning, prerequisite, and ordering dependency in the correct canonical document. -->

## Baseline

This inventory describes repository `AGENTS.md` at commit `bc7eb141964` before the operator-runbook split. Line
ranges are migration coordinates, not permanent links. The destination heading names are the stable post-migration
checks. A moved command is preserved only when its adjacent prerequisite, topology, warning, and order move with it.

The inventory intentionally distinguishes four dispositions:

- **AGENTS summary:** retain an imperative auto-loaded rule and link to its canonical detail;
- **operator runbook:** move current exact commands and workflow facts;
- **existing canonical leaf:** replace duplicate history/diagnostics/baselines/broken shapes with a link;
- **remove as refuted:** do not preserve a statement disproved by the 2026-07-16 factual audit.

## Section mapping

| old `AGENTS.md` section | destination | required retained meaning |
|---|---|---|
| `1-14` project guide | `AGENTS.md` router + operator runbook purpose | Name the operator runbook, hazards, diagnostics, runner plan, and implementation contracts. |
| `15-37` setup background | runbook `Environment and storage layout`; AGENTS summary | Keep both repo roots, runtime user, split ownership, and active-tree discovery. |
| `41-71` non-negotiables | AGENTS summary; incidents stay in hazards | Keep all nine safety imperatives; do not copy incident narratives. |
| `75-130` topology/ports/setup | runbook `Topology and role identity`; AGENTS summary | Keep own-local-DPU, port 5433, same-lane RDMA, and both-DPU ABI-deploy rules. Source ABI version from the header; remove stale version 3. |
| `133-240` build/install/artifacts/clangd | runbook `Build, install, and artifact proof`; AGENTS summary | Preserve all-target build and Citus-install then PostgreSQL-relink/install order. |
| `243-266` host sync | runbook `Peer-host artifact deployment`; AGENTS summary | Replace broad source-tree rsync/md5 with installed-prefix receipt/checksum proof; no broad source `--delete`. |
| `270-343` clean baseline | runbook `Clean baseline`; AGENTS summary | Preserve data-dir stop, canonical `/proc/PID/exe` identity, both-DPU cleanup, and project-only shm removal. |
| `345-376` measured preflight | runbook `Candidate preflight`; AGENTS summary | Preserve clean process set and exact database identity; timeout/interrupted/manual-cleanup attempts are diagnostic-only. |
| `378-483` role/schema start | runbook `Start roles and prepare workload`; AGENTS summary | Preserve frontend env in postmaster, mandatory DPU services, supervisor identity, and schema/OID setup. |
| `485-526` workload matrix/incidents | compact AGENTS matrix + runbook `Acceptance profile`; incidents link to hazards/implementation notes | Gate and basebackup prove distinct paths; host-service paths are not acceptance. |
| `528-538` measurement rules | AGENTS summary + runbook `Measured candidates` | Warmup is never quoted; report full warmed set; no verbose logging; prove intended path. |
| `540-558` stable events | AGENTS policy + runner plan/implementation contracts | Preserve `HOMER_EVENT` grammar, bounded emission, typed identity, and contract-site update rule. |
| `560-614` runner authority | short AGENTS fail-closed rule + runner plan | Applicable checkers are mandatory now; live execute/resume/takeover cleanup remain disabled. |
| `616-644` DPU TCP smoke | runbook `DPU TCP transport smoke`; hazards for traps | Preserve both endpoint flags/logs and applicable before/after trigger. |
| `645-674` legacy host-service | compact AGENTS status; diagnostics for history | No routine command recipe or acceptance claim. |
| `675-923` selected-DPU gate | runbook `Selected-DPU command gate`; AGENTS summary | Preserve no-host-service topology, own/remote DPU distinction, four proofs, both-DPU scan/restart, client/capacity rules; baseline table stays diagnostics-only. |
| `924-964` four-role basebackup | runbook `Four-role basebackup`; AGENTS summary | Preserve consumer-first order, matching geometry, both role statuses, and wrap proof. |
| `965-1012` broken COPY | diagnostics broken-shape/history + implementation regression note | Keep only broken/not-acceptance status in AGENTS; no runnable recipe in operator runbook. |
| `1013-1039` mixed workload needing re-derivation | diagnostics as non-runnable history | Do not present as a procedure. |
| `1043-1075` regression sweep | AGENTS matrix/rule + runbook execution detail | Preserve all-target build, gate+basebackup, conditional TCP smoke, frontend-agent liveness, and enum/collector audit rule. |
| `1079-1095` post-run checklist | runbook `Teardown and verdict`; AGENTS verdict questions | Prove intended path and uncontaminated starting state, not exit zero alone. |
| `1098-1108` routing table | AGENTS router | Add operator runbook and runner plan; retain implementation-contract routing. |

## Normalized fenced-block baseline

The hash is SHA-256 over nonblank lines after trimming leading/trailing whitespace. Plain evidence examples are
listed because their interpretation must survive even though they are not executable. Post-migration comparison
must classify each entry as `moved unchanged`, `moved with a documented factual correction`, `policy retained`, or
`removed because existing canonical leaf owns it`.

| old lines | kind | hash prefix | owning section / first substantive line | intended disposition |
|---|---|---|---|---|
| `28-31` | sh | `683be75e1929` | setup / ownership repair | runbook, corrected to split ownership rather than blanket chown |
| `144-159` | sh | `daf44411f412` | install order / `cd /data/dbcomm/citus-dbcomm` | runbook unchanged ordering |
| `163-167` | sh | `ce01b23329fc` | performance rebuild | runbook |
| `178-184` | sh | `a8ada3aeb2af` | all-target build | runbook |
| `195-198` | sh | `d4172b00eefb` | legacy shared DPU tree build | replace with run-scoped snapshot/DPU build procedure |
| `218-221` | sh | `a2816fd69c06` | installed protocol-name proof | runbook |
| `229-232` | sh | `fb9d1e325c0b` | clangd database | runbook developer setup |
| `256-266` | sh | `dc5010c153ea` | broad source/prefix rsync | remove refuted source sync; replace with snapshot plus prefix receipt/checksum procedure |
| `277-280` | sh | `ca112eef6c71` | two-host PostgreSQL stop | runbook, peer missing-data role made explicit |
| `285-295` | sh | `6ba04fa917d1` | host process reap | runbook, use checked-in canonical-path helper |
| `310-316` | sh | `39f07dbf3110` | DPU process reap | runbook, use checked-in helper/supervisor |
| `320-322` | sh | `a23efaefc401` | shm inventory | runbook |
| `355-358` | sh | `65e971f95085` | approximate process eyeball | runbook, explicitly non-authoritative |
| `367-371` | sh | `0320208d05af` | exact database identity | runbook |
| `380-384` | sh | `240791d4a879` | PostgreSQL start | runbook, reconciled port fact |
| `393-400` | sh | `34a754310795` | frontend-agent PostgreSQL start | runbook |
| `409-423` | sh | `d2952fd4dbde` | host services | historical/non-selected-DPU only; do not make acceptance recipe |
| `440-444` | sh | `0e43b6b07bb7` | DPU service | runbook, supervised current procedure |
| `458-467` | sh | `445833413da7` | schema/OID preparation | runbook |
| `475-481` | sh | `62bec62f503a` | performance-table reset/stats | runbook |
| `546-548` | text | `a88d1440c5c6` | `HOMER_EVENT v=1` grammar | AGENTS policy |
| `577-581` | sh | `e1fafdcee32b` | disabled live-runner commands | short AGENTS prohibition + runner plan |
| `626-641` | sh | `9a5a172d6e2c` | DPU TCP smoke | runbook |
| `661-670` | sh | `692d040b5151` | local legacy host-service smoke/baseline | diagnostics or explicit task only; not acceptance runbook |
| `728-753` | sh | `0eb8bfc61734` | selected-DPU gate | runbook |
| `820-823` | sh | `86ac2ea6864d` | CPU sizing example | runbook |
| `840-844` | evidence | `d65073001227` | pool-exhaustion symptom | capacity implementation note; concise runbook warning |
| `877-881` | evidence | `c457f65ae828` | untagged semantic failure | parser/hazards; concise runbook evidence rule |
| `887-890` | sh | `f1e0ed7c3ba8` | legacy alarm scan | runbook fallback; deterministic checker preferred |
| `940-953` | sh | `11d72b526d7b` | basebackup consumer then sender | runbook |
| `957-960` | sh | `25efb5c18c79` | basebackup abort probe | diagnostics/explicit negative validation only |
| `977-1007` | sh | `8cdc1e40af66` | broken COPY | diagnostics only; no operator recipe |
| `1072-1075` | sh | `e3967dea5b4f` | enum/collector audit | AGENTS coding rule + runbook reference |
| `1081-1089` | sh | `c781d1ec94fa` | post-run output/log scan | runbook, deterministic checker preferred |

## Post-migration proof

Stages 2-4 produced one canonical operator runbook and a policy-only `AGENTS.md`:

- All 34 old fenced-block entries above have a disposition: current operator commands moved into the runbook;
  `HOMER_EVENT` and runner prohibitions remain policy; diagnostic/negative/broken shapes route to diagnostics; the
  ownership, DPU shared-tree, source-sync, port, protocol-version, and basebackup ordering blocks were replaced by the
  factual corrections recorded in the split plan.
- The operator runbook contains 28 workflow-focused fenced blocks. It intentionally has no runnable host-service
  pgbench, backend-to-backend COPY, obsolete mixed-workload, basebackup negative-probe recipe, or performance baseline
  table.
- `AGENTS.md` names all twelve safety sentinels, the acceptance matrix, measurement and stable-event rules, runner
  fail-closed/checker boundary, unexpected-state handoff, enum/collector audit, and canonical KB routes.
- Repository links and headings are checked in Stage 5. The personal Claude/Codex validation-agent instructions and
  the installed farnet-validation skill now name the operator runbook explicitly.
- Intentionally changed command families: split ownership repair; installed-prefix-only checksum deployment;
  run-scoped DPU source/build; canonical process helpers; farnet0 explicit absent-data capability; pinned port 5433;
  bridge version sourced as 5 on 2026-07-16; consumer-first basebackup. Their rationale/evidence is in the split plan.
