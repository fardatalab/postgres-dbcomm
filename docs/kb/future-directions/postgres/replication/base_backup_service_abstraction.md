# Base backup service abstraction

## Scope

- **What this doc explains**: high-level DB-semantic abstraction options for offloading PostgreSQL `BASE_BACKUP` archive transfer through the existing remote execution session, typed object, sink, and RDMA transport framework.
- **What this doc does NOT cover**: immediate implementation patches, replacing PostgreSQL backup consistency logic, backup restore/replay redesign, or logical backup formats.
- **Primary directory**: `docs/kb/future-directions/postgres/replication/`
- **Doc type**: `future-direction`

## Grounding

Current behavior is documented in [base_backup_archive_stream.md](../../../postgres/replication/base_backup_archive_stream.md).

The important current-code facts are:

- [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988) builds a server-side `bbsink` chain.
- [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:234) owns backup mode, archive order, backup stop, optional fetched WAL, manifest send, and final end LSN.
- [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1572) reads filesystem bytes into `sink->bbs_buffer` and calls [`bbsink_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:200).
- [`bbsink_copystream_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:183) wraps archive bytes as `CopyData` payloads with a base-backup type byte.
- [`ReceiveArchiveStreamChunk()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1332) parses typed base-backup `CopyData` payloads.
- [`bbstreamer`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer.h:98) is the frontend archive-processing sink chain.

Existing prototype vocabulary this design should reuse:

- [`RemoteExecutionSession`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:179) as the backend-visible operation/session object.
- [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_session.c:2289) as the current tuple-sink session open point.
- [`RemoteExecutionSessionIntentSpec`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:162) and [`RemoteExecutionOperationSpec`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:146) as the split between compatibility/session intent and exact operation.
- [`TupleSinkServicePostPeerInlineBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2965), [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3055), and [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3129) as the already-landed control/payload/doorbell pattern.

## First-Principles Semantic Object

The base-backup workload is a physical snapshot export, not tuple exchange and not continuous WAL replication.

The semantic object should be:

```c
BaseBackupArchiveStream
```

owned under a:

```c
RemoteExecutionSession(opKind = REMOTE_EXEC_OP_BASE_BACKUP)
```

The stream is ordered and mostly append-only. It contains:

- backup start metadata: start LSN, start timeline, tablespace list, estimated sizes
- one or more archive streams: base data directory plus each tablespace
- optional manifest stream
- backup end metadata: end LSN and end timeline

Progress is intentionally not part of the core data-plane object model. The
service can infer bytes-sent / bytes-received counters from archive chunk
descriptors, and if we want observability it can publish sparse status through
the existing command/completion mailbox. We do not need compatibility with
`pg_basebackup`-style UI progress messages for this research prototype.

This is intentionally not a `TupleView`. The producer is not deforming rows, and the receiver is not rebuilding tuples. The producer is walking a filesystem snapshot window and serializing it as archive bytes whose consistency depends on backup-mode/WAL rules.

## Proposed Typed Objects

Use explicit fixed-width descriptors for control and metadata, plus payload slots for bytes.

Candidate object family:

```c
typedef enum RemoteBaseBackupObjectKind
{
    REMOTE_BASEBACKUP_OBJECT_BEGIN,
    REMOTE_BASEBACKUP_OBJECT_TABLESPACE_LIST,
    REMOTE_BASEBACKUP_OBJECT_ARCHIVE_BEGIN,
    REMOTE_BASEBACKUP_OBJECT_ARCHIVE_CHUNK,
    REMOTE_BASEBACKUP_OBJECT_ARCHIVE_END,
    REMOTE_BASEBACKUP_OBJECT_MANIFEST_BEGIN,
    REMOTE_BASEBACKUP_OBJECT_MANIFEST_CHUNK,
    REMOTE_BASEBACKUP_OBJECT_MANIFEST_END,
    REMOTE_BASEBACKUP_OBJECT_END,
    REMOTE_BASEBACKUP_OBJECT_ERROR
} RemoteBaseBackupObjectKind;
```

Important payload-bearing objects:

```c
typedef struct RemoteBaseBackupArchiveBegin
{
    uint32 archive_index;
    uint32 tablespace_oid;      /* InvalidOid for PGDATA. */
    uint64 estimated_bytes;     /* 0/invalid if estimate disabled. */
    char archive_name[MAXPGPATH];
    char tablespace_path[MAXPGPATH];
} RemoteBaseBackupArchiveBegin;

typedef struct RemoteBaseBackupChunk
{
    uint32 archive_index;
    uint64 stream_offset;
    uint32 payload_len;
    uint32 flags;               /* compressed, tar, final-fragment, etc. */
} RemoteBaseBackupChunk;
```

The descriptor should not embed large payload bytes. It should identify the payload slot/range owned by the send ring or service staging buffer.

For the first prototype, `RemoteBaseBackupChunk` can mean "bytes after the selected PostgreSQL sink transforms." If the hook is below compression, payload bytes may already be compressed. If the hook is above compression, the service must own compression or forward through another sink first. That placement must be explicit in the operation spec.

### Progress handling

Progress should stay out of the hot data-plane object family.

For the `TARGET 'homer'` research path, PostgreSQL does not need to preserve
the upstream progress sink chain as-is. [`bbsink_progress_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:150)
updates `bbsink_state.bytes_done`, and
[`bbsink_progress_end_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:114)
advances `bbsink_state.tablespace_num`. Those state transitions matter because
[`bbsink_end_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:255)
asserts that every tablespace archive has completed, but the UI-oriented
`pg_stat_progress_basebackup` updates are not research-interesting.

The specialized Homer sink should therefore fold in the minimal state updates
needed by the normal tar/no-compression/no-throttle path and skip or patch the
generic progress wrapper when it interferes with no-copy buffer rotation. This
is an intentional prototype constraint, not a general PostgreSQL extension
compatibility goal.

In our framework, progress is derived state:

- sender service increments counters from each `REMOTE_BASEBACKUP_OBJECT_ARCHIVE_CHUNK.payload_len`
- receiver service increments received/written counters as it consumes and materializes those chunks
- archive-level summaries can be emitted when `REMOTE_BASEBACKUP_OBJECT_ARCHIVE_END` is observed

If the coordinator/backend needs status, send an optional, infrequent
`RemoteBaseBackupProgress` status record through the existing command mailbox or
completion path. This record should be control-plane/debug metadata, not a
backpressure or payload-ordering primitive:

```c
typedef struct RemoteBaseBackupProgress
{
    uint32 archive_index;
    uint64 archive_bytes_sent;
    uint64 total_bytes_sent;
    uint64 archive_bytes_written;
    uint64 total_bytes_written;
} RemoteBaseBackupProgress;
```

This keeps the research focus on the semantic payload object, the
`BaseBackupArchiveStream`, while preserving enough observability to debug slow
or stuck backups.

## Session And Operation Specs

Extend the session vocabulary with a new operation kind rather than overloading tuple-sink mode:

```c
typedef enum RemoteExecOpKind
{
    ...
    REMOTE_EXEC_OP_BASE_BACKUP
} RemoteExecOpKind;
```

Suggested intent fields:

- source node identity
- destination node/service identity
- replication/session identity if a real PostgreSQL replication connection is still involved
- access kind: physical backup/export
- transaction policy: outside normal SQL transaction
- consistency policy: PostgreSQL-owned backup mode

Suggested operation fields:

- target mode: client stream, remote service writes files, remote service writes tar archives, server-side target equivalent
- output format: tar archive stream vs extracted files
- compression placement: PostgreSQL server sink, service, receiver, or none
- manifest policy
- WAL policy: none, fetch-in-archive, separate stream
- tablespace mapping policy
- buffer/slot sizing hints
- optional sparse progress reporting interval for command-mailbox status, if enabled

Important correction: `REMOTE_EXEC_OP_BASE_BACKUP` should not be modeled as a long-lived replication stream like physical WAL. It has a clear begin/end, ordered archives, and a manifest boundary.

## Recommended First Prototype

### Approach 1: Specialized service-backed `bbsink` after PostgreSQL archive production

Add a new `bbsink` implementation that receives the existing server-side callbacks:

- `begin_backup`
- `begin_archive`
- `archive_contents`
- `end_archive`
- `begin_manifest`
- `manifest_contents`
- `end_manifest`
- `end_backup`

It converts those callbacks into the typed objects above and publishes them to
the local service. For the research prototype, this should be selected
explicitly with `TARGET 'homer'` and it may use a specialized sink-chain shape
rather than preserving every upstream `bbsink` wrapper. The objective is the
correct base-backup lifecycle and high-performance external-service data path,
not compatibility with every server-side base-backup option.

Pros:

- minimal disturbance to [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:234), [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1187), and [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1572)
- preserves PostgreSQL's backup-mode, archive ordering, tar, checksum, manifest, and WAL-fetch behavior
- matches the already-established sink abstraction
- easy to instrument and compare against [`bbsink_copystream_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_copy.c:183)
- can avoid the hot-path CPU copy by exposing registered RDMA payload slots as the active `bbsink` buffer

Cons:

- still reads filesystem bytes into memory before RDMA publication; the no-copy goal here means no extra CPU copy from a PostgreSQL buffer into a separate RDMA staging buffer
- inherited chunk size is only [`SINK_BUFFER_LENGTH`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:60), usually 32 KiB, unless the Homer sink overrides the active buffer length with a larger BLCKSZ-aligned registered slot
- if hooked below compression, the service sees transformed archive bytes rather than file-level semantics

This is the best first prototype because it preserves the important backup
semantics while allowing the prototype to patch or bypass unimportant upstream
sink-chain assumptions for `TARGET 'homer'`.

#### First coding decisions

The first implementation pass should treat these choices as settled prototype
constraints:

1. **Progress wrapper**: skip or patch the upstream progress wrapper for
   `TARGET 'homer'`. The Homer sink should fold in the minimal local
   `bbsink_state` updates done by
   [`bbsink_progress_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:150)
   and [`bbsink_progress_end_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_progress.c:114),
   but should not preserve UI-oriented progress as a data-plane concern.
2. **Manifest**: implement `begin_manifest`, `manifest_contents`, and
   `end_manifest` in the same typed stream family rather than forcing manifests
   off. [`SendBackupManifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/backup_manifest.c:316)
   already uses the `bbsink` API, so the no-copy slot discipline should apply
   to manifest chunks as well as archive chunks.
3. **RDMA substrate naming**: generalize the current tuple-named payload-ring
   protocol and service helpers when practical. Base backup should not introduce
   a second RDMA data plane, but the common substrate should also not remain
   semantically named after tuple sinks once it carries non-tuple DB objects.
4. **Slot lifetime**: enforce the no-copy slot lifecycle as a hard invariant.
   `archive_contents(len)` and `manifest_contents(len)` must not return with
   `sink->bbs_buffer` pointing at a slot that is still published or otherwise
   unsafe to overwrite.
5. **Blackhole receiver**: the first receiver can blackhole/count bytes, but it
   must still exercise the real service/RDMA publication and reclamation path.
   A backend-local byte counter alone would not validate the main research
   mechanism.

#### No-copy registered-slot variant

The preferred `TARGET 'homer'` data path is a refined rotating-slot design:

1. During `begin_backup`, the Homer sink maps or allocates a fixed ring of
   local RDMA-registered payload slots using the same service-to-service payload
   substrate as the tuple path.
2. The sink sets `sink->bbs_buffer` to the current free slot and sets
   `sink->bbs_buffer_length` to that slot's BLCKSZ-aligned payload capacity.
3. PostgreSQL writes tar bytes directly into the current registered slot. This
   covers file data read by [`read_file_data_into_buffer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1847)
   and tar headers emitted by [`_tarWriteHeader()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:2019).
4. `archive_contents(len)` publishes the current slot as a
   `REMOTE_BASEBACKUP_OBJECT_ARCHIVE_CHUNK` descriptor and then rotates
   `sink->bbs_buffer` to another free registered slot before returning.
5. If no free slot exists, `archive_contents(len)` blocks until the service or
   remote side marks a slot reusable.

The slot lifetime is:

```text
FREE -> WRITING -> PUBLISHED -> FREE
```

`sink->bbs_buffer` must only point at a `WRITING` slot. A published slot must
not be reused until RDMA visibility/completion and receiver consumption rules
make it free again.

This intentionally bends the upstream `bbsink` expectation that `bbs_buffer`
generally does not change after [`bbsink_begin_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:175).
That is acceptable for this research path if the Homer target owns the
outermost buffer-visible sink or the intervening wrappers are patched to refresh
their buffer pointers after each publish. The normal path of interest is tar,
no compression, no throttling, no `STREAM_WAL`.

### Approach 2: File-extent service below tar generation

Move the abstraction lower: emit typed file objects before tar serialization.

Candidate objects:

- file begin: path, mode, uid/gid, mtime, tablespace OID, expected size
- file extent: offset, payload
- file end
- directory/symlink records

Pros:

- more semantic than opaque tar bytes
- receiver can write extracted files directly without tar parse/rearchive cost
- better long-term path to larger file extents and fewer tiny tar-header/padding messages

Cons:

- much larger change to [`sendDir()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1187), [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1572), manifest generation, and frontend restore compatibility
- must preserve tar output mode separately for `pg_basebackup -Ft`
- easy to accidentally change backup-label, tablespace-map, `pg_control`, WAL-fetch, or concurrent truncation semantics

This is plausible later, but it is the wrong first offload target.

### Approach 3: Replace `pg_basebackup` receiver with service-owned remote backup target

Keep PostgreSQL's producer mostly intact, but have the destination service consume typed backup objects and materialize files or archives without libpq/frontend `pg_basebackup`.

Pros:

- best match for Citus node-to-node cloning or internal physical copy workflows
- receiver can use preallocated file-writing buffers and direct service-to-filesystem scheduling
- avoids libpq's [`pqGetCopyData3()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1751) malloc/copy loop

Cons:

- not a drop-in replacement for user-facing `pg_basebackup`
- needs receiver-side failure, fsync, cleanup, tablespace mapping, and partial-backup removal policy
- needs careful definition of "remote node" because current `pg_basebackup` normally receives into a frontend process, not another PostgreSQL backend

This is attractive for the research system if the target workload is physical clone/rebuild between nodes.

## RDMA Data-Path Mapping

Use the same control/payload/publish split as the tuple path, but with backup
stream descriptors instead of tuple-view contracts.

The current implementation vocabulary is still tuple-oriented in several
places, for example the payload-ring descriptor and RDMA helpers are anchored in
the tuple sink subsystem:

- [`CitusTupleSinkPayloadRingDescriptor`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:204)
- [`CitusTupleSinkPayloadRingControl`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:163)
- [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:300)
- [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3055)

For the base-backup implementation, prefer renaming or factoring these into a
generic remote payload channel, e.g. `RemotePayloadRingDescriptor`,
`RemotePayloadRingControl`, `RemotePayloadTransportHeader`, and
`RemotePayloadPostPeerRegisteredBytesUnsignaledRdma()`, if the rename can be
done without derailing the prototype. If a full rename is too invasive for the
first patch, add a narrow compatibility layer with generic names and keep the
old tuple-named symbols as wrappers temporarily. Do not add a separate
base-backup-only RDMA descriptor or a second RDMA publication path unless the
shared substrate proves impossible to generalize.

Suggested send-side path for Approach 1:

1. PostgreSQL backend enters a service-backed `bbsink_archive_contents()` callback.
2. The active `sink->bbs_buffer` is already a local registered payload slot.
3. The sink publishes a descriptor for the current slot/range; it does not copy
   bytes into another staging buffer.
4. The local service posts the payload with [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3055).
5. The local service publishes the descriptor/tail with [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3129).
6. The Homer sink rotates to another free registered slot before returning to PostgreSQL.
7. The remote service consumes objects in stream order and writes/extracts bytes through a receiver sink.

The hot-path performance issue is chunk granularity. A 32 KiB source chunk is reasonable for socket COPY but may be too small for RDMA efficiency. Three options:

- increase `SINK_BUFFER_LENGTH` for the service-backed sink if the producer and checksum assumptions still hold
- use the service sink's `bbs_buffer` as a larger preallocated registered slot, while keeping the length a multiple of `BLCKSZ`
- coalesce multiple PostgreSQL chunks into larger service payloads before remote publication

The second option is the cleanest first experiment: it keeps the producer
writing into the sink-provided buffer but lets the new sink choose a bigger
registered buffer. Coalescing is deferred because it requires maintaining
sub-slot offsets and ensuring the next PostgreSQL write does not overwrite an
unpublished prefix.

## Receiver-Side Model

The receiver should also be a sink chain, not a tuple consumer.

For compatibility with the current frontend model:

- archive bytes can feed a `bbstreamer`-equivalent chain
- tar parsing remains optional, just as [`CreateBackupStreamer()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/pg_basebackup.c:1061) chooses whether to parse
- direct file materialization should be modeled as an extractor sink, analogous to [`bbstreamer_extractor_content()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/bin/pg_basebackup/bbstreamer_file.c:203)

For a service-to-service physical clone:

- the remote service can materialize archives or extracted files
- the remote PostgreSQL backend does not need to parse every chunk
- control completion should report durable materialization progress, not just network arrival

The completion plane should include at least:

- archive completed
- manifest completed
- fsync/durable completion if the target semantics require it
- terminal error with enough context to clean up a partial backup

Byte counters are useful, but they should be maintained locally from chunk
descriptors and surfaced only as sparse status if requested. They should not
force a progress object into the payload stream.

For the first prototype, the receiver should be a blackhole/counting sink only
after the real remote payload channel has delivered the object. In other words,
the receiver may discard archive and manifest bytes, but it must still consume
typed descriptors, observe ordered boundaries, update byte counters, and release
payload slots back through the same lifecycle that a later archive-writer or
extractor sink would use.

## Performance Notes

- Avoid per-chunk allocation. The current frontend path allocates per `CopyData` in [`pqGetCopyData3()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/interfaces/libpq/fe-protocol3.c:1784); the service path should use preallocated rings.
- Avoid putting mutexes in the archive chunk hot path. Use ring slots, atomics, and doorbells as in the tuple sink path.
- Keep file read and network publication overlapped. The producer currently performs read then send in one loop inside [`sendFile()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1680); a service sink can decouple backend file-read progress from remote network completion with bounded backpressure.
- First prototype should assume no service-visible compression or throttling. Server-side compression in the existing `bbsink` chain can be revisited later, but it hides byte-to-file relationships and consumes backend CPU; throttling is not research-interesting for the hot path.
- Progress should not create hot-path traffic. Infer counters locally from chunk descriptors and optionally publish sparse command-mailbox status.
- If `FETCH_WAL` is included, those WAL segments are archive file payloads read from `pg_wal`, not the live WAL replication stream. Do not reuse the physical WAL streaming offload model unless `STREAM_WAL` is selected.
- For `TARGET 'homer'`, it is acceptable to patch or bypass upstream assertions
  and sink-chain assumptions that only protect option combinations outside the
  experiment. Replace broad upstream assumptions with narrow prototype
  assertions that check the chosen path: tar archive payloads, no compression,
  no throttling, no `STREAM_WAL`, registered-slot lifetime, and ordered archive
  boundaries.

## Rejected Or Deferred Shortcuts

- Do not model base backup as tuple transport. There is no tuple descriptor, no per-row deforming, and no tuple-view contract.
- Do not model it as ordinary WAL streaming. `BASE_BACKUP` has archive/manifest structure and finite backup-mode lifecycle.
- Do not skip PostgreSQL's backup start/stop and simply copy files from outside the server. [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:267) and [`perform_base_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:394) are the consistency envelope.
- Do not make remote arrival equal successful backup completion. A useful remote backup target must distinguish received, written, fsynced, and finalized states.
- Do not start with file-extent semantics unless the goal explicitly includes changing archive compatibility. It is more powerful but much easier to get wrong.
- Do not create a dedicated progress sink or first-class progress payload object for the prototype. It is too niche for the research goal and can be inferred from archive chunks.

## Implementation Progress

- **Status**: `not-started`
- **What exists in code now**: upstream PostgreSQL `bbsink`/`bbstreamer` base-backup path plus the Homer tuple-sink/remote-session/RDMA infrastructure.
- **Feature gates / switches**: none for base-backup offload yet.
- **Assumptions / shortcuts / scaffolding**:
  - first prototype should hook at the `bbsink` layer through explicit `TARGET 'homer'`
  - Homer may use a specialized sink-chain shape and patch/bypass upstream assumptions that are irrelevant to the selected research path
  - sender hot path should use rotating registered RDMA payload slots as the active `bbsink` buffer and avoid copying into a second transport buffer
  - existing tuple-named RDMA payload-ring types and helpers should be generalized to a remote payload channel if practical; otherwise use a temporary generic wrapper rather than creating a base-backup-only RDMA data plane
  - receiver should initially blackhole or count archive and manifest bytes after consuming the real remote payload channel, then later materialize archive bytes or files in a service-owned sink
  - manifest callbacks should be implemented in the same typed stream family rather than disabled for the first run
  - compression and throttling are disabled or ignored for the first prototype
  - progress is locally inferred from archive chunks and optionally reported sparsely through the command/completion mailbox; minimal local `bbsink_state` updates still need to be preserved or the end-of-backup checks patched for the Homer path
  - WAL is out of scope for the first prototype; `STREAM_WAL` remains a separate replication-offload problem
- **Changes from earlier tuple-copy work**:
  - the typed object is an archive/chunk/control stream, not `CitusTupleViewContract`
  - batch lifetime maps to archive payload slots, not tuple slots
  - receiver processing maps to `bbstreamer`/file sinks, not slot binding

## Related

- [../../../postgres/replication/base_backup_archive_stream.md](../../../postgres/replication/base_backup_archive_stream.md): grounded current `BASE_BACKUP` path.
- [replication_transport_offload_abstraction.md](replication_transport_offload_abstraction.md): physical WAL streaming offload, which applies to `-X stream` but not to archive payloads.
- [../../citus/connection-management/remote_execution_session_control_plane.md](../../citus/connection-management/remote_execution_session_control_plane.md): existing remote execution session abstraction.
- [../../citus/data-movement/tuple_sink_data_plane_completion_plan.md](../../citus/data-movement/tuple_sink_data_plane_completion_plan.md): tuple-sink data-plane plan for comparison.
- [../../citus/transport/rdma_publication_visibility_and_doorbells.md](../../citus/transport/rdma_publication_visibility_and_doorbells.md): RDMA publish/visibility rules reused by this direction.
