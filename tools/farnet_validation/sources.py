"""Immutable source manifests and deterministic run-owned snapshot archives."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceEntry:
    path: str
    mode: int
    kind: str
    sha256: str
    symlink_target: str | None = None


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(("git", "-C", str(repo), *args), stderr=subprocess.DEVNULL)


def _entry(repo: Path, relative: str) -> SourceEntry:
    path = repo / relative
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        digest = hashlib.sha256(target.encode()).hexdigest()
        return SourceEntry(relative, mode, "symlink", digest, target)
    if not path.is_file():
        raise ValueError(f"unsupported source input type: {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceEntry(relative, mode, "file", digest)


def source_manifest(repo: Path, authorized_untracked: tuple[str, ...] = ()) -> dict:
    """Fingerprint tracked files plus explicit untracked inputs; reject implicit inputs."""
    tracked = [p.decode() for p in _git(repo, "ls-files", "-z").split(b"\0") if p]
    untracked = [p.decode() for p in _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    authorized = set(authorized_untracked)
    unauthorized = sorted(set(untracked) - authorized)
    missing = sorted(authorized - set(untracked) - set(tracked))
    if unauthorized:
        raise ValueError(f"untracked inputs require explicit authorization: {unauthorized}")
    if missing:
        raise ValueError(f"authorized source inputs do not exist: {missing}")
    entries = [_entry(repo, path) for path in sorted(set(tracked) | authorized)]
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    index = hashlib.sha256(_git(repo, "diff", "--cached", "--binary")).hexdigest()
    unstaged = hashlib.sha256(_git(repo, "diff", "--binary")).hexdigest()
    body = [entry.__dict__ for entry in entries]
    identity = hashlib.sha256(repr((head, index, unstaged, body)).encode()).hexdigest()
    return {"repo": str(repo), "head": head, "index_sha256": index,
            "unstaged_sha256": unstaged, "entries": body, "identity": identity}


def capture_snapshot(repo: Path, archive: Path, authorized_untracked: tuple[str, ...] = ()) -> dict:
    """Capture only if byte/mode/symlink identity is unchanged across archive creation."""
    before = source_manifest(repo, authorized_untracked)
    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(archive, "w") as tar:
        for entry in before["entries"]:
            tar.add(repo / entry["path"], arcname=entry["path"], recursive=False)
    after = source_manifest(repo, authorized_untracked)
    if before != after:
        archive.unlink(missing_ok=True)
        raise RuntimeError("source changed while snapshot was captured")
    before["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    return before
