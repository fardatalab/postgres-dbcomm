#!/usr/bin/env python3
"""Plot lineitem DPA benchmark results from the CSV summaries.

The figures are meant to make the main research points visible:

1. DPA worker scaling keeps improving through 512 workers, but host-observed
   throughput gains taper.
2. Row batching has a non-monotonic sweet spot because tiny batches pay enqueue
   overhead while huge batches reduce scheduling granularity.
3. Output/status working-set size and tuple-input working-set size affect
   different parts of the benchmark.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("/tmp") / "matplotlib-dpa-printtup"))

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


SCRIPT_DIR = Path(__file__).resolve().parent
RESULTS_DIR = SCRIPT_DIR / "results"
FIGURES_DIR = SCRIPT_DIR / "figures"

THREAD_CSV = RESULTS_DIR / "lineitem_100k_thread_scaling_summary.csv"
ROWS_PER_TASK_CSV = RESULTS_DIR / "lineitem_100k_rows_per_task_summary.csv"
WORKING_SET_CSV = RESULTS_DIR / "lineitem_100k_working_set_sweep_t256_summary.csv"
CACHE_WORKING_SET_CSV = RESULTS_DIR / "lineitem_100k_cache_working_set_sweep_t190_summary.csv"


def configure_style() -> None:
    """Use a restrained plotting style suitable for quick research notes."""

    sns.set_theme(
        context="paper",
        style="whitegrid",
        font_scale=1.15,
        rc={
            "figure.dpi": 140,
            "savefig.dpi": 220,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titleweight": "bold",
        },
    )


def save_figure(fig: plt.Figure, stem: str) -> None:
    """Save a figure as both PNG and PDF for slides and documents."""

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / f"{stem}.png", bbox_inches="tight")
    fig.savefig(FIGURES_DIR / f"{stem}.pdf", bbox_inches="tight")
    plt.close(fig)


def plot_thread_scaling(thread_df: pd.DataFrame) -> None:
    """Plot host-observed throughput and speedup as worker count rises."""

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.1), gridspec_kw={"width_ratios": [1.4, 1.0]})
    ax = axes[0]
    sns.lineplot(
        data=thread_df,
        x="threads",
        y="host_payload_throughput_mb_s",
        marker="o",
        linewidth=2.2,
        color="#1f77b4",
        ax=ax,
    )
    ax.set_xscale("log", base=2)
    ax.set_xticks(thread_df["threads"].tolist())
    ax.set_xticklabels([str(v) for v in thread_df["threads"]])
    ax.set_xlabel("DPA workers")
    ax.set_ylabel("Host-observed payload throughput (MB/s)")
    ax.set_title("Thread scaling tapers at high worker counts")
    ax.axvline(64, color="#777777", linestyle="--", linewidth=1)
    ax.text(66, 40, "64-worker\nbaseline", color="#555555", fontsize=9)
    ax.annotate(
        "408 MB/s at 512 workers",
        xy=(512, thread_df.loc[thread_df["threads"] == 512, "host_payload_throughput_mb_s"].iloc[0]),
        xytext=(150, 430),
        arrowprops={"arrowstyle": "->", "color": "#333333", "lw": 1},
        fontsize=9,
    )

    ax = axes[1]
    ideal = thread_df["threads"].astype(float)
    ideal = ideal / ideal.iloc[0]
    ax.plot(thread_df["threads"], ideal, color="#bbbbbb", linestyle="--", label="ideal speedup")
    sns.lineplot(
        data=thread_df,
        x="threads",
        y="speedup_vs_1_worker",
        marker="o",
        linewidth=2.2,
        color="#d95f02",
        label="measured speedup",
        ax=ax,
    )
    ax.set_xscale("log", base=2)
    ax.set_yscale("log", base=2)
    ax.set_xticks(thread_df["threads"].tolist())
    ax.set_xticklabels([str(v) for v in thread_df["threads"]], rotation=30)
    ax.set_xlabel("DPA workers")
    ax.set_ylabel("Speedup vs 1 worker")
    ax.set_title("Speedup diverges from ideal")
    ax.legend(frameon=False)
    save_figure(fig, "lineitem_thread_scaling")


def plot_rows_per_task(rows_df: pd.DataFrame) -> None:
    """Plot the row batching sweet spot and the CmdQ task count tradeoff."""

    rows_df = rows_df.copy()
    rows_df["rows_per_task_label"] = rows_df["rows_per_task"].astype(str)
    order = ["1", "4", "16", "64", "all"]
    rows_df["cmdq_tasks_m"] = rows_df["cmdq_tasks"] / 1_000_000.0

    fig, axes = plt.subplots(1, 2, figsize=(10.5, 4.1), gridspec_kw={"width_ratios": [1.25, 1.0]})
    palette = ["#8da0cb" if label != "4" else "#1b9e77" for label in order]

    ax = axes[0]
    sns.barplot(
        data=rows_df,
        x="rows_per_task_label",
        y="host_payload_throughput_mb_s",
        order=order,
        palette=palette,
        hue="rows_per_task_label",
        legend=False,
        ax=ax,
    )
    ax.set_xlabel("Rows serialized per CmdQ task")
    ax.set_ylabel("Host-observed payload throughput (MB/s)")
    ax.set_title("Row batching has a clear sweet spot")
    for patch, value in zip(ax.patches, rows_df.set_index("rows_per_task_label").loc[order, "host_payload_throughput_mb_s"]):
        ax.text(
            patch.get_x() + patch.get_width() / 2,
            patch.get_height() + 8,
            f"{value:.0f}",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    ax = axes[1]
    sns.lineplot(
        data=rows_df,
        x="rows_per_task_label",
        y="cmdq_tasks_m",
        sort=False,
        marker="o",
        linewidth=2.2,
        color="#7570b3",
        ax=ax,
    )
    ax.set_yscale("log")
    ax.set_xlabel("Rows serialized per CmdQ task")
    ax.set_ylabel("CmdQ tasks submitted (millions, log scale)")
    ax.set_title("Fewer tasks is not always faster")
    ax2 = ax.twinx()
    sns.lineplot(
        data=rows_df,
        x="rows_per_task_label",
        y="enqueue_pct_of_host_elapsed",
        sort=False,
        marker="s",
        linewidth=1.8,
        color="#e7298a",
        ax=ax2,
    )
    ax2.set_ylabel("Enqueue time (% of host elapsed)")
    ax2.grid(False)
    save_figure(fig, "lineitem_rows_per_task")


def plot_working_set_sweep(ws_df: pd.DataFrame) -> None:
    """Plot separated output/status and tuple-input working-set effects."""

    scenario_names = {
        "output_only": "vary output slots only",
        "input_only": "vary tuple input only",
        "both_equal": "vary both together",
    }
    ws_df = ws_df.copy()
    ws_df["scenario_label"] = ws_df["scenario"].map(scenario_names)

    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.3), gridspec_kw={"width_ratios": [1.45, 1.0]})
    ax = axes[0]
    sns.lineplot(
        data=ws_df,
        x="value",
        y="host_payload_throughput_mb_s",
        hue="scenario_label",
        style="scenario_label",
        markers=True,
        dashes=False,
        linewidth=2.2,
        ax=ax,
    )
    ax.set_xscale("log", base=2)
    values = sorted(ws_df["value"].unique())
    ax.set_xticks(values)
    ax.set_xticklabels([str(v) for v in values], rotation=30)
    ax.set_xlabel("Working-set rows varied")
    ax.set_ylabel("Host-observed payload throughput (MB/s)")
    ax.set_title("Output slots and tuple-input footprint behave differently")
    ax.legend(title="", frameon=False, loc="lower right")

    heat = ws_df.pivot(index="scenario_label", columns="value", values="host_payload_throughput_mb_s")
    heat = heat.loc[[scenario_names["output_only"], scenario_names["input_only"], scenario_names["both_equal"]]]
    ax = axes[1]
    sns.heatmap(
        heat,
        annot=True,
        fmt=".0f",
        cmap="viridis",
        cbar_kws={"label": "MB/s"},
        linewidths=0.5,
        linecolor="white",
        ax=ax,
    )
    ax.set_xlabel("Rows")
    ax.set_ylabel("")
    ax.set_title("Throughput matrix")
    save_figure(fig, "lineitem_working_set_sweep")


def plot_overview(thread_df: pd.DataFrame, rows_df: pd.DataFrame, ws_df: pd.DataFrame) -> None:
    """Create a compact one-page overview of the three benchmark stories."""

    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.0))

    ax = axes[0]
    sns.lineplot(
        data=thread_df,
        x="threads",
        y="host_payload_throughput_mb_s",
        marker="o",
        linewidth=2.0,
        color="#1f77b4",
        ax=ax,
    )
    ax.set_xscale("log", base=2)
    ax.set_xticks(thread_df["threads"].tolist())
    ax.set_xticklabels([str(v) for v in thread_df["threads"]], rotation=30)
    ax.set_xlabel("DPA workers")
    ax.set_ylabel("MB/s")
    ax.set_title("Scaling: gains taper")

    ax = axes[1]
    order = ["1", "4", "16", "64", "all"]
    rows_df = rows_df.copy()
    rows_df["rows_per_task_label"] = rows_df["rows_per_task"].astype(str)
    sns.barplot(
        data=rows_df,
        x="rows_per_task_label",
        y="host_payload_throughput_mb_s",
        order=order,
        color="#1b9e77",
        ax=ax,
    )
    ax.set_xlabel("Rows per CmdQ task")
    ax.set_ylabel("MB/s")
    ax.set_title("Batching: sweet spot at 4")

    ax = axes[2]
    scenario_names = {
        "output_only": "output only",
        "input_only": "input only",
        "both_equal": "both",
    }
    ws_df = ws_df.copy()
    ws_df["scenario_label"] = ws_df["scenario"].map(scenario_names)
    sns.lineplot(
        data=ws_df,
        x="value",
        y="host_payload_throughput_mb_s",
        hue="scenario_label",
        marker="o",
        linewidth=2.0,
        ax=ax,
    )
    ax.set_xscale("log", base=2)
    values = sorted(ws_df["value"].unique())
    ax.set_xticks(values)
    ax.set_xticklabels([str(v) for v in values], rotation=30)
    ax.set_xlabel("Working-set rows")
    ax.set_ylabel("MB/s")
    ax.set_title("Working set: output vs input")
    ax.legend(title="", frameon=False)

    save_figure(fig, "lineitem_benchmark_overview")


def plot_cache_working_set_sweep(cache_df: pd.DataFrame) -> None:
    """Plot the packed-DPA-input cache-locality sweep.

    This newer sweep keeps CmdQ task count fixed with static workers and varies
    the moving DPA output ring separately from the packed tuple-input footprint.
    The annotations show the per-worker output ring size and input row count so
    cache-sized configurations are visible without requiring another table.
    """

    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.2))

    output_df = cache_df[cache_df["scenario"] == "output_ring"].copy()
    output_df["ring_mb"] = output_df["ring_output_bytes"] / 1024.0 / 1024.0
    output_df["ring_kib_per_worker"] = output_df["ring_bytes_per_worker"] / 1024.0

    ax = axes[0]
    sns.lineplot(
        data=output_df,
        x="ring_mb",
        y="host_payload_throughput_mb_s",
        marker="o",
        linewidth=2.2,
        color="#1f77b4",
        ax=ax,
    )
    ax.set_xscale("log", base=4)
    ax.set_xlabel("Total DPA output ring (MiB, log)")
    ax.set_ylabel("Host-observed payload throughput (MB/s)")
    ax.set_title("Output ring size is mostly flat")
    for _, row in output_df.iterrows():
        ax.annotate(
            f"{row['ring_kib_per_worker']:.0f} KiB/worker",
            (row["ring_mb"], row["host_payload_throughput_mb_s"]),
            textcoords="offset points",
            xytext=(0, 6),
            ha="center",
            fontsize=7,
        )

    input_df = cache_df[cache_df["scenario"] == "input_rows"].copy()
    input_df["input_mb"] = input_df["input_working_set_bytes"] / 1024.0 / 1024.0

    ax = axes[1]
    sns.lineplot(
        data=input_df,
        x="input_mb",
        y="host_payload_throughput_mb_s",
        marker="o",
        linewidth=2.2,
        color="#d95f02",
        ax=ax,
    )
    ax.set_xscale("log", base=4)
    ax.set_xlabel("Packed DPA tuple input footprint (MiB, log)")
    ax.set_ylabel("Host-observed payload throughput (MB/s)")
    ax.set_title("Very small tuple input is slower here")
    for _, row in input_df.iterrows():
        ax.annotate(
            str(int(row["input_working_set_rows"])),
            (row["input_mb"], row["host_payload_throughput_mb_s"]),
            textcoords="offset points",
            xytext=(0, 6),
            ha="center",
            fontsize=7,
        )

    save_figure(fig, "lineitem_cache_working_set_sweep")


def main() -> None:
    """Load CSV summaries and generate all figures."""

    configure_style()

    thread_df = pd.read_csv(THREAD_CSV)
    rows_df = pd.read_csv(ROWS_PER_TASK_CSV)
    ws_df = pd.read_csv(WORKING_SET_CSV)
    cache_df = pd.read_csv(CACHE_WORKING_SET_CSV)

    plot_thread_scaling(thread_df)
    plot_rows_per_task(rows_df)
    plot_working_set_sweep(ws_df)
    plot_overview(thread_df, rows_df, ws_df)
    plot_cache_working_set_sweep(cache_df)

    print(f"wrote figures to {FIGURES_DIR}")


if __name__ == "__main__":
    main()
