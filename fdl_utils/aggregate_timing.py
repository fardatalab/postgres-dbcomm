#!/usr/bin/env python3
"""
Aggregate timing statistics from log files.

Parses timing report blocks from log files and aggregates statistics
for each unique timer name, including counts, total time, and custom stats.
"""

import argparse
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List

from timing_bucket_utils import build_bucket_rows, summary_notes


def parse_custom_stats(stats_str: str) -> Dict[str, float]:
    """
    Parse comma-separated key=value pairs from custom stats string.
    
    Only accepts pairs in the form 'key=value' where value is numeric.
    Discards any malformed data.
    """
    stats = {}
    if not stats_str or stats_str.strip() == '':
        return stats
    
    # Split by comma and parse key=value pairs
    pairs = stats_str.split(',')
    for pair in pairs:
        pair = pair.strip()
        # Only process if it contains exactly one '=' sign
        if '=' in pair and pair.count('=') == 1:
            key, value = pair.split('=', 1)
            key = key.strip()
            value = value.strip()
            
            # Validate key: should be alphanumeric with underscores
            if not key or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
                continue
            
            # Validate value: must be a number
            try:
                stats[key] = float(value)
            except ValueError:
                # If it's not a number, skip it and print a warning
                print(f"Warning: Invalid custom stat '{pair}', skipping")
                pass
    return stats


def aggregate_custom_stats(all_stats: List[Dict[str, float]]) -> str:
    """Aggregate custom stats from multiple entries."""
    if not all_stats:
        return ''
    
    # Sum all numeric values for each key
    aggregated = defaultdict(float)
    for stats_dict in all_stats:
        for key, value in stats_dict.items():
            aggregated[key] += value
    
    # Format as comma-separated key=value pairs
    if not aggregated:
        return ''
    
    return ', '.join(f'{key}={int(value)}' for key, value in sorted(aggregated.items()))


def parse_log_file(filepath: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse log file and extract timing statistics.
    
    Returns a dictionary mapping timer names to aggregated statistics.
    Validates numeric columns and warns about malformed data.
    """
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        'count': 0,
        'total_time': 0,
        'custom_stats': []
    })
    
    # Regex pattern to match timing lines
    # Format: [optional whitespace/tab] Timer Name | Count | Total Time | Average Time | Custom Stats
    # We need to be flexible about what comes after the timer name since there may be misprints
    # The new format has a leading tab before each timer line
    pattern = re.compile(
        r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|\s*(.*)$'
    )
    
    line_number = 0
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line_number += 1
            line = line.rstrip()
            match = pattern.match(line)
            if match:
                timer_name = match.group(1)
                rest = match.group(2)
                
                # Split the rest by pipe character
                parts = [p.strip() for p in rest.split('|')]
                
                # We expect at least 4 parts: Count, Total Time, Average Time, Custom Stats
                if len(parts) < 4:
                    continue
                
                # Parse count (1st column after timer name)
                try:
                    count = int(parts[0])
                except ValueError:
                    print(f"Warning (line {line_number}): Invalid count '{parts[0]}' for timer '{timer_name}', using 0", file=sys.stderr)
                    count = 0
                
                # Parse total time (2nd column after timer name)
                try:
                    total_time = int(parts[1])
                except ValueError:
                    print(f"Warning (line {line_number}): Invalid total time '{parts[1]}' for timer '{timer_name}', using 0", file=sys.stderr)
                    total_time = 0
                
                # Parse average time (3rd column after timer name) - we don't use it but validate it
                try:
                    int(parts[2])
                except ValueError:
                    print(f"Warning (line {line_number}): Invalid average time '{parts[2]}' for timer '{timer_name}'", file=sys.stderr)
                
                # The custom stats is everything from the 4th part onwards, joined by '|'
                # (in case there are extra pipes in malformed lines)
                custom_stats_str = '|'.join(parts[3:]).strip()
                
                # Only aggregate if we have valid count (total_time can be 0)
                if count > 0 or total_time > 0:
                    # Aggregate the statistics
                    stats[timer_name]['count'] += count
                    stats[timer_name]['total_time'] += total_time
                    
                    # Parse and store custom stats (only valid key=value pairs will be kept)
                    if custom_stats_str and custom_stats_str != '':
                        custom_stats = parse_custom_stats(custom_stats_str)
                        if custom_stats:
                            stats[timer_name]['custom_stats'].append(custom_stats)
    
    return stats


def format_output_csv(stats: Dict[str, Dict[str, Any]]) -> str:
    """Format aggregated statistics as CSV."""
    lines = []
    
    # Header
    lines.append('Timer Name,Count,Total Time (ns),Average Time (ns),Custom Stats')
    
    # Sort timer names for consistent output
    for timer_name in sorted(stats.keys()):
        data = stats[timer_name]
        count = data['count']
        total_time = data['total_time']
        avg_time = total_time // count if count > 0 else 0
        custom_stats_str = aggregate_custom_stats(data['custom_stats'])
        
        lines.append(f'{timer_name},{count},{total_time},{avg_time},"{custom_stats_str}"')
    
    return '\n'.join(lines)


def format_summary_csv(stats: Dict[str, Dict[str, Any]]) -> str:
    """Format derived bucket totals as CSV.

    The emitted summary includes the primary active-execution denominator plus
    additive communication buckets and clearly-marked optional/debug buckets.
    """

    timer_totals = {
        timer_name: int(timer_data["total_time"])
        for timer_name, timer_data in stats.items()
    }
    rows = build_bucket_rows(timer_totals)

    header = (
        "bucket_name,classification,total_ns,pct_of_query_active_wall,timers,description,note"
    )
    lines = [header]
    for row in rows:
        timers = str(row["timers"]).replace('"', '""')
        description = str(row["description"]).replace('"', '""')
        note = str(row["note"]).replace('"', '""')
        lines.append(
            f'{row["bucket_name"]},{row["classification"]},{row["total_ns"]},'
            f'{row["pct_of_query_active_wall"]},'
            f'"{timers}","{description}","{note}"'
        )

    return '\n'.join(lines)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate timing report blocks from a log file. Summary view emits "
            "derived communication-stack buckets directly, plus the active-"
            "execution denominator and legacy/debug context timers."
        )
    )
    parser.add_argument("log_file", help="Path to the log file to parse")
    parser.add_argument(
        "--view",
        choices=("summary", "raw", "both"),
        default="summary",
        help=(
            "summary: derived communication buckets; raw: original per-timer CSV; "
            "both: print summary then raw."
        ),
    )

    args = parser.parse_args()
    log_file = args.log_file
    
    try:
        # Parse the log file
        stats = parse_log_file(log_file)
        
        if not stats:
            print("No timing statistics found in log file.", file=sys.stderr)
            sys.exit(1)

        if args.view in ("summary", "both"):
            for note in summary_notes():
                print(f"Note: {note}", file=sys.stderr)

        if args.view in ("summary", "both"):
            print(format_summary_csv(stats))
        if args.view == "both":
            print()
        if args.view in ("raw", "both"):
            print(format_output_csv(stats))
        
    except FileNotFoundError:
        print(f"Error: File '{log_file}' not found.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error processing log file: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
