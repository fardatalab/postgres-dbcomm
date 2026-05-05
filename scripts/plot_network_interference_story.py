#!/usr/bin/env python3
"""Make focused seaborn plots for the network-interference benchmark story."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


MODE_ORDER = ["local", "on", "remote_write", "remote_apply"]
CLIENT_ORDER = [1, 8, 16]
DATASET_ORDER = ["vanilla", "citus"]
DATASET_LABELS = {
    "vanilla": "Vanilla PostgreSQL",
    "citus": "Citus distributed",
}
MODE_LABELS = {
    "local": "local",
    "on": "on",
    "remote_write": "remote_write",
    "remote_apply": "remote_apply",
}


def load_frame(path: Path, dataset: str) -> pd.DataFrame:
    """Load one benchmark CSV and attach a dataset label."""
    df = pd.read_csv(path)
    df["dataset"] = dataset
    df["background_level"] = df["background_level"].astype(int)
    df["clients"] = df["clients"].astype(int)
    df["repeat"] = df["repeat"].astype(int)
    df["mode"] = pd.Categorical(df["mode"], categories=MODE_ORDER, ordered=True)
    df["dataset"] = pd.Categorical(df["dataset"], categories=DATASET_ORDER, ordered=True)
    return df


def add_baseline_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Add per-repeat ratios relative to matching no-background rows."""
    keys = ["dataset", "clients", "mode", "repeat"]
    baseline = (
        df[df["background_level"] == 0][[*keys, "p99_ms", "throughput_tps"]]
        .rename(columns={"p99_ms": "baseline_p99_ms", "throughput_tps": "baseline_tps"})
    )
    out = df.merge(baseline, on=keys, how="left")
    out["p99_slowdown"] = out["p99_ms"] / out["baseline_p99_ms"]
    out["throughput_fraction"] = out["throughput_tps"] / out["baseline_tps"]
    out["dataset_label"] = out["dataset"].astype(str).map(DATASET_LABELS)
    out["mode_label"] = out["mode"].astype(str).map(MODE_LABELS)
    out["clients_label"] = out["clients"].map(lambda value: f"{value} client" if value == 1 else f"{value} clients")
    return out


def save_lineplot(g, path: Path, *, legend_columns: int = 4) -> None:
    """Save a FacetGrid or relplot result in png/pdf formats."""
    if g.legend is not None:
        sns.move_legend(
            g,
            "lower center",
            bbox_to_anchor=(0.5, -0.01),
            ncol=legend_columns,
            frameon=False,
            title=None,
        )
        for text in g.legend.texts:
            if text.get_text() == "clients_label":
                text.set_text("clients")
            elif text.get_text() == "mode_label":
                text.set_text("synchronous_commit")
    g.figure.tight_layout(rect=(0, 0.08, 1, 0.96))
    g.figure.savefig(path, dpi=220, bbox_inches="tight")
    g.figure.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(g.figure)


def plot_absolute_p99(df: pd.DataFrame, output_dir: Path) -> None:
    """Plot absolute p99 latency with one facet per dataset/client-count."""
    g = sns.relplot(
        data=df,
        x="background_level",
        y="p99_ms",
        hue="mode_label",
        col="clients_label",
        row="dataset_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.0,
        aspect=1.15,
    )
    g.set_axis_labels("Background reader/writer sessions", "p99 latency (ms, log scale)")
    g.set_titles("{row_name}\n{col_name}")
    for ax in g.axes.flat:
        ax.set_yscale("log")
        ax.grid(True, which="both", axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["background_level"].unique()))
    g.figure.suptitle("Transaction p99 latency rises with background network traffic", y=1.02)
    save_lineplot(g, output_dir / "story_p99_absolute.png")


def plot_p99_slowdown(df: pd.DataFrame, output_dir: Path) -> None:
    """Plot p99 latency normalized to the same run shape with no background traffic."""
    g = sns.relplot(
        data=df,
        x="background_level",
        y="p99_slowdown",
        hue="mode_label",
        col="clients_label",
        row="dataset_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.0,
        aspect=1.15,
    )
    g.set_axis_labels("Background reader/writer sessions", "p99 latency / no-background baseline")
    g.set_titles("{row_name}\n{col_name}")
    for ax in g.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["background_level"].unique()))
    g.figure.suptitle("All-else-equal p99 latency slowdown", y=1.02)
    save_lineplot(g, output_dir / "story_p99_slowdown.png")


def plot_replication_mode_focus(df: pd.DataFrame, output_dir: Path, mode: str) -> None:
    """Plot one replicated sync mode for latency and throughput."""
    mode_df = df[df["mode"].astype(str) == mode].copy()
    long = mode_df.melt(
        id_vars=["dataset_label", "clients", "clients_label", "background_level", "repeat"],
        value_vars=["p99_slowdown", "throughput_fraction"],
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(
        {
            "p99_slowdown": "p99 latency / baseline",
            "throughput_fraction": "TPS / baseline",
        }
    )

    g = sns.relplot(
        data=long,
        x="background_level",
        y="value",
        hue="clients_label",
        col="metric",
        row="dataset_label",
        kind="line",
        marker="o",
        linewidth=2.1,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.1,
        aspect=1.25,
    )
    g.set_axis_labels("Background reader/writer sessions", "")
    g.set_titles("{row_name}\n{col_name}")
    for ax in g.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["background_level"].unique()))
    g.figure.suptitle(f"{mode}: latency increases while throughput falls", y=1.02)
    save_lineplot(g, output_dir / f"story_{mode}_focus.png", legend_columns=3)


def plot_replication_modes_comparison(df: pd.DataFrame, output_dir: Path) -> None:
    """Compare remote_write and remote_apply in the same normalized view."""
    replicated = df[df["mode"].astype(str).isin(["remote_write", "remote_apply"])].copy()
    long = replicated.melt(
        id_vars=["dataset_label", "clients", "clients_label", "mode_label", "background_level", "repeat"],
        value_vars=["p99_slowdown", "throughput_fraction"],
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(
        {
            "p99_slowdown": "p99 latency / baseline",
            "throughput_fraction": "TPS / baseline",
        }
    )

    g = sns.relplot(
        data=long,
        x="background_level",
        y="value",
        hue="clients_label",
        style="mode_label",
        col="metric",
        row="dataset_label",
        kind="line",
        markers=True,
        dashes=True,
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=3.2,
        aspect=1.35,
    )
    g.set_axis_labels("Background reader/writer sessions", "")
    g.set_titles("{row_name}\n{col_name}")
    for ax in g.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(df["background_level"].unique()))
    g.figure.suptitle("remote_write vs remote_apply under background traffic", y=1.02)
    save_lineplot(g, output_dir / "story_replication_modes_comparison.png", legend_columns=5)


def plot_slowdown_heatmap(df: pd.DataFrame, output_dir: Path) -> None:
    """Heatmap the p99 slowdown at the highest background level."""
    high_bg = int(df["background_level"].max())
    summary = (
        df[df["background_level"] == high_bg]
        .groupby(["dataset_label", "clients", "mode_label"], observed=True)["p99_slowdown"]
        .mean()
        .reset_index()
    )

    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), dpi=220, sharey=True)
    vmax = summary["p99_slowdown"].max()
    for ax, dataset_label in zip(axes, [DATASET_LABELS["vanilla"], DATASET_LABELS["citus"]], strict=True):
        pivot = (
            summary[summary["dataset_label"] == dataset_label]
            .pivot(index="mode_label", columns="clients", values="p99_slowdown")
            .reindex([MODE_LABELS[mode] for mode in MODE_ORDER])
            .reindex(CLIENT_ORDER, axis=1)
        )
        sns.heatmap(
            pivot,
            ax=ax,
            annot=True,
            fmt=".1f",
            cmap="rocket_r",
            vmin=1.0,
            vmax=vmax,
            cbar=ax is axes[-1],
            cbar_kws={"label": "p99 slowdown (x)"},
        )
        ax.set_title(dataset_label)
        ax.set_xlabel("clients")
        ax.set_ylabel("synchronous_commit" if ax is axes[0] else "")

    fig.suptitle(f"p99 slowdown at background level {high_bg}", y=1.04)
    fig.tight_layout()
    fig.savefig(output_dir / "story_p99_slowdown_heatmap.png", dpi=220, bbox_inches="tight")
    fig.savefig(output_dir / "story_p99_slowdown_heatmap.pdf", bbox_inches="tight")
    plt.close(fig)


def write_summary(df: pd.DataFrame, output_dir: Path) -> None:
    """Write the aggregated numbers behind the story plots."""
    summary = (
        df.groupby(["dataset", "clients", "mode", "background_level"], observed=True)
        .agg(
            p99_ms_mean=("p99_ms", "mean"),
            p99_ms_std=("p99_ms", "std"),
            p99_slowdown_mean=("p99_slowdown", "mean"),
            p99_slowdown_std=("p99_slowdown", "std"),
            throughput_tps_mean=("throughput_tps", "mean"),
            throughput_tps_std=("throughput_tps", "std"),
            throughput_fraction_mean=("throughput_fraction", "mean"),
            throughput_fraction_std=("throughput_fraction", "std"),
            repeats=("repeat", "count"),
        )
        .reset_index()
    )
    summary.to_csv(output_dir / "story_summary.csv", index=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot focused network-interference figures with seaborn.")
    parser.add_argument("--citus-csv", type=Path, required=True)
    parser.add_argument("--vanilla-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    sns.set_theme(
        context="paper",
        style="whitegrid",
        palette=sns.color_palette("colorblind"),
        font_scale=1.05,
    )

    df = pd.concat(
        [
            load_frame(args.vanilla_csv, "vanilla"),
            load_frame(args.citus_csv, "citus"),
        ],
        ignore_index=True,
    )
    df = df[df["workload"] == "client-private-table"].copy()
    df = add_baseline_ratios(df)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    plot_absolute_p99(df, args.output_dir)
    plot_p99_slowdown(df, args.output_dir)
    plot_replication_mode_focus(df, args.output_dir, "remote_write")
    plot_replication_mode_focus(df, args.output_dir, "remote_apply")
    plot_replication_modes_comparison(df, args.output_dir)
    plot_slowdown_heatmap(df, args.output_dir)
    write_summary(df, args.output_dir)

    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
