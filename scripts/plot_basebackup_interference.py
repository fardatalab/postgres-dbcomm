#!/usr/bin/env python3
"""Plot pgbench latency under physical basebackup interference."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


MODE_ORDER = ["local", "remote_write", "remote_apply"]
MODE_LABELS = {
    "local": "local",
    "remote_write": "remote_write",
    "remote_apply": "remote_apply",
}


def load_frame(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for column in (
        "basebackup_streams",
        "clients",
        "repeat",
        "latency_avg_ms",
        "throughput_tps",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "node0_tx_gbps",
        "node1_rx_gbps",
    ):
        df[column] = pd.to_numeric(df[column])
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df["mode_label"] = df["mode"].astype(str).map(MODE_LABELS)
    df["clients_label"] = df["clients"].map(lambda value: f"{value} client" if value == 1 else f"{value} clients")
    return df


def add_ratios(df: pd.DataFrame) -> pd.DataFrame:
    keys = ["mode", "clients", "repeat"]
    baseline = (
        df[df["basebackup_streams"] == 0][
            [*keys, "latency_avg_ms", "p50_ms", "p95_ms", "p99_ms", "throughput_tps"]
        ]
        .rename(
            columns={
                "latency_avg_ms": "baseline_avg_ms",
                "p50_ms": "baseline_p50_ms",
                "p95_ms": "baseline_p95_ms",
                "p99_ms": "baseline_p99_ms",
                "throughput_tps": "baseline_tps",
            }
        )
    )
    out = df.merge(baseline, on=keys, how="left")
    out["avg_slowdown"] = out["latency_avg_ms"] / out["baseline_avg_ms"]
    out["p50_slowdown"] = out["p50_ms"] / out["baseline_p50_ms"]
    out["p95_slowdown"] = out["p95_ms"] / out["baseline_p95_ms"]
    out["p99_slowdown"] = out["p99_ms"] / out["baseline_p99_ms"]
    out["throughput_fraction"] = out["throughput_tps"] / out["baseline_tps"]

    local = out[out["mode"].astype(str) == "local"][
        [
            "clients",
            "repeat",
            "basebackup_streams",
            "avg_slowdown",
            "p50_slowdown",
            "p95_slowdown",
            "p99_slowdown",
            "throughput_fraction",
        ]
    ].rename(
        columns={
            "avg_slowdown": "local_avg_slowdown",
            "p50_slowdown": "local_p50_slowdown",
            "p95_slowdown": "local_p95_slowdown",
            "p99_slowdown": "local_p99_slowdown",
            "throughput_fraction": "local_throughput_fraction",
        }
    )
    out = out.merge(local, on=["clients", "repeat", "basebackup_streams"], how="left")
    out["avg_slowdown_over_local"] = out["avg_slowdown"] / out["local_avg_slowdown"]
    out["p50_slowdown_over_local"] = out["p50_slowdown"] / out["local_p50_slowdown"]
    out["p95_slowdown_over_local"] = out["p95_slowdown"] / out["local_p95_slowdown"]
    out["p99_slowdown_over_local"] = out["p99_slowdown"] / out["local_p99_slowdown"]
    out["throughput_fraction_over_local"] = out["throughput_fraction"] / out["local_throughput_fraction"]
    return out


def write_summary(df: pd.DataFrame, output_dir: Path) -> None:
    metrics = [
        "latency_avg_ms",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "throughput_tps",
        "node0_tx_gbps",
        "node1_rx_gbps",
        "avg_slowdown",
        "p99_slowdown",
        "throughput_fraction",
        "avg_slowdown_over_local",
        "p99_slowdown_over_local",
        "throughput_fraction_over_local",
    ]
    summary = (
        df.groupby(["mode", "clients", "basebackup_streams"], observed=True)[metrics]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    summary.columns = ["_".join(str(part) for part in column if part) for column in summary.columns]
    summary.to_csv(output_dir / "summary.csv", index=False)


def save_grid(grid: sns.FacetGrid, path: Path, *, legend_columns: int = 3, bottom: float = 0.1) -> None:
    if grid.legend is not None:
        sns.move_legend(
            grid,
            "lower center",
            bbox_to_anchor=(0.5, -0.01),
            ncol=legend_columns,
            frameon=False,
            title=None,
        )
    grid.figure.tight_layout(rect=(0, bottom, 1, 0.96))
    grid.figure.savefig(path, dpi=220, bbox_inches="tight")
    grid.figure.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(grid.figure)


def plot_absolute_latency(df: pd.DataFrame, output_dir: Path) -> None:
    long = df.melt(
        id_vars=["mode_label", "clients_label", "basebackup_streams", "repeat"],
        value_vars=["latency_avg_ms", "p99_ms"],
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(
        {
            "latency_avg_ms": "average latency (ms)",
            "p99_ms": "p99 latency (ms)",
        }
    )
    grid = sns.relplot(
        data=long,
        x="basebackup_streams",
        y="value",
        hue="mode_label",
        row="metric",
        col="clients_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.0,
        aspect=1.15,
    )
    grid.set_axis_labels("Concurrent pg_basebackup streams", "")
    grid.set_titles("{row_name}\n{col_name}")
    for ax in grid.axes.flat:
        ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["basebackup_streams"].unique()))
    grid.figure.suptitle("Physical backup traffic increases replicated transaction latency", y=1.02)
    save_grid(grid, output_dir / "basebackup_latency_absolute.png")


def plot_raw_slowdown(df: pd.DataFrame, output_dir: Path) -> None:
    long = df.melt(
        id_vars=["mode_label", "clients_label", "basebackup_streams", "repeat"],
        value_vars=["avg_slowdown", "p99_slowdown", "throughput_fraction"],
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(
        {
            "avg_slowdown": "average latency / no-backup baseline",
            "p99_slowdown": "p99 latency / no-backup baseline",
            "throughput_fraction": "TPS / no-backup baseline",
        }
    )
    grid = sns.relplot(
        data=long,
        x="basebackup_streams",
        y="value",
        hue="mode_label",
        row="metric",
        col="clients_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=2.9,
        aspect=1.15,
    )
    grid.set_axis_labels("Concurrent pg_basebackup streams", "")
    grid.set_titles("{row_name}\n{col_name}")
    for ax in grid.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["basebackup_streams"].unique()))
    grid.figure.suptitle("Raw slowdown relative to each mode's no-backup baseline", y=1.02)
    save_grid(grid, output_dir / "basebackup_raw_slowdown.png")


def plot_local_normalized(df: pd.DataFrame, output_dir: Path) -> None:
    remote = df[df["mode"].astype(str).isin(["remote_write", "remote_apply"])].copy()
    long = remote.melt(
        id_vars=["mode_label", "clients_label", "basebackup_streams", "repeat"],
        value_vars=[
            "avg_slowdown_over_local",
            "p99_slowdown_over_local",
            "throughput_fraction_over_local",
        ],
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(
        {
            "avg_slowdown_over_local": "extra average-latency slowdown vs local",
            "p99_slowdown_over_local": "extra p99-latency slowdown vs local",
            "throughput_fraction_over_local": "TPS fraction vs local",
        }
    )
    grid = sns.relplot(
        data=long,
        x="basebackup_streams",
        y="value",
        hue="mode_label",
        row="metric",
        col="clients_label",
        kind="line",
        marker="o",
        linewidth=2.1,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=2.9,
        aspect=1.15,
    )
    grid.set_axis_labels("Concurrent pg_basebackup streams", "")
    grid.set_titles("{row_name}\n{col_name}")
    for ax in grid.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["basebackup_streams"].unique()))
    grid.figure.suptitle("Replicated commits degrade beyond the local primary-side control", y=1.02)
    save_grid(grid, output_dir / "basebackup_local_normalized.png")


def plot_by_measured_bandwidth(df: pd.DataFrame, output_dir: Path) -> None:
    grid = sns.relplot(
        data=df,
        x="node0_tx_gbps",
        y="p99_ms",
        hue="mode_label",
        col="clients_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.0,
        aspect=1.15,
    )
    grid.set_axis_labels("Measured node-0 TX bandwidth (Gbps)", "p99 latency (ms, log scale)")
    grid.set_titles("{col_name}")
    for ax in grid.axes.flat:
        ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
    grid.figure.suptitle("Latency versus measured database-originated network load", y=1.02)
    save_grid(grid, output_dir / "basebackup_p99_by_measured_bandwidth.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_csv", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    output_dir = args.output_dir or args.results_csv.parent / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)

    sns.set_theme(context="paper", style="whitegrid", palette="colorblind")
    df = add_ratios(load_frame(args.results_csv))
    df.to_csv(output_dir / "normalized_rows.csv", index=False)
    write_summary(df, output_dir)
    plot_absolute_latency(df, output_dir)
    plot_raw_slowdown(df, output_dir)
    plot_local_normalized(df, output_dir)
    plot_by_measured_bandwidth(df, output_dir)
    print(f"wrote plots to {output_dir}")


if __name__ == "__main__":
    main()
