# Homer base backup target checkpoint

## Scope

- **What this doc explains**: the current implementation checkpoint for the `TARGET 'homer'` base-backup prototype, including the PostgreSQL `bbsink`, Homer client API, service-side typed object handling, local blackhole smoke mode, two-service RDMA blackhole mode, and verification/performance results.
- **What this doc does NOT cover**: final restore/materialization semantics, WAL streaming/fetch support, compression/throttling support, or durable receiver-side completion.
- **Primary directory**: `docs/kb/implementations/postgres/replication/`
- **Doc type**: `implementation`

## Design link

This checkpoint advances [base_backup_service_abstraction.md](../../../future-directions/postgres/replication/base_backup_service_abstraction.md) and depends on the grounded lifecycle in [base_backup_archive_stream.md](../../../postgres/replication/base_backup_archive_stream.md).

The implementation keeps the chosen semantic split:

- PostgreSQL still owns backup consistency, filesystem traversal, tar serialization, and manifest generation.
- Homer owns the service-backed data path after the `bbsink` callback boundary.
- The typed data-plane object is a finite ordered `BaseBackupArchiveStream`, represented today by `CitusRemoteBaseBackupMessageHeader` plus optional payload bytes.

## Current PostgreSQL-side behavior

`TARGET 'homer'` is registered as a built-in basebackup target in [`builtin_backup_targets`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_target.c:42). [`homer_get_sink()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_target.c:212) wraps the normal copy-stream sink with [`bbsink_homer_new()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:391), and [`BaseBackupTargetIs()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_target.c:174) lets [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:988) select the special prototype chain.

For Homer targets, [`SendBaseBackup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1033) rejects compression, throttling, and WAL inclusion at [`basebackup.c:1034`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1034), skips throttle/compression wrappers at [`basebackup.c:1046`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1046), and skips the generic progress wrapper at [`basebackup.c:1058`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup.c:1058).

[`bbsink_homer_begin_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:191) still calls `bbsink_begin_backup()` on `bbs_next` so the ordinary replication protocol result sets and `COPY OUT` lifecycle remain visible to `pg_basebackup`. It then opens Homer control and the basebackup stream, reserves a queue slot, publishes `CITUS_REMOTE_BASEBACKUP_OBJECT_BEGIN`, and exposes the next reserved payload slot as `sink->bbs_buffer`.

The hot payload callbacks do not copy into a second staging buffer:

- [`bbsink_homer_archive_contents()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:253) updates `bbs_state->bytes_done`, publishes `CITUS_REMOTE_BASEBACKUP_OBJECT_ARCHIVE_CHUNK`, and reserves the next slot.
- [`bbsink_homer_end_archive()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:272) publishes `ARCHIVE_END` and increments `bbs_state->tablespace_num`.
- [`bbsink_homer_begin_manifest()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:290), `manifest_contents`, and `end_manifest` publish manifest objects in the same stream family.
- [`bbsink_homer_end_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:341) publishes `CITUS_REMOTE_BASEBACKUP_OBJECT_END`, closes the Homer stream/control handles, and then forwards backup end to the copy-stream sink.

The important progress decision is now implemented: the prototype does not emit a separate progress data-plane object, but it preserves the local `bbsink_state` updates needed by [`bbsink_end_backup()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/include/backup/basebackup_sink.h:255).

## Current Citus/Homer-side behavior

The basebackup object vocabulary lives in [`tuple_sink_protocol.h`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:37). [`CitusRemoteBaseBackupMessageHeader`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/tuple_sink_protocol.h:331) is the semantic descriptor at the front of each published object; archive and manifest bytes follow the header only for chunk records.

The control-plane op kind is [`CITUS_REMOTE_EXEC_OP_BASE_BACKUP`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_control_protocol.h:99), mirrored in the backend-facing enum as [`REMOTE_EXEC_OP_BASE_BACKUP`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/include/distributed/homer/remote_execution_session.h:38). The service still records that remote-execution op in [`HomerServicePayloadStreamEntry.semanticOpKind`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:675), but the hot payload path now uses cached [`HomerServicePayloadStreamEntry.objectFamily`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:676), derived by [`TupleSinkServicePayloadFamilyForOpKind()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:795). This is the neutral payload-stream split: generic queue/RDMA state lives in [`HomerPayloadStreamState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:569), and basebackup-specific counters live in [`HomerBaseBackupPayloadState`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:652), not in tuple-view contract state.

The standalone Homer client API was extended for basebackup. Its default basebackup queue geometry is now 8 payload slots x 8 MiB at [`HOMER_CLIENT_DEFAULT_BASEBACKUP_PAYLOAD_BYTES`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:41) and [`HOMER_CLIENT_DEFAULT_BASEBACKUP_SLOT_COUNT`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:42); this preserves roughly the old 64 MiB ring footprint while avoiding the per-object overhead seen with 1 MiB chunks.

- [`HomerClientOpenBaseBackupStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1173) opens a send-side stream using the existing tuple-sink local control op, but sets the session key op kind to `CITUS_REMOTE_EXEC_OP_BASE_BACKUP` while opening the basebackup stream.
- [`HomerClientReserveBaseBackupSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1390) blocks until a queue slot is available and returns the payload pointer after `CitusRemoteBaseBackupMessageHeader`.
- [`HomerClientSubmitBaseBackupSlot()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1450) fills the semantic header in-place and advances the published tail.

The remote data path reuses the existing queue/RDMA substrate. After the neutral
payload-stream refactor, basebackup is a payload object family on the generic
stream container rather than a tuple-sink special case.
[`HomerServicePumpOutgoingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6069)
uses the cached object family to validate in-place payload objects through
[`HomerServiceValidateOutgoingPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5843),
computes the exact published byte range, and hands generic payload-write
descriptors to the shared RDMA batch publisher at
[`tuple_sink_service_process.c:6438`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6438).
[`HomerServicePumpIncomingPayloadStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6576)
validates and consumes received objects through
[`HomerServiceValidateReceivedPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5961);
basebackup objects are still blackholed/counted for the first receiver
milestone.

The receiver currently coalesces consumed-head publication once per pump pass after draining one or more incoming payload objects. This change did not materially improve the basebackup run by itself; the decisive performance factor in this checkpoint was chunk size rather than receiver-side consumed-head doorbell frequency.

## Local blackhole smoke mode

One implementation caveat surfaced during verification: a single service process cannot self-connect through the RDMA path today. With `host=127.0.0.1`, RDMA CM route resolution fails. With the host's RoCE interface address (`10.10.1.101` in this run), route resolution succeeds but the outgoing side times out waiting for `ESTABLISHED` because the same service thread is blocked in outgoing connect and cannot pump the listener/accept path.

To keep local correctness testing useful without pretending to test RDMA, `mode=blackhole` is a deliberately narrow local smoke mode for `REMOTE_EXEC_OP_BASE_BACKUP` only. It is selected through `TARGET 'homer:mode=blackhole,...'`, not through PostgreSQL's built-in `TARGET 'blackhole'`. [`bbsink_homer_apply_detail()`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_homer.c:78) parses the public `mode=` detail and rejects the old `host=blackhole` spelling with a hint. [`HomerClientOpenBaseBackupStream()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/bin/homer_client.c:1168) maps that public mode to a private control-protocol sentinel because the control protocol does not yet have an explicit receiver-mode field.

[`HomerServicePayloadStreamIsLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:813)
identifies the local Homer blackhole mode. [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7184)
skips peer-open only for basebackup plus the private blackhole sentinel at
[`tuple_sink_service_process.c:7575`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7575).
[`HomerServicePumpLocalBaseBackupBlackhole()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:6489)
then validates the same semantic headers directly from the backend-visible send
queue, records accounting through
[`HomerServiceRecordBaseBackupPayloadObject()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:5942),
and advances `consumedHead`.

PostgreSQL's separate `TARGET 'blackhole'` remains the upstream server-side discard target registered in [`builtin_backup_targets`](/data/dbcomm/postgres-citus-separate-comm-stack/src/backend/backup/basebackup_target.c:42). That path still uses libpq for the `pg_basebackup` replication command/control session, but the bulk archive bytes are discarded inside the PostgreSQL backend; they do not traverse libpq and do not exercise Homer.

This mode proves:

- PostgreSQL can run `BASE_BACKUP TARGET 'homer'` through the normal `pg_basebackup` entry point.
- The Homer sink's buffer lifetime is correct enough for the full tar/manifest producer path.
- The minimal progress bookkeeping preserves the end-of-backup tablespace assertion.
- The service can parse and count basebackup semantic objects without tuple-view metadata.

It does not prove:

- service-to-service RDMA throughput
- remote receiver materialization
- two-node peer-open/peer-close timing

## Two-service RDMA blackhole mode

The real data-path milestone uses two service processes on the same host with separate control regions and separate RDMA listener ports. The local/default service remains the backend-facing service, while the remote peer service is started with:

```sh
env HOMER_SERVICE_CONTROL_SHM_NAME=/citus_remote_execution_control_v18_remote \
    HOMER_SERVICE_SHM_TAG=remote \
    HOMER_SERVICE_PEER_PORT=9718 \
    /data/dbcomm/pg-citus/bin/citus_tuple_sink_service
```

The service runtime reads those knobs in [`main()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:7405), passes the configured RDMA listener port into [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3181), and maps the configured control region through [`TupleSinkServiceMapControlRegion()`](/data/dbcomm/citus-dbcomm-separate-comm-stack/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1139).

This mode proves the important path that local `mode=blackhole` does not:

- peer-open binds the sender sink to a peer-provisioned receive sink
- registered local queue slots are published through RDMA write plus immediate notification
- the remote service validates `CitusRemoteBaseBackupMessageHeader`, counts payload bytes, and releases payload slots continuously
- peer-close drains and reclaims both sides after `CITUS_REMOTE_BASEBACKUP_OBJECT_END`

## Verification Snapshot

Commands run after installing the modified Postgres and Citus/Homer trees:

```sh
make -j8 service-bin client-bin
sudo -n make install-service-bin
ninja -C build src/backend/postgres
sudo -n ninja -C build install
```

Local smoke command:

```sh
/usr/bin/time -p /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm -X none \
  -t 'homer:mode=blackhole,node=1,slots=64,bytes=1048576' -v
```

Observed local-smoke result:

- `pg_basebackup` completed successfully; after final reinstall/restart the repeated smoke run completed in `4.43s`.
- Service log reported `objects=25788`, `payload_bytes=23196101440`, and `last_archive=1` on the repeated run.
- Upstream `TARGET 'blackhole'` producer-only reference completed in `4.64s`. This is a PostgreSQL server-side discard path, not a Homer path.
- Normal libpq tar-to-stdout reference (`-D - -F t -X none > /dev/null`) completed in `10.11s` before the later restart and `10.05s` after the default-geometry patch/restart.

May 14, 2026 naming cleanup: the local Homer blackhole receiver was changed
from the confusing public spelling `host=blackhole` to `mode=blackhole`.
After rebuilding/reinstalling dbcomm and PostgreSQL and restarting the service,
the first post-restart local smoke run took `10.93s`, then warmed repeats took
`4.79s`, `4.59s`, and `4.35s`, matching the previous local blackhole band. The
old `host=blackhole` spelling now fails with an explicit hint to use
`mode=blackhole`.

The same rebuild was checked against the foreground pgbench Homer path to make
sure this basebackup-only naming change did not regress client SQL sessions.
After one cold/variance run at `12753 TPS`, three c4/j4/t20000 repeats completed
with zero failures at `15909 TPS`, `15827 TPS`, and `15368 TPS`, with p99 latency
between `0.416 ms` and `0.453 ms`. After the final formatting rebuild/install
and PostgreSQL restart, `mode=blackhole` completed in `4.55s`, and two
c4/j4/t20000 pgbench repeats completed with zero failures at `15271 TPS` and
`15359 TPS`, with p99 latency around `0.40 ms`.

These timings are only sanity references. The Homer run used local shared-memory blackhole validation, not remote RDMA, so it should not be used as the final performance comparison.

Two-service RDMA command used for the final default-geometry check:

```sh
/usr/bin/time -p /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm -X none \
  -t 'homer:host=10.10.1.101,port=9718,node=2' -v
```

Observed two-service RDMA results:

- With the original 64 x 1 MiB geometry, the run completed correctly but took `31.05s` to `31.77s`. The receiver log reported `objects=25788`, `payload_bytes=23196102464`, and `last_archive=1`.
- Coalescing receiver consumed-head publication by pump pass did not improve that result, which ruled out reverse consumed-head doorbells as the main bottleneck.
- With 32 x 4 MiB geometry, the same two-service RDMA path completed in `10.11s`.
- With 16 x 8 MiB geometry, it completed in `9.18s`; with 8 x 8 MiB geometry, it completed in `9.20s`.
- After changing the default to 8 x 8 MiB and rebuilding/restarting Postgres, the command without explicit `slots=` or `bytes=` completed in `9.28s`.
- The local service log for the default run showed `payload_ring_slot_count=8`, `payload_ring_slot_bytes=8388808`, and `published_tail=6466`; the remote service log reported `objects=6466` and `payload_bytes=23196106048`.

Follow-up transport fix: the shared RDMA payload substrate now batches local send-completion checkpoints as described in [rdma_publication_visibility_and_doorbells.md](../../../future-directions/citus/transport/rdma_publication_visibility_and_doorbells.md#payload-publish-pipelining-plan). After rebuilding and restarting both services:

- 64 x 1 MiB slots improved from `31.05s` to `10.14s`; the remote log reported `objects=25788` and `payload_bytes=23196112704`, or about `2288 MB/s`.
- Default 8 x 8 MiB slots improved from `9.28s` to `6.53s`; the remote log reported `objects=6466` and `payload_bytes=23196113216`, or about `3552 MB/s`.
- The same restarted-server libpq tar-to-stdout reference completed in `10.16s`, or about `2283 MB/s`.

After the final compile-time batch-size guard was added and the installed service binary was restarted again, the repeat measurements were:

- 64 x 1 MiB slots: `7.20s`, `objects=25788`, `payload_bytes=23196114752`, about `3222 MB/s`.
- Default 8 x 8 MiB slots: `6.50s`, `objects=6466`, `payload_bytes=23196115264`, about `3569 MB/s`.

The next transport checkpoint implemented the asynchronous posted/completed
frontier split described in
[rdma_publication_visibility_and_doorbells.md](../../../future-directions/citus/transport/rdma_publication_visibility_and_doorbells.md#implementation-checkpoint-asynchronous-postedcompleted-split).
The important details are that payload send completions are now tagged by sink
and semantic frontier, the sender can post ahead of the completed source-slot
frontier, and a larger QP/tail-source window lets the 64-slot 1 MiB experiment
keep the ring mostly full. After rebuilding, installing, and restarting both
service processes, the repeat measurements were:

- 64 x 1 MiB slots: `6.60s`, `objects=25788`, `payload_bytes=23196131136`, about `3515 MB/s`.
- Default 8 x 8 MiB slots: `6.01s`, `objects=6466`, `payload_bytes=23196131136`, about `3860 MB/s`.

Performance conclusion for this checkpoint: 1 MiB payload objects are no longer
pathologically slow once the service separates "posted to the NIC" from "safe
for backend source-slot reuse." The remaining 8 MiB-vs-1 MiB gap is now small
enough to treat as normal payload sizing / producer-overlap tuning, not as a
first-order correctness problem in the RDMA substrate.

The follow-up range-publication checkpoint implemented the batch publication
plan in
[rdma_publication_visibility_and_doorbells.md](../../../future-directions/citus/transport/rdma_publication_visibility_and_doorbells.md#implementation-checkpoint-range-publication-and-wr-chain-post).
The service now builds up to 16 generic payload-write descriptors and publishes
one remote tail doorbell for the whole contiguous semantic range. The RDMA
helper posts that range as one linked verbs work-request chain, preserving the
same typed-object sink abstraction above the transport layer.

After rebuilding, installing, and restarting both service processes on
May 12, 2026, the repeat measurements were:

- 64 x 1 MiB slots: first run after restart was `8.92s`, then warmed repeats
  were `5.08s`, `5.08s`, `5.06s`, and `4.25s`. Remote logs reported
  `objects=25788` and `payload_bytes` around `23196144xxx`, or roughly
  `4566-5458 MB/s` for the warmed runs.
- Default 8 x 8 MiB slots: repeats were `5.84s`, `5.87s`, `4.81s`, `4.82s`,
  and `4.72s`. Remote logs reported `objects=6466` and `payload_bytes` around
  `23196145xxx`, or roughly `3952-4914 MB/s`.

The result is directionally good but should be interpreted carefully: the
end-to-end `pg_basebackup` wall clock still includes PostgreSQL checkpoint and
cache effects, so the cold `8.92s` 1 MiB run is not a clean transport result.
The warmed measurements show that object-rate publication overhead was reduced
without introducing a basebackup-specific RDMA path.

## Current Milestone State

The current codebase is sufficient for the research milestone of an external
service-backed basebackup workload:

- `TARGET 'homer'` drives the normal PostgreSQL basebackup lifecycle while
  sending typed `BaseBackupArchiveStream` objects through Homer.
- The prototype intentionally supports the common tar/no-WAL/no-compression path
  and rejects unsupported wrappers before the hot path.
- The backend writes basebackup payload bytes directly into Homer queue slots;
  the service adds the transport header in the same registered slot before RDMA
  publication.
- The remote two-service blackhole path validates and counts basebackup semantic
  objects through the shared RDMA substrate.
- The shared RDMA substrate now uses posted/completed frontiers, a larger
  send-window geometry, range tail publication, and linked WR-chain posting.

This is not yet a complete backup receiver. It is a validated semantic and
transport milestone for the base archive stream. The next receiver milestone is
materializing the remote archive stream into a filesystem layout, not further
proving that the blackhole transport can carry the stream.

## Remaining Work

- Decide whether to keep or remove the local `mode=blackhole` smoke hook once two-node testing is routine.
- Materialize a receiver-side basebackup sink after the blackhole/counting receiver.
- Add WAL policy support only after the base archive stream path is stable.
- Generalize tuple-named queue/RDMA symbols into neutral names when practical; the payload substrate is already carrying non-tuple semantic objects.
- Move from same-host two-service RDMA testing to two physical nodes when the lab setup is ready; the semantic and transport code path should be the same, but the performance numbers should be remeasured.
- Add transport-only counters/timestamps only if later experiments need to
  separate pure RDMA publication throughput from end-to-end basebackup producer
  costs.
