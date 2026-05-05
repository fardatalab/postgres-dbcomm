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


MODE_ORDER = ["local", "on", "remote_write", "remote_apply"]
MODE_LABELS = {
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
    if "clients" in df.columns:
        df["clients"] = df["clients"].astype(int)
    if "repeat" in df.columns:
        df["repeat"] = df["repeat"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df["dataset"] = pd.Categorical([dataset] * len(df), categories=DATASET_ORDER, ordered=True)
    sort_columns = ["dataset", "mode", "background_level"]
    if "workload" in df.columns:
        sort_columns = ["workload", *sort_columns]
    if "clients" in df.columns:
        sort_columns = ["clients", *sort_columns]
    return df.sort_values(sort_columns).reset_index(drop=True)


def metric_summary(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Aggregate repeated rows into mean and standard deviation."""
    summary = (
        df.groupby(["mode", "background_level"], observed=True)[metric]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    summary["std"] = summary["std"].fillna(0.0)
    return summary


def errorbar_values(rows: pd.DataFrame, *, logy: bool) -> list[float] | list[list[float]] | None:
    """Return symmetric or log-safe asymmetric error bars."""
    if not (rows["count"] > 1).any():
        return None
    upper = rows["std"].astype(float).tolist()
    if not logy:
        return upper
    lower = [
        min(float(stddev), max(float(mean) * 0.95, 0.0))
        for mean, stddev in zip(rows["mean"], rows["std"], strict=True)
    ]
    return [lower, upper]


def plot_metric(ax, df: pd.DataFrame, *, metric: str, logy: bool) -> None:
    """Plot one metric as mean plus standard-deviation error bars."""
    summary = metric_summary(df, metric)
    for mode in MODE_ORDER:
        rows = summary[summary["mode"] == mode]
        if rows.empty:
            continue
        ax.errorbar(
            rows["background_level"],
            rows["mean"],
            yerr=errorbar_values(rows, logy=logy),
            label=MODE_LABELS[mode],
            marker="o",
            linewidth=2.0,
            capsize=3,
        )


def make_comparison_grid(df: pd.DataFrame, out_path: Path, *, title_suffix: str = "") -> None:
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
            plot_metric(ax, subset, metric=metric, logy=logy)
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
            if ax.get_legend() is not None:
                ax.get_legend().remove()

    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        title="synchronous_commit",
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=5,
        frameon=False,
    )
    fig.suptitle(
        f"pgbench network-interference comparison: Citus vs vanilla PostgreSQL{title_suffix}",
        y=0.985,
    )
    fig.tight_layout(rect=(0, 0.08, 1, 0.96))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def plot_subsets(df: pd.DataFrame) -> list[tuple[int | None, str | None, pd.DataFrame]]:
    """Split input into one comparison figure per foreground load and workload."""
    clients = sorted(df["clients"].unique()) if "clients" in df.columns else [None]
    workloads = sorted(df["workload"].dropna().unique()) if "workload" in df.columns else [None]
    subsets = []
    for client_count in clients:
        for workload in workloads:
            subset = df
            if client_count is not None:
                subset = subset[subset["clients"] == client_count]
            if workload is not None:
                subset = subset[subset["workload"] == workload]
            subsets.append((None if client_count is None else int(client_count), workload, subset))
    return subsets


def output_name(base_name: str, client_count: int | None, workload: str | None) -> str:
    """Add suffixes when comparison inputs contain multiple loads or workloads."""
    stem, suffix = base_name.rsplit(".", 1)
    parts = [stem]
    if workload is not None:
        parts.append(workload.replace("-", "_"))
    if client_count is not None:
        parts.append(f"c{client_count:02d}")
    return "_".join(parts) + f".{suffix}"


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

    for client_count, workload, subset in plot_subsets(df):
        title_parts = []
        if workload is not None:
            title_parts.append(f"workload={workload}")
        if client_count is not None:
            title_parts.append(f"clients={client_count}")
        title_suffix = "" if not title_parts else f" ({', '.join(title_parts)})"
        make_comparison_grid(
            subset,
            output_dir / output_name("network_interference_comparison.png", client_count, workload),
            title_suffix=title_suffix,
        )
    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
