#!/usr/bin/env python3
"""plot_latency_trace_waterfall.py

Plot one representative coordinator transaction trace as a swimlane/waterfall PDF.

The figure combines:
- coordinator-local ordered trace stages
- coordinator-observed remote envelope stages per worker session
- coordinator-side parent envelopes for each remote query round trip
- matched worker-side wait / protocol / local-processing / result-serde bars
  nested approximately inside those envelopes

This complements the scenario-level composition plot by making control-path
"bubbles" visible within one concrete transaction. By default, the selected
transaction is the median traced-span coordinator row so the figure stays
grounded in one real transaction while still using the full sweep to choose a
representative example.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import seaborn as sns

from parse_latency_trace_csv import (
    StageInstance,
    TraceBlock,
    WorkerTimingBlock,
    match_worker_artifacts,
    parse_latency_trace_blocks,
    parse_worker_timing_blocks,
    worker_local_processing_total_ns,
    worker_receive_protocol_total_ns,
    worker_result_serde_total_ns,
    worker_send_protocol_total_ns,
    worker_wait_total_ns,
)


COORDINATOR_STAGES: Tuple[str, ...] = (
    "backend_spawn",
    "client_session_establish",
    "client_session_teardown",
    "client_command_wait",
    "client_command_receive",
    "backend_parse_plan",
    "client_bind_complete_send",
    "client_describe_response_send",
    "client_result_send",
    "placement_bind",
    "remote_command_dispatch",
    "client_command_complete",
    "client_ready_for_query",
)

QUERY_REMOTE_ROUND_STAGES: Tuple[str, ...] = (
    "placement_bind",
    "remote_command_dispatch",
    "remote_command_flush",
    "remote_command_wait",
    "remote_result_drain",
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
    "client_session_teardown": "session teardown",
    "client_command_wait": "client wait",
    "client_command_receive": "client recv",
    "backend_parse_plan": "parse/plan",
    "client_bind_complete_send": "bind response",
    "client_describe_response_send": "describe response",
    "client_result_send": "result send",
    "placement_bind": "placement bind",
    "remote_command_dispatch": "dispatch",
    "remote_command_flush": "flush",
    "client_command_complete": "cmd complete",
    "client_ready_for_query": "ready",
    "worker_session_acquire": "session acquire",
    "remote_tx_attach": "BEGIN;",
    "remote_command_wait": "worker query",
    "remote_result_drain": "result drain",
    "remote_tx_commit": "remote commit",
    "remote_tx_abort": "remote abort",
    "remote_tx_prepare": "remote prepare",
    "worker_session_release": "session release",
}

COLORS: Dict[str, str] = {
    "coordinator": "#4F6D7A",
    "client_envelope": "#C8B8A8",
    "coordinator_session_actual": "#7C8A9F",
    "coordinator_teardown_actual": "#A05C68",
    "coordinator_command_envelope": "#D8D2C2",
    "coordinator_remote_envelope": "#C7D3E3",
    "coordinator_local_processing": "#D8A657",
    "client_wait": "#7D97AB",
    "client": "#8A817C",
    "worker_envelope": "#AFC5D6",
    "worker_lifecycle_actual": "#52796F",
    "worker_wait_actual": "#E9C46A",
    "worker_protocol_actual": "#4D908E",
    "worker_local_actual": "#A26769",
    "worker_result_serde_actual": "#7D8F69",
}

ALPHAS: Dict[str, float] = {
    "coordinator": 0.92,
    "client_envelope": 0.32,
    "coordinator_session_actual": 0.95,
    "coordinator_teardown_actual": 0.95,
    "coordinator_command_envelope": 0.18,
    "coordinator_remote_envelope": 0.26,
    "coordinator_local_processing": 0.50,
    "client_wait": 0.82,
    "client": 0.92,
    "worker_envelope": 0.38,
    "worker_lifecycle_actual": 0.95,
    "worker_wait_actual": 0.95,
    "worker_protocol_actual": 0.95,
    "worker_local_actual": 0.95,
    "worker_result_serde_actual": 0.95,
}

LABEL_THRESHOLD_MS = 0.12

PGBENCH_TPCB_STAGE_LABELS: Tuple[str, ...] = (
    "1. BEGIN",
    "2. Update accounts",
    "3. Select account",
    "4. Update tellers",
    "5. Update branches",
    "6. Insert history",
    "7. COMMIT / 2PC",
)

LEGEND_ITEM_ORDER: Tuple[Tuple[str, str], ...] = (
    ("client_envelope", "Coordinator Session Setup Envelope"),
    ("coordinator_session_actual", "Coordinator Session Setup Actual"),
    ("coordinator_teardown_actual", "Coordinator Session Teardown"),
    ("coordinator_command_envelope", "Coordinator Command Envelope"),
    ("coordinator_remote_envelope", "Coordinator Remote Round Envelope"),
    ("coordinator_local_processing", "Coordinator Local DB / Control (Derived)"),
    ("client_wait", "Client Wait"),
    ("coordinator", "Coordinator Comm / Control"),
    ("client", "Frontend Protocol / Replies"),
    ("worker_envelope", "Worker Envelope"),
    ("worker_lifecycle_actual", "Worker Session Setup Actual"),
    ("worker_wait_actual", "Worker Wait"),
    ("worker_protocol_actual", "Worker Protocol / Transport"),
    ("worker_result_serde_actual", "Worker Row Serde"),
    ("worker_local_actual", "Worker Local Processing"),
)


@dataclass
class PlotSegment:
    """One drawable bar on the swimlane figure."""

    lane: str
    start_ns: int
    duration_ns: int
    label: str
    color_key: str
    is_actual: bool
    default_label: bool = False
    label_y_offset: float = 0.0


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
        default=None,
        help=(
            "Coordinator trace row index to plot. Negative values count from the end. "
            "If omitted, the script plots the median trace_total_ns row."
        ),
    )
    parser.add_argument(
        "--first-pgbench-transaction",
        action="store_true",
        help=(
            "Select the first real pgbench client transaction after the row-start "
            "marker. In prepared mode this can still include first-use PQprepare "
            "command cycles, so it may not have the clean steady-state 7-command "
            "shape."
        ),
    )
    parser.add_argument(
        "--first-pgbench-tpcb",
        action="store_true",
        help=(
            "Select the first clean 7-command pgbench tpcb-like transaction "
            "after the row-start marker instead of using --row-index or the "
            "default median row."
        ),
    )
    parser.add_argument(
        "--median-pgbench-session-transaction",
        action="store_true",
        help=(
            "Select a representative real pgbench transaction after the "
            "row-start marker by taking the median traced span within the "
            "dominant transaction shape observed in the log, then stitch "
            "adjacent session startup/teardown rows around it. This is the "
            "right selector for short-lived-session views such as pgbench "
            "--connect, where a raw file-wide median can land on setup or "
            "teardown rows instead of a meaningful transaction lifecycle."
        ),
    )
    parser.add_argument(
        "--stitch-session-startup",
        action="store_true",
        help=(
            "Merge adjacent session-lifecycle rows around the selected "
            "transaction when they belong to the same backend. Today this "
            "includes the immediately preceding startup/auth row and the "
            "immediately following session_teardown row when present. This is "
            "useful for cold-session views and for future pgbench -C "
            "per-transaction connection plots."
        ),
    )
    parser.add_argument("-o", "--output", required=True, help="Output PDF path")
    parser.add_argument(
        "--title",
        default=None,
        help="Optional plot title. Defaults to row index plus preceding query.",
    )
    parser.add_argument(
        "--show-labels",
        action="store_true",
        help=(
            "Render text labels on all bars. By default the plot labels only the "
            "envelope bars so the main lifecycle stays readable without re-adding "
            "the old dense child-bar clutter."
        ),
    )
    parser.add_argument(
        "--annotate-pgbench-tpcb",
        action="store_true",
        help=(
            "Add the numbered/bracketed pgbench tpcb-like command-stage overlay "
            "used in our transaction-lifecycle analysis."
        ),
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


def trace_total_ns(trace: TraceBlock) -> int:
    """Return the traced span used as the CSV trace_total_ns field for one row."""

    return trace.max_end_offset_ns


def select_median_trace(traces: List[TraceBlock]) -> TraceBlock:
    """Select a representative coordinator row by median traced span.

    This keeps the waterfall grounded in one physically realizable transaction
    while still using the full transaction sample to choose a typical example.
    For even counts, the lower median is chosen deterministically.
    """

    if not traces:
        raise SystemExit("no coordinator traces found")

    sorted_traces = sorted(traces, key=lambda trace: (trace_total_ns(trace), trace.row_index))
    median_index = (len(sorted_traces) - 1) // 2
    return sorted_traces[median_index]


def is_row_start_marker_trace(trace: TraceBlock) -> bool:
    """Return whether one row is our explicit benchmark row-start marker."""

    return "pgbench row marker phase=start" in trace.preceding_query


def is_real_pgbench_transaction_trace(trace: TraceBlock) -> bool:
    """Return whether one row is a real pgbench client transaction.

    We key this off the explicit per-session connection-transaction identity
    emitted by the latency trace logger. That cleanly excludes setup/control
    statements that may still carry a distributed transaction id without being
    part of the client-side pgbench transaction stream itself.
    """

    return "conn_txn=" in trace.identity and "dtxid=" in trace.identity


def is_remote_or_worker_stage(stage_name: str) -> bool:
    """Return whether one stage belongs to remote/worker-side lifecycle work."""

    return stage_name in WORKER_ENVELOPE_STAGES or stage_name in (
        "placement_bind",
        "remote_command_dispatch",
    )


def has_clean_pgbench_tpcb_shape(trace: TraceBlock) -> bool:
    """Return whether one coordinator row looks like one steady pgbench transaction.

    For this benchmark family, the first clean steady-state transaction after
    the row-start marker has:
    - a real pgbench client transaction identity
    - exactly seven client ReadyForQuery boundaries / command envelopes

    We deliberately do not treat the earlier setup-heavy row as a parser bug:
    in prepared mode, pgbench lazily prepares statements on first use, so the
    first session transaction can legitimately have more command cycles than
    the steady seven-command tpcb-like shape.
    """

    if not is_real_pgbench_transaction_trace(trace):
        return False

    ready_count = sum(
        1 for stage in trace.stage_instances if stage.stage_name == "client_ready_for_query"
    )
    return ready_count == len(PGBENCH_TPCB_STAGE_LABELS)


def pgbench_transaction_shape(trace: TraceBlock) -> Tuple[int, int]:
    """Return a coarse transaction-shape key for one real pgbench row.

    We use the command-envelope count plus matched worker timing block count as
    a practical signature for "same kind of transaction" when selecting a
    representative row from pgbench --connect runs. That is stable enough to
    separate setup-only rows, one-worker rows, and the two-worker / 2PC shape
    we usually care about, without hard-coding a single worker topology.
    """

    ready_count = sum(
        1 for stage in trace.stage_instances if stage.stage_name == "client_ready_for_query"
    )
    return ready_count, trace.matched_worker_timing_blocks


def build_trace_from_instances(
    base_trace: TraceBlock,
    stage_instances: Sequence[StageInstance],
    *,
    preserve_offsets: bool = False,
) -> TraceBlock:
    """Build a synthetic trace block from one ordered stage list.

    This is used only in analysis/plotting so we can present one logical
    transaction lifecycle even when the raw log emitted it as adjacent trace
    blocks. The synthetic row keeps the selected row's identity / worker-match
    counters, but recomputes stage summaries from the supplied stage instances.

    By default we rebase from absolute timestamps so stitched adjacent rows
    preserve their true cross-row ordering. For display-only time compression,
    callers can pass ``preserve_offsets=True`` to keep the supplied
    start/end offsets exactly as provided.
    """

    if not stage_instances:
        return base_trace

    ordered_instances = sorted(
        stage_instances,
        key=lambda stage: (stage.abs_start_ns, stage.abs_end_ns, stage.stage_name),
    )
    base_start_ns = ordered_instances[0].abs_start_ns

    synthetic_instances: List[StageInstance] = []
    stage_duration_ns: Dict[str, int] = {}
    stage_count: Dict[str, int] = {}
    stage_first_start_ns: Dict[str, int] = {}
    stage_last_end_ns: Dict[str, int] = {}
    raw_stage_order: List[str] = []
    max_end_offset_ns = 0

    for stage in ordered_instances:
        if preserve_offsets:
            rebased_start_ns = stage.start_offset_ns
            rebased_end_ns = stage.end_offset_ns
        else:
            rebased_start_ns = stage.abs_start_ns - base_start_ns
            rebased_end_ns = stage.abs_end_ns - base_start_ns
        rebased_duration_ns = rebased_end_ns - rebased_start_ns

        synthetic_stage = StageInstance(
            stage_name=stage.stage_name,
            remote_session_id=stage.remote_session_id,
            remote_command_id=stage.remote_command_id,
            start_offset_ns=rebased_start_ns,
            end_offset_ns=rebased_end_ns,
            duration_ns=rebased_duration_ns,
            abs_start_ns=stage.abs_start_ns,
            abs_end_ns=stage.abs_end_ns,
        )
        synthetic_instances.append(synthetic_stage)
        raw_stage_order.append(stage.stage_name)
        stage_duration_ns[stage.stage_name] = stage_duration_ns.get(stage.stage_name, 0) + rebased_duration_ns
        stage_count[stage.stage_name] = stage_count.get(stage.stage_name, 0) + 1
        stage_first_start_ns[stage.stage_name] = min(
            stage_first_start_ns.get(stage.stage_name, rebased_start_ns),
            rebased_start_ns,
        )
        stage_last_end_ns[stage.stage_name] = max(
            stage_last_end_ns.get(stage.stage_name, 0),
            rebased_end_ns,
        )
        max_end_offset_ns = max(max_end_offset_ns, rebased_end_ns)

    synthetic_trace = TraceBlock(
        source_file=base_trace.source_file,
        row_index=base_trace.row_index,
        timestamp=base_trace.timestamp,
        timestamp_ns=base_trace.timestamp_ns,
        pid=base_trace.pid,
        identity=base_trace.identity,
        command_tag=base_trace.command_tag,
        dropped_entries=base_trace.dropped_entries,
        preceding_query=base_trace.preceding_query,
        stage_duration_ns=stage_duration_ns,
        stage_count=stage_count,
        stage_first_start_ns=stage_first_start_ns,
        stage_last_end_ns=stage_last_end_ns,
        stage_instances=synthetic_instances,
        raw_stage_order=raw_stage_order,
        max_end_offset_ns=max_end_offset_ns,
        worker_session_acquire_actual_ns=base_trace.worker_session_acquire_actual_ns,
        worker_session_acquire_comm_ns=base_trace.worker_session_acquire_comm_ns,
        remote_tx_attach_actual_ns=base_trace.remote_tx_attach_actual_ns,
        remote_tx_attach_comm_ns=base_trace.remote_tx_attach_comm_ns,
        remote_command_wait_actual_ns=base_trace.remote_command_wait_actual_ns,
        remote_command_wait_comm_ns=base_trace.remote_command_wait_comm_ns,
        remote_result_drain_actual_ns=base_trace.remote_result_drain_actual_ns,
        remote_result_drain_comm_ns=base_trace.remote_result_drain_comm_ns,
        remote_tx_commit_actual_ns=base_trace.remote_tx_commit_actual_ns,
        remote_tx_commit_comm_ns=base_trace.remote_tx_commit_comm_ns,
        remote_tx_abort_actual_ns=base_trace.remote_tx_abort_actual_ns,
        remote_tx_abort_comm_ns=base_trace.remote_tx_abort_comm_ns,
        remote_tx_prepare_actual_ns=base_trace.remote_tx_prepare_actual_ns,
        remote_tx_prepare_comm_ns=base_trace.remote_tx_prepare_comm_ns,
        matched_worker_timing_blocks=base_trace.matched_worker_timing_blocks,
        matched_worker_trace_blocks=base_trace.matched_worker_trace_blocks,
        unmatched_worker_timing_blocks=base_trace.unmatched_worker_timing_blocks,
    )
    return synthetic_trace


def compressed_offset_ns(offset_ns: int, ignored_intervals: Sequence[Tuple[int, int]]) -> int:
    """Compress one row-local offset by subtracting ignored interval coverage.

    The plot uses this to remove control-path stages that we explicitly do not
    want to treat as part of the transaction lifecycle. Today that means
    `backend_command_turnaround`: we keep the trace row itself, but collapse
    those intervals out of the displayed elapsed-time axis.
    """

    compressed_ns = offset_ns
    for ignored_start_ns, ignored_end_ns in ignored_intervals:
        if offset_ns <= ignored_start_ns:
            break

        covered_ns = min(offset_ns, ignored_end_ns) - ignored_start_ns
        if covered_ns > 0:
            compressed_ns -= covered_ns

    return compressed_ns


def build_display_trace(trace: TraceBlock) -> TraceBlock:
    """Build a plot-facing trace after removing non-lifecycle overhead.

    `backend_command_turnaround` is real backend CPU time, but it represents
    our own timing/reporting handoff rather than transaction-semantic work.
    For the lifecycle waterfall we therefore remove those intervals entirely
    instead of leaving dead space on the x-axis.
    """

    ignored_intervals = merge_intervals(
        [
            (stage.start_offset_ns, stage.end_offset_ns)
            for stage in trace.stage_instances
            if stage.stage_name == "backend_command_turnaround"
        ]
    )

    if not ignored_intervals:
        return trace

    display_instances: List[StageInstance] = []
    for stage in sorted(
        trace.stage_instances,
        key=lambda stage: (stage.start_offset_ns, stage.end_offset_ns, stage.stage_name),
    ):
        if stage.stage_name == "backend_command_turnaround":
            continue

        display_start_ns = compressed_offset_ns(stage.start_offset_ns, ignored_intervals)
        display_end_ns = compressed_offset_ns(stage.end_offset_ns, ignored_intervals)
        display_duration_ns = max(0, display_end_ns - display_start_ns)

        if display_duration_ns == 0:
            continue

        display_instances.append(
            StageInstance(
                stage_name=stage.stage_name,
                remote_session_id=stage.remote_session_id,
                remote_command_id=stage.remote_command_id,
                start_offset_ns=display_start_ns,
                end_offset_ns=display_end_ns,
                duration_ns=display_duration_ns,
                abs_start_ns=stage.abs_start_ns,
                abs_end_ns=stage.abs_end_ns,
            )
        )

    return build_trace_from_instances(trace, display_instances, preserve_offsets=True)


def stitch_first_pgbench_transaction_prelude(
    traces: List[TraceBlock], first_transaction: TraceBlock
) -> TraceBlock:
    """Extend the first real pgbench row with the detached first BEGIN prepare.

    On the cold first prepared-session transaction, the one-time `PQprepare()`
    cycle for `BEGIN` can appear in the immediately preceding empty-identity
    trace block before the `conn_txn=...` identity is established. We stitch
    only the post-startup suffix of that adjacent row into the selected first
    transaction so the plotted cold path reflects the full 14-command semantic
    without dragging the unrelated startup/auth envelope into the transaction.
    """

    if first_transaction.row_index <= 0:
        return first_transaction

    previous_trace = traces[first_transaction.row_index - 1]
    if previous_trace.pid != first_transaction.pid or previous_trace.identity:
        return first_transaction

    previous_ready_indexes = [
        index
        for index, stage in enumerate(previous_trace.stage_instances)
        if stage.stage_name == "client_ready_for_query"
    ]
    if len(previous_ready_indexes) < 2:
        return first_transaction

    prelude_instances = previous_trace.stage_instances[previous_ready_indexes[0] + 1 :]
    if not prelude_instances:
        return first_transaction

    if any(is_remote_or_worker_stage(stage.stage_name) for stage in prelude_instances):
        return first_transaction

    if prelude_instances[-1].stage_name != "client_ready_for_query":
        return first_transaction

    return build_trace_from_instances(
        first_transaction,
        [*prelude_instances, *first_transaction.stage_instances],
    )


def stitch_session_startup_into_trace(traces: List[TraceBlock], trace: TraceBlock) -> TraceBlock:
    """Merge the immediately preceding startup/auth row into one transaction row.

    This is intentionally conservative: we only stitch the adjacent previous
    row when it belongs to the same backend pid, has no transaction identity of
    its own, and clearly contains coordinator session-startup stages. That lets
    us present a cold session + transaction view without accidentally
    swallowing unrelated benchmark-control rows from other connections.
    """

    if trace.row_index <= 0:
        return trace

    previous_trace = traces[trace.row_index - 1]
    if previous_trace.pid != trace.pid or previous_trace.identity:
        return trace

    if not (
        previous_trace.stage_duration_ns.get("backend_spawn", 0) > 0
        or previous_trace.stage_duration_ns.get("client_session_establish", 0) > 0
    ):
        return trace

    if any(is_remote_or_worker_stage(stage.stage_name) for stage in previous_trace.stage_instances):
        return trace

    return build_trace_from_instances(
        trace,
        [*previous_trace.stage_instances, *trace.stage_instances],
    )


def stitch_session_teardown_into_trace(traces: List[TraceBlock], trace: TraceBlock) -> TraceBlock:
    """Append the immediately following teardown row onto one transaction row.

    The server-side disconnect stage is emitted as its own final row on backend
    exit. When the adjacent next row belongs to the same backend and is tagged
    as `session_teardown`, merge it so one session-scoped plot can show the
    tail as well as the transaction body.
    """

    if trace.row_index >= len(traces) - 1:
        return trace

    next_trace = traces[trace.row_index + 1]
    if next_trace.pid != trace.pid:
        return trace

    if next_trace.command_tag != "session_teardown":
        return trace

    if next_trace.stage_duration_ns.get("client_session_teardown", 0) <= 0:
        return trace

    return build_trace_from_instances(
        trace,
        [*trace.stage_instances, *next_trace.stage_instances],
    )


def select_first_pgbench_tpcb_trace(traces: List[TraceBlock]) -> TraceBlock:
    """Select the first clean pgbench tpcb-like transaction after the row marker."""

    if not traces:
        raise SystemExit("no coordinator traces found")

    start_marker_index = None
    for trace in traces:
        if is_row_start_marker_trace(trace):
            start_marker_index = trace.row_index
            break

    if start_marker_index is None:
        raise SystemExit(
            "could not find a row-start marker in the coordinator trace log"
        )

    for trace in traces:
        if trace.row_index <= start_marker_index:
            continue
        if has_clean_pgbench_tpcb_shape(trace):
            return trace

    raise SystemExit(
        "could not find a clean 7-command pgbench transaction after the row-start marker"
    )


def find_first_pgbench_transaction_trace(traces: List[TraceBlock]) -> TraceBlock:
    """Find the raw first real pgbench client transaction after the row marker."""

    start_marker_index = None
    for trace in traces:
        if is_row_start_marker_trace(trace):
            start_marker_index = trace.row_index
            break

    if start_marker_index is None:
        raise SystemExit(
            "could not find a row-start marker in the coordinator trace log"
        )

    for trace in traces:
        if trace.row_index <= start_marker_index:
            continue
        if is_real_pgbench_transaction_trace(trace):
            return trace

    raise SystemExit(
        "could not find a real pgbench client transaction after the row-start marker"
    )


def select_first_pgbench_transaction_trace(traces: List[TraceBlock]) -> TraceBlock:
    """Select the first real pgbench client transaction after the row marker.

    This intentionally differs from the first clean steady-state transaction:
    the first real transaction can include one-time PQprepare command cycles
    before the normal seven-command prepared pgbench shape appears.
    """

    if not traces:
        raise SystemExit("no coordinator traces found")

    return stitch_first_pgbench_transaction_prelude(
        traces,
        find_first_pgbench_transaction_trace(traces),
    )


def select_median_pgbench_session_transaction_trace(traces: List[TraceBlock]) -> TraceBlock:
    """Select a representative real pgbench row from the dominant shape class.

    Raw file-wide medians are not meaningful for pgbench --connect because the
    logfile interleaves benchmark-control rows, startup rows, teardown rows,
    and real transaction rows. This selector first narrows to real pgbench
    transaction identities after the row-start marker, then picks the most
    common transaction shape and finally chooses the median traced span within
    that class.
    """

    if not traces:
        raise SystemExit("no coordinator traces found")

    start_marker_index = None
    for trace in traces:
        if is_row_start_marker_trace(trace):
            start_marker_index = trace.row_index
            break

    if start_marker_index is None:
        raise SystemExit(
            "could not find a row-start marker in the coordinator trace log"
        )

    real_transactions = [
        trace
        for trace in traces
        if trace.row_index > start_marker_index and is_real_pgbench_transaction_trace(trace)
    ]
    if not real_transactions:
        raise SystemExit(
            "could not find any real pgbench client transactions after the row-start marker"
        )

    shape_counts = Counter(pgbench_transaction_shape(trace) for trace in real_transactions)
    dominant_shape, _ = max(
        shape_counts.items(),
        key=lambda item: (item[1], item[0][1], item[0][0]),
    )

    dominant_shape_transactions = [
        trace for trace in real_transactions if pgbench_transaction_shape(trace) == dominant_shape
    ]
    return select_median_trace(dominant_shape_transactions)


def select_target_trace(
    traces: List[TraceBlock],
    row_index: Optional[int],
    first_pgbench_transaction: bool,
    first_pgbench_tpcb: bool,
    median_pgbench_session_transaction: bool,
    stitch_session_startup: bool,
) -> TraceBlock:
    """Select one coordinator trace row with explicit selection priority."""

    if not traces:
        raise SystemExit("no coordinator traces found")

    mutually_exclusive_selectors = sum(
        bool(selector)
        for selector in (
            first_pgbench_transaction,
            first_pgbench_tpcb,
            median_pgbench_session_transaction,
        )
    )
    if mutually_exclusive_selectors > 1:
        raise SystemExit(
            "--first-pgbench-transaction, --first-pgbench-tpcb, and "
            "--median-pgbench-session-transaction are mutually exclusive"
        )

    if first_pgbench_transaction:
        if row_index is not None:
            raise SystemExit(
                "--first-pgbench-transaction cannot be combined with --row-index"
            )
        if stitch_session_startup:
            target = stitch_session_startup_into_trace(
                traces,
                find_first_pgbench_transaction_trace(traces),
            )
        else:
            target = select_first_pgbench_transaction_trace(traces)
    elif first_pgbench_tpcb:
        if row_index is not None:
            raise SystemExit("--first-pgbench-tpcb cannot be combined with --row-index")
        target = select_first_pgbench_tpcb_trace(traces)
    elif median_pgbench_session_transaction:
        if row_index is not None:
            raise SystemExit(
                "--median-pgbench-session-transaction cannot be combined with --row-index"
            )
        target = select_median_pgbench_session_transaction_trace(traces)
        stitch_session_startup = True
    elif row_index is None:
        target = select_median_trace(traces)
    elif row_index < 0:
        target = traces[row_index]
    else:
        by_row_index = {trace.row_index: trace for trace in traces}
        if row_index not in by_row_index:
            raise SystemExit(f"row index {row_index} not found in coordinator trace log")
        target = by_row_index[row_index]

    if stitch_session_startup:
        if not first_pgbench_transaction:
            target = stitch_session_startup_into_trace(traces, target)
        target = stitch_session_teardown_into_trace(traces, target)

    return target


def centered_nested_start(envelope: StageInstance, nested_duration_ns: int) -> int:
    """Place one approximate nested bar centered within an envelope stage."""

    if nested_duration_ns >= envelope.duration_ns:
        return envelope.start_offset_ns

    return envelope.start_offset_ns + ((envelope.duration_ns - nested_duration_ns) // 2)


def clipped_duration(envelope: StageInstance, requested_duration_ns: int) -> int:
    """Keep approximate nested bars inside the coordinator envelope."""

    return max(0, min(requested_duration_ns, envelope.duration_ns))


def merge_intervals(intervals: Sequence[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Merge overlapping intervals while preserving wall-clock coverage."""

    merged: List[Tuple[int, int]] = []
    for start_ns, end_ns in sorted((start_ns, end_ns) for start_ns, end_ns in intervals if end_ns > start_ns):
        if not merged or start_ns > merged[-1][1]:
            merged.append((start_ns, end_ns))
            continue

        previous_start_ns, previous_end_ns = merged[-1]
        merged[-1] = (previous_start_ns, max(previous_end_ns, end_ns))

    return merged


def build_command_cycles(trace: TraceBlock) -> List[Tuple[int, int]]:
    """Infer one coordinator command envelope from first recv to ReadyForQuery.

    The ordered latency stages already bracket each prepared/simple statement by
    repeated client_command_wait / client_command_receive entries followed by
    one ReadyForQuery. Those boundaries let the plot synthesize a parent
    envelope even though the instrumentation only emits the finer child stages.
    """

    command_cycles: List[Tuple[int, int]] = []
    current_cycle_start_ns: Optional[int] = None
    has_client_command_wait = any(
        stage.stage_name == "client_command_wait" for stage in trace.stage_instances
    )

    if trace.stage_instances and not has_client_command_wait:
        raise SystemExit(
            "selected trace row is missing client_command_wait; rerun with the "
            "new latency instrumentation instead of plotting archived logs"
        )

    for stage in sorted(
        trace.stage_instances,
        key=lambda stage: (stage.start_offset_ns, stage.end_offset_ns, stage.stage_name),
    ):
        if stage.stage_name in ("client_command_wait", "client_command_receive"):
            if current_cycle_start_ns is None or stage.start_offset_ns < current_cycle_start_ns:
                current_cycle_start_ns = stage.start_offset_ns
            continue

        if stage.stage_name != "client_ready_for_query" or current_cycle_start_ns is None:
            continue

        if stage.end_offset_ns > current_cycle_start_ns:
            command_cycles.append((current_cycle_start_ns, stage.end_offset_ns))
        current_cycle_start_ns = None

    return command_cycles


def build_inferred_coordinator_gap_segments(
    trace: TraceBlock, command_cycles: Sequence[Tuple[int, int]]
) -> List[PlotSegment]:
    """Fill uncovered coordinator command time with one derived local bucket.

    New instrumentation now records explicit client_command_wait stages, so the
    only remaining inferred coordinator bucket is the local/control-path
    complement inside one command envelope after we subtract the measured child
    stages already present in that row.
    """

    segments: List[PlotSegment] = []
    measured_intervals = merge_intervals(
        [
            (stage.start_offset_ns, stage.end_offset_ns)
            for stage in trace.stage_instances
        ]
    )

    for cycle_start_ns, cycle_end_ns in command_cycles:
        covered_slices = merge_intervals(
            [
                (max(cycle_start_ns, start_ns), min(cycle_end_ns, end_ns))
                for start_ns, end_ns in measured_intervals
                if end_ns > cycle_start_ns and start_ns < cycle_end_ns
            ]
        )
        cursor_ns = cycle_start_ns
        for covered_start_ns, covered_end_ns in covered_slices:
            if covered_start_ns > cursor_ns:
                segments.append(
                    PlotSegment(
                        lane="Coordinator",
                        start_ns=cursor_ns,
                        duration_ns=covered_start_ns - cursor_ns,
                        label="coord local processing (derived)",
                        color_key="coordinator_local_processing",
                        is_actual=False,
                    )
                )
            cursor_ns = max(cursor_ns, covered_end_ns)

        if cycle_end_ns > cursor_ns:
            segments.append(
                PlotSegment(
                    lane="Coordinator",
                    start_ns=cursor_ns,
                    duration_ns=cycle_end_ns - cursor_ns,
                    label="coord local processing (derived)",
                    color_key="coordinator_local_processing",
                    is_actual=False,
                )
            )

    return segments


def build_coordinator_remote_round_segments(trace: TraceBlock) -> List[PlotSegment]:
    """Build coordinator-side parent envelopes for worker query round trips.

    The ordered trace logs the dispatch/wait/drain child stages individually,
    but the coordinator lane previously lacked any parent bar showing the full
    remote round-trip window. Group those child stages by (rsid, rcid) so the
    figure exposes the round-trip envelope without losing the exact child bars.
    """

    grouped_rounds: Dict[Tuple[int, str], List[StageInstance]] = {}
    segments: List[PlotSegment] = []

    for stage in trace.stage_instances:
        if stage.stage_name not in QUERY_REMOTE_ROUND_STAGES:
            continue
        if stage.remote_session_id == 0 or not stage.remote_command_id:
            continue
        grouped_rounds.setdefault(
            (stage.remote_session_id, stage.remote_command_id), []
        ).append(stage)

    for (_, _), round_stages in sorted(
        grouped_rounds.items(),
        key=lambda item: min(stage.start_offset_ns for stage in item[1]),
    ):
        round_start_ns = min(stage.start_offset_ns for stage in round_stages)
        round_end_ns = max(stage.end_offset_ns for stage in round_stages)
        if round_end_ns <= round_start_ns:
            continue

        segments.append(
            PlotSegment(
                lane="Coordinator",
                start_ns=round_start_ns,
                duration_ns=round_end_ns - round_start_ns,
                label="worker round",
                color_key="coordinator_remote_envelope",
                is_actual=False,
                default_label=False,
                label_y_offset=-0.13,
            )
        )

    return segments


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
    """Build the coordinator lane from stages plus inferred parent buckets."""

    segments: List[PlotSegment] = []
    startup_begin_ns: Optional[int] = None
    startup_end_ns: Optional[int] = None
    sorted_stages = sorted(
        trace.stage_instances,
        key=lambda stage: (stage.start_offset_ns, stage.end_offset_ns, stage.stage_name),
    )

    # Treat startup as one contiguous prefix that ends as soon as the backend
    # begins reading the first transaction-related frontend command. This is
    # much more reliable for stitched session-scoped plots than trying to infer
    # the end from later parse-plan activity, which is absent in some shapes.
    startup_prefix_stage_names = {
        "backend_spawn",
        "client_session_establish",
        "client_ready_for_query",
    }

    for stage in sorted_stages:
        if startup_begin_ns is None:
            if stage.stage_name not in startup_prefix_stage_names:
                break
            startup_begin_ns = stage.start_offset_ns
            startup_end_ns = stage.end_offset_ns
            continue

        if stage.stage_name not in startup_prefix_stage_names:
            break
        startup_end_ns = stage.end_offset_ns

    if startup_begin_ns is not None and startup_end_ns is not None:
        if startup_end_ns > startup_begin_ns:
            segments.append(
                PlotSegment(
                    lane="Coordinator",
                    start_ns=startup_begin_ns,
                    duration_ns=startup_end_ns - startup_begin_ns,
                    label="startup/auth",
                    color_key="client_envelope",
                    is_actual=False,
                    default_label=False,
                    label_y_offset=0.13,
                )
            )

    command_cycles = build_command_cycles(trace)
    first_command_cycle_start_ns = command_cycles[0][0] if command_cycles else None
    for cycle_start_ns, cycle_end_ns in command_cycles:
        segments.append(
            PlotSegment(
                lane="Coordinator",
                start_ns=cycle_start_ns,
                duration_ns=cycle_end_ns - cycle_start_ns,
                label="client cmd",
                color_key="coordinator_command_envelope",
                is_actual=False,
                default_label=False,
                label_y_offset=0.13,
            )
        )
    segments.extend(build_coordinator_remote_round_segments(trace))
    segments.extend(build_inferred_coordinator_gap_segments(trace, command_cycles))

    for stage in sorted_stages:
        if stage.stage_name not in COORDINATOR_STAGES:
            continue

        color_key = "coordinator"
        if stage.stage_name in (
            "client_command_wait",
            "client_command_receive",
            "client_bind_complete_send",
            "client_describe_response_send",
            "client_result_send",
            "client_command_complete",
            "client_ready_for_query",
        ):
            color_key = "client"

        default_label = False

        if stage.stage_name in ("backend_spawn", "client_session_establish"):
            color_key = "coordinator_session_actual"
            default_label = False
        elif stage.stage_name == "client_session_teardown":
            color_key = "coordinator_teardown_actual"
            default_label = False
        elif stage.stage_name == "client_command_wait":
            color_key = "client_wait"

        segments.append(
            PlotSegment(
                lane="Coordinator",
                start_ns=stage.start_offset_ns,
                duration_ns=stage.duration_ns,
                label=SHORT_STAGE_LABELS.get(stage.stage_name, stage.stage_name),
                color_key=color_key,
                is_actual=False,
                default_label=default_label,
            )
        )

    return segments


def build_worker_envelope_segments(trace: TraceBlock) -> List[PlotSegment]:
    """Build one envelope lane per remote session used by the transaction."""

    segments: List[PlotSegment] = []
    for stage in trace.stage_instances:
        if stage.stage_name not in WORKER_ENVELOPE_STAGES:
            continue

        default_label = stage.stage_name in (
            "remote_tx_attach",
            "remote_tx_abort",
        )

        segments.append(
            PlotSegment(
                lane=f"Worker rsid={stage.remote_session_id}",
                start_ns=stage.start_offset_ns,
                duration_ns=stage.duration_ns,
                label=SHORT_STAGE_LABELS.get(stage.stage_name, stage.stage_name),
                color_key="worker_envelope",
                is_actual=False,
                default_label=default_label,
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

        candidate_traces_with_startup = []
        for worker_trace in candidate_worker_traces:
            actual_stages = [
                worker_stage
                for worker_stage in worker_trace.stage_instances
                if worker_stage.stage_name in ("backend_spawn", "client_session_establish")
            ]
            if actual_stages:
                candidate_traces_with_startup.append((worker_trace, actual_stages))

        if not candidate_traces_with_startup:
            continue

        # Prefer the startup-bearing worker trace nearest to the coordinator's
        # selected transaction row. Without this filter the timestamp match can
        # pick a later tx_prepare/tx_commit worker row, which leaves the
        # session-acquire envelope visibly empty even though startup/auth was
        # actually captured in the archived worker logs.
        best_worker_trace, actual_stages = min(
            candidate_traces_with_startup,
            key=lambda item: abs(item[0].timestamp_ns - trace.timestamp_ns),
        )
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
                    default_label=False,
                )
            )

    return segments


def append_sequential_nested_segments(
    segments: List[PlotSegment],
    lane: str,
    envelope_start_ns: int,
    envelope_duration_ns: int,
    components: Sequence[Tuple[str, str, int]],
) -> None:
    """Lay out approximate worker leaf components from left to right.

    Worker timing reports expose additive leaf totals, but not exact
    cross-node start offsets for each leaf. Rather than centering one opaque
    "actual" blob, place the measured leaves sequentially inside the envelope.
    Center the whole measured group inside the envelope so the unknown
    uninstrumented slack remains split on both sides instead of being implied to
    happen only after the measured leaves.
    """

    positive_durations_ns = [requested_duration_ns for _, _, requested_duration_ns in components if requested_duration_ns > 0]
    displayed_group_duration_ns = max(0, min(sum(positive_durations_ns), envelope_duration_ns))
    cursor_ns = envelope_start_ns + ((envelope_duration_ns - displayed_group_duration_ns) // 2)
    remaining_ns = displayed_group_duration_ns

    for label, color_key, requested_duration_ns in components:
        if remaining_ns <= 0:
            break

        component_duration_ns = max(0, min(requested_duration_ns, remaining_ns))
        if component_duration_ns <= 0:
            continue

        segments.append(
            PlotSegment(
                lane=lane,
                start_ns=cursor_ns,
                duration_ns=component_duration_ns,
                label=label,
                color_key=color_key,
                is_actual=True,
            )
        )
        cursor_ns += component_duration_ns
        remaining_ns -= component_duration_ns


def worker_query_envelope_bounds(
    trace: TraceBlock, rsid: int, rcid: str
) -> Optional[Tuple[int, int]]:
    """Return the full query-round envelope bounds for one worker command."""

    relevant_stages = [
        stage
        for stage in trace.stage_instances
        if (
            stage.remote_session_id == rsid and
            stage.remote_command_id == rcid and
            stage.stage_name in QUERY_REMOTE_ROUND_STAGES
        )
    ]
    if not relevant_stages:
        return None

    return (
        min(stage.start_offset_ns for stage in relevant_stages),
        max(stage.end_offset_ns for stage in relevant_stages),
    )


def worker_lifecycle_envelope_bounds(
    trace: TraceBlock, rsid: int, rcid: str, stage_name: str
) -> Optional[Tuple[int, int]]:
    """Return the envelope bounds for one worker lifecycle command."""

    envelope_stage = first_matching_stage(trace, stage_name, rsid, rcid)
    if envelope_stage is None:
        return None

    return envelope_stage.start_offset_ns, envelope_stage.end_offset_ns


def build_worker_command_actual_segments(
    trace: TraceBlock, matched_worker_timings: Sequence[WorkerTimingBlock]
) -> List[PlotSegment]:
    """Approximate worker timing leaves inside coordinator-observed envelopes.

    The older plot centered one coarse QUERY_ACTIVE_WALL/Printtup bar inside
    the worker envelope. That hid the already-measured distinction between
    worker wait, protocol/transport CPU, row serialization, and local backend
    processing. This version keeps the same approximate cross-node placement
    but decomposes the worker report into those categories.
    """

    segments: List[PlotSegment] = []

    for worker_block in matched_worker_timings:
        rsid = worker_block.parsed_tag.rsid
        rcid = worker_block.parsed_tag.rcid
        kind = worker_block.parsed_tag.kind
        lane = f"Worker rsid={rsid}"

        if kind == "task_query":
            envelope_bounds = worker_query_envelope_bounds(trace, rsid, rcid)
            if envelope_bounds is None:
                continue

            envelope_start_ns, envelope_end_ns = envelope_bounds
            append_sequential_nested_segments(
                segments,
                lane,
                envelope_start_ns,
                envelope_end_ns - envelope_start_ns,
                (
                    ("worker wait", "worker_wait_actual", worker_wait_total_ns(worker_block.timers)),
                    (
                        "recv/protocol",
                        "worker_protocol_actual",
                        worker_receive_protocol_total_ns(worker_block.timers),
                    ),
                    (
                        "local exec",
                        "worker_local_actual",
                        worker_local_processing_total_ns(worker_block.timers),
                    ),
                    (
                        "row serde",
                        "worker_result_serde_actual",
                        worker_result_serde_total_ns(worker_block.timers),
                    ),
                    (
                        "send/protocol",
                        "worker_protocol_actual",
                        worker_send_protocol_total_ns(worker_block.timers),
                    ),
                ),
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

            envelope_bounds = worker_lifecycle_envelope_bounds(
                trace, rsid, rcid, target_stage_name
            )
            if envelope_bounds is None:
                continue

            envelope_start_ns, envelope_end_ns = envelope_bounds
            lifecycle_label = SHORT_STAGE_LABELS.get(target_stage_name, target_stage_name)
            append_sequential_nested_segments(
                segments,
                lane,
                envelope_start_ns,
                envelope_end_ns - envelope_start_ns,
                (
                    ("worker wait", "worker_wait_actual", worker_wait_total_ns(worker_block.timers)),
                    (
                        "recv/protocol",
                        "worker_protocol_actual",
                        worker_receive_protocol_total_ns(worker_block.timers),
                    ),
                    (
                        f"{lifecycle_label} local",
                        "worker_local_actual",
                        worker_local_processing_total_ns(worker_block.timers),
                    ),
                    (
                        "send/protocol",
                        "worker_protocol_actual",
                        worker_send_protocol_total_ns(worker_block.timers),
                    ),
                ),
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


def maybe_label_bar(
    ax: plt.Axes,
    segment: PlotSegment,
    lane_y: float,
    max_end_ms: float,
    *,
    show_labels: bool,
) -> None:
    """Optionally annotate bars while avoiding unreadable text on tiny bars."""

    if not segment.label:
        return

    if not show_labels and not segment.default_label:
        return

    duration_ms = segment.duration_ns / 1_000_000.0
    if duration_ms < LABEL_THRESHOLD_MS and duration_ms < (0.04 * max_end_ms):
        # Short worker tx-attach envelopes are semantically important even when
        # they are too narrow to hold an on-bar label. Keep a tiny callout for
        # the synthetic "BEGIN;" marker so hot-session plots do not silently
        # lose the worker-enlistment point just because the bar became short.
        if segment.label == "BEGIN;" and segment.lane.startswith("Worker rsid="):
            center_ms = (segment.start_ns + (segment.duration_ns / 2.0)) / 1_000_000.0
            callout_y = lane_y + 0.01
            text_y = lane_y + 0.33
            ax.annotate(
                segment.label,
                xy=(center_ms, callout_y),
                xytext=(center_ms + 0.03, text_y),
                textcoords="data",
                ha="left",
                va="center",
                fontsize=7.4,
                color="#2F3447",
                arrowprops={
                    "arrowstyle": "-",
                    "color": "#2F3447",
                    "linewidth": 0.9,
                    "shrinkA": 0.0,
                    "shrinkB": 0.0,
                },
                clip_on=True,
                zorder=5,
            )
        return

    ax.text(
        (segment.start_ns + (segment.duration_ns / 2.0)) / 1_000_000.0,
        lane_y + segment.label_y_offset,
        segment.label,
        ha="center",
        va="center",
        fontsize=7.8 if segment.default_label and not show_labels else 8.5,
        color="black",
        clip_on=True,
        zorder=5,
    )


def annotate_pgbench_tpcb_cycles(
    ax: plt.Axes,
    trace: TraceBlock,
    lane_to_y: Dict[str, float],
) -> None:
    """Draw the numbered/bracketed pgbench tpcb-like command-stage overlay.

    The median-row waterfall is easiest to read when we keep the envelope bars
    terse but add one higher-level annotation per SQL command in the built-in
    pgbench tpcb-like transaction. For stitched cold-session views we also
    allow:
    - the 14-cycle detached-prelude first-transaction shape
    - the 15-cycle startup + first-transaction shape

    In the latter case we drop the leading startup cycle and then collapse the
    remaining prepare+execute pairs back into the seven semantic commands.
    """

    command_cycles = build_command_cycles(trace)
    if len(command_cycles) == len(PGBENCH_TPCB_STAGE_LABELS):
        semantic_cycles = command_cycles
    elif len(command_cycles) == (2 * len(PGBENCH_TPCB_STAGE_LABELS)):
        semantic_cycles = []
        for pair_index in range(0, len(command_cycles), 2):
            pair_start_ns = command_cycles[pair_index][0]
            pair_end_ns = command_cycles[pair_index + 1][1]
            semantic_cycles.append((pair_start_ns, pair_end_ns))
    elif len(command_cycles) == (2 * len(PGBENCH_TPCB_STAGE_LABELS)) + 1:
        command_cycles = command_cycles[1:]
        semantic_cycles = []
        for pair_index in range(0, len(command_cycles), 2):
            pair_start_ns = command_cycles[pair_index][0]
            pair_end_ns = command_cycles[pair_index + 1][1]
            semantic_cycles.append((pair_start_ns, pair_end_ns))
    else:
        raise SystemExit(
            "pgbench tpcb annotation requires either the steady 7-cycle shape "
            "or one of the stitched cold first-transaction shapes, found "
            f"{len(command_cycles)} cycles"
        )

    coordinator_y = lane_to_y["Coordinator"]
    bracket_y = coordinator_y + 0.58
    base_label_y = coordinator_y + 0.84
    tick_height = 0.11
    previous_cycle_mid_ms: Optional[float] = None
    alternate_label_row = False

    sorted_stages = sorted(
        trace.stage_instances,
        key=lambda stage: (stage.start_offset_ns, stage.end_offset_ns, stage.stage_name),
    )

    startup_start_ns: Optional[int] = None
    startup_end_ns: Optional[int] = None
    startup_prefix_stage_names = {
        "backend_spawn",
        "client_session_establish",
        "client_ready_for_query",
    }

    for stage in sorted_stages:
        if startup_start_ns is None:
            if stage.stage_name not in startup_prefix_stage_names:
                break
            startup_start_ns = stage.start_offset_ns
            startup_end_ns = stage.end_offset_ns
            continue

        if stage.stage_name not in startup_prefix_stage_names:
            break
        startup_end_ns = stage.end_offset_ns

    teardown_stage = next(
        (stage for stage in reversed(sorted_stages) if stage.stage_name == "client_session_teardown"),
        None,
    )

    def draw_bracket(start_ns: int, end_ns: int, label: str, label_y: float) -> None:
        start_ms = start_ns / 1_000_000.0
        end_ms = end_ns / 1_000_000.0
        mid_ms = (start_ms + end_ms) / 2.0

        ax.plot(
            [start_ms, end_ms],
            [bracket_y, bracket_y],
            color="#3D405B",
            linewidth=1.3,
            zorder=6,
            solid_capstyle="butt",
        )
        ax.plot(
            [start_ms, start_ms],
            [bracket_y, bracket_y - tick_height],
            color="#3D405B",
            linewidth=1.1,
            zorder=6,
        )
        ax.plot(
            [end_ms, end_ms],
            [bracket_y, bracket_y - tick_height],
            color="#3D405B",
            linewidth=1.1,
            zorder=6,
        )
        ax.text(
            mid_ms,
            label_y,
            label,
            ha="center",
            va="bottom",
            fontsize=8.5,
            color="#2F3447",
            zorder=6,
        )

    if (
        startup_start_ns is not None and
        startup_end_ns is not None and
        startup_end_ns > startup_start_ns
    ):
        draw_bracket(
            startup_start_ns,
            startup_end_ns,
            "0. Session setup",
            base_label_y,
        )

    for (cycle_start_ns, cycle_end_ns), label in zip(semantic_cycles, PGBENCH_TPCB_STAGE_LABELS):
        cycle_start_ms = cycle_start_ns / 1_000_000.0
        cycle_end_ms = cycle_end_ns / 1_000_000.0
        cycle_mid_ms = (cycle_start_ms + cycle_end_ms) / 2.0
        label_y = base_label_y

        # Dense cold-session views can place adjacent command brackets close
        # enough that their captions overlap visually. Stagger nearby labels
        # onto two rows so the SQL-command numbering remains readable without
        # changing the command boundaries themselves.
        if previous_cycle_mid_ms is not None and abs(cycle_mid_ms - previous_cycle_mid_ms) < 6.0:
            alternate_label_row = not alternate_label_row
        else:
            alternate_label_row = False
        if alternate_label_row:
            label_y += 0.18

        draw_bracket(cycle_start_ns, cycle_end_ns, label, label_y)
        previous_cycle_mid_ms = cycle_mid_ms

    if teardown_stage is not None and teardown_stage.duration_ns > 0:
        draw_bracket(
            teardown_stage.start_offset_ns,
            teardown_stage.end_offset_ns,
            "8. Session teardown",
            base_label_y,
        )


def plot_segments(
    segments: Sequence[PlotSegment],
    trace: TraceBlock,
    title: Optional[str],
    output_path: Path,
    *,
    show_labels: bool,
    annotate_pgbench_tpcb: bool,
) -> None:
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
        maybe_label_bar(ax, segment, lane_y, max_end_ms, show_labels=show_labels)

    if title:
        ax.set_title(title, fontsize=14, pad=12)
    ax.set_xlabel("Elapsed Time Within Traced Transaction (ms)", labelpad=12)
    ax.set_yticks([lane_to_y[lane] for lane in lanes])
    ax.set_yticklabels(lanes)
    ax.set_xlim(0.0, max_end_ms * 1.03 if max_end_ms > 0 else 1.0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", linestyle="--", alpha=0.30, zorder=1)
    ax.grid(axis="y", visible=False)

    if annotate_pgbench_tpcb:
        annotate_pgbench_tpcb_cycles(ax, trace, lane_to_y)

    present_color_keys = {segment.color_key for segment in segments}
    legend_items = [
        Patch(facecolor=COLORS[color_key], edgecolor="white", alpha=ALPHAS[color_key], label=label)
        for color_key, label in LEGEND_ITEM_ORDER
        if color_key in present_color_keys
    ]
    if legend_items:
        ax.legend(
            handles=legend_items,
            loc="upper center",
            bbox_to_anchor=(0.5, -0.42),
            ncol=4,
            frameon=False,
        )

    # Use explicit subplot margins instead of tight_layout() here. The figure
    # has three stacked decoration bands (top brackets, x-axis label, external
    # legend), and tight_layout() tends to pull the x-axis label back into the
    # legend on short figures.
    fig.subplots_adjust(left=0.09, right=0.995, top=0.88, bottom=0.40)

    # Always emit both raster and vector outputs from one render pass so the
    # benchmark artifact directories stay consistent regardless of which suffix
    # the caller requested.
    output_variants = {output_path}
    if output_path.suffix.lower() == ".png":
        output_variants.add(output_path.with_suffix(".pdf"))
    elif output_path.suffix.lower() == ".pdf":
        output_variants.add(output_path.with_suffix(".png"))

    for variant_path in sorted(output_variants):
        fig.savefig(variant_path, bbox_inches="tight")
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
    trace = select_target_trace(
        coordinator_traces,
        args.row_index,
        args.first_pgbench_transaction,
        args.first_pgbench_tpcb,
        args.median_pgbench_session_transaction,
        args.stitch_session_startup,
    )
    # Collapse our own post-command timing/reporting handoff out of the plotted
    # elapsed-time axis so the waterfall only shows transaction-semantic work.
    trace = build_display_trace(trace)
    segments = build_segments(
        trace,
        matched_worker_timings_by_row,
        matched_worker_traces_by_trace_session,
    )
    plot_segments(
        segments,
        trace,
        args.title,
        Path(args.output),
        show_labels=args.show_labels,
        annotate_pgbench_tpcb=args.annotate_pgbench_tpcb,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
