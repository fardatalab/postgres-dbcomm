# Cross-node session identity and independent-consumer pairing

## Purpose

Canonical design note for how a Homer cross-node payload session is *identified*
and how the two independent endpoints of a basebackup are *paired* on the
receiver node. It records the three-layer identity model, the "throwaway
session" + "pending-binding registry" warts that basebackup grew, the
feasibility spike that reframed the fix, the **decision** we adopted
(receiver-session base-compat+tag scan, **no** cross-node handshake), and the
end-state (session-spawned receiver) that is still deferred.

Grounds against the current code in
`citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c`
and the client in `citus-dbcomm/src/bin/homer_client.c`. Implementation progress
lives in
[`../../../implementations/citus/transport/cross_node_dpu_migration_checkpoint.md`](../../../implementations/citus/transport/cross_node_dpu_migration_checkpoint.md).

## Three layers of identity (do not conflate)

1. **`sessionKey` — the DB-semantic *reuse* identity (NON-unique, by design).**
   `CitusRemoteExecSessionKey`, 13 connection/policy fields; base-compatibility
   is the 5-field subset in
   `HomerServiceSessionKeysBaseCompatible` (`tuple_sink_service_process.c:20660`)
   — `protocolVersion, destinationNodeId, effectiveUserId, databaseId,
   executionLane`. It is directed-node-pair scoped and deliberately shared by
   compatible sessions so they can *rendezvous/reuse*. It is a
   reuse/rendezvous key, **never** a unique instance identity or a stream/relay
   key.

2. **`serviceSessionId` — NODE-LOCAL.** Each node mints its own via
   `NextTupleSinkServiceSessionId++` (`tuple_sink_service_process.c:21970`).
   farnet0's id for a session and farnet1's id for the same logical session are
   different numbers; they only learn each other's through explicit
   `localServiceSessionId`/`peerServiceSessionId` linkage on the peer-open. It
   therefore **cannot** be the cross-node binding id.

3. **`sessionUID` — the CROSS-NODE-STABLE binding id.** Minted by the receiver
   (equals `dpuBridgeGeneration`, the id the receiver's role-7 host consumer ring
   is imported under). The DPU relay pump resolves *which role-7 ring* to DMA
   landed bytes into by this value, not by role alone. It is **not** redundant
   with `serviceSessionId`: the apparent equality is only on the *ring
   descriptor* (the opener sets `descriptor.serviceSessionId = sessionUID`); the
   *session's* `serviceSessionId` is a different, node-local value. Field:
   `TupleSinkServiceSessionState.sessionUID` (`tuple_sink_service_process.c:1269`)
   and `HomerServicePayloadStreamEntry.dpuRelayResultSessionUID` (the stream's
   cached copy).

> Naming: we kept `sessionUID` (the earlier confusion was missing docs, not the
> name). Read it as *"the cross-node-stable per-session binding id the DPU relay
> resolves on"*, always in contrast to the node-local `serviceSessionId`.

## Why basebackup grew warts that SQL/COPY did not

The real axis is **consumer-spawned-by-session vs independent-consumer**, *not*
push-vs-pull:

- **SQL and Citus COPY spawn their consumer backend *from the session*** (via
  `TupleSinkServiceSubmitBackendSpawnRequest`). The consumer is inherently
  paired with the session that spawned it, so the `sessionUID` is threaded along
  the command-session bridge (client → command peer-open → farnet1 session state
  → per-command result SEND peer-open, which carries `request->sessionUID`).
  Nothing extra is needed.

- **basebackup's receiver is an *independent process*** (`pg_basebackup
  --homer-receive`, `RunHomerReceiveConsume` in
  `postgres-citus/src/bin/pg_basebackup/pg_basebackup.c`). It opens its own
  selected-DPU RECEIVE stream and has **no** command-session bridge to thread
  the `sessionUID` through. Two independent processes (the farnet1 walsender
  *sender* and the farnet0 *receiver*) must still be paired on the farnet0
  service. That pairing is the root of both warts below.

### Wart 1 — the "throwaway session"

On the farnet0 service the relay stream (the `HomerServicePayloadStreamEntry`
the DPU relay pump drains) is created by the *far sender's* peer-open
(`TupleSinkServiceHandlePeerOpenRequest:25276`). Because both basebackup and SQL
sessions are `FRESHNESS_FORCE_FRESH`, `TupleSinkServiceFindReusableSession`
(`:25422`) never reuses, so the peer-open **creates a fresh base-compat session
`T`** (`:25436`) purely to own the relay stream. The receiver's own RECEIVE-open
(`:33796-33862`) creates *no* session — it just parks its `sessionUID` in a side
registry and attaches to `T`'s stream. So the single relay-owning session is a
base-compat shell with the receiver's authoritative `sessionUID` bolted on from
the side, rather than a session that *is* the receiver's, identified by its
`sessionUID`.

### Wart 2 — the pending-binding registry

`ActivePendingBindingTable` (`tuple_sink_service_process.c:539`) with
Register (`:20691`) / Lookup (`:20727`) / Unregister (`:20744`), all
base-compat keyed. Its **only** job is to bridge **consumer-first ordering**: if
the receiver's RECEIVE-open arrives before the sender's peer-open, the receiver
has a `sessionUID` but no stream yet to stamp it onto, so it parks the uid in the
registry (`:33817`); the later peer-open / relay pump looks it up by the parent
session's `sessionKey`
(`HomerServiceResolveRelayResultSessionUID:30826` → `:30847`). In the
sender-first ordering the RECEIVE-open stamps the stream directly (`:33836`) and
the registry is never read.

## The feasibility spike that reframed the fix (2026-07-08)

The originally-approved plan
([`here-s-a-snippet-on-linked-papert.md`](../../../../../.claude/plans/here-s-a-snippet-on-linked-papert.md))
proposed an **automatic cross-node handshake**: the receiver would drive a
stripped `OP_COMMAND_SESSION` open to farnet1 (mirroring SQL's command-session
spine "minus the SQL-only branches"), so the sender's SEND session adopts the
receiver's `sessionUID` and the farnet0 relay binds by a clean cross-node id,
letting us delete the registry. A step-0 spike was required to confirm the
receiver could drive that open. It surfaced three facts that invalidate the
handshake's "just reuse SQL's spine" premise:

1. **There are two 9717 peer legs, same binary, different instances.**
   `citus_tuple_sink_service` always maps the host control shm and binds the
   9717 peer transport; only with `HOMER_SERVICE_ENABLE_DPU_DMA=1` does the same
   instance add the DOCA engine + 9727 setup TCP. In the DPU-basebackup
   deployment the **sender's SEND session is owned by the farnet1 *DPU* service**
   (`HomerClientOpenBaseBackupStreamSelectedDpu` submits over the sender's local
   DPU; that DPU service issues the DPU↔DPU SEND peer-open). SQL's cross-node
   command spine, by contrast, lives entirely in the **host** service on both
   ends. So the plan's hint "map the farnet0 host service control region, as
   pgbench does" routes the linkage into the farnet1 **host** service — the wrong
   instance to rendezvous with the SEND session. The linkage would have to travel
   **DPU↔DPU**, not host↔host.

2. **The DPU↔DPU command-session path is net-new, not reuse.** The async
   command-open state machine hard-rejects any `opKind` that is not
   SQL/client-SQL at `COMMAND_CREATE` (`:34872-34884`) and unconditionally
   requires a `commandMailbox` at `REGISTER_MEMORY` (`:34936-34941`); only the
   registrations below that are `opKind`-gated. A record-only `OP_BASE_BACKUP`
   linkage would need a **new** DPU-control-slot command-session opener on the
   receiver (none exists — it only emits `OP_TUPLE_SINK`) **plus** three
   service-side gate relaxations.

3. **There is no receiver session `R` today** (see Wart 1) — the RECEIVE-open
   creates none. Any "own the relay by `R`" fix must *create* that session, which
   both the handshake and the chosen alternative need.

### The decisive realization

Even the handshake still performs exactly **one** `sessionKey`+tag rendezvous —
the plan's own risk note has the sender's SEND session "adopt the
linkage-recorded `sessionUID` (base-compat **+ tag** match)" on farnet1. So the
handshake does not remove the heuristic pairing; it **relocates** it from farnet0
to farnet1 and then threads the authoritative id back to farnet0, at the cost of
an entire cross-node control channel. The registry is fundamentally just a
"pending-binding index" for consumer-first ordering; you can delete it either by
(a) making the SEND peer-open self-sufficient via the handshake, or (b) turning
the receiver into a real, scannable session `R` and replacing the registry lookup
with a session-table scan. Both need to create `R`; both need the tag. (b) needs
no cross-node channel.

## Decision — receiver-session base-compat+tag scan (no handshake)

Adopted 2026-07-08 (user chose "session-scan, no handshake" over the planned
handshake after the spike). Same end-state as the plan — **no throwaway, no
registry** — with far less machinery and the **same** correctness guarantee (one
`sessionKey`+tag rendezvous, then bind by the receiver-minted `sessionUID`):

- **Concurrency tag.** `uint32_t launchDiscriminatorTag` on the RECEIVE
  `OpenSession` request and the SEND `PeerOpen` request, stored on the session
  (`TupleSinkServiceSessionState`). It is the launch discriminator that makes
  base-compat *unique enough* to pair concurrent same-base-compat backups. It is
  externally coordinated: the receiver gets `--homer-tag N`, the sender gets
  `tag=N` in `TARGET 'homer:mode=rdma,...'`, so both farnet0 opens carry the same
  tag. `0` = single backup, unchanged behavior. It is a *separate* field, **not**
  folded into `HomerServiceSessionKeysBaseCompatible` (which stays the pure
  DB-semantic reuse key); it is matched *alongside* base-compat only at the
  basebackup rendezvous scan.

- **Create/own the relay by a real receiver session `R`.** The basebackup
  RECEIVE-open (`:33796-33862`) and the SEND peer-open (`:25422-25488`) each
  find-or-create the single owning session `R` by base-compat **+ tag**:
  - *consumer-first:* the RECEIVE-open creates `R` (stamped `R.sessionUID =
    receiver mint`); the later SEND peer-open finds `R` by base-compat+tag and
    owns the relay stream by it, stamping the stream's `dpuRelayResultSessionUID
    = R.sessionUID`.
  - *sender-first:* the SEND peer-open finds no `R`, so it creates the owner
    placeholder `P` (base-compat+tag, `sessionUID = 0`) and owns the stream by
    it; the RECEIVE-open then finds `P` by base-compat+tag, adopts it as `R`
    (stamps `sessionUID`), and stamps the stream. `P` *is* `R` — one session, no
    throwaway.
  In both orderings the stream's `dpuRelayResultSessionUID` ends up stamped, so
  `HomerServiceResolveRelayResultSessionUID` returns it from the stream cache
  (`:30833`) and the **registry lookup is dead** and deleted.

- **Delete `ActivePendingBindingTable`** and its Register/Lookup/Unregister ops
  and call sites; the resolver reads the owning session's `sessionUID` (via the
  stream cache, or `FindSessionById(parentServiceSessionId)->sessionUID`).

### Validation correction (2026-07-08): pairing is (node, tag), NOT base-compat

The first cross-node DPU validation of the base-compat pairing **failed
deterministically** and corrected the pairing key. What the "base-compat + tag"
framing above got wrong (kept here as the correction, since it is easy to
re-introduce):

- **The basebackup sender is a PHYSICAL-REPLICATION walsender**, so its session
  key has `databaseId == MyDatabaseId == InvalidOid (0)` (`basebackup_homer.c`
  sets `options.databaseOid = MyDatabaseId`) and a replication-role user. It can
  **never** equal the receiver's real `--homer-database-oid` / `--homer-user-oid`.
- `HomerServiceSessionKeysBaseCompatible` requires `databaseId` **and**
  `effectiveUserId` equality (part of its 5-field set), so the two independent
  endpoints were **never base-compatible**. The SEND peer-open therefore fell
  through to its *sender-first* branch (created a fresh owner instead of finding
  `R`), the relay never armed, and the DPU↔DPU transport reset with **zero bytes
  moved** (farnet0 DPU log: `created basebackup owner session=... (sender-first)`
  → `CM event=DISCONNECTED status=0` → `reclaiming ... payload-failure-reclaim`).
- This was a **latent Part 3.5.1-era bug**, not new to 3.5.2: the pending-binding
  registry uses the same base-compat and was equally broken. It was never caught
  because the only prior DPU basebackup PASS (run #2) predates Part 3.5.1 and
  resolved *role-only*, not by base-compat.

**Fix (implemented): pair basebackup on `(destinationNodeId, launchDiscriminator
Tag)`, not full base-compat.** `HomerServiceBaseBackupSessionKeysPairable`
(`tuple_sink_service_process.c`) matches only the fields the two endpoints
actually share — `protocolVersion` + `destinationNodeId` + `executionLane` —
deliberately **excluding** `databaseId`/`effectiveUserId`.
`TupleSinkServiceFindBaseBackupOwnerSession` uses it (+ `opKind == OP_BASE_BACKUP`
+ the tag). So everywhere above that says "base-compat + tag" for **basebackup**,
read "(node, tag)": base-compat is the DB-semantic SQL-session reuse key and was
the wrong tool for pairing a DB-less replication walsender with a DB'd consumer.
The tag is still the concurrency discriminator; `node` is the directed pair. (SQL
is unaffected — it threads `sessionUID` via the command-session bridge and its
endpoints *do* share db/user.)

- **Second, independent bug observed (pre-existing, out of scope):** on the peer
  reset the sender backend hung, ignoring `SIGTERM` and `pg_terminate_backend()`
  (needs `SIGKILL` + crash recovery). This is the missing `CHECK_FOR_INTERRUPTS`
  in the DPU pull/reserve loop already documented in the implementation
  checkpoint; it is not caused by this work.

### Honest tradeoff vs. the handshake

The scan does **not** give basebackup the "authoritative `sessionUID` exchanged
cross-node like SQL" property — its farnet0 bind is preceded by a base-compat+tag
match. But the handshake has that *same* heuristic match (on farnet1), so the
uniformity gain was smaller than the plan implied, and the true "uniform
`sessionUID` everywhere, no heuristic" end-state is what the deferred
session-spawned receiver (below) delivers regardless. Cost saved: no new
cross-node channel, no DPU command-session opener, no `COMMAND_CREATE` /
`REGISTER_MEMORY` gate surgery, no new PEER ABI beyond the tag both options need.

### Cost-neutrality note

`TupleSinkServiceCreateSession` (`:21955`) unconditionally creates
command/completion mailboxes (`:21987`) for every session. Creating `R` is
therefore *not* new overhead: the current throwaway `T` already pays exactly this
(it is created the same way at `:25436`), and there is still only **one** session
per backup.

## Deferred end-state — session-spawned basebackup receiver

The only item left for the future: make the basebackup receiver
**session-spawned** like SQL/COPY (the consumer becomes part of the session, so
pairing is inherent). That removes *both* the tag **and** the farnet0
base-compat rendezvous entirely — the deepest fix, and the only place a true
"uniform, authoritative, no-heuristic" identity is achievable. It is larger
because it touches where the backup bytes are consumed/written, not just how the
endpoints rendezvous. Until then, base-compat+tag pairing of two independent
processes is irreducible.

## Related

- [`../../../implementations/citus/transport/cross_node_dpu_migration_checkpoint.md`](../../../implementations/citus/transport/cross_node_dpu_migration_checkpoint.md)
  — landed implementation + milestone history for this work.
- [`../../../implementations/citus/transport/dpu_payload_byte_ring.md`](../../../implementations/citus/transport/dpu_payload_byte_ring.md)
  — the byte-ring the relay pump drains into the role-7 ring identified by `sessionUID`.
- [`rdma_transport_control_plane_abstraction.md`](rdma_transport_control_plane_abstraction.md)
  — broader control-plane abstraction these session-identity choices serve.
