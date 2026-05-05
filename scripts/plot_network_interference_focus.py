#!/usr/bin/env python3
"""Create dependency-free SVG plots for the network-interference benchmark."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


MODE_ORDER = ["local", "on", "remote_write", "remote_apply"]
MODE_LABELS = {
    "local": "local",
    "on": "on",
    "remote_write": "remote_write",
    "remote_apply": "remote_apply",
}
MODE_COLORS = {
    "local": "#4E79A7",
    "on": "#F28E2B",
    "remote_write": "#59A14F",
    "remote_apply": "#E15759",
}
CLIENT_ORDER = [1, 8, 16]
CLIENT_COLORS = {
    1: "#4E79A7",
    8: "#F28E2B",
    16: "#E15759",
}
DATASET_LABELS = {
    "citus": "Citus distributed",
    "vanilla": "Vanilla PostgreSQL",
}


@dataclass(frozen=True)
class Summary:
    mean: float
    std: float
    count: int


def load_rows(path: Path, dataset: str) -> list[dict[str, str]]:
    """Load benchmark rows and attach a short dataset name."""
    with path.open(newline="") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        row["dataset"] = dataset
        row["background_level"] = int(row["background_level"])
        row["clients"] = int(row["clients"])
        for metric in ("p99_ms", "throughput_tps"):
            row[metric] = float(row[metric])
    return rows


def summarize(rows: list[dict[str, str]], metric: str) -> dict[tuple[str, int, str, int], Summary]:
    """Return mean/std summaries keyed by dataset, clients, mode, and background level."""
    grouped: dict[tuple[str, int, str, int], list[float]] = defaultdict(list)
    for row in rows:
        grouped[(row["dataset"], row["clients"], row["mode"], row["background_level"])].append(float(row[metric]))

    summaries = {}
    for key, values in grouped.items():
        std = statistics.stdev(values) if len(values) > 1 else 0.0
        summaries[key] = Summary(statistics.mean(values), std, len(values))
    return summaries


def normalize_summaries(
    summaries: dict[tuple[str, int, str, int], Summary],
    *,
    higher_is_worse: bool,
) -> dict[tuple[str, int, str, int], Summary]:
    """Normalize each series to its no-background baseline."""
    normalized = {}
    for key, summary in summaries.items():
        dataset, clients, mode, background_level = key
        baseline = summaries[(dataset, clients, mode, 0)].mean
        if baseline <= 0:
            continue
        if higher_is_worse:
            mean = summary.mean / baseline
            std = summary.std / baseline
        else:
            mean = summary.mean / baseline
            std = summary.std / baseline
        normalized[(dataset, clients, mode, background_level)] = Summary(mean, std, summary.count)
    return normalized


def nice_ticks(y_min: float, y_max: float, *, log_scale: bool) -> list[float]:
    """Create readable y-axis ticks for a simple SVG plot."""
    if log_scale:
        start = math.floor(math.log10(max(y_min, 0.001)))
        end = math.ceil(math.log10(y_max))
        ticks = []
        for power in range(start, end + 1):
            for multiplier in (1, 2, 5):
                value = multiplier * (10**power)
                if y_min <= value <= y_max:
                    ticks.append(value)
        return ticks or [y_min, y_max]

    if y_max <= y_min:
        return [y_min]
    span = y_max - y_min
    raw_step = span / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = min((1, 2, 5, 10), key=lambda candidate: abs(candidate * magnitude - raw_step)) * magnitude
    first = math.ceil(y_min / step) * step
    ticks = []
    value = first
    while value <= y_max + step * 0.25:
        ticks.append(value)
        value += step
    return ticks or [y_min, y_max]


def fmt_tick(value: float) -> str:
    """Format axis ticks without visual noise."""
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if value >= 1:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def svg_text(x: float, y: float, text: str, *, size: int = 12, anchor: str = "middle", weight: str = "400") -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{escaped}</text>'
    )


def draw_panel(
    *,
    summaries: dict[tuple[str, int, str, int], Summary],
    dataset: str,
    client: int | None,
    modes: list[str],
    backgrounds: list[int],
    x: float,
    y: float,
    width: float,
    height: float,
    y_label: str,
    title: str,
    log_scale: bool,
    y_domain: tuple[float, float] | None = None,
    color_by: str = "mode",
) -> list[str]:
    """Draw one line-chart panel."""
    values = []
    for mode in modes:
        clients = CLIENT_ORDER if client is None else [client]
        for client_value in clients:
            for background in backgrounds:
                summary = summaries.get((dataset, client_value, mode, background))
                if summary is not None:
                    values.extend([max(summary.mean - summary.std, 0.0001), summary.mean + summary.std])

    if not values:
        return []
    if y_domain is None:
        y_min = min(values)
        y_max = max(values)
        if log_scale:
            y_min = max(0.001, 10 ** math.floor(math.log10(y_min)))
            y_max = 10 ** math.ceil(math.log10(y_max))
        else:
            y_min = 0.0 if y_min >= 0 else y_min
            y_max *= 1.12
    else:
        y_min, y_max = y_domain

    plot_left = x + 58
    plot_right = x + width - 18
    plot_top = y + 38
    plot_bottom = y + height - 48
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top
    x_min = min(backgrounds)
    x_max = max(backgrounds)

    def map_x(background: int) -> float:
        if x_max == x_min:
            return plot_left + plot_width / 2
        return plot_left + ((background - x_min) / (x_max - x_min)) * plot_width

    def map_y(value: float) -> float:
        value = min(max(value, y_min), y_max)
        if log_scale:
            pos = (math.log10(value) - math.log10(y_min)) / (math.log10(y_max) - math.log10(y_min))
        else:
            pos = (value - y_min) / (y_max - y_min)
        return plot_bottom - pos * plot_height

    elements = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" fill="#ffffff"/>',
        svg_text(x + width / 2, y + 20, title, size=13, weight="600"),
    ]

    for tick in nice_ticks(y_min, y_max, log_scale=log_scale):
        tick_y = map_y(tick)
        elements.append(
            f'<line x1="{plot_left:.1f}" y1="{tick_y:.1f}" x2="{plot_right:.1f}" y2="{tick_y:.1f}" '
            'stroke="#d8dde6" stroke-width="1"/>'
        )
        elements.append(svg_text(plot_left - 8, tick_y + 4, fmt_tick(tick), size=10, anchor="end"))

    for background in backgrounds:
        tick_x = map_x(background)
        elements.append(
            f'<line x1="{tick_x:.1f}" y1="{plot_bottom:.1f}" x2="{tick_x:.1f}" y2="{plot_bottom + 5:.1f}" '
            'stroke="#41464f" stroke-width="1"/>'
        )
        elements.append(svg_text(tick_x, plot_bottom + 20, str(background), size=10))

    elements.extend(
        [
            f'<line x1="{plot_left:.1f}" y1="{plot_bottom:.1f}" x2="{plot_right:.1f}" y2="{plot_bottom:.1f}" '
            'stroke="#41464f" stroke-width="1.1"/>',
            f'<line x1="{plot_left:.1f}" y1="{plot_top:.1f}" x2="{plot_left:.1f}" y2="{plot_bottom:.1f}" '
            'stroke="#41464f" stroke-width="1.1"/>',
            svg_text(plot_left + plot_width / 2, y + height - 12, "background reader/writer sessions", size=11),
            (
                f'<text x="{x + 14:.1f}" y="{plot_top + plot_height / 2:.1f}" font-size="11" '
                f'text-anchor="middle" transform="rotate(-90 {x + 14:.1f} {plot_top + plot_height / 2:.1f})">'
                f'{y_label}</text>'
            ),
        ]
    )

    series_clients = CLIENT_ORDER if client is None else [client]
    for mode in modes:
        for client_value in series_clients:
            points = []
            for background in backgrounds:
                summary = summaries.get((dataset, client_value, mode, background))
                if summary is None:
                    continue
                point_x = map_x(background)
                point_y = map_y(summary.mean)
                points.append((point_x, point_y, summary))
            if not points:
                continue

            color = MODE_COLORS[mode] if color_by == "mode" else CLIENT_COLORS[client_value]
            path = " ".join(f"{px:.1f},{py:.1f}" for px, py, _ in points)
            elements.append(
                f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.2" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )
            for point_x, point_y, summary in points:
                high_y = map_y(summary.mean + summary.std)
                low_y = map_y(max(summary.mean - summary.std, 0.0001))
                elements.append(
                    f'<line x1="{point_x:.1f}" y1="{high_y:.1f}" x2="{point_x:.1f}" y2="{low_y:.1f}" '
                    f'stroke="{color}" stroke-width="1.2"/>'
                )
                elements.append(
                    f'<line x1="{point_x - 4:.1f}" y1="{high_y:.1f}" x2="{point_x + 4:.1f}" y2="{high_y:.1f}" '
                    f'stroke="{color}" stroke-width="1.2"/>'
                )
                elements.append(
                    f'<line x1="{point_x - 4:.1f}" y1="{low_y:.1f}" x2="{point_x + 4:.1f}" y2="{low_y:.1f}" '
                    f'stroke="{color}" stroke-width="1.2"/>'
                )
                elements.append(f'<circle cx="{point_x:.1f}" cy="{point_y:.1f}" r="3.4" fill="{color}"/>')

    return elements


def write_svg(path: Path, width: int, height: int, elements: list[str]) -> None:
    """Write a standalone SVG file."""
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,Helvetica,sans-serif;fill:#252a31}</style>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        *elements,
        "</svg>",
    ]
    path.write_text("\n".join(svg) + "\n")


def legend(x: float, y: float, items: list[tuple[str, str]]) -> list[str]:
    """Draw a compact horizontal legend."""
    elements = []
    cursor = x
    for label, color in items:
        elements.append(f'<line x1="{cursor:.1f}" y1="{y:.1f}" x2="{cursor + 22:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="2.6"/>')
        elements.append(f'<circle cx="{cursor + 11:.1f}" cy="{y:.1f}" r="3.4" fill="{color}"/>')
        elements.append(svg_text(cursor + 30, y + 4, label, size=12, anchor="start"))
        cursor += 132
    return elements


def make_absolute_p99(rows: list[dict[str, str]], output_dir: Path) -> None:
    """Plot absolute p99 latency by dataset/client with sync-mode lines."""
    summaries = summarize(rows, "p99_ms")
    backgrounds = sorted({int(row["background_level"]) for row in rows})
    elements = [
        svg_text(720, 30, "p99 transaction latency under increasing background traffic", size=20, weight="700"),
        svg_text(720, 54, "client-private pgbench tables; points are mean +/- SD over repeats", size=13),
        *legend(450, 84, [(MODE_LABELS[mode], MODE_COLORS[mode]) for mode in MODE_ORDER]),
    ]
    panel_w = 440
    panel_h = 250
    for row_idx, dataset in enumerate(("citus", "vanilla")):
        elements.append(svg_text(42, 205 + row_idx * panel_h, DATASET_LABELS[dataset], size=14, anchor="start", weight="700"))
        for col_idx, client in enumerate(CLIENT_ORDER):
            elements.extend(
                draw_panel(
                    summaries=summaries,
                    dataset=dataset,
                    client=client,
                    modes=MODE_ORDER,
                    backgrounds=backgrounds,
                    x=50 + col_idx * panel_w,
                    y=105 + row_idx * panel_h,
                    width=panel_w,
                    height=panel_h,
                    y_label="p99 latency (ms, log)",
                    title=f"{client} client{'s' if client != 1 else ''}",
                    log_scale=True,
                    color_by="mode",
                )
            )
    write_svg(output_dir / "network_interference_p99_absolute.svg", 1420, 650, elements)


def make_normalized_p99(rows: list[dict[str, str]], output_dir: Path) -> None:
    """Plot p99 latency slowdown by dataset/client with sync-mode lines."""
    summaries = normalize_summaries(summarize(rows, "p99_ms"), higher_is_worse=True)
    backgrounds = sorted({int(row["background_level"]) for row in rows})
    elements = [
        svg_text(720, 30, "p99 latency slowdown relative to no-background baseline", size=20, weight="700"),
        svg_text(720, 54, "all points divide by background_level=0 for the same dataset, clients, and synchronous_commit mode", size=13),
        *legend(450, 84, [(MODE_LABELS[mode], MODE_COLORS[mode]) for mode in MODE_ORDER]),
    ]
    panel_w = 440
    panel_h = 250
    for row_idx, dataset in enumerate(("citus", "vanilla")):
        elements.append(svg_text(42, 205 + row_idx * panel_h, DATASET_LABELS[dataset], size=14, anchor="start", weight="700"))
        for col_idx, client in enumerate(CLIENT_ORDER):
            elements.extend(
                draw_panel(
                    summaries=summaries,
                    dataset=dataset,
                    client=client,
                    modes=MODE_ORDER,
                    backgrounds=backgrounds,
                    x=50 + col_idx * panel_w,
                    y=105 + row_idx * panel_h,
                    width=panel_w,
                    height=panel_h,
                    y_label="p99 slowdown (x)",
                    title=f"{client} client{'s' if client != 1 else ''}",
                    log_scale=False,
                    y_domain=None,
                    color_by="mode",
                )
            )
    write_svg(output_dir / "network_interference_p99_slowdown.svg", 1420, 650, elements)


def make_remote_apply_focus(rows: list[dict[str, str]], output_dir: Path) -> None:
    """Plot the highest-signal remote_apply case for latency and throughput."""
    p99 = normalize_summaries(summarize(rows, "p99_ms"), higher_is_worse=True)
    tps = normalize_summaries(summarize(rows, "throughput_tps"), higher_is_worse=False)
    backgrounds = sorted({int(row["background_level"]) for row in rows})
    elements = [
        svg_text(620, 30, "remote_apply focus: latency rises while throughput falls", size=20, weight="700"),
        svg_text(620, 54, "normalized to no-background baseline for the same dataset and client count", size=13),
        *legend(448, 84, [(f"{client} client{'s' if client != 1 else ''}", CLIENT_COLORS[client]) for client in CLIENT_ORDER]),
    ]
    panel_w = 570
    panel_h = 255
    for row_idx, dataset in enumerate(("citus", "vanilla")):
        elements.append(svg_text(38, 210 + row_idx * panel_h, DATASET_LABELS[dataset], size=14, anchor="start", weight="700"))
        elements.extend(
            draw_panel(
                summaries=p99,
                dataset=dataset,
                client=None,
                modes=["remote_apply"],
                backgrounds=backgrounds,
                x=50,
                y=105 + row_idx * panel_h,
                width=panel_w,
                height=panel_h,
                y_label="p99 slowdown (x)",
                title="p99 latency",
                log_scale=False,
                color_by="client",
            )
        )
        elements.extend(
            draw_panel(
                summaries=tps,
                dataset=dataset,
                client=None,
                modes=["remote_apply"],
                backgrounds=backgrounds,
                x=650,
                y=105 + row_idx * panel_h,
                width=panel_w,
                height=panel_h,
                y_label="TPS / baseline",
                title="throughput retention",
                log_scale=False,
                y_domain=(0.0, 1.2),
                color_by="client",
            )
        )
    write_svg(output_dir / "network_interference_remote_apply_focus.svg", 1260, 655, elements)


def write_summary_csv(rows: list[dict[str, str]], output_dir: Path) -> None:
    """Write a compact summarized CSV used by the SVG figures."""
    p99 = summarize(rows, "p99_ms")
    tps = summarize(rows, "throughput_tps")
    p99_norm = normalize_summaries(p99, higher_is_worse=True)
    tps_norm = normalize_summaries(tps, higher_is_worse=False)
    keys = sorted(p99)
    out_path = output_dir / "network_interference_summary.csv"
    with out_path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "dataset",
                "clients",
                "mode",
                "background_level",
                "p99_ms_mean",
                "p99_ms_std",
                "p99_slowdown",
                "throughput_tps_mean",
                "throughput_tps_std",
                "throughput_fraction",
                "repeat_count",
            ]
        )
        for key in keys:
            p99_summary = p99[key]
            tps_summary = tps[key]
            writer.writerow(
                [
                    *key,
                    f"{p99_summary.mean:.6f}",
                    f"{p99_summary.std:.6f}",
                    f"{p99_norm[key].mean:.6f}",
                    f"{tps_summary.mean:.6f}",
                    f"{tps_summary.std:.6f}",
                    f"{tps_norm[key].mean:.6f}",
                    p99_summary.count,
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create focused SVG plots for network-interference runs.")
    parser.add_argument("--citus-csv", type=Path, required=True)
    parser.add_argument("--vanilla-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    rows = load_rows(args.citus_csv, "citus") + load_rows(args.vanilla_csv, "vanilla")
    rows = [row for row in rows if row.get("workload") == "client-private-table"]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    make_absolute_p99(rows, args.output_dir)
    make_normalized_p99(rows, args.output_dir)
    make_remote_apply_focus(rows, args.output_dir)
    write_summary_csv(rows, args.output_dir)

    print(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
