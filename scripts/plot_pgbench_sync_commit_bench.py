#!/usr/bin/env python3
"""Plot the synchronous-commit pgbench sweep results with seaborn.

The input CSV is expected to come from ``run_pgbench_sync_commit_bench.py``.
The script generates one combined overview figure and individual plots for
throughput, p50 latency, and p99 latency.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


MODE_ORDER = ["local", "on", "remote_write", "remote_apply"]
MODE_LABELS = {
    "local": "local",
    "on": "on",
    "remote_write": "remote_write",
    "remote_apply": "remote_apply",
}


def load_frame(csv_path: Path) -> pd.DataFrame:
    """Load and normalize the benchmark CSV."""
    df = pd.read_csv(csv_path)
    df["clients"] = df["clients"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df = df.sort_values(["mode", "clients"]).reset_index(drop=True)
    return df


def make_lineplot(
    df: pd.DataFrame,
    *,
    metric: str,
    ylabel: str,
    title: str,
    out_path: Path,
    logy: bool = False,
    legend: bool = True,
) -> None:
    """Render one seaborn line plot for a given metric."""
    fig, ax = plt.subplots(figsize=(12, 7), dpi=200)
    sns.lineplot(
        data=df,
        x="clients",
        y=metric,
        hue="mode",
        hue_order=MODE_ORDER,
        palette="deep",
        marker="o",
        linewidth=2.0,
        ax=ax,
    )

    ax.set_title(title)
    ax.set_xlabel("Clients / Threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(sorted(df["clients"].unique()))
    if logy:
        ax.set_yscale("log")
    ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
    sns.despine(ax=ax)

    if not legend:
        legend_obj = ax.get_legend()
        if legend_obj is not None:
            legend_obj.remove()
    else:
        ax.legend(title="synchronous_commit", loc="best")

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def make_overview(df: pd.DataFrame, out_path: Path) -> None:
    """Render a three-panel overview figure."""
    fig, axes = plt.subplots(3, 1, figsize=(13, 15), dpi=200, sharex=True)
    specs = [
        ("throughput_tps", "TPS", False),
        ("p50_ms", "p50 latency (ms, log scale)", True),
        ("p99_ms", "p99 latency (ms, log scale)", True),
    ]

    for ax, (metric, ylabel, logy) in zip(axes, specs, strict=True):
        sns.lineplot(
            data=df,
            x="clients",
            y=metric,
            hue="mode",
            hue_order=MODE_ORDER,
            palette="deep",
            marker="o",
            linewidth=2.0,
            ax=ax,
            legend=False,
        )
        ax.set_ylabel(ylabel)
        ax.set_xticks(sorted(df["clients"].unique()))
        ax.set_xlabel("Clients / Threads")
        if logy:
            ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
        sns.despine(ax=ax)

    # Build one shared legend from the first axes so the overview stays compact.
    handles = axes[0].lines[: len(MODE_ORDER)]
    labels = [MODE_LABELS[mode] for mode in MODE_ORDER]
    fig.legend(
        handles,
        labels,
        title="synchronous_commit",
        loc="lower center",
        bbox_to_anchor=(0.5, 0.005),
        ncol=5,
        frameon=False,
    )
    fig.suptitle("pgbench synchronous_commit sweep on distributed Citus tables", y=0.985)
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot pgbench synchronous_commit benchmark results.")
    parser.add_argument("--csv", type=Path, required=True, help="results CSV produced by the benchmark sweep")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="directory where plots should be written (default: sibling 'plots' directory next to the CSV)",
    )
    args = parser.parse_args()

    df = load_frame(args.csv)
    output_dir = args.output_dir
    if output_dir is None:
        output_dir = args.csv.parent / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    make_overview(df, output_dir / "sync_commit_overview.png")
    make_lineplot(
        df,
        metric="throughput_tps",
        ylabel="TPS",
        title="pgbench throughput vs clients",
        out_path=output_dir / "sync_commit_tps.png",
        legend=True,
    )
    make_lineplot(
        df,
        metric="p50_ms",
        ylabel="Latency (ms, log scale)",
        title="pgbench p50 latency vs clients",
        out_path=output_dir / "sync_commit_p50.png",
        logy=True,
        legend=True,
    )
    make_lineplot(
        df,
        metric="p99_ms",
        ylabel="Latency (ms, log scale)",
        title="pgbench p99 latency vs clients",
        out_path=output_dir / "sync_commit_p99.png",
        logy=True,
        legend=True,
    )

    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
