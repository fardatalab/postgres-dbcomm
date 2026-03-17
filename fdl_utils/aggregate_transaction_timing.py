#!/usr/bin/env python3
"""aggregate_transaction_timing.py

Parse PostgreSQL/Citus logs and aggregate timing reports per *transaction*.

aggregates timers within each logged transaction.

- A "transaction log block" starts when we see a line containing "Query executed: BEGIN".
- It ends when we see a transaction terminator such as:
    - "Query executed: END" / "COMMIT" / "ROLLBACK" (coordinator), or
    - "Query executed: COMMIT PREPARED" (worker).
- Timing report blocks are attributed to the correct transaction using the logged
    DistributedTransactionId line, which can appear immediately before a timing report block.
    This enables correct aggregation even when multiple transactions are interleaved.

Special handling:
- Within a transaction, any timing report block that contains *only* the single timer
  "PG_WAIT_DONT_COUNT" is discarded and does not contribute to aggregation.

Output:
- CSV to stdout (or to a file via --output), with one row per transaction.
- Columns are timer names (union across all parsed transactions).
- Cell values are the aggregated "Total Time (ns)" for that timer within that transaction.

Notes:
- aggregate by "Total Time (ns)" because the CSV is "timer names as columns" (one value per timer).
We still parse Count for validation but do not emit it.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import DefaultDict, Dict, Iterable, List, Optional, Set, Tuple

from timing_bucket_utils import (
    QUERY_ACTIVE_WALL_QUALITY,
    QUERY_EXEC_CORE_QUALITY,
    XACT_LIFECYCLE_WALL_QUALITY,
    build_bucket_values,
    safe_pct,
    summary_notes,
)


# Regex used to parse timer lines within a timing report table.
# Example:
#   PG_WAIT_DONT_COUNT             |          1 |                270 |                270 |
_TIMER_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(.*)$")


# Separator line used in the timing report table.
# Example:
#   -----------------------------------------------------------------------------------------------------------
_SEPARATOR_RE = re.compile(r"^\s*-{10,}\s*$")


# Transaction delimiters.
_BEGIN_RE = re.compile(r"Query executed:\s*BEGIN\b")
_COMMIT_RE = re.compile(r"Query executed:\s*COMMIT\b(?!\s+PREPARED)")
_COMMIT_PREPARED_RE = re.compile(r"Query executed:\s*COMMIT\s+PREPARED\b")
_END_RE = re.compile(r"Query executed:\s*END\b")
_ROLLBACK_RE = re.compile(r"Query executed:\s*ROLLBACK\b")


# Distributed transaction id marker.
# Example:
#   DistributedTransactionId: conn_txn=pid=106004,start=1766434437,seq=1 dtxid=0:3
# The "composite key" we use is the conn_txn=... portion (we ignore any trailing dtxid=...).
_DISTRIBUTED_TXN_RE = re.compile(r"DistributedTransactionId:\s*(.*)$")


# PID marker in normal PostgreSQL log prefix: "[12345]".
_PID_RE = re.compile(r"\[(\d+)\]")


# The timer name that indicates "ignore-only" timing blocks.
_IGNORE_ONLY_TIMER_NAME = "PG_WAIT_DONT_COUNT"


@dataclass
class TransactionAggregate:
    """Aggregated timer values for a single transaction."""

    index: int
    # A stable identifier derived from the log (preferred: DistributedTransactionId conn_txn=...)
    txn_key: str
    # Aggregate per timer: total time in ns
    timer_total_ns: DefaultDict[str, int]


def _extract_pid_from_log_line(line: str) -> Optional[int]:
    """Extract the backend PID from a log line.

    Most relevant lines include a prefix like:
      2025-... [106003] LOG: ...
    Timing report body lines do not include the prefix, so callers should track the
    last-seen pid context for attribution.
    """

    match = _PID_RE.search(line)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _extract_conn_txn_key_from_distributed_txn_line(line: str) -> Optional[Tuple[int, str]]:
    """Extract (pid, conn_txn_key) from a DistributedTransactionId log line.

    The returned key is the "conn_txn=..." portion, with any trailing "dtxid=..." removed.
    Returns None for empty/unknown DistributedTransactionId formats (e.g., only dtxid=...).
    """

    match = _DISTRIBUTED_TXN_RE.search(line)
    if not match:
        return None

    payload = match.group(1).strip()
    if payload == "":
        return None

    # Only consider entries that include conn_txn=...; standalone statements may log only dtxid=...
    # or an empty DistributedTransactionId, which we ignore for transaction attribution.
    if "conn_txn=" not in payload:
        return None

    # Remove trailing dtxid=... if present.
    payload = payload.split(" dtxid=", 1)[0].strip()

    # We expect the composite key to include "pid=<n>".
    pid_match = re.search(r"\bpid=(\d+)\b", payload)
    if not pid_match:
        return None

    try:
        pid = int(pid_match.group(1))
    except ValueError:
        return None

    return pid, payload


def _parse_timer_table_line(
    line: str,
    *,
    line_number: int,
) -> Optional[Tuple[str, int, int]]:
    """Parse a single timing table row.

    Returns (timer_name, count, total_time_ns) if the line looks like a timer row,
    otherwise returns None.

    We keep parsing logic close to aggregate_timing.py, but focus on just the numeric columns
    we care about.
    """

    match = _TIMER_LINE_RE.match(line.rstrip("\n"))
    if not match:
        return None

    timer_name = match.group(1)
    rest = match.group(2)

    # Split the rest by pipe character.
    parts = [p.strip() for p in rest.split("|")]

    # We expect at least: Count | Total Time | Average Time | ...
    if len(parts) < 3:
        return None

    # Parse count and total_time. If malformed, warn and skip this line.
    try:
        count = int(parts[0])
    except ValueError:
        print(
            f"Warning (line {line_number}): Invalid count '{parts[0]}' for timer '{timer_name}', skipping row",
            file=sys.stderr,
        )
        return None

    try:
        total_time_ns = int(parts[1])
    except ValueError:
        print(
            f"Warning (line {line_number}): Invalid total time '{parts[1]}' for timer '{timer_name}', skipping row",
            file=sys.stderr,
        )
        return None

    return timer_name, count, total_time_ns


def _extract_timing_report_block(
    lines: Iterable[Tuple[int, str]],
) -> Tuple[Dict[str, int], bool]:
    """Extract one timing report block from the provided line iterator.

    The caller must have already consumed the line containing:
      --- Timing Report (Nanoseconds) ---

    We will consume subsequent lines until the end of the table.

    Returns:
    - block_totals: mapping timer_name -> total_time_ns for this *single* block
    - ended_cleanly: whether we saw the expected terminating separator

    Important: this function advances the iterator as it reads.
    """

    block_totals: Dict[str, int] = {}

    # The table usually looks like:
    #   Timer Name | Count | Total Time (ns) | Average | Custom
    #   -----------
    #   <rows>
    #   -----------
    # We ignore the header lines and only parse timer rows.
    in_data_rows = False

    for line_number, line in lines:
        stripped = line.strip("\n")

        # Detect separator lines; after we have started parsing data rows, a separator ends the block.
        if _SEPARATOR_RE.match(stripped):
            if in_data_rows:
                return block_totals, True
            # The first separator after the header begins data rows.
            # We keep scanning until we see timer rows.
            continue

        parsed = _parse_timer_table_line(stripped, line_number=line_number)
        if parsed is None:
            # Non-row line (blank line, header, etc.). Keep scanning.
            continue

        in_data_rows = True
        timer_name, _count, total_time_ns = parsed

        # Aggregate within the block.
        # Note: timer names can repeat within a block in malformed logs; sum defensively.
        block_totals[timer_name] = block_totals.get(timer_name, 0) + total_time_ns

    # Iterator ended before we saw a terminating separator.
    return block_totals, False


def parse_transactions_from_log(filepath: str) -> List[TransactionAggregate]:
    """Parse a log file and return per-transaction aggregated timing totals."""

    # All known transactions by key (keyed by DistributedTransactionId conn_txn=... when available).
    transactions_by_key: Dict[str, TransactionAggregate] = {}

    # Preserve creation order via a monotonically increasing index.
    next_transaction_index = 1

    # Track which transaction key is currently active for each pid.
    active_txn_key_for_pid: Dict[int, str] = {}

    # Some transaction-ending statements (END / COMMIT PREPARED) emit their own timing report
    # *after* the end marker line. We therefore mark a pid as "pending close" and actually close
    # after we have consumed the next timing report block for that pid.
    pending_close_for_pid: Set[int] = set()

    # Track which pid we are currently in context for (timing report body lines do not include pid).
    last_pid_context: Optional[int] = None

    def _get_or_create_transaction(txn_key: str) -> TransactionAggregate:
        nonlocal next_transaction_index

        existing = transactions_by_key.get(txn_key)
        if existing is not None:
            return existing

        created = TransactionAggregate(
            index=next_transaction_index,
            txn_key=txn_key,
            timer_total_ns=defaultdict(int),
        )
        transactions_by_key[txn_key] = created
        next_transaction_index += 1
        return created

    # Global line iterator with line numbers, so helper functions can consume from it.
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        enumerated_lines = iter(enumerate(f, start=1))

        for line_number, line in enumerated_lines:
            # Update pid context whenever present.
            pid_in_line = _extract_pid_from_log_line(line)
            if pid_in_line is not None:
                last_pid_context = pid_in_line

            # DistributedTransactionId lines tell us exactly which transaction the subsequent timing
            # report block belongs to (even if transactions are interleaved).
            extracted = _extract_conn_txn_key_from_distributed_txn_line(line)
            if extracted is not None:
                pid_from_key, conn_txn_key = extracted
                _get_or_create_transaction(conn_txn_key)
                active_txn_key_for_pid[pid_from_key] = conn_txn_key
                continue

            # Detect transaction boundaries.
            if _BEGIN_RE.search(line):
                # BEGIN should include a pid prefix. If not, we cannot reliably attribute.
                if last_pid_context is None:
                    print(
                        f"Warning (line {line_number}): BEGIN encountered without pid context; ignoring",
                        file=sys.stderr,
                    )
                    continue

                # If we see a BEGIN while a transaction is already active for this pid, close it
                # (best-effort recovery for malformed logs).
                if last_pid_context in active_txn_key_for_pid:
                    print(
                        f"Warning (line {line_number}): BEGIN encountered while pid {last_pid_context} already has an open transaction; "
                        "closing it early",
                        file=sys.stderr,
                    )
                    del active_txn_key_for_pid[last_pid_context]
                    pending_close_for_pid.discard(last_pid_context)
                continue

            # Coordinator logs typically end a transaction with END/COMMIT/ROLLBACK.
            # Worker prepared transactions end with COMMIT PREPARED.
            if (
                _COMMIT_PREPARED_RE.search(line)
                or _COMMIT_RE.search(line)
                or _END_RE.search(line)
                or _ROLLBACK_RE.search(line)
            ):
                if last_pid_context is None:
                    print(
                        f"Warning (line {line_number}): Transaction end marker encountered without pid context; ignoring",
                        file=sys.stderr,
                    )
                    continue

                if last_pid_context not in active_txn_key_for_pid:
                    print(
                        f"Warning (line {line_number}): Transaction end marker encountered without an open transaction for pid {last_pid_context}; ignoring",
                        file=sys.stderr,
                    )
                    continue

                # Delay the actual close until after the next timing report block for this pid.
                pending_close_for_pid.add(last_pid_context)
                continue

            # Only aggregate timing blocks that occur inside a transaction.
            if last_pid_context is None:
                continue

            txn_key = active_txn_key_for_pid.get(last_pid_context)
            if txn_key is None:
                continue

            current_txn = transactions_by_key.get(txn_key)
            if current_txn is None:
                continue

            # Detect start of a timing report block.
            if "--- Timing Report (Nanoseconds) ---" in line:
                # Consume the timing block from the shared iterator.
                block_totals, ended_cleanly = _extract_timing_report_block(enumerated_lines)

                # If the block is malformed, warn but still process what we got.
                if not ended_cleanly:
                    print(
                        f"Warning (line {line_number}): Timing report block did not terminate cleanly; "
                        "processing parsed rows anyway",
                        file=sys.stderr,
                    )

                # Discard blocks that contain only PG_WAIT_DONT_COUNT.
                if len(block_totals) == 1 and _IGNORE_ONLY_TIMER_NAME in block_totals:
                    continue

                # Aggregate block totals into the current transaction.
                for timer_name, total_ns in block_totals.items():
                    current_txn.timer_total_ns[timer_name] += total_ns

                # If this pid was marked for closure by a transaction-end marker, close it
                # now that we have consumed a timing report block for the terminating statement.
                if last_pid_context in pending_close_for_pid:
                    pending_close_for_pid.discard(last_pid_context)
                    active_txn_key_for_pid.pop(last_pid_context, None)

    # If the log ended while there are active transactions, keep them (best-effort).
    if active_txn_key_for_pid:
        open_pids = ", ".join(str(pid) for pid in sorted(active_txn_key_for_pid.keys()))
        print(
            f"Warning: log ended with open transaction(s) for pid(s): {open_pids}; emitting partial aggregates",
            file=sys.stderr,
        )

    # Return transactions in index order (stable creation order).
    return sorted(transactions_by_key.values(), key=lambda t: t.index)


def write_transactions_csv(transactions: List[TransactionAggregate], output_file) -> None:
    """Write the per-transaction aggregates as a wide CSV."""

    # Determine the union of timer names across all transactions.
    all_timer_names: Set[str] = set()
    for txn in transactions:
        all_timer_names.update(txn.timer_total_ns.keys())

    # Stable column order for easy diffing.
    timer_columns = sorted(all_timer_names)

    # Always include identifying columns so rows are interpretable.
    fieldnames = ["transaction_index", "transaction_key", *timer_columns]

    writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()

    for txn in transactions:
        # csv.DictWriter values can be strings or numbers; we include both.
        row: Dict[str, object] = {"transaction_index": txn.index, "transaction_key": txn.txn_key}
        for name in timer_columns:
            row[name] = int(txn.timer_total_ns.get(name, 0))
        writer.writerow(row)


def write_transaction_bucket_csv(transactions: List[TransactionAggregate], output_file) -> None:
    """Write one summary row per transaction with bucket totals and denominators."""

    fieldnames = [
        "transaction_index",
        "transaction_key",
        "query_active_wall_ns",
        "query_wait_wall_ns",
        "transport_protocol_cpu_ns",
        "row_serde_cpu_ns",
        "result_materialization_cpu_ns",
        "file_comm_io_ns",
        "optional_connection_setup_cpu_ns",
        "communication_stack_core_total_ns",
        "communication_stack_extended_total_ns",
        "communication_stack_extended_with_optional_setup_total_ns",
        "nonadditive_debug_total_ns",
        "legacy_query_exec_core_ns",
        "legacy_distributed_tx_lifecycle_wall_ns",
        "core_pct_of_query_active_wall",
        "extended_pct_of_query_active_wall",
        "extended_with_optional_setup_pct_of_query_active_wall",
        "query_exec_core_quality",
        "query_active_wall_quality",
        "xact_lifecycle_wall_quality",
    ]

    writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()

    for txn in transactions:
        bucket_values = build_bucket_values(txn.timer_total_ns)
        query_active_total_ns = bucket_values["query_active_wall_ns"]
        query_exec_core_ns = bucket_values["query_exec_core_ns"]
        xact_total_ns = bucket_values["distributed_tx_lifecycle_wall_ns"]

        row: Dict[str, object] = {
            "transaction_index": txn.index,
            "transaction_key": txn.txn_key,
            "query_active_wall_ns": query_active_total_ns,
            "query_wait_wall_ns": bucket_values["query_wait_wall_ns"],
            "transport_protocol_cpu_ns": bucket_values["transport_protocol_cpu_ns"],
            "row_serde_cpu_ns": bucket_values["row_serde_cpu_ns"],
            "result_materialization_cpu_ns": bucket_values["result_materialization_cpu_ns"],
            "file_comm_io_ns": bucket_values["file_comm_io_ns"],
            "optional_connection_setup_cpu_ns": bucket_values["optional_connection_setup_cpu_ns"],
            "communication_stack_core_total_ns": bucket_values["communication_stack_core_total_ns"],
            "communication_stack_extended_total_ns": bucket_values["communication_stack_extended_total_ns"],
            "communication_stack_extended_with_optional_setup_total_ns": bucket_values[
                "communication_stack_extended_with_optional_setup_total_ns"
            ],
            "nonadditive_debug_total_ns": bucket_values["nonadditive_debug_total_ns"],
            "legacy_query_exec_core_ns": query_exec_core_ns,
            "legacy_distributed_tx_lifecycle_wall_ns": xact_total_ns,
            "core_pct_of_query_active_wall": safe_pct(
                bucket_values["communication_stack_core_total_ns"], query_active_total_ns
            ),
            "extended_pct_of_query_active_wall": safe_pct(
                bucket_values["communication_stack_extended_total_ns"], query_active_total_ns
            ),
            "extended_with_optional_setup_pct_of_query_active_wall": safe_pct(
                bucket_values["communication_stack_extended_with_optional_setup_total_ns"],
                query_active_total_ns,
            ),
            "query_exec_core_quality": QUERY_EXEC_CORE_QUALITY,
            "query_active_wall_quality": QUERY_ACTIVE_WALL_QUALITY,
            "xact_lifecycle_wall_quality": XACT_LIFECYCLE_WALL_QUALITY,
        }
        writer.writerow(row)


def main() -> None:
    """CLI entrypoint."""

    parser = argparse.ArgumentParser(
        description=(
            "Aggregate timing report blocks per transaction (BEGIN..END/COMMIT/"
            "ROLLBACK/COMMIT PREPARED). Summary view emits additive communication "
            "buckets plus the active-execution denominator and legacy/debug "
            "context timers."
        )
    )
    parser.add_argument("log_file", help="Path to the log file to parse")
    parser.add_argument(
        "-o",
        "--output",
        help="Write CSV output to this file instead of stdout",
        default=None,
    )
    parser.add_argument(
        "--view",
        choices=("summary", "raw"),
        default="summary",
        help=(
            "summary: per-transaction communication buckets and current denominator "
            "context timers; raw: original wide per-timer CSV."
        ),
    )

    args = parser.parse_args()

    transactions = parse_transactions_from_log(args.log_file)

    if not transactions:
        print("No transactions found (no BEGIN transaction blocks).", file=sys.stderr)
        sys.exit(1)

    if args.view == "summary":
        for note in summary_notes():
            print(f"Note: {note}", file=sys.stderr)

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as out_f:
            if args.view == "summary":
                write_transaction_bucket_csv(transactions, out_f)
            else:
                write_transactions_csv(transactions, out_f)
    else:
        if args.view == "summary":
            write_transaction_bucket_csv(transactions, sys.stdout)
        else:
            write_transactions_csv(transactions, sys.stdout)


if __name__ == "__main__":
    main()
