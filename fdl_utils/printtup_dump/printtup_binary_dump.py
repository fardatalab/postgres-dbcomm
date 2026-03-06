#!/usr/bin/env python3
"""Shared parser and low-level helpers for printtup binary dump files."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HeaderRecord:
    magic: str
    version: int
    words_bigendian: bool
    datum_size: int
    pointer_size: int
    pg_version_num: int

    @property
    def dump_byteorder(self) -> str:
        return "big" if self.words_bigendian else "little"


@dataclass
class AttrMeta:
    attnum: int
    atttypid: int
    atttypmod: int
    attlen: int
    attbyval: bool
    attisdropped: bool
    attalign: str
    attstorage: str
    fmt: int
    typsend: int
    typreceive: int
    typioparam: int
    attname: str | None
    typename: str | None
    sendname: str | None
    recvname: str | None


@dataclass
class SchemaRecord:
    schema_id: int
    attrs: list[AttrMeta]


@dataclass
class FieldRecord:
    normalized: bytes | None
    serialized: bytes | None

    @property
    def normalized_len(self) -> int:
        return -1 if self.normalized is None else len(self.normalized)

    @property
    def serialized_len(self) -> int:
        return -1 if self.serialized is None else len(self.serialized)


@dataclass
class RowRecord:
    schema_id: int
    row_id: int
    natts: int
    row_payload: bytes
    fields: list[FieldRecord]

    @property
    def row_payload_len(self) -> int:
        return len(self.row_payload)


@dataclass
class DumpFile:
    counts: dict[str, int]
    schemas: dict[int, SchemaRecord]
    rows: list[RowRecord]
    header: HeaderRecord | None


def read_u32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("!I", buf, off)[0], off + 4


def read_i32(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("!i", buf, off)[0], off + 4


def read_u64(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("!Q", buf, off)[0], off + 8


def read_u16(buf: bytes, off: int) -> tuple[int, int]:
    return struct.unpack_from("!H", buf, off)[0], off + 2


def read_len_string(buf: bytes, off: int) -> tuple[str | None, int]:
    length, off = read_i32(buf, off)
    if length < 0:
        return None, off
    value = buf[off:off + length].decode("utf-8", "replace")
    return value, off + length


def parse_dump(path: Path) -> DumpFile:
    """Parse one printtup dump file into schema and row records."""
    data = path.read_bytes()
    counts: dict[str, int] = {}
    schemas: dict[int, SchemaRecord] = {}
    rows: list[RowRecord] = []
    header: HeaderRecord | None = None

    off = 0
    while off + 8 <= len(data):
        tag = data[off:off + 4].decode("ascii")
        length = struct.unpack_from("!I", data, off + 4)[0]
        payload = data[off + 8:off + 8 + length]
        counts[tag] = counts.get(tag, 0) + 1

        if tag == "HEAD":
            pos = 0
            magic, pos = read_len_string(payload, pos)
            version, pos = read_u32(payload, pos)
            words_bigendian = payload[pos] == 1
            pos += 1
            datum_size = payload[pos]
            pos += 1
            pointer_size = payload[pos]
            pos += 1
            pg_version_num, pos = read_u32(payload, pos)
            header = HeaderRecord(
                magic=magic or "",
                version=version,
                words_bigendian=words_bigendian,
                datum_size=datum_size,
                pointer_size=pointer_size,
                pg_version_num=pg_version_num,
            )
        elif tag == "SCHM":
            pos = 0
            schema_id, pos = read_u64(payload, pos)
            natts, pos = read_u32(payload, pos)
            attrs: list[AttrMeta] = []
            for _ in range(natts):
                attnum, pos = read_u32(payload, pos)
                atttypid, pos = read_u32(payload, pos)
                atttypmod, pos = read_i32(payload, pos)
                attlen_raw, pos = read_u16(payload, pos)
                attlen = attlen_raw if attlen_raw < 0x8000 else attlen_raw - 0x10000
                attbyval = payload[pos] == 1
                pos += 1
                attisdropped = payload[pos] == 1
                pos += 1
                attalign = chr(payload[pos])
                pos += 1
                attstorage = chr(payload[pos])
                pos += 1
                fmt, pos = read_u16(payload, pos)
                typsend, pos = read_u32(payload, pos)
                typreceive, pos = read_u32(payload, pos)
                typioparam, pos = read_u32(payload, pos)
                attname, pos = read_len_string(payload, pos)
                typename, pos = read_len_string(payload, pos)
                sendname, pos = read_len_string(payload, pos)
                recvname, pos = read_len_string(payload, pos)
                attrs.append(
                    AttrMeta(
                        attnum=attnum,
                        atttypid=atttypid,
                        atttypmod=atttypmod,
                        attlen=attlen,
                        attbyval=attbyval,
                        attisdropped=attisdropped,
                        attalign=attalign,
                        attstorage=attstorage,
                        fmt=fmt,
                        typsend=typsend,
                        typreceive=typreceive,
                        typioparam=typioparam,
                        attname=attname,
                        typename=typename,
                        sendname=sendname,
                        recvname=recvname,
                    )
                )
            schemas[schema_id] = SchemaRecord(schema_id=schema_id, attrs=attrs)
        elif tag == "ROWD":
            pos = 0
            schema_id, pos = read_u64(payload, pos)
            row_id, pos = read_u64(payload, pos)
            natts, pos = read_u32(payload, pos)
            row_payload_len, pos = read_u32(payload, pos)
            row_payload = payload[pos:pos + row_payload_len]
            pos += row_payload_len
            fields: list[FieldRecord] = []
            for _ in range(natts):
                norm_len, pos = read_i32(payload, pos)
                normalized = None
                if norm_len >= 0:
                    normalized = payload[pos:pos + norm_len]
                    pos += norm_len
                ser_len, pos = read_i32(payload, pos)
                serialized = None
                if ser_len >= 0:
                    serialized = payload[pos:pos + ser_len]
                    pos += ser_len
                fields.append(FieldRecord(normalized=normalized, serialized=serialized))
            rows.append(
                RowRecord(
                    schema_id=schema_id,
                    row_id=row_id,
                    natts=natts,
                    row_payload=row_payload,
                    fields=fields,
                )
            )

        off += 8 + length

    if off != len(data):
        raise ValueError(f"trailing bytes detected: parsed={off} total={len(data)}")

    return DumpFile(counts=counts, schemas=schemas, rows=rows, header=header)
