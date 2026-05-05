# Timing spots catalog

This file lists every timing spot with (1) a concise purpose and (2) the **primary instrumentation locations** in the codebase.

- Spot IDs live in `/data/dbcomm/postgres-citus/src/include/timing_spots.h` and are shared by `/data/dbcomm/citus-dbcomm`.
- For explicit include/nesting relationships (coverage trees), see `spot_hierarchy.md`.

## Notes carried from `timing_spots.h`

The comment block in `/data/dbcomm/postgres-citus/src/include/timing_spots.h` captures intended workflow pairings that are easy to lose if one only scans call sites. The important ones for communication/data movement are:

- `FetchIntermediate_*` and `SendViaCopy_*` are the pull-side pair for intermediate-result transfer.
- `ReceiveAndWriteCopyData_*` is the worker-side receive/write half of COPY-based intermediate-result push.
- `ReceiveResults_*` is the adaptive-executor coordinator path that receives worker results through libpq and then rebuilds tuples.
- `PG_parseInput` is intentionally parse-only over bytes already buffered by `pqReadData()`; it does not include socket IO.

Those intent notes matter because some spot names look symmetric at a high level but actually sit on different plumbing:

- raw backend `pq_*` protocol handling on the server side
- frontend libpq `PGconn` / `PGresult` handling inside Citus backend processes for inter-node traffic
- file-backed intermediate-result transfer, where one side may be tuple serialization while the other side is just opaque byte forwarding

## Catalog (grouped by workflow)

### A) Socket waiting and IO

#### `PG_WAIT`

- Purpose: Wait time inside backend/libpq-style WaitLatchOrSocket/WaitEventSetWait loops (socket readiness).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:217`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure-gssapi.c:460`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure-openssl.c:522`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1051`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:876`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1070`
    - ... (+3 more)
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:226`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure-gssapi.c:464`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure-openssl.c:525`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1056`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:883`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1074`
    - ... (+3 more)

#### `PG_WAIT_DONT_COUNT`

- Purpose: Legacy subset of backend socket-wait time that used to track idle
  message-loop waits outside query processing.
- Relationship to `PG_WAIT`: still nested under `PG_WAIT` at the remaining
  instrumentation sites, but no longer activated by `PostgresMain()`.
- Reporting guidance: keep this only for compatibility with older logs. It is
  no longer part of the recommended communication-stack or denominator analysis.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:219`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:357`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:225`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:363`

Activation scope:

- The old `PostgresMain()` toggles were removed. The remaining call sites in
  `secure_read()` / `secure_write()` stay compiled for old-log compatibility,
  but `pg_wait_dont_count_active` is no longer raised in the backend main loop.

#### `QUERY_WAIT_WALL`

- Purpose: Top-level excluded wait total paired with `QUERY_ACTIVE_WALL`.
- Reporting guidance: optional/debug denominator companion. Use it to inspect
  how much wait was subtracted from the query-cycle wall clock, but do not add
  it into the communication-stack total.
- Driver call sites:
  - `logger_query_active_wall_pause_wait()` / `resume_wait()` in
    `/data/dbcomm/postgres-citus/src/common/time_instr.c:325` and `:342`
  - backend wait backbone hooks in
    `/data/dbcomm/postgres-citus/src/include/utils/wait_event.h:87` and `:111`
  - FE-libpq explicit wait hooks in
    `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1222`,
    `:1252`, `:1270`, and `:1313`

#### `QUERY_ACTIVE_WALL`

- Purpose: Primary active-execution wall-clock denominator for one logical
  query cycle on this backend.
- Reporting guidance: use this as the denominator for communication-stack
  percentages. For explicit distributed transactions, sum this per-statement
  timer across the transaction.
- Driver call sites:
  - `logger_query_active_wall_start()` /
    `logger_query_active_wall_stop()` in
    `/data/dbcomm/postgres-citus/src/common/time_instr.c:285` and `:299`
  - query-cycle start helpers in `PostgresMain()` at
    `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5037`,
    `:5096`, `:5126`, `:5147`, `:5172`, and `:5261`
  - query-cycle stop/print boundary in `PostgresMain()` at
    `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:4954`

#### `PG_FE_WAIT`

- Purpose: Frontend (libpq) time spent in poll/select waiting for socket readiness (PQsocketPoll).
- Reporting guidance: this remains the concrete FE-libpq wait-location timer.
  The same wait intervals are also excluded from `QUERY_ACTIVE_WALL`.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1223`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1271`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1251`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:1310`

#### `PG_FE_SOCK_READ`

- Purpose: Frontend (libpq) socket-read path (pqReadData): buffer management + pqsecure_read retries + EOF/error handling.
- Reporting guidance: this remains an execution leaf, not a wait bucket. The timer is paused around the rare `pqReadReady()` -> `PQsocketPoll()` fallback, so `PG_FE_WAIT` remains the only frontend wait timer.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:598`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:604`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:644`
    - ... (+12 more)

#### `PG_FE_SOCK_WRITE`

- Purpose: Frontend (libpq) active socket-write execution in `pqSendSome()`. The timer is now paused around `pqWait()` and the opportunistic `pqReadData()` side path, so wait/read are left to `PG_FE_WAIT` / `PG_FE_SOCK_READ`.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:861`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:882`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-misc.c:887`
    - ... (+5 more)

#### `PG_BE_SOCK_READ`

- Purpose: Backend secure read path (`secure_read`): active recv/copy/TLS/GSS execution only. The timer is paused around the blocking `PG_WAIT` section.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:182`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:270`

#### `PG_BE_SOCK_WRITE`

- Purpose: Backend secure write path (`secure_write`): active send/copy/TLS/GSS execution only. The timer is paused around the blocking `PG_WAIT` section.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:319`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/be-secure.c:390`

#### `PG_parseInput`

- Purpose: Frontend parsing of already-buffered protocol bytes (parseInput -> pqParseInput3), no socket IO.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2026`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2028`

#### `PQ_getbyte`

- Purpose: Backend receive-side protocol type-byte consumption in `pq_getbyte()`.
- Reporting guidance: use this with `PQ_getmessage`, `PQ_getbytes`, and `PG_BE_SOCK_READ` when you want the additive backend receive-side protocol/transport leaves. The timer is paused around `pq_recvbuf()` so lower socket refill stays in `PG_BE_SOCK_READ`.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:972`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:986`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:990`

#### `PQ_getmessage`

- Purpose: Backend receive-side protocol framing and message-buffer management in `pq_getmessage()`: length decoding, destination `StringInfo` management, and whole-message framing around the lower buffered-byte copies.
- Reporting guidance: this is a backend receive-side staging/control leaf, analogous to `PQ_putmessage` on the send side. Nested `pq_getbytes()` calls are paused out, so `PQ_getmessage` is intended to be additive with `PQ_getbytes` and `PG_BE_SOCK_READ`.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1243`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1254`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1266`
    - ... (+4 more)

#### `PQ_getbytes`

- Purpose: Backend receive-buffer draining/copy in `pq_getbytes()`: copy already-buffered FE/BE message bytes from `PqRecvBuffer` into the destination message buffer or scalar field storage.
- Reporting guidance: this is the backend-side counterpart to `PQ_putmessage` for input-side buffer handling. The actual socket refill remains in `PG_BE_SOCK_READ`, and higher receive-side protocol framing now sits in `PQ_getbyte` / `PQ_getmessage`.

### B) Backend query lifecycle

#### `ExecSimpleQuery`

- Purpose: Coarse backend timing around one simple-query/execute message in Postgres main loop.
- Reporting guidance: the summary scripts expose this as `query_exec_core_ns`
  only as legacy context. `QUERY_ACTIVE_WALL` is now the primary denominator.
- Boundary caveat: this timer still only covers the backend command-execution
  core inside `exec_simple_query()` / `exec_execute_message()`, not the whole
  query-cycle envelope.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5064`
    - `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5158`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5074`
    - `/data/dbcomm/postgres-citus/src/backend/tcop/postgres.c:5160`

#### `ParseQuery`

- Purpose: Intended backend SQL parse timing (not wired in this snapshot).
- Call sites: none (not wired in C code).

#### `QueryAnalyzeAndRewrite`

- Purpose: Intended backend analyze+rewrite timing (not wired in this snapshot).
- Call sites: none (not wired in C code).

#### `QueryExecution`

- Purpose: Intended backend executor timing (not wired in this snapshot).
- Call sites: none (not wired in C code).

#### `QueryPlanning`

- Purpose: Intended backend planner timing (not wired in this snapshot).
- Call sites: none (not wired in C code).

#### `EndingComms`

- Purpose: Intended backend end-of-command comms timing (not wired in this snapshot).
- Call sites: none (not wired in C code).

### C) PostgreSQL COPY

#### `CopyTo_`

- Purpose: Coarse backend timing for COPY TO execution (DoCopy -> DoCopyTo).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copy.c:324`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copy.c:332`

#### `CopyTo_GetTuples`

- Purpose: COPY TO: time spent fetching tuples from the scan (per-iteration).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:873`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:883`

#### `CopyTo_CopyOneRowTo`

- Purpose: COPY TO: time spent formatting one tuple into COPY output (CopyOneRowTo path).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:886`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:891`

#### `CopyTo_fwrite`

- Purpose: COPY TO: time spent writing COPY bytes to a server-side file (fwrite path).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:208`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyto.c:242`

#### `Receiver_CopyFrom`

- Purpose: Coarse backend timing for COPY FROM receive+ingest (DoCopy -> CopyFrom).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copy.c:307`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copy.c:317`

#### `NextCopyFrom_`

- Purpose: COPY FROM: per-row decode/parse work to produce the next row (NextCopyFrom).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:997`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1002`

#### `CopyFrom_CopyGetData`

- Purpose: COPY FROM: low-level byte acquisition for COPY protocol (CopyGetData).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:252`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfromparse.c:354`

#### `CopyFromInsertIntoTable`

- Purpose: COPY FROM: per-row insert into table (constraints/triggers/insert).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1187`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1256`
    - `/data/dbcomm/postgres-citus/src/backend/commands/copyfrom.c:1273`
    - ... (+1 more)

### D) Backend tuple send

#### `Printtup_Startup`

- Purpose: Intended startup timing for tuple DestReceiver setup (not wired in this snapshot).
- Call sites: none (not wired in C code).

#### `Printtup`

- Purpose: Backend per-row DataRow construction + attribute output conversion + send to client/coordinator.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:317`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:402`

#### `Printtup_Ser`

- Purpose: Backend per-row row-message serialization before the transport handoff (`slot_getallattrs`, type output/send functions, and appending fields into the reusable `StringInfo`).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:323`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:387`

#### `Printtup_Net`

- Purpose: Backend per-row transport handoff from the completed row buffer into libpq backend transport (`pq_endmessage_reuse`); lower leaves such as `PQ_putmessage` and `PG_BE_SOCK_WRITE` can further refine this region.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:395`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:401`

#### `PQ_putmessage`

- Purpose: Backend protocol message staging/handoff at `socket_putmessage()`: per-message framing plus `PqSendBuffer` staging.
- Reporting guidance: `PQ_putmessage` is now paused around forced flushes in `internal_putbytes()`, so lower socket-write execution remains in `PG_BE_SOCK_WRITE`.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1502`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1515`
    - `/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1520`

#### `PQ_sendCommand`

- Purpose: Frontend-libpq query/control message staging for `PQsendQuery*()`, `PQsendPrepare()`, and `PQsendTypedCommand()` message assembly.
- Reporting guidance: this is the staging/copy leaf for remote-command submission. Flush/write work is excluded and remains in `PG_FE_SOCK_WRITE`.

#### `PQ_putCopyData`

- Purpose: Frontend-libpq COPY message staging for inter-node COPY IN traffic (`PQputCopyData`): frame/copy the CopyData message into libpq buffers.
- Reporting guidance: `parseInput()` and `pqFlush()` are no longer counted inside this leaf; use `PG_parseInput` and `PG_FE_SOCK_WRITE` for those parts.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2730`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2754`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2760`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2767`
    - `/data/dbcomm/postgres-citus/src/interfaces/libpq/fe-exec.c:2771`

#### `PQ_putCopyEnd`

- Purpose: Frontend-libpq COPY-end message staging for `PQputCopyEnd()`.
- Reporting guidance: this captures `CopyDone` / `CopyFail` / optional `Sync` message assembly only; lower flush/write work remains in `PG_FE_SOCK_WRITE`.

#### `PQ_getResult`

- Purpose: Frontend-libpq buffered-result/control path in `PQgetResult()`.
- Reporting guidance: this timer is paused around `pqFlush()`, `pqWait()`, `pqReadData()`, and `parseInput()`, so it is intended to be additive with `PG_FE_WAIT`, `PG_FE_SOCK_READ`, `PG_FE_SOCK_WRITE`, and `PG_parseInput`.

#### `PQ_getCopyData`

- Purpose: Frontend-libpq buffered COPY-row extraction/copy path in `pqGetCopyData3()`.
- Reporting guidance: this is the pull-side counterpart to `PQ_putCopyData`. Waiting and socket reads are excluded and remain in `PG_FE_WAIT` / `PG_FE_SOCK_READ`.

#### `PQ_connectStart`

- Purpose: Citus connection-setup start path around `PQconnectStartParams()` in `StartConnectionEstablishment()`.
- Reporting guidance: this is the synchronous front half of connection setup, including conninfo processing and address/DNS preparation before the later poll loop.

#### `PQ_connectPoll`

- Purpose: Citus connection-setup progress path around `PQconnectPoll()` in both connection-management and adaptive-executor connection loops.
- Reporting guidance: treat this as a connection-setup bucket, not a steady-state transport leaf.

### E) Citus off-path bulk movement

#### `CreateCitusTable_`

- Purpose: Coarse timing for Citus table distribution/metadata workflow inside CreateCitusTable.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1114`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:1384`

#### `CopyFromLocalTableIntoDistTable_`

- Purpose: Off-path copy-local-data-into-shards workflow: heap scan + per-tuple DestReceiver send via COPY.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2696`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2710`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2778`

#### `DoCopyFromLocalTableIntoShards_`

- Purpose: Off-path local table scan loop that feeds tuples into the Citus COPY DestReceiver.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2792`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/create_distributed_table.c:2819`

#### `CitusSendTupleToPlacements_`

- Purpose: Per-tuple DestReceiver work that routes/serializes and sends COPY bytes to shard placements.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2358`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2456`

#### `SerializeAndCopyRow_`

- Purpose: Serialize one tuple into COPY row bytes (AppendCopyRowData + buffer append).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2419`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2439`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2428`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2445`

#### `SendCopyDataToPlacement_`

- Purpose: Send COPY bytes for a placement over a libpq connection (COPY IN data plane).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2379`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2391`
    - ... (+1 more)
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2385`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2408`
    - ... (+1 more)

#### `WriteTupleToLocal_`

- Purpose: Local-path handling during multi-copy: buffer/serialize tuple for local placement and/or flush to local file/shard.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2335`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2350`
    - ... (+1 more)
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2344`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2354`
    - ... (+1 more)

#### `DoLocalCopy_`

- Purpose: Final local COPY flush at end of multi-copy for remaining buffered tuples.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2692`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2694`

### F) Citus intermediate results (pull)

#### `FetchIntermediate_`

- Purpose: Pull-based intermediate results: fetch_intermediate_results UDF end-to-end (remote COPY OUT -> local file).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:926`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:988`

#### `FetchIntermediate_CopyAndWrite`

- Purpose: Pull-based intermediate results: one iteration of COPY chunk read + write attempt.
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1051`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1055`

#### `FetchIntermediate_FileWrite`

- Purpose: Pull-based intermediate results: write received COPY chunk bytes into local intermediate file.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1123`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:1135`

#### `SendViaCopy_`

- Purpose: Send a regular file via COPY TO STDOUT (used for intermediate-result transfer).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:118`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:162`

#### `SendViaCopy_Send`

- Purpose: SendViaCopy: emit COPY data messages to the peer (network/protocol send).
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:144`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:148`

#### `SendViaCopy_FileRead`

- Purpose: SendViaCopy: read a chunk from disk into a buffer (FileReadCompat).
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:134`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:151`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:136`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:153`

### G) Citus intermediate results (push)

#### `ExecutePlanIntoColocatedIntermediateResults_`

- Purpose: Coordinator-side execution that produces colocated intermediate results for INSERT..SELECT paths.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:347`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:382`

#### `ExecutePlanIntoDestReceiver_`

- Purpose: Execute a plan into a DestReceiver (wrapper used by intermediate-results push execution).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:374`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:376`

#### `ReceiveAndWriteCopyData_`

- Purpose: Push-based intermediate results (receiver side): receive COPY FROM STDIN bytes and write a regular file.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:55`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:101`

#### `ReceiveAndWriteCopyData_Deser`

- Purpose: Push receive path: deserialize/mem-copy COPY protocol payload into a buffer.
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:65`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:90`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:67`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:95`

#### `ReceiveAndWriteCopyData_WriteFile`

- Purpose: Push receive path: file append of received COPY bytes.
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:75`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/transmit.c:77`

#### `RemoteFileDestReceiver_Init`

- Purpose: RemoteFileDestReceiver init: setup COPY format state and establish remote COPY IN sessions / local file.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:264`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:299`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:286`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:379`

#### `RemoteFileDestReceiver_SerAndSend`

- Purpose: RemoteFileDestReceiver per-tuple: serialize tuple to COPY bytes + broadcast to nodes (+ optional local write).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:409`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:475`

#### `RemoteFileDestReceiver_Ser`

- Purpose: RemoteFileDestReceiver: serialize tuple to COPY bytes (attribute output + buffer build).
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:431`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:447`

#### `RemoteFileDestReceiver_Send`

- Purpose: RemoteFileDestReceiver: broadcast COPY bytes to remote connections.
- Call sites (first shown + count):
  - `verbose_timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:450`
  - `verbose_timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:455`

#### `RemoteFileDestReceiver_SerAndSend_WriteLocal`

- Purpose: RemoteFileDestReceiver: optional local file append of the same COPY bytes.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:461`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:463`

### H) Citus result streaming

#### `CheckConnectionReady_`

- Purpose: Citus adaptive-executor post-wakeup libpq processing before result extraction: `PQflush`, `PQconsumeInput`, `PQisBusy`, and local connection-state checks. This intentionally excludes the outer `WaitEventSetWait()` sleep.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3716`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3722`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3729`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3738`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3752`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3767`

#### `ReceiveResults_`

- Purpose: Coordinator adaptive-executor receive loop for worker result streaming (PQgetResult -> tuple store).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4009`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4247`

#### `ReceiveResults_Net`

- Purpose: ReceiveResults: buffered `PQgetResult()` extraction after the connection is no longer busy. This is not the outer socket wait and not the earlier `PQconsumeInput` / `PQisBusy` processing.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4042`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4044`

#### `ReceiveResults_Deserialize`

- Purpose: ReceiveResults: interpret `PGresult` status and extract column values (libpq result parsing). Under the current adaptive-executor `PQsetSingleRowMode()` invariant, this is effectively the per-streamed-row libpq-side handling bucket.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4037`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4058`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4073`
    - ... (+3 more)

#### `ReceiveResults_BuildTuples`

- Purpose: ReceiveResults result materialization: build local heap tuples and push into tuple store.
- Reporting guidance: keep this in a separate result-materialization bucket rather than the strict transport/protocol bucket. Under the current adaptive-executor single-row-mode invariant, each call corresponds to the materialization of one streamed worker row.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4206`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4235`

#### `ReceiveResults_HeapFormTuple`

- Purpose: Tuple materialization cost (heap_form_tuple) while building received rows.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:120`
    - `/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:2266`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:125`
    - `/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:2268`

### I) COPY hook / utility

#### `ProcessCopyStmt_`

- Purpose: Citus COPY hook wrapper timing in ProcessCopyStmt (covers special result-id COPY and CitusCopyFrom/To dispatch).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2993`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3013`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:3073`
    - ... (+2 more)

### J) Distributed transactions

#### `XACT_PROCESSING`

- Purpose: Coarse distributed-transaction lifecycle timing (started once per distributed xact).
- Reporting guidance: the summary scripts expose this as
  `distributed_tx_lifecycle_wall_ns` only as legacy context because it is a
  lifecycle wall timer, not the primary active-execution denominator.
- Boundary caveat: while [`logger_state.in_distributed_xact`](/data/dbcomm/postgres-citus/src/common/time_instr.c:605) is true, [`logger_reset()`](/data/dbcomm/postgres-citus/src/common/time_instr.c:598) preserves transactional timers across per-command prints, so this timer can span inter-statement gaps until [`EndDistributedTransactionTimingIfNeeded()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:159) ends it.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:88`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:167`

#### `XACT_TS_SendRemoteCommand`

- Purpose: Coordinator time to send a remote command to a worker connection (enqueue/flush).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:530`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:570`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:551`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:588`

#### `XACT_TS_GetRemoteCommandResult`

- Purpose: Coordinator time to receive+parse a remote command result (GetRemoteCommandResult).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:689`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:725`

#### `XACT_TS_WaitForConnections`

- Purpose: Coordinator time spent driving IO until connections are not-busy/ready (flush+consume+wait).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:976`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:825`
    - ... (+1 more)
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:1182`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:871`
    - ... (+2 more)

#### `XACT_TS_coordinated_commit_abort`

- Purpose: Coordinator time spent in coordinated commit/abort across worker transactions.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1066`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1142`
    - ... (+4 more)
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1126`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/remote_transaction.c:1198`
    - ... (+4 more)

#### `XACT_TS_EndCommand`

- Purpose: Backend time sending CommandComplete at end of command (protocol ACK).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:175`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/backend/tcop/dest.c:201`

#### `XACT_TS_CoordinatorPrepare`

- Purpose: Coordinator-side prepare bookkeeping step in transaction management.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:347`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:351`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/transaction/transaction_management.c:386`

#### `XACT_WAIT`

- Purpose: Blocking/sleeping wait inside distributed remote-command IO loops (WaitLatchOrSocket/WaitEventSetWait).
- Relationship to `PG_WAIT`: strict subset at current call sites (each `XACT_WAIT` region is nested under `PG_WAIT`).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:877`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:994`
  - `timing_end`:
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:882`
    - `/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:1000`

### K) psql client

#### `SendQuery_func`

- Purpose: psql SendQuery() end-to-end time for one user query string (includes BEGIN/autocommit + result processing).
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/bin/psql/common.c:1096`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/bin/psql/common.c:1308`

#### `ExecQueryAndProcessResults_func`

- Purpose: psql core send-query + PQgetResult loop + printing.
- Call sites (first shown + count):
  - `timing_start`:
    - `/data/dbcomm/postgres-citus/src/bin/psql/common.c:1470`
  - `timing_end`:
    - `/data/dbcomm/postgres-citus/src/bin/psql/common.c:1492`
    - `/data/dbcomm/postgres-citus/src/bin/psql/common.c:1530`
    - ... (+3 more)

#### `ExecQueryAndProcessResults_parse_results`

- Purpose: Intended parse-results timing in client/libpq (currently commented out).
- Call sites: none (not wired in C code).

#### `ExecQueryAndProcessResults_read_data`

- Purpose: Intended client-side socket read timing during result fetch (not wired).
- Call sites: none (not wired in C code).

#### `SomethingElseFunc`

- Purpose: Placeholder timing spot (not wired).
- Call sites: none (not wired in C code).
