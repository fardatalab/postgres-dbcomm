"""Exact-argv command execution with hard evidence and soft-timeout takeover."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .evidence import EvidenceStore, sha256_file
from .model import FailureKind, Verdict


ALLOWLISTED_ENV = frozenset({
    "CCACHE_DISABLE", "CPPFLAGS", "HOMER_VALIDATION_RUN_ID", "HOMER_SERVICE_CPU",
    "HOMER_REMOTE_EXEC_BACKEND_CPUS", "HOMER_SERVICE_ENABLE_DPU_DMA",
    "HOMER_SERVICE_ENABLE_DOCA_DMA", "HOMER_SERVICE_DPU_SETUP_PORT",
    "HOMER_SERVICE_DOCA_DEV_PCI", "HOMER_SERVICE_PEER_BIND_HOST",
    "HOMER_SERVICE_PEER_PORT", "HOMER_FRONTEND_DPU_SETUP_HOST",
    "HOMER_FRONTEND_DPU_SETUP_PORT", "HOMER_FRONTEND_DOCA_DEV_PCI",
    "HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS", "PATH", "LC_ALL",
})


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 60.0
    mutates: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "argv": list(self.argv), "cwd": self.cwd,
                "env": dict(sorted(self.env.items())), "timeout_s": self.timeout_s,
                "mutates": self.mutates}


@dataclass(frozen=True)
class CommandResult:
    verdict: Verdict
    failure_kind: FailureKind | None
    returncode: int | None
    timed_out: bool
    pid: int | None
    log_path: str | None


class CommandRunner:
    def __init__(self, evidence: EvidenceStore, execute: bool):
        self.evidence = evidence
        self.execute = execute
        self.live_processes: dict[int, subprocess.Popen] = {}

    def run(self, spec: CommandSpec) -> CommandResult:
        unknown = set(spec.env) - ALLOWLISTED_ENV
        if unknown:
            raise ValueError(f"non-allowlisted environment keys: {sorted(unknown)}")
        if not self.execute:
            self.evidence.command({**spec.as_dict(), "status": "DRY_RUN"})
            return CommandResult(Verdict.PASS, None, 0, False, None, None)
        command_no = sum(1 for _ in (self.evidence.root / "commands.jsonl").open()) if (self.evidence.root / "commands.jsonl").exists() else 0
        log = self.evidence.root / "logs" / f"{command_no:04d}-{spec.name}.log"
        log.parent.mkdir(mode=0o700, exist_ok=True)
        started_ns = time.time_ns()
        # Do not let an unrecorded caller environment alter a candidate command.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"}
        env.update(spec.env)
        with log.open("wb") as output:
            process = subprocess.Popen(spec.argv, cwd=spec.cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                rc = process.wait(timeout=spec.timeout_s)
                timed_out = False
            except subprocess.TimeoutExpired:
                # Soft timeout deliberately leaves the process group live for bounded takeover.
                rc = None
                timed_out = True
                self.live_processes[process.pid] = process
        record = {**spec.as_dict(), "status": "SOFT_TIMEOUT" if timed_out else "EXITED",
                  "started_ns": started_ns, "ended_ns": time.time_ns(), "pid": process.pid,
                  "returncode": rc, "log": str(log), "log_sha256": sha256_file(log)}
        self.evidence.command(record)
        if timed_out:
            return CommandResult(Verdict.AWAITING_AGENT, FailureKind.TIMEOUT, None, True,
                                 process.pid, str(log))
        if rc:
            kind = FailureKind.BUILD if "build" in spec.name or "relink" in spec.name else FailureKind.WORKLOAD
            verdict = Verdict.FAIL if kind == FailureKind.BUILD else Verdict.INCONCLUSIVE
            return CommandResult(verdict, kind, rc, False, process.pid, str(log))
        return CommandResult(Verdict.PASS, None, rc, False, process.pid, str(log))

    def reap(self, pid: int) -> int | None:
        """Collect a preserved soft-timeout child after authenticated takeover cleanup."""
        process = self.live_processes.pop(pid, None)
        if process is None:
            return None
        return process.wait()


def terminate_verified_process(pid: int, start_ticks: int, sig: int = signal.SIGTERM) -> bool:
    """Signal only when PID and kernel start time still match the captured identity."""
    stat = Path(f"/proc/{pid}/stat")
    try:
        current = int(stat.read_text().split()[21])
    except (OSError, ValueError, IndexError):
        return False
    if current != start_ticks:
        return False
    os.killpg(pid, sig)
    return True
