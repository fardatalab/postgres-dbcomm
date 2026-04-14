#!/usr/bin/env python3
"""Run a synchronous_commit pgbench sweep on vanilla PostgreSQL.

This helper is the plain-Postgres counterpart to the Citus benchmark runner.
It assumes node-0 is the primary, node-3 is its physical standby, and the
standard pgbench tables already exist in the target database.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import shlex
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_HELPER = REPO_ROOT / "scripts" / "pgbench_report.py"


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
ARCHIVE_SERVER_LOGS = REPO_ROOT / "scripts" / "archive_server_logs.py"
RESET_SERVER_LOGS = REPO_ROOT / "scripts" / "reset_server_logs.py"

PRIMARY_NODE = "node-0"
STANDBY_NODE = "node-3"
PRIMARY_APPNAME = "vanilla_stby"
PRIMARY_HOST = "127.0.0.1"
PRIMARY_PORT = "5432"
SERVER_LOG_NODES = ("node-0", "node-3")


def build_client_counts(max_clients: int) -> list[int]:
    """Return a powers-of-two client sweep up to the requested maximum."""

    counts: list[int] = []
    clients = 1
    while clients <= max_clients:
        counts.append(clients)
        clients *= 2
    return counts


def run_cmd(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and raise immediately on failure."""
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        text=True,
        capture_output=True,
    )


def ssh_cmd(node: str, remote_cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command on a remote node via SSH."""
    return run_cmd(["ssh", "-o", "BatchMode=yes", node, remote_cmd])


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

    The benchmark row can span multiple collector segments, so archiving only
    the newest logfile can truncate the earliest traced transactions in the
    row and break coordinator/worker matching later.
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


def finalize_server_logs_for_archive(nodes: tuple[str, ...]) -> None:
    """Force collector-visible row boundaries before archiving.

    The row-end marker gives parsing a logical fence, but the logging
    collector may still be writing into the current segment when we snapshot
    the files. Rotating once here makes the row-local contents durable before
    we copy them into the benchmark archive.
    """

    remote_cmd = (
        "export PATH=$HOME/pg/bin:$PATH; "
        "psql -v ON_ERROR_STOP=1 -X -d postgres -c 'SELECT pg_rotate_logfile();'"
    )
    for node in nodes:
        ssh_cmd(node, remote_cmd)

    time.sleep(0.5)


def emit_row_log_marker(*, mode: str, clients: int, phase: str, nodes: tuple[str, ...]) -> None:
    """Emit one explicit LOG line on each node around one benchmark row.

    The start marker forces the fresh collector segment to exist before pgbench
    begins, while the end marker leaves a terminal collector entry before the
    archival step.
    """

    message = f"pgbench row marker phase={phase} mode={mode} clients={clients}"
    sql = f"DO $$ BEGIN RAISE LOG {message!r}; END $$;"
    remote_cmd = (
        "export PATH=$HOME/pg/bin:$PATH; "
        f"psql -v ON_ERROR_STOP=1 -X -d postgres -c {shlex.quote(sql)}"
    )
    for node in nodes:
        ssh_cmd(node, remote_cmd)


def configure_synchronous_standby() -> None:
    """Make the primary wait for the physical standby when needed."""
    set_system_setting_local("synchronous_standby_names", f"FIRST 1 ({PRIMARY_APPNAME})")


def configure_sync_commit(mode: str) -> None:
    """Apply the requested synchronous_commit mode to the primary and standby."""
    set_system_setting_local("synchronous_commit", mode)
    set_system_setting_remote(STANDBY_NODE, "synchronous_commit", mode)


def restore_defaults() -> None:
    """Put the vanilla pair back into the normal committed mode after the benchmark."""
    try:
        set_system_setting_local("synchronous_commit", "on")
        set_system_setting_remote(STANDBY_NODE, "synchronous_commit", "on")
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
                "ALTER SYSTEM RESET synchronous_standby_names;",
            ]
        )
        run_cmd([str(Path.home() / "pg" / "bin" / "pg_ctl"), "reload", "-D", str(Path.home() / "pg" / "data")])
    except Exception as exc:  # pragma: no cover - cleanup path
        print(f"warning: could not restore synchronous_commit to on: {exc}", file=sys.stderr)


def collect_metrics(summary_path: Path, log_prefix: Path, sampling_rate: float) -> dict[str, float | int]:
    """Parse pgbench summary text and transaction logs into one metrics dict."""
    summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
    summary = REPORT.parse_summary(summary_text)

    log_paths = sorted(log_prefix.parent.glob(f"{log_prefix.name}*"))
    times_us, non_numeric, total_lines = REPORT.load_log_times(log_paths)
    if not times_us:
        raise RuntimeError(f"no transaction samples were found for {log_prefix}")

    metrics: dict[str, float | int] = {
        "transactions_processed": summary.processed if summary.processed is not None else -1,
        "failed_transactions": summary.failed if summary.failed is not None else -1,
        "latency_avg_ms": summary.latency_avg_ms if summary.latency_avg_ms is not None else -1.0,
        "latency_stddev_ms": summary.latency_stddev_ms if summary.latency_stddev_ms is not None else -1.0,
        "throughput_tps": summary.tps if summary.tps is not None else -1.0,
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


def run_pgbench(
    *,
    mode: str,
    clients: int,
    worker_threads: int,
    duration_s: int,
    dbname: str,
    sample_rate: float,
    run_dir: Path,
) -> dict[str, float | int]:
    """Execute one pgbench run and return the parsed metrics."""
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "pgbench.out"
    log_prefix = run_dir / "pgbench_log"

    pgbench_bin = Path.home() / "pg" / "bin" / "pgbench"
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
        f"running mode={mode} clients={clients} threads={threads} duration={duration_s}s",
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

    metrics = collect_metrics(summary_path, log_prefix, sample_rate)
    metrics["mode"] = mode
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


def ensure_setup_ready(dbname: str) -> None:
    """Verify the vanilla pgbench tables exist and the standby is streaming."""
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
        ]
    )
    count = int(result.stdout.strip() or "0")
    if count != 4:
        raise RuntimeError(f"expected 4 vanilla pgbench tables in {dbname}, found {count}. Run scripts/setup_pgbench_vanilla.py first.")

    repl_sql = f"SELECT count(*) FROM pg_stat_replication WHERE application_name = '{PRIMARY_APPNAME}' AND state = 'streaming';"
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
        ]
    )
    if int(repl.stdout.strip() or "0") != 1:
        raise RuntimeError(
            f"expected vanilla standby {PRIMARY_APPNAME} to be streaming from node-0. "
            "Run scripts/setup_pgbench_vanilla.py first."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synchronous_commit pgbench sweep on vanilla PostgreSQL.")
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--duration", type=int, default=12)
    parser.add_argument("--max-clients", type=int, default=32)
    parser.add_argument(
        "--pgbench-workers",
        type=int,
        default=8,
        help="maximum pgbench client-side worker threads to use for -j",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["local", "on", "remote_write", "remote_apply"],
        help="synchronous_commit modes to benchmark",
    )
    parser.add_argument("--sampling-rate", type=float, default=0.05)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "bench-results-vanilla",
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
        help="skip validation that the pgbench database exists and the standby is streaming",
    )
    args = parser.parse_args()

    if args.pgbench_workers < 1:
        raise SystemExit("--pgbench-workers must be at least 1")

    if not args.skip_setup_check:
        ensure_setup_ready(args.dbname)

    configure_synchronous_standby()
    reset_server_logs()

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    results_dir = args.results_dir / f"pgbench-sync-commit-{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)

    csv_path = results_dir / (args.csv_name or "results.csv")
    fieldnames = [
        "mode",
        "clients",
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
    ]

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for mode in args.modes:
                print(f"configuring synchronous_commit={mode}", flush=True)
                configure_sync_commit(mode)

                for clients in build_client_counts(args.max_clients):
                    run_dir = results_dir / mode / f"c{clients:02d}"
                    emit_row_log_marker(
                        mode=mode,
                        clients=clients,
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
                        run_dir=run_dir,
                    )
                    writer.writerow(metrics)
                    csv_file.flush()
                    emit_row_log_marker(
                        mode=mode,
                        clients=clients,
                        phase="end",
                        nodes=SERVER_LOG_NODES,
                    )
                    finalize_server_logs_for_archive(SERVER_LOG_NODES)
                    archive_server_logs(run_dir)
                    reset_server_logs()

                    # Keep only the summary and the CSV by default. Raw logs can
                    # be very large across 160 benchmark points.
                    for log_file in run_dir.glob("pgbench_log*"):
                        if log_file.is_file():
                            log_file.unlink()
    finally:
        restore_defaults()

    print(f"results written to {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
