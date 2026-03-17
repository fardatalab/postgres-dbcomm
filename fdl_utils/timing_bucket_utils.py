#!/usr/bin/env python3
"""Shared timing bucket definitions for communication-stack summaries.

The communication-stack buckets below intentionally distinguish between:

- additive leaf buckets that can be summed directly
- optional additive buckets that should stay separate unless explicitly wanted
- non-additive coarse/debug buckets that overlap lower leaves and must not be
  summed into the additive totals
- the primary active-execution denominator
- legacy context timers that are still useful for debugging but are no longer
  the recommended denominator
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class BucketDefinition:
    """Describe one derived timing bucket."""

    name: str
    classification: str
    timers: Tuple[str, ...]
    description: str
    note: str = ""


QUERY_ACTIVE_WALL_QUALITY = "query_cycle_active_wall_wait_excluded"
QUERY_ACTIVE_WALL_NOTE = (
    "QUERY_ACTIVE_WALL now spans the logical query cycle on this backend and is "
    "paused around PostgreSQL wait events plus FE-libpq socket waits. It "
    "includes the query-cycle tail up to the ReadyForQuery boundary, so this is "
    "the primary communication-stack denominator, not just executor-core time."
)

QUERY_WAIT_WALL_NOTE = (
    "QUERY_WAIT_WALL accumulates the waits excluded from QUERY_ACTIVE_WALL. "
    "It is an optional/debug bucket, not part of the communication-stack sum."
)

QUERY_EXEC_CORE_QUALITY = "legacy_statement_exec_core_only"
QUERY_EXEC_CORE_NOTE = (
    "ExecSimpleQuery only covers the backend command-execution core. Keep this "
    "as legacy context when comparing against older runs, but do not use it as "
    "the primary communication percentage denominator anymore."
)

XACT_LIFECYCLE_WALL_QUALITY = (
    "lifecycle_wall_time_may_include_idle_and_excludes_final_ready"
)
XACT_LIFECYCLE_WALL_NOTE = (
    "XACT_PROCESSING is a coordinated-transaction lifecycle wall timer. It is "
    "preserved across logger_reset while a distributed transaction is active, "
    "so it can include inter-statement gaps. Sum QUERY_ACTIVE_WALL across the "
    "transaction for an active-execution denominator instead."
)


BUCKET_DEFINITIONS: Tuple[BucketDefinition, ...] = (
    BucketDefinition(
        name="transport_protocol_cpu_ns",
        classification="additive_core",
        timers=(
            "PG_FE_SOCK_READ",
            "PG_FE_SOCK_WRITE",
            "PG_BE_SOCK_READ",
            "PG_BE_SOCK_WRITE",
            "PG_parseInput",
            "PQ_getbyte",
            "PQ_getmessage",
            "PQ_getbytes",
            "PQ_putmessage",
            "PQ_sendCommand",
            "PQ_putCopyData",
            "PQ_putCopyEnd",
            "PQ_getResult",
            "PQ_getCopyData",
        ),
        description=(
            "Strict additive transport/protocol execution leaves: socket IO, "
            "protocol framing, parse, and protocol message staging."
        ),
    ),
    BucketDefinition(
        name="row_serde_cpu_ns",
        classification="additive_core",
        timers=(
            "Printtup_Ser",
            "RemoteFileDestReceiver_Ser",
            "ReceiveResults_Deserialize",
        ),
        description=(
            "Additive row serialization/deserialization leaves used by result "
            "send, intermediate-result push, and worker-result receive."
        ),
    ),
    BucketDefinition(
        name="result_materialization_cpu_ns",
        classification="additive_separate",
        timers=("ReceiveResults_BuildTuples",),
        description=(
            "Separate result-materialization bucket for turning received rows "
            "into local heap tuples."
        ),
        note=(
            "Keep this separate from the strict communication core. "
            "ReceiveResults_HeapFormTuple is a nested debug child and is not "
            "added here."
        ),
    ),
    BucketDefinition(
        name="file_comm_io_ns",
        classification="additive_separate",
        timers=(
            "SendViaCopy_FileRead",
            "ReceiveAndWriteCopyData_WriteFile",
            "FetchIntermediate_FileWrite",
            "RemoteFileDestReceiver_SerAndSend_WriteLocal",
        ),
        description=(
            "Separate file-backed communication bucket for intermediate-result "
            "file reads/writes and optional local persistence."
        ),
    ),
    BucketDefinition(
        name="optional_connection_setup_cpu_ns",
        classification="optional_additive",
        timers=("PQ_connectStart",),
        description=(
            "Optional additive setup bucket for the synchronous front half of "
            "connection establishment."
        ),
        note="Keep separate unless connection setup should count toward the total.",
    ),
    BucketDefinition(
        name="query_active_wall_ns",
        classification="denominator_primary",
        timers=("QUERY_ACTIVE_WALL",),
        description="Primary active-execution wall denominator for the logical query cycle.",
        note=QUERY_ACTIVE_WALL_NOTE,
    ),
    BucketDefinition(
        name="query_wait_wall_ns",
        classification="optional_wait_debug",
        timers=("QUERY_WAIT_WALL",),
        description="Optional/debug wait total excluded from QUERY_ACTIVE_WALL.",
        note=QUERY_WAIT_WALL_NOTE,
    ),
    BucketDefinition(
        name="coarse_ready_processing_debug_ns",
        classification="nonadditive_debug",
        timers=("CheckConnectionReady_",),
        description="Coarse post-wakeup libpq processing region in adaptive executor.",
        note="Overlaps PG_FE_* and PG_parseInput leaves.",
    ),
    BucketDefinition(
        name="coarse_buffered_result_debug_ns",
        classification="nonadditive_debug",
        timers=("ReceiveResults_Net",),
        description="Coarse buffered PQgetResult region in ReceiveResults().",
        note="Overlaps the lower PQ_getResult leaf.",
    ),
    BucketDefinition(
        name="coarse_worker_copy_receive_debug_ns",
        classification="nonadditive_debug",
        timers=("ReceiveAndWriteCopyData_Deser",),
        description="Coarse worker COPY receive region while writing pushed results.",
        note="Overlaps PQ_getbyte, PQ_getmessage, PQ_getbytes, and PG_BE_SOCK_READ.",
    ),
    BucketDefinition(
        name="coarse_pull_copy_and_write_debug_ns",
        classification="nonadditive_debug",
        timers=("FetchIntermediate_CopyAndWrite",),
        description="Coarse pull-side COPY receive/write region in fetch_intermediate_results().",
        note="Overlaps PQ_getCopyData and FetchIntermediate_FileWrite.",
    ),
    BucketDefinition(
        name="coarse_push_send_debug_ns",
        classification="nonadditive_debug",
        timers=("RemoteFileDestReceiver_Send",),
        description="Coarse push-side send region for serialized intermediate-result rows.",
        note="Overlaps PQ_putCopyData/PQ_putCopyEnd and lower PG_FE_* leaves.",
    ),
    BucketDefinition(
        name="coarse_backend_row_send_debug_ns",
        classification="nonadditive_debug",
        timers=("Printtup", "Printtup_Net"),
        description="Coarse backend row-send parents for regular query results.",
        note="Overlaps Printtup_Ser, PQ_putmessage, and PG_BE_SOCK_WRITE.",
    ),
    BucketDefinition(
        name="coarse_connection_setup_debug_ns",
        classification="nonadditive_debug",
        timers=("PQ_connectPoll",),
        description="Coarse connection-setup poll-loop region.",
        note="Overlaps lower PG_FE_SOCK_READ/WRITE during connection establishment.",
    ),
    BucketDefinition(
        name="query_exec_core_ns",
        classification="legacy_context",
        timers=("ExecSimpleQuery",),
        description="Legacy coarse backend command-execution core timer.",
        note=QUERY_EXEC_CORE_NOTE,
    ),
    BucketDefinition(
        name="distributed_tx_lifecycle_wall_ns",
        classification="legacy_context",
        timers=("XACT_PROCESSING",),
        description="Legacy distributed-transaction lifecycle wall timer.",
        note=XACT_LIFECYCLE_WALL_NOTE,
    ),
)


def sum_timers(timer_totals: Mapping[str, int], timer_names: Sequence[str]) -> int:
    """Sum timer totals defensively."""

    return sum(int(timer_totals.get(timer_name, 0)) for timer_name in timer_names)


def build_bucket_values(timer_totals: Mapping[str, int]) -> Dict[str, int]:
    """Compute bucket totals plus derived communication totals."""

    values = {
        bucket.name: sum_timers(timer_totals, bucket.timers)
        for bucket in BUCKET_DEFINITIONS
    }

    values["communication_stack_core_total_ns"] = (
        values["transport_protocol_cpu_ns"] +
        values["row_serde_cpu_ns"]
    )
    values["communication_stack_extended_total_ns"] = (
        values["communication_stack_core_total_ns"] +
        values["result_materialization_cpu_ns"] +
        values["file_comm_io_ns"]
    )
    values["communication_stack_extended_with_optional_setup_total_ns"] = (
        values["communication_stack_extended_total_ns"] +
        values["optional_connection_setup_cpu_ns"]
    )
    values["nonadditive_debug_total_ns"] = sum(
        values[bucket.name]
        for bucket in BUCKET_DEFINITIONS
        if bucket.classification == "nonadditive_debug"
    )

    return values


def build_bucket_rows(timer_totals: Mapping[str, int]) -> List[Dict[str, object]]:
    """Build row dictionaries for summary CSV output."""

    values = build_bucket_values(timer_totals)
    denominator_ns = values["query_active_wall_ns"]
    rows: List[Dict[str, object]] = []

    for bucket in BUCKET_DEFINITIONS:
        rows.append(
            {
                "bucket_name": bucket.name,
                "classification": bucket.classification,
                "total_ns": values[bucket.name],
                "pct_of_query_active_wall": safe_pct(values[bucket.name], denominator_ns),
                "timers": ", ".join(bucket.timers),
                "description": bucket.description,
                "note": bucket.note,
            }
        )

    rows.extend(
        (
            {
                "bucket_name": "communication_stack_core_total_ns",
                "classification": "derived_total",
                "total_ns": values["communication_stack_core_total_ns"],
                "pct_of_query_active_wall": safe_pct(
                    values["communication_stack_core_total_ns"], denominator_ns
                ),
                "timers": "transport_protocol_cpu_ns + row_serde_cpu_ns",
                "description": (
                    "Strict additive communication-stack total: transport/protocol "
                    "plus row serialization/deserialization."
                ),
                "note": "Recommended primary additive communication-stack total.",
            },
            {
                "bucket_name": "communication_stack_extended_total_ns",
                "classification": "derived_total",
                "total_ns": values["communication_stack_extended_total_ns"],
                "pct_of_query_active_wall": safe_pct(
                    values["communication_stack_extended_total_ns"], denominator_ns
                ),
                "timers": (
                    "communication_stack_core_total_ns + "
                    "result_materialization_cpu_ns + file_comm_io_ns"
                ),
                "description": (
                    "Extended additive total that also includes the separate result "
                    "materialization and file-backed communication buckets."
                ),
                "note": (
                    "Useful when those loosely communication-related buckets should "
                    "count toward the total."
                ),
            },
            {
                "bucket_name": "communication_stack_extended_with_optional_setup_total_ns",
                "classification": "derived_total",
                "total_ns": values["communication_stack_extended_with_optional_setup_total_ns"],
                "pct_of_query_active_wall": safe_pct(
                    values["communication_stack_extended_with_optional_setup_total_ns"],
                    denominator_ns,
                ),
                "timers": (
                    "communication_stack_extended_total_ns + "
                    "optional_connection_setup_cpu_ns"
                ),
                "description": (
                    "Extended additive total with the optional connection-setup bucket."
                ),
                "note": "Only use when connection setup should count toward the total.",
            },
            {
                "bucket_name": "nonadditive_debug_total_ns",
                "classification": "derived_total",
                "total_ns": values["nonadditive_debug_total_ns"],
                "pct_of_query_active_wall": safe_pct(
                    values["nonadditive_debug_total_ns"], denominator_ns
                ),
                "timers": (
                    "coarse_ready_processing_debug_ns + "
                    "coarse_buffered_result_debug_ns + "
                    "coarse_worker_copy_receive_debug_ns + "
                    "coarse_pull_copy_and_write_debug_ns + "
                    "coarse_push_send_debug_ns + "
                    "coarse_backend_row_send_debug_ns + "
                    "coarse_connection_setup_debug_ns"
                ),
                "description": (
                    "Sum of coarse/debug parent regions that overlap lower leaves."
                ),
                "note": "Do not add this into any additive communication-stack total.",
            },
        )
    )

    return rows


def safe_pct(numerator_ns: int, denominator_ns: int) -> str:
    """Return a stable percentage string or empty string when undefined."""

    if denominator_ns <= 0:
        return ""
    return f"{(100.0 * numerator_ns) / denominator_ns:.6f}"


def summary_notes() -> List[str]:
    """Human-readable notes for callers to print once."""

    return [
        "Additive totals: communication_stack_core_total_ns is the recommended "
        "strict communication-stack total.",
        "Extended totals: communication_stack_extended_total_ns also includes the "
        "separate result-materialization and file-backed communication buckets.",
        "Optional additive bucket: optional_connection_setup_cpu_ns is not "
        "included unless communication_stack_extended_with_optional_setup_total_ns "
        "is used.",
        "Primary denominator: query_active_wall_ns is the recommended active-"
        "execution wall-clock denominator for communication percentages.",
        "Optional/debug wait bucket: query_wait_wall_ns is the excluded wait "
        "time paired with query_active_wall_ns.",
        "Non-additive buckets: any bucket with classification=nonadditive_debug "
        "overlaps lower leaves and must not be summed into additive totals.",
        f"query_active_wall_ns quality: {QUERY_ACTIVE_WALL_QUALITY} "
        f"({QUERY_ACTIVE_WALL_NOTE})",
        f"query_exec_core_ns quality: {QUERY_EXEC_CORE_QUALITY} ({QUERY_EXEC_CORE_NOTE})",
        "This remains a legacy statement-core context timer only.",
        f"distributed_tx_lifecycle_wall_ns quality: {XACT_LIFECYCLE_WALL_QUALITY} "
        f"({XACT_LIFECYCLE_WALL_NOTE})",
        "This remains a lifecycle context timer, not the active-execution "
        "denominator.",
    ]
