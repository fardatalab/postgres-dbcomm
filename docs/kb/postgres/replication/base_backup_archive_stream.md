# BASE_BACKUP archive stream

## Scope

- **What this doc explains**: how PostgreSQL `BASE_BACKUP` produces filesystem backup data, how that data is framed into the replication protocol, how `pg_basebackup` receives and processes it, and which invariants matter for an external communication service.
- **What this doc does NOT cover**: logical backup formats, `pg_dump`, restore/replay internals after the backup has been written, or implementation changes for the Homer prototype.
- **Primary directory**: `docs/kb/postgres/replication/`
- **Doc type**: `factual`

## Why this exists

The base-backup path looks superficially like other PostgreSQL streaming paths because it uses a replication connection and `CopyData`, but the semantic object is different from both tuple movement and physical WAL streaming.

The core correction is:

- tuple copy sends per-row COPY-format data
- physical WAL streaming sends an LSN-ordered WAL byte stream
- `BASE_BACKUP` sends an ordered set of backup archives plus an optional manifest, with small stream-control records around tar payload chunks

That makes the right offload boundary closer to a typed **backup archive stream** than to a `TupleView`.

## Key Code Pointers

- Replication command parse and dispatch:
  - [`base_backup`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/repl_gram.y:165) parses `BASE_BACKUP`.
  - [`exec_replication_command()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/walsender.c:1992) dispatches replication commands.
  - [`T_BaseBackupCmd` case](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/walsender.c:2120) calls [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988).
- Server-side backup producer:
  - [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988) builds the `bbsink` chain and starts the backup.
  - [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:234) starts backup mode, emits archives, stops backup mode, emits WAL if requested, then sends the manifest.
  - [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1187) recursively walks PGDATA or a tablespace directory.
  - [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1572) emits a tar member header, reads file bytes, pads, and adds a manifest entry.
  - [`basebackup_read_file()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2111) reads filesystem bytes with `pg_pread()`.
  - [`_tarWriteHeader()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2019) serializes a tar header into the sink buffer.
  - [`SendBackupManifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/backup_manifest.c:316) streams the generated manifest through the same sink API.
- Server-side sink and protocol framing:
  - [`bbsink`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:99) is the generic archive/manifest sink object.
  - [`bbsink_ops`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:118) defines backup, archive, manifest, and cleanup callbacks.
  - [`bbsink_copystream_new()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:108) creates the client protocol sink.
  - [`bbsink_copystream_begin_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:126) sends start-LSN and tablespace result sets, then starts one `COPY OUT`.
  - [`bbsink_copystream_begin_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:165) emits a typed `new archive` `CopyData`.
  - [`bbsink_copystream_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:183) sends archive payload chunks.
  - [`bbsink_copystream_begin_manifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:260) emits the `manifest begins` marker.
  - [`bbsink_copystream_manifest_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:273) sends manifest payload chunks.
  - [`SendCopyOutResponse()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:317) starts the COPY stream.
  - [`socket_putmessage()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/libpq/pqcomm.c:1488) wraps payload as a frontend/backend message.
  - [`internal_putbytes()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/libpq/pqcomm.c:1276) stages bytes into the socket send buffer or direct-sends large messages.
- Client-side receiver:
  - [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1753) builds and sends the `BASE_BACKUP` command, reads start/tablespace result sets, and receives archive data.
  - [`ReceiveCopyData()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1014) loops over `PQgetCopyData()`.
  - [`PQgetCopyData()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-exec.c:2834) is the frontend API used by `pg_basebackup`.
  - [`pqGetCopyData3()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1751) allocates and copies each `CopyData` payload out of libpq's input buffer.
  - [`ReceiveArchiveStream()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1284) receives the modern single-COPY stream.
  - [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1332) parses the per-`CopyData` type byte.
  - [`CreateBackupStreamer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1061) builds the frontend `bbstreamer` chain for writing, parsing, extracting, decompressing, or injecting files.
  - [`bbstreamer`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer.h:98) is the frontend archive-processing sink abstraction.
  - [`bbstreamer_tar_parser_content()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_tar.c:111) parses tar blocks into typed member chunks.
  - [`bbstreamer_extractor_content()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_file.c:203) creates directories/files/symlinks and writes member payloads.

## Current Behavior

### Command Entry

`BASE_BACKUP` is a replication command, not ordinary SQL.

The grammar production [`base_backup`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/repl_gram.y:165) creates a [`BaseBackupCmd`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/nodes/replnodes.h:41). [`exec_replication_command()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/walsender.c:1992) handles that command in its [`T_BaseBackupCmd` case](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/replication/walsender.c:2120), prevents use inside a transaction block, and calls [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988).

So the server side is a walsender/replication-command backend, but during backup it reads regular data-directory files and emits archive stream records.

### Source Of The Data

Most archive bytes come from the server filesystem, not from shared buffers or tuple slots.

[`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988) documents the key invariant: PostgreSQL enters backup mode so the backup is consistent even though it reads directly from the filesystem and bypasses the buffer cache. [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:267) calls `do_pg_backup_start()`, then emits archive members, and later calls [`do_pg_backup_stop()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:394).

The recursive scan happens in [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1187). Important filters and ordering there include:

- skip temp/special excluded names at [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1255)
- skip unlogged relation forks except init forks at [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1315)
- skip temporary relations at [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1337)
- represent `pg_wal` as an empty directory and do not recurse into it at [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1383)
- defer `global/pg_control` so it is emitted last from the base archive at [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:351)

For a regular file, [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1572) opens the path, writes a tar header with [`_tarWriteHeader()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2019), reads content with [`read_file_data_into_buffer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1847), and sends each filled sink buffer through [`bbsink_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:200). The actual file read is [`pg_pread()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2117) in [`basebackup_read_file()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2111).

### Chunking And Serialization Before Socket

The server has two serialization layers:

1. tar/archive serialization
2. replication-protocol `CopyData` framing with a base-backup-specific type byte

The archive chunk buffer size is [`SINK_BUFFER_LENGTH`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:60), currently `Max(32768, BLCKSZ)`. The sink API requires the buffer length to be a multiple of `BLCKSZ`, enforced by [`bbsink_begin_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:175).

Tar serialization happens in the backup producer:

- [`_tarWriteHeader()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2019) writes a 512-byte tar header into `sink->bbs_buffer` and immediately calls `bbsink_archive_contents()`.
- [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1680) reads file payload chunks into `sink->bbs_buffer` and calls `bbsink_archive_contents()`.
- [`_tarWritePadding()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2071) appends tar block padding.
- [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:382) writes two zero tar blocks to terminate each archive.

COPY framing happens in [`basebackup_copy.c`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:1). [`bbsink_copystream_begin_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:132) allocates one buffer with enough leading room for a type byte, then points `base.bbs_buffer` at the aligned payload area. It sets [`msgbuffer[0] = 'd'`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:146), so archive data can be sent with one [`pq_putmessage()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:194) call and without an extra copy from the sink buffer into a separate message buffer.

Modern PostgreSQL uses one `COPY OUT` stream for all archives and the manifest. Each `CopyData` payload begins with one base-backup type byte:

- `'n'`: new archive, emitted by [`bbsink_copystream_begin_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:165)
- `'d'`: archive or manifest bytes, emitted by [`bbsink_copystream_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:183) and [`bbsink_copystream_manifest_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:273)
- `'p'`: progress, emitted in [`bbsink_copystream_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:223) and forced at [`bbsink_copystream_end_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:241)
- `'m'`: manifest begins, emitted by [`bbsink_copystream_begin_manifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:260)

The protocol documentation describes the same message families at [`protocol.sgml`](/data/dbcomm/postgres-citus-separate-comm-stack/doc/src/sgml/protocol.sgml:3017).

After `pq_putmessage()`, [`socket_putmessage()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/libpq/pqcomm.c:1488) adds the frontend/backend message type and 4-byte message length. [`internal_putbytes()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/libpq/pqcomm.c:1276) then either copies bytes into `PqSendBuffer` or direct-sends when the send buffer is empty and the data length is at least the current send-buffer size.

### Sink Chain Semantics

[`bbsink`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:99) is already the server-side abstraction point.

[`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1032) starts with a `copystream` sink. If the backup target is not the client, [`BaseBackupGetSink()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1034) wraps the client sink with a target sink. Then throttling, compression, and progress sinks can wrap the chain at [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1036).

The practical order means the producer calls the outermost sink, and each sink may transform or forward to `bbs_next`. This is the key offload hook: a service-backed sink can be inserted without making `sendDir()` or `sendFile()` understand RDMA.

### WAL Inclusion Is A Separate Subcase

Base backup has two WAL modes that should not be conflated.

If `pg_basebackup -X fetch` requests WAL inside the backup, [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:406) keeps the base tar open, scans `pg_wal`, validates the required segment range, and reads WAL files from disk into the same archive sink at [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:521).

If `pg_basebackup -X stream` is used, [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2100) starts a separate background WAL receiver with [`StartLogStreamer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2119). That path uses physical WAL streaming, not the base-backup archive stream.

So for offload design, `FETCH_WAL` belongs to the backup archive stream; `STREAM_WAL` belongs to the WAL replication stream described in [physical_wal_streaming_and_synchronous_commit.md](physical_wal_streaming_and_synchronous_commit.md).

### Client Receive And Processing

[`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2001) builds the command, sends it via [`PQsendQuery()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2007), then receives:

1. start LSN result set at [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2014)
2. tablespace list result set at [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2045)
3. one archive/manifest `COPY OUT` stream via [`ReceiveArchiveStream()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2124)
4. final end-LSN result set at [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2187)
5. final command status at [`BaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:2198)

[`ReceiveCopyData()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1014) gets `PGRES_COPY_OUT`, then repeatedly calls [`PQgetCopyData()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1032). In libpq, [`pqGetCopyData3()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1751) waits for a full `CopyData` message, allocates a new buffer with [`malloc()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1784), copies from `conn->inBuffer` with [`memcpy()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1790), and returns that buffer to the caller.

[`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1332) parses the base-backup type byte:

- `'n'` closes the prior archive, parses archive name and tablespace location, and builds a new `bbstreamer` chain at [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1383).
- `'d'` strips the type byte and forwards payload to either manifest buffering/writing or the active archive streamer at [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1412).
- `'p'` parses the 64-bit progress count at [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1448).
- `'m'` switches subsequent `'d'` chunks to manifest handling at [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1467).

The frontend `bbstreamer` abstraction mirrors the server `bbsink` idea but is archive-consumer oriented. [`CreateBackupStreamer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1061) chooses a chain for plain extraction, tar-file writing, client compression, decompression, manifest injection, and recovery config injection. If the stream must be parsed, [`bbstreamer_tar_parser_content()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_tar.c:111) turns raw tar bytes into typed member chunks. The plain-format extractor [`bbstreamer_extractor_content()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_file.c:203) creates directories, symlinks, and regular files, and writes file payload with [`fwrite()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_file.c:253).

## Invariants And Caveats

- Base backup consistency comes from backup mode plus WAL, not from locking every data file while it is copied.
- The producer intentionally reads files directly from the filesystem. A service design that wants zero-copy from the buffer cache is not matching the current base-backup source object.
- `backup_label` is emitted first, and `global/pg_control` is emitted last for the base archive; these ordering choices in [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:331) and [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:351) are semantic, not cosmetic.
- Concurrent file extension is ignored after the size captured by `stat`; [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1674) relies on WAL replay to restore later changes.
- Concurrent truncation is tolerated by padding zeros at [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1786).
- Checksum verification is best-effort around concurrent page writes. [`read_file_data_into_buffer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1877) explicitly retries once for possible torn reads but does not have a page-write interlock.
- Manifest payload is generated into a temporary `BufFile` first by [`InitializeBackupManifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/backup_manifest.c:55), then read back into `sink->bbs_buffer` by [`SendBackupManifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/backup_manifest.c:362).
- Modern base backup uses one COPY stream for all archives and the manifest. Older per-archive COPY behavior remains visible in client compatibility code, but [`basebackup_copy.c`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:16) says the older method is no longer supported by the server.

## Relationship To Existing Prototype Abstractions

The closest current PostgreSQL abstraction to reuse is `bbsink`, not `TupleView`.

Important semantic mapping:

- `RemoteExecutionSession`: one remote backup operation/session on a replication connection or service-owned equivalent
- typed DB semantic objects: archive begin, archive bytes, archive end, manifest begin, manifest bytes, backup end
- sink: `bbsink` on the producer side and `bbstreamer` on the receiver side
- transport payload: mostly opaque archive byte ranges, often tar bytes, possibly compressed depending on sink placement

Progress should be treated as local/session status rather than a required
payload object in the service design. In upstream PostgreSQL, the progress sink
also maintains local `bbsink_state` fields, notably `bytes_done` in
[`bbsink_progress_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:150)
and `tablespace_num` in
[`bbsink_progress_end_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:114).
For the `TARGET 'homer'` research path, the specialized Homer sink can fold in
only those required state updates and skip or patch the UI-oriented progress
wrapper so no-copy registered-slot rotation is not constrained by the generic
forwarding sink chain. The external service can infer byte counters from archive
chunks and optionally publish sparse status through the command/completion
mailbox.

This motivates the separate future-direction note [base_backup_service_abstraction.md](../../future-directions/postgres/replication/base_backup_service_abstraction.md).

## Open Questions And Resolved Prototype Choices

Resolved for the first `TARGET 'homer'` prototype:

- Hook at the server-side `bbsink` layer through an explicit backup target, not
  through the frontend `pg_basebackup` receiver.
- Use tar archive bytes, no server-side compression, no throttling, and no WAL.
- Use a Homer typed descriptor ring for archive/manifest/control objects rather
  than preserving PostgreSQL's current one-byte `CopyData` mini-protocol.
- Use rotating RDMA-registered payload slots as the active `bbsink` buffer so
  archive bytes do not need a second CPU copy into a transport staging buffer.
- Start with a blackhole/counting receiver that exercises the same slot
  lifecycle; archive materialization and extraction are later receiver sinks.
- Implement manifest callbacks in the same typed stream family; the manifest is
  not worth disabling because [`SendBackupManifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/backup_manifest.c:316)
  already emits through the same `bbsink` interface.
- Generalize the currently tuple-named RDMA payload-ring substrate if practical,
  or add temporary generic wrappers over the existing symbols. Do not create a
  base-backup-only RDMA data plane.

Still open before or during implementation:

- Exact control-plane handshake fields in `RemoteExecutionOperationSpec` for a
  base-backup operation.
- Whether the first sender path blocks in `archive_contents()` until a free slot
  exists or starts with a small multi-slot pipeline.
- Exact fixed wire structs for the base-backup semantic descriptors, separate
  from the now-resolved decision that their payload transport should use the
  shared/generic RDMA payload channel.

## Related

- [physical_wal_streaming_and_synchronous_commit.md](physical_wal_streaming_and_synchronous_commit.md): physical WAL streaming path used by `-X stream` and by normal replication.
- [../../future-directions/postgres/replication/base_backup_service_abstraction.md](../../future-directions/postgres/replication/base_backup_service_abstraction.md): proposed service/RDMA abstraction for base-backup archive streams.
- [../serialization/datarow_vs_copy_and_citus_transport.md](../serialization/datarow_vs_copy_and_citus_transport.md): contrast with tuple/COPY row movement.
