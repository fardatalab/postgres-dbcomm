#!/usr/bin/env python3
"""parse_latency_trace_csv.py

Parse ordered latency trace blocks from PostgreSQL/Citus logs and emit one CSV
row per trace block / transaction.

Design notes:
- The coordinator ordered trace emitted by latency_instr.c is already the
  sequential transaction/query-cycle boundary we care about. This parser
  therefore treats one "--- Ordered Latency Trace ---" block as one row.
- Repeated stages within the same trace (for example multiple
  client_ready_for_query entries in an explicit transaction) are summed into the
  stage's latency column. Optional count columns can also be emitted.
- The parser keeps the most recent "Query executed:" line per backend PID and
  attaches it to the next trace block from that PID. For explicit transactions
  this is usually the terminal statement that triggered the flush, often
  COMMIT.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# Keep this schema aligned with LATENCY_STAGE_* in src/include/latency_instr.h.
KNOWN_STAGES: Tuple[str, ...] = (
    "backend_spawn",
    "client_session_establish",
    "client_command_wait",
    "client_command_receive",
    "backend_parse_plan",
    "client_bind_complete_send",
    "client_describe_response_send",
    "client_result_send",
    "worker_session_acquire",
    "placement_bind",
    "remote_tx_attach",
    "remote_command_dispatch",
    "remote_command_flush",
    "remote_command_wait",
    "remote_result_drain",
    "remote_tx_commit",
    "remote_tx_abort",
    "remote_tx_prepare",
    "worker_session_release",
    "client_command_complete",
    "client_ready_for_query",
    "backend_command_turnaround",
    "client_session_teardown",
)


TRACE_HEADER_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\S+)\s+"
    r"\[(?P<pid>\d+)\]\s+LOG:\s+LatencyTraceIdentity:\s*(?P<identity>.*)$"
)
TRACE_COMMAND_TAG_RE = re.compile(r"^\s*LatencyTraceCommandTag:\s*(?P<tag>.*)$")
TRACE_DROPPED_RE = re.compile(r"^\s*LatencyTraceDroppedEntries:\s*(?P<dropped>\d+)\s*$")
QUERY_EXECUTED_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\S+)\s+"
    r"\[(?P<pid>\d+)\]\s+LOG:\s+.*Query executed:\s*(?P<query>.*)$"
)
TIMING_HEADER_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}\s+\S+)\s+"
    r"\[(?P<pid>\d+)\]\s+LOG:\s+DistributedTransactionId:\s*(?P<identity>.*)$"
)
REMOTE_COMMAND_TAG_RE = re.compile(r"^\s*RemoteCommandTag:\s*(?P<tag>.*)$")
TIMER_ROW_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(.*)$")
REMOTE_COMMAND_TAG_PAYLOAD_RE = re.compile(
    r"\brsid=(?P<rsid>\d+)\s+rcid=(?P<rcid>\d+\.\d+)\s+kind=(?P<kind>[A-Za-z_]+)\b"
)
IDENTITY_DTXID_RE = re.compile(r"\bdtxid=(?P<dtxid>\d+:\d+)\b")

STAGE_KIND_MAP: Dict[str, Tuple[str, ...]] = {
    "task_query": (
        "placement_bind",
        "remote_command_dispatch",
        "remote_command_flush",
        "remote_command_wait",
        "remote_result_drain",
    ),
    "tx_begin": ("remote_tx_attach",),
    "tx_commit": ("remote_tx_commit",),
    "tx_abort": ("remote_tx_abort",),
    "tx_prepare": ("remote_tx_prepare",),
}

ACTUAL_TIMER_CANDIDATES: Tuple[str, ...] = ("QUERY_ACTIVE_WALL", "ExecSimpleQuery")
RESULT_TIMER_CANDIDATES: Tuple[str, ...] = ("Printtup",)
WORKER_WAIT_TIMER_CANDIDATES: Tuple[str, ...] = ("QUERY_WAIT_WALL",)
WORKER_RECEIVE_PROTOCOL_TIMER_LEAVES: Tuple[str, ...] = (
    "PG_BE_SOCK_READ",
    "PQ_getbyte",
    "PQ_getmessage",
    "PQ_getbytes",
)
WORKER_RESULT_SERDE_TIMER_LEAVES: Tuple[str, ...] = ("Printtup_Ser",)
WORKER_SEND_PROTOCOL_TIMER_LEAVES: Tuple[str, ...] = (
    "PQ_putmessage",
    "PG_BE_SOCK_WRITE",
)


@dataclass
class StageInstance:
    """One row in the ordered latency trace table."""

    stage_name: str
    remote_session_id: int
    remote_command_id: str
    start_offset_ns: int
    end_offset_ns: int
    duration_ns: int
    abs_start_ns: int
    abs_end_ns: int


@dataclass
class RemoteCommandTag:
    """Structured form of the rsid/rcid/kind tag propagated to workers."""

    rsid: int
    rcid: str
    kind: str


@dataclass
class WorkerTimingBlock:
    """One worker timing report with a RemoteCommandTag."""

    source_file: str
    timestamp: str
    timestamp_ns: int
    pid: int
    identity: str
    command_tag: str
    parsed_tag: RemoteCommandTag
    timers: Dict[str, int]


@dataclass
class TimingReportBlock:
    """One raw timing report, optionally tagged with a RemoteCommandTag."""

    source_file: str
    timestamp: str
    timestamp_ns: int
    pid: int
    identity: str
    command_tag: str
    parsed_tag: Optional[RemoteCommandTag]
    timers: Dict[str, int]


@dataclass
class TraceBlock:
    """One ordered latency trace block as logged by one backend."""

    source_file: str
    row_index: int
    timestamp: str
    timestamp_ns: int
    pid: int
    identity: str
    command_tag: str = ""
    dropped_entries: int = 0
    preceding_query: str = ""
    stage_duration_ns: Dict[str, int] = field(default_factory=dict)
    stage_count: Dict[str, int] = field(default_factory=dict)
    stage_first_start_ns: Dict[str, int] = field(default_factory=dict)
    stage_last_end_ns: Dict[str, int] = field(default_factory=dict)
    stage_instances: List[StageInstance] = field(default_factory=list)
    raw_stage_order: List[str] = field(default_factory=list)
    max_end_offset_ns: int = 0
    worker_session_acquire_actual_ns: int = 0
    worker_session_acquire_comm_ns: int = 0
    remote_tx_attach_actual_ns: int = 0
    remote_tx_attach_comm_ns: int = 0
    remote_command_wait_actual_ns: int = 0
    remote_command_wait_comm_ns: int = 0
    remote_result_drain_actual_ns: int = 0
    remote_result_drain_comm_ns: int = 0
    remote_tx_commit_actual_ns: int = 0
    remote_tx_commit_comm_ns: int = 0
    remote_tx_abort_actual_ns: int = 0
    remote_tx_abort_comm_ns: int = 0
    remote_tx_prepare_actual_ns: int = 0
    remote_tx_prepare_comm_ns: int = 0
    matched_worker_timing_blocks: int = 0
    matched_worker_trace_blocks: int = 0
    unmatched_worker_timing_blocks: int = 0

    @property
    def transaction_key(self) -> str:
        """Prefer logged identity, otherwise synthesize a stable row key."""

        if self.identity:
            return self.identity

        return f"{self.timestamp}|pid={self.pid}|row={self.row_index}"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse ordered latency trace blocks from PostgreSQL/Citus logs into CSV."
    )
    parser.add_argument(
        "logfiles",
        nargs="+",
        help="Input PostgreSQL/Citus log file(s). One ordered latency trace block becomes one CSV row.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Output CSV path. Defaults to stdout.",
    )
    parser.add_argument(
        "--include-counts",
        action="store_true",
        help="Also emit <stage>_count columns showing how many times each stage occurred in the trace.",
    )
    parser.add_argument(
        "--worker-log",
        action="append",
        default=[],
        help=(
            "Worker PostgreSQL log file(s). When provided, the parser joins worker timing "
            "reports and worker ordered traces back into each coordinator transaction row."
        ),
    )
    return parser.parse_args(argv)


def parse_log_timestamp_ns(timestamp: str) -> int:
    """Parse PostgreSQL log timestamps into comparable nanoseconds.

    The logs include a timezone abbreviation, but for cross-node correlation in
    this parser the wall-clock order is sufficient. We therefore parse the date
    and time portion and intentionally ignore the trailing timezone token.
    """

    tokens = timestamp.split()
    if len(tokens) < 2:
        raise ValueError(f"invalid log timestamp: {timestamp!r}")

    dt = datetime.strptime(f"{tokens[0]} {tokens[1]}", "%Y-%m-%d %H:%M:%S.%f")
    return int(dt.timestamp() * 1_000_000_000)


def parse_remote_command_tag(tag: str) -> Optional[RemoteCommandTag]:
    """Parse 'rsid=... rcid=... kind=...' into a structured tag."""

    match = REMOTE_COMMAND_TAG_PAYLOAD_RE.search(tag)
    if match is None:
        return None

    return RemoteCommandTag(
        rsid=int(match.group("rsid")),
        rcid=match.group("rcid"),
        kind=match.group("kind"),
    )


def extract_distributed_transaction_id(identity: str) -> Optional[str]:
    """Extract the globally comparable dtxid suffix from one logger identity.

    The latency/timing logs include backend-local connection identity details
    ahead of the distributed transaction id. Those local pid/sequence fields do
    not match between coordinator and workers, so worker joins should compare
    only the shared dtxid portion when it exists.
    """

    match = IDENTITY_DTXID_RE.search(identity)
    if match is None:
        return None

    return match.group("dtxid")


def parse_stage_row(line: str) -> Optional[Tuple[str, int, str, int, int, int]]:
    """Parse one row from the ordered latency trace table.

    Returns:
        (stage_name, remote_session_id, remote_command_id,
         start_offset_ns, end_offset_ns, duration_ns)
    """

    if "|" not in line:
        return None

    parts = [part.strip() for part in line.rstrip("\n").split("|")]
    if len(parts) != 7:
        return None

    # The first column is the numeric row index. Ignore it if malformed.
    try:
        int(parts[0])
    except ValueError:
        return None

    stage_name = parts[1]
    if not stage_name:
        return None

    try:
        remote_session_id = int(parts[2]) if parts[2] else 0
        start_offset_ns = int(parts[4]) if parts[4] else 0
        end_offset_ns = int(parts[5]) if parts[5] else 0
        duration_ns = int(parts[6]) if parts[6] else 0
    except ValueError:
        return None

    return stage_name, remote_session_id, parts[3], start_offset_ns, end_offset_ns, duration_ns


def parse_latency_trace_blocks(logfile: Path, row_index_start: int = 0) -> List[TraceBlock]:
    """Parse every ordered latency trace block in one logfile."""

    rows: List[TraceBlock] = []
    last_query_by_pid: Dict[int, str] = {}

    with logfile.open("r", encoding="utf-8", errors="replace") as f:
        lines = list(f)

    i = 0
    row_index = row_index_start

    while i < len(lines):
        line = lines[i]

        query_match = QUERY_EXECUTED_RE.match(line)
        if query_match:
            last_query_by_pid[int(query_match.group("pid"))] = query_match.group("query").strip()
            i += 1
            continue

        header_match = TRACE_HEADER_RE.match(line)
        if not header_match:
            i += 1
            continue

        pid = int(header_match.group("pid"))
        block = TraceBlock(
            source_file=str(logfile),
            row_index=row_index,
            timestamp=header_match.group("timestamp"),
            timestamp_ns=parse_log_timestamp_ns(header_match.group("timestamp")),
            pid=pid,
            identity=header_match.group("identity").strip(),
            preceding_query=last_query_by_pid.get(pid, ""),
        )
        row_index += 1

        i += 1

        # Parse the fixed block header lines until the stage table begins.
        while i < len(lines):
            current = lines[i]

            command_tag_match = TRACE_COMMAND_TAG_RE.match(current)
            if command_tag_match:
                block.command_tag = command_tag_match.group("tag").strip()
                i += 1
                continue

            dropped_match = TRACE_DROPPED_RE.match(current)
            if dropped_match:
                block.dropped_entries = int(dropped_match.group("dropped"))
                i += 1
                continue

            if "--- Ordered Latency Trace" in current:
                i += 1
                break

            i += 1

        # Skip column header and separator lines until real stage rows begin.
        while i < len(lines):
            current = lines[i]
            parsed = parse_stage_row(current)
            if parsed is not None:
                break

            # A separator after the table means an empty / malformed block.
            if set(current.strip()) == {"-"} and current.strip():
                i += 1
                continue

            i += 1

        # Consume stage rows.
        while i < len(lines):
            current = lines[i]
            parsed = parse_stage_row(current)
            if parsed is None:
                # The table ends at the next separator / blank / unrelated log line.
                if current.strip() == "" or set(current.strip()) == {"-"}:
                    i += 1
                    continue
                break

            (
                stage_name,
                remote_session_id,
                remote_command_id,
                start_offset_ns,
                end_offset_ns,
                duration_ns,
            ) = parsed
            block.raw_stage_order.append(stage_name)
            block.stage_duration_ns[stage_name] = block.stage_duration_ns.get(stage_name, 0) + duration_ns
            block.stage_count[stage_name] = block.stage_count.get(stage_name, 0) + 1
            if stage_name not in block.stage_first_start_ns:
                block.stage_first_start_ns[stage_name] = start_offset_ns
            block.stage_last_end_ns[stage_name] = end_offset_ns
            block.max_end_offset_ns = max(block.max_end_offset_ns, end_offset_ns)
            block.stage_instances.append(
                StageInstance(
                    stage_name=stage_name,
                    remote_session_id=remote_session_id,
                    remote_command_id=remote_command_id,
                    start_offset_ns=start_offset_ns,
                    end_offset_ns=end_offset_ns,
                    duration_ns=duration_ns,
                    abs_start_ns=0,
                    abs_end_ns=0,
                )
            )
            i += 1

        trace_start_ns = block.timestamp_ns - block.max_end_offset_ns
        for stage_instance in block.stage_instances:
            stage_instance.abs_start_ns = trace_start_ns + stage_instance.start_offset_ns
            stage_instance.abs_end_ns = trace_start_ns + stage_instance.end_offset_ns

        rows.append(block)

    return rows


def parse_timer_row(line: str) -> Optional[Tuple[str, int]]:
    """Parse one timing report row into (timer_name, total_time_ns)."""

    match = TIMER_ROW_RE.match(line.rstrip("\n"))
    if match is None:
        return None

    parts = [part.strip() for part in match.group(2).split("|")]
    if len(parts) < 2:
        return None

    try:
        total_time_ns = int(parts[1])
    except ValueError:
        return None

    return match.group(1), total_time_ns


def parse_timing_report_blocks(logfile: Path) -> List[TimingReportBlock]:
    """Parse every timing report block in one logfile.

    Worker reports carry a propagated RemoteCommandTag, while coordinator
    reports usually leave that tag blank. Keeping the raw parser generic lets
    callers reuse the same timing-report reader for both sides and then decide
    how to associate those reports with ordered-trace rows.
    """

    rows: List[TimingReportBlock] = []

    with logfile.open("r", encoding="utf-8", errors="replace") as f:
        lines = list(f)

    i = 0
    while i < len(lines):
        header_match = TIMING_HEADER_RE.match(lines[i])
        if header_match is None:
            i += 1
            continue

        timestamp = header_match.group("timestamp")
        pid = int(header_match.group("pid"))
        identity = header_match.group("identity").strip()
        i += 1

        command_tag = ""
        while i < len(lines):
            current = lines[i]

            tag_match = REMOTE_COMMAND_TAG_RE.match(current)
            if tag_match is not None:
                command_tag = tag_match.group("tag").strip()
                i += 1
                continue

            if "--- Timing Report" in current:
                i += 1
                break

            i += 1

        timers: Dict[str, int] = {}

        # Skip the timing-table column header and separator lines first.
        while i < len(lines):
            current = lines[i]
            parsed_timer = parse_timer_row(current)
            if parsed_timer is not None:
                break

            if current.strip() == "" or set(current.strip()) == {"-"}:
                i += 1
                continue

            if "Timer Name" in current and "Total Time (ns)" in current:
                i += 1
                continue

            i += 1

        while i < len(lines):
            current = lines[i]
            parsed_timer = parse_timer_row(current)
            if parsed_timer is None:
                if current.strip() == "" or set(current.strip()) == {"-"}:
                    i += 1
                    continue
                break

            timer_name, total_time_ns = parsed_timer
            timers[timer_name] = total_time_ns
            i += 1

        rows.append(
            TimingReportBlock(
                source_file=str(logfile),
                timestamp=timestamp,
                timestamp_ns=parse_log_timestamp_ns(timestamp),
                pid=pid,
                identity=identity,
                command_tag=command_tag,
                parsed_tag=parse_remote_command_tag(command_tag),
                timers=timers,
            )
        )

    return rows


def parse_worker_timing_blocks(logfile: Path) -> List[WorkerTimingBlock]:
    """Parse worker timing reports that carry a RemoteCommandTag."""

    rows: List[WorkerTimingBlock] = []

    for timing_block in parse_timing_report_blocks(logfile):
        if timing_block.parsed_tag is None:
            continue

        rows.append(
            WorkerTimingBlock(
                source_file=timing_block.source_file,
                timestamp=timing_block.timestamp,
                timestamp_ns=timing_block.timestamp_ns,
                pid=timing_block.pid,
                identity=timing_block.identity,
                command_tag=timing_block.command_tag,
                parsed_tag=timing_block.parsed_tag,
                timers=timing_block.timers,
            )
        )

    return rows


def pick_timer_total(timers: Dict[str, int], candidates: Tuple[str, ...]) -> int:
    """Return the first available timer total from a list of preferred names."""

    for timer_name in candidates:
        if timer_name in timers:
            return timers[timer_name]

    return 0


def sum_timer_totals(timers: Dict[str, int], timer_names: Tuple[str, ...]) -> int:
    """Sum a group of additive timer leaves defensively."""

    return sum(int(timers.get(timer_name, 0)) for timer_name in timer_names)


def worker_wait_total_ns(timers: Dict[str, int]) -> int:
    """Return the worker-side excluded-wait total for one timing report."""

    return pick_timer_total(timers, WORKER_WAIT_TIMER_CANDIDATES)


def worker_receive_protocol_total_ns(timers: Dict[str, int]) -> int:
    """Return additive worker receive/protocol CPU leaves."""

    return sum_timer_totals(timers, WORKER_RECEIVE_PROTOCOL_TIMER_LEAVES)


def worker_result_serde_total_ns(timers: Dict[str, int]) -> int:
    """Return worker row-serialization CPU leaves for query results."""

    return sum_timer_totals(timers, WORKER_RESULT_SERDE_TIMER_LEAVES)


def worker_send_protocol_total_ns(timers: Dict[str, int]) -> int:
    """Return additive worker send/protocol CPU leaves."""

    return sum_timer_totals(timers, WORKER_SEND_PROTOCOL_TIMER_LEAVES)


def worker_local_processing_total_ns(timers: Dict[str, int]) -> int:
    """Return the worker local-processing remainder within QUERY_ACTIVE_WALL.

    QUERY_ACTIVE_WALL is the best active-cycle denominator we have on the
    worker. Subtract the additive communication leaves that are already shown
    explicitly in the waterfall; whatever remains is worker-local backend work.
    """

    active_total_ns = pick_timer_total(timers, ACTUAL_TIMER_CANDIDATES)
    communication_cpu_ns = (
        worker_receive_protocol_total_ns(timers) +
        worker_result_serde_total_ns(timers) +
        worker_send_protocol_total_ns(timers)
    )
    return clamp_residual(active_total_ns, communication_cpu_ns)


def trace_matches_rcid_kind(trace: TraceBlock, rcid: str, kind: str) -> bool:
    """Whether a coordinator trace contains the given rcid/kind stage."""

    allowed_stages = STAGE_KIND_MAP.get(kind)
    if allowed_stages is None:
        return False

    return any(
        stage.remote_command_id == rcid and stage.stage_name in allowed_stages
        for stage in trace.stage_instances
    )


def stage_distance_ns(trace: TraceBlock, rcid: str, kind: str, timestamp_ns: int) -> int:
    """Compute how far a worker command timestamp is from the matching coordinator stage."""

    allowed_stages = STAGE_KIND_MAP.get(kind, ())
    distances: List[int] = []

    for stage in trace.stage_instances:
        if stage.remote_command_id != rcid or stage.stage_name not in allowed_stages:
            continue

        if stage.abs_end_ns != 0 and stage.abs_start_ns <= timestamp_ns <= stage.abs_end_ns:
            return 0

        if stage.abs_end_ns != 0:
            distances.append(min(abs(timestamp_ns - stage.abs_start_ns), abs(timestamp_ns - stage.abs_end_ns)))
        else:
            distances.append(abs(timestamp_ns - stage.abs_start_ns))

    if not distances:
        return abs(timestamp_ns - trace.timestamp_ns)

    return min(distances)


def find_best_trace_for_worker_command(
    candidate_traces: List[TraceBlock], rcid: str, kind: str, timestamp_ns: int
) -> Optional[TraceBlock]:
    """Choose the coordinator transaction row that best matches one worker command."""

    if not candidate_traces:
        return None

    return min(
        candidate_traces,
        key=lambda trace: (stage_distance_ns(trace, rcid, kind, timestamp_ns), abs(trace.timestamp_ns - timestamp_ns)),
    )


def filter_traces_by_shared_dtxid(
    candidate_traces: List[TraceBlock], worker_identity: str
) -> List[TraceBlock]:
    """Prefer rows whose distributed transaction id matches the worker block.

    rcid values are only unique within one coordinator backend. Under
    concurrency, different coordinator backends can therefore reuse the same
    rsid.rcid string in the same millisecond. When both sides logged a dtxid,
    narrow the candidate set first so the later timestamp heuristic only breaks
    ties within the same distributed transaction.
    """

    worker_dtxid = extract_distributed_transaction_id(worker_identity)
    if worker_dtxid is None:
        return candidate_traces

    dtxid_matched_traces = [
        trace
        for trace in candidate_traces
        if extract_distributed_transaction_id(trace.identity) == worker_dtxid
    ]
    if dtxid_matched_traces:
        return dtxid_matched_traces

    return candidate_traces


def expected_kind_for_stage(stage_name: str) -> Optional[str]:
    """Map a coordinator stage name back to the propagated command kind."""

    for kind, stage_names in STAGE_KIND_MAP.items():
        if stage_name in stage_names:
            return kind

    return None


def first_remote_command_for_session(
    trace: TraceBlock, remote_session_id: int
) -> Optional[Tuple[str, str]]:
    """Find the first (rcid, kind) observed on a newly acquired worker session."""

    matching = [
        stage
        for stage in trace.stage_instances
        if stage.remote_session_id == remote_session_id and stage.remote_command_id
    ]
    if not matching:
        return None

    matching.sort(key=lambda stage: (stage.start_offset_ns, stage.remote_command_id))
    first_stage = matching[0]
    expected_kind = expected_kind_for_stage(first_stage.stage_name)
    if expected_kind is None:
        return None

    return first_stage.remote_command_id, expected_kind


def clamp_residual(total_ns: int, actual_ns: int) -> int:
    """Avoid negative residuals when the worker clocks or accounting differ slightly."""

    return max(0, total_ns - actual_ns)


def match_worker_artifacts(
    coordinator_traces: List[TraceBlock],
    worker_timing_blocks: List[WorkerTimingBlock],
    worker_trace_blocks: List[TraceBlock],
) -> Tuple[Dict[int, List[WorkerTimingBlock]], Dict[Tuple[int, int], List[TraceBlock]]]:
    """Match worker timing/trace blocks back to coordinator trace rows.

    The returned dictionaries are keyed by:
    - coordinator row index for worker timing blocks
    - (coordinator row index, remote session id) for worker trace blocks

    Keeping this logic in one place ensures the CSV decomposition and the
    waterfall/swimlane plot use the same coordinator-worker association rules.
    """

    traces_by_rcid: Dict[str, List[TraceBlock]] = {}
    matched_worker_timings_by_row: Dict[int, List[WorkerTimingBlock]] = {}
    matched_worker_traces_by_trace_session: Dict[Tuple[int, int], List[TraceBlock]] = {}

    for trace in coordinator_traces:
        for stage in trace.stage_instances:
            if stage.remote_command_id:
                traces_by_rcid.setdefault(stage.remote_command_id, []).append(trace)

    for worker_block in worker_timing_blocks:
        rcid = worker_block.parsed_tag.rcid
        kind = worker_block.parsed_tag.kind
        candidate_traces = [
            trace
            for trace in traces_by_rcid.get(rcid, [])
            if trace_matches_rcid_kind(trace, rcid, kind)
        ]
        candidate_traces = filter_traces_by_shared_dtxid(
            candidate_traces, worker_block.identity
        )

        best_trace = find_best_trace_for_worker_command(
            candidate_traces, rcid, kind, worker_block.timestamp_ns
        )
        if best_trace is None:
            continue

        matched_worker_timings_by_row.setdefault(best_trace.row_index, []).append(worker_block)

    for worker_trace in worker_trace_blocks:
        parsed_tag = parse_remote_command_tag(worker_trace.command_tag)
        if parsed_tag is None:
            continue

        candidate_traces = [
            trace
            for trace in traces_by_rcid.get(parsed_tag.rcid, [])
            if trace_matches_rcid_kind(trace, parsed_tag.rcid, parsed_tag.kind)
        ]
        candidate_traces = filter_traces_by_shared_dtxid(
            candidate_traces, worker_trace.identity
        )
        best_trace = find_best_trace_for_worker_command(
            candidate_traces, parsed_tag.rcid, parsed_tag.kind, worker_trace.timestamp_ns
        )
        if best_trace is None:
            continue

        matched_worker_traces_by_trace_session.setdefault(
            (best_trace.row_index, parsed_tag.rsid), []
        ).append(worker_trace)

    return matched_worker_timings_by_row, matched_worker_traces_by_trace_session


def attach_worker_decomposition(
    coordinator_traces: List[TraceBlock],
    worker_timing_blocks: List[WorkerTimingBlock],
    worker_trace_blocks: List[TraceBlock],
) -> None:
    """Join worker logs back into each coordinator transaction row.

    This intentionally supports any number of worker log files. All worker
    timing records are pooled, then matched by rcid/kind and timestamp
    proximity to the coordinator transaction rows.
    """

    traces_by_row_index = {trace.row_index: trace for trace in coordinator_traces}
    matched_worker_timings_by_row, matched_worker_traces_by_trace_session = match_worker_artifacts(
        coordinator_traces, worker_timing_blocks, worker_trace_blocks
    )

    for row_index, matched_worker_blocks in matched_worker_timings_by_row.items():
        best_trace = traces_by_row_index[row_index]
        for worker_block in matched_worker_blocks:
            kind = worker_block.parsed_tag.kind
            best_trace.matched_worker_timing_blocks += 1

            actual_ns = pick_timer_total(worker_block.timers, ACTUAL_TIMER_CANDIDATES)
            result_prepare_ns = pick_timer_total(worker_block.timers, RESULT_TIMER_CANDIDATES)

            if kind == "task_query":
                best_trace.remote_command_wait_actual_ns += actual_ns
                best_trace.remote_result_drain_actual_ns += result_prepare_ns
            elif kind == "tx_begin":
                best_trace.remote_tx_attach_actual_ns += actual_ns
            elif kind == "tx_commit":
                best_trace.remote_tx_commit_actual_ns += actual_ns
            elif kind == "tx_abort":
                best_trace.remote_tx_abort_actual_ns += actual_ns
            elif kind == "tx_prepare":
                best_trace.remote_tx_prepare_actual_ns += actual_ns

    for trace in coordinator_traces:
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
            trace.worker_session_acquire_actual_ns += best_worker_trace.stage_duration_ns.get(
                "backend_spawn", 0
            )
            trace.worker_session_acquire_actual_ns += best_worker_trace.stage_duration_ns.get(
                "client_session_establish", 0
            )
            trace.matched_worker_trace_blocks += 1

        trace.worker_session_acquire_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("worker_session_acquire", 0),
            trace.worker_session_acquire_actual_ns,
        )
        trace.remote_tx_attach_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_tx_attach", 0),
            trace.remote_tx_attach_actual_ns,
        )
        trace.remote_command_wait_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_command_wait", 0),
            trace.remote_command_wait_actual_ns,
        )
        trace.remote_result_drain_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_result_drain", 0),
            trace.remote_result_drain_actual_ns,
        )
        trace.remote_tx_commit_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_tx_commit", 0),
            trace.remote_tx_commit_actual_ns,
        )
        trace.remote_tx_abort_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_tx_abort", 0),
            trace.remote_tx_abort_actual_ns,
        )
        trace.remote_tx_prepare_comm_ns = clamp_residual(
            trace.stage_duration_ns.get("remote_tx_prepare", 0),
            trace.remote_tx_prepare_actual_ns,
        )


def collect_dynamic_stages(rows: Iterable[TraceBlock]) -> List[str]:
    """Return stage names not already present in the stable schema."""

    extras = set()
    for row in rows:
        extras.update(stage for stage in row.stage_duration_ns if stage not in KNOWN_STAGES)

    return sorted(extras)


def write_csv(rows: List[TraceBlock], output_path: str, include_counts: bool) -> None:
    """Write parsed trace blocks into a flat CSV."""

    dynamic_stages = collect_dynamic_stages(rows)
    ordered_stages = list(KNOWN_STAGES) + dynamic_stages

    fieldnames = [
        "row_index",
        "transaction_key",
        "source_file",
        "timestamp",
        "pid",
        "identity",
        "command_tag",
        "preceding_query",
        "dropped_entries",
        "trace_total_ns",
        "worker_session_acquire_actual_ns",
        "worker_session_acquire_comm_ns",
        "remote_tx_attach_actual_ns",
        "remote_tx_attach_comm_ns",
        "remote_command_wait_actual_ns",
        "remote_command_wait_comm_ns",
        "remote_result_drain_actual_ns",
        "remote_result_drain_comm_ns",
        "remote_tx_commit_actual_ns",
        "remote_tx_commit_comm_ns",
        "remote_tx_abort_actual_ns",
        "remote_tx_abort_comm_ns",
        "remote_tx_prepare_actual_ns",
        "remote_tx_prepare_comm_ns",
        "matched_worker_timing_blocks",
        "matched_worker_trace_blocks",
        "unmatched_worker_timing_blocks",
    ]
    fieldnames.extend(f"{stage}_ns" for stage in ordered_stages)

    if include_counts:
        fieldnames.extend(f"{stage}_count" for stage in ordered_stages)

    output_stream = sys.stdout if output_path == "-" else open(output_path, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(output_stream, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            csv_row = {
                "row_index": row.row_index,
                "transaction_key": row.transaction_key,
                "source_file": row.source_file,
                "timestamp": row.timestamp,
                "pid": row.pid,
                "identity": row.identity,
                "command_tag": row.command_tag,
                "preceding_query": row.preceding_query,
                "dropped_entries": row.dropped_entries,
                "trace_total_ns": row.max_end_offset_ns,
                "worker_session_acquire_actual_ns": row.worker_session_acquire_actual_ns,
                "worker_session_acquire_comm_ns": row.worker_session_acquire_comm_ns,
                "remote_tx_attach_actual_ns": row.remote_tx_attach_actual_ns,
                "remote_tx_attach_comm_ns": row.remote_tx_attach_comm_ns,
                "remote_command_wait_actual_ns": row.remote_command_wait_actual_ns,
                "remote_command_wait_comm_ns": row.remote_command_wait_comm_ns,
                "remote_result_drain_actual_ns": row.remote_result_drain_actual_ns,
                "remote_result_drain_comm_ns": row.remote_result_drain_comm_ns,
                "remote_tx_commit_actual_ns": row.remote_tx_commit_actual_ns,
                "remote_tx_commit_comm_ns": row.remote_tx_commit_comm_ns,
                "remote_tx_abort_actual_ns": row.remote_tx_abort_actual_ns,
                "remote_tx_abort_comm_ns": row.remote_tx_abort_comm_ns,
                "remote_tx_prepare_actual_ns": row.remote_tx_prepare_actual_ns,
                "remote_tx_prepare_comm_ns": row.remote_tx_prepare_comm_ns,
                "matched_worker_timing_blocks": row.matched_worker_timing_blocks,
                "matched_worker_trace_blocks": row.matched_worker_trace_blocks,
                "unmatched_worker_timing_blocks": row.unmatched_worker_timing_blocks,
            }

            for stage in ordered_stages:
                csv_row[f"{stage}_ns"] = row.stage_duration_ns.get(stage, 0)

            if include_counts:
                for stage in ordered_stages:
                    csv_row[f"{stage}_count"] = row.stage_count.get(stage, 0)

            writer.writerow(csv_row)
    finally:
        if output_stream is not sys.stdout:
            output_stream.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    coordinator_rows: List[TraceBlock] = []
    next_row_index = 0

    for logfile_arg in args.logfiles:
        logfile = Path(logfile_arg)
        try:
            logfile_exists = logfile.exists()
        except OSError as exc:
            print(f"error: cannot access logfile {logfile}: {exc}", file=sys.stderr)
            return 1

        if not logfile_exists:
            print(f"error: logfile does not exist: {logfile}", file=sys.stderr)
            return 1

        try:
            parsed_rows = parse_latency_trace_blocks(logfile, row_index_start=next_row_index)
        except OSError as exc:
            print(f"error: failed to parse logfile {logfile}: {exc}", file=sys.stderr)
            return 1

        coordinator_rows.extend(parsed_rows)
        next_row_index += len(parsed_rows)

    worker_timing_blocks: List[WorkerTimingBlock] = []
    worker_trace_blocks: List[TraceBlock] = []
    for logfile_arg in args.worker_log:
        logfile = Path(logfile_arg)
        try:
            logfile_exists = logfile.exists()
        except OSError as exc:
            print(f"error: cannot access worker logfile {logfile}: {exc}", file=sys.stderr)
            return 1

        if not logfile_exists:
            print(f"error: worker logfile does not exist: {logfile}", file=sys.stderr)
            return 1

        try:
            worker_timing_blocks.extend(parse_worker_timing_blocks(logfile))
            worker_trace_blocks.extend(parse_latency_trace_blocks(logfile))
        except OSError as exc:
            print(f"error: failed to parse worker logfile {logfile}: {exc}", file=sys.stderr)
            return 1

    if worker_timing_blocks or worker_trace_blocks:
        attach_worker_decomposition(coordinator_rows, worker_timing_blocks, worker_trace_blocks)

    try:
        write_csv(coordinator_rows, args.output, args.include_counts)
    except OSError as exc:
        print(f"error: failed to write CSV {args.output}: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
