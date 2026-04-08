#!/usr/bin/env python3
"""Archive PostgreSQL collector logs from one or more cluster nodes.

The latency-tracing benchmark runs now rely on collector-backed server logs
instead of the old pg_ctl startup redirection file. This helper copies every
collector logfile from each node into a local archive directory so the latency
trace parser can consume a stable, run-specific snapshot.

The script intentionally keeps the control path simple:
    - discover the collector logfiles on each node
    - copy those files into the requested archive directory
- record a small CSV manifest for later parsing/debugging

    It does not rotate logs itself; callers should rotate first if they want a
    fresh boundary around a benchmark run.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_NODES = ("node-0", "node-1", "node-2", "node-3", "node-4")


@dataclass(frozen=True)
class ArchivedLog:
    """One copied logfile and the remote source path it came from."""

    node: str
    source_path: str
    destination_path: str
    size_bytes: int


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one command and capture stdout/stderr for error reporting."""

    return subprocess.run(args, check=check, text=True, capture_output=True)


def ssh_cmd(node: str, remote_cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a remote command through SSH."""

    return run_cmd(["ssh", "-o", "BatchMode=yes", node, remote_cmd])


def discover_logfiles(node: str) -> list[str]:
    """Return every collector logfile currently present on one node.

    The helper intentionally snapshots the whole collector directory instead of
    just pg_current_logfile(), because the benchmark runs can rotate through
    many log segments before we get a chance to archive them. Keeping the whole
    set makes the later latency-trace parser robust against rotation timing.
    """

    list_cmd = (
        "set -e; "
        "cd /users/JasonHu/pg/data/log; "
        "find . -maxdepth 1 -type f -name 'postgresql-*.log' | sort"
    )
    result = ssh_cmd(node, list_cmd)
    paths: list[str] = []
    for rel_path in result.stdout.splitlines():
        rel_path = rel_path.strip()
        if not rel_path:
            continue
        if rel_path.startswith("./"):
            rel_path = rel_path[2:]
        paths.append(f"/users/JasonHu/pg/data/log/{rel_path}")

    if not paths:
        raise RuntimeError(f"could not find any collector logfiles on {node}")

    return paths


def discover_current_logfile(node: str) -> str:
    """Return the newest existing collector logfile path for one node.

    The logging collector can advance its internal "current" logfile name
    before the corresponding file is visible on disk. For per-row snapshots we
    want the logfile that actually exists and contains the finished row's
    output, so pick the newest collector file by modification time instead of
    trusting `pg_current_logfile()`.
    """

    list_cmd = (
        "set -e; "
        "cd /users/JasonHu/pg/data/log; "
        "latest=$(ls -1t postgresql-*.log 2>/dev/null | head -n 1); "
        "if [ -n \"$latest\" ]; then printf '%s\n' \"$latest\"; fi"
    )
    result = ssh_cmd(node, list_cmd)
    path = result.stdout.strip()
    if path:
        return f"/users/JasonHu/pg/data/log/{path}"
    raise RuntimeError(f"could not determine current logfile on {node}")


def copy_logfile(node: str, source_path: str, dest_path: Path) -> None:
    """Copy one logfile from a node into the local archive directory."""

    scp_target = f"{node}:{source_path}"
    run_cmd(["scp", "-o", "BatchMode=yes", scp_target, str(dest_path)])


def archive_nodes(nodes: list[str], output_dir: Path) -> list[ArchivedLog]:
    """Archive all collector logfiles from every requested node."""

    output_dir.mkdir(parents=True, exist_ok=True)
    archived: list[ArchivedLog] = []

    for node in nodes:
        for source_path in discover_logfiles(node):
            dest_path = output_dir / f"{node}-{Path(source_path).name}"
            copy_logfile(node, source_path, dest_path)
            size_bytes = dest_path.stat().st_size
            archived.append(
                ArchivedLog(
                    node=node,
                    source_path=source_path,
                    destination_path=str(dest_path),
                    size_bytes=size_bytes,
                )
            )

    return archived


def write_manifest(archived: list[ArchivedLog], output_dir: Path) -> None:
    """Write a small CSV manifest describing the copied logs."""

    manifest_path = output_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["node", "source_path", "destination_path", "size_bytes"],
        )
        writer.writeheader()
        for entry in archived:
            writer.writerow(
                {
                    "node": entry.node,
                    "source_path": entry.source_path,
                    "destination_path": entry.destination_path,
                    "size_bytes": entry.size_bytes,
                }
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Archive PostgreSQL collector logs from cluster nodes.")
    parser.add_argument(
        "--nodes",
        nargs="+",
        default=list(DEFAULT_NODES),
        help="SSH hostnames to archive. Defaults to all five cluster nodes.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Local directory where the copied logs and manifest should be written.",
    )
    parser.add_argument(
        "--current-only",
        action="store_true",
        help="Archive only the current logfile for each node instead of every collector segment.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Copy each node's active logfile into the requested archive directory."""

    args = parse_args(argv)
    output_dir = Path(args.output_dir)

    try:
        if args.current_only:
            archived = []
            output_dir.mkdir(parents=True, exist_ok=True)
            for node in list(args.nodes):
                source_path = discover_current_logfile(node)
                dest_path = output_dir / f"{node}.log"
                copy_logfile(node, source_path, dest_path)
                size_bytes = dest_path.stat().st_size
                archived.append(
                    ArchivedLog(
                        node=node,
                        source_path=source_path,
                        destination_path=str(dest_path),
                        size_bytes=size_bytes,
                    )
                )
            write_manifest(archived, output_dir)
        else:
            archived = archive_nodes(list(args.nodes), output_dir)
            write_manifest(archived, output_dir)
    except Exception as exc:
        print(f"error: failed to archive logs: {exc}", file=sys.stderr)
        return 1

    for entry in archived:
        print(f"{entry.node}: {entry.source_path} -> {entry.destination_path} ({entry.size_bytes} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
