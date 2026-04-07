#!/usr/bin/env python3
"""Summarize pgbench output and per-transaction log files.

This helper keeps pgbench's own throughput/summary reporting as the source of
truth for TPS and average latency, then computes tail latency percentiles from
the sampled transaction log files produced by ``--log`` / ``-l``.
"""

from __future__ import annotations

import argparse
import glob
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SUMMARY_RE = {
    "processed": re.compile(r"^number of transactions actually processed:\s+(\d+)"),
    "failed": re.compile(r"^number of failed transactions:\s+(\d+)"),
    "latency_avg": re.compile(r"^latency average =\s+([0-9.]+)\s+ms"),
    "latency_stddev": re.compile(r"^latency stddev =\s+([0-9.]+)\s+ms"),
    "tps": re.compile(r"^tps =\s+([0-9.]+)\s+\(without initial connection time\)"),
}

SUMMARY_FIELD = {
    "processed": "processed",
    "failed": "failed",
    "latency_avg": "latency_avg_ms",
    "latency_stddev": "latency_stddev_ms",
    "tps": "tps",
}


@dataclass
class PgbenchSummary:
    processed: int | None = None
    failed: int | None = None
    latency_avg_ms: float | None = None
    latency_stddev_ms: float | None = None
    tps: float | None = None


def parse_summary(text: str) -> PgbenchSummary:
    """Parse the standard pgbench terminal summary."""
    summary = PgbenchSummary()
    for line in text.splitlines():
        for key, pattern in SUMMARY_RE.items():
            match = pattern.match(line.strip())
            if not match:
                continue
            value = match.group(1)
            field_name = SUMMARY_FIELD[key]
            if key in {"processed", "failed"}:
                setattr(summary, field_name, int(value))
            else:
                setattr(summary, field_name, float(value))
            break
    return summary


def percentile_nearest_rank(values: Sequence[int], percentile: float) -> int:
    """Return the nearest-rank percentile for a sorted or unsorted sample."""
    if not values:
        raise ValueError("cannot compute percentiles on an empty sample")
    if percentile <= 0:
        return min(values)
    if percentile >= 100:
        return max(values)

    ordered = sorted(values)
    rank = math.ceil((percentile / 100.0) * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def load_log_times(paths: Iterable[Path]) -> tuple[list[int], int, int]:
    """Load transaction times in microseconds from pgbench log files.

    Returns:
        times_us: Numeric transaction latencies.
        failed_or_skipped: Non-numeric transaction entries, if present.
        total_lines: Total lines read from the log files.
    """
    times_us: list[int] = []
    failed_or_skipped = 0
    total_lines = 0

    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                total_lines += 1
                fields = line.split()
                if len(fields) < 3:
                    continue
                time_field = fields[2]
                try:
                    times_us.append(int(time_field))
                except ValueError:
                    failed_or_skipped += 1

    return times_us, failed_or_skipped, total_lines


def format_ms(us: int | float) -> str:
    return f"{us / 1000.0:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize pgbench TPS and percentile latency from logs."
    )
    parser.add_argument(
        "--summary",
        type=Path,
        help="pgbench stdout/stderr summary file. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--log",
        action="append",
        nargs="+",
        default=[],
        help="pgbench log file or glob. May be specified multiple times.",
    )
    parser.add_argument(
        "--sampling-rate",
        type=float,
        default=1.0,
        help="Sampling rate used when writing logs. Only affects estimated total count.",
    )
    parser.add_argument(
        "--percentiles",
        type=float,
        nargs="*",
        default=[50.0, 95.0, 99.0],
        help="Percentiles to report from the logged transaction sample.",
    )
    args = parser.parse_args()

    if args.summary is None:
        summary_text = sys.stdin.read()
    else:
        summary_text = args.summary.read_text(encoding="utf-8", errors="replace")

    summary = parse_summary(summary_text)

    log_paths: list[Path] = []
    for spec_group in args.log:
        for spec in spec_group:
            matches = sorted(glob.glob(spec))
            if matches:
                log_paths.extend(Path(match) for match in matches)
            else:
                log_paths.append(Path(spec))

    times_us, non_numeric, total_lines = load_log_times(log_paths)
    if not times_us:
        raise SystemExit("no numeric transaction times found in the provided log files")

    sampled = len(times_us)
    sample_rate = args.sampling_rate
    estimated_total = None
    if sample_rate > 0:
        estimated_total = sampled / sample_rate

    print("pgbench summary")
    print("---------------")
    if summary.processed is not None:
        print(f"transactions processed: {summary.processed}")
    if summary.failed is not None:
        print(f"failed transactions: {summary.failed}")
    if summary.latency_avg_ms is not None:
        print(f"latency average: {summary.latency_avg_ms:.3f} ms")
    if summary.latency_stddev_ms is not None:
        print(f"latency stddev: {summary.latency_stddev_ms:.3f} ms")
    if summary.tps is not None:
        print(f"throughput: {summary.tps:.3f} tps")

    print()
    print("log sample")
    print("----------")
    print(f"log files: {len(log_paths)}")
    print(f"total log lines read: {total_lines}")
    print(f"numeric latency samples: {sampled}")
    if non_numeric:
        print(f"non-numeric log entries: {non_numeric}")
    if sample_rate != 1.0 and estimated_total is not None:
        print(f"estimated total transactions from sample rate: {estimated_total:.0f}")
    for pct in args.percentiles:
        value_us = percentile_nearest_rank(times_us, pct)
        print(f"p{pct:g}: {format_ms(value_us)} ms")
    print(f"min: {format_ms(min(times_us))} ms")
    print(f"max: {format_ms(max(times_us))} ms")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
