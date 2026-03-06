#!/usr/bin/env python3
"""Replay a subset of PostgreSQL binary send functions against a printtup dump."""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from printtup_binary_dump import AttrMeta, DumpFile, FieldRecord, parse_dump


NUMERIC_SIGN_MASK = 0xC000
NUMERIC_POS = 0x0000
NUMERIC_NEG = 0x4000
NUMERIC_SHORT = 0x8000
NUMERIC_SPECIAL = 0xC000
NUMERIC_EXT_SIGN_MASK = 0xF000
NUMERIC_SHORT_SIGN_MASK = 0x2000
NUMERIC_SHORT_DSCALE_MASK = 0x1F80
NUMERIC_SHORT_DSCALE_SHIFT = 7
NUMERIC_SHORT_WEIGHT_SIGN_MASK = 0x0040
NUMERIC_SHORT_WEIGHT_MASK = 0x003F
NUMERIC_DSCALE_MASK = 0x3FFF


def pack_pg_int16(value: int) -> bytes:
    """Match pq_sendint16() by emitting the raw 16-bit network-order bits."""
    return struct.pack("!H", value & 0xFFFF)


def pack_pg_int32(value: int) -> bytes:
    """Match pq_sendint32() by emitting the raw 32-bit network-order bits."""
    return struct.pack("!I", value & 0xFFFFFFFF)


def decode_varlena(blob: bytes, dump: DumpFile) -> tuple[str, int, int, bytes]:
    """
    Decode a normalized varlena blob into header kind, total size, header size,
    and payload bytes. The dump stores PostgreSQL's native in-memory varlena
    layout, so we must interpret the flag bits the same way varatt.h does.
    """
    if dump.header is None:
        raise ValueError("dump header is missing")
    if not blob:
        raise ValueError("empty varlena blob")

    first = blob[0]
    byteorder = dump.header.dump_byteorder

    if dump.header.words_bigendian:
        is_4b = (first & 0x80) == 0x00
        is_4b_u = (first & 0xC0) == 0x00
        is_4b_c = (first & 0xC0) == 0x40
        is_1b = (first & 0x80) == 0x80
        is_1b_e = first == 0x80
    else:
        is_4b = (first & 0x01) == 0x00
        is_4b_u = (first & 0x03) == 0x00
        is_4b_c = (first & 0x03) == 0x02
        is_1b = (first & 0x01) == 0x01
        is_1b_e = first == 0x01

    if is_4b:
        if len(blob) < 4:
            raise ValueError("4-byte varlena blob is truncated")
        header = int.from_bytes(blob[:4], byteorder=byteorder, signed=False)
        total_size = (header & 0x3FFFFFFF) if dump.header.words_bigendian else ((header >> 2) & 0x3FFFFFFF)
        if total_size != len(blob):
            raise ValueError(f"4-byte varlena size mismatch: header={total_size} actual={len(blob)}")
        if is_4b_c:
            raise ValueError("compressed varlena blobs are not supported in the replay helper")
        if not is_4b_u:
            raise ValueError("unexpected non-plain 4-byte varlena header")
        return "4b", total_size, 4, blob[4:]

    if is_1b:
        if is_1b_e:
            raise ValueError("external TOAST pointers are not supported in the replay helper")
        total_size = (first & 0x7F) if dump.header.words_bigendian else ((first >> 1) & 0x7F)
        if total_size != len(blob):
            raise ValueError(f"1-byte varlena size mismatch: header={total_size} actual={len(blob)}")
        return "1b", total_size, 1, blob[1:]

    raise ValueError(f"unrecognized varlena header byte: 0x{first:02x}")


def reserialize_int4send(blob: bytes, dump: DumpFile) -> bytes:
    if dump.header is None:
        raise ValueError("dump header is missing")
    if len(blob) != 4:
        raise ValueError(f"int4 blob has unexpected length {len(blob)}")
    value = int.from_bytes(blob, byteorder=dump.header.dump_byteorder, signed=True)
    return pack_pg_int32(value)


def reserialize_date_send(blob: bytes, dump: DumpFile) -> bytes:
    if dump.header is None:
        raise ValueError("dump header is missing")
    if len(blob) != 4:
        raise ValueError(f"date blob has unexpected length {len(blob)}")
    value = int.from_bytes(blob, byteorder=dump.header.dump_byteorder, signed=True)
    return pack_pg_int32(value)


def reserialize_textsend(blob: bytes, dump: DumpFile) -> bytes:
    _, _, _, payload = decode_varlena(blob, dump)
    return payload


def reserialize_numeric_send(blob: bytes, dump: DumpFile) -> bytes:
    """
    Duplicate numeric_send() for packed finite/special Numeric datums by
    decoding the packed Numeric header exactly like init_var_from_num() does.
    """
    if dump.header is None:
        raise ValueError("dump header is missing")

    _, total_size, _, _ = decode_varlena(blob, dump)
    byteorder = dump.header.dump_byteorder

    if len(blob) < 6:
        raise ValueError("numeric blob is too short")

    header_word = int.from_bytes(blob[4:6], byteorder=byteorder, signed=False)
    flagbits = header_word & NUMERIC_SIGN_MASK
    if flagbits == NUMERIC_SHORT:
        sign = NUMERIC_NEG if (header_word & NUMERIC_SHORT_SIGN_MASK) else NUMERIC_POS
        dscale = (header_word & NUMERIC_SHORT_DSCALE_MASK) >> NUMERIC_SHORT_DSCALE_SHIFT
        short_weight = header_word & (NUMERIC_SHORT_WEIGHT_SIGN_MASK | NUMERIC_SHORT_WEIGHT_MASK)
        weight = short_weight - 0x80 if (short_weight & NUMERIC_SHORT_WEIGHT_SIGN_MASK) else short_weight
        numeric_header_size = 6
        digits_off = 6
    else:
        if len(blob) < 8:
            raise ValueError("long-format numeric blob is too short")
        sign = (header_word & NUMERIC_EXT_SIGN_MASK) if flagbits == NUMERIC_SPECIAL else flagbits
        dscale = header_word & NUMERIC_DSCALE_MASK
        weight = int.from_bytes(blob[6:8], byteorder=byteorder, signed=True)
        numeric_header_size = 8
        digits_off = 8

    ndigits = (total_size - numeric_header_size) // 2
    if total_size < numeric_header_size or (total_size - numeric_header_size) % 2 != 0:
        raise ValueError("numeric blob has an invalid packed size")
    if digits_off + ndigits * 2 != len(blob):
        raise ValueError("numeric digit array length does not match packed size")

    out = bytearray()
    out += pack_pg_int16(ndigits)
    out += pack_pg_int16(weight)
    out += pack_pg_int16(sign)
    out += pack_pg_int16(dscale)
    for idx in range(ndigits):
        digit_off = digits_off + idx * 2
        digit = int.from_bytes(blob[digit_off:digit_off + 2], byteorder=byteorder, signed=False)
        out += pack_pg_int16(digit)
    return bytes(out)


def reserialize_field(attr: AttrMeta, field: FieldRecord, dump: DumpFile) -> bytes | None:
    """Dispatch to the matching replay routine using the recorded send function."""
    if field.normalized is None:
        return None
    if attr.sendname is None:
        raise ValueError(f"attribute {attr.attname} has no send function metadata")

    sendname = attr.sendname
    if sendname == "pg_catalog.int4send(integer)":
        return reserialize_int4send(field.normalized, dump)
    if sendname == "pg_catalog.date_send(pg_catalog.date)":
        return reserialize_date_send(field.normalized, dump)
    if sendname in (
        "pg_catalog.bpcharsend(character)",
        "pg_catalog.varcharsend(character varying)",
        "pg_catalog.textsend(text)",
    ):
        return reserialize_textsend(field.normalized, dump)
    if sendname == "pg_catalog.numeric_send(numeric)":
        return reserialize_numeric_send(field.normalized, dump)

    raise ValueError(
        f"attribute {attr.attname} uses unsupported send function {sendname}"
    )


def build_row_payload(fields: list[bytes | None]) -> bytes:
    """Rebuild the DataRow payload emitted by printtup() for one row."""
    out = bytearray()
    out += struct.pack("!H", len(fields))
    for field in fields:
        if field is None:
            out += struct.pack("!i", -1)
            continue
        out += struct.pack("!i", len(field))
        out += field
    return bytes(out)


def verify_dump(dump: DumpFile, schema_id: int, max_mismatches: int) -> int:
    if dump.header is None:
        raise ValueError("dump is missing the HEAD record")
    if schema_id not in dump.schemas:
        raise ValueError(f"schema id {schema_id} is not present in the dump")

    schema = dump.schemas[schema_id]
    total_rows = 0
    total_fields = 0
    mismatches = 0

    for row in dump.rows:
        if row.schema_id != schema_id:
            continue
        if len(schema.attrs) != row.natts or len(row.fields) != row.natts:
            raise ValueError(
                f"row {row.row_id} natts mismatch: schema={len(schema.attrs)} row={row.natts}"
            )

        total_rows += 1
        replay_fields: list[bytes | None] = []
        for attr, field in zip(schema.attrs, row.fields):
            total_fields += 1
            replay = reserialize_field(attr, field, dump)
            replay_fields.append(replay)
            if replay != field.serialized:
                mismatches += 1
                print(
                    f"field mismatch row_id={row.row_id} attnum={attr.attnum} "
                    f"name={attr.attname} send={attr.sendname}",
                    file=sys.stderr,
                )
                print(
                    f"  normalized={None if field.normalized is None else field.normalized.hex()}",
                    file=sys.stderr,
                )
                print(
                    f"  expected={None if field.serialized is None else field.serialized.hex()}",
                    file=sys.stderr,
                )
                print(
                    f"  replayed={None if replay is None else replay.hex()}",
                    file=sys.stderr,
                )
                if mismatches >= max_mismatches:
                    return report_summary(total_rows, total_fields, mismatches, schema_id)

        replay_row = build_row_payload(replay_fields)
        if replay_row != row.row_payload:
            mismatches += 1
            print(
                f"row payload mismatch row_id={row.row_id} schema_id={row.schema_id}",
                file=sys.stderr,
            )
            print(f"  expected_row={row.row_payload.hex()}", file=sys.stderr)
            print(f"  replayed_row={replay_row.hex()}", file=sys.stderr)
            if mismatches >= max_mismatches:
                return report_summary(total_rows, total_fields, mismatches, schema_id)

    return report_summary(total_rows, total_fields, mismatches, schema_id)


def report_summary(total_rows: int, total_fields: int, mismatches: int, schema_id: int) -> int:
    print(
        f"schema_id={schema_id} rows_checked={total_rows} "
        f"fields_checked={total_fields} mismatches={mismatches}"
    )
    return 0 if mismatches == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Path to the printtup binary dump")
    parser.add_argument(
        "--schema-id",
        type=int,
        default=None,
        help="Schema id to verify (default: the only schema in the dump)",
    )
    parser.add_argument(
        "--max-mismatches",
        type=int,
        default=5,
        help="Stop after reporting this many mismatches (default: 5)",
    )
    args = parser.parse_args()

    dump = parse_dump(args.path)

    if args.schema_id is None:
        if len(dump.schemas) != 1:
            raise ValueError(
                f"dump contains {len(dump.schemas)} schemas; pass --schema-id explicitly"
            )
        schema_id = next(iter(dump.schemas))
    else:
        schema_id = args.schema_id

    return verify_dump(dump, schema_id, args.max_mismatches)


if __name__ == "__main__":
    sys.exit(main())
