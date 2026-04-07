#!/usr/bin/env python3
"""Prepare vanilla PostgreSQL plus background traffic tables for interference runs.

This wraps the existing vanilla pgbench setup so node-0 becomes a plain
primary, node-3 becomes its physical standby, and the standard pgbench tables
exist. After that, it creates a dedicated local sink table used by the
background COPY writer during the interference benchmark.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and raise immediately on failure."""
    try:
        return subprocess.run(args, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - debugging aid
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare vanilla PostgreSQL plus the background sink table for interference benchmarks."
    )
    parser.add_argument("--scale", type=int, default=32)
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--primary-node", default="node-0")
    parser.add_argument("--standby-node", default="node-3")
    parser.add_argument("--primary-host", default="10.10.1.2")
    parser.add_argument("--primary-port", default="5432")
    parser.add_argument("--primary-db", default="postgres")
    parser.add_argument("--pguser", default="JasonHu")
    parser.add_argument("--sink-table", default="bg_sink")
    args = parser.parse_args()

    setup_script = REPO_ROOT / "scripts" / "setup_pgbench_vanilla.py"
    print(
        "running vanilla setup: "
        f"primary={args.primary_node} standby={args.standby_node} db={args.dbname} scale={args.scale}",
        flush=True,
    )
    run_cmd(
        [
            sys.executable,
            str(setup_script),
            "--scale",
            str(args.scale),
            "--dbname",
            args.dbname,
            "--primary-node",
            args.primary_node,
            "--standby-node",
            args.standby_node,
            "--primary-host",
            args.primary_host,
            "--primary-port",
            args.primary_port,
            "--primary-db",
            args.primary_db,
            "--pguser",
            args.pguser,
        ]
    )

    psql_bin = Path.home() / "pg" / "bin" / "psql"
    sink_sql = f"""
CREATE TABLE IF NOT EXISTS {args.sink_table} (
    id bigint,
    payload text
);
TRUNCATE TABLE {args.sink_table};
"""
    print(f"creating background sink table {args.sink_table} in {args.dbname}", flush=True)
    run_cmd(
        [
            str(psql_bin),
            "-v",
            "ON_ERROR_STOP=1",
            "-X",
            "-Atq",
            "-h",
            args.primary_host,
            "-p",
            args.primary_port,
            "-U",
            args.pguser,
            "-d",
            args.dbname,
            "-c",
            sink_sql,
        ]
    )

    print(
        "vanilla interference setup complete: "
        f"db={args.dbname}, sink_table={args.sink_table}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
