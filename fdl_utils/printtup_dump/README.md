# `printtup` Binary Dump Utilities

This directory contains the prototype tooling for capturing and replaying the
binary result-row serialization performed by PostgreSQL's
[`printtup()`](../../src/backend/access/common/printtup.c#L745).

The goal is to make PostgreSQL dump:

1. the normalized pre-serialization input for each output column
2. the exact serialized binary bytes produced for each output column
3. the exact `DataRow` payload bytes sent by `printtup()`
4. enough schema and function metadata to identify which type serializer was used

That dump can then be inspected or replayed out of band, which is useful for
building an offloaded serializer such as a DPU-side implementation.

## Where The Dump Comes From

The backend instrumentation lives in
[`printtup.c`](../../src/backend/access/common/printtup.c).

- [`PrinttupBinaryDumpInit()`](../../src/backend/access/common/printtup.c#L263)
  opens the optional sidecar file and writes a `HEAD` record.
- [`printtup_prepare_info()`](../../src/backend/access/common/printtup.c#L691)
  resolves the binary serializer metadata for each output column via
  [`getTypeBinaryOutputInfo()`](../../src/backend/utils/cache/lsyscache.c#L2973)
  and [`getTypeBinaryInputInfo()`](../../src/backend/utils/cache/lsyscache.c#L2940),
  caches those in `PrinttupAttrInfo`, and then calls
  [`PrinttupBinaryDumpWriteSchema()`](../../src/backend/access/common/printtup.c#L340)
  to emit a `SCHM` record.
- [`printtup()`](../../src/backend/access/common/printtup.c#L745) still performs
  normal binary serialization through [`SendFunctionCall()`](../../src/backend/utils/fmgr/fmgr.c#L1743).
  After each attribute is serialized, it stores the resulting field payload.
  After the row is complete, it calls
  [`PrinttupBinaryDumpWriteRow()`](../../src/backend/access/common/printtup.c#L392)
  to emit a `ROWD` record.
- [`PrinttupBinaryDumpNormalizeDatumBytes()`](../../src/backend/access/common/printtup.c#L223)
  is the key normalization step. It copies by-value datums directly, detoasts
  varlena values with `PG_DETOAST_DATUM`, copies cstrings including the
  terminator, and otherwise copies the fixed-length referenced bytes.

## Binary-Only Scope

This prototype is intentionally binary-format only.

[`PrinttupBinaryDumpWriteSchema()`](../../src/backend/access/common/printtup.c#L340)
 skips descriptors that are not fully binary, and
[`printtup_prepare_info()`](../../src/backend/access/common/printtup.c#L709)
 only records `typsend` / `typreceive` metadata for `format == 1`.

Text mode is not covered because text output depends on additional layers such
as [`pq_sendcountedtext()`](../../src/backend/libpq/pqformat.c#L136), client
encoding conversion, and type-specific formatting GUCs.

## Dump File Location

Enable the dump by setting:

```bash
export PG_PRINTTUP_BINARY_DUMP=1
export PG_PRINTTUP_BINARY_DUMP_FILE=printtup_binary_dump_test.bin
```

One subtle but important detail: the backend changes its working directory to
the data directory in
[`ChangeToDataDir()`](../../src/backend/utils/init/miscinit.c#L455).
So a relative dump path lands under `PGDATA`, not under the shell directory
from which `pg_ctl` was started.

Example from the current setup:

```text
/data/dbcomm/pg-citus/data/printtup_binary_dump_test.bin
```

## Dump Record Format

The dump is a sequence of records. Each record is:

1. 4-byte ASCII tag
2. 4-byte big-endian payload length
3. payload bytes

The current tags are:

- `HEAD`: dump header
- `SCHM`: one tuple descriptor / schema block
- `ROWD`: one serialized result row

### `HEAD`

Written by
[`PrinttupBinaryDumpInit()`](../../src/backend/access/common/printtup.c#L263).

Contains:

- dump magic (`PTBDMP1`)
- dump version
- backend endianness flag
- `sizeof(Datum)`
- `sizeof(void *)`
- `PG_VERSION_NUM`

This is required because the normalized blobs preserve PostgreSQL's native
in-memory representation for some values, especially varlena datums.

### `SCHM`

Written by
[`PrinttupBinaryDumpWriteSchema()`](../../src/backend/access/common/printtup.c#L340).

For each attribute it records:

- attribute number
- type OID and typmod
- `attlen`, `attbyval`, `attalign`, `attstorage`, `attisdropped`
- output format code
- chosen `typsend` OID
- matching `typreceive` OID
- `typioparam`
- attribute name
- type name
- qualified send function name
- qualified receive function name

The qualified send function name is the main bridge to the backend C code.

### `ROWD`

Written by
[`PrinttupBinaryDumpWriteRow()`](../../src/backend/access/common/printtup.c#L392).

Contains:

- schema id
- row id
- number of attributes
- full `DataRow` payload bytes accumulated in `myState->buf`
- for each attribute:
  - normalized length or `-1` for NULL
  - normalized bytes
  - serialized length or `-1` for NULL
  - serialized field payload bytes

The `row_payload` is the exact payload that [`printtup()`](../../src/backend/access/common/printtup.c#L745)
 built with [`pq_sendint16()`](../../src/include/libpq/pqformat.h#L136),
 [`pq_sendint32()`](../../src/include/libpq/pqformat.h#L144), and
 [`pq_sendbytes()`](../../src/include/libpq/pqformat.h#L202).
It does not include the protocol message type byte or the outer message length.

## How To Find The Corresponding C Serialization Code

The lookup path is:

1. In the dump schema, read `sendname`.
2. Map that to the backend function implementation.
3. If needed, inspect helper functions that the send function calls.

For example, the validated `lineitem` dump includes:

- `pg_catalog.int4send(integer)` ->
  [`int4send()`](../../src/backend/utils/adt/int.c#L321)
- `pg_catalog.date_send(pg_catalog.date)` ->
  [`date_send()`](../../src/backend/utils/adt/date.c#L231)
- `pg_catalog.bpcharsend(character)` ->
  [`bpcharsend()`](../../src/backend/utils/adt/varchar.c#L251) ->
  [`textsend()`](../../src/backend/utils/adt/varlena.c#L619)
- `pg_catalog.varcharsend(character varying)` ->
  [`varcharsend()`](../../src/backend/utils/adt/varchar.c#L548) ->
  [`textsend()`](../../src/backend/utils/adt/varlena.c#L619)
- `pg_catalog.numeric_send(numeric)` ->
  [`numeric_send()`](../../src/backend/utils/adt/numeric.c#L1161),
  which first calls
  [`init_var_from_num()`](../../src/backend/utils/adt/numeric.c#L7467)
  to decode the packed `Numeric` datum

If you need to trace a new type:

1. inspect the `sendname` in the `SCHM` record
2. search the tree for that function name
3. inspect any helper functions/macros it uses
4. if the type is varlena, also inspect
   [`varatt.h`](../../src/include/varatt.h#L141)
   because the normalized dump preserves PostgreSQL varlena headers

## Varlena Handling

The normalized dump keeps a
portable copy of the datum bytes that `printtup()` saw.

For varlena types, those bytes still include PostgreSQL's in-memory varlena
header. That header is endian-sensitive, as documented in
[`varatt.h`](../../src/include/varatt.h#L141) and implemented through macros
such as [`VARATT_IS_4B` / `VARSIZE_4B`](../../src/include/varatt.h#L211) and
[`VARSIZE_ANY_EXHDR` / `VARDATA_ANY`](../../src/include/varatt.h#L316).

This matters for:

- `bpchar`
- `varchar`
- `text`
- `numeric`

For `numeric`, the normalized blob is a packed varlena `Numeric`, not the
wire-format payload. The wire payload is the external numeric format described
by [`numeric_recv()`](../../src/backend/utils/adt/numeric.c#L1075) and emitted
by [`numeric_send()`](../../src/backend/utils/adt/numeric.c#L1161):

```text
ndigits, weight, sign, dscale, digits[]
```

## Utilities In This Directory

### Shared Parser

[`printtup_binary_dump.py`](./printtup_binary_dump.py)

Provides a common parser for `HEAD`, `SCHM`, and `ROWD`. Both Python tools use
this parser so inspection and replay stay consistent.

### Inspector

[`printtup_binary_dump_inspect.py`](./printtup_binary_dump_inspect.py)

Prints a quick summary of the dump, optional schema metadata, and row-length
summaries.

Example:

```bash
sudo -u dbcomm python3 fdl_utils/printtup_dump/printtup_binary_dump_inspect.py \
  /data/dbcomm/pg-citus/data/printtup_binary_dump_test.bin \
  --show-schema --show-rows 2
```

### Python Reserializer

[`printtup_binary_dump_reserialize.py`](./printtup_binary_dump_reserialize.py)

This replays a narrow subset of PostgreSQL binary serializers for the current
`lineitem` dump:

- `int4send`
- `date_send`
- `bpcharsend` / `varcharsend` through `textsend`
- `numeric_send`

It dispatches by the `sendname` recorded in the dump schema, compares the
replayed field bytes against the serialized field payloads in the `ROWD`
record, and also rebuilds the full `DataRow` payload to compare against the
captured `row_payload`.

Example:

```bash
sudo -u dbcomm python3 fdl_utils/printtup_dump/printtup_binary_dump_reserialize.py \
  /data/dbcomm/pg-citus/data/printtup_binary_dump_test.bin
```

Expected output for the validated `lineitem` dump:

```text
schema_id=1 rows_checked=100 fields_checked=1600 mismatches=0
```

### C Reserializer

[`printtup_binary_dump_reserialize.c`](./printtup_binary_dump_reserialize.c)

This is the standalone C equivalent of the Python reserializer. It does not
directly invoke backend `typsend` functions, because those require backend
runtime and `fmgr` setup. Instead, it duplicates the relevant PostgreSQL C
serialization logic directly in an offline verifier.

Important pieces:

- [`parse_schema_record()`](./printtup_binary_dump_reserialize.c#L324)
  loads the schema metadata
- [`decode_varlena()`](./printtup_binary_dump_reserialize.c#L404)
  interprets the normalized varlena headers
- [`reserialize_numeric_send()`](./printtup_binary_dump_reserialize.c#L517)
  duplicates the packed-`Numeric` decoding used by
  [`init_var_from_num()`](../../src/backend/utils/adt/numeric.c#L7467)
- [`verify_row_record()`](./printtup_binary_dump_reserialize.c#L653)
  reconstructs field payloads and the full `DataRow` payload

Build and run:

```bash
cc -O2 -Wall -Wextra -std=c11 \
  fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c \
  -o /tmp/printtup_binary_dump_reserialize_c

sudo -u dbcomm /tmp/printtup_binary_dump_reserialize_c \
  /data/dbcomm/pg-citus/data/printtup_binary_dump_test.bin
```

Expected output:

```text
head_records=1 schema_records=1 row_records=100
rows_checked=100 fields_checked=1600 mismatches=0
```

## Recommended Workflow

1. Run PostgreSQL with `PG_PRINTTUP_BINARY_DUMP=1`.
2. Ensure the client actually requests binary results.
   The previous validation used a tiny libpq program with
   `PQexecParams(..., resultFormat=1)` instead of plain `psql`, because
   `psql` typically uses text results.
3. Inspect the dump with the inspector.
4. Read the `sendname` entries from the schema.
5. Map those names back to the backend C implementations.
6. Replay the dump with the Python or C verifier.
7. Use the normalized field bytes plus schema metadata as the input to your
   out-of-band serializer prototype.

## Current Validation Status

The current validated dump is:

```text
/data/dbcomm/pg-citus/data/printtup_binary_dump_test.bin
```

Both replay paths have been run against it successfully:

- Python verifier: `rows_checked=100`, `fields_checked=1600`, `mismatches=0`
- C verifier: `rows_checked=100`, `fields_checked=1600`, `mismatches=0`

That means the captured normalized inputs are sufficient, for this `lineitem`
binary dump, to reproduce the exact field payloads and the exact `DataRow`
payload bytes offline.
