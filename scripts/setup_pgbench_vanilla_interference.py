#!/usr/bin/env python3
"""Prepare vanilla PostgreSQL plus background traffic tables for interference runs.

This wraps the existing vanilla pgbench setup so node-0 becomes a plain
primary, node-3 becomes its physical standby, and the standard pgbench tables
exist. After that, it creates per-session background read-source and write-sink
tables for the interference benchmark.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TABLE_PREFIX_DEFAULT = "pgbench_private_accounts"
PRIVATE_CLIENT_COUNT_DEFAULT = 16
CLIENT_PRIVATE_ROWS_DEFAULT = 100000
BACKGROUND_READ_PREFIX_DEFAULT = "bg_read_src"
BACKGROUND_WRITE_PREFIX_DEFAULT = "bg_write_sink"
BACKGROUND_MAX_SESSIONS_DEFAULT = 8
BACKGROUND_READ_ROW_COUNT_DEFAULT = 50000
BACKGROUND_READ_PAYLOAD_BYTES_DEFAULT = 256


def run_cmd(args: list[str], *, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and raise immediately on failure."""
    try:
        return subprocess.run(args, check=True, text=True, input=input, capture_output=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - debugging aid
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def sql_identifier(name: str) -> str:
    """Quote a SQL identifier for direct embedding in generated SQL."""
    return '"' + name.replace('"', '""') + '"'


def background_table_name(prefix: str, session_id: int, total_sessions: int) -> str:
    """Format a per-session background table name with stable zero padding."""
    width = max(2, len(str(max(total_sessions - 1, 0))))
    return f"{prefix}_{session_id:0{width}d}"


def make_background_rows(session_id: int, row_count: int, payload_bytes: int) -> str:
    """Generate CSV rows for one background read-source table."""
    payload = "r" * payload_bytes
    base_id = session_id * 10_000_000_000
    lines = [f"{base_id + row_id},{payload}" for row_id in range(row_count)]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare vanilla PostgreSQL plus the background sink table for interference benchmarks."
    )
    parser.add_argument("--scale", type=int, default=32)
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--primary-node", default="node-0")
    parser.add_argument("--standby-node", default="node-3")
    parser.add_argument("--primary-host", default="10.10.2.1")
    parser.add_argument("--primary-port", default="5432")
    parser.add_argument("--primary-db", default="postgres")
    parser.add_argument("--pguser", default="JasonHu")
    parser.add_argument(
        "--background-max-sessions",
        type=int,
        default=BACKGROUND_MAX_SESSIONS_DEFAULT,
        help="number of per-session background table pairs to create",
    )
    parser.add_argument(
        "--background-read-prefix",
        default=BACKGROUND_READ_PREFIX_DEFAULT,
        help="prefix for per-session background read-source tables",
    )
    parser.add_argument(
        "--background-write-prefix",
        default=BACKGROUND_WRITE_PREFIX_DEFAULT,
        help="prefix for per-session background write-sink tables",
    )
    parser.add_argument(
        "--background-read-row-count",
        type=int,
        default=BACKGROUND_READ_ROW_COUNT_DEFAULT,
        help="rows to populate in each background read-source table",
    )
    parser.add_argument(
        "--background-read-payload-bytes",
        type=int,
        default=BACKGROUND_READ_PAYLOAD_BYTES_DEFAULT,
        help="payload width in bytes for each background read-source row",
    )
    parser.add_argument(
        "--private-table-prefix",
        default=PRIVATE_TABLE_PREFIX_DEFAULT,
        help="prefix for client-private foreground account tables",
    )
    parser.add_argument(
        "--private-client-count",
        type=int,
        default=PRIVATE_CLIENT_COUNT_DEFAULT,
        help="number of client-private foreground account tables to create",
    )
    parser.add_argument(
        "--client-private-rows",
        type=int,
        default=CLIENT_PRIVATE_ROWS_DEFAULT,
        help="rows in each client-private foreground account table",
    )
    parser.add_argument(
        "--skip-private-table-setup",
        action="store_true",
        help="skip creating/verifying client-private foreground tables",
    )
    args = parser.parse_args()

    if args.private_client_count < 1:
        raise SystemExit("--private-client-count must be at least 1")
    if args.client_private_rows < 1:
        raise SystemExit("--client-private-rows must be at least 1")
    if args.background_max_sessions < 1:
        raise SystemExit("--background-max-sessions must be at least 1")
    if args.background_read_row_count < 1:
        raise SystemExit("--background-read-row-count must be at least 1")
    if args.background_read_payload_bytes < 1:
        raise SystemExit("--background-read-payload-bytes must be at least 1")

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
    print(
        f"creating {args.background_max_sessions} private background table pairs "
        f"with {args.background_read_row_count} source rows each",
        flush=True,
    )
    for session_id in range(args.background_max_sessions):
        read_table = background_table_name(args.background_read_prefix, session_id, args.background_max_sessions)
        write_table = background_table_name(args.background_write_prefix, session_id, args.background_max_sessions)
        read_ident = sql_identifier(read_table)
        write_ident = sql_identifier(write_table)
        table_sql = f"""
DROP TABLE IF EXISTS {read_ident};
DROP TABLE IF EXISTS {write_ident};
CREATE UNLOGGED TABLE {read_ident} (
    id bigint NOT NULL,
    payload text NOT NULL
);
CREATE UNLOGGED TABLE {write_ident} (
    id bigint,
    payload text
);
"""
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
                table_sql,
            ]
        )
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
                f"COPY {read_ident} (id, payload) FROM STDIN WITH (FORMAT csv);",
            ],
            input=make_background_rows(
                session_id,
                args.background_read_row_count,
                args.background_read_payload_bytes,
            ),
        )
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
                f"ANALYZE {read_ident}; ANALYZE {write_ident};",
            ]
        )

    if not args.skip_private_table_setup:
        private_sql = []
        for client_id in range(args.private_client_count):
            table_name = f"{args.private_table_prefix}_{client_id:02d}"
            table_ident = sql_identifier(table_name)
            private_sql.append(
                f"""
DROP TABLE IF EXISTS {table_ident};
CREATE TABLE {table_ident} (
    aid integer PRIMARY KEY,
    abalance integer NOT NULL DEFAULT 0
);
INSERT INTO {table_ident} (aid, abalance)
SELECT series_id, 0
FROM generate_series(1, {args.client_private_rows}) AS rows(series_id);
ANALYZE {table_ident};
"""
            )

        print(
            f"creating {args.private_client_count} client-private foreground tables "
            f"with {args.client_private_rows} rows each",
            flush=True,
        )
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
                "\n".join(private_sql),
            ]
        )
    else:
        print("skipping client-private foreground table setup by request", flush=True)

    print(
        "vanilla interference setup complete: "
        f"db={args.dbname}, background_tables={args.background_max_sessions}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
