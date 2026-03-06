#!/usr/bin/env python3
"""Inspect binary printtup dump files produced by printtup.c instrumentation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from printtup_binary_dump import parse_dump


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to the printtup binary dump")
    parser.add_argument("--show-schema", action="store_true",
                        help="Print the schema metadata and serializer names")
    parser.add_argument("--show-rows", type=int, default=3,
                        help="Print the first N row-length summaries (default: 3)")
    args = parser.parse_args()

    dump = parse_dump(args.path)

    print(f"path={args.path}")
    print(f"header={(dump.header.magic, dump.header.version) if dump.header else None}")
    print(f"record_counts={dump.counts}")
    print(f"schema_ids={sorted(dump.schemas)}")
    print(f"row_count={len(dump.rows)}")

    if args.show_schema:
        for schema_id in sorted(dump.schemas):
            schema = dump.schemas[schema_id]
            print(f"schema {schema_id} attrs={len(schema.attrs)}")
            for attr in schema.attrs:
                print(
                    f"  attnum={attr.attnum} name={attr.attname} type={attr.typename} "
                    f"format={attr.fmt} send={attr.sendname} recv={attr.recvname} "
                    f"attlen={attr.attlen} byval={attr.attbyval} typioparam={attr.typioparam}"
                )

    for row in dump.rows[: max(args.show_rows, 0)]:
        print(
            f"row schema={row.schema_id} row_id={row.row_id} natts={row.natts} "
            f"row_payload_len={row.row_payload_len} "
            f"norm_first5={[field.normalized_len for field in row.fields[:5]]} "
            f"ser_first5={[field.serialized_len for field in row.fields[:5]]}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
