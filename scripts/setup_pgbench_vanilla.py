#!/usr/bin/env python3
"""Prepare a vanilla PostgreSQL pgbench benchmark on node-0/node-3.

This script temporarily removes Citus from the primary and standby preload
configuration, reclones node-3 from node-0 as a physical standby, and then
initializes a fresh standard pgbench database on node-0.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


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


def ssh_cmd(node: str, remote_cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a remote node via SSH."""
    return run_cmd(["ssh", "-o", "BatchMode=yes", node, remote_cmd])


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare vanilla pgbench on node-0/node-3.")
    parser.add_argument("--scale", type=int, default=32)
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--primary-node", default="node-0")
    parser.add_argument("--standby-node", default="node-3")
    parser.add_argument("--primary-host", default="10.10.2.1")
    parser.add_argument("--primary-port", default="5432")
    parser.add_argument("--primary-db", default="postgres")
    parser.add_argument("--pguser", default="JasonHu")
    parser.add_argument("--repl-user", default="vanilla_repl")
    parser.add_argument("--repl-password", default="vanilla_repl_pw")
    parser.add_argument("--slot-name", default="vanilla_slot")
    parser.add_argument("--standby-name", default="vanilla_stby")
    args = parser.parse_args()

    pgbench_bin = Path.home() / "pg" / "bin" / "pgbench"
    psql_bin = Path.home() / "pg" / "bin" / "psql"
    createdb_bin = Path.home() / "pg" / "bin" / "createdb"
    dropdb_bin = Path.home() / "pg" / "bin" / "dropdb"
    pg_ctl_bin = Path.home() / "pg" / "bin" / "pg_ctl"
    data_dir = Path.home() / "pg" / "data"

    # Reconfigure the primary first. The standby is recloned from the primary
    # after this, so the primary's config becomes the source of truth.
    primary_setup = f"""set -e
sed -i -E "s/^shared_preload_libraries *= *'citus'/# shared_preload_libraries='citus'  # disabled for vanilla benchmark/" {data_dir}/postgresql.conf
{psql_bin} -v ON_ERROR_STOP=1 -X -d {args.primary_db} \
  -c "ALTER SYSTEM RESET shared_preload_libraries;" \
  -c "ALTER SYSTEM SET wal_level = 'replica';" \
  -c "ALTER SYSTEM SET max_wal_senders = '10';" \
  -c "ALTER SYSTEM SET max_replication_slots = '10';" \
  -c "ALTER SYSTEM SET wal_keep_size = '1GB';" \
  -c "ALTER SYSTEM RESET synchronous_standby_names;" \
  -c "ALTER SYSTEM SET synchronous_commit = 'local';"
{psql_bin} -v ON_ERROR_STOP=1 -X -d {args.primary_db} <<'SQL'
DROP ROLE IF EXISTS {args.repl_user};
CREATE ROLE {args.repl_user} WITH REPLICATION LOGIN PASSWORD '{args.repl_password}';
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = '{args.slot_name}') THEN
    PERFORM pg_drop_replication_slot('{args.slot_name}');
  END IF;
END
$$;
SQL
{pg_ctl_bin} restart -D {data_dir} -m fast -w -t 30 -l {Path.home() / 'pg' / 'startup.log'}
"""
    print(f"configuring primary {args.primary_node}")
    ssh_cmd(args.primary_node, primary_setup)

    standby_conf = data_dir / "postgresql.auto.conf"
    primary_conninfo_value = (
        f"host={args.primary_host} port={args.primary_port} "
        f"user={args.repl_user} password={args.repl_password} "
        f"application_name={args.standby_name}"
    )
    standby_setup = f"""set -e
{pg_ctl_bin} stop -D {data_dir} -m fast || true
rm -rf {data_dir}/*
PGPASSWORD='{args.repl_password}' {Path.home() / 'pg' / 'bin' / 'pg_basebackup'} \
  -h {args.primary_host} \
  -p {args.primary_port} \
  -U {args.repl_user} \
  -D {data_dir} \
  -R \
  -X stream \
  -C \
  -S {args.slot_name}
sed -i -E "s|^primary_conninfo = .*|primary_conninfo = '{primary_conninfo_value}'|" "{standby_conf}"
sed -i -E "s|^primary_slot_name = .*|primary_slot_name = '{args.slot_name}'|" "{standby_conf}"
{pg_ctl_bin} start -D {data_dir} -w -t 30 -l {Path.home() / 'pg' / 'startup.log'}
"""
    print(f"recloning standby {args.standby_node}")
    ssh_cmd(args.standby_node, standby_setup)

    sync_setup = f"""set -e
{psql_bin} -v ON_ERROR_STOP=1 -X -d {args.primary_db} \
  -c "ALTER SYSTEM SET synchronous_standby_names = 'FIRST 1 ({args.standby_name})';" \
  -c "ALTER SYSTEM SET synchronous_commit = 'on';"
{pg_ctl_bin} reload -D {data_dir}
"""
    print("enabling vanilla synchronous standby")
    ssh_cmd(args.primary_node, sync_setup)

    init_db = f"""set -e
{dropdb_bin} --if-exists -U {args.pguser} -h 127.0.0.1 -p {args.primary_port} {args.dbname}
{createdb_bin} -U {args.pguser} -h 127.0.0.1 -p {args.primary_port} {args.dbname}
{pgbench_bin} -i -s {args.scale} -U {args.pguser} -h 127.0.0.1 -p {args.primary_port} {args.dbname}
"""
    print(f"initializing pgbench database {args.dbname}")
    ssh_cmd(args.primary_node, init_db)

    verify = f"""{psql_bin} -Atq -h 127.0.0.1 -p {args.primary_port} -U {args.pguser} -d {args.primary_db} \
  -c "SELECT application_name, state FROM pg_stat_replication;"
"""
    result = ssh_cmd(args.primary_node, verify)
    print(result.stdout.strip())
    print(f"vanilla pgbench setup complete for {args.dbname} (scale {args.scale})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
