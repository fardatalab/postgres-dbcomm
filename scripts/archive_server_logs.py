#!/usr/bin/env python3
"""Archive PostgreSQL collector logs from one or more cluster nodes.

The benchmark runner can now hand this script a row-start snapshot containing
each node's active logfile path and the byte offset that marks the beginning of
the row. When that snapshot is present, we copy only the row-local suffix for
each node and stitch those suffixes into one logical ``node-X.log`` per
server.

The script still keeps the older whole-directory archive mode as a debugging
fallback, but the benchmark runner should prefer the snapshot mode so
determinism does not depend on logfile names changing across rotations.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
import shlex


DEFAULT_NODES = ("node-0", "node-1", "node-2", "node-3", "node-4")


@dataclass(frozen=True)
class ArchivedLog:
    """One copied logfile and the remote source path it came from."""

    node: str
    source_path: str
    destination_path: str
    size_bytes: int
    start_offset_bytes: int
    copied_bytes: int


@dataclass(frozen=True)
class LogfileSnapshot:
    """One remote collector logfile plus its byte size at snapshot time."""

    source_path: str
    size_bytes: int


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one command and capture stdout/stderr for error reporting."""

    return subprocess.run(args, check=check, text=True, capture_output=True)


def ssh_cmd(node: str, remote_cmd: str) -> subprocess.CompletedProcess[str]:
    """Run a remote command through SSH."""

    return run_cmd(["ssh", "-o", "BatchMode=yes", node, remote_cmd])


def discover_logfiles(node: str) -> list[LogfileSnapshot]:
    """Return every collector logfile currently present on one node.

    The helper intentionally snapshots the whole collector directory instead of
    just pg_current_logfile(), because the benchmark runs can rotate through
    many log segments before we get a chance to archive them. Keeping the whole
    set makes the later latency-trace parser robust against rotation timing.
    """

    list_cmd = (
        "set -e; "
        "cd /users/JasonHu/pg/data/log; "
        "find . -maxdepth 1 -type f -name 'postgresql-*.log' "
        "-printf '%P\t%s\n' | sort"
    )
    result = ssh_cmd(node, list_cmd)
    snapshots: list[LogfileSnapshot] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rel_path, size_text = line.split("\t", 1)
        snapshots.append(
            LogfileSnapshot(
                source_path=f"/users/JasonHu/pg/data/log/{rel_path}",
                size_bytes=int(size_text),
            )
        )

    if not snapshots:
        raise RuntimeError(f"could not find any collector logfiles on {node}")

    return snapshots


def copy_logfile(node: str, source_path: str, dest_path: Path) -> None:
    """Copy one logfile from a node into the local archive directory."""

    scp_target = f"{node}:{source_path}"
    run_cmd(["scp", "-o", "BatchMode=yes", scp_target, str(dest_path)])


def copy_log_suffix(node: str, source_path: str, start_offset_bytes: int, dest_path: Path) -> None:
    """Copy only the bytes written after the captured row-start offset.

    Postgres can reopen the same logfile path when the logging collector
    rotates a daily file. Capturing the byte offset at row start lets us archive
    just the row-local suffix even when the filename itself does not change.
    """

    if start_offset_bytes < 0:
        raise ValueError(f"negative row-start offset for {node}: {start_offset_bytes}")

    remote_cmd = "set -e; tail -c +{} {}".format(start_offset_bytes + 1, shlex.quote(source_path))
    with dest_path.open("wb") as dest:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", node, remote_cmd],
            check=True,
            stdout=dest,
        )


def load_baseline(baseline_json: Path | None) -> dict[str, dict[str, int]]:
    """Load the optional row-start logfile byte offsets.

    The benchmark runner records one snapshot just before the row-start marker
    is emitted. When a node keeps reusing one daily collector filename, that
    byte offset is what lets the archive helper keep only the row-local suffix
    instead of replaying the whole day's logfile into the parser again.
    """

    if baseline_json is None:
        return {}

    raw = json.loads(baseline_json.read_text(encoding="utf-8"))
    baseline: dict[str, dict[str, int]] = {}
    for node, entries in raw.items():
        baseline[node] = {
            entry["source_path"]: int(entry["size_bytes"])
            for entry in entries
        }
    return baseline


def archive_nodes(
    nodes: list[str],
    output_dir: Path,
    baseline: dict[str, dict[str, int]],
) -> list[ArchivedLog]:
    """Archive all collector logfiles from every requested node."""

    output_dir.mkdir(parents=True, exist_ok=True)
    archived: list[ArchivedLog] = []

    for node in nodes:
        node_baseline = baseline.get(node, {})
        for snapshot in discover_logfiles(node):
            source_path = snapshot.source_path
            dest_path = output_dir / f"{node}-{Path(source_path).name}"
            copy_logfile(node, source_path, dest_path)
            size_bytes = dest_path.stat().st_size
            start_offset_bytes = node_baseline.get(source_path, 0)
            copied_bytes = max(0, size_bytes - start_offset_bytes)
            archived.append(
                ArchivedLog(
                    node=node,
                    source_path=source_path,
                    destination_path=str(dest_path),
                    size_bytes=size_bytes,
                    start_offset_bytes=start_offset_bytes,
                    copied_bytes=copied_bytes,
                )
            )

    return archived


def archive_nodes_from_snapshot(snapshot_json: Path, output_dir: Path) -> list[ArchivedLog]:
    """Archive only the row-local suffix captured for each node.

    The snapshot JSON is expected to contain a list of objects with keys:
    ``node``, ``source_path``, and ``start_offset_bytes``.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(snapshot_json.read_text(encoding="utf-8"))
    archived: list[ArchivedLog] = []

    for entry in payload:
        node = str(entry["node"])
        source_path = str(entry["source_path"])
        start_offset_bytes = int(entry["start_offset_bytes"])
        dest_path = output_dir / f"{node}.log"
        copy_log_suffix(node, source_path, start_offset_bytes, dest_path)
        size_bytes = dest_path.stat().st_size
        archived.append(
            ArchivedLog(
                node=node,
                source_path=source_path,
                destination_path=str(dest_path),
                size_bytes=size_bytes,
                start_offset_bytes=start_offset_bytes,
                copied_bytes=size_bytes,
            )
        )

    return archived


def write_combined_logs(archived: list[ArchivedLog], output_dir: Path) -> None:
    """Combine each node's copied segments into one chronological logfile.

    The benchmark parser and plotting helpers already expect one logical
    ``node-X.log`` per server. Preserve that interface here, while still
    keeping the individual collector segments around for debugging.
    """

    archived_by_node: dict[str, list[ArchivedLog]] = {}
    for entry in archived:
        archived_by_node.setdefault(entry.node, []).append(entry)

    for node, entries in archived_by_node.items():
        # The collector filenames embed timestamps, so lexicographic order is
        # the on-disk chronological order we want to replay for parsing.
        entries.sort(key=lambda entry: entry.source_path)
        combined_path = output_dir / f"{node}.log"
        with combined_path.open("wb") as combined:
            for entry in entries:
                segment_path = Path(entry.destination_path)
                segment_bytes = segment_path.read_bytes()
                if not segment_bytes:
                    continue

                combined.write(segment_bytes)
                # Guard against a missing trailing newline in one segment
                # gluing the next segment's first log line onto it.
                if not segment_bytes.endswith(b"\n"):
                    combined.write(b"\n")


def write_manifest(archived: list[ArchivedLog], output_dir: Path) -> None:
    """Write a small CSV manifest describing the copied logs."""

    manifest_path = output_dir / "manifest.csv"
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "node",
                "source_path",
                "destination_path",
                "size_bytes",
                "start_offset_bytes",
                "copied_bytes",
            ],
        )
        writer.writeheader()
        for entry in archived:
            writer.writerow(
                {
                    "node": entry.node,
                    "source_path": entry.source_path,
                    "destination_path": entry.destination_path,
                    "size_bytes": entry.size_bytes,
                    "start_offset_bytes": entry.start_offset_bytes,
                    "copied_bytes": entry.copied_bytes,
                }
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(description="Archive PostgreSQL collector logs from cluster nodes.")
    parser.add_argument(
        "--nodes",
        nargs="+",
        default=list(DEFAULT_NODES),
        help="SSH hostnames to archive. Defaults to all five cluster nodes.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Local directory where the copied logs and manifest should be written.",
    )
    parser.add_argument(
        "--baseline-json",
        default=None,
        help=(
            "Optional row-start snapshot JSON emitted by the benchmark runner. "
            "When provided, the archive copies only the row-local suffix for "
            "each node instead of snapshotting the whole collector directory."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Copy each node's row-local collector snapshot into the archive directory."""

    args = parse_args(argv)
    output_dir = Path(args.output_dir)
    baseline_json = Path(args.baseline_json) if args.baseline_json else None

    try:
        snapshot_mode = False
        if baseline_json is not None:
            payload = json.loads(baseline_json.read_text(encoding="utf-8"))
            if isinstance(payload, list):
                archived = archive_nodes_from_snapshot(baseline_json, output_dir)
                snapshot_mode = True
            else:
                baseline = load_baseline(baseline_json)
                archived = archive_nodes(list(args.nodes), output_dir, baseline)
        else:
            baseline = load_baseline(baseline_json)
            archived = archive_nodes(list(args.nodes), output_dir, baseline)
        write_manifest(archived, output_dir)
        if not snapshot_mode:
            write_combined_logs(archived, output_dir)
    except Exception as exc:
        print(f"error: failed to archive logs: {exc}", file=sys.stderr)
        return 1

    for entry in archived:
        print(f"{entry.node}: {entry.source_path} -> {entry.destination_path} ({entry.size_bytes} bytes)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
