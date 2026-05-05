#!/usr/bin/env python3
"""Prepare the Citus cluster for the network-interference benchmark.

The setup restores node-0 back to a Citus coordinator if it was temporarily
repurposed for vanilla PostgreSQL, ensures the standard pgbench tables are
present, and creates per-session distributed background tables for the COPY
reader and writer workloads. The background tables are UNLOGGED by default so
the benchmark focuses on Citus/network traffic instead of background WAL, but a
logged-table mode remains available for comparison runs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import shlex
from pathlib import Path


PRIMARY_NODE = "node-0"
PRIMARY_HOST = "10.10.2.1"
PGUSER = "JasonHu"
PGPORT = "5432"
DBNAME = "postgres"
BACKGROUND_READ_PREFIX_DEFAULT = "bg_read_src"
BACKGROUND_WRITE_PREFIX_DEFAULT = "bg_write_sink"
BACKGROUND_MAX_SESSIONS_DEFAULT = 8
BACKGROUND_TABLE_SHARD_COUNT_DEFAULT = 2
BACKGROUND_READ_ROW_COUNT_DEFAULT = 50000
BACKGROUND_READ_PAYLOAD_BYTES_DEFAULT = 256
PRIVATE_TABLE_PREFIX_DEFAULT = "pgbench_private_accounts"
PRIVATE_CLIENT_COUNT_DEFAULT = 16
CLIENT_PRIVATE_ROWS_DEFAULT = 100000


def run_cmd(
    args: list[str],
    *,
    input_text: str | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and raise immediately on failure."""
    try:
        return subprocess.run(
            args,
            check=True,
            text=True,
            input=input_text,
            stdout=subprocess.PIPE if stdout is None else stdout,
            stderr=subprocess.PIPE if stderr is None else stderr,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - debugging aid
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def ssh_cmd(
    node: str,
    remote_cmd: str,
    *,
    input_text: str | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a remote node via SSH."""
    return run_cmd(
        ["ssh", "-o", "BatchMode=yes", node, remote_cmd],
        input_text=input_text,
        stdout=stdout,
        stderr=stderr,
    )


def remote_psql(
    node: str,
    dbname: str,
    sql: str,
    *,
    input_text: str | None = None,
    stdout=None,
) -> subprocess.CompletedProcess[str]:
    """Run a single psql command on a remote node."""
    psql_bin = Path.home() / "pg" / "bin" / "psql"
    quoted_sql = shlex.quote(sql)
    return ssh_cmd(
        node,
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -Atq -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -d {dbname} -c {quoted_sql}",
        input_text=input_text,
        stdout=stdout,
        stderr=subprocess.PIPE,
    )


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
    result = remote_psql(node, dbname, sql, stdout=subprocess.PIPE)
    return int(result.stdout.strip() or "0")


def fetch_scalar(node: str, dbname: str, sql: str) -> str:
    """Return a single scalar value from a remote psql query."""
    result = remote_psql(node, dbname, sql, stdout=subprocess.PIPE)
    return result.stdout.strip()


def sql_identifier(name: str) -> str:
    """Quote a SQL identifier for direct embedding in generated SQL."""
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value: str) -> str:
    """Quote a SQL string literal for direct embedding in generated SQL."""
    return "'" + value.replace("'", "''") + "'"


def background_table_name(prefix: str, session_id: int, total_sessions: int) -> str:
    """Format a per-session background table name with stable zero padding."""
    width = max(2, len(str(max(total_sessions - 1, 0))))
    return f"{prefix}_{session_id:0{width}d}"


def make_table_rows(session_id: int, row_count: int, payload_bytes: int) -> str:
    """Generate CSV rows for a background read-source table.

    Each session gets its own read table so the benchmark avoids same-relation
    contention while still producing worker-to-coordinator traffic.
    """
    payload = "r" * payload_bytes
    base_id = session_id * 10_000_000_000
    lines = [f"{base_id + i},{payload}" for i in range(row_count)]
    return "\n".join(lines) + "\n"


def create_background_table(
    node: str,
    dbname: str,
    table_name: str,
    *,
    logged: bool,
    shard_count: int,
) -> None:
    """Create one background table and distribute it across the two workers."""
    persistence_sql = "" if logged else "UNLOGGED "
    table_ident = sql_identifier(table_name)
    sql = (
        f"DROP TABLE IF EXISTS {table_ident} CASCADE; "
        f"CREATE {persistence_sql}TABLE {table_ident} (id bigint NOT NULL, payload text NOT NULL); "
        f"SELECT create_distributed_table({sql_literal(table_name)}, 'id', shard_count => {shard_count}, colocate_with => 'none');"
    )
    remote_psql(node, dbname, sql)


def copy_read_source_rows(
    node: str,
    dbname: str,
    table_name: str,
    *,
    session_id: int,
    row_count: int,
    payload_bytes: int,
) -> None:
    """Populate one read-source table exactly once during setup."""
    rows = make_table_rows(session_id, row_count, payload_bytes)
    sql = f"COPY {sql_identifier(table_name)} (id, payload) FROM STDIN WITH (FORMAT csv);"
    remote_psql(node, dbname, sql, input_text=rows, stdout=subprocess.DEVNULL)


def verify_background_table(
    node: str,
    dbname: str,
    table_name: str,
    *,
    expected_logged: bool,
    expected_rows: int | None = None,
) -> None:
    """Fail fast if a background table is missing, not distributed, or empty."""
    exists = fetch_scalar(node, dbname, f"SELECT to_regclass({sql_literal(table_name)}) IS NOT NULL;")
    if exists != "t":
        raise RuntimeError(f"expected background table {table_name} to exist in {dbname}")

    distributed = count_rows(
        node,
        dbname,
        f"SELECT count(*) FROM pg_dist_partition WHERE logicalrelid = {sql_literal(table_name)}::regclass;",
    )
    if distributed != 1:
        raise RuntimeError(f"expected background table {table_name} to be distributed on node-0")

    persistence = fetch_scalar(
        node,
        dbname,
        f"SELECT relpersistence FROM pg_class WHERE oid = {sql_literal(table_name)}::regclass;",
    )
    expected_persistence = "p" if expected_logged else "u"
    if persistence != expected_persistence:
        raise RuntimeError(
            f"expected background table {table_name} to be "
            f"{'logged' if expected_logged else 'unlogged'} but relpersistence was {persistence!r}"
        )

    if expected_rows is not None:
        row_count = count_rows(node, dbname, f"SELECT count(*) FROM {sql_identifier(table_name)};")
        if row_count != expected_rows:
            raise RuntimeError(
                f"expected background read table {table_name} to contain {expected_rows} rows, found {row_count}"
            )


def create_client_private_table(
    node: str,
    dbname: str,
    table_name: str,
    *,
    row_count: int,
    shard_count: int,
) -> None:
    """Create one logged, distributed foreground table for one pgbench client."""
    table_ident = sql_identifier(table_name)
    sql = (
        f"DROP TABLE IF EXISTS {table_ident} CASCADE; "
        f"CREATE TABLE {table_ident} (aid integer PRIMARY KEY, abalance integer NOT NULL DEFAULT 0); "
        f"SELECT create_distributed_table({sql_literal(table_name)}, 'aid', "
        f"shard_count => {shard_count}, colocate_with => 'none'); "
        f"INSERT INTO {table_ident} (aid, abalance) "
        f"SELECT series_id, 0 FROM generate_series(1, {row_count}) AS rows(series_id); "
        f"ANALYZE {table_ident};"
    )
    remote_psql(node, dbname, sql)


def verify_client_private_table(node: str, dbname: str, table_name: str, *, expected_rows: int) -> None:
    """Fail fast if a client-private foreground table is missing or not distributed."""
    exists = fetch_scalar(node, dbname, f"SELECT to_regclass({sql_literal(table_name)}) IS NOT NULL;")
    if exists != "t":
        raise RuntimeError(f"expected client-private table {table_name} to exist in {dbname}")

    distributed = count_rows(
        node,
        dbname,
        f"SELECT count(*) FROM pg_dist_partition WHERE logicalrelid = {sql_literal(table_name)}::regclass;",
    )
    if distributed != 1:
        raise RuntimeError(f"expected client-private table {table_name} to be distributed on node-0")

    row_count = count_rows(node, dbname, f"SELECT count(*) FROM {sql_identifier(table_name)};")
    if row_count != expected_rows:
        raise RuntimeError(
            f"expected client-private table {table_name} to contain {expected_rows} rows, found {row_count}"
        )


def configure_citus_fast_fabric_metadata(dbname: str) -> None:
    """Point Citus node metadata at the 100G 10.10.2.x fabric."""
    sql = """
SELECT citus_set_coordinator_host('10.10.2.1', 5432);
WITH target_nodes(groupid, noderole, nodename) AS (
    VALUES
        (0, 'primary'::noderole, '10.10.2.1'),
        (1, 'primary'::noderole, '10.10.2.2'),
        (1, 'secondary'::noderole, '10.10.2.4'),
        (2, 'primary'::noderole, '10.10.2.3'),
        (2, 'secondary'::noderole, '10.10.2.5')
)
SELECT citus_update_node(pg_dist_node.nodeid, target_nodes.nodename, pg_dist_node.nodeport)
FROM pg_dist_node
JOIN target_nodes USING (groupid, noderole)
WHERE pg_dist_node.nodename <> target_nodes.nodename;
"""
    remote_psql(PRIMARY_NODE, dbname, sql)


def restore_worker1_standby() -> None:
    """Re-clone node-3 from node-1 so the Citus worker standby topology is restored."""
    pg_root = Path.home() / "pg"
    pg_basebackup = pg_root / "bin" / "pg_basebackup"
    pg_ctl_bin = pg_root / "bin" / "pg_ctl"
    data_dir = pg_root / "data"
    psql_bin = pg_root / "bin" / "psql"

    primary_conninfo_value = (
        "host=10.10.2.2 port=5432 user=replicator "
        "application_name=worker1_stby"
    )
    remote_cmd = f"""set -e
{pg_ctl_bin} stop -D {data_dir} -m fast || true
{psql_bin} -v ON_ERROR_STOP=1 -X -h 10.10.2.2 -p 5432 -U {PGUSER} -d postgres -c "SELECT pg_drop_replication_slot('worker1_slot') WHERE EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name = 'worker1_slot');"
rm -rf {data_dir}/*
{pg_basebackup} -h 10.10.2.2 -p 5432 -U replicator -D {data_dir} -R -X stream -C -S worker1_slot
sed -i -E "s|^primary_conninfo = .*|primary_conninfo = '{primary_conninfo_value}'|" {data_dir}/postgresql.auto.conf
sed -i -E "s|^primary_slot_name = .*|primary_slot_name = 'worker1_slot'|" {data_dir}/postgresql.auto.conf
chmod 700 {data_dir}
{pg_ctl_bin} start -D {data_dir} -w -t 20 -l {pg_root}/startup.log
"""
    ssh_cmd("node-3", remote_cmd)


def ensure_replication_role(node: str) -> None:
    """Create the worker standby replication role if it is missing."""
    pg_root = Path.home() / "pg"
    psql_bin = pg_root / "bin" / "psql"
    ssh_cmd(
        node,
        (
            f"{psql_bin} -v ON_ERROR_STOP=1 -X -d postgres "
            "-c \"DO \\$\\$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN "
            "CREATE ROLE replicator WITH REPLICATION LOGIN; "
            "END IF; "
            "END \\$\\$;\""
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Citus for the network-interference benchmark.")
    parser.add_argument("--dbname", default=DBNAME)
    parser.add_argument("--pgbench-scale", type=int, default=32)
    parser.add_argument(
        "--background-max-sessions",
        type=int,
        default=BACKGROUND_MAX_SESSIONS_DEFAULT,
        help="number of per-session background tables to precreate",
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
        "--background-table-shard-count",
        type=int,
        default=BACKGROUND_TABLE_SHARD_COUNT_DEFAULT,
        help="number of shards for each background table",
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
        "--background-tables-logged",
        action="store_true",
        help="create background read/write tables as regular logged tables instead of unlogged tables",
    )
    parser.add_argument("--skip-pgbench-setup", action="store_true", help="skip pgbench table verification / setup")
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

    if args.background_max_sessions < 1:
        raise SystemExit("--background-max-sessions must be at least 1")
    if args.background_table_shard_count < 1:
        raise SystemExit("--background-table-shard-count must be at least 1")
    if args.background_read_row_count < 1:
        raise SystemExit("--background-read-row-count must be at least 1")
    if args.background_read_payload_bytes < 1:
        raise SystemExit("--background-read-payload-bytes must be at least 1")
    if args.private_client_count < 1:
        raise SystemExit("--private-client-count must be at least 1")
    if args.client_private_rows < 1:
        raise SystemExit("--client-private-rows must be at least 1")

    pg_root = Path.home() / "pg"
    psql_bin = pg_root / "bin" / "psql"
    pg_ctl_bin = pg_root / "bin" / "pg_ctl"
    data_dir = pg_root / "data"
    setup_pgbench = Path(__file__).resolve().parent / "setup_pgbench_citus.sh"
    background_logged = args.background_tables_logged
    background_persistence = "logged" if background_logged else "unlogged"

    print("restoring node-0 as a Citus coordinator", flush=True)
    ssh_cmd(
        PRIMARY_NODE,
        f"set -e; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET shared_preload_libraries = 'citus';\"; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET citus.enable_repartition_joins = 'on';\"; "
        f"{psql_bin} -v ON_ERROR_STOP=1 -X -d {DBNAME} -h 127.0.0.1 -p {PGPORT} -U {PGUSER} -c \"ALTER SYSTEM SET synchronous_commit = 'local';\"; "
        f"{pg_ctl_bin} restart -D {data_dir} -m fast -w -t 20 -l {pg_root}/startup.log",
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

    print("clearing stale worker synchronous standby targets", flush=True)
    set_system_setting_remote("node-1", "synchronous_standby_names", "")
    set_system_setting_remote("node-2", "synchronous_standby_names", "")

    print("pointing Citus metadata at the 100G fabric", flush=True)
    configure_citus_fast_fabric_metadata(args.dbname)

    print("ensuring the replication role exists on the worker primaries", flush=True)
    ensure_replication_role("node-1")
    ensure_replication_role("node-2")

    print("restoring node-3 as the worker1 standby", flush=True)
    restore_worker1_standby()

    print("enabling synchronous wait targets on the worker primaries", flush=True)
    set_system_setting_remote("node-1", "synchronous_standby_names", "FIRST 1 (worker1_stby)")
    set_system_setting_remote("node-2", "synchronous_standby_names", "FIRST 1 (worker2_stby)")

    pgbench_count = count_rows(
        PRIMARY_NODE,
        args.dbname,
        "SELECT count(*) FROM pg_dist_partition "
        "WHERE logicalrelid IN ("
        "SELECT to_regclass(table_name)::oid "
        "FROM (VALUES ('pgbench_accounts'), ('pgbench_branches'), "
        "('pgbench_tellers'), ('pgbench_history')) AS names(table_name) "
        "WHERE to_regclass(table_name) IS NOT NULL"
        ");",
    )
    if not args.skip_pgbench_setup and pgbench_count != 4:
        print(f"ensuring pgbench tables exist at scale {args.pgbench_scale}", flush=True)
        run_cmd(["bash", str(setup_pgbench), str(args.pgbench_scale), args.dbname])
    elif pgbench_count == 4:
        print("pgbench tables already present; skipping reinitialization", flush=True)
    else:
        print("skipping pgbench table setup by request", flush=True)

    if not args.skip_private_table_setup:
        print(
            f"creating {args.private_client_count} client-private foreground tables "
            f"with {args.client_private_rows} rows each",
            flush=True,
        )
        for client_id in range(args.private_client_count):
            table_name = f"{args.private_table_prefix}_{client_id:02d}"
            create_client_private_table(
                PRIMARY_NODE,
                args.dbname,
                table_name,
                row_count=args.client_private_rows,
                shard_count=args.background_table_shard_count,
            )
            verify_client_private_table(
                PRIMARY_NODE,
                args.dbname,
                table_name,
                expected_rows=args.client_private_rows,
            )
    else:
        print("skipping client-private foreground table setup by request", flush=True)

    print(
        f"creating per-session {background_persistence} background tables "
        f"with {args.background_table_shard_count} shards each",
        flush=True,
    )
    # Keep the background read/write traffic isolated by giving each session
    # its own source and sink tables rather than sharing a hot relation.
    for session_id in range(args.background_max_sessions):
        read_table = background_table_name(args.background_read_prefix, session_id, args.background_max_sessions)
        write_table = background_table_name(args.background_write_prefix, session_id, args.background_max_sessions)

        create_background_table(
            PRIMARY_NODE,
            args.dbname,
            read_table,
            logged=background_logged,
            shard_count=args.background_table_shard_count,
        )
        create_background_table(
            PRIMARY_NODE,
            args.dbname,
            write_table,
            logged=background_logged,
            shard_count=args.background_table_shard_count,
        )
        copy_read_source_rows(
            PRIMARY_NODE,
            args.dbname,
            read_table,
            session_id=session_id,
            row_count=args.background_read_row_count,
            payload_bytes=args.background_read_payload_bytes,
        )

        verify_background_table(
            PRIMARY_NODE,
            args.dbname,
            read_table,
            expected_logged=background_logged,
            expected_rows=args.background_read_row_count,
        )
        verify_background_table(
            PRIMARY_NODE,
            args.dbname,
            write_table,
            expected_logged=background_logged,
            expected_rows=0,
        )

    print("Citus network-interference setup complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
