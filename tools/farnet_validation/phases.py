"""Ordered phase planner and engine for known-path farnet validation."""

from __future__ import annotations

import json
import re
import time
import fcntl
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .commands import CommandRunner, CommandSpec
from .evidence import EvidenceStore
from .model import FailureKind, Verdict
from .checks import alarm_check, basebackup_check, gate_check, teardown_checks
from .profiles import expand_profile
from .remote_helpers import HELPER_ROOT, helper_digest, invoke_spec, upload_specs
from .receipts import load_receipt, validate_chain
from .sources import capture_snapshot


@dataclass(frozen=True)
class RunConfig:
    run_id: str
    profile: str
    artifact_mode: str
    prepare_through: str | None
    postgres_tree: str
    citus_tree: str
    prefix: str
    evidence_dir: str
    artifact_receipt: str | None = None
    baseline_run: str | None = None
    repeats: int = 3
    gate_transactions: int = 2000
    execute: bool = False
    postgres_untracked: tuple[str, ...] = ()
    citus_untracked: tuple[str, ...] = ()


def _sudo_dbcomm(*argv: str) -> tuple[str, ...]:
    return ("sudo", "-n", "-u", "dbcomm", *argv)


def _remote_helper_names() -> tuple[str, ...]:
    """Return every checked-in executable helper that must retain digest parity."""
    return tuple(path.name for path in sorted(HELPER_ROOT.iterdir())
                 if path.suffix in {".sh", ".py"})


class PhasePlanner:
    """Translate each named state into auditable argv without executing shell text."""

    def __init__(self, config: RunConfig):
        self.c = config
        self.private = Path(config.evidence_dir) / "private"

    def phases(self) -> tuple[str, ...]:
        return expand_profile(self.c.profile, self.c.artifact_mode, self.c.prepare_through)

    def specs(self, phase: str) -> list[CommandSpec]:
        c, pg, citus, prefix = self.c, self.c.postgres_tree, self.c.citus_tree, self.c.prefix
        env = {"HOMER_VALIDATION_RUN_ID": c.run_id, "LC_ALL": "C"}
        if phase in {"orient", "artifact-receipt-validate"}:
            specs = [CommandSpec("git-postgres-head", ("git", "-C", pg, "rev-parse", "HEAD"), pg),
                     CommandSpec("git-citus-head", ("git", "-C", citus, "rev-parse", "HEAD"), pg)]
            helpers = _remote_helper_names()
            specs += upload_specs("dpu", c.run_id, helpers, pg)
            specs += upload_specs("farnet0", c.run_id, helpers, pg)
            specs += [invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh", ("prepare",), pg, 60, True)]
            specs += [invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh", ("install", name), pg, 60, True)
                      for name in helpers]
            return specs
        if phase == "ownership":
            roots = (f"{citus}/build", f"{pg}/build", prefix)
            return [CommandSpec("ownership-preflight", ("find", *roots, "-not", "-user", "dbcomm", "-print"), pg)]
        if phase == "source-snapshot":
            return []  # Python captures both manifests and archives atomically.
        if phase == "citus-config-build":
            src = str(self.private / "citus-src")
            return [
                CommandSpec("extract-citus-snapshot", ("tar", "-xf", str(Path(c.evidence_dir) / "sources/citus.tar"),
                                                       "-C", src), pg, mutates=True),
                CommandSpec("citus-configure", (f"{src}/configure", f"--with-pgconfig={prefix}/bin/pg_config"), src,
                            env={**env, "CCACHE_DISABLE": "1"}, timeout_s=300, mutates=True),
                CommandSpec("citus-build-all-targets", ("make", "-B", "-j8", "all", "service-bin", "client-bin",
                            "dpu-comch-transport-smoke-bin", "dpu-tcp-transport-smoke-bin", "frontend-dma-smoke",
                            "service-dpu-dma-smoke", "tuple-deform-smoke", "service-dpu-dma-doca-smoke",
                            "service-dpu-comch-smoke-bin"), src,
                            env={**env, "CPPFLAGS": "-D_GNU_SOURCE", "CCACHE_DISABLE": "1"},
                            timeout_s=1800, mutates=True)]
        if phase == "citus-install":
            src = str(self.private / "citus-src")
            return [CommandSpec("citus-install", _sudo_dbcomm("make", "install-headers", "install-service-bin", "install"),
                                src, env=env, timeout_s=900, mutates=True)]
        if phase == "postgres-config-relink":
            src, build = str(self.private / "postgres-src"), str(self.private / "postgres-build")
            return [CommandSpec("extract-postgres-snapshot", ("tar", "-xf", str(Path(c.evidence_dir) / "sources/postgres.tar"),
                                                          "-C", src), pg, mutates=True),
                    CommandSpec("postgres-meson-config", ("meson", "setup", build, src, f"--prefix={prefix}"), pg,
                                env={**env, "CCACHE_DISABLE": "1"}, timeout_s=600, mutates=True),
                    CommandSpec("postgres-relink", ("ninja", "-C", build), pg,
                                env={**env, "CCACHE_DISABLE": "1"}, timeout_s=1800, mutates=True)]
        if phase == "postgres-install":
            return [CommandSpec("postgres-install", _sudo_dbcomm("meson", "install", "-C",
                                str(self.private / "postgres-build"), "--no-rebuild"), pg,
                                env=env, timeout_s=900, mutates=True)]
        if phase == "peer-sync":
            return [CommandSpec("peer-prefix-sync", ("rsync", "-az", "--delete", "--exclude", "data/",
                                "--exclude", "*.log", "--exclude", "logfile", "--exclude", "stage_tmp/",
                                "--rsync-path=sudo -n -u dbcomm rsync", f"{prefix}/", f"farnet0:{prefix}/"),
                                pg, timeout_s=900, mutates=True)]
        if phase == "dpu-deploy":
            archive = str(Path(c.evidence_dir) / "sources/citus.tar")
            remote_archive = f"/tmp/farnet-validation-{c.run_id}/citus.tar"
            targets = ("service-bin", "dpu-tcp-transport-smoke-bin") if c.profile == "dma-bridge" else ("service-bin",)
            args = (c.run_id, remote_archive, *targets)
            return [CommandSpec("farnet1-dpu-source-upload", ("scp", archive, f"dpu:{remote_archive}"),
                                pg, timeout_s=300, mutates=True),
                    CommandSpec("farnet0-source-staging", ("scp", archive, f"farnet0:{remote_archive}"),
                                pg, timeout_s=300, mutates=True),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("install-data", remote_archive), pg, 300, True),
                    invoke_spec("dpu", c.run_id, "dpu_build.sh", args, pg, 1200, True),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("dpu_build.sh", *args), pg, 1200, True)]
        if phase == "validate-baseline-smoke":
            return [CommandSpec("validate-baseline-receipt", ("python3", "-m", "json.tool", c.baseline_run or ""), pg)]
        if phase == "candidate-dpu-tcp-smoke":
            return [invoke_spec("dpu", c.run_id, "dpu_tcp_smoke.sh", ("start", c.run_id), pg, 60, True),
                    CommandSpec("dpu-tcp-smoke-client", (str(self.private / "citus-src/build/homer/homer_dpu_tcp_transport_smoke"),
                                "--client", "--host", "10.10.1.201", "--dev-pci", "0000:21:00.0",
                                "--port", "9727", "--timeout-ms", "40000", "--export-posix-shm", "--fork-reader",
                                "--expect-backend-command-publish", "--expect-response-publish",
                                "--expect-backend-completion-pull", "--expect-byte-ring-pull",
                                "--expect-dpu-to-host-payload", "--expect-loop2-backpressure"), pg,
                                timeout_s=50, mutates=True),
                    invoke_spec("dpu", c.run_id, "dpu_tcp_smoke.sh", ("wait", c.run_id), pg, 60, True)]
        if phase == "clean":
            return [CommandSpec("postgres-stop", ("bash", str(HELPER_ROOT / "postgres_stop.sh"), prefix),
                                pg, timeout_s=60, mutates=True),
                    invoke_spec("farnet0", c.run_id, "postgres_stop.sh",
                                (prefix, "--allow-missing-data"), pg, 60, True),
                    CommandSpec("local-host-process-cleanup",
                                ("bash", str(HELPER_ROOT / "host_process_cleanup.sh"), prefix),
                                pg, timeout_s=60, mutates=True),
                    invoke_spec("farnet0", c.run_id, "host_process_cleanup.sh", (prefix,), pg, 60, True),
                    invoke_spec("dpu", c.run_id, "dpu_process_cleanup.sh", (c.run_id,), pg, 60, True),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh", ("dpu_process_cleanup.sh", c.run_id), pg, 60, True),
                    CommandSpec("local-project-shm-cleanup",
                                ("bash", str(HELPER_ROOT / "project_shm_cleanup.sh")), pg,
                                timeout_s=60, mutates=True),
                    invoke_spec("farnet0", c.run_id, "project_shm_cleanup.sh", (), pg, 60, True)]
        if phase == "start":
            return [invoke_spec("dpu", c.run_id, "dpu_supervisor.sh",
                                ("start", c.run_id, "10.10.1.201"), pg, 60, True),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("dpu_supervisor.sh", "start", c.run_id, "10.10.1.200"), pg, 60, True),
                    CommandSpec("postgres-start-frontend-agent", _sudo_dbcomm("env",
                                "HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.201", "HOMER_FRONTEND_DPU_SETUP_PORT=9727",
                                "HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0", "HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000",
                                f"{prefix}/bin/pg_ctl", "-D", f"{prefix}/data", "-l", f"{prefix}/data/postgres.log",
                                "-o", "-c citus.enable_homer_dpu_frontend_agent=on", "start", "-w"),
                                pg, env=env, timeout_s=90, mutates=True)]
        if phase.endswith("preflight") or phase in {"post-start-preflight", "live-final-scan", "alarm-scan"}:
            dpu_expected = "zero" if phase == "final-process-listener-preflight" else "one"
            postgres_expected = "stopped" if dpu_expected == "zero" else "running"
            specs = [] if dpu_expected == "zero" else [CommandSpec(
                f"{phase}-database-identity", _sudo_dbcomm(f"{prefix}/bin/psql", "-h", "/tmp",
                "-p", "5433", "-U", "dbcomm", "-AtX", "postgres", "-c",
                "select current_setting('data_directory')"), pg)]
            specs += [CommandSpec(f"{phase}-local-host-process-probe",
                                  ("bash", str(HELPER_ROOT / "host_process_probe.sh"), prefix,
                                   postgres_expected), pg),
                      invoke_spec("farnet0", c.run_id, "host_process_probe.sh", (prefix, "stopped"), pg),
                      invoke_spec("dpu", c.run_id, "dpu_process_probe.sh", (c.run_id, dpu_expected), pg),
                      invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                  ("dpu_process_probe.sh", c.run_id, dpu_expected), pg)]
            return specs
        if phase == "prepare":
            return [CommandSpec("pgbench-schema-init", _sudo_dbcomm(f"{prefix}/bin/pgbench", "-h", "/tmp",
                                "-p", "5433", "-U", "dbcomm", "-i", "-s", "1", "postgres"), pg,
                                timeout_s=300, mutates=True),
                    CommandSpec("pgbench-history-truncate", _sudo_dbcomm(f"{prefix}/bin/psql", "-h", "/tmp",
                                "-p", "5433", "-U", "dbcomm", "-v", "ON_ERROR_STOP=1", "postgres", "-c",
                                "truncate pgbench_history"), pg, timeout_s=300, mutates=True),
                    *[CommandSpec(f"pgbench-vacuum-{table}",
                                  _sudo_dbcomm(f"{prefix}/bin/psql", "-h", "/tmp", "-p", "5433", "-U",
                                               "dbcomm", "-v", "ON_ERROR_STOP=1", "postgres", "-c",
                                               f"vacuum analyze {table}"),
                                  pg, timeout_s=300, mutates=True)
                      for table in ("pgbench_accounts", "pgbench_branches", "pgbench_tellers")],
                    CommandSpec("discover-db-user-oids", _sudo_dbcomm(f"{prefix}/bin/psql", "-h", "/tmp", "-p", "5433",
                                "-U", "dbcomm", "-AtX", "postgres", "-c",
                                "select (select oid from pg_database where datname='postgres'),"
                                "(select oid from pg_roles where rolname='dbcomm')"), pg),
                    CommandSpec("discover-arena-name", ("grep", "HOMER_FRONTEND_ARENA_SHM_NAME",
                                f"{citus}/src/backend/distributed/utils/homer/homer_frontend_agent.h"), pg)]
        if phase in {"debug-gate", "warmup"} or phase.startswith("repeat-"):
            transactions = "5" if phase == "debug-gate" else str(c.gate_transactions)
            debug = "1" if phase == "debug-gate" else "0"
            label = phase
            specs = [invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", ("mark", c.run_id, label), pg),
                     invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                 ("dpu_supervisor.sh", "mark", c.run_id, label), pg),
                     invoke_spec("farnet0", c.run_id, "gate.sh",
                                (prefix, "${DBOID}", "${USEROID}", transactions, debug), pg,
                                300 if phase == "debug-gate" else 900, True),
                     invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", ("interval", c.run_id, label), pg),
                     invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                 ("dpu_supervisor.sh", "interval", c.run_id, label), pg)]
            names = (f"{phase}-farnet1-mark", f"{phase}-farnet0-mark", f"{phase}-gate-client",
                     f"{phase}-farnet1-dpu-interval", f"{phase}-farnet0-dpu-interval")
            return [replace(spec, name=name) for spec, name in zip(specs, names)]
        if phase == "basebackup":
            tag = c.run_id.replace("-", "")[-12:]
            specs = [invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", ("mark", c.run_id, "basebackup"), pg),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("dpu_supervisor.sh", "mark", c.run_id, "basebackup"), pg),
                    invoke_spec("farnet0", c.run_id, "basebackup_consumer.sh",
                                ("start", c.run_id, prefix, "${DBOID}", "${USEROID}", tag, "4", "524288"), pg, 60, True),
                    CommandSpec("basebackup-sender", ("/usr/bin/time", "-p", *_sudo_dbcomm(f"{prefix}/bin/pg_basebackup", "-h", "/tmp",
                                "-p", "5433", "-U", "dbcomm", "-X", "none", "-c", "fast", "-t",
                                f"homer:mode=rdma,host=10.10.1.200,port=9717,node=2,slots=4,bytes=524288,tag={tag}", "-v")),
                                pg, env=env, timeout_s=900, mutates=True),
                    invoke_spec("farnet0", c.run_id, "basebackup_consumer.sh",
                                ("wait", c.run_id), pg, 900, True),
                    invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", ("interval", c.run_id, "basebackup"), pg),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("dpu_supervisor.sh", "interval", c.run_id, "basebackup"), pg)]
            names = ("basebackup-farnet1-mark", "basebackup-farnet0-mark", "basebackup-consumer-start",
                     "basebackup-sender", "basebackup-consumer-wait", "basebackup-farnet1-dpu-interval",
                     "basebackup-farnet0-dpu-interval")
            return [replace(spec, name=name) for spec, name in zip(specs, names)]
        if phase == "postgres-orderly-stop":
            return [CommandSpec("postgres-orderly-stop",
                                ("bash", str(HELPER_ROOT / "postgres_stop.sh"), prefix),
                                pg, timeout_s=60, mutates=True)]
        if phase == "host-survivor-cleanup":
            return [CommandSpec("local-host-survivor-cleanup",
                                ("bash", str(HELPER_ROOT / "host_process_cleanup.sh"), prefix),
                                pg, timeout_s=60, mutates=True),
                    invoke_spec("farnet0", c.run_id, "host_process_cleanup.sh", (prefix,), pg, 60, True)]
        if phase == "dpu-orderly-stop-wait":
            return [invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", ("stop", c.run_id), pg, 60, True),
                    invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                ("dpu_supervisor.sh", "stop", c.run_id), pg, 60, True)]
        if phase in {"collect-teardown-logs", "teardown-ledger-scan"}:
            action = "log" if phase == "collect-teardown-logs" else "status"
            specs = [invoke_spec("dpu", c.run_id, "dpu_supervisor.sh", (action, c.run_id), pg),
                     invoke_spec("farnet0", c.run_id, "dpu_dispatch.sh",
                                 ("dpu_supervisor.sh", action, c.run_id), pg)]
            names = (("farnet1-dpu-full", "farnet0-dpu-full") if action == "log" else
                     ("farnet1-dpu-status", "farnet0-dpu-status"))
            return [replace(spec, name=name) for spec, name in zip(specs, names)]
        if phase == "shared-memory-cleanup":
            return [CommandSpec("local-project-shm-cleanup",
                                ("bash", str(HELPER_ROOT / "project_shm_cleanup.sh")), pg,
                                timeout_s=60, mutates=True),
                    invoke_spec("farnet0", c.run_id, "project_shm_cleanup.sh", (), pg, 60, True)]
        return []


class PhaseEngine:
    def __init__(self, config: RunConfig, evidence: EvidenceStore):
        self.config, self.evidence = config, evidence
        self.planner = PhasePlanner(config)
        self.runner = CommandRunner(evidence, config.execute)
        self.runtime_values: dict[str, str] = {}
        self.command_logs: dict[str, Path] = {}
        self.lock_fd: int | None = None
        if config.execute:
            self.lock_fd = os.open("/tmp/farnet-validation-install.lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self.lock_fd)
                self.lock_fd = None
                raise ValueError("another farnet validation install/deploy lease is active")

    def _internal_phase(self, phase: str) -> tuple[Verdict, str | None]:
        """Run Python-owned provenance operations that have no shell command body."""
        if not self.config.execute:
            return Verdict.PASS, None
        if phase == "source-snapshot":
            source_dir = self.evidence.root / "sources"
            private = self.evidence.root / "private"
            for name in ("citus-src", "postgres-src", "postgres-build"):
                (private / name).mkdir(parents=True, exist_ok=True, mode=0o700)
            postgres = capture_snapshot(Path(self.config.postgres_tree), source_dir / "postgres.tar",
                                        self.config.postgres_untracked)
            citus = capture_snapshot(Path(self.config.citus_tree), source_dir / "citus.tar",
                                     self.config.citus_untracked)
            from .evidence import atomic_json
            atomic_json(source_dir / "manifest.json", {"postgres": postgres, "citus": citus})
        if phase == "artifact-receipt-validate":
            if not self.config.artifact_receipt:
                return Verdict.INVALID, FailureKind.INVOCATION.value
            receipt = load_receipt(Path(self.config.artifact_receipt))
            valid, _edge, _detail = validate_chain(receipt.get("edges", []), receipt.get("source_identity"))
            if not valid:
                return Verdict.INCONCLUSIVE, FailureKind.IDENTITY.value
        return Verdict.PASS, None

    def _resolve(self, spec: CommandSpec) -> CommandSpec:
        argv = tuple(self.runtime_values.get(arg[2:-1], arg)
                     if arg.startswith("${") and arg.endswith("}") else arg for arg in spec.argv)
        missing = [arg for arg in argv if arg.startswith("${")]
        if missing and self.config.execute:
            raise ValueError(f"runtime discovery missing for {missing}")
        return replace(spec, argv=argv)

    def _check_phase(self, phase: str) -> tuple[Verdict, str | None]:
        """Apply acceptance predicates before a later phase can borrow the evidence."""
        if not self.config.execute:
            return Verdict.PASS, None
        def text(name: str) -> str:
            path = self.command_logs.get(name)
            return path.read_text(errors="replace") if path else ""
        if phase in {"orient", "artifact-receipt-validate"}:
            for host in ("dpu", "farnet0"):
                for helper in _remote_helper_names():
                    output = text(f"hash-{host}-{helper}").split()
                    if len(output) < 2 or output[0] != helper_digest(helper):
                        return Verdict.INCONCLUSIVE, FailureKind.IDENTITY.value
        if phase == "ownership" and text("ownership-preflight").strip():
            return Verdict.INCONCLUSIVE, FailureKind.ENVIRONMENT.value
        if phase.endswith("preflight") or phase in {"post-start-preflight", "live-final-scan", "alarm-scan"}:
            if phase != "final-process-listener-preflight":
                identity = text(f"{phase}-database-identity").strip()
                if identity != f"{self.config.prefix}/data":
                    return Verdict.INCONCLUSIVE, FailureKind.IDENTITY.value
        if phase in {"debug-gate", "warmup"} or phase.startswith("repeat-"):
            expected = 5 if phase == "debug-gate" else self.config.gate_transactions
            farnet1_interval = text(f"{phase}-farnet1-dpu-interval")
            farnet0_interval = text(f"{phase}-farnet0-dpu-interval")
            interval_anchor = f"LOG_INTERVAL label={phase} "
            if not farnet1_interval.startswith(interval_anchor) or not farnet0_interval.startswith(interval_anchor):
                return Verdict.INCONCLUSIVE, FailureKind.EVIDENCE.value
            result = gate_check(text(f"{phase}-gate-client"), farnet1_interval,
                                expected, phase == "debug-gate")
            alarm = alarm_check(farnet1_interval, farnet0_interval)
            for check in (result, alarm):
                if check.verdict != Verdict.PASS:
                    return check.verdict, check.failure_kind.value if check.failure_kind else FailureKind.EVIDENCE.value
        if phase == "candidate-dpu-tcp-smoke":
            client = text("dpu-tcp-smoke-client")
            server = text("dpu-dpu_tcp_smoke-wait")
            if client.count("homer_dpu_tcp_transport_smoke: ok") != 1 or server.count("homer_dpu_tcp_transport_smoke: ok") != 1:
                return Verdict.INCONCLUSIVE, FailureKind.EVIDENCE.value
        if phase == "basebackup":
            farnet1_interval = text("basebackup-farnet1-dpu-interval")
            farnet0_interval = text("basebackup-farnet0-dpu-interval")
            if (not farnet1_interval.startswith("LOG_INTERVAL label=basebackup ") or
                    not farnet0_interval.startswith("LOG_INTERVAL label=basebackup ")):
                return Verdict.INCONCLUSIVE, FailureKind.EVIDENCE.value
            result = basebackup_check(text("basebackup-sender"), text("basebackup-consumer-wait"))
            alarm = alarm_check(farnet1_interval, farnet0_interval)
            for check in (result, alarm):
                if check.verdict != Verdict.PASS:
                    return check.verdict, check.failure_kind.value if check.failure_kind else FailureKind.EVIDENCE.value
        if phase == "teardown-ledger-scan":
            checks = teardown_checks(text("farnet1-dpu-full") + "\n" + text("farnet0-dpu-full"))
            checks.append(alarm_check(text("farnet1-dpu-full"), text("farnet0-dpu-full")))
            for check in checks:
                if check.verdict != Verdict.PASS:
                    return check.verdict, check.failure_kind.value if check.failure_kind else FailureKind.EVIDENCE.value
        return Verdict.PASS, None

    def run(self) -> Verdict:
        manifest = {"version": 1, "run_id": self.config.run_id, "profile": self.config.profile,
                    "options": self.config.__dict__, "status": "RUNNING", "phases": []}
        self.evidence.write_run(manifest)
        for phase in self.planner.phases():
            started = time.time_ns()
            self.evidence.event("phase-start", phase=phase)
            internal_verdict, failure = self._internal_phase(phase)
            status = internal_verdict.value
            for spec in self.planner.specs(phase) if internal_verdict == Verdict.PASS else ():
                spec = self._resolve(spec)
                result = self.runner.run(spec)
                if result.log_path:
                    self.command_logs[spec.name] = Path(result.log_path)
                if spec.name == "discover-db-user-oids" and result.log_path and result.verdict == Verdict.PASS:
                    match = re.search(r"(?m)^(\d+)\|(\d+)$", Path(result.log_path).read_text())
                    if not match:
                        status, failure = Verdict.INCONCLUSIVE.value, FailureKind.EVIDENCE.value
                        break
                    self.runtime_values.update(DBOID=match.group(1), USEROID=match.group(2))
                if result.verdict != Verdict.PASS:
                    status, failure = result.verdict.value, result.failure_kind.value if result.failure_kind else None
                    if result.verdict == Verdict.AWAITING_AGENT and result.pid:
                        from .takeover import create_takeover
                        create_takeover(self.evidence.root, self.config.run_id, [result.pid])
                    break
            if status == "PASS":
                checked, check_failure = self._check_phase(phase)
                status = checked.value
                failure = check_failure
                if checked == Verdict.INCONCLUSIVE:
                    from .takeover import create_takeover
                    create_takeover(self.evidence.root, self.config.run_id, [])
                    status = Verdict.AWAITING_AGENT.value
            record = {"name": phase, "started_ns": started, "ended_ns": time.time_ns(),
                      "status": status, "failure_kind": failure}
            manifest["phases"].append(record)
            self.evidence.event("phase-end", **record)
            self.evidence.write_run(manifest)
            print(f"{phase}: {status}", flush=True)
            if status != "PASS":
                manifest["status"] = status
                self.evidence.write_run(manifest)
                return Verdict(status)
        manifest["status"] = "PASS" if self.config.execute else "DRY_RUN"
        self.evidence.write_run(manifest)
        return Verdict.PASS


def render_plan(config: RunConfig) -> dict:
    planner = PhasePlanner(config)
    return {"run_id": config.run_id, "profile": config.profile,
            "artifact_mode": config.artifact_mode,
            "phases": [{"name": phase, "commands": [spec.as_dict() for spec in planner.specs(phase)]}
                       for phase in planner.phases()]}
