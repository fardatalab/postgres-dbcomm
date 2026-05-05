#!/usr/bin/env python3
"""Plot the SQL background-generator negative result against basebackup."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


METRIC_LABELS = {
    "avg_slowdown_over_local": "extra average-latency slowdown vs local",
    "p99_slowdown_over_local": "extra p99-latency slowdown vs local",
    "throughput_fraction_over_local": "TPS fraction vs local",
}


def load_sql(path: Path, label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["mode"] == "remote_write"].copy()
    df["experiment"] = label
    df["background_kind"] = "SQL COPY/SELECT sessions"
    df["background_value"] = pd.to_numeric(df["background_level"])
    return normalize(df)


def load_basebackup(path: Path, label: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["mode"] == "remote_write"].copy()
    df["experiment"] = label
    df["background_kind"] = "pg_basebackup streams"
    df["background_value"] = pd.to_numeric(df["basebackup_streams"])
    return normalize(df)


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    for column in ("clients", "repeat", "latency_avg_ms", "p99_ms", "throughput_tps", "background_value"):
        df[column] = pd.to_numeric(df[column])

    keys = ["experiment", "clients", "repeat"]
    baseline = (
        df[df["background_value"] == 0][[*keys, "latency_avg_ms", "p99_ms", "throughput_tps"]]
        .rename(
            columns={
                "latency_avg_ms": "baseline_avg_ms",
                "p99_ms": "baseline_p99_ms",
                "throughput_tps": "baseline_tps",
            }
        )
    )
    out = df.merge(baseline, on=keys, how="left")
    out["avg_slowdown"] = out["latency_avg_ms"] / out["baseline_avg_ms"]
    out["p99_slowdown"] = out["p99_ms"] / out["baseline_p99_ms"]
    out["throughput_fraction"] = out["throughput_tps"] / out["baseline_tps"]
    return out


def add_local_control(remote: pd.DataFrame, local_sources: list[tuple[Path, str, str]]) -> pd.DataFrame:
    locals_frames = []
    for path, label, value_column in local_sources:
        df = pd.read_csv(path)
        df = df[df["mode"] == "local"].copy()
        df["experiment"] = label
        df["background_value"] = pd.to_numeric(df[value_column])
        locals_frames.append(normalize(df))

    local = pd.concat(locals_frames, ignore_index=True)[
        [
            "experiment",
            "clients",
            "repeat",
            "background_value",
            "avg_slowdown",
            "p99_slowdown",
            "throughput_fraction",
        ]
    ].rename(
        columns={
            "avg_slowdown": "local_avg_slowdown",
            "p99_slowdown": "local_p99_slowdown",
            "throughput_fraction": "local_throughput_fraction",
        }
    )
    out = remote.merge(local, on=["experiment", "clients", "repeat", "background_value"], how="left")
    out["avg_slowdown_over_local"] = out["avg_slowdown"] / out["local_avg_slowdown"]
    out["p99_slowdown_over_local"] = out["p99_slowdown"] / out["local_p99_slowdown"]
    out["throughput_fraction_over_local"] = out["throughput_fraction"] / out["local_throughput_fraction"]
    out["clients_label"] = out["clients"].map(lambda value: f"{value} client" if value == 1 else f"{value} clients")
    return out


def save_grid(grid: sns.FacetGrid, path: Path, *, bottom: float = 0.12) -> None:
    if grid.legend is not None:
        sns.move_legend(
            grid,
            "lower center",
            bbox_to_anchor=(0.5, -0.01),
            ncol=3,
            frameon=False,
            title=None,
        )
    grid.figure.tight_layout(rect=(0, bottom, 1, 0.96))
    grid.figure.savefig(path, dpi=220, bbox_inches="tight")
    grid.figure.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(grid.figure)


def plot_sql_lines(df: pd.DataFrame, output_dir: Path) -> None:
    sql = df[df["background_kind"] == "SQL COPY/SELECT sessions"].copy()
    long = sql.melt(
        id_vars=["experiment", "clients_label", "background_value", "repeat"],
        value_vars=list(METRIC_LABELS),
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(METRIC_LABELS)
    grid = sns.relplot(
        data=long,
        x="background_value",
        y="value",
        hue="experiment",
        row="metric",
        col="clients_label",
        kind="line",
        marker="o",
        linewidth=2.0,
        errorbar="sd",
        err_style="bars",
        facet_kws={"sharey": False},
        height=2.8,
        aspect=1.15,
    )
    grid.set_axis_labels("Background SQL reader/writer sessions", "")
    grid.set_titles("{row_name}\n{col_name}")
    for ax in grid.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.set_xticks(sorted(sql["background_value"].unique()))
    grid.figure.suptitle("UNLOGGED SQL background sinks largely remove the latency signal", y=1.02)
    save_grid(grid, output_dir / "sql_logged_vs_unlogged_local_normalized.png")


def plot_max_contrast(df: pd.DataFrame, output_dir: Path) -> None:
    max_rows = []
    for _, group in df.groupby("experiment"):
        max_rows.append(group[group["background_value"] == group["background_value"].max()])
    high = pd.concat(max_rows, ignore_index=True)
    long = high.melt(
        id_vars=["experiment", "clients_label", "repeat"],
        value_vars=list(METRIC_LABELS),
        var_name="metric",
        value_name="value",
    )
    long["metric"] = long["metric"].map(METRIC_LABELS)

    grid = sns.catplot(
        data=long,
        x="experiment",
        y="value",
        hue="experiment",
        row="metric",
        col="clients_label",
        kind="bar",
        errorbar="sd",
        dodge=False,
        sharey=False,
        height=2.8,
        aspect=1.1,
        legend=False,
    )
    grid.set_axis_labels("", "")
    grid.set_titles("{row_name}\n{col_name}")
    for ax in grid.axes.flat:
        ax.axhline(1.0, color="#30343b", linewidth=0.9, linestyle=":")
        ax.grid(True, axis="y", linestyle="--", alpha=0.35)
        ax.tick_params(axis="x", labelrotation=25)
    grid.figure.suptitle("Max-intensity contrast: WAL artifact, negative SQL result, physical backup signal", y=1.02)
    grid.figure.tight_layout(rect=(0, 0, 1, 0.96))
    path = output_dir / "max_intensity_negative_result_vs_basebackup.png"
    grid.figure.savefig(path, dpi=220, bbox_inches="tight")
    grid.figure.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(grid.figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql-logged", type=Path, required=True)
    parser.add_argument("--sql-unlogged", type=Path, required=True)
    parser.add_argument("--basebackup", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    labels = {
        "logged": "SQL logged sinks",
        "unlogged": "SQL UNLOGGED sinks",
        "basebackup": "pg_basebackup",
    }
    remote = pd.concat(
        [
            load_sql(args.sql_logged, labels["logged"]),
            load_sql(args.sql_unlogged, labels["unlogged"]),
            load_basebackup(args.basebackup, labels["basebackup"]),
        ],
        ignore_index=True,
    )
    df = add_local_control(
        remote,
        [
            (args.sql_logged, labels["logged"], "background_level"),
            (args.sql_unlogged, labels["unlogged"], "background_level"),
            (args.basebackup, labels["basebackup"], "basebackup_streams"),
        ],
    )
    df.to_csv(args.output_dir / "normalized_rows.csv", index=False)
    summary = (
        df.groupby(["experiment", "clients", "background_value"], observed=True)[
            [
                "avg_slowdown_over_local",
                "p99_slowdown_over_local",
                "throughput_fraction_over_local",
            ]
        ]
        .agg(["mean", "std", "count"])
        .reset_index()
    )
    summary.columns = ["_".join(str(part) for part in column if part) for column in summary.columns]
    summary.to_csv(args.output_dir / "summary.csv", index=False)

    sns.set_theme(context="paper", style="whitegrid", palette="colorblind")
    plot_sql_lines(df, args.output_dir)
    plot_max_contrast(df, args.output_dir)
    print(f"wrote plots to {args.output_dir}")


if __name__ == "__main__":
    main()
