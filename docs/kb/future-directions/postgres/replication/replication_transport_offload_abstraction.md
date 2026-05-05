# Replication transport offload abstraction

## Scope

- **What this doc explains**: how PostgreSQL physical WAL replication should map onto the external communication service, which parts can plausibly be offloaded, and why the right high-level object is not a tuple-like or command-like semantic object.
- **What this doc does NOT cover**: logical replication redesign, replacing PostgreSQL redo/apply with an external engine, or immediate code changes.
- **Primary directory**: `docs/kb/future-directions/postgres/replication/`
- **Doc type**: `future-direction`

## Why this exists (context)

The current research prototype already has:

- tuple views as the high-level payload object for tuple movement
- typed commands plus typed completion for worker execution and transaction control

The open question is whether physical replication should get a comparable abstraction.

The grounded replication note shows an important correction: physical replication already has a standardized payload, but it sits lower than tuples or typed commands. The canonical object is the WAL byte stream itself, ordered by LSN and timeline, with cumulative `write` / `flush` / `apply` progress. That suggests the offload boundary should be phrased in terms of:

- **session/control state**
- **durable WAL-chunk publication**
- **cumulative progress/ACK frontiers**

not in terms of re-deriving higher-level database semantics at the receiver.

## Key code pointers

- Grounded WAL construction and shared-buffer staging:
  - [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750)
  - [`CopyXLogRecordToWAL()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1227)
  - [`XLogCtl->pages`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:493)
  - [`GetXLogBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1618)
  - [`AdvanceXLInsertBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1980)
  - [`PGSharedMemoryCreate()`](/data/dbcomm/postgres-citus/src/include/storage/pg_shmem.h:87)
  - [`InitShmemAccess()`](/data/dbcomm/postgres-citus/src/backend/storage/ipc/shmem.c:93)
- Grounded local durability frontier:
  - [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778)
  - [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478)
  - [`RecordTransactionCommit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xact.c:1304)
- Grounded send path:
  - [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3100)
  - [`walsender.c:3204`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3204)
  - [`walsender.c:3325`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3325)
  - [`pq_putmessage_noblock()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1511)
  - [`internal_putbytes()`](/data/dbcomm/postgres-citus/src/backend/libpq/pqcomm.c:1273)
- Grounded receive/write/flush path:
  - [`XLogWalRcvProcessMsg()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:839)
  - [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910)
  - [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993)
  - [`XLogWalRcvSendReply()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:1100)
- Grounded replay/apply boundary:
  - [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131)
  - [`XLogReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogreader.c:389)
  - [`GetRmgr(record->xl_rmid).rm_redo(...)`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:1991)
- Protocol envelope:
  - [`doc/src/sgml/protocol.sgml:2332`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2332)
  - [`doc/src/sgml/protocol.sgml:2446`](/data/dbcomm/postgres-citus/doc/src/sgml/protocol.sgml:2446)
- Existing prototype vocabulary for comparison:
  - [`docs/kb/postgres/serialization/printtup_row_serialization_and_transport.md`](../../../postgres/serialization/printtup_row_serialization_and_transport.md)
  - [`docs/kb/future-directions/citus/data-movement/command_dispatch_completion_plane.md`](../../citus/data-movement/command_dispatch_completion_plane.md)
  - [`docs/kb/future-directions/citus/transport/rdma_publication_visibility_and_doorbells.md`](../../citus/transport/rdma_publication_visibility_and_doorbells.md)
- Current Homer RDMA publication helpers:
  - [`TupleSinkServicePostPeerInlineBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2888)
  - [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2978)
  - [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3040)
  - [`TupleSinkServicePostWriteBufferInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1051)
  - [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3850)

## Current behavior / grounded findings

### The standardized replication payload already exists, and it is WAL

Physical replication does not begin from “backend local state that needs to be converted into a transport object” in the same way tuple or command abstractions do.

Instead:

1. backend code constructs WAL privately
2. PostgreSQL converts that into the canonical WAL record/page byte stream in shared WAL buffers through [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750)
3. local durability is established through [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778)
4. replication then transports that same WAL byte stream

So the “standardized form” is already the WAL stream itself.

### Receiver ingress is not a high-level semantic decode step

Receiver ingress is small-envelope parse plus raw-byte landing:

- [`XLogWalRcvProcessMsg()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:839) parses the header
- [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) lands raw bytes
- [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993) establishes standby durability

Only later do [`ReadRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:3131) and [`rm_redo`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlogrecovery.c:1991) interpret those bytes. That is a strong hint that transport offload and replay/apply should remain separate layers.

### The most natural abstraction boundary is lower-level than tuple/command

For the external service, the closest analogue is not:

- `TupleView`
- typed SQL-ish command

but something like:

- `ReplicationSession`
- `DurableWalChunkPublication`
- `ReplicationProgressFrontier`

That is lower-level, more cumulative, and more durability-oriented than the current tuple/command abstractions.

### Direct RDMA visibility of primary `XLogCtl->pages` is plausible, but not automatically free

- [`XLOGShmemInit()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:4873) allocates the shared WAL pages once, so in principle they can be made visible to the external service.
- [`PGSharedMemoryCreate()`](/data/dbcomm/postgres-citus/src/include/storage/pg_shmem.h:87) and [`InitShmemAccess()`](/data/dbcomm/postgres-citus/src/backend/storage/ipc/shmem.c:93) show that those pages live in PostgreSQL's actual shared-memory segment, not backend-private memory.
- [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1750) already demonstrates that a sender path can source payload directly from those pages.
- But [`GetXLogBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1618), [`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1779), and [`AdvanceXLInsertBuffer()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1980) together show the important hidden constraint: the WAL buffers are a recycled circular cache, not an indefinitely stable publication region.

So a direct-RDMA primary design can plausibly remove the sender-side copy into libpq staging, but it still needs:

- a published range descriptor
- a rule that only locally durable ranges may become remotely visible
- either credits/pinning or fallback to `pg_wal` when the service lags beyond the WAL-buffer window

The extra correction from the design discussion is that "shared-memory visibility" and "safe no-copy" are different things. If the service or NIC may still be reading a page when PostgreSQL wants to recycle that WAL-buffer slot, the design needs backpressure, an explicit ownership/credit contract, or a copied fallback buffer for that range.

### The current Homer RDMA transport already has the right control/payload/doorbell split for replication

The existing tuple path is useful because it already separates:

- small control/header bytes
- large registered payload bytes
- the final responder-visible publish step

The current helpers do that explicitly:

- [`TupleSinkServicePostPeerInlineBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2888) copies a small caller buffer into a connection-owned registered scratch buffer and posts it unsignaled. This is good for a small replication descriptor or fixed header.
- [`TupleSinkServicePostPeerRegisteredBytesUnsignaledRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2978) sends directly from a caller-owned registered buffer range. This is the closer match for a WAL payload borrowed from shared WAL pages or copied into a service-owned staging buffer.
- [`TupleSinkServiceWritePeerUint64WithImmediateRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3040) publishes one 64-bit state word with `RDMA_WRITE_WITH_IMM`, and waits for the local completion that closes the whole earlier unsignaled sequence.
- [`TupleSinkServicePostWriteBufferInternal()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1051) documents the exact rule: remote publication and local completion are separate, and callers may reuse shared source buffers only after a later signaled WR on the same QP completes.

Important distinction from the command/completion control path:

- the current peer-control mailbox does **not** use `inlineWriteBuffer`
- [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2109) writes the whole fixed-width [`TupleSinkServicePeerControlMessage`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:74) from `connectionState->outgoingMessageBuffer` into the peer mailbox slot, then publishes the mailbox tail with `WRITE_WITH_IMM`
- [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2060) consumes that fixed-width mailbox message only after the matching doorbell arrives

So the current Homer implementation already shows two patterns:

- control mailbox: one fixed-width registered message plus a tail publish
- tuple payload: inline transport header plus registered payload plus a tail publish

So for replication, "inline control bytes" should mean small metadata such as:

- timeline
- `start_lsn`
- `end_lsn`
- payload length
- maybe a source durable frontier / send timestamp

while "registered bytes" should mean the raw WAL payload region itself.

That is the same logical split as the tuple path in [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:3968), which posts:

1. an unsignaled inline header
2. an unsignaled registered payload
3. a final signaled `WRITE_WITH_IMM` tail publish

The replication design does not have to preserve PostgreSQL's `XLogData` envelope on the wire. It only needs the same publication structure.

## Pitfalls, caveats, and hidden constraints

- A tempting but wrong shortcut is to treat physical replication like “another command family”. WAL is not a request/response action initiated by one backend; it is a shared, ordered durability stream produced by the whole system.
- Another tempting shortcut is to look for a tuple-like semantic object on the receive side. The receive side mostly writes exact bytes and reports cumulative positions.
- Another tempting shortcut is to offload WAL construction itself. That would force the external service to reproduce PostgreSQL WAL semantics, local durability integration, and recovery invariants. That is much larger than communication offload.
- Streaming before local flush is not just a performance tweak. [`walsender.c:3193`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3193) shows it changes the safety model.
- Another tempting shortcut is to assume that “WAL has arrived in standby shared memory” is enough for `remote_write`. Current PostgreSQL ties the write frontier to [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) and [`GetWalRcvWriteRecPtr()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiverfuncs.c:347), i.e. the WAL-file write.
- Another tempting shortcut is to say "if the shared WAL pages are RDMA-accessible, then replication is zero-copy". The current send path still does libpq/socket staging, which can be removed, but no-copy from PostgreSQL-owned WAL pages is only valid while those pages remain stable long enough for the transport to consume them.
- Another tempting shortcut is to reuse PostgreSQL's `XLogData` replication envelope byte-for-byte in the new service protocol. That envelope is only the current libpq replication framing. A cleaner service protocol should keep metadata in its own ring descriptor/control header and keep the payload area as raw WAL bytes only.

## Implementation progress / prototype state

- **Status**: `not-started`
- **What exists in code now**: tuple and typed-command abstractions in the Homer prototype; no replication-specific offload abstraction yet.
- **Feature gates / switches**: none yet for this design.
- **Assumptions / shortcuts / scaffolding**: this note assumes the first target is PostgreSQL physical replication, not logical replication.
- **Changes from the motivating design**:
  - the initial intuition was “maybe replication can become another high-level semantic object”
  - grounded correction: the right boundary is likely a WAL-chunk publication/control split, not a reconstructed semantic object
- **Corrections from earlier assumptions / rejected approaches**:
  - rejected-first-step idea: external service derives WAL from backend state
  - reason for rejection: PostgreSQL already must construct the exact WAL bytes locally for crash consistency and local `pg_wal`
- **Canonical future-direction note**: this note

## Architecture / data flow

### Entry points

Potential future boundary:

1. PostgreSQL inserts WAL into shared buffers through [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750).
2. PostgreSQL establishes local durability with [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778).
3. An offloaded replication service is notified of the newly durable WAL frontier and the relevant byte range.
4. The service transports raw WAL bytes and receives cumulative progress frontiers from the remote side.
5. On standby, the service lands the bytes durably and reports `write` / `flush` progress.
6. PostgreSQL recovery continues to own WAL interpretation and redo/apply.

### Control flow split

- **Control plane**:
  - replication session open/close
  - start LSN / timeline / slot identity / sync mode
  - keepalive and liveness
- **Data plane**:
  - contiguous raw WAL byte chunks
  - ordered by `(timeline, startLsn, endLsn)`
- **Completion / ACK plane**:
  - cumulative `write`
  - cumulative `flush`
  - cumulative `apply`

### Candidate data structures

Suggested backend-visible shape:

```c
typedef struct ReplicationSessionIntent
{
    TimeLineID timeline;
    XLogRecPtr start_lsn;
    int sync_mode; /* write / flush / apply policy */
    bool cascading;
} ReplicationSessionIntent;

typedef struct DurableWalChunk
{
    TimeLineID timeline;
    XLogRecPtr start_lsn;
    XLogRecPtr end_lsn;
    const char *bytes;
    Size len;
    bool bytes_borrowed_from_shared_wal;
} DurableWalChunk;

typedef struct ReplicationProgressFrontier
{
    XLogRecPtr write_lsn;
    XLogRecPtr flush_lsn;
    XLogRecPtr apply_lsn;
} ReplicationProgressFrontier;
```

This is intentionally transport-facing, not redo-facing.

The extra `bytes_borrowed_from_shared_wal` flag is not meant as final API surface. It records the important design distinction between:

- a borrowed fast path where the service directly publishes PostgreSQL-owned shared WAL pages and therefore inherits their lifetime constraints
- a copied fallback where the service owns the payload buffer and no longer depends on the WAL-buffer recycle window

For the network path, the ring descriptor/control header should stay separate from the payload bytes. Unlike PostgreSQL's current libpq path, the service does not need to prepend an `XLogData` envelope to the payload itself.

## Behavior details

### Recommended first abstraction

The first abstraction should be:

- **Postgres constructs and durably publishes WAL**
- **service transports and lands raw WAL**
- **Postgres still owns replay/apply**

That preserves the strongest existing invariants while still moving the network/control work into the external service.

Important refinement after further code reading:

- on the primary side, the service may be able to source payload directly from shared WAL pages instead of from a copied service-owned buffer
- on the standby side, a network-only design does **not** reach the current write/flush boundary unless PostgreSQL still has a local writer stage
- the existing Homer RDMA transport already provides the right publish shape: small inline control bytes, registered payload bytes, then one signaled publish/doorbell WR

### Candidate approaches

#### Option A: transport-only offload after local flush

Shape:

- PostgreSQL still owns shared WAL buffers, `XLogFlush()`, and local `pg_wal`
- once [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478) advances, the service is told “these WAL bytes are now durably publishable”
- service replaces `walsender`/transport mechanics, but not WAL construction or standby persistence
- if feasible, service reads directly from the primary shared WAL pages instead of requiring a copied publication buffer
- if direct publication from shared WAL pages is unsafe because send completion may outlive the WAL-buffer lifetime window, the service falls back to a copied service-owned payload buffer for that range

Pros:

- smallest semantic change
- preserves current primary durable-prefix invariant
- lines up directly with [`XLogSendPhysical()`](/data/dbcomm/postgres-citus/src/backend/replication/walsender.c:3100)
- can remove libpq/socket staging copies on the primary send path
- can prepare descriptors/ring bookkeeping before the local fsync completes, then publish only after the durable frontier advances

Cons:

- less total offload
- standby ingress path still largely sits in PostgreSQL
- direct access to primary shared WAL pages needs buffer-lifetime control and lag fallback

Assessment:

- best first step

Expected benefit:

- CPU savings are mainly in the replication transport path, not in normal OLTP backends, because PostgreSQL backends still do [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778)
- the likely primary-side win is bypassing libpq/socket staging and possibly one extra payload copy
- latency improvement is limited but real if RDMA transport and descriptor setup are cheaper than the current socket path; it does not remove the primary local flush or the standby WAL-file write that gates `remote_write`

#### Option B: transport plus standby durable landing offload

Shape:

- primary side same as Option A
- standby-side service receives raw WAL, writes/fsyncs durable landing storage, and reports `write` / `flush`
- PostgreSQL recovery consumes from that landed durable store and still performs redo/apply

Pros:

- larger offload win on the receiver side
- receiver envelope parse, file write, fsync, and ACK logic can move out

Cons:

- must preserve `WalRcv->flushedUpto`-style semantics and recovery wakeups
- must define crash-safe ownership of landed WAL and replay handoff

Assessment:

- plausible second step if the primary-side publish boundary is already stable

#### Option A.1: network-only receive offload with a PostgreSQL local writer stage

Shape:

- service receives raw WAL over RDMA into a shared-memory inbound ring
- PostgreSQL local writer consumes from that ring and performs the current [`XLogWalRcvWrite()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:910) / [`XLogWalRcvFlush()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiver.c:993) responsibilities
- recovery remains unchanged and still watches [`GetWalRcvFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/replication/walreceiverfuncs.c:331)
- the local writer can still `pwrite()` directly from the service-populated ring entry, so the receive side can avoid socket/libpq copies even though it keeps the current file-write boundary

Clarification:

- this inbound ring is **not** an existing PostgreSQL WAL buffer
- it is a new service-managed registered ring whose slots are visible to both the RDMA sender and the local standby-side PostgreSQL/service boundary
- the local writer borrows one ready slot, uses that slot's payload buffer as the source for `pwrite()`, then releases the slot back to the ring after the file write is complete

So yes: for a network-only standby design, the inbound replication ring is the borrowable sink.

Pros:

- stays within a “network communication only” scope
- can remove socket/libpq receive overhead and reduce standby-side transport copies
- preserves the existing WAL-file and recovery contract

Cons:

- `remote_write` ACK still cannot be emitted until the PostgreSQL local writer has done the WAL-file write
- adds one service-to-PostgreSQL handoff on the standby before the current write frontier advances
- does not remove standby `pwrite()` / `fsync()` costs

Assessment:

- valid if the project scope is strictly network-only
- likely more of a replication-process CPU win than a synchronous-commit latency win

### Candidate optimization: prepare publication metadata before local flush, publish only after flush

The design discussion produced one optimization that stays within PostgreSQL's current safety model:

- PostgreSQL already has the raw WAL payload in [`XLogCtl->pages`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:493) before [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778) completes
- the service can reserve queue slots and compute `(timeline, start_lsn, end_lsn)` publication metadata while that local fsync is still in flight
- but the service must not actually expose the chunk to the remote side until [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478) has advanced far enough

So the safe overlap is descriptor/ring preparation behind the primary local fsync, not remote publication before local durability.

### Important correction from PostgreSQL's current copy rule

[`WALReadFromBuffers()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:1774) already has a correctness rule for copied extraction from recycled WAL pages: verify page identity, copy, verify again, and stop if the page changed.

That rule is enough for the current copied send path because the source page only needs to remain valid until one local memcpy finishes. It is **not** enough for a borrowed RDMA send, because the source page would need to remain valid until the final local completion checkpoint of the publish sequence.

So the replication offload design should not describe this as "PostgreSQL already pins for copying, just pin longer". The current system avoids long-lived ownership entirely by copying into a private buffer and then releasing dependency on the WAL page immediately.

#### Option C: offload WAL construction or redo/apply semantics

Shape:

- service derives WAL from backend semantics or interprets WAL deeply enough to replay/apply

Pros:

- potentially larger long-term offload

Cons:

- duplicates PostgreSQL WAL/resource-manager semantics
- crosses from communication offload into database-engine behavior
- extremely high semantic risk

Assessment:

- wrong first abstraction for this project

### Relationship to tuple and command abstractions

Comparison with current prototype objects:

- `TupleView`:
  - high-level payload object
  - object identity is per tuple/batch
  - receiver consumes semantic fields directly
- typed command/completion:
  - high-level control object
  - object identity is per operation / transaction-control event
  - receiver executes semantic action directly
- physical replication:
  - low-level durability stream
  - object identity is cumulative LSN/timeline range
  - receiver should usually not reinterpret semantics on ingress

So replication is analogous mainly in the sense that it still wants:

- a session/control abstraction
- a data-plane payload abstraction
- a completion abstraction

But the payload object itself is much lower-level.

One more useful comparison:

- tuple path: semantic payload is transformed before transport
- command path: semantic intent is transformed before transport
- physical replication path: payload is already in final semantic form; transport mostly adds framing, staging, and publication ordering

## Invariants and assumptions

- PostgreSQL must still construct exact WAL bytes locally before they can be durable or replicated.
- The first safe publish boundary is the local durable WAL frontier, not mere shared-buffer presence.
- Receiver ingress and replay/apply should remain separate concerns unless the project deliberately expands scope into recovery semantics.
- Cumulative `write` / `flush` / `apply` frontiers are the right completion semantics to preserve.
- In a network-only standby design, shared-memory arrival is an internal transport state, not the externally visible `remote_write` frontier.

## Design/implementation mismatches to remember

- Tuple/command abstractions are semantic objects; physical replication is closer to a publication log.
- An external service can own transport and durable landing without owning redo/apply.
- “Standardized form” here is not something new to invent; it already exists in upstream PostgreSQL as WAL format.

## Configuration / flags

- Future prototype should preserve the semantics of:
  - `synchronous_commit`
  - sync standby configuration
  - physical receiver status intervals / keepalive behavior

## Interactions and cross-links

- **Overlapping KB areas**:
  - [`../../../postgres/replication/physical_wal_streaming_and_synchronous_commit.md`](../../../postgres/replication/physical_wal_streaming_and_synchronous_commit.md)
  - [`../../../postgres/serialization/printtup_row_serialization_and_transport.md`](../../../postgres/serialization/printtup_row_serialization_and_transport.md)
  - [`../../citus/data-movement/command_dispatch_completion_plane.md`](../../citus/data-movement/command_dispatch_completion_plane.md)
- **Shared code / canonical docs**: the factual replication behavior stays in the PostgreSQL replication KB note.
- **Implementation note / factual note / future-direction note**: no implementation note exists yet for replication offload.

## Future directions / design ideas

- **Status**: `exploratory`
- **Goal**: define a replication offload boundary that moves communication work out of PostgreSQL without absorbing WAL-generation or redo semantics into the external service.
- **Grounding in current code**:
  - WAL is already standardized by [`XLogInsertRecord()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:750)
  - safe publication is gated by [`XLogFlush()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:2778) and [`GetFlushRecPtr()`](/data/dbcomm/postgres-citus/src/backend/access/transam/xlog.c:6478)
  - current sender/receiver are transport envelopes around raw WAL plus cumulative progress
  - replay/apply is later and separate
- **Candidate approaches**:
  - Option A: transport-only after local flush
  - Option B: transport plus standby durable landing
  - Option C: full semantic offload of WAL construction or apply, rejected as the first target
- **Risks / tradeoffs**:
  - local durability ordering is non-negotiable unless the safety model changes
  - receiver-side landing must stay crash-safe and replay-compatible
  - over-abstracting into tuple/command semantics would lose the important cumulative LSN nature of the workflow
  - direct RDMA reads from primary shared WAL pages trade sender copies for buffer-lifetime/recycle complexity
  - network-only standby offload may reduce CPU while offering little latency improvement because the PostgreSQL local writer remains on the `remote_write` critical path
- **Next experiments / implementation steps**:
  1. define a narrow “new durable WAL frontier” publication API from PostgreSQL to the service
  2. prototype a transport-only sender replacement that reads exact WAL bytes from the existing shared-buffer / `pg_wal` sources, preferably with direct shared-WAL-buffer reads where the recycle window allows
  3. prototype the strict network-only standby variant with an RDMA inbound ring plus PostgreSQL local writer, to measure whether copy/syscall savings outweigh the extra handoff
  4. if that is stable, explore a standby-side durable-landing service that still leaves replay in PostgreSQL

## Debugging notes / gotchas

- If a design sketch starts talking about “replication tuples” or “replication commands” without explaining the WAL byte-stream boundary, it is probably abstracting at the wrong level.
- If a design sketch tries to skip local `XLogFlush()` before publish, it is changing semantics, not just optimizing transport.
- If a design sketch claims receiver ingress must decode semantic objects before durability, it is likely mixing physical and logical replication ideas.
- If a design sketch claims “shared-memory arrival on standby is enough for `remote_write`”, it is redefining the current PostgreSQL contract rather than matching it.

## Open questions / TODO

- What is the least-invasive way to let the external service consume newly durable WAL ranges without adding hot-path lock contention?
- Should the primary-side publication API expose only “durable frontier advanced” and let the service pull the bytes, or should PostgreSQL push explicit chunk descriptors?
- On the standby side, is a durable landing file API enough, or does recovery need a more direct shared-memory handoff for acceptable wakeup latency?
- For primary-side direct RDMA reads from `XLogCtl->pages`, what is the best lifetime rule: explicit credits, page pinning, or fallback to `pg_wal` after a bounded lag window?
