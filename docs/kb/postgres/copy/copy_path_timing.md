# COPY timing spots (backend)

## Scope

This doc explains how COPY TO / COPY FROM are instrumented, and which spots cover others.

Relevant timing spots (definitions in `src/include/timing_spots.h`):

- COPY TO: `CopyTo_` (`src/include/timing_spots.h:69`), `CopyTo_GetTuples` (`src/include/timing_spots.h:70`), `CopyTo_CopyOneRowTo` (`src/include/timing_spots.h:71`), `CopyTo_fwrite` (`src/include/timing_spots.h:72`)
- COPY FROM: `Receiver_CopyFrom` (`src/include/timing_spots.h:74`), `NextCopyFrom_` (`src/include/timing_spots.h:75`), `CopyFrom_CopyGetData` (`src/include/timing_spots.h:76`), `CopyFromInsertIntoTable` (`src/include/timing_spots.h:77`)

## COPY TO (export)

Top-level wrapper:

- `DoCopy()` wraps the whole COPY TO command with `CopyTo_` (`src/backend/commands/copy.c:320`, `src/backend/commands/copy.c:324`–`src/backend/commands/copy.c:332`).

Tuple fetch + row send:

- In `DoCopyTo()` table scan loop, `CopyTo_GetTuples` wraps `table_scan_getnextslot(...)` and immediate per-tuple deconstruction (`slot_getallattrs`) (`src/backend/commands/copyto.c:865`, `src/backend/commands/copyto.c:873`–`src/backend/commands/copyto.c:883`).
- `CopyTo_CopyOneRowTo` wraps `CopyOneRowTo(cstate, slot)` (`src/backend/commands/copyto.c:886`–`src/backend/commands/copyto.c:891`).

File write:

- When the COPY destination is a file (`COPY_FILE`), `CopyTo_fwrite` wraps the `fwrite()` call in `CopySendEndOfRow()` (`src/backend/commands/copyto.c:190`, `src/backend/commands/copyto.c:208`–`src/backend/commands/copyto.c:242`).

Coverage relationship summary:

- `CopyTo_` includes `CopyTo_GetTuples`, `CopyTo_CopyOneRowTo`, and `CopyTo_fwrite` (depending on COPY destination and query form).

## COPY FROM (import)

Top-level wrapper:

- `DoCopy()` wraps Begin/CopyFrom/End with `Receiver_CopyFrom` (`src/backend/commands/copy.c:296`, `src/backend/commands/copy.c:307`–`src/backend/commands/copy.c:317`).

Row production:

- The main COPY FROM loop wraps `NextCopyFrom(...)` with `NextCopyFrom_` (`src/backend/commands/copyfrom.c:957`, `src/backend/commands/copyfrom.c:997`–`src/backend/commands/copyfrom.c:1002`).

Low-level byte fetch:

- `CopyFrom_CopyGetData` wraps the `CopyGetData()` helper (`src/backend/commands/copyfromparse.c:246`, `src/backend/commands/copyfromparse.c:252`–`src/backend/commands/copyfromparse.c:354`).
- `CopyGetData()` is called from the COPY FROM parse/consume pipeline, so in practice its time is typically included inside `NextCopyFrom_`.

Insert path:

- `CopyFromInsertIntoTable` wraps the “insert tuple” portion in the COPY FROM loop (`src/backend/commands/copyfrom.c:1186`, `src/backend/commands/copyfrom.c:1187` … `src/backend/commands/copyfrom.c:1318` with multiple early-continue `timing_end` sites).

Coverage relationship summary:

- `Receiver_CopyFrom` includes:
  - `NextCopyFrom_` (row production)
  - `CopyFrom_CopyGetData` (raw bytes)
  - `CopyFromInsertIntoTable` (insertion)

