# Physical WAL streaming and synchronous commit

## Scope

- **What this doc explains**: how PostgreSQL physical replication moves WAL from backend-private assembly into shared WAL buffers, to local durable `pg_wal`, to `walsender` messages, to standby write/flush/apply; and how `synchronous_commit` changes the commit wait point.
- **What this doc does NOT cover**: logical replication decoding/publication, archive recovery details beyond where WAL replay starts, or custom prototype code.
- **Primary directory**: `docs/kb/postgres/replication/`
- **Doc type**: `factual`

## Why this exists (context)

The user discussion focused on whether PostgreSQL physical replication is just “send raw WAL bytes and wait for an ACK”, where the bytes live before send, what the protocol format is, why the primary flushes locally before streaming, and whether this could map onto the external communication service the same way tuple views and typed commands do.

This note records the current code path and the main corrections:

- `synchronous_commit` does not start streaming replication; it only changes what commit waits for.
- physical replication does not stream backend-private memory directly; WAL is inserted into shared WAL buffers first.
- the transport payload is raw WAL bytes with a small replication-protocol envelope, not tuple- or command-level semantics.
- the standby does not reconstruct a higher-level object on ingress; it writes raw WAL and replay later interprets it.

## Key code pointers

- [`XLogInsert()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xloginsert.c:474) - top-level WAL insertion after record assembly in backend-private working memory.
- [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750) - reserves shared WAL space and copies the record into the shared WAL buffer cache.
- [`XLogCtlData`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:451) and [`XLogCtl->pages`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:493) - shared memory holding unwritten WAL pages.
- [`XLOGShmemInit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:4873) - allocates and initializes the shared WAL buffer cache.
- [`PGSharedMemoryCreate()`](/data/dbcomm/postgres-citus/src/include/storage/pg_shmem.h:87) and [`InitShmemAccess()`](/data/dbcomm/postgres-citus/src/backend/storage/ipc/shmem.c:93) - show that PostgreSQL's main shared memory is a real shared segment, not backend-private memory.
- [`CopyXLogRecordToWAL()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1227) - copies the record bytes into shared WAL pages.
- [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778) - local write/fsync path and group-commit piggyback.
- [`XLogWrite()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2297) - writes shared WAL pages to local `pg_wal` files and fsyncs.
- [`RecordTransactionCommit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1304) - commit sequence that flushes locally first and only then waits for sync replication.
- [`SyncRepWaitForLSN()`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:148) - wait queue keyed by LSN threshold rather than by per-commit message.
- [`WalSndLoop()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2786) and [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3100) - physical replication send loop.
- [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478) - current locally fsync'd WAL frontier used as the physical send limit.
- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1750) - sender reads fresh WAL from shared buffers before falling back to WAL files.
- [`GetXLogBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1618) and [`AdvanceXLInsertBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1980) - the key lifetime/recycle constraints for direct readers of shared WAL pages.
- [`pq_putmessage_noblock()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1511) and [`internal_putbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1273) - the current libpq staging path that adds another send-side copy around the WAL payload.
- [`XLogWalRcvProcessMsg()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:839), [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910), and [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993) - standby receive/write/flush path.
- [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131), [`XLogReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogreader.c:389), and [`GetRmgr(record->xl_rmid).rm_redo(...)`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:1991) - later replay path that actually interprets WAL records.
- [`doc/src/sgml/protocol.sgml:2332`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2332) - `XLogData` replication message format.
- [`doc/src/sgml/protocol.sgml:2446`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2446) - standby status update (`write` / `flush` / `apply`) message format.

## Current behavior / grounded findings

### `synchronous_commit` changes waiting, not whether streaming exists

Physical replication starts from a replication connection and `walsender`, not from `synchronous_commit`.

- [`RecordTransactionCommit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1304) calls [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1481) and only later [`SyncRepWaitForLSN()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1536).
- [`SyncRepRequested()`](/data/dbcomm/postgres-citus/src/include/replication/syncrep.h:18) gates whether commit waits at all.
- [`SYNCHRONOUS_COMMIT_ON`](/data/dbcomm/postgres-citus/src/include/access/xact.h:80) is just an alias for `SYNCHRONOUS_COMMIT_REMOTE_FLUSH`, not a separate mode.
- [`syncrep.c:1125`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:1125), [`syncrep.c:1128`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:1128), and [`syncrep.c:1131`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:1131) map `remote_write`, `remote_flush/on`, and `remote_apply` to the standby's cumulative `write`, `flush`, and `apply` frontiers.

### WAL is assembled privately but inserted into shared WAL buffers

Backend-private memory is only the construction scratch space.

- [`xloginsert.c`](/data/dbcomm/postgres-citus/src/backend/access/transam/xloginsert.c:1) explicitly describes WAL record construction in private working memory.
- [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:790) documents the two-step process:
  1. reserve space from shared `Insert->CurrBytePos`
  2. copy the record to the shared WAL buffer cache
- [`CopyXLogRecordToWAL()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1227) performs that copy into shared pages obtained by [`GetXLogBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1634).

So the authoritative pre-flush WAL object is already a standardized byte stream in shared memory, not a backend-local tuple or command object.

### Group commit works because the WAL durability frontier is shared

Group commit is not about merging backend-private buffers. It is about one backend flushing a shared WAL frontier for many committers.

- [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2809) explicitly tries to piggyback as much WAL as possible onto each fsync.
- [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2831) expands the flush request to the shared `XLogCtl->LogwrtRqst.Write`.
- [`WaitXLogInsertionsToFinish()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1510) waits until older insertions have fully copied their bytes into the shared WAL pages.
- [`LWLockAcquireOrWait(WALWriteLock, ...)`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2848) lets one backend become the flusher while others wait and recheck.
- [`CommitDelay`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2865) can widen the group-commit window, but by default that sleep is off.

The piggyback window is therefore usually narrow under low concurrency, but it is wider than just “between lock acquisition and fsync”: it includes time spent following another flusher, waiting for insertions to finish, and any configured `CommitDelay`.

### Physical `walsender` is a separate process, but it usually reads fresh WAL from shared buffers

- [`walsender.c`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:5) describes `walsender` as a separate process, with one process per replication connection.
- [`WalSndLoop()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2786) is the process main loop.
- [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3204) chooses `GetFlushRecPtr(NULL)` as the send limit on a primary.
- [`walsender.c:3193`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3193) explicitly says sending beyond the locally durable frontier would be unsafe if the primary later crashed and lost that WAL.
- [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3325) reads from shared WAL buffers first with [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1750).
- [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3333) falls back to WAL file reads only for bytes no longer present in the shared buffer cache.

So “flush before send” is a correctness gate on the send frontier, not necessarily an extra disk-read requirement.

### Primary shared WAL buffers are a plausible direct source for offloaded transport, but only within a tight lifetime window

- [`XLOGShmemInit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:4873) allocates `XLogCtl->pages` once in shared memory.
- [`PGSharedMemoryCreate()`](/data/dbcomm/postgres-citus/src/include/storage/pg_shmem.h:87), [`DEFAULT_SHARED_MEMORY_TYPE`](/data/dbcomm/postgres-citus/src/include/storage/pg_shmem.h:76), and [`InitShmemAccess()`](/data/dbcomm/postgres-citus/src/backend/storage/ipc/shmem.c:93) show that PostgreSQL's main shared memory is a real shared segment, so a same-host external service can in principle map/register it.
- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1750) already proves that a separate sender path can read the raw WAL payload directly from those shared pages.
- But [`GetXLogBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1618) explicitly warns that older buffers may be recycled already, and that callers must ensure the target page is not evicted.
- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1779) also documents that once a page is evicted it never returns to the WAL buffers.
- [`AdvanceXLInsertBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1980) is the recycling path that reuses buffer slots after old pages have been written out.

So a transport offload that RDMA-reads directly from `XLogCtl->pages` can remove a sender-side copy, but it must preserve the current lifetime rules:

- only read a contiguous WAL range that has already been fully inserted
- do not publish before the local durable frontier
- do not let the service lag beyond the WAL-buffer recycle window unless it can fall back to `pg_wal`
- do not let the NIC/service keep referencing a WAL-buffer page after PostgreSQL is free to recycle it; direct visibility alone is not a lifetime guarantee

So "make shared WAL buffers RDMA-visible" is plausible, but it is not automatically a permanent zero-copy publication API. The hard part is not access to the memory; it is preserving page ownership and recycle safety while the transport is still using those bytes.

### PostgreSQL's current shared-buffer send path is safe without page pinning because it only needs a bounded copy window

The current PostgreSQL path does **not** solve this with a long-lived page pin. It solves a narrower problem: "was this page stable for the duration of one local memcpy?"

- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1774) explicitly documents the rule: verify the page mapping, copy, then verify the mapping again.
- The first verification reads the expected page-end pointer from [`XLogCtl->xlblocks`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1805).
- [`pg_read_barrier()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1821) prevents reordering before the copy, [`memcpy()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1824) copies into the caller-owned destination buffer, and another [`pg_read_barrier()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1830) prevents reordering after it.
- The second verification at [`xlog.c:1840`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1840) checks that the same page mapping still holds. If not, the function stops and returns only the bytes copied successfully so far.
- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1779) relies on the invariant that once a page is evicted, it never returns to the WAL buffers under the same identity.

That is enough for the existing copied send path because the source page only has to stay valid until the local memcpy finishes. After that, the bytes are already in `output_message`, and PostgreSQL no longer depends on the WAL page remaining stable.

This is exactly why the current rule does **not** automatically extend to a borrowed RDMA send from shared WAL pages:

- a copied send needs page stability only for the duration of one local copy
- a borrowed RDMA send needs page stability until the final local completion checkpoint says the NIC is done reading that page

So PostgreSQL already has a correctness rule for copied extraction from recycled WAL pages, but it does **not** yet have the ownership/completion rule that a true borrowed zero-copy publish path would require.

### The current sender path is still raw-WAL transport, but not a zero-copy socket path

The physical replication payload is already the WAL byte stream, but PostgreSQL still performs framing and staging around it.

- [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3308) through [`walsender.c:3315`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3315) build only the tiny `XLogData` envelope (`'w'`, `dataStart`, `walEnd`, `sendTime`).
- [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3325) then copies raw WAL bytes into `output_message`, first from shared WAL buffers and then, if needed, from `pg_wal`.
- [`pq_putmessage_noblock()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3382) wraps that payload as a `CopyData` message.
- [`socket_putmessage_noblock()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1518) may repalloc the send buffer, and [`internal_putbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1299) usually `memcpy`s the message into `PqSendBuffer`.

So a network offload does not need to invent a new WAL serialization format. The likely gain is bypassing libpq/socket staging and possibly one extra send-side payload copy, not changing the canonical WAL bytes themselves.

### The replication message format is a small envelope plus raw WAL bytes

Physical replication does not serialize tuples or commands onto the wire.

- [`protocol.sgml:2332`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2332) defines `XLogData` as `Byte1('w') + dataStart + walEnd + sendTime + raw WAL bytes`.
- [`walsender.c:3313`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3313), [`walsender.c:3314`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3314), and [`walsender.c:3315`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3315) build that fixed-width header.
- [`protocol.sgml:2380`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2380) notes that one WAL record is never split across two `XLogData` messages, though a large record plus continuation records can occupy several messages.
- [`MAX_SEND_SIZE`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:110) caps one payload batch at 128 KiB.

This is a byte-stream transport protocol. The standardized payload is the existing WAL format itself.

### Standby ACKs are cumulative LSN frontiers, not per-message ACKs

- [`protocol.sgml:2446`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2446) defines the standby status update as `Byte1('r') + write + flush + apply + replyTime + requestReply`.
- [`XLogWalRcvSendReply()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1100) serializes those three cumulative frontiers with [`walreceiver.c:1142`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1142), [`walreceiver.c:1143`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1143), and [`walreceiver.c:1144`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1144).
- [`walsender.c:2422`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2422), [`walsender.c:2423`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2423), and [`walsender.c:2424`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2424) parse them on the primary.
- [`SyncRepReleaseWaiters()`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:474) releases all commit waiters up to the acknowledged LSN frontier.

So one status update can release many waiting commits. This is LSN-threshold pipelining, not just packet-level batching.

### Walreceiver mostly persists raw WAL; replay later interprets it

Physical walreceiver does not reconstruct a tuple- or command-level object on receive.

- [`XLogWalRcvProcessMsg()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:839) parses the small replication envelope with [`initReadOnlyStringInfo()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:860) and [`pq_getmsgint64()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:863).
- [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) writes the raw WAL payload into standby `pg_wal` files with `pg_pwrite`.
- [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993) fsyncs and advances `WalRcv->flushedUpto`.
- [`replication/README:32`](/data/dbcomm/postgres-citus/src/backend/replication/README:32) describes walreceiver as writing/flushing WAL to disk and signaling the startup process.

Only later does recovery/replay interpret those bytes:

- [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131) obtains decoded WAL records for replay.
- [`XLogReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogreader.c:389) is the record reader/decoder.
- [`GetRmgr(record->xl_rmid).rm_redo(...)`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:1991) dispatches to the resource-manager-specific redo routine.

That split matters for offload design: receive/write/flush is a transport-plus-durability problem, while redo/apply is a PostgreSQL recovery semantics problem.

### Standby currently has shared control state, not a shared WAL-payload receive cache

- [`WalRcv->writtenUpto`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:974) and [`WalRcv->flushedUpto`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1007) are the important shared progress frontiers.
- [`GetWalRcvFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiverfuncs.c:331) is the recovery-side contract for the flushed WAL frontier.
- [`xlogrecovery.c:3339`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3339) and [`xlogrecovery.c:3398`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3398) show that recovery expects WAL to be available in the WAL file and reads it with `pg_pread()`.

There is no existing standby analogue of `XLogCtl->pages` that recovery consumes as a WAL-payload buffer. A network-only receive offload therefore needs a new shared-memory receive ring plus a PostgreSQL-side local writer stage before the existing recovery contract can be satisfied.

## Pitfalls, caveats, and hidden constraints

- A tempting but wrong shortcut is to think `synchronous_commit` means “commit after the replica ACKs, regardless of local state”. The actual order in [`RecordTransactionCommit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1304) is local flush first, then optional sync-rep wait.
- Another tempting shortcut is to think `walsender` must reread WAL from disk after fsync. In the hot path it often reads the same bytes directly from shared `XLogCtl->pages` via [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1750).
- Physical replication should not be modeled as a tuple-level or command-level protocol. The payload is already the canonical WAL byte stream.
- Receiver-side “deserialize and convert to a high-level object before replay” is not what the code does. Physical walreceiver only parses the small envelope; replay later reads and decodes WAL records from storage.
- `remote_write` avoids standby fsync cost, but it does not remove the primary's local flush requirement. [`SYNCHRONOUS_COMMIT_REMOTE_WRITE`](/data/dbcomm/postgres-citus/src/include/access/xact.h:72) still includes local flush.
- A network-only standby offload that only stages WAL into shared memory does not yet satisfy current `remote_write` semantics. The current write frontier is keyed to [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) and [`GetWalRcvWriteRecPtr()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiverfuncs.c:347), i.e. the WAL-file write, not shared-memory arrival.

## Implementation progress / prototype state

- **Status**: `not-started`
- **What exists in code now**: upstream PostgreSQL physical replication path only.
- **Feature gates / switches**: `synchronous_commit`, synchronous standby configuration, `wal_level`, `wal_receiver_status_interval`.
- **Assumptions / shortcuts / scaffolding**: none for the offload prototype yet.
- **Changes from the motivating design**: none yet.
- **Corrections from earlier assumptions / rejected approaches**:
  - earlier assumption: “stream from backend memory, maybe before local durability”
  - grounded correction: bytes become a shared WAL stream first, and physical send is intentionally capped at the local durable LSN frontier
- **Canonical future-direction note**: [`../../future-directions/postgres/replication/replication_transport_offload_abstraction.md`](../../future-directions/postgres/replication/replication_transport_offload_abstraction.md)

## Architecture / data flow

### Entry points

1. A backend constructs a WAL record and calls [`XLogInsert()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xloginsert.c:474).
2. [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750) reserves shared WAL space and copies the bytes into [`XLogCtl->pages`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:493).
3. Commit calls [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1481).
4. [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778) and [`XLogWrite()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2297) write/fsync local `pg_wal`.
5. [`WalSndLoop()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:2786) sends all WAL up to [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478).
6. Standby [`XLogWalRcvProcessMsg()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:839) receives the message, [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) writes it, and [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993) fsyncs if needed.
7. Standby replies with cumulative `write` / `flush` / `apply` LSNs via [`XLogWalRcvSendReply()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1100).
8. Replay later advances through [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131) and [`rm_redo`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:1991).

### Data structures

- Shared local WAL buffer cache: [`XLogCtlData`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:451), especially `XLogCtl->pages` at [xlog.c:493](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:493)
- Shared insert state: [`XLogCtlInsert`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:397)
- Shared walsender registry and sync-rep frontiers: [`WalSndCtlData`](/data/dbcomm/postgres-citus/src/include/replication/walsender_private.h:82)
- Shared walreceiver progress: `WalRcv->writtenUpto` / `WalRcv->flushedUpto` updated at [`walreceiver.c:969`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:969) and [`walreceiver.c:1007`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1007)

### State / lifecycle

- Backend-private WAL assembly dies after insertion.
- Shared WAL pages live until written and later recycled.
- Local `pg_wal` durability is the authoritative primary send frontier.
- Standby `writtenUpto` / `flushedUpto` are ingress durability frontiers.
- Standby `apply` is a later replay frontier, not an ingress transport frontier.

## Behavior details

### Happy path for one synchronous commit

1. Backend assembles the COMMIT WAL record privately.
2. [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750) copies it into shared WAL pages.
3. [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778) performs local write/fsync, possibly piggybacking other backends.
4. [`RecordTransactionCommit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1487) marks commit state locally.
5. [`SyncRepWaitForLSN()`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:148) waits until the chosen remote frontier passes the commit LSN.
6. `walsender` streams raw WAL bytes up to the local flush frontier.
7. `walreceiver` writes and maybe flushes them, then sends cumulative progress.
8. The primary releases every waiter at or below the acknowledged LSN.

### Error paths / edge cases

- If the standby is behind the sender's shared-buffer window, [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3333) falls back to WAL-file reads.
- If a receiver reply is absent or slow, waiters remain blocked in [`SyncRepWaitForLSN()`](/data/dbcomm/postgres-citus/src/backend/replication/syncrep.c:148).
- `remote_apply` requires recovery/replay to reach the commit record, not just receiver ingress.

### Concurrency / locking / ordering constraints

- WAL insertion uses `WALInsertLock`s; [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:790) documents that the copy step is mostly parallel.
- Local flush uses `WALWriteLock`; [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2848) allows follower backends to let one backend flush on behalf of many.
- Sync replication waiters are keyed by LSN, so one cumulative receiver reply can release many commits.

## Invariants and assumptions

- A primary physical sender must not stream beyond its locally durable WAL frontier.
- Standby status updates are cumulative position reports, not per-message ACKs.
- Walreceiver ingress does not define the replay/apply semantics; recovery does.
- The canonical transport payload for physical replication is the WAL byte stream already stored in shared WAL pages and later in `pg_wal`.
- A primary-side transport service may read directly from shared WAL buffers, but only while respecting the recycle window and durable-publication ordering.
- Direct access to PostgreSQL shared memory is an access mechanism, not a lifetime guarantee; safe no-copy send still needs backpressure, ownership tracking, or copied fallback when WAL pages may recycle too early.
- A standby-side network-only service still needs a PostgreSQL local writer to reach the existing write/flush recovery contract.

## Design/implementation mismatches to remember

- If future prototype work searches for a “tuple-like” or “command-like” replication object, that is the wrong level for physical replication. The standardized object already exists and is lower level: the WAL stream.
- If future prototype work tries to offload replay/apply together with transport, it is crossing from communication offload into PostgreSQL recovery semantics.
- If future prototype work tries to stream before local flush for latency reasons, it is changing the safety model, not just the transport implementation.

## Configuration / flags

- [`wal_level`](/data/dbcomm/postgres-citus/src/include/access/xlog.h:61)
- [`synchronous_commit`](/data/dbcomm/postgres-citus/src/include/access/xact.h:68)
- synchronous standby configuration checked through [`SyncRepRequested()`](/data/dbcomm/postgres-citus/src/include/replication/syncrep.h:18)
- `wal_receiver_status_interval` behavior described in [`replication/README:35`](/data/dbcomm/postgres-citus/src/backend/replication/README:35)

## Interactions and cross-links

- **Overlapping KB areas**: `docs/kb/postgres/serialization/` for higher-level row serialization; `docs/kb/citus/replication/` for the remaining Citus-side replication/2PC semantics.
- **Shared code / canonical docs**: this is the canonical factual note for physical WAL replication.
- **Implementation note / factual note / future-direction note**: the future offload design note is [`../../future-directions/postgres/replication/replication_transport_offload_abstraction.md`](../../future-directions/postgres/replication/replication_transport_offload_abstraction.md).

## Future directions / design ideas

- **Status**: `exploratory`
- **Goal**: treat physical replication as a durable WAL-chunk publication problem plus cumulative frontier reporting, rather than as tuple/command serialization.
- **Grounding in current code**: the durable shared WAL frontier in [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778), sender gating in [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3204), receiver write/flush in [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) / [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993), and later replay in [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131) motivate the split.
- **Candidate approaches**: see the canonical future-direction note.
- **Risks / tradeoffs**: local durability ordering and replay semantics are the main invariants to preserve.
- **Next experiments / implementation steps**: see the canonical future-direction note.

## Debugging notes / gotchas

- Symptom: standby has written WAL but sync commit still waits.
  - Likely cause: wait mode is `remote_flush`/`remote_apply`, so `write` is not enough.
- Symptom: thinking receiver “already deserialized” WAL on ingress.
  - Correction: receiver only parsed the replication envelope and persisted raw bytes; replay is later.
- Symptom: assuming sender must read from disk after fsync.
  - Correction: check [`walsender.c:3325`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3325) before [`walsender.c:3333`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3333).

## Open questions / TODO

- For prototype work, what is the cleanest way to publish “new durable WAL frontier” from PostgreSQL to the external service without perturbing the hot WAL path too much?
- If standby-side receive/write/flush is offloaded, what is the narrowest interface back into PostgreSQL recovery for advancing `flushedUpto` and waking replay?
