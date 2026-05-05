#!/usr/bin/env python3
"""Plot the Citus network-interference benchmark results with seaborn."""

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
    df["background_level"] = df["background_level"].astype(int)
    if "clients" in df.columns:
        df["clients"] = df["clients"].astype(int)
    if "repeat" in df.columns:
        df["repeat"] = df["repeat"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    sort_columns = ["mode", "background_level"]
    if "workload" in df.columns:
        sort_columns = ["workload", *sort_columns]
    if "clients" in df.columns:
        sort_columns = ["clients", *sort_columns]
    df = df.sort_values(sort_columns).reset_index(drop=True)
    return df


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


def make_lineplot(
    df: pd.DataFrame,
    *,
    metric: str,
    ylabel: str,
    title: str,
    out_path: Path,
    logy: bool = False,
    title_suffix: str = "",
) -> None:
    """Render one seaborn line plot for a single metric."""
    fig, ax = plt.subplots(figsize=(10, 6), dpi=200)
    plot_metric(ax, df, metric=metric, logy=logy)
    ax.set_title(f"{title}{title_suffix}")
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


def make_overview(df: pd.DataFrame, out_path: Path, *, title_suffix: str = "") -> None:
    """Render a three-panel overview figure."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 14), dpi=200, sharex=True)
    specs = [
        ("throughput_tps", "TPS", False),
        ("p50_ms", "p50 latency (ms, log scale)", True),
        ("p99_ms", "p99 latency (ms, log scale)", True),
    ]

    for ax, (metric, ylabel, logy) in zip(axes, specs, strict=True):
        plot_metric(ax, df, metric=metric, logy=logy)
        ax.set_ylabel(ylabel)
        ax.set_xlabel("Background reader/writer sessions per direction")
        ax.set_xticks(sorted(df["background_level"].unique()))
        if logy:
            ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
        sns.despine(ax=ax)
        if ax.get_legend() is not None:
            ax.get_legend().remove()

    handles, labels = axes[0].get_legend_handles_labels()
    for ax in axes:
        if ax.get_legend() is not None:
            ax.get_legend().remove()
    fig.legend(
        handles,
        labels,
        title="synchronous_commit",
        loc="lower center",
        bbox_to_anchor=(0.5, 0.01),
        ncol=5,
        frameon=False,
    )
    fig.suptitle(f"Citus pgbench network-interference sweep{title_suffix}", y=0.985)
    fig.tight_layout(rect=(0, 0.06, 1, 0.97))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def plot_subsets(df: pd.DataFrame) -> list[tuple[int | None, str | None, pd.DataFrame]]:
    """Split a CSV so each plot shows one foreground load and workload."""
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
    """Add suffixes when one CSV contains multiple foreground loads or workloads."""
    stem, suffix = base_name.rsplit(".", 1)
    parts = [stem]
    if workload is not None:
        parts.append(workload.replace("-", "_"))
    if client_count is not None:
        parts.append(f"c{client_count:02d}")
    return "_".join(parts) + f".{suffix}"


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

    for client_count, workload, subset in plot_subsets(df):
        title_parts = []
        if workload is not None:
            title_parts.append(f"workload={workload}")
        if client_count is not None:
            title_parts.append(f"clients={client_count}")
        title_suffix = "" if not title_parts else f" ({', '.join(title_parts)})"
        make_overview(
            subset,
            output_dir / output_name("network_interference_overview.png", client_count, workload),
            title_suffix=title_suffix,
        )
        make_lineplot(
            subset,
            metric="throughput_tps",
            ylabel="TPS",
            title="pgbench throughput vs background traffic",
            out_path=output_dir / output_name("network_interference_tps.png", client_count, workload),
            logy=False,
            title_suffix=title_suffix,
        )
        make_lineplot(
            subset,
            metric="p50_ms",
            ylabel="Latency (ms, log scale)",
            title="pgbench p50 latency vs background traffic",
            out_path=output_dir / output_name("network_interference_p50.png", client_count, workload),
            logy=True,
            title_suffix=title_suffix,
        )
        make_lineplot(
            subset,
            metric="p99_ms",
            ylabel="Latency (ms, log scale)",
            title="pgbench p99 latency vs background traffic",
            out_path=output_dir / output_name("network_interference_p99.png", client_count, workload),
            logy=True,
            title_suffix=title_suffix,
        )

    print(output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
