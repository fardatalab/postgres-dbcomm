"""Atomic evidence files, command records, and candidate log intervals."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


class EvidenceStore:
    """Append-only event/command journals plus an atomically replaced run manifest."""

    def __init__(self, root: Path, create: bool = True):
        self.root = root
        if create:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)

    def append_jsonl(self, name: str, value: Any) -> None:
        path = self.root / name
        line = json.dumps(value, sort_keys=True) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def event(self, event: str, **fields: Any) -> None:
        self.append_jsonl("events.jsonl", {"time_ns": time.time_ns(), "event": event, **fields})

    def command(self, record: dict[str, Any]) -> None:
        self.append_jsonl("commands.jsonl", record)

    def write_run(self, value: dict[str, Any]) -> None:
        atomic_json(self.root / "run.json", value)

    def write_summary(self, value: dict[str, Any]) -> None:
        atomic_json(self.root / "summary.json", value)
        lines = [f"# Farnet validation: {value.get('verdict', 'INCONCLUSIVE')}", "",
                 f"Run: `{value.get('run_id', '-')}`", f"Profile: `{value.get('profile', '-')}`", ""]
        for check in value.get("checks", []):
            lines.append(f"- **{check['verdict']}** `{check['name']}` — {check['detail']}")
        atomic_write(self.root / "summary.md", "\n".join(lines) + "\n")


@dataclass(frozen=True)
class LogMark:
    device: int
    inode: int
    offset: int


def mark_log(path: Path) -> LogMark:
    st = path.stat()
    return LogMark(st.st_dev, st.st_ino, st.st_size)


def read_log_interval(path: Path, start: LogMark, end: LogMark | None = None) -> bytes:
    """Read one candidate interval and reject rotation, truncation, or overlap ambiguity."""
    current = path.stat()
    finish = end or LogMark(current.st_dev, current.st_ino, current.st_size)
    if (start.device, start.inode) != (finish.device, finish.inode):
        raise ValueError("log rotated during candidate interval")
    if (current.st_dev, current.st_ino) != (finish.device, finish.inode):
        raise ValueError("log identity changed after end mark")
    if finish.offset < start.offset or current.st_size < finish.offset:
        raise ValueError("log truncated during candidate interval")
    with path.open("rb") as stream:
        stream.seek(start.offset)
        return stream.read(finish.offset - start.offset)
