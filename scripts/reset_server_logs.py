#!/usr/bin/env python3
"""Rotate PostgreSQL collector logs on selected cluster nodes.

The benchmark runner now snapshots the active logfile path plus byte offset at
row start, so row-local archiving no longer depends on pruning filenames.
Keeping this helper limited to rotation avoids guessing which collector
segment is "current" when the logging collector reuses the same logfile name
across rotations.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_NODES = ("node-0", "node-1", "node-2")


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one command and capture stdout/stderr for debugging."""

    return subprocess.run(args, check=check, text=True, capture_output=True)


def ssh_cmd(node: str, remote_cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a remote command through SSH."""

    return run_cmd(["ssh", "-o", "BatchMode=yes", node, remote_cmd])


def rotate_node(node: str) -> str:
    """Rotate the node's collector logfile and report the active logfile."""

    query_cmd = (
        "export PATH=$HOME/pg/bin:$PATH; "
        "psql -Atqc \"select pg_rotate_logfile();\" postgres"
    )
    ssh_cmd(node, query_cmd)

    list_cmd = (
        "set -e; "
        "cd /users/JasonHu/pg/data/log; "
        "current=$(ls -1t postgresql-*.log 2>/dev/null | head -n 1); "
        "if [ -z \"$current\" ]; then exit 1; fi; "
        "printf '%s\n' \"$current\""
    )
    result = ssh_cmd(node, list_cmd)
    current_name = result.stdout.strip()
    if not current_name:
        raise RuntimeError(f"could not determine current logfile on {node}")
    return f"/users/JasonHu/pg/data/log/{current_name}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Rotate and prune PostgreSQL collector logs on cluster nodes.")
    parser.add_argument(
        "--nodes",
        nargs="+",
        default=list(DEFAULT_NODES),
        help="SSH hostnames to rotate/prune. Defaults to the primary benchmark nodes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Rotate and prune the collector logs on the requested nodes."""

    args = parse_args(argv)

    try:
        for node in list(args.nodes):
            current_path = rotate_node(node)
            print(f"{node}: rotated to {current_path}")
    except Exception as exc:
        print(f"error: failed to reset logs: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
