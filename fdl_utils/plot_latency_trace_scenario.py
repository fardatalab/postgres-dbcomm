#!/usr/bin/env python3
"""plot_latency_trace_scenario.py

Plot one scenario CSV produced by parse_latency_trace_csv.py as a consolidated
stacked horizontal bar using the mean of all rows.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import seaborn as sns


CONSOLIDATED_BUCKETS: Sequence[Tuple[str, Tuple[str, ...]]] = (
    (
        "Client Session Setup",
        ("backend_spawn_ns", "client_session_establish_ns"),
    ),
    (
        "Coordinator Backend Work",
        ("backend_parse_plan_ns",),
    ),
    (
        "Worker Session Setup",
        (
            "worker_session_acquire_comm_ns",
        ),
    ),
    (
        "Worker Lifecycle Work",
        (
            "worker_session_acquire_actual_ns",
            "remote_tx_attach_actual_ns",
            "remote_tx_commit_actual_ns",
            "remote_tx_abort_actual_ns",
            "remote_tx_prepare_actual_ns",
        ),
    ),
    (
        "Distributed TX Management",
        (
            "remote_tx_attach_comm_ns",
            "remote_tx_commit_comm_ns",
            "remote_tx_abort_comm_ns",
            "remote_tx_prepare_comm_ns",
        ),
    ),
    (
        "Remote Execution Control",
        (
            "placement_bind_ns",
            "remote_command_dispatch_ns",
            "remote_command_wait_comm_ns",
            "remote_result_drain_comm_ns",
        ),
    ),
    (
        "Worker Backend Work",
        (
            "remote_command_wait_actual_ns",
            "remote_result_drain_actual_ns",
        ),
    ),
    (
        "Client Response",
        (
            "client_command_complete_ns",
            "client_ready_for_query_ns",
        ),
    ),
)


MUTED_JEWEL_TONES: Sequence[str] = (
    "#52796F",  # jade
    "#4F6D7A",  # muted sapphire
    "#A26769",  # dusty ruby
    "#8C6A5D",  # garnet brown
    "#6D597A",  # plum
    "#7D8F69",  # moss
    "#5C7A8D",  # teal blue
    "#8A817C",  # muted stone
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot one scenario CSV from parse_latency_trace_csv.py as a stacked horizontal PDF."
    )
    parser.add_argument("csv_path", help="Scenario CSV path produced by parse_latency_trace_csv.py")
    parser.add_argument("-o", "--output", required=True, help="Output PDF path")
    parser.add_argument(
        "--title",
        default=None,
        help="Optional plot title. Defaults to the CSV filename stem.",
    )
    return parser.parse_args()


def load_rows(csv_path: Path) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def column_mean_ns(rows: List[Dict[str, str]], column_name: str) -> float:
    """Return the mean of one CSV column, defaulting missing columns to zero."""

    if not rows:
        return 0.0

    if column_name not in rows[0]:
        return 0.0

    values = [int(row[column_name]) for row in rows]
    return float(sum(values)) / float(len(values))


def mean_bucket_values(rows: List[Dict[str, str]]) -> List[Tuple[str, float]]:
    """Compute consolidated mean bucket values for one scenario CSV.

    The parser may be run either with or without worker logs. When worker joins
    are missing, fall back to the raw coordinator stage totals so the plot still
    represents the traced span instead of collapsing buckets to zero.
    """

    if not rows:
        return []

    bucket_values: List[Tuple[str, float]] = []
    first_row = rows[0]

    worker_join_available = any(
        int(row.get("matched_worker_timing_blocks", "0")) > 0 for row in rows
    )

    for label, columns in CONSOLIDATED_BUCKETS:
        mean_value = 0.0

        if label == "Worker Session Setup":
            if worker_join_available:
                mean_value = column_mean_ns(rows, "worker_session_acquire_comm_ns")
            else:
                mean_value = column_mean_ns(rows, "worker_session_acquire_ns")
        elif label == "Worker Lifecycle Work":
            if worker_join_available:
                mean_value = sum(column_mean_ns(rows, column_name) for column_name in columns)
            else:
                mean_value = 0.0
        elif label == "Distributed TX Management":
            if worker_join_available:
                mean_value = sum(column_mean_ns(rows, column_name) for column_name in columns)
            else:
                mean_value = (
                    column_mean_ns(rows, "remote_tx_attach_ns")
                    + column_mean_ns(rows, "remote_tx_commit_ns")
                    + column_mean_ns(rows, "remote_tx_abort_ns")
                    + column_mean_ns(rows, "remote_tx_prepare_ns")
                )
        elif label == "Remote Execution Control":
            if worker_join_available:
                mean_value = sum(column_mean_ns(rows, column_name) for column_name in columns)
            else:
                mean_value = (
                    column_mean_ns(rows, "placement_bind_ns")
                    + column_mean_ns(rows, "remote_command_dispatch_ns")
                    + column_mean_ns(rows, "remote_command_wait_ns")
                    + column_mean_ns(rows, "remote_result_drain_ns")
                )
        elif label == "Worker Backend Work":
            if worker_join_available:
                mean_value = sum(column_mean_ns(rows, column_name) for column_name in columns)
            else:
                mean_value = 0.0
        else:
            mean_value = sum(column_mean_ns(rows, column_name) for column_name in columns if column_name in first_row)

        if mean_value > 0.0:
            bucket_values.append((label, mean_value))

    return bucket_values


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv_path)
    output_path = Path(args.output)

    rows = load_rows(csv_path)
    if not rows:
        raise SystemExit(f"no rows found in {csv_path}")

    components = mean_bucket_values(rows)
    if not components:
        raise SystemExit(f"no plottable latency columns found in {csv_path}")

    sns.set_theme(style="whitegrid")
    fig, ax = plt.subplots(figsize=(14, 3.8))

    total_mean_ns = sum(value_ns for _, value_ns in components)
    if total_mean_ns <= 0.0:
        raise SystemExit(f"scenario mean is zero in {csv_path}")

    left = 0.0
    patches = []
    labels = []
    for idx, (label, mean_ns) in enumerate(components):
        color = MUTED_JEWEL_TONES[idx % len(MUTED_JEWEL_TONES)]
        width_pct = (mean_ns / total_mean_ns) * 100.0
        patch = ax.barh(
            y=[0],
            width=[width_pct],
            left=left,
            height=0.62,
            color=color,
            edgecolor="white",
            linewidth=0.8,
        )[0]
        left += width_pct
        patches.append(patch)
        labels.append(label)

    ax.set_title(args.title or csv_path.stem, fontsize=14, pad=12)
    ax.set_xlabel("Mean Share Of Traced Transaction Latency (%)")
    ax.set_yticks([0])
    ax.set_yticklabels(["Mean"])
    ax.set_xlim(0.0, 100.0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", linestyle="--", alpha=0.35)
    ax.grid(axis="y", visible=False)
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.set_xticklabels([f"{tick}%" for tick in [0, 20, 40, 60, 80, 100]])
    ax.legend(
        patches,
        labels,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.24),
        ncol=3,
        frameon=False,
        fontsize=9,
    )

    fig.tight_layout()
    fig.savefig(output_path, format="pdf", bbox_inches="tight")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
