#!/usr/bin/env python3
"""Run a pgbench synchronous-commit sweep and write a CSV report.

This script assumes it is executed on the coordinator host (`node-0`) so the
local `pgbench` and `psql` binaries can be used directly, while the worker and
standby nodes are managed over SSH.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import subprocess
import sys
import time
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Node:
    ssh_name: str
    label: str


NODES = [
    Node("node-0", "coordinator"),
    Node("node-1", "worker1"),
    Node("node-2", "worker2"),
    Node("node-3", "worker1_standby"),
    Node("node-4", "worker2_standby"),
]

WORKER_SYNC_STANDBY_NAMES = {
    "node-1": "FIRST 1 (worker1_stby)",
    "node-2": "FIRST 1 (worker2_stby)",
}


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
    """Set a runtime GUC on the local coordinator node and reload."""
    sql = f"ALTER SYSTEM SET {setting} = '{value}';"
    run_cmd([str(Path.home() / "pg" / "bin" / "psql"), "-v", "ON_ERROR_STOP=1", "-X", "-d", "postgres", "-h", "127.0.0.1", "-c", sql])
    run_cmd([str(Path.home() / "pg" / "bin" / "pg_ctl"), "reload", "-D", str(Path.home() / "pg" / "data")])


def set_system_setting_remote(node: str, setting: str, value: str) -> None:
    """Set a runtime GUC on a remote node and reload that server."""
    remote_cmd = (
        f"set -e; "
        f"$HOME/pg/bin/psql -v ON_ERROR_STOP=1 -X -d postgres -c \"ALTER SYSTEM SET {setting} = '{value}';\"; "
        f"$HOME/pg/bin/pg_ctl reload -D $HOME/pg/data"
    )
    ssh_cmd(node, remote_cmd)


def configure_synchronous_standbys() -> None:
    """Ensure the worker primaries are actually waiting on their physical standbys."""
    for node, standby_names in WORKER_SYNC_STANDBY_NAMES.items():
        set_system_setting_remote(node, "synchronous_standby_names", standby_names)


def configure_sync_commit(mode: str) -> None:
    """Apply the requested synchronous_commit mode cluster-wide."""
    set_system_setting_local("synchronous_commit", mode)
    for node in ("node-1", "node-2", "node-3", "node-4"):
        set_system_setting_remote(node, "synchronous_commit", mode)


def restore_defaults() -> None:
    """Put the cluster back into the normal committed mode after the benchmark."""
    try:
        set_system_setting_local("synchronous_commit", "on")
        for node in ("node-1", "node-2", "node-3", "node-4"):
            set_system_setting_remote(node, "synchronous_commit", "on")
        for node in ("node-1", "node-2"):
            ssh_cmd(
                node,
                "$HOME/pg/bin/psql -v ON_ERROR_STOP=1 -X -d postgres -c "
                "\"ALTER SYSTEM RESET synchronous_standby_names;\"; "
                "$HOME/pg/bin/pg_ctl reload -D $HOME/pg/data",
            )
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
    # Keep transaction concurrency controlled by -c while avoiding unnecessary
    # client-side worker oversubscription. pgbench's -j only affects how the
    # benchmark driver feeds sessions, so we cap it independently.
    threads = min(worker_threads, clients)
    cmd = [
        str(pgbench_bin),
        "-U",
        "JasonHu",
        "-h",
        "10.10.1.2",
        "-p",
        "5432",
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

    print(
        "  done: "
        f"tps={metrics['throughput_tps']:.3f} "
        f"p50={metrics['p50_ms']:.3f}ms "
        f"p99={metrics['p99_ms']:.3f}ms",
        flush=True,
    )

    return metrics


def ensure_setup_ready(dbname: str) -> None:
    """Verify that the distributed pgbench schema exists before sweeping modes."""
    sql = (
        "SELECT count(*) FROM pg_dist_partition "
        "WHERE logicalrelid IN ('pgbench_accounts'::regclass, 'pgbench_branches'::regclass, "
        "'pgbench_tellers'::regclass, 'pgbench_history'::regclass);"
    )
    result = run_cmd([str(Path.home() / "pg" / "bin" / "psql"), "-Atq", "-h", "127.0.0.1", "-p", "5432", "-U", "JasonHu", "-d", dbname, "-c", sql])
    count = int(result.stdout.strip() or "0")
    if count != 4:
        raise RuntimeError(
            f"expected 4 distributed pgbench tables in {dbname}, found {count}. "
            "Run scripts/setup_pgbench_citus.sh first."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a synchronous_commit pgbench sweep on Citus.")
    parser.add_argument("--dbname", default="postgres")
    parser.add_argument("--duration", type=int, default=5)
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
        default=["off", "local", "on", "remote_write", "remote_apply"],
        help="synchronous_commit modes to benchmark",
    )
    parser.add_argument("--sampling-rate", type=float, default=1.0)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=REPO_ROOT / "bench-results",
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
        help="skip validation that the pgbench tables are distributed",
    )
    args = parser.parse_args()

    if args.pgbench_workers < 1:
        raise SystemExit("--pgbench-workers must be at least 1")

    if not args.skip_setup_check:
        ensure_setup_ready(args.dbname)

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
    ]

    configure_synchronous_standbys()
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for mode in args.modes:
                print(f"configuring synchronous_commit={mode}", flush=True)
                configure_sync_commit(mode)

                for clients in range(1, args.max_clients + 1):
                    run_dir = results_dir / mode / f"c{clients:02d}"
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
