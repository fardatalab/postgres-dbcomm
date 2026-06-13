#!/usr/bin/env python3
"""Plot Homer fixed-loop scheduler-motivation figures.

The measurements below are encoded from the warmed run bands recorded in
``homer_fixed_loop_baseline_plan_v3.md``.  The source note usually records
low/high bands rather than every raw run value, so the plots use band midpoints
with asymmetric error bars.  This intentionally keeps the paper figures honest:
the visual claim is about the observed performance region, not a fabricated
sample distribution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


OUT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Band:
    """Closed low/high measurement band for warmed runs."""

    low: float
    high: float

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def err_low(self) -> float:
        return self.mid - self.low

    @property
    def err_high(self) -> float:
        return self.high - self.mid


def degradation_band(numerator: Band, denominator: Band) -> Band:
    """Return the conservative ratio band numerator / denominator.

    For positive performance bands, the smallest ratio is low/high and the
    largest is high/low.  We use this for normalized TPS degradation and p99
    inflation in Figure 1.
    """

    return Band(numerator.low / denominator.high, numerator.high / denominator.low)


def add_range_errorbars(ax, rows: Iterable[dict], x_col: str, y_col: str, hue_col: str,
                        x_order: list[str], hue_order: list[str]) -> None:
    """Overlay asymmetric low/high-band error bars onto a seaborn barplot."""

    row_map = {
        (row[x_col], row[hue_col]): row
        for row in rows
    }

    # Seaborn creates one BarContainer per hue level, each ordered by x_order.
    for container, hue in zip(ax.containers, hue_order):
        for bar, x_value in zip(container, x_order):
            row = row_map[(x_value, hue)]
            x = bar.get_x() + bar.get_width() / 2.0
            y = row[y_col]
            ax.errorbar(
                x,
                y,
                yerr=[[row["err_low"]], [row["err_high"]]],
                fmt="none",
                ecolor="#222222",
                elinewidth=1.0,
                capsize=2.5,
                capthick=1.0,
                zorder=5,
            )


def save_figure(fig: plt.Figure, stem: str) -> None:
    """Save a figure in both raster and vector formats."""

    fig.savefig(OUT_DIR / f"{stem}.png", dpi=300, bbox_inches="tight")
    fig.savefig(OUT_DIR / f"{stem}.pdf", bbox_inches="tight")
    plt.close(fig)


def configure_style() -> None:
    """Use a compact, paper-friendly seaborn style."""

    sns.set_theme(
        context="paper",
        style="whitegrid",
        font="DejaVu Sans",
        rc={
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.labelsize": 9,
            "axes.titlesize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "legend.title_fontsize": 8,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        },
    )


def plot_figure1() -> pd.DataFrame:
    """Figure 1: default FixedLoop foreground/bulk interference."""

    pgbench_alone = {
        "c1": {
            "tps": Band(3535, 3786),
            "p99_ms": Band(0.294, 0.321),
        },
        "c4": {
            "tps": Band(8491, 8557),
            "p99_ms": Band(0.703, 0.711),
        },
    }
    mixed = {
        "c1": {
            "tps": Band(2097, 2215),
            "p99_ms": Band(1.396, 1.641),
        },
        "c4": {
            "tps": Band(4526, 4734),
            "p99_ms": Band(1.418, 1.495),
        },
    }
    basebackup = {
        "standalone": Band(4.07, 4.11),
        "+ c1 txn client": Band(4.18, 4.22),
        "+ c4 txn clients": Band(4.19, 4.26),
    }

    fg_rows = []
    for client in ["c1", "c4"]:
        tps_deg = degradation_band(pgbench_alone[client]["tps"], mixed[client]["tps"])
        p99_inf = degradation_band(mixed[client]["p99_ms"], pgbench_alone[client]["p99_ms"])
        for metric, band in [
            ("TPS", tps_deg),
            ("p99", p99_inf),
        ]:
            fg_rows.append(
                {
                    "client": client,
                    "metric": metric,
                    "value": band.mid,
                    "err_low": band.err_low,
                    "err_high": band.err_high,
                }
            )

    bb_rows = [
        {
            "condition": condition,
            "elapsed_s": band.mid,
            "err_low": band.err_low,
            "err_high": band.err_high,
        }
        for condition, band in basebackup.items()
    ]

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(7.2, 2.8), gridspec_kw={"width_ratios": [1.15, 1]})

    fg_df = pd.DataFrame(fg_rows)
    fg_palette = sns.color_palette("colorblind", 2)
    x_order = ["c1", "c4"]
    hue_order = ["TPS", "p99"]
    sns.barplot(
        data=fg_df,
        x="client",
        y="value",
        hue="metric",
        order=x_order,
        hue_order=hue_order,
        errorbar=None,
        palette=fg_palette,
        ax=ax0,
    )
    add_range_errorbars(ax0, fg_rows, "client", "value", "metric", x_order, hue_order)
    ax0.axhline(1.0, color="#444444", linewidth=0.9, linestyle="--")
    ax0.set_xlabel("# transactional clients")
    ax0.set_ylabel("x worse than txns-only")
    # Subplot title intentionally omitted for paper layout flexibility.
    ax0.legend(frameon=False, loc="upper right")

    bb_df = pd.DataFrame(bb_rows)
    sns.barplot(
        data=bb_df,
        x="condition",
        y="elapsed_s",
        order=["standalone", "+ c1 txn client", "+ c4 txn clients"],
        errorbar=None,
        color=sns.color_palette("colorblind")[2],
        ax=ax1,
    )
    for bar, row in zip(ax1.containers[0], bb_rows):
        x = bar.get_x() + bar.get_width() / 2.0
        ax1.errorbar(
            x,
            row["elapsed_s"],
            yerr=[[row["err_low"]], [row["err_high"]]],
            fmt="none",
            ecolor="#222222",
            elinewidth=1.0,
            capsize=2.5,
            capthick=1.0,
            zorder=5,
        )
    ax1.set_xlabel("")
    ax1.set_ylabel("Base backup elapsed time (s)")
    # Subplot title intentionally omitted for paper layout flexibility.
    ax1.set_ylim(0, 4.45)
    ax1.tick_params(axis="x", rotation=18)

    # Figure title intentionally omitted for paper layout flexibility.
    fig.tight_layout()
    save_figure(fig, "figure1_fixedloop_interference")

    return pd.concat(
        [
            fg_df.assign(figure="figure1a"),
            bb_df.rename(columns={"condition": "client", "elapsed_s": "value"}).assign(
                figure="figure1b",
                metric="basebackup elapsed",
            ),
        ],
        ignore_index=True,
        sort=False,
    )


def plot_figure2() -> pd.DataFrame:
    """Figure 2: WaitBatch spin budget sweep."""

    rows = [
        {
            "spin_limit": "0",
            "spin_limit_numeric": 0,
            "target": "256 KiB",
            "tps": Band(6677, 6877),
            "p99_ms": Band(0.868, 0.915),
            "basebackup_s": Band(4.54, 4.57),
            "kind": "256 KiB target",
        },
        {
            "spin_limit": "32",
            "spin_limit_numeric": 32,
            "target": "256 KiB",
            "tps": Band(6681, 6687),
            "p99_ms": Band(0.899, 0.903),
            "basebackup_s": Band(4.56, 4.57),
            "kind": "256 KiB target",
        },
        {
            "spin_limit": "128",
            "spin_limit_numeric": 128,
            "target": "256 KiB",
            "tps": Band(6307, 6448),
            "p99_ms": Band(0.937, 0.952),
            "basebackup_s": Band(4.17, 4.55),
            "kind": "256 KiB target",
        },
        {
            "spin_limit": "512",
            "spin_limit_numeric": 512,
            "target": "256 KiB",
            "tps": Band(6120, 6128),
            "p99_ms": Band(0.976, 0.982),
            "basebackup_s": Band(4.42, 4.58),
            "kind": "256 KiB target",
        },
        {
            "spin_limit": "2K",
            "spin_limit_numeric": 2048,
            "target": "8 MiB",
            "tps": Band(4976.535634, 5001.968275),
            "p99_ms": Band(1.153, 1.167),
            "basebackup_s": Band(5.54, 5.55),
            "kind": "8 MiB target diagnostic",
        },
        {
            "spin_limit": "8K",
            "spin_limit_numeric": 8192,
            "target": "8 MiB",
            "tps": Band(2705.176843, 2726.695411),
            "p99_ms": Band(2.009, 2.023),
            "basebackup_s": Band(14.86, 14.94),
            "kind": "8 MiB target diagnostic",
        },
        {
            "spin_limit": "16K",
            "spin_limit_numeric": 16384,
            "target": "8 MiB",
            "tps": Band(1268, 1634),
            "p99_ms": Band(3.392, 3.513),
            "basebackup_s": Band(25.02, 28.79),
            "kind": "8 MiB target diagnostic",
        },
    ]

    plot_rows = []
    for row in rows:
        for metric, band in [
            ("c4 TPS", row["tps"]),
            ("c4 p99 latency", row["p99_ms"]),
            ("basebackup elapsed", row["basebackup_s"]),
        ]:
            plot_rows.append(
                {
                    "spin_limit": row["spin_limit"],
                    "spin_limit_numeric": row["spin_limit_numeric"],
                    "target": row["target"],
                    "kind": row["kind"],
                    "metric": metric,
                    "value": band.mid,
                    "err_low": band.err_low,
                    "err_high": band.err_high,
                }
            )

    df = pd.DataFrame(plot_rows)
    x_order = [row["spin_limit"] for row in rows]
    x_positions = list(range(len(x_order)))
    x_map = dict(zip(x_order, x_positions))
    line_color = sns.color_palette("colorblind")[0]

    fig, axes = plt.subplots(1, 3, figsize=(8.6, 2.9), sharex=True)
    metrics = [
        ("c4 TPS", "Transaction TPS", (0, 7200)),
        ("c4 p99 latency", "Transaction p99 latency (ms)", (0, 3.9)),
        ("basebackup elapsed", "Base backup elapsed time (s)", (0, 30)),
    ]

    for ax, (metric, ylabel, ylim) in zip(axes, metrics):
        metric_df = df[df["metric"] == metric]
        sns.lineplot(
            data=metric_df,
            x="spin_limit",
            y="value",
            markers=True,
            sort=False,
            color=line_color,
            marker="o",
            ax=ax,
            legend=False,
        )
        for _, row in metric_df.iterrows():
            x = x_map[row["spin_limit"]]
            ax.errorbar(
                x,
                row["value"],
                yerr=[[row["err_low"]], [row["err_high"]]],
                fmt="none",
                ecolor="#222222",
                elinewidth=0.9,
                capsize=2.2,
                capthick=0.9,
                zorder=5,
            )
        ax.set_xticks(x_positions)
        ax.set_xticklabels(x_order)
        ax.set_xlabel("Fixed wait budget (spin iterations)")
        ax.set_ylabel(ylabel)
        ax.set_ylim(*ylim)
        # Subplot titles intentionally omitted for paper layout flexibility.
    # Figure title intentionally omitted for paper layout flexibility.
    fig.tight_layout()
    save_figure(fig, "figure2_waitbatch_spin_sweep")

    return df.assign(figure="figure2")


def plot_figure3() -> pd.DataFrame:
    """Figure 3: static FixedLoop tradeoff surface."""

    rows = [
        {
            "point": "8M Exh",
            "config": "Exh-8M",
            "basebackup_s": Band(4.19, 4.26),
            "tps": Band(4526, 4734),
            "p99_ms": Band(1.418, 1.495),
            "dx_tps": -34,
            "dy_tps": -16,
            "dx_p99": 8,
            "dy_p99": -14,
        },
        {
            "point": "64K Exh",
            "config": "Exh-64K",
            "basebackup_s": Band(4.18, 4.55),
            "tps": Band(6354, 6666),
            "p99_ms": Band(0.911, 0.952),
            "dx_tps": -50,
            "dy_tps": -2,
            "dx_p99": -42,
            "dy_p99": -18,
        },
        {
            "point": "64K Lim-S",
            "config": "LimS-64K",
            "basebackup_s": Band(4.55, 4.57),
            "tps": Band(6671, 6737),
            "p99_ms": Band(0.888, 0.906),
            "dx_tps": -48,
            "dy_tps": 10,
            "dx_p99": 8,
            "dy_p99": 8,
        },
        {
            "point": "1M Lim-M",
            "config": "LimM-1M",
            "basebackup_s": Band(3.91, 3.93),
            "tps": Band(4445, 4680),
            "p99_ms": Band(1.346, 1.880),
            "dx_tps": 8,
            "dy_tps": 0,
            "dx_p99": 8,
            "dy_p99": 9,
        },
        {
            "point": "8M Lim-L",
            "config": "LimL-8M",
            "basebackup_s": Band(4.24, 4.29),
            "tps": Band(4489, 4759),
            "p99_ms": Band(1.456, 2.089),
            "dx_tps": 8,
            "dy_tps": 0,
            "dx_p99": 8,
            "dy_p99": 7,
        },
    ]

    plot_rows = []
    for row in rows:
        plot_rows.append(
            {
                "point": row["point"],
                "config": row["config"],
                "basebackup_s": row["basebackup_s"].mid,
                "basebackup_err_low": row["basebackup_s"].err_low,
                "basebackup_err_high": row["basebackup_s"].err_high,
                "tps": row["tps"].mid,
                "tps_err_low": row["tps"].err_low,
                "tps_err_high": row["tps"].err_high,
                "p99_ms": row["p99_ms"].mid,
                "p99_err_low": row["p99_ms"].err_low,
                "p99_err_high": row["p99_ms"].err_high,
                "dx_tps": row["dx_tps"],
                "dx_p99": row["dx_p99"],
                "dy_tps": row["dy_tps"],
                "dy_p99": row["dy_p99"],
            }
        )
    df = pd.DataFrame(plot_rows)

    palette = dict(zip(df["point"], sns.color_palette("colorblind", len(df))))

    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(8.2, 3.45))

    sns.scatterplot(
        data=df,
        x="basebackup_s",
        y="tps",
        hue="point",
        s=62,
        palette=palette,
        ax=ax0,
        legend=False,
    )
    sns.scatterplot(
        data=df,
        x="basebackup_s",
        y="p99_ms",
        hue="point",
        s=62,
        palette=palette,
        ax=ax1,
        legend=False,
    )

    for _, row in df.iterrows():
        for ax, y_col, dy_col, dx_col in [
            (ax0, "tps", "dy_tps", "dx_tps"),
            (ax1, "p99_ms", "dy_p99", "dx_p99"),
        ]:
            ax.annotate(
                row["point"],
                xy=(row["basebackup_s"], row[y_col]),
                xytext=(row[dx_col], row[dy_col]),
                textcoords="offset points",
                fontsize=7.2,
                color="#222222",
            )

    best_bulk_only_s = 3.81
    txns_only_c4_tps = (8491 + 8557) / 2.0
    txns_only_c4_p99_ms = (0.703 + 0.711) / 2.0
    for ax in (ax0, ax1):
        ax.axvline(best_bulk_only_s, color="#555555", linewidth=1.25, linestyle="--", alpha=0.9)

    # Subplot title intentionally omitted for paper layout flexibility.
    ax0.set_xlabel("Base backup elapsed time (s), lower is better")
    ax0.set_ylabel("Mixed c4 pgbench TPS, higher is better")
    ax0.set_xlim(3.76, 4.68)
    ax0.set_ylim(4100, 8800)
    ax0.axhline(txns_only_c4_tps, color="#555555", linewidth=1.25, linestyle="--", alpha=0.9)
    ax0.text(4.66, txns_only_c4_tps + 75, "txns-only", ha="right", fontsize=8, fontweight="bold", color="#333333")
    ax0.annotate(
        "better",
        xy=(3.815, 8610),
        xytext=(3.91, 8320),
        arrowprops={"arrowstyle": "->", "lw": 1.2, "color": "#333333"},
        fontsize=8,
        fontweight="bold",
        color="#333333",
    )
    ax0.text(
        best_bulk_only_s + 0.012,
        0.06,
        "best bulk-only",
        transform=ax0.get_xaxis_transform(),
        rotation=90,
        va="bottom",
        ha="left",
        fontsize=8,
        fontweight="bold",
        color="#333333",
    )

    # Subplot title intentionally omitted for paper layout flexibility.
    ax1.set_xlabel("Base backup elapsed time (s), lower is better")
    ax1.set_ylabel("Mixed c4 p99 latency (ms), lower is better")
    ax1.set_xlim(3.76, 4.74)
    ax1.set_ylim(0.62, 2.20)
    ax1.axhline(txns_only_c4_p99_ms, color="#555555", linewidth=1.25, linestyle="--", alpha=0.9)
    ax1.text(4.66, txns_only_c4_p99_ms + 0.028, "txns-only", ha="right", fontsize=8, fontweight="bold", color="#333333")
    ax1.annotate(
        "better",
        xy=(3.82, 0.70),
        xytext=(3.96, 0.83),
        arrowprops={"arrowstyle": "->", "lw": 1.2, "color": "#333333"},
        fontsize=8,
        fontweight="bold",
        color="#333333",
    )

    # Figure title intentionally omitted for paper layout flexibility.
    fig.tight_layout()
    save_figure(fig, "figure3_static_tradeoff_surface")

    return df.assign(figure="figure3")


def main() -> None:
    configure_style()
    all_data = pd.concat(
        [
            plot_figure1(),
            plot_figure2(),
            plot_figure3(),
        ],
        ignore_index=True,
        sort=False,
    )
    all_data.to_csv(OUT_DIR / "homer_fixed_loop_figure_data.csv", index=False)


if __name__ == "__main__":
    main()
