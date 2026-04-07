#!/usr/bin/env python3
"""Plot and compare vanilla vs Citus network-interference sweeps.

The two input CSVs must use the same foreground workload shape and the same
background-level sweep. The figure layout mirrors the single-family
network-interference plotter but adds one row per dataset so the impact of
concurrent traffic is easy to compare.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


MODE_ORDER = ["off", "local", "on", "remote_write", "remote_apply"]
MODE_LABELS = {
    "off": "off",
    "local": "local",
    "on": "on",
    "remote_write": "remote_write",
    "remote_apply": "remote_apply",
}
DATASET_ORDER = ["citus", "vanilla"]
DATASET_LABELS = {
    "citus": "Citus distributed",
    "vanilla": "Vanilla PostgreSQL",
}


def load_frame(csv_path: Path, dataset: str) -> pd.DataFrame:
    """Load a benchmark CSV and attach a dataset label for comparison plots."""
    df = pd.read_csv(csv_path)
    df["background_level"] = df["background_level"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df["dataset"] = pd.Categorical([dataset] * len(df), categories=DATASET_ORDER, ordered=True)
    return df.sort_values(["dataset", "mode", "background_level"]).reset_index(drop=True)


def make_comparison_grid(df: pd.DataFrame, out_path: Path) -> None:
    """Render a 2x3 grid comparing Citus and vanilla across the same metrics."""
    metrics = [
        ("throughput_tps", "TPS", False),
        ("p50_ms", "p50 latency (ms, log scale)", True),
        ("p99_ms", "p99 latency (ms, log scale)", True),
    ]

    fig, axes = plt.subplots(
        len(DATASET_ORDER),
        len(metrics),
        figsize=(20, 10),
        dpi=200,
        sharex=True,
    )

    for row_idx, dataset in enumerate(DATASET_ORDER):
        subset = df[df["dataset"] == dataset]
        for col_idx, (metric, ylabel, logy) in enumerate(metrics):
            ax = axes[row_idx, col_idx]
            sns.lineplot(
                data=subset,
                x="background_level",
                y=metric,
                hue="mode",
                hue_order=MODE_ORDER,
                palette="deep",
                marker="o",
                linewidth=2.0,
                ax=ax,
                legend=False,
            )
            if row_idx == 0:
                ax.set_title(ylabel)
            if col_idx == 0:
                ax.set_ylabel(DATASET_LABELS[dataset])
            else:
                ax.set_ylabel("")
            ax.set_xlabel("Background reader/writer sessions per direction")
            ax.set_xticks(sorted(subset["background_level"].unique()))
            if logy:
                ax.set_yscale("log")
            ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
            sns.despine(ax=ax)

    handles = axes[0, 0].lines[: len(MODE_ORDER)]
    labels = [MODE_LABELS[mode] for mode in MODE_ORDER]
    fig.legend(
        handles,
        labels,
        title="synchronous_commit",
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=5,
        frameon=False,
    )
    fig.suptitle("pgbench network-interference comparison: Citus vs vanilla PostgreSQL", y=0.985)
    fig.tight_layout(rect=(0, 0.08, 1, 0.96))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plot a side-by-side comparison of two pgbench network-interference sweeps."
    )
    parser.add_argument("--citus-csv", type=Path, required=True, help="CSV from the Citus interference run")
    parser.add_argument("--vanilla-csv", type=Path, required=True, help="CSV from the vanilla interference run")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="directory where plots should be written (default: sibling 'comparison-plots' next to the Citus CSV)",
    )
    args = parser.parse_args()

    df = pd.concat(
        [
            load_frame(args.citus_csv, "citus"),
            load_frame(args.vanilla_csv, "vanilla"),
        ],
        ignore_index=True,
    )

    output_dir = args.output_dir
    if output_dir is None:
        output_dir = args.citus_csv.parent / "comparison-plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    make_comparison_grid(df, output_dir / "network_interference_comparison.png")
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
