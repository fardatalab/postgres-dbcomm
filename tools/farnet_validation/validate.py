#!/usr/bin/env python3
"""CLI for planning, running, artifact-resuming, summarizing, and takeover."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.farnet_validation.checks import alarm_check, basebackup_check, gate_check, teardown_checks
from tools.farnet_validation.evidence import EvidenceStore, sha256_file
from tools.farnet_validation.model import CheckResult, FailureKind, Verdict, combine
from tools.farnet_validation.phases import PhaseEngine, RunConfig, render_plan
from tools.farnet_validation.profiles import PROFILES, expand_profile
from tools.farnet_validation.receipts import load_receipt, validate_chain
from tools.farnet_validation.takeover import capture_takeover, cleanup_takeover


# Live execution stays closed until the receipt/probe/takeover implementation
# passes the plan-mandated known-green run.  Planning, evidence parsing, and
# fixtures are useful meanwhile; silently running a weaker live workflow is not.
LIVE_EXECUTION_READY = False
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")


def run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def add_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--artifact-mode", choices=("current-source", "existing"), default="current-source")
    parser.add_argument("--artifact-receipt")
    parser.add_argument("--prepare-through", choices=("build", "install", "peer", "dpus"))
    parser.add_argument("--baseline-run")
    parser.add_argument("--postgres-tree", default="/data/dbcomm/postgres-citus")
    parser.add_argument("--citus-tree", default="/data/dbcomm/citus-dbcomm")
    parser.add_argument("--prefix", default="/data/dbcomm/pg-citus")
    parser.add_argument("--run-id", default=run_id())
    parser.add_argument("--evidence-dir")
    parser.add_argument("--gate-transactions", type=int, default=2000)
    parser.add_argument("--postgres-untracked", action="append", default=[])
    parser.add_argument("--citus-untracked", action="append", default=[])


def config_from(args: argparse.Namespace, execute: bool = False) -> RunConfig:
    evidence = args.evidence_dir or f"/tmp/farnet-validation-{args.run_id}"
    if not SAFE_RUN_ID.fullmatch(args.run_id):
        raise ValueError("run id must be a 1-64 character shell-safe token")
    if (not SAFE_REMOTE_PATH.fullmatch(args.prefix) or ".." in Path(args.prefix).parts
            or not Path(args.prefix).is_absolute()):
        raise ValueError("prefix must be an absolute shell-safe path without '..'")
    if args.artifact_mode == "existing" and not args.artifact_receipt:
        raise ValueError("existing artifact mode requires --artifact-receipt")
    if args.profile == "dma-bridge" and not args.baseline_run:
        raise ValueError("dma-bridge requires --baseline-run")
    if execute and not LIVE_EXECUTION_READY:
        raise ValueError(
            "authoritative live execution is fail-closed until receipt, remote takeover, "
            "runtime identity, and known-green forward validation are complete")
    return RunConfig(args.run_id, args.profile, args.artifact_mode, args.prepare_through,
                     args.postgres_tree, args.citus_tree, args.prefix, evidence,
                     args.artifact_receipt, args.baseline_run,
                     gate_transactions=args.gate_transactions, execute=execute,
                     postgres_untracked=tuple(args.postgres_untracked),
                     citus_untracked=tuple(args.citus_untracked))


def summarize(directory: Path) -> Verdict:
    store = EvidenceStore(directory, create=False)
    run = json.loads((directory / "run.json").read_text())
    checks: list[CheckResult] = []
    run_status = run.get("status")
    if run_status != "PASS":
        checks.append(CheckResult(
            "run-status", Verdict.INCONCLUSIVE,
            f"runner did not finish authoritatively: status={run_status!r}",
            FailureKind.EVIDENCE))
    options = run.get("options", {})
    try:
        expected_phases = expand_profile(run["profile"], options.get("artifact_mode", "current-source"),
                                         options.get("prepare_through"))
    except (KeyError, ValueError):
        expected_phases = ()
        checks.append(CheckResult("phase-journal", Verdict.INCONCLUSIVE,
                                  "run profile/options cannot be expanded", FailureKind.EVIDENCE))
    phase_records = run.get("phases", [])
    if ([item.get("name") for item in phase_records] != list(expected_phases) or
            any(item.get("status") != "PASS" for item in phase_records)):
        checks.append(CheckResult("phase-journal", Verdict.INCONCLUSIVE,
                                  "phase journal is incomplete, reordered, or non-PASS",
                                  FailureKind.EVIDENCE))
    command_records = []
    commands_path = directory / "commands.jsonl"
    if commands_path.is_file():
        for line in commands_path.read_text(errors="replace").splitlines():
            try:
                command_records.append(json.loads(line))
            except json.JSONDecodeError:
                checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                          "malformed command journal record",
                                          FailureKind.EVIDENCE))
                break
    if not command_records:
        checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                  "command journal is absent or empty", FailureKind.EVIDENCE))
    elif any(item.get("status") != "EXITED" or item.get("returncode") != 0
             for item in command_records):
        checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                  "one or more commands lack a clean captured exit",
                                  FailureKind.EVIDENCE))
    else:
        evidence_root = directory.resolve()
        for item in command_records:
            log_name = item.get("log")
            digest = item.get("log_sha256")
            if not log_name or not digest:
                checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                          "a command lacks log identity evidence", FailureKind.EVIDENCE))
                break
            log_path = Path(log_name).resolve()
            try:
                log_path.relative_to(evidence_root / "logs")
            except ValueError:
                checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                          "a command log escapes the evidence directory",
                                          FailureKind.EVIDENCE))
                break
            if not log_path.is_file() or sha256_file(log_path) != digest:
                checks.append(CheckResult("command-journal", Verdict.INCONCLUSIVE,
                                          "a command log is absent or its digest changed",
                                          FailureKind.EVIDENCE))
                break
    receipt_path = options.get("artifact_receipt")
    if not receipt_path:
        checks.append(CheckResult("artifact-receipt", Verdict.INCONCLUSIVE,
                                  "authoritative artifact receipt is absent", FailureKind.IDENTITY))
    else:
        try:
            receipt = load_receipt(Path(receipt_path))
            valid, edge, detail = validate_chain(receipt.get("edges", []), receipt.get("source_identity"))
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            checks.append(CheckResult("artifact-receipt", Verdict.INCONCLUSIVE,
                                      f"artifact receipt could not be validated: {exc}",
                                      FailureKind.IDENTITY))
        else:
            if not valid:
                checks.append(CheckResult("artifact-receipt", Verdict.INCONCLUSIVE,
                                          f"artifact receipt invalid at {edge}: {detail}",
                                          FailureKind.IDENTITY))
    def read_command(name: str) -> str:
        paths = [Path(item["log"]) for item in command_records
                 if item.get("name") == name and item.get("log")]
        return "\n".join(path.read_text(errors="replace") for path in paths if path.is_file())
    gate_phases = [phase for phase in ("debug-gate", "warmup", "repeat-1", "repeat-2", "repeat-3")
                   if phase in expected_phases]
    if gate_phases:
        for phase in gate_phases:
            client = read_command(f"{phase}-gate-client")
            if not client:
                checks.append(CheckResult(f"{phase}-gate", Verdict.INCONCLUSIVE,
                                          "candidate client log is missing", FailureKind.EVIDENCE))
                continue
            expected = 5 if phase == "debug-gate" else int(run.get("options", {}).get("gate_transactions", 2000))
            farnet1 = read_command(f"{phase}-farnet1-dpu-interval")
            farnet0 = read_command(f"{phase}-farnet0-dpu-interval")
            checks.append(gate_check(client, farnet1, expected, phase == "debug-gate"))
            item = alarm_check(farnet1, farnet0)
            checks.append(CheckResult(f"{phase}-{item.name}", item.verdict, item.detail,
                                      item.failure_kind, item.values))
    if run.get("profile") in {"transport-acceptance", "dma-bridge"}:
        checks.append(basebackup_check(read_command("basebackup-sender"),
                                       read_command("basebackup-consumer-wait")))
        checks.append(alarm_check(read_command("basebackup-farnet1-dpu-interval"),
                                  read_command("basebackup-farnet0-dpu-interval")))
    if "collect-teardown-logs" in expected_phases:
        dpu1, dpu0 = read_command("farnet1-dpu-full"), read_command("farnet0-dpu-full")
        checks.append(alarm_check(dpu1, dpu0))
        for role, output in (("farnet1-dpu", dpu1), ("farnet0-dpu", dpu0)):
            role_checks = teardown_checks(output)
            checks.extend(CheckResult(f"{role}-{item.name}", item.verdict, item.detail,
                                      item.failure_kind, item.values) for item in role_checks)
    verdict = combine(checks)
    store.write_summary({"run_id": run.get("run_id"), "profile": run.get("profile"),
                         "verdict": verdict.value, "checks": [check.as_dict() for check in checks]})
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        child = sub.add_parser(name)
        add_run_args(child)
        if name == "run":
            child.add_argument("--execute", action="store_true",
                               help="authorize state mutation; absent means evidence-producing dry-run")
    resume = sub.add_parser("resume")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--execute", action="store_true")
    summary = sub.add_parser("summarize")
    summary.add_argument("run_dir", type=Path)
    takeover = sub.add_parser("takeover")
    takeover.add_argument("run_dir", type=Path)
    takeover.add_argument("--token", required=True)
    takeover.add_argument("--execute", action="store_true",
                          help="authorize takeover cleanup mutation; capture stays read-only")
    action = takeover.add_mutually_exclusive_group(required=True)
    action.add_argument("--capture", action="store_true")
    action.add_argument("--cleanup", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            print(json.dumps(render_plan(config_from(args)), sort_keys=True, indent=2))
            return 0
        if args.command == "run":
            config = config_from(args, args.execute)
            evidence_path = Path(config.evidence_dir)
            if evidence_path.exists() and any(evidence_path.iterdir()):
                raise ValueError("new run evidence directory must not already contain files")
            verdict = PhaseEngine(config, EvidenceStore(Path(config.evidence_dir))).run()
            return verdict.exit_code
        if args.command == "resume":
            if args.execute and not LIVE_EXECUTION_READY:
                raise ValueError(
                    "authoritative live resume is fail-closed until the runner passes known-green validation")
            old = json.loads((args.run_dir / "run.json").read_text())
            options = dict(old["options"])
            if any(phase["name"] == "clean" for phase in old.get("phases", [])):
                print("runtime state is never resumed; validating artifacts and starting a fresh run", file=sys.stderr)
            receipt_path = options.get("artifact_receipt")
            if not receipt_path:
                raise ValueError("resume requires a persisted artifact receipt")
            receipt = load_receipt(Path(receipt_path))
            valid, edge, detail = validate_chain(receipt.get("edges", []), receipt.get("source_identity"))
            if not valid:
                raise ValueError(f"artifact receipt invalid at {edge}: {detail}")
            options.update(run_id=f"{old['run_id']}-resume-{int(time.time())}", artifact_mode="existing",
                           execute=args.execute)
            options["evidence_dir"] = f"/tmp/farnet-validation-{options['run_id']}"
            config = RunConfig(**options)
            return PhaseEngine(config, EvidenceStore(Path(config.evidence_dir))).run().exit_code
        if args.command == "summarize":
            return summarize(args.run_dir).exit_code
        if args.command == "takeover":
            if args.capture:
                print(capture_takeover(args.run_dir, args.token))
            else:
                if not args.execute:
                    raise ValueError("takeover cleanup requires --execute")
                if not LIVE_EXECUTION_READY:
                    raise ValueError(
                        "takeover cleanup is fail-closed until live runner lifecycle handling is authoritative")
                print(json.dumps(cleanup_takeover(args.run_dir, args.token), sort_keys=True, indent=2))
            return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"invalid/inconclusive invocation: {exc}", file=sys.stderr)
        return 3
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
