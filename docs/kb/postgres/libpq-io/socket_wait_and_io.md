# FE/BE socket waits and IO timing

## Scope

This note documents the wait and active-IO timing paths in:

- backend FE/BE transport code under `src/backend/libpq/*`
- frontend libpq transport code under `src/interfaces/libpq/*`

The key distinction after the new active-wall work is:

- `QUERY_ACTIVE_WALL` is the primary execution-only denominator
- `QUERY_WAIT_WALL` is the excluded wait total
- `PG_FE_WAIT` / `PG_WAIT` remain useful wait-location diagnostics
- `PG_WAIT_DONT_COUNT` is now legacy-only

## Backend waits now flow through the PostgreSQL wait backbone

The central hook is in `pgstat_report_wait_start()` /
`pgstat_report_wait_end()` in `src/include/utils/wait_event.h:87` and
`src/include/utils/wait_event.h:111`.

Those helpers now call:

- `logger_query_active_wall_pause_wait()` in `src/common/time_instr.c:325`
- `logger_query_active_wall_resume_wait()` in `src/common/time_instr.c:342`

So any backend wait that PostgreSQL already reports through the standard
wait-event framework automatically pauses `QUERY_ACTIVE_WALL`.

That covers, for example:

- backend client socket waits in `secure_read()` / `secure_write()` when
  `WaitEventSetWait(...)` runs in `src/backend/libpq/be-secure.c:227` and
  `src/backend/libpq/be-secure.c:371`
- latch/socket waits in `WaitEventSetWait()` under `src/backend/storage/ipc/latch.c`
- file IO waits such as `FileReadV()` in `src/backend/storage/file/fd.c`
- lock/LWLock/buffer-pin waits that already report through the same wait-event
  API

## FE-libpq waits still need an explicit hook

Frontend libpq wait polling does not use PostgreSQL's backend wait-event API.

So `PQsocketPoll()` in `src/interfaces/libpq/fe-misc.c:1207` now explicitly:

- pauses the active wall before `poll()` / `select()` at
  `src/interfaces/libpq/fe-misc.c:1222` and `src/interfaces/libpq/fe-misc.c:1270`
- resumes it afterwards at
  `src/interfaces/libpq/fe-misc.c:1252` and `src/interfaces/libpq/fe-misc.c:1311`

`PG_FE_WAIT` still wraps the actual `poll()` / `select()` wait:

- `timing_start(PG_FE_WAIT)` at
  `src/interfaces/libpq/fe-misc.c:1223` and `src/interfaces/libpq/fe-misc.c:1271`
- `timing_end(PG_FE_WAIT)` at
  `src/interfaces/libpq/fe-misc.c:1251` and `src/interfaces/libpq/fe-misc.c:1310`

So `PG_FE_WAIT` remains necessary as a wait-location timer even though
`QUERY_WAIT_WALL` now accumulates the excluded top-level wait total.

## Active read/write leaves

### Backend side

- `PG_BE_SOCK_READ` wraps active recv/copy work in `secure_read()` at
  `src/backend/libpq/be-secure.c:182`.
- `PG_BE_SOCK_WRITE` wraps active send/copy work in `secure_write()` at
  `src/backend/libpq/be-secure.c:319`.

Both timers are paused across the blocking wait path, so the actual sleep is no
longer inside those active execution leaves.

### Frontend side

- `PG_FE_SOCK_READ` wraps active libpq receive work in `pqReadData()` at
  `src/interfaces/libpq/fe-misc.c:598`.
- `PG_FE_SOCK_WRITE` wraps active libpq send work in `pqSendSome()` at
  `src/interfaces/libpq/fe-misc.c:861`.
- `PG_parseInput` wraps parse-only work on already-buffered bytes in
  `parseInput()` at `src/interfaces/libpq/fe-exec.c:2026`.

`PG_FE_SOCK_READ` is still paused around the rare `pqReadReady()` ->
`PQsocketPoll()` fallback path, so `PG_FE_WAIT` remains the only frontend wait
leaf.

## Backend receive-side protocol leaves

The backend receive path is now leaf-shaped enough to account for protocol
framing separately from socket waits:

- `PQ_getbyte` in `src/backend/libpq/pqcomm.c:972`
- `PQ_getbytes` in `src/backend/libpq/pqcomm.c:1064`
- `PQ_getmessage` in `src/backend/libpq/pqcomm.c:1243`

Each pauses around `pq_recvbuf()` or nested lower helpers so the additive
relationship is:

- `PG_BE_SOCK_READ` for socket execution
- `PQ_getbyte` / `PQ_getmessage` / `PQ_getbytes` for backend receive-side
  protocol framing and buffered-byte handling

## `PG_WAIT` and `PG_WAIT_DONT_COUNT`

### `PG_WAIT`

`PG_WAIT` is still useful as a local wait-location timer. It remains explicitly
instrumented in places such as:

- `src/backend/libpq/be-secure.c:222`
- `src/backend/libpq/be-secure.c:366`
- `src/backend/distributed/connection/remote_commands.c:880`
- `src/backend/distributed/executor/intermediate_results.c:1070`

But it is no longer the top-level denominator correction term.

### `PG_WAIT_DONT_COUNT`

`PG_WAIT_DONT_COUNT` is now redundant for the new workflow:

- the old `pg_wait_dont_count_active` toggles in `PostgresMain()` were removed
- the timer remains only in `secure_read()` / `secure_write()` for compatibility
  with older logs

Treat it as legacy/debug only. The aggregation scripts should rely on
`QUERY_ACTIVE_WALL` and `QUERY_WAIT_WALL` instead.
