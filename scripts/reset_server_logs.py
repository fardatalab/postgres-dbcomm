#!/usr/bin/env python3
"""Rotate and prune PostgreSQL collector logs on selected cluster nodes.

This helper is used before each benchmark row so the next row starts from a
clean collector log boundary without truncating an actively open logfile.
The workflow is:

1. force a logfile rotation on the node
2. query the new current logfile path
3. delete any older collector segments in the log directory

That leaves exactly one fresh collector logfile behind for the next row.
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


def rotate_and_prune_node(node: str) -> str:
    """Rotate the node's collector logfile and delete older segments.

    Returns the newly active logfile path so callers can log what was kept.
    """

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

    prune_cmd = (
        "set -e; "
        "cd /users/JasonHu/pg/data/log; "
        f"find . -maxdepth 1 -type f -name 'postgresql-*.log' ! -name '{current_name}' -delete"
    )
    ssh_cmd(node, prune_cmd)
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
            current_path = rotate_and_prune_node(node)
            print(f"{node}: kept {current_path}")
    except Exception as exc:
        print(f"error: failed to reset logs: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
