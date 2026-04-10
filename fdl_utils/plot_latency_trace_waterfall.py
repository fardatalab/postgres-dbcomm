#!/usr/bin/env python3
"""plot_latency_trace_waterfall.py

Plot one coordinator transaction trace as a swimlane/waterfall PDF.

The figure combines:
- coordinator-local ordered trace stages
- coordinator-observed remote envelope stages per worker session
- matched worker-side actual work bars nested approximately inside those
  envelopes

This complements the scenario-level composition plot by making control-path
"bubbles" visible within one concrete transaction.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import seaborn as sns

from parse_latency_trace_csv import (
    ACTUAL_TIMER_CANDIDATES,
    RESULT_TIMER_CANDIDATES,
    StageInstance,
    TraceBlock,
    WorkerTimingBlock,
    match_worker_artifacts,
    parse_latency_trace_blocks,
    parse_worker_timing_blocks,
    pick_timer_total,
)


COORDINATOR_STAGES: Tuple[str, ...] = (
    "backend_spawn",
    "client_session_establish",
    "client_command_receive",
    "backend_parse_plan",
    "placement_bind",
    "remote_command_dispatch",
    "client_command_complete",
    "client_ready_for_query",
)

WORKER_ENVELOPE_STAGES: Tuple[str, ...] = (
    "worker_session_acquire",
    "remote_tx_attach",
    "remote_command_flush",
    "remote_command_wait",
    "remote_result_drain",
    "remote_tx_commit",
    "remote_tx_abort",
    "remote_tx_prepare",
    "worker_session_release",
)

SHORT_STAGE_LABELS: Dict[str, str] = {
    "backend_spawn": "backend spawn",
    "client_session_establish": "auth/startup",
    "client_command_receive": "client recv",
    "backend_parse_plan": "parse/plan",
    "placement_bind": "placement bind",
    "remote_command_dispatch": "dispatch",
    "remote_command_flush": "flush",
    "client_command_complete": "cmd complete",
    "client_ready_for_query": "ready",
    "worker_session_acquire": "session acquire",
    "remote_tx_attach": "tx attach",
    "remote_command_wait": "query envelope",
    "remote_result_drain": "result envelope",
    "remote_tx_commit": "tx commit",
    "remote_tx_abort": "tx abort",
    "remote_tx_prepare": "tx prepare",
    "worker_session_release": "session release",
}

COLORS: Dict[str, str] = {
    "coordinator": "#4F6D7A",
    "client_envelope": "#C8B8A8",
    "client": "#8A817C",
    "worker_envelope": "#AFC5D6",
    "worker_lifecycle_actual": "#52796F",
    "worker_query_actual": "#A26769",
    "worker_result_actual": "#7D8F69",
}

ALPHAS: Dict[str, float] = {
    "coordinator": 0.92,
    "client_envelope": 0.32,
    "client": 0.92,
    "worker_envelope": 0.38,
    "worker_lifecycle_actual": 0.95,
    "worker_query_actual": 0.95,
    "worker_result_actual": 0.95,
}

LABEL_THRESHOLD_MS = 0.12


@dataclass
class PlotSegment:
    """One drawable bar on the swimlane figure."""

    lane: str
    start_ns: int
    duration_ns: int
    label: str
    color_key: str
    is_actual: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot one traced transaction as a swimlane/waterfall PDF."
    )
    parser.add_argument("coordinator_log", help="Coordinator PostgreSQL logfile")
    parser.add_argument(
        "--worker-log",
        action="append",
        default=[],
        help="Worker PostgreSQL logfile(s). Repeat for multiple worker nodes.",
    )
    parser.add_argument(
        "--row-index",
        type=int,
        default=0,
        help="Coordinator trace row index to plot. Negative values count from the end.",
    )
    parser.add_argument("-o", "--output", required=True, help="Output PDF path")
    parser.add_argument(
        "--title",
        default=None,
        help="Optional plot title. Defaults to row index plus preceding query.",
    )
    return parser.parse_args()


def load_traces_and_worker_artifacts(
    coordinator_log: Path, worker_logs: Sequence[Path]
) -> Tuple[List[TraceBlock], Dict[int, List[WorkerTimingBlock]], Dict[Tuple[int, int], List[TraceBlock]]]:
    """Parse logs and build reusable coordinator-worker match structures."""

    coordinator_traces = parse_latency_trace_blocks(coordinator_log)
    worker_timing_blocks: List[WorkerTimingBlock] = []
    worker_trace_blocks: List[TraceBlock] = []

    for worker_log in worker_logs:
        worker_timing_blocks.extend(parse_worker_timing_blocks(worker_log))
        worker_trace_blocks.extend(parse_latency_trace_blocks(worker_log))

    matched_worker_timings_by_row, matched_worker_traces_by_trace_session = match_worker_artifacts(
        coordinator_traces, worker_timing_blocks, worker_trace_blocks
    )
    return coordinator_traces, matched_worker_timings_by_row, matched_worker_traces_by_trace_session


def select_target_trace(traces: List[TraceBlock], row_index: int) -> TraceBlock:
    """Select one coordinator trace row, allowing Python-style negative indexing."""

    if not traces:
        raise SystemExit("no coordinator traces found")

    if row_index < 0:
        target = traces[row_index]
    else:
        by_row_index = {trace.row_index: trace for trace in traces}
        if row_index not in by_row_index:
            raise SystemExit(f"row index {row_index} not found in coordinator trace log")
        target = by_row_index[row_index]

    return target


def centered_nested_start(envelope: StageInstance, nested_duration_ns: int) -> int:
    """Place one approximate nested bar centered within an envelope stage."""

    if nested_duration_ns >= envelope.duration_ns:
        return envelope.start_offset_ns

    return envelope.start_offset_ns + ((envelope.duration_ns - nested_duration_ns) // 2)


def clipped_duration(envelope: StageInstance, requested_duration_ns: int) -> int:
    """Keep approximate nested bars inside the coordinator envelope."""

    return max(0, min(requested_duration_ns, envelope.duration_ns))


def first_matching_stage(
    trace: TraceBlock, stage_name: str, remote_session_id: int, remote_command_id: Optional[str] = None
) -> Optional[StageInstance]:
    """Find one stage instance by stage/session/command."""

    for stage in trace.stage_instances:
        if stage.stage_name != stage_name:
            continue
        if stage.remote_session_id != remote_session_id:
            continue
        if remote_command_id is not None and stage.remote_command_id != remote_command_id:
            continue
        return stage

    return None


def build_coordinator_segments(trace: TraceBlock) -> List[PlotSegment]:
    """Build the coordinator lane from local/backend/control stages."""

    segments: List[PlotSegment] = []
    startup_begin_ns: Optional[int] = None
    startup_end_ns: Optional[int] = None
    first_parse_plan_start_ns: Optional[int] = None

    for stage in trace.stage_instances:
        if stage.stage_name == "backend_spawn" and startup_begin_ns is None:
            startup_begin_ns = stage.start_offset_ns
        if stage.stage_name == "backend_parse_plan":
            if first_parse_plan_start_ns is None or stage.start_offset_ns < first_parse_plan_start_ns:
                first_parse_plan_start_ns = stage.start_offset_ns

    if startup_begin_ns is not None:
        for stage in trace.stage_instances:
            if stage.stage_name != "client_ready_for_query":
                continue
            if first_parse_plan_start_ns is not None and stage.end_offset_ns > first_parse_plan_start_ns:
                continue
            startup_end_ns = stage.end_offset_ns

        if startup_end_ns is not None and startup_end_ns > startup_begin_ns:
            segments.append(
                PlotSegment(
                    lane="Coordinator",
                    start_ns=startup_begin_ns,
                    duration_ns=startup_end_ns - startup_begin_ns,
                    label="startup envelope",
                    color_key="client_envelope",
                    is_actual=False,
                )
            )

    for stage in trace.stage_instances:
        if stage.stage_name not in COORDINATOR_STAGES:
            continue

        color_key = "coordinator"
        if stage.stage_name in (
            "backend_spawn",
            "client_session_establish",
            "client_command_receive",
            "client_command_complete",
            "client_ready_for_query",
        ):
            color_key = "client"

        segments.append(
            PlotSegment(
                lane="Coordinator",
                start_ns=stage.start_offset_ns,
                duration_ns=stage.duration_ns,
                label=SHORT_STAGE_LABELS.get(stage.stage_name, stage.stage_name),
                color_key=color_key,
                is_actual=False,
            )
        )

    return segments


def build_worker_envelope_segments(trace: TraceBlock) -> List[PlotSegment]:
    """Build one envelope lane per remote session used by the transaction."""

    segments: List[PlotSegment] = []
    for stage in trace.stage_instances:
        if stage.stage_name not in WORKER_ENVELOPE_STAGES:
            continue

        segments.append(
            PlotSegment(
                lane=f"Worker rsid={stage.remote_session_id}",
                start_ns=stage.start_offset_ns,
                duration_ns=stage.duration_ns,
                label=SHORT_STAGE_LABELS.get(stage.stage_name, stage.stage_name),
                color_key="worker_envelope",
                is_actual=False,
            )
        )

    return segments


def build_worker_setup_actual_segments(
    trace: TraceBlock,
    matched_worker_traces_by_trace_session: Dict[Tuple[int, int], List[TraceBlock]],
) -> List[PlotSegment]:
    """Approximate worker startup/auth placement inside worker_session_acquire."""

    segments: List[PlotSegment] = []
    seen_session_ids = set()

    for stage in trace.stage_instances:
        if stage.stage_name != "worker_session_acquire" or stage.remote_session_id == 0:
            continue
        if stage.remote_session_id in seen_session_ids:
            continue
        seen_session_ids.add(stage.remote_session_id)

        candidate_worker_traces = matched_worker_traces_by_trace_session.get(
            (trace.row_index, stage.remote_session_id), []
        )
        if not candidate_worker_traces:
            continue

        best_worker_trace = min(
            candidate_worker_traces,
            key=lambda worker_trace: abs(worker_trace.timestamp_ns - trace.timestamp_ns),
        )
        actual_stages = [
            worker_stage
            for worker_stage in best_worker_trace.stage_instances
            if worker_stage.stage_name in ("backend_spawn", "client_session_establish")
        ]
        if not actual_stages:
            continue

        actual_stages.sort(key=lambda worker_stage: worker_stage.start_offset_ns)
        first_actual_start = min(worker_stage.start_offset_ns for worker_stage in actual_stages)
        last_actual_end = max(worker_stage.end_offset_ns for worker_stage in actual_stages)
        actual_group_duration_ns = last_actual_end - first_actual_start
        group_start_ns = centered_nested_start(
            stage, clipped_duration(stage, actual_group_duration_ns)
        )

        for worker_stage in actual_stages:
            relative_start_ns = worker_stage.start_offset_ns - first_actual_start
            stage_duration_ns = clipped_duration(stage, worker_stage.duration_ns)
            segments.append(
                PlotSegment(
                    lane=f"Worker rsid={stage.remote_session_id}",
                    start_ns=group_start_ns + relative_start_ns,
                    duration_ns=stage_duration_ns,
                    label=SHORT_STAGE_LABELS.get(worker_stage.stage_name, worker_stage.stage_name),
                    color_key="worker_lifecycle_actual",
                    is_actual=True,
                )
            )

    return segments


def build_worker_command_actual_segments(
    trace: TraceBlock, matched_worker_timings: Sequence[WorkerTimingBlock]
) -> List[PlotSegment]:
    """Approximate worker command actual work inside coordinator envelopes."""

    segments: List[PlotSegment] = []

    for worker_block in matched_worker_timings:
        rsid = worker_block.parsed_tag.rsid
        rcid = worker_block.parsed_tag.rcid
        kind = worker_block.parsed_tag.kind
        lane = f"Worker rsid={rsid}"

        if kind == "task_query":
            wait_stage = first_matching_stage(trace, "remote_command_wait", rsid, rcid)
            wait_actual_ns = pick_timer_total(worker_block.timers, ACTUAL_TIMER_CANDIDATES)
            if wait_stage is not None and wait_actual_ns > 0:
                duration_ns = clipped_duration(wait_stage, wait_actual_ns)
                segments.append(
                    PlotSegment(
                        lane=lane,
                        start_ns=centered_nested_start(wait_stage, duration_ns),
                        duration_ns=duration_ns,
                        label="query actual",
                        color_key="worker_query_actual",
                        is_actual=True,
                    )
                )

            drain_stage = first_matching_stage(trace, "remote_result_drain", rsid, rcid)
            result_actual_ns = pick_timer_total(worker_block.timers, RESULT_TIMER_CANDIDATES)
            if drain_stage is not None and result_actual_ns > 0:
                duration_ns = clipped_duration(drain_stage, result_actual_ns)
                segments.append(
                    PlotSegment(
                        lane=lane,
                        start_ns=centered_nested_start(drain_stage, duration_ns),
                        duration_ns=duration_ns,
                        label="result actual",
                        color_key="worker_result_actual",
                        is_actual=True,
                    )
                )
        else:
            stage_name_map = {
                "tx_begin": "remote_tx_attach",
                "tx_commit": "remote_tx_commit",
                "tx_abort": "remote_tx_abort",
                "tx_prepare": "remote_tx_prepare",
            }
            target_stage_name = stage_name_map.get(kind)
            if target_stage_name is None:
                continue

            envelope_stage = first_matching_stage(trace, target_stage_name, rsid, rcid)
            actual_ns = pick_timer_total(worker_block.timers, ACTUAL_TIMER_CANDIDATES)
            if envelope_stage is None or actual_ns <= 0:
                continue

            duration_ns = clipped_duration(envelope_stage, actual_ns)
            segments.append(
                PlotSegment(
                    lane=lane,
                    start_ns=centered_nested_start(envelope_stage, duration_ns),
                    duration_ns=duration_ns,
                    label=f"{SHORT_STAGE_LABELS.get(target_stage_name, target_stage_name)} actual",
                    color_key="worker_lifecycle_actual",
                    is_actual=True,
                )
            )

    return segments


def build_segments(
    trace: TraceBlock,
    matched_worker_timings_by_row: Dict[int, List[WorkerTimingBlock]],
    matched_worker_traces_by_trace_session: Dict[Tuple[int, int], List[TraceBlock]],
) -> List[PlotSegment]:
    """Assemble all lanes for the selected transaction."""

    segments: List[PlotSegment] = []
    segments.extend(build_coordinator_segments(trace))
    segments.extend(build_worker_envelope_segments(trace))
    segments.extend(build_worker_setup_actual_segments(trace, matched_worker_traces_by_trace_session))
    segments.extend(
        build_worker_command_actual_segments(
            trace, matched_worker_timings_by_row.get(trace.row_index, [])
        )
    )
    return segments


def lane_order(segments: Sequence[PlotSegment]) -> List[str]:
    """Coordinator first, then worker lanes sorted by rsid."""

    worker_lanes = sorted(
        {
            segment.lane
            for segment in segments
            if segment.lane != "Coordinator"
        },
        key=lambda lane: int(lane.split("=", 1)[1]),
    )
    return ["Coordinator"] + worker_lanes


def maybe_label_bar(ax: plt.Axes, segment: PlotSegment, lane_y: float, max_end_ms: float) -> None:
    """Avoid unreadable text on tiny bars."""

    duration_ms = segment.duration_ns / 1_000_000.0
    if duration_ms < LABEL_THRESHOLD_MS and duration_ms < (0.04 * max_end_ms):
        return

    ax.text(
        (segment.start_ns + (segment.duration_ns / 2.0)) / 1_000_000.0,
        lane_y,
        segment.label,
        ha="center",
        va="center",
        fontsize=8.5,
        color="black",
        clip_on=True,
    )


def plot_segments(segments: Sequence[PlotSegment], title: str, output_path: Path) -> None:
    """Render the swimlane/waterfall PDF."""

    if not segments:
        raise SystemExit("no segments available to plot")

    sns.set_theme(style="whitegrid")
    lanes = lane_order(segments)
    lane_to_y = {lane: float(len(lanes) - 1 - idx) for idx, lane in enumerate(lanes)}
    fig_height = max(3.8, 1.4 + 1.05 * len(lanes))
    fig, ax = plt.subplots(figsize=(15, fig_height))

    max_end_ns = max(segment.start_ns + segment.duration_ns for segment in segments)
    max_end_ms = max_end_ns / 1_000_000.0

    envelope_height = 0.58
    actual_height = 0.30

    for segment in segments:
        lane_y = lane_to_y[segment.lane]
        bar_height = actual_height if segment.is_actual else envelope_height
        color = COLORS[segment.color_key]
        alpha = ALPHAS[segment.color_key]
        ax.barh(
            y=[lane_y],
            width=[segment.duration_ns / 1_000_000.0],
            left=[segment.start_ns / 1_000_000.0],
            height=bar_height,
            color=color,
            edgecolor="white",
            linewidth=0.8,
            alpha=alpha,
            zorder=3 if segment.is_actual else 2,
        )
        maybe_label_bar(ax, segment, lane_y, max_end_ms)

    ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel("Elapsed Time Within Traced Transaction (ms)")
    ax.set_yticks([lane_to_y[lane] for lane in lanes])
    ax.set_yticklabels(lanes)
    ax.set_xlim(0.0, max_end_ms * 1.03 if max_end_ms > 0 else 1.0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", linestyle="--", alpha=0.30, zorder=1)
    ax.grid(axis="y", visible=False)

    legend_items = [
        Patch(facecolor=COLORS["client_envelope"], edgecolor="white", alpha=ALPHAS["client_envelope"], label="Coordinator Startup Envelope"),
        Patch(facecolor=COLORS["coordinator"], edgecolor="white", alpha=ALPHAS["coordinator"], label="Coordinator Backend / Control"),
        Patch(facecolor=COLORS["client"], edgecolor="white", alpha=ALPHAS["client"], label="Coordinator / Client"),
        Patch(facecolor=COLORS["worker_envelope"], edgecolor="white", alpha=ALPHAS["worker_envelope"], label="Worker Envelope"),
        Patch(facecolor=COLORS["worker_lifecycle_actual"], edgecolor="white", alpha=ALPHAS["worker_lifecycle_actual"], label="Worker Lifecycle Actual"),
        Patch(facecolor=COLORS["worker_query_actual"], edgecolor="white", alpha=ALPHAS["worker_query_actual"], label="Worker Query Actual"),
        Patch(facecolor=COLORS["worker_result_actual"], edgecolor="white", alpha=ALPHAS["worker_result_actual"], label="Worker Result Actual"),
    ]
    ax.legend(
        handles=legend_items,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.17),
        ncol=3,
        frameon=False,
    )

    fig.tight_layout()
    fig.savefig(output_path, format="pdf", bbox_inches="tight")
    plt.close(fig)


def default_title(trace: TraceBlock) -> str:
    """Build a readable title when the caller does not provide one."""

    query = trace.preceding_query.strip()
    if len(query) > 90:
        query = query[:87] + "..."

    if query:
        return f"Transaction Waterfall: row {trace.row_index} | {query}"

    return f"Transaction Waterfall: row {trace.row_index}"


def main() -> int:
    args = parse_args()
    coordinator_log = Path(args.coordinator_log)
    worker_logs = [Path(worker_log) for worker_log in args.worker_log]

    if not coordinator_log.exists():
        raise SystemExit(f"coordinator logfile does not exist: {coordinator_log}")
    for worker_log in worker_logs:
        if not worker_log.exists():
            raise SystemExit(f"worker logfile does not exist: {worker_log}")

    coordinator_traces, matched_worker_timings_by_row, matched_worker_traces_by_trace_session = (
        load_traces_and_worker_artifacts(coordinator_log, worker_logs)
    )
    trace = select_target_trace(coordinator_traces, args.row_index)
    segments = build_segments(
        trace,
        matched_worker_timings_by_row,
        matched_worker_traces_by_trace_session,
    )
    plot_segments(segments, args.title or default_title(trace), Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
