#!/usr/bin/env python3
import argparse
import csv
import re
import sys
from typing import Optional, TextIO


# kernel-network regex (TCP/IP + skb + NAPI + qdisc + mlx5 +
# netfilter/conntrack + neighbor/ARP + backlog + loopback).
NET = re.compile(
    r"(net_rx_action|net_tx_action|__napi_poll|napi_|gro_|gso_|"
    r"tcp_|udp_|ip_|ipv4_|ipv6_|inet_|inet6_|sock_|skb_|"
    r"\bsk_|"                              # word-boundary avoids task_, mask_, etc.
    r"__dev_queue_xmit|dev_queue_xmit|dev_hard_start_xmit|"
    r"netif_|neigh_|"                      # RX dispatch + ARP/neighbor layer
    r"nf_|resolve_normal_ct|"              # netfilter + conntrack lookup
    r"qdisc_|sch_|"
    r"_backlog|kfree_skb|consume_skb|"     # backlog RX path + skb lifecycle
    r"cubictcp_|bbr_|westwood_|"           # TCP congestion-control algorithms
    r"loopback_xmit|"                      # loopback device transmit
    r"mlx5)"
)


def parse_perf_script(input_stream: TextIO) -> tuple[int, int]:
    """Accumulate sampled kernel total/net periods from perf script output."""

    total_period = 0
    net_period = 0

    cur_period: Optional[int] = None
    cur_is_net = False
    in_sample = False

    def flush_current_sample() -> tuple[int, int]:
        if not in_sample or cur_period is None:
            return 0, 0
        return cur_period, cur_period if cur_is_net else 0

    for line in input_stream:
        if line.startswith("\t"):  # callchain frame line
            if NET.search(line):
                cur_is_net = True
            continue

        line = line.strip()
        if not line:
            continue

        sample_total, sample_net = flush_current_sample()
        total_period += sample_total
        net_period += sample_net

        in_sample = True
        cur_is_net = False
        cur_period = None

        # With -F comm,period,event,ip,sym,dso, the 2nd token is period.
        parts = line.split()
        if len(parts) >= 2:
            try:
                cur_period = int(parts[1])
            except ValueError:
                cur_period = None

        # Also allow header-line match (shouldn't be the case, but harmless).
        if NET.search(line):
            cur_is_net = True

    sample_total, sample_net = flush_current_sample()
    total_period += sample_total
    net_period += sample_net

    return total_period, net_period


def load_query_active_wall_ns(csv_path: str) -> int:
    """Load the active-wall denominator from an aggregate CSV.

    Supports:
    - aggregate_timing.py summary CSV:
      bucket_name,classification,total_ns,...
    - aggregate_transaction_timing.py summary CSV:
      transaction_index,transaction_key,query_active_wall_ns,...
    """

    with open(csv_path, newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path} has no CSV header")

        fieldnames = set(reader.fieldnames)

        if {"bucket_name", "total_ns"}.issubset(fieldnames):
            total_ns = 0
            found = False
            for row in reader:
                if row.get("bucket_name") != "query_active_wall_ns":
                    continue
                total_ns += int(row["total_ns"])
                found = True
            if not found:
                raise ValueError(
                    f"{csv_path} does not contain a query_active_wall_ns bucket row"
                )
            return total_ns

        if "query_active_wall_ns" in fieldnames:
            total_ns = 0
            found = False
            for row in reader:
                value = row.get("query_active_wall_ns")
                if value is None or value == "":
                    continue
                total_ns += int(value)
                found = True
            if not found:
                raise ValueError(
                    f"{csv_path} does not contain any query_active_wall_ns values"
                )
            return total_ns

    raise ValueError(
        f"{csv_path} is not a supported aggregate CSV; expected "
        "aggregate_timing.py summary output or aggregate_transaction_timing.py output"
    )


def safe_ratio(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Summarize sampled kernel-network periods from perf script output. "
            "Optionally combine them with query_active_wall_ns from the timing "
            "aggregation scripts to compute the node-level kernel-network "
            "average-core ratio for the same perf-enabled run on the same node."
        )
    )
    parser.add_argument(
        "perf_script",
        nargs="?",
        default="-",
        help="Path to perf script text, or '-' to read from stdin",
    )
    parser.add_argument(
        "--query-active-wall-ns",
        type=int,
        default=None,
        help=(
            "Explicit query_active_wall_ns denominator from the same perf-enabled "
            "run on the same node. Use the sum across all measured "
            "queries/transactions for that run."
        ),
    )
    parser.add_argument(
        "--aggregate-csv",
        default=None,
        help=(
            "CSV output from aggregate_timing.py --view summary or "
            "aggregate_transaction_timing.py for the same perf-enabled run on "
            "the same node. query_active_wall_ns will be loaded and, for "
            "transaction CSVs, summed across rows."
        ),
    )
    parser.add_argument(
        "--show-sampled-kernel-fraction",
        action="store_true",
        help=(
            "Also emit the old sampled-kernel fraction "
            "(net_period / total_period) as a diagnostic."
        ),
    )

    args = parser.parse_args()

    if args.query_active_wall_ns is not None and args.aggregate_csv is not None:
        parser.error("use only one of --query-active-wall-ns or --aggregate-csv")

    input_stream: TextIO
    if args.perf_script == "-":
        input_stream = sys.stdin
    else:
        input_stream = open(args.perf_script, "r")

    try:
        total_period, net_period = parse_perf_script(input_stream)
    finally:
        if input_stream is not sys.stdin:
            input_stream.close()

    query_active_wall_ns: Optional[int] = args.query_active_wall_ns
    if args.aggregate_csv is not None:
        query_active_wall_ns = load_query_active_wall_ns(args.aggregate_csv)

    print(f"sampled_kernel_total_ns={total_period}")
    print(f"sampled_kernel_net_ns={net_period}")

    if args.show_sampled_kernel_fraction:
        print(
            "kernel_net_fraction_of_sampled_kernel_cpu="
            f"{safe_ratio(net_period, total_period):.7f}"
        )

    if query_active_wall_ns is None:
        return

    kernel_net_avg_cores = safe_ratio(net_period, query_active_wall_ns)

    print(f"query_active_wall_ns={query_active_wall_ns}")
    print(f"kernel_net_avg_cores={kernel_net_avg_cores:.7f}")
    print(
        "kernel_net_pct_of_query_active_wall="
        f"{(kernel_net_avg_cores * 100.0):.4f}"
    )


if __name__ == "__main__":
    main()
