#!/usr/bin/env python3
"""Plot the Citus network-interference benchmark results with seaborn."""

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


def load_frame(csv_path: Path) -> pd.DataFrame:
    """Load and normalize the benchmark CSV."""
    df = pd.read_csv(csv_path)
    df["background_level"] = df["background_level"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df = df.sort_values(["mode", "background_level"]).reset_index(drop=True)
    return df


def make_lineplot(df: pd.DataFrame, *, metric: str, ylabel: str, title: str, out_path: Path, logy: bool = False) -> None:
    """Render one seaborn line plot for a single metric."""
    fig, ax = plt.subplots(figsize=(10, 6), dpi=200)
    sns.lineplot(
        data=df,
        x="background_level",
        y=metric,
        hue="mode",
        hue_order=MODE_ORDER,
        marker="o",
        linewidth=2.0,
        ax=ax,
        palette="deep",
    )
    ax.set_title(title)
    ax.set_xlabel("Background reader/writer sessions per direction")
    ax.set_ylabel(ylabel)
    ax.set_xticks(sorted(df["background_level"].unique()))
    if logy:
        ax.set_yscale("log")
    ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
    sns.despine(ax=ax)
    ax.legend(title="synchronous_commit", loc="best")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def make_overview(df: pd.DataFrame, out_path: Path) -> None:
    """Render a three-panel overview figure."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 14), dpi=200, sharex=True)
    specs = [
        ("throughput_tps", "TPS", False),
        ("p50_ms", "p50 latency (ms, log scale)", True),
        ("p99_ms", "p99 latency (ms, log scale)", True),
    ]

    for ax, (metric, ylabel, logy) in zip(axes, specs, strict=True):
        sns.lineplot(
            data=df,
            x="background_level",
            y=metric,
            hue="mode",
            hue_order=MODE_ORDER,
            marker="o",
            linewidth=2.0,
            ax=ax,
            palette="deep",
        )
        ax.set_ylabel(ylabel)
        ax.set_xlabel("Background reader/writer sessions per direction")
        ax.set_xticks(sorted(df["background_level"].unique()))
        if logy:
            ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
        sns.despine(ax=ax)
        if ax.get_legend() is not None:
            ax.get_legend().remove()

    handles = axes[0].lines[: len(MODE_ORDER)]
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
    fig.suptitle("Citus pgbench network-interference sweep", y=0.985)
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot Citus network-interference benchmark results.")
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

    make_overview(df, output_dir / "network_interference_overview.png")
    make_lineplot(
        df,
        metric="throughput_tps",
        ylabel="TPS",
        title="pgbench throughput vs background traffic",
        out_path=output_dir / "network_interference_tps.png",
        logy=False,
    )
    make_lineplot(
        df,
        metric="p50_ms",
        ylabel="Latency (ms, log scale)",
        title="pgbench p50 latency vs background traffic",
        out_path=output_dir / "network_interference_p50.png",
        logy=True,
    )
    make_lineplot(
        df,
        metric="p99_ms",
        ylabel="Latency (ms, log scale)",
        title="pgbench p99 latency vs background traffic",
        out_path=output_dir / "network_interference_p99.png",
        logy=True,
    )

    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
