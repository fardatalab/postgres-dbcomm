"""Bounded unexpected-state capture and token-authenticated takeover controls."""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .commands import terminate_verified_process
from .evidence import atomic_json


def process_identity(pid: int) -> dict:
    base = Path(f"/proc/{pid}")
    result = {"pid": pid}
    try:
        parts = (base / "stat").read_text().split()
        result.update(start_ticks=int(parts[21]), state=parts[2])
    except (OSError, ValueError, IndexError) as exc:
        result["identity_error"] = str(exc)
        return result
    for name in ("status", "wchan", "stack"):
        try:
            result[name] = (base / name).read_text(errors="replace")[-8192:]
        except OSError as exc:
            result[f"{name}_error"] = str(exc)
    try:
        result["exe"] = os.readlink(base / "exe")
    except OSError as exc:
        result["exe_error"] = str(exc)
    return result


def create_takeover(run_dir: Path, run_id: str, suspect_pids: list[int], window_s: int = 300) -> dict:
    now = time.time()
    lease = {"version": 1, "run_id": run_id, "owner_pid": os.getpid(),
             "owner_start_ticks": process_identity(os.getpid()).get("start_ticks"),
             "token": secrets.token_urlsafe(32), "created": now, "expires": now + window_s,
             "cleanup_required": 1, "cleanup_completed": 0,
             "suspects": [process_identity(pid) for pid in suspect_pids]}
    atomic_json(run_dir / "takeover.json", lease)
    capture_takeover(run_dir, lease["token"])
    return lease


def _load_authorized(run_dir: Path, token: str) -> dict:
    lease = json.loads((run_dir / "takeover.json").read_text())
    if not secrets.compare_digest(str(lease.get("token", "")), token):
        raise PermissionError("takeover token mismatch")
    return lease


def capture_takeover(run_dir: Path, token: str) -> Path:
    lease = _load_authorized(run_dir, token)
    snapshot = {"captured": time.time(), "expired": time.time() > lease["expires"],
                "suspects": [process_identity(item["pid"]) for item in lease["suspects"]]}
    path = run_dir / f"takeover-capture-{time.time_ns()}.json"
    atomic_json(path, snapshot)
    return path


def cleanup_takeover(run_dir: Path, token: str) -> dict:
    lease = _load_authorized(run_dir, token)
    outcomes = []
    for suspect in lease["suspects"]:
        outcomes.append({"pid": suspect["pid"], "signalled": terminate_verified_process(
            suspect["pid"], suspect.get("start_ticks", -1))})
    lease["cleanup_completed"] = int(all(item["signalled"] for item in outcomes))
    lease["cleanup_outcomes"] = outcomes
    atomic_json(run_dir / "takeover.json", lease)
    return lease
