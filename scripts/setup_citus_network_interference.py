#!/usr/bin/env python3
"""Prepare the Citus cluster for the network-interference benchmark.

The setup restores node-0 back to a Citus coordinator if it was temporarily
repurposed for vanilla PostgreSQL, ensures the standard pgbench tables are
present, and creates a dedicated distributed sink table used by the background
COPY writer workload.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PRIMARY_NODE = "node-0"
PRIMARY_HOST = "10.10.1.2"
PGUSER = "JasonHu"
PGPORT = "5432"
DBNAME = "postgres"


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


def set_system_setting_remote(node: str, setting: str, value: str) -> None:
    """Set a runtime GUC on a remote node and reload that server."""
    psql_bin = Path.home() / "pg" / "bin" / "psql"
    pg_ctl_bin = Path.home() / "pg" / "bin" / "pg_ctl"
    remote_cmd = (
        f"set -e; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d postgres -c \"ALTER SYSTEM SET {setting} = '{value}';\"; "
        f"{pg_ctl_bin} reload -D $HOME/pg/data"
    )
    ssh_cmd(node, remote_cmd)


def count_rows(node: str, dbname: str, sql: str) -> int:
    """Return a scalar count from a remote psql query."""
    psql_bin = Path.home() / "pg" / "bin" / "psql"
    result = ssh_cmd(
        node,
        f"{psql_bin} -Atq -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -d {dbname} -c \"{sql}\"",
    )
    return int(result.stdout.strip() or "0")


def restore_worker1_standby() -> None:
    """Re-clone node-3 from node-1 so the Citus worker standby topology is restored."""
    pg_root = Path.home() / "pg"
    pg_basebackup = pg_root / "bin" / "pg_basebackup"
    pg_ctl_bin = pg_root / "bin" / "pg_ctl"
    data_dir = pg_root / "data"
    psql_bin = pg_root / "bin" / "psql"

    ssh_cmd(
        "node-1",
        (
            f"{psql_bin} -v ON_ERROR_STOP=1 -X -d postgres "
            "-c \"DO $$ BEGIN "
            "IF EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = 'worker1_slot') THEN "
            "PERFORM pg_drop_replication_slot('worker1_slot'); "
            "END IF; "
            "END $$;\""
        ),
    )

    primary_conninfo_value = (
        "host=10.10.1.3 port=5432 user=replicator "
        "application_name=worker1_stby"
    )
    remote_cmd = f"""set -e
{pg_ctl_bin} stop -D {data_dir} -m fast || true
rm -rf {data_dir}/*
{pg_basebackup} -h 10.10.1.3 -p 5432 -U replicator -D {data_dir} -R -X stream -C -S worker1_slot
sed -i -E "s|^primary_conninfo = .*|primary_conninfo = '{primary_conninfo_value}'|" {data_dir}/postgresql.auto.conf
sed -i -E "s|^primary_slot_name = .*|primary_slot_name = 'worker1_slot'|" {data_dir}/postgresql.auto.conf
{pg_ctl_bin} start -D {data_dir}
"""
    ssh_cmd("node-3", remote_cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Citus for the network-interference benchmark.")
    parser.add_argument("--dbname", default=DBNAME)
    parser.add_argument("--pgbench-scale", type=int, default=32)
    parser.add_argument("--sink-table", default="bg_sink")
    parser.add_argument("--skip-pgbench-setup", action="store_true", help="skip pgbench table verification / setup")
    args = parser.parse_args()

    pg_root = Path.home() / "pg"
    psql_bin = pg_root / "bin" / "psql"
    pg_ctl_bin = pg_root / "bin" / "pg_ctl"
    data_dir = pg_root / "data"
    setup_pgbench = Path(__file__).resolve().parent / "setup_pgbench_citus.sh"

    print("restoring node-0 as a Citus coordinator", flush=True)
    ssh_cmd(
        PRIMARY_NODE,
        f"set -e; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET shared_preload_libraries = 'citus';\"; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET citus.enable_repartition_joins = 'on';\"; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET synchronous_commit = 'local';\"; "
        f"{pg_ctl_bin} restart -D {data_dir} -m fast",
    )

    print("verifying that Citus loads on node-0", flush=True)
    verify = ssh_cmd(
        PRIMARY_NODE,
        f"{psql_bin} -Atq -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -d {DBNAME} -c \"SHOW shared_preload_libraries;\"",
    )
    if "citus" not in verify.stdout:
        raise RuntimeError("node-0 did not come back with citus in shared_preload_libraries")

    print("ensuring the citus extension is available in postgres", flush=True)
    ssh_cmd(
        PRIMARY_NODE,
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -d {args.dbname} -c \"CREATE EXTENSION IF NOT EXISTS citus;\"",
    )

    print("restoring node-3 as the worker1 standby", flush=True)
    restore_worker1_standby()

    print("enabling synchronous wait targets on the worker primaries", flush=True)
    set_system_setting_remote("node-1", "synchronous_standby_names", "FIRST 1 (worker1_stby)")
    set_system_setting_remote("node-2", "synchronous_standby_names", "FIRST 1 (worker2_stby)")

    pgbench_count = count_rows(
        PRIMARY_NODE,
        args.dbname,
        "SELECT count(*) FROM pg_dist_partition "
        "WHERE logicalrelid IN ('pgbench_accounts'::regclass, 'pgbench_branches'::regclass, "
        "'pgbench_tellers'::regclass, 'pgbench_history'::regclass);",
    )
    if not args.skip_pgbench_setup and pgbench_count != 4:
        print(f"ensuring pgbench tables exist at scale {args.pgbench_scale}", flush=True)
        run_cmd([str(setup_pgbench), str(args.pgbench_scale), args.dbname])
    elif pgbench_count == 4:
        print("pgbench tables already present; skipping reinitialization", flush=True)
    else:
        print("skipping pgbench table setup by request", flush=True)

    print(f"recreating distributed sink table {args.sink_table}", flush=True)
    sink_sql = (
        f"DROP TABLE IF EXISTS {args.sink_table} CASCADE; "
        f"CREATE TABLE {args.sink_table} (id bigint NOT NULL, payload text NOT NULL); "
        f"SELECT create_distributed_table('{args.sink_table}', 'id');"
    )
    ssh_cmd(
        PRIMARY_NODE,
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -d {args.dbname} -c \"{sink_sql}\"",
    )

    print("checking distributed-table metadata", flush=True)
    sink_count = count_rows(
        PRIMARY_NODE,
        args.dbname,
        f"SELECT count(*) FROM pg_dist_partition WHERE logicalrelid = '{args.sink_table}'::regclass;",
    )
    if sink_count != 1:
        raise RuntimeError(f"expected {args.sink_table} to be distributed on node-0")

    print("Citus network-interference setup complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
