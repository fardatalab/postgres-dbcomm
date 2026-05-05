#!/usr/bin/env python3
"""Run vanilla pgbench while physical basebackup streams run in the background."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import threading
import time
from pathlib import Path
from textwrap import dedent

import run_pgbench_network_interference_vanilla as vanilla


GENERATOR_NODE = "node-1"
PRIMARY_NODE = "node-0"
PRIMARY_NIC = "enp23s0f0"
GENERATOR_NIC = "enp23s0f0"
REPL_USER = "vanilla_repl"


def ssh_cmd(node: str, remote_cmd: str, *, stdout=None, stderr=None) -> subprocess.CompletedProcess[str]:
    """Run one command on a remote node."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", node, remote_cmd],
        check=True,
        text=True,
        stdout=stdout,
        stderr=stderr,
    )


def read_counter(node: str, interface: str, counter_name: str) -> int:
    """Read one Linux network device counter."""
    result = ssh_cmd(
        node,
        f"cat /sys/class/net/{interface}/statistics/{counter_name}",
        stdout=subprocess.PIPE,
    )
    return int(result.stdout.strip())


def build_basebackup_script(*, streams: int, stop_at: float) -> str:
    """Build the remote node-1 script that loops pg_basebackup streams."""
    return dedent(
        f"""\
        import os
        import subprocess
        import threading
        import time

        streams = {streams!r}
        stop_at = {stop_at!r}
        pg_basebackup = os.path.expanduser("~/pg/bin/pg_basebackup")
        counts = [0] * streams
        failures = [0] * streams
        command = [
            pg_basebackup,
            "-h", {vanilla.PRIMARY_HOST!r},
            "-p", {vanilla.PRIMARY_PORT!r},
            "-U", {REPL_USER!r},
            "-w",
            "-D", "-",
            "-F", "t",
            "-X", "none",
            "-c", "fast",
            "--no-sync",
            "--no-manifest",
        ]

        def worker(slot: int) -> None:
            while time.time() < stop_at:
                remain = max(stop_at - time.time(), 0.0)
                if remain <= 0:
                    break
                try:
                    subprocess.run(
                        ["timeout", f"{{remain:.3f}}s", *command],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    counts[slot] += 1
                except subprocess.CalledProcessError:
                    failures[slot] += 1

        threads = [threading.Thread(target=worker, args=(idx,), daemon=True) for idx in range(streams)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        print(sum(counts), sum(failures))
        """
    )


def run_basebackup_background(
    *,
    barrier: threading.Barrier,
    streams: int,
    stop_at: float,
    stats: dict[str, int],
    errors: list[str],
) -> None:
    """Run looping pg_basebackup streams on node-1."""
    barrier.wait()
    if streams <= 0:
        return
    remote_script = build_basebackup_script(streams=streams, stop_at=stop_at)
    remote_cmd = f"/usr/bin/env python3 - <<'PY'\n{remote_script}\nPY"
    try:
        result = ssh_cmd(GENERATOR_NODE, remote_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        parts = (result.stdout or "").split()
        if len(parts) >= 2:
            stats["completed_backups"] = int(parts[0])
            stats["failed_backups"] = int(parts[1])
    except Exception as exc:  # pragma: no cover - benchmark control path
        errors.append(str(exc))


def remove_pgbench_logs(run_dir: Path) -> None:
    """Remove raw pgbench logs after metrics have been parsed."""
    for log_file in run_dir.glob("pgbench_log*"):
        if log_file.is_file():
            log_file.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pgbench with background physical basebackup traffic.")
    parser.add_argument("--dbname", default="pgbench_vanilla")
    parser.add_argument("--duration", type=int, default=16)
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 8, 16])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", nargs="+", default=["local", "remote_write", "remote_apply"])
    parser.add_argument("--basebackup-streams", type=int, nargs="+", default=[0, 1, 4, 8])
    parser.add_argument("--background-start-delay", type=float, default=8.0)
    parser.add_argument("--background-pad", type=float, default=4.0)
    parser.add_argument("--sampling-rate", type=float, default=0.2)
    parser.add_argument("--pgbench-workers", type=int, default=8)
    parser.add_argument("--private-table-prefix", default=vanilla.PRIVATE_TABLE_PREFIX_DEFAULT)
    parser.add_argument("--client-private-rows", type=int, default=100000)
    parser.add_argument("--results-dir", type=Path, default=vanilla.REPO_ROOT / "bench-results-network-interference")
    args = parser.parse_args()

    if any(clients < 1 for clients in args.clients):
        raise SystemExit("--clients must contain only positive integers")
    if any(streams < 0 for streams in args.basebackup_streams):
        raise SystemExit("--basebackup-streams must contain only non-negative integers")
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")

    vanilla.ensure_setup_ready(
        args.dbname,
        background_read_prefix=vanilla.BACKGROUND_READ_PREFIX_DEFAULT,
        background_write_prefix=vanilla.BACKGROUND_WRITE_PREFIX_DEFAULT,
        background_max_sessions=vanilla.BACKGROUND_MAX_SESSIONS_DEFAULT,
    )
    vanilla.ensure_client_private_tables(args.dbname, args.private_table_prefix, max(args.clients))
    vanilla.configure_synchronous_standby()
    vanilla.reset_server_logs()

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    results_dir = args.results_dir / f"vanilla-basebackup-interference-{timestamp}"
    results_dir.mkdir(parents=True, exist_ok=True)
    csv_path = results_dir / "results.csv"

    fieldnames = [
        "basebackup_streams",
        "completed_backups",
        "failed_backups",
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
        "node0_tx_bytes",
        "node1_rx_bytes",
        "node0_tx_gbps",
        "node1_rx_gbps",
        "server_logs_dir",
    ]

    try:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()

            for mode in args.modes:
                print(f"configuring synchronous_commit={mode}", flush=True)
                vanilla.configure_sync_commit(mode)

                for clients in args.clients:
                    for streams in args.basebackup_streams:
                        for repeat in range(1, args.repeats + 1):
                            print(
                                f"running mode={mode} clients={clients} "
                                f"basebackup_streams={streams} repeat={repeat}",
                                flush=True,
                            )
                            vanilla.reset_client_private_tables(
                                args.dbname,
                                args.private_table_prefix,
                                clients,
                                args.client_private_rows,
                                "logical",
                            )

                            run_dir = (
                                results_dir
                                / "client-private-table"
                                / mode
                                / f"c{clients:02d}"
                                / f"bb{streams:02d}"
                                / f"r{repeat:02d}"
                            )
                            run_dir.mkdir(parents=True, exist_ok=True)
                            start_at = time.time() + args.background_start_delay
                            stop_at = start_at + args.duration + args.background_pad
                            barrier = threading.Barrier(2 if streams > 0 else 1)
                            stats = {"completed_backups": 0, "failed_backups": 0}
                            errors: list[str] = []

                            if streams > 0:
                                bg_thread = threading.Thread(
                                    target=run_basebackup_background,
                                    kwargs={
                                        "barrier": barrier,
                                        "streams": streams,
                                        "stop_at": stop_at,
                                        "stats": stats,
                                        "errors": errors,
                                    },
                                    daemon=True,
                                )
                                bg_thread.start()
                                barrier.wait()

                            while time.time() < start_at:
                                time.sleep(min(0.5, start_at - time.time()))

                            tx_before = read_counter(PRIMARY_NODE, PRIMARY_NIC, "tx_bytes")
                            rx_before = read_counter(GENERATOR_NODE, GENERATOR_NIC, "rx_bytes")
                            metric_start = time.monotonic()
                            metrics = vanilla.run_pgbench(
                                mode=mode,
                                clients=clients,
                                worker_threads=args.pgbench_workers,
                                duration_s=args.duration,
                                dbname=args.dbname,
                                sample_rate=args.sampling_rate,
                                workload="client-private-table",
                                private_table_prefix=args.private_table_prefix,
                                client_private_rows=args.client_private_rows,
                                run_dir=run_dir,
                            )
                            elapsed = time.monotonic() - metric_start
                            tx_after = read_counter(PRIMARY_NODE, PRIMARY_NIC, "tx_bytes")
                            rx_after = read_counter(GENERATOR_NODE, GENERATOR_NIC, "rx_bytes")

                            if streams > 0:
                                bg_thread.join()
                            if errors:
                                raise RuntimeError("; ".join(errors))

                            tx_delta = tx_after - tx_before
                            rx_delta = rx_after - rx_before
                            metrics["basebackup_streams"] = streams
                            metrics["completed_backups"] = stats["completed_backups"]
                            metrics["failed_backups"] = stats["failed_backups"]
                            metrics["repeat"] = repeat
                            metrics["node0_tx_bytes"] = tx_delta
                            metrics["node1_rx_bytes"] = rx_delta
                            metrics["node0_tx_gbps"] = tx_delta * 8 / elapsed / 1e9
                            metrics["node1_rx_gbps"] = rx_delta * 8 / elapsed / 1e9
                            writer.writerow({name: metrics.get(name, "") for name in fieldnames})
                            csv_file.flush()

                            remove_pgbench_logs(run_dir)
                            vanilla.reset_server_logs()
                            vanilla.prune_inactive_server_logs(vanilla.SERVER_LOG_NODES)
    finally:
        vanilla.restore_defaults()

    print(f"results written to {csv_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
