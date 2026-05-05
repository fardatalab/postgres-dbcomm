#!/usr/bin/env python3
"""Run vanilla PostgreSQL pgbench while background traffic is active.

The foreground workload is the same synchronized-commit pgbench sweep used by
the vanilla comparison run, but now we add long-running background reader and
writer loops from node-1 so the primary experiences concurrent network load
while node-3 continues replaying WAL as a standby.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_HELPER = REPO_ROOT / "scripts" / "pgbench_report.py"
ARCHIVE_SERVER_LOGS = REPO_ROOT / "scripts" / "archive_server_logs.py"
RESET_SERVER_LOGS = REPO_ROOT / "scripts" / "reset_server_logs.py"
GENERATOR_NODE = "node-1"
PRIMARY_NODE = "node-0"
STANDBY_NODE = "node-3"
PRIMARY_HOST = "10.10.2.1"
PRIMARY_PORT = "5432"
PRIMARY_APPNAME = "vanilla_stby"
SERVER_LOG_NODES = ("node-0", "node-3")
PRIVATE_TABLE_PREFIX_DEFAULT = "pgbench_private_accounts"
BACKGROUND_READ_PREFIX_DEFAULT = "bg_read_src"
BACKGROUND_WRITE_PREFIX_DEFAULT = "bg_write_sink"
BACKGROUND_MAX_SESSIONS_DEFAULT = 8


def load_report_helper():
    """Load the shared pgbench parsing helpers from the report script."""
    spec = importlib.util.spec_from_file_location("pgbench_report", REPORT_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load report helper from {REPORT_HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPORT = load_report_helper()


def run_cmd(
    args: list[str],
    *,
    input_text: str | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and raise immediately on failure."""
    return subprocess.run(
        args,
        check=True,
        text=True,
        input=input_text,
        stdout=stdout,
        stderr=stderr,
    )


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


def set_system_setting_local(setting: str, value: str) -> None:
    """Set a runtime GUC on the local primary and reload."""
    sql = f"ALTER SYSTEM SET {setting} = '{value}';"
    run_cmd(
        [
            str(Path.home() / "pg" / "bin" / "psql"),
            "-v",
            "ON_ERROR_STOP=1",
            "-X",
            "-d",
            "postgres",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-c",
            sql,
        ]
    )
    run_cmd([str(Path.home() / "pg" / "bin" / "pg_ctl"), "reload", "-D", str(Path.home() / "pg" / "data")])


def set_system_setting_remote(node: str, setting: str, value: str) -> None:
    """Set a runtime GUC on a remote node and reload that server."""
    remote_cmd = (
        f"set -e; "
        f"$HOME/pg/bin/psql -v ON_ERROR_STOP=1 -X -d postgres -c \"ALTER SYSTEM SET {setting} = '{value}';\"; "
        f"$HOME/pg/bin/pg_ctl reload -D $HOME/pg/data"
    )
    ssh_cmd(node, remote_cmd)


def reset_server_logs() -> None:
    """Rotate and prune the collector logs before the next benchmark row."""
    run_cmd(
        [
            sys.executable,
            str(RESET_SERVER_LOGS),
            "--nodes",
            *SERVER_LOG_NODES,
        ]
    )


def archive_server_logs(run_dir: Path) -> None:
    """Copy the full row-local collector snapshot from each node.

    Even one benchmark row can span multiple collector segments, so preserving
    all row-local segments is the only safe way to keep early traced
    transactions available for later matching.
    """
    run_cmd(
        [
            sys.executable,
            str(ARCHIVE_SERVER_LOGS),
            "--output-dir",
            str(run_dir / "server-logs"),
            "--nodes",
            *SERVER_LOG_NODES,
        ]
    )


def prune_inactive_server_logs(nodes: tuple[str, ...]) -> None:
    """Remove old collector logs after a row when archival is disabled."""
    remote_cmd = (
        "set -e; "
        "logdir=$HOME/pg/data/log; "
        "cd \"$logdir\"; "
        "current=$(ls -1t postgresql-*.log 2>/dev/null | head -n 1 || true); "
        "if [ -n \"$current\" ]; then "
        "find \"$logdir\" -type f -name 'postgresql-*.log' ! -name \"$current\" -delete; "
        "fi"
    )
    for node in nodes:
        ssh_cmd(node, remote_cmd)


def emit_row_log_marker(
    *, mode: str, level: int, clients: int, repeat: int, phase: str, nodes: tuple[str, ...]
) -> None:
    """Emit one explicit LOG line on each node around one interference row.

    The start marker forces the fresh collector segment to exist before the row
    begins so the first traced transaction cannot start before the logfile
    boundary. The end marker leaves a terminal collector entry before archival.
    """

    message = (
        f"pgbench row marker phase={phase} mode={mode} "
        f"background_level={level} clients={clients} repeat={repeat}"
    )
    sql = f"DO $$ BEGIN RAISE LOG {message!r}; END $$;"
    remote_cmd = (
        "export PATH=$HOME/pg/bin:$PATH; "
        f"psql -v ON_ERROR_STOP=1 -X -d postgres -c {shlex.quote(sql)}"
    )
    for node in nodes:
        ssh_cmd(node, remote_cmd)


def finalize_server_logs_for_archive(nodes: tuple[str, ...]) -> None:
    """Force collector-visible row boundaries before archiving.

    The end marker gives parsing a logical fence, but the collector may still
    be draining the active segment when we copy the logs immediately
    afterwards. Rotating here makes the row-local contents durable first.
    """

    remote_cmd = (
        "export PATH=$HOME/pg/bin:$PATH; "
        "psql -v ON_ERROR_STOP=1 -X -d postgres -c 'SELECT pg_rotate_logfile();'"
    )
    for node in nodes:
        ssh_cmd(node, remote_cmd)

    time.sleep(0.5)


def configure_synchronous_standby() -> None:
    """Make the primary wait for the physical standby when needed."""
    set_system_setting_local("synchronous_standby_names", f"FIRST 1 ({PRIMARY_APPNAME})")


def configure_sync_commit(mode: str) -> None:
    """Apply the requested synchronous_commit mode to the primary and standby."""
    set_system_setting_local("synchronous_commit", mode)
    set_system_setting_remote(STANDBY_NODE, "synchronous_commit", mode)


def restore_defaults() -> None:
    """Put the vanilla pair back into the normal synchronous committed mode."""
    try:
        set_system_setting_local("synchronous_commit", "on")
        set_system_setting_remote(STANDBY_NODE, "synchronous_commit", "on")
        configure_synchronous_standby()
    except Exception as exc:  # pragma: no cover - cleanup path
        print(f"warning: could not restore synchronous_commit to on: {exc}", file=sys.stderr)


def collect_metrics(
    summary_paths: list[Path],
    log_prefixes: list[Path],
    sampling_rate: float,
) -> dict[str, float | int]:
    """Parse one or more pgbench summaries and logs into one metrics dict."""
    summaries = [
        REPORT.parse_summary(path.read_text(encoding="utf-8", errors="replace"))
        for path in summary_paths
    ]

    log_paths: list[Path] = []
    for log_prefix in log_prefixes:
        log_paths.extend(sorted(log_prefix.parent.glob(f"{log_prefix.name}*")))
    times_us, non_numeric, total_lines = REPORT.load_log_times(log_paths)
    if not times_us:
        prefixes = ", ".join(str(prefix) for prefix in log_prefixes)
        raise RuntimeError(f"no transaction samples were found for {prefixes}")

    processed_values = [summary.processed for summary in summaries if summary.processed is not None]
    failed_values = [summary.failed for summary in summaries if summary.failed is not None]
    tps_values = [summary.tps for summary in summaries if summary.tps is not None]
    latency_inputs = [
        (summary.processed, summary.latency_avg_ms)
        for summary in summaries
        if summary.processed is not None and summary.latency_avg_ms is not None
    ]
    stddev_inputs = [
        (summary.processed, summary.latency_stddev_ms)
        for summary in summaries
        if summary.processed is not None and summary.latency_stddev_ms is not None
    ]
    latency_weight = sum(processed for processed, _ in latency_inputs)
    stddev_weight = sum(processed for processed, _ in stddev_inputs)

    metrics: dict[str, float | int] = {
        "transactions_processed": sum(processed_values) if processed_values else -1,
        "failed_transactions": sum(failed_values) if failed_values else -1,
        "latency_avg_ms": (
            sum(processed * latency for processed, latency in latency_inputs) / latency_weight
            if latency_weight
            else -1.0
        ),
        "latency_stddev_ms": (
            sum(processed * stddev for processed, stddev in stddev_inputs) / stddev_weight
            if stddev_weight
            else -1.0
        ),
        "throughput_tps": sum(tps_values) if tps_values else -1.0,
        "sampled_transactions": len(times_us),
        "total_log_lines": total_lines,
        "non_numeric_log_entries": non_numeric,
        "p50_ms": REPORT.percentile_nearest_rank(times_us, 50.0) / 1000.0,
        "p95_ms": REPORT.percentile_nearest_rank(times_us, 95.0) / 1000.0,
        "p99_ms": REPORT.percentile_nearest_rank(times_us, 99.0) / 1000.0,
        "min_ms": min(times_us) / 1000.0,
        "max_ms": max(times_us) / 1000.0,
    }
    if sampling_rate > 0:
        metrics["estimated_total_transactions"] = round(len(times_us) / sampling_rate)
    else:
        metrics["estimated_total_transactions"] = -1
    return metrics


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


def background_table_names(prefix: str, total_sessions: int) -> list[str]:
    """Return all background table names for a prefix."""
    return [background_table_name(prefix, session_id, total_sessions) for session_id in range(total_sessions)]


def write_client_private_script(script_path: Path, table_name: str) -> None:
    """Write a pgbench script that only touches one client's private table."""
    quoted_table = sql_identifier(table_name)
    script_path.write_text(
        "\n".join(
            [
                "\\set aid random(1, :client_private_rows)",
                "\\set delta random(-5000, 5000)",
                "BEGIN;",
                f"UPDATE {quoted_table} SET abalance = abalance + :delta WHERE aid = :aid;",
                f"SELECT abalance FROM {quoted_table} WHERE aid = :aid;",
                "END;",
                "",
            ]
        ),
        encoding="utf-8",
    )


def workload_run_dir(
    base_dir: Path,
    workload: str,
    mode: str,
    clients: int,
    level: int,
    repeat: int,
) -> Path:
    """Return a stable per-row result directory."""
    return base_dir / workload / mode / f"c{clients:02d}" / f"b{level:02d}" / f"r{repeat:02d}"


def run_pgbench(
    *,
    mode: str,
    clients: int,
    worker_threads: int,
    duration_s: int,
    dbname: str,
    sample_rate: float,
    workload: str,
    private_table_prefix: str,
    client_private_rows: int,
    run_dir: Path,
) -> dict[str, float | int]:
    """Execute one pgbench run and return the parsed metrics."""
    run_dir.mkdir(parents=True, exist_ok=True)
    pgbench_bin = Path.home() / "pg" / "bin" / "pgbench"
    summary_paths: list[Path] = []
    log_prefixes: list[Path] = []

    if workload == "tpcb":
        summary_path = run_dir / "pgbench.out"
        log_prefix = run_dir / "pgbench_log"
        threads = min(worker_threads, clients)
        cmd = [
            str(pgbench_bin),
            "-U",
            "JasonHu",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-c",
            str(clients),
            "-j",
            str(threads),
            "-M",
            "prepared",
            "-T",
            str(duration_s),
            "-l",
            "--log-prefix",
            str(log_prefix),
        ]
        if sample_rate != 1.0:
            cmd.extend(["--sampling-rate", str(sample_rate)])
        cmd.append(dbname)

        print(
            f"running workload={workload} mode={mode} clients={clients} "
            f"threads={threads} duration={duration_s}s",
            flush=True,
        )
        with summary_path.open("w", encoding="utf-8") as summary_file:
            subprocess.run(
                cmd,
                stdout=summary_file,
                stderr=subprocess.STDOUT,
                text=True,
                check=True,
            )
        summary_paths.append(summary_path)
        log_prefixes.append(log_prefix)
    elif workload == "client-private-table":
        threads = clients
        processes: list[tuple[int, subprocess.Popen[str], object]] = []
        print(
            f"running workload={workload} mode={mode} clients={clients} "
            f"processes={clients} duration={duration_s}s",
            flush=True,
        )
        for client_id in range(clients):
            table_name = f"{private_table_prefix}_{client_id:02d}"
            script_path = run_dir / f"client_{client_id:02d}.sql"
            summary_path = run_dir / f"pgbench_client_{client_id:02d}.out"
            log_prefix = run_dir / f"pgbench_log_client_{client_id:02d}"
            write_client_private_script(script_path, table_name)
            cmd = [
                str(pgbench_bin),
                "-U",
                "JasonHu",
                "-h",
                PRIMARY_HOST,
                "-p",
                PRIMARY_PORT,
                "-c",
                "1",
                "-j",
                "1",
                "-M",
                "prepared",
                "-T",
                str(duration_s),
                "-D",
                f"client_private_rows={client_private_rows}",
                "-f",
                str(script_path),
                "-l",
                "--log-prefix",
                str(log_prefix),
            ]
            if sample_rate != 1.0:
                cmd.extend(["--sampling-rate", str(sample_rate)])
            cmd.append(dbname)
            summary_file = summary_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                cmd,
                stdout=summary_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            processes.append((client_id, process, summary_file))
            summary_paths.append(summary_path)
            log_prefixes.append(log_prefix)

        failures: list[str] = []
        for client_id, process, summary_file in processes:
            return_code = process.wait()
            summary_file.close()
            if return_code != 0:
                failures.append(f"client {client_id} exited with status {return_code}")
        if failures:
            raise RuntimeError("; ".join(failures))
    else:
        raise ValueError(f"unsupported workload: {workload}")

    metrics = collect_metrics(summary_paths, log_prefixes, sample_rate)
    metrics["mode"] = mode
    metrics["workload"] = workload
    metrics["clients"] = clients
    metrics["threads"] = threads
    metrics["duration_s"] = duration_s
    metrics["sampling_rate"] = sample_rate
    metrics["server_logs_dir"] = str(run_dir / "server-logs")

    print(
        "  done: "
        f"tps={metrics['throughput_tps']:.3f} "
        f"p50={metrics['p50_ms']:.3f}ms "
        f"p99={metrics['p99_ms']:.3f}ms",
        flush=True,
    )

    return metrics


def ensure_setup_ready(
    dbname: str,
    *,
    background_read_prefix: str,
    background_write_prefix: str,
    background_max_sessions: int,
) -> None:
    """Verify the vanilla pgbench, background tables, and standby are ready."""
    tables_sql = (
        "SELECT count(*) FROM pg_class "
        "WHERE relkind = 'r' "
        "AND relnamespace = 'public'::regnamespace "
        "AND relname IN ('pgbench_accounts', 'pgbench_branches', 'pgbench_tellers', 'pgbench_history');"
    )
    result = run_cmd(
        [
            str(Path.home() / "pg" / "bin" / "psql"),
            "-Atq",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-U",
            "JasonHu",
            "-d",
            dbname,
            "-c",
            tables_sql,
        ],
        stdout=subprocess.PIPE,
    )
    count = int(result.stdout.strip() or "0")
    if count != 4:
        raise RuntimeError(
            f"expected 4 vanilla pgbench tables in {dbname}, found {count}. "
            "Run scripts/setup_pgbench_vanilla_interference.py first."
        )

    for session_id in range(background_max_sessions):
        read_table = background_table_name(background_read_prefix, session_id, background_max_sessions)
        write_table = background_table_name(background_write_prefix, session_id, background_max_sessions)
        for table_name, expected_rows in ((read_table, 1), (write_table, 0)):
            table_sql = f"SELECT to_regclass({sql_literal(table_name)}) IS NOT NULL;"
            exists = run_cmd(
                [
                    str(Path.home() / "pg" / "bin" / "psql"),
                    "-Atq",
                    "-h",
                    PRIMARY_HOST,
                    "-p",
                    PRIMARY_PORT,
                    "-U",
                    "JasonHu",
                    "-d",
                    dbname,
                    "-c",
                    table_sql,
                ],
                stdout=subprocess.PIPE,
            ).stdout.strip()
            if exists != "t":
                raise RuntimeError(
                    f"expected background table {table_name} in {dbname}. "
                    "Run scripts/setup_pgbench_vanilla_interference.py first."
                )

            persistence_sql = f"SELECT relpersistence FROM pg_class WHERE oid = {sql_literal(table_name)}::regclass;"
            persistence = run_cmd(
                [
                    str(Path.home() / "pg" / "bin" / "psql"),
                    "-Atq",
                    "-h",
                    PRIMARY_HOST,
                    "-p",
                    PRIMARY_PORT,
                    "-U",
                    "JasonHu",
                    "-d",
                    dbname,
                    "-c",
                    persistence_sql,
                ],
                stdout=subprocess.PIPE,
            ).stdout.strip()
            if persistence != "u":
                raise RuntimeError(
                    f"expected background table {table_name} to be unlogged, "
                    f"but relpersistence was {persistence!r}. "
                    "Run scripts/setup_pgbench_vanilla_interference.py first."
                )

            count_sql = f"SELECT count(*) FROM {sql_identifier(table_name)};"
            row_count = int(
                run_cmd(
                    [
                        str(Path.home() / "pg" / "bin" / "psql"),
                        "-Atq",
                        "-h",
                        PRIMARY_HOST,
                        "-p",
                        PRIMARY_PORT,
                        "-U",
                        "JasonHu",
                        "-d",
                        dbname,
                        "-c",
                        count_sql,
                    ],
                    stdout=subprocess.PIPE,
                ).stdout.strip()
                or "0"
            )
            if expected_rows == 1 and row_count < 1:
                raise RuntimeError(f"expected background read table {table_name} to contain rows")
            if expected_rows == 0 and row_count != 0:
                raise RuntimeError(f"expected background write table {table_name} to be empty")

    repl_sql = (
        f"SELECT count(*) FROM pg_stat_replication "
        f"WHERE application_name = '{PRIMARY_APPNAME}' AND state = 'streaming';"
    )
    repl = run_cmd(
        [
            str(Path.home() / "pg" / "bin" / "psql"),
            "-Atq",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-U",
            "JasonHu",
            "-d",
            "postgres",
            "-c",
            repl_sql,
        ],
        stdout=subprocess.PIPE,
    )
    if int(repl.stdout.strip() or "0") != 1:
        raise RuntimeError(
            f"expected vanilla standby {PRIMARY_APPNAME} to be streaming from node-0. "
            "Run scripts/setup_pgbench_vanilla_interference.py first."
        )


def make_writer_batch(rows: int, payload_bytes: int, *, session_id: int, batch_id: int) -> str:
    """Generate a CSV batch for the COPY writer workload."""
    payload = "x" * payload_bytes
    base_id = session_id * 10_000_000_000 + batch_id * rows
    lines = [f"{base_id + i},{payload}" for i in range(rows)]
    return "\n".join(lines) + "\n"


@dataclass
class BackgroundStats:
    """Track background worker completion and batch counts."""

    role: str
    sessions: int
    batches: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def build_remote_reader_script(
    *,
    start_at: float,
    stop_at: float,
    dbname: str,
    source_table: str,
) -> str:
    """Build the remote Python script that continuously streams a table to stdout."""
    return dedent(
        f"""\
        import os
        import subprocess
        import time

        start_at = {start_at!r}
        stop_at = {stop_at!r}
        dbname = {dbname!r}
        source_table = {source_table!r}
        psql_bin = os.path.expanduser("~/pg/bin/psql")
        batches = 0

        while time.time() < start_at:
            time.sleep(min(0.2, start_at - time.time()))

        while time.time() < stop_at:
            subprocess.run(
                [
                    psql_bin,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-X",
                    "-q",
                    "-h",
                    {PRIMARY_HOST!r},
                    "-p",
                    {PRIMARY_PORT!r},
                    "-U",
                    "JasonHu",
                    "-d",
                    dbname,
                    "-c",
                    f"COPY (SELECT * FROM {{source_table}}) TO STDOUT;",
                ],
                check=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            batches += 1

        print(batches)
        """
    )


def build_remote_writer_script(
    *,
    start_at: float,
    stop_at: float,
    dbname: str,
    sink_table: str,
    batch_rows: int,
    payload_bytes: int,
    session_id: int,
) -> str:
    """Build the remote Python script that continuously COPYs rows into the sink."""
    return dedent(
        f"""\
        import os
        import subprocess
        import time

        start_at = {start_at!r}
        stop_at = {stop_at!r}
        dbname = {dbname!r}
        sink_table = {sink_table!r}
        batch_rows = {batch_rows!r}
        payload_bytes = {payload_bytes!r}
        session_id = {session_id!r}
        psql_bin = os.path.expanduser("~/pg/bin/psql")
        payload = "x" * payload_bytes
        batches = 0
        batch_id = 0

        while time.time() < start_at:
            time.sleep(min(0.2, start_at - time.time()))

        while time.time() < stop_at:
            base_id = session_id * 10_000_000_000 + batch_id * batch_rows
            input_text = "".join(f"{{base_id + i}},{{payload}}\\n" for i in range(batch_rows))
            subprocess.run(
                [
                    psql_bin,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-X",
                    "-q",
                    "-h",
                    {PRIMARY_HOST!r},
                    "-p",
                    {PRIMARY_PORT!r},
                    "-U",
                    "JasonHu",
                    "-d",
                    dbname,
                    "-c",
                    f"COPY {{sink_table}} (id, payload) FROM STDIN WITH (FORMAT csv);",
                ],
                input=input_text,
                check=True,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            batches += 1
            batch_id += 1

        print(batches)
        """
    )


def build_remote_level_script(
    *,
    start_at: float,
    stop_at: float,
    dbname: str,
    source_tables: list[str],
    sink_tables: list[str],
    reader_sessions: int,
    writer_sessions: int,
    batch_rows: int,
    payload_bytes: int,
) -> str:
    """Build the remote Python script that fans out reader and writer threads on node-1."""
    return dedent(
        f"""\
        import os
        import subprocess
        import threading
        import time

        start_at = {start_at!r}
        stop_at = {stop_at!r}
        dbname = {dbname!r}
        source_tables = {source_tables!r}
        sink_tables = {sink_tables!r}
        reader_sessions = {reader_sessions!r}
        writer_sessions = {writer_sessions!r}
        batch_rows = {batch_rows!r}
        payload_bytes = {payload_bytes!r}
        psql_bin = os.path.expanduser("~/pg/bin/psql")
        payload = "x" * payload_bytes
        reader_batches = [0] * reader_sessions
        writer_batches = [0] * writer_sessions
        failure_event = threading.Event()

        def wait_for_start() -> None:
            while time.time() < start_at:
                time.sleep(min(0.2, start_at - time.time()))

        def reader_worker(slot: int) -> None:
            batches = 0
            source_table = source_tables[slot]
            try:
                wait_for_start()
                while time.time() < stop_at and not failure_event.is_set():
                    subprocess.run(
                        [
                            psql_bin,
                            "-v",
                            "ON_ERROR_STOP=1",
                            "-X",
                            "-q",
                            "-h",
                            {PRIMARY_HOST!r},
                            "-p",
                            {PRIMARY_PORT!r},
                            "-U",
                            "JasonHu",
                            "-d",
                            dbname,
                            "-c",
                            f"COPY (SELECT * FROM {{source_table}}) TO STDOUT;",
                        ],
                        check=True,
                        text=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    batches += 1
            except Exception:
                failure_event.set()
                raise
            finally:
                reader_batches[slot] = batches

        def writer_worker(slot: int) -> None:
            batches = 0
            batch_id = 0
            sink_table = sink_tables[slot]
            try:
                wait_for_start()
                while time.time() < stop_at and not failure_event.is_set():
                    base_id = slot * 10_000_000_000 + batch_id * batch_rows
                    input_text = "".join(f"{{base_id + i}},{{payload}}\\n" for i in range(batch_rows))
                    subprocess.run(
                        [
                            psql_bin,
                            "-v",
                            "ON_ERROR_STOP=1",
                            "-X",
                            "-q",
                            "-h",
                            {PRIMARY_HOST!r},
                            "-p",
                            {PRIMARY_PORT!r},
                            "-U",
                            "JasonHu",
                            "-d",
                            dbname,
                            "-c",
                            f"COPY {{sink_table}} (id, payload) FROM STDIN WITH (FORMAT csv);",
                        ],
                        input=input_text,
                        check=True,
                        text=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    batches += 1
                    batch_id += 1
            except Exception:
                failure_event.set()
                raise
            finally:
                writer_batches[slot] = batches

        threads = []
        for idx in range(reader_sessions):
            threads.append(threading.Thread(target=reader_worker, args=(idx,), daemon=True))
        for idx in range(writer_sessions):
            threads.append(threading.Thread(target=writer_worker, args=(idx,), daemon=True))

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        print(sum(reader_batches), sum(writer_batches))
        """
    )


def launch_remote_background_thread(
    *,
    barrier: threading.Barrier,
    level: int,
    start_at: float,
    stop_at: float,
    dbname: str,
    source_tables: list[str],
    sink_tables: list[str],
    batch_rows: int,
    payload_bytes: int,
    stats: list[BackgroundStats],
    failure_event: threading.Event,
) -> None:
    """Run one remote node-1 process that fans out all reader and writer sessions."""
    barrier.wait()
    remote_script = build_remote_level_script(
        start_at=start_at,
        stop_at=stop_at,
        dbname=dbname,
        source_tables=source_tables,
        sink_tables=sink_tables,
        reader_sessions=level,
        writer_sessions=level,
        batch_rows=batch_rows,
        payload_bytes=payload_bytes,
    )
    remote_cmd = f"/usr/bin/env python3 - <<'PY'\n{remote_script}\nPY"
    try:
        result = ssh_cmd(
            GENERATOR_NODE,
            remote_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        reader_batches, writer_batches = [int(part) for part in (result.stdout or "").split()[:2]]
        stats[0].batches = reader_batches
        stats[1].batches = writer_batches
    except Exception as exc:  # pragma: no cover - benchmark control path
        stats[0].errors.append(f"reader-level-{level}: {exc}")
        stats[1].errors.append(f"writer-level-{level}: {exc}")
        failure_event.set()


def launch_remote_reader_thread(
    *,
    barrier: threading.Barrier,
    session_id: int,
    start_at: float,
    stop_at: float,
    dbname: str,
    source_table: str,
    stats: BackgroundStats,
    failure_event: threading.Event,
) -> None:
    """Wait for the launch barrier and run one long-lived reader on node-1."""
    barrier.wait()
    remote_script = build_remote_reader_script(
        start_at=start_at,
        stop_at=stop_at,
        dbname=dbname,
        source_table=source_table,
    )
    remote_cmd = f"/usr/bin/env python3 - <<'PY'\n{remote_script}\nPY"
    try:
        result = ssh_cmd(
            GENERATOR_NODE,
            remote_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stats.batches = int((result.stdout or "").strip() or "0")
    except Exception as exc:  # pragma: no cover - benchmark control path
        stats.errors.append(f"reader-{session_id}: {exc}")
        failure_event.set()


def launch_remote_writer_thread(
    *,
    barrier: threading.Barrier,
    session_id: int,
    start_at: float,
    stop_at: float,
    dbname: str,
    sink_table: str,
    batch_rows: int,
    payload_bytes: int,
    stats: BackgroundStats,
    failure_event: threading.Event,
) -> None:
    """Wait for the launch barrier and run one long-lived writer on node-1."""
    barrier.wait()
    remote_script = build_remote_writer_script(
        start_at=start_at,
        stop_at=stop_at,
        dbname=dbname,
        sink_table=sink_table,
        batch_rows=batch_rows,
        payload_bytes=payload_bytes,
        session_id=session_id,
    )
    remote_cmd = f"/usr/bin/env python3 - <<'PY'\n{remote_script}\nPY"
    try:
        result = ssh_cmd(
            GENERATOR_NODE,
            remote_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stats.batches = int((result.stdout or "").strip() or "0")
    except Exception as exc:  # pragma: no cover - benchmark control path
        stats.errors.append(f"writer-{session_id}: {exc}")
        failure_event.set()


def truncate_background_write_tables(dbname: str, table_names: list[str]) -> None:
    """Keep background write tables from growing across rows."""
    if not table_names:
        return
    quoted_tables = ", ".join(sql_identifier(table_name) for table_name in table_names)
    run_cmd(
        [
            str(Path.home() / "pg" / "bin" / "psql"),
            "-v",
            "ON_ERROR_STOP=1",
            "-X",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-U",
            "JasonHu",
            "-d",
            dbname,
            "-c",
            f"TRUNCATE TABLE {quoted_tables};",
        ]
    )


def reset_pgbench_state(dbname: str, reset_mode: str) -> None:
    """Restore pgbench's logical table state before a measured row.

    The foreground TPC-B transaction updates account, branch, and teller
    balances and appends rows to pgbench_history. Reset those logical effects
    before each row so client-count and background-level comparisons start from
    the same table contents. This avoids full pgbench reinitialization because
    doing that for every row would dominate the experiment runtime.
    """
    if reset_mode == "none":
        return
    if reset_mode != "logical":
        raise ValueError(f"unsupported pgbench reset mode: {reset_mode}")

    psql_bin = str(Path.home() / "pg" / "bin" / "psql")
    reset_statements = [
        "TRUNCATE TABLE pgbench_history;",
        "UPDATE pgbench_accounts SET abalance = 0;",
        "UPDATE pgbench_branches SET bbalance = 0;",
        "UPDATE pgbench_tellers SET tbalance = 0;",
    ]
    for statement in reset_statements:
        run_cmd(
            [
                psql_bin,
                "-v",
                "ON_ERROR_STOP=1",
                "-X",
                "-h",
                PRIMARY_HOST,
                "-p",
                PRIMARY_PORT,
                "-U",
                "JasonHu",
                "-d",
                dbname,
                "-c",
                statement,
            ]
        )

    # Refresh planner statistics after the deterministic table-wide reset.
    for table_name in (
        "pgbench_accounts",
        "pgbench_branches",
        "pgbench_tellers",
        "pgbench_history",
    ):
        run_cmd(
            [
                psql_bin,
                "-v",
                "ON_ERROR_STOP=1",
                "-X",
                "-h",
                PRIMARY_HOST,
                "-p",
                PRIMARY_PORT,
                "-U",
                "JasonHu",
                "-d",
                dbname,
                "-c",
                f"ANALYZE {table_name};",
            ]
        )


def private_table_names(prefix: str, clients: int) -> list[str]:
    """Return the client-private foreground table names needed by one row."""
    return [f"{prefix}_{client_id:02d}" for client_id in range(clients)]


def run_primary_sql(dbname: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Run SQL on the vanilla primary and return stdout."""
    return run_cmd(
        [
            str(Path.home() / "pg" / "bin" / "psql"),
            "-v",
            "ON_ERROR_STOP=1",
            "-X",
            "-Atq",
            "-h",
            PRIMARY_HOST,
            "-p",
            PRIMARY_PORT,
            "-U",
            "JasonHu",
            "-d",
            dbname,
            "-c",
            sql,
        ],
        stdout=subprocess.PIPE,
    )


def ensure_client_private_tables(dbname: str, table_prefix: str, max_clients: int) -> None:
    """Verify that the client-private foreground tables exist on the primary."""
    for table_name in private_table_names(table_prefix, max_clients):
        exists = run_primary_sql(
            dbname,
            f"SELECT to_regclass({sql_literal(table_name)}) IS NOT NULL;",
        ).stdout.strip()
        if exists != "t":
            raise RuntimeError(
                f"expected client-private table {table_name} to exist. "
                "Run setup_pgbench_vanilla_interference.py with enough --private-client-count first."
            )


def reset_client_private_tables(
    dbname: str,
    table_prefix: str,
    clients: int,
    client_private_rows: int,
    reset_mode: str,
) -> None:
    """Restore the private foreground tables touched by the low-contention workload."""
    if reset_mode == "none":
        return
    if reset_mode != "logical":
        raise ValueError(f"unsupported pgbench reset mode: {reset_mode}")

    for table_name in private_table_names(table_prefix, clients):
        quoted_table = sql_identifier(table_name)
        run_primary_sql(
            dbname,
            (
                f"TRUNCATE TABLE {quoted_table}; "
                f"INSERT INTO {quoted_table} (aid, abalance) "
                f"SELECT series_id, 0 FROM generate_series(1, {client_private_rows}) AS rows(series_id);"
            ),
        )
        run_primary_sql(dbname, f"ANALYZE {quoted_table};")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a vanilla PostgreSQL benchmark with concurrent network traffic.")
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--duration", type=int, default=16)
    parser.add_argument(
        "--clients",
        type=int,
        nargs="+",
        default=[16],
        help="foreground pgbench client counts to benchmark",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="number of independent repetitions for each mode/client/background row",
    )
    parser.add_argument(
        "--pgbench-workers",
        type=int,
        default=8,
        help="maximum pgbench client-side worker threads to use for -j",
    )
    parser.add_argument(
        "--workload",
        choices=("client-private-table", "tpcb"),
        default="client-private-table",
        help=(
            "foreground transaction workload; client-private-table uses one "
            "single-client pgbench process per private account table"
        ),
    )
    parser.add_argument(
        "--private-table-prefix",
        default=PRIVATE_TABLE_PREFIX_DEFAULT,
        help="table prefix for the client-private-table foreground workload",
    )
    parser.add_argument(
        "--client-private-rows",
        type=int,
        default=100000,
        help="number of account rows in each client-private foreground table",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["local", "on", "remote_write", "remote_apply"],
        help="synchronous_commit modes to benchmark",
    )
    parser.add_argument("--sampling-rate", type=float, default=0.2)
    parser.add_argument(
        "--background-levels",
        type=int,
        nargs="+",
        default=[0, 1, 2, 4, 8],
        help="number of background reader/writer sessions per direction",
    )
    parser.add_argument(
        "--background-start-delay",
        type=float,
        default=8.0,
        help="seconds to wait before all traffic starts",
    )
    parser.add_argument(
        "--background-pad",
        type=float,
        default=10.0,
        help="extra seconds beyond the pgbench run that background traffic should continue",
    )
    parser.add_argument(
        "--writer-batch-rows",
        type=int,
        default=20000,
        help="rows per COPY batch for each background writer session",
    )
    parser.add_argument(
        "--writer-payload-bytes",
        type=int,
        default=256,
        help="payload width per row for background writer COPY batches",
    )
    parser.add_argument(
        "--background-max-sessions",
        type=int,
        default=BACKGROUND_MAX_SESSIONS_DEFAULT,
        help="number of per-session background table pairs prepared by setup",
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
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "bench-results-network-interference",
        help="directory for per-run logs and the final CSV",
    )
    parser.add_argument(
        "--csv-name",
        default=None,
        help="CSV filename inside results-dir (default: timestamped name)",
    )
    parser.add_argument(
        "--skip-setup-check",
        action="store_true",
        help="skip validation that the pgbench tables and sink table exist",
    )
    parser.add_argument(
        "--skip-server-log-archive",
        action="store_true",
        help="do not copy per-row PostgreSQL collector logs into the result tree",
    )
    parser.add_argument(
        "--reset-pgbench-between-runs",
        choices=("logical", "none"),
        default="logical",
        help=(
            "reset foreground table side effects before each measured row; "
            "'logical' restores the selected workload's account tables"
        ),
    )
    args = parser.parse_args()

    if args.pgbench_workers < 1:
        raise SystemExit("--pgbench-workers must be at least 1")
    if any(clients < 1 for clients in args.clients):
        raise SystemExit("--clients must contain only positive integers")
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    if args.client_private_rows < 1:
        raise SystemExit("--client-private-rows must be at least 1")
    if args.background_max_sessions < 1:
        raise SystemExit("--background-max-sessions must be at least 1")
    if not args.background_levels:
        raise SystemExit("--background-levels must not be empty")
    if max(args.background_levels) > args.background_max_sessions:
        raise SystemExit("--background-max-sessions must cover the largest --background-levels value")
    if not args.modes:
        raise SystemExit("--modes must not be empty")

    if not args.skip_setup_check:
        ensure_setup_ready(
            args.dbname,
            background_read_prefix=args.background_read_prefix,
            background_write_prefix=args.background_write_prefix,
            background_max_sessions=args.background_max_sessions,
        )
        if args.workload == "client-private-table":
            ensure_client_private_tables(args.dbname, args.private_table_prefix, max(args.clients))

    configure_synchronous_standby()
    reset_server_logs()

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    results_dir = args.results_dir / f"vanilla-network-interference-{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    csv_path = results_dir / (args.csv_name or "results.csv")
    fieldnames = [
        "background_level",
        "reader_sessions",
        "writer_sessions",
        "background_max_sessions",
        "background_read_prefix",
        "background_write_prefix",
        "workload",
        "private_table_prefix",
        "client_private_rows",
        "mode",
        "clients",
        "repeat",
        "threads",
        "duration_s",
        "sampling_rate",
        "transactions_processed",
        "failed_transactions",
        "latency_avg_ms",
        "latency_stddev_ms",
        "throughput_tps",
        "sampled_transactions",
        "total_log_lines",
        "estimated_total_transactions",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "min_ms",
        "max_ms",
        "non_numeric_log_entries",
        "server_logs_dir",
        "reader_batches",
        "writer_batches",
    ]

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for mode in args.modes:
                print(f"configuring synchronous_commit={mode}", flush=True)
                configure_sync_commit(mode)

                for clients in args.clients:
                    for level in args.background_levels:
                        for repeat in range(1, args.repeats + 1):
                            read_tables = background_table_names(
                                args.background_read_prefix,
                                args.background_max_sessions,
                            )[:level]
                            write_tables = background_table_names(
                                args.background_write_prefix,
                                args.background_max_sessions,
                            )[:level]
                            all_write_tables = background_table_names(
                                args.background_write_prefix,
                                args.background_max_sessions,
                            )
                            print(
                                f"running clients={clients} background level={level} repeat={repeat}",
                                flush=True,
                            )
                            if args.workload == "tpcb":
                                reset_pgbench_state(args.dbname, args.reset_pgbench_between_runs)
                            else:
                                reset_client_private_tables(
                                    args.dbname,
                                    args.private_table_prefix,
                                    clients,
                                    args.client_private_rows,
                                    args.reset_pgbench_between_runs,
                                )
                            truncate_background_write_tables(args.dbname, all_write_tables)

                            start_at = time.time() + args.background_start_delay
                            stop_at = start_at + args.duration + args.background_pad
                            barrier = threading.Barrier(2 if level > 0 else 1)
                            failure_event = threading.Event()

                            stats: list[BackgroundStats] = []
                            threads: list[threading.Thread] = []

                            if level > 0:
                                reader_stats = BackgroundStats(role="reader", sessions=level)
                                writer_stats = BackgroundStats(role="writer", sessions=level)
                                stats.extend([reader_stats, writer_stats])
                                threads.append(
                                    threading.Thread(
                                        target=launch_remote_background_thread,
                                        kwargs={
                                            "barrier": barrier,
                                            "level": level,
                                            "start_at": start_at,
                                            "stop_at": stop_at,
                                            "dbname": args.dbname,
                                            "source_tables": read_tables,
                                            "sink_tables": write_tables,
                                            "batch_rows": args.writer_batch_rows,
                                            "payload_bytes": args.writer_payload_bytes,
                                            "stats": stats,
                                            "failure_event": failure_event,
                                        },
                                        daemon=True,
                                    )
                                )

                                for thread in threads:
                                    thread.start()
                                barrier.wait()

                            while time.time() < start_at:
                                time.sleep(min(0.5, start_at - time.time()))

                            run_dir = workload_run_dir(results_dir, args.workload, mode, clients, level, repeat)
                            emit_row_log_marker(
                                mode=mode,
                                level=level,
                                clients=clients,
                                repeat=repeat,
                                phase="start",
                                nodes=SERVER_LOG_NODES,
                            )
                            metrics = run_pgbench(
                                mode=mode,
                                clients=clients,
                                worker_threads=args.pgbench_workers,
                                duration_s=args.duration,
                                dbname=args.dbname,
                                sample_rate=args.sampling_rate,
                                workload=args.workload,
                                private_table_prefix=args.private_table_prefix,
                                client_private_rows=args.client_private_rows,
                                run_dir=run_dir,
                            )

                            for thread in threads:
                                thread.join()

                            if failure_event.is_set():
                                detail = "; ".join(
                                    err
                                    for stat in stats
                                    for err in stat.errors
                                    if err
                                )
                                raise RuntimeError(f"background traffic worker failed at level {level}: {detail}")

                            metrics["background_level"] = level
                            metrics["reader_sessions"] = level
                            metrics["writer_sessions"] = level
                            metrics["background_max_sessions"] = args.background_max_sessions
                            metrics["background_read_prefix"] = args.background_read_prefix
                            metrics["background_write_prefix"] = args.background_write_prefix
                            metrics["repeat"] = repeat
                            metrics["private_table_prefix"] = args.private_table_prefix
                            metrics["client_private_rows"] = args.client_private_rows
                            metrics["reader_batches"] = sum(stat.batches for stat in stats if stat.role == "reader")
                            metrics["writer_batches"] = sum(stat.batches for stat in stats if stat.role == "writer")
                            metrics["server_logs_dir"] = str(run_dir / "server-logs")
                            emit_row_log_marker(
                                mode=mode,
                                level=level,
                                clients=clients,
                                repeat=repeat,
                                phase="end",
                                nodes=SERVER_LOG_NODES,
                            )
                            writer.writerow(metrics)
                            csv_file.flush()
                            finalize_server_logs_for_archive(SERVER_LOG_NODES)
                            if not args.skip_server_log_archive:
                                archive_server_logs(run_dir)
                            reset_server_logs()
                            if args.skip_server_log_archive:
                                prune_inactive_server_logs(SERVER_LOG_NODES)

                            truncate_background_write_tables(args.dbname, all_write_tables)
                            for log_file in run_dir.glob("pgbench_log*"):
                                if log_file.is_file():
                                    log_file.unlink()
    finally:
        try:
            restore_defaults()
        except Exception as exc:  # pragma: no cover - cleanup path
            print(f"warning: could not restore vanilla defaults: {exc}", file=sys.stderr)

    print(f"results written to {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
