# Homer frontend/service separation plan

## Scope

This note is the concrete refactor plan for separating the PostgreSQL/Citus-facing Homer frontend from the Homer service/backend before the DPU migration.

The desired end state is:

- PostgreSQL and Citus backend processes include one public frontend API header, `src/include/distributed/homer/homer_frontend.h`
- those backend processes link a separately identifiable Homer frontend component/library
- the Homer service compiles and runs on its own, today as the standalone host process and later on the DPU
- both sides include only shared fixed-width ABI headers for control messages, tuple-stream records, queue attachments, completion mailboxes, and object protocols
- the current POSIX shared-memory channel is isolated behind frontend SHM files so a later DMA channel can replace it without editing ordinary Citus call sites

This plan intentionally does **not** introduce a runtime transport vtable in the first pass. The first pass should be mechanical: move functions into better files, update includes, and update the build boundary. A `HomerFrontendTransportOps`-style vtable can be introduced later only if SHM and DMA need to coexist in one binary or be selected at runtime.

## Implementation progress

### 2026-06-27: Remote RDMA acceptance complete after lane-0 MTU correction

Remote validation is now complete for the frontend/backend separation refactor. The apparent RDMA-CM/GID blocker was resolved by fixing a host netdev MTU mismatch on the tested lane-0 path, not by changing Homer code.

Corrected environment facts:

- Host-side Homer validation uses the BF3 integrated host functions `mlx5_0`/`mlx5_1`, not the DPU-OS `mlx5_2`/`mlx5_3` devices. On farnet1 host, `mlx5_0` maps to `enp33s0f0np0` with `10.10.1.101`; on farnet0 host, `mlx5_0` maps to `enp33s0f0np0` with `10.10.1.100`.
- The lane-0 RDMA-CM failure was reproduced while farnet1 `enp33s0f0np0` had MTU `9000` and farnet0 `enp33s0f0np0` had MTU `1500`.
- Setting farnet0 lane 0 to MTU `9000` made the same RDMA-CM perftest command pass. This is runtime network state and must be made persistent if the host may reboot or the network service may reset the interface.

RDMA-CM sanity evidence after the MTU correction:

```bash
ssh farnet0 'sudo -n ip link set dev enp33s0f0np0 mtu 9000'

ib_read_bw -R -d mlx5_0 -i 1 -F --report_gbits -s 1048576 -D 5 \
  -p 18560 --bind_source_ip 10.10.1.101 10.10.1.100
# completed successfully: client_rc=0 server_rc=0
# perftest reported rdma_cm QPs ON, Ethernet link, MTU 4096[B],
# farnet1 GID index 4, farnet0 GID index 3, and about 237.51 Gbit/s average
```

Remote workload acceptance evidence after install-prefix sync and clean process baseline:

```bash
# Remote pgbench c1 smoke from farnet0 to farnet1 through Homer/RDMA.
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer --homer-database-oid 5 --homer-user-oid 10 \
  --homer-peer-host 10.10.1.101 --homer-peer-port 9717 --homer-peer-node 1 \
  --latency-percentiles --client-cpu=3 \
  -n -M simple -c 1 -j 1 -t 2000 postgres"
# completed: 2000/2000 transactions, 0 failures

# Warmed remote pgbench c1.
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer --homer-database-oid 5 --homer-user-oid 10 \
  --homer-peer-host 10.10.1.101 --homer-peer-port 9717 --homer-peer-node 1 \
  --latency-percentiles --client-cpu=3 \
  -n -M simple -c 1 -j 1 -t 20000 postgres"
# completed: 20000/20000 transactions, 0 failures, about 4104.75 TPS, p99 0.265 ms

# Remote pgbench c4 correctness/scaling smoke.
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5432 -U dbcomm \
  --homer --homer-database-oid 5 --homer-user-oid 10 \
  --homer-peer-host 10.10.1.101 --homer-peer-port 9717 --homer-peer-node 1 \
  --latency-percentiles --client-cpus=8,9,10,11 \
  -n -M simple -c 4 -j 4 -t 10000 postgres"
# completed: 40000/40000 transactions, 0 failures, about 10134.20 TPS, p99 0.589 ms

# Remote RDMA basebackup to the farnet0 Homer service.
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5432 -U dbcomm \
  -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2,slots=8,bytes=8388608' \
  -v
# run 1 completed, real 6.22; warmed run 2 completed, real 4.14
```

Post-run log scan result:

- both Homer service logs had no `status=12`, transport retry, fatal, panic, or assertion lines after the corrected run
- PostgreSQL still logged repeated libpq/Citus warnings for `dbcomm@10.10.1.100:5432`; those are expected in this validation shape because farnet0 PostgreSQL was not running, only the farnet0 Homer service was required

Current acceptance conclusion:

- The mechanical frontend/backend separation passed static checks, build/install checks, local pgbench, local basebackup blackhole, remote RDMA pgbench, and remote RDMA basebackup.
- The main operational caveat is outside the refactor: lane-0 MTU must remain consistent across farnet1/farnet0 for RDMA-CM/Homer validation. The runbook should treat `farnet0 enp33s0f0np0 mtu 9000` as required when farnet1 lane 0 is `9000`.

### 2026-06-27: Superseded RDMA-CM diagnostic before MTU correction

Follow-up diagnostic after the remote pgbench failure narrowed the blocker to the rdma_cm path rather than the frontend/backend refactor itself:

- Homer's RDMA transport uses `rdma_getaddrinfo`, `rdma_create_id`, and `rdma_resolve_addr` for outgoing peer connections in `remote_execution_peer_transport_rdma.c`, and `rdma_bind_addr`/`rdma_listen` for the service listener. It does not expose a service environment knob for GID index or RoCE version selection.
- Plain perftest RC without rdma_cm can pass on lane 0 with GID index 2, but perftest with `-R` reproduces the same failure class as Homer: `transport retry counter exceeded (12)`.
- The local librdmacm headers expose `rdma_set_option` choices for TOS, reuseaddr, AF-only, and ACK timeout, but not a per-connection GID-index option. The host-level RDMA-CM RoCE-mode knob is `cma_roce_mode`; it currently reports `RoCE v2` for `mlx5_0`/`mlx5_1` on both hosts.

Evidence:

```bash
ib_read_bw -R -d mlx5_0 -i 1 -F --report_gbits -s 1048576 -D 5   -p 18553 --bind_source_ip 10.10.1.101 10.10.1.100
# failed: Completion with error at client; Failed status-transport retry counter exceeded (12)
# perftest reported rdma_cm QPs ON and GID index 4 on farnet1

grep -R "ROCE_MODE\|RDMA_OPTION_ID\|rdma_set_option" /usr/include/rdma /data/dbcomm/pg-citus/include 2>/dev/null
# showed RDMA_OPTION_ID_TOS, RDMA_OPTION_ID_REUSEADDR, RDMA_OPTION_ID_AFONLY, and RDMA_OPTION_ID_ACK_TIMEOUT only

sudo -n cma_roce_mode -d mlx5_0 -p 1
sudo -n cma_roce_mode -d mlx5_1 -p 1
ssh farnet0 "sudo -n cma_roce_mode -d mlx5_0 -p 1; sudo -n cma_roce_mode -d mlx5_1 -p 1"
# all four checks reported RoCE v2
```


Follow-up controlled RoCE-mode test:

```bash
sudo -n cma_roce_mode -d mlx5_0 -p 1 -m 1
ssh farnet0 "sudo -n cma_roce_mode -d mlx5_0 -p 1 -m 1"
# both hosts reported IB/RoCE v1

ib_read_bw -R -d mlx5_0 -i 1 -F --report_gbits -s 1048576 -D 5   -p 18555 --bind_source_ip 10.10.1.101 10.10.1.100
# still failed with transport retry counter exceeded (12); perftest still reported GID index 4

sudo -n cma_roce_mode -d mlx5_0 -p 1 -m 2
ssh farnet0 "sudo -n cma_roce_mode -d mlx5_0 -p 1 -m 2"
# restored both lane-0 ports to RoCE v2
```

This weakens the earlier hypothesis that simply switching RDMA-CM to RoCE v1 would restore the path. The current failure is still rdma_cm-specific compared with non-CM `ib_read_bw -x 2`, but not solved by toggling `cma_roce_mode` for lane 0.

Superseded conclusion:

- This diagnostic correctly showed that changing the frontend/backend split was the wrong lever, but it misidentified the remaining cause as unresolved rdma_cm/RoCE behavior. The later clean check found a lane-0 host MTU mismatch: farnet1 `enp33s0f0np0` was MTU `9000` while farnet0 `enp33s0f0np0` was MTU `1500`.
- After setting farnet0 lane 0 to MTU `9000`, RDMA-CM perftest and the remote Homer workloads passed. Keep this section as failed-diagnostic history, not as the current acceptance state.

### 2026-06-27: Static and local runtime validation complete; remote RDMA initially blocked

Completed in `/data/dbcomm/citus-dbcomm` and installed prefix `/data/dbcomm/pg-citus`:

- installed the rebuilt Citus/Homer artifacts with `sudo -n -u dbcomm make install-headers install-service-bin install`
- verified installed `citus.so` depends on `libpq` but not `librdmacm` or `libibverbs`
- verified installed `citus_tuple_sink_service` still depends on `librdmacm` and `libibverbs`
- verified `pgbench`, `citus_tuple_sink_service`, and `libhomer_client.a` all contain `/citus_remote_execution_control_v27`
- passed the plan's header, frontend implementation, ABI, and build-boundary checks after removing one `TupleDesc` type-name mention from an ABI-header comment
- passed local `pgbench --homer` smoke on farnet1: 1000/1000 transactions, 0 failures, average latency 0.163 ms, about 6123 TPS without initial connection time
- passed local Homer basebackup blackhole smoke on farnet1 with `pg_basebackup -X none -t 'homer:mode=blackhole,node=1'`; elapsed real time was about 4.02 seconds

Validation commands and evidence:

```bash
cd /data/dbcomm/citus-dbcomm
readelf -d /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so | rg "NEEDED|rdma|ibverbs|libpq"
# expected/current: libpq present; librdmacm and libibverbs absent

readelf -d /data/dbcomm/pg-citus/bin/citus_tuple_sink_service | rg "NEEDED|rdma|ibverbs|libpq"
# expected/current: standalone service has librdmacm and libibverbs

strings /data/dbcomm/pg-citus/bin/pgbench | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/bin/citus_tuple_sink_service | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/libhomer_client.a | grep citus_remote_execution_control
# expected/current: all report /citus_remote_execution_control_v27

rg -n 'distributed/homer/remote_execution_session\.h|distributed/homer/tuple_sink_service\.h' src
# expected/current: no results

rg -n "shm_open|mmap|munmap|ftruncate" src/backend/distributed/utils/homer/homer_frontend.c
rg -n "rdma_|ibv_" src/backend/distributed/utils/homer/homer_frontend*.c
# expected/current: no results

grep -R "postgres.h\|executor/tuptable.h\|utils/rel.h\|Datum\|TupleDesc\|TupleTableSlot" src/include/distributed/homer/homer_*_abi.h
# expected/current: no results

sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench   -h /tmp -p 5432 -U dbcomm   --homer --homer-database-oid 5 --homer-user-oid 10   --latency-percentiles --client-cpu=3   -n -M simple -c 1 -j 1 -t 1000 postgres
# completed: 1000/1000 transactions, 0 failures, latency average 0.163 ms, tps 6123.023794

/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup   -h /tmp -p 5432 -U dbcomm -X none -c fast   -t 'homer:mode=blackhole,node=1' -v
# completed; real 4.02
```

Remote RDMA validation blocker:

```text
remote pgbench farnet0 -> farnet1 failed after session open during sql_execute:
pgbench: error: client 0 Homer sql_execute failed: Homer control request failed

farnet1 service log:
tuple-sink service: requesting outgoing peer transport reset host=10.10.1.100 traffic_class=2 after send-CQ drain failure detail=async send completion failed: opcode=0 status=12 wr_id=2305843009213759488
tuple-sink service: async local open failed phase=10 request=1 detail=peer async op connection became inactive host=

farnet0 service log:
tuple-sink service: peer transport host=10.10.1.101 delivered CM event=DISCONNECTED status=0, resetting connection
```

The low-level RDMA check showed current environment drift from the runbook's earlier GID-index assumption:

```bash
# runbook-style lane 0 GID index 3 failed in this run
ib_read_bw -d mlx5_0 -i 1 -x 3 -F --report_gbits -s 1048576 -D 5   -p 18550 --bind_source_ip 10.10.1.101 10.10.1.100
# failed with: Found Incompatibility issue with GID types. Please Try to use a different IP version.

# lane 0 GID index 2 passed basic RC RDMA read
ib_read_bw -d mlx5_0 -i 1 -x 2 -F --report_gbits -s 1048576 -D 5   -p 18552 --bind_source_ip 10.10.1.101 10.10.1.100
# completed, but only about 0.53 Gbit/s in this untuned diagnostic run
```

Current GID snapshot:

```text
farnet1 mlx5_0/enp33s0f0np0: 10.10.1.101 has IPv4 RoCE v1 at index 2 and IPv4 RoCE v2 at index 4.
farnet0 mlx5_0/enp33s0f0np0: 10.10.1.100 has IPv4 RoCE v1 at index 2 and IPv4 RoCE v2 at index 3.
```

Superseded caveat:

- Treat the failed remote pgbench attempts in this subsection as diagnostic-only and do not use them for performance claims. The later MTU correction resolved the remote RDMA path and the acceptance workloads passed.
- The static, build, install, local pgbench, local basebackup, remote pgbench, and remote basebackup acceptance state is recorded in the latest implementation-progress entry above.

### 2026-06-27: Phase 9 build boundary split complete

Completed in `/data/dbcomm/citus-dbcomm`:

- updated `src/backend/distributed/Makefile` to define explicit `HOMER_FRONTEND_OBJS` and `HOMER_SERVICE_OBJS` groups
- filtered `HOMER_SERVICE_OBJS` out of the extension `OBJS`, so `tuple_sink_service_process.o` and `remote_execution_peer_transport_rdma.o` no longer link into `citus.so`
- restored the extension `SHLIB_LINK` to `$(libpq)` plus the normal Citus/PostgreSQL libraries, removing the temporary `-lrdmacm -libverbs` dependency from ordinary PostgreSQL backend linkage
- kept the standalone `citus_tuple_sink_service` build in the top-level `Makefile` linked with RDMA libraries, preserving service-side RDMA support

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
make -pn -C src/backend/distributed all | rg "^(HOMER_FRONTEND_OBJS|HOMER_SERVICE_OBJS|OBJS|SHLIB_LINK) ="
# expected/current: HOMER_SERVICE_OBJS contains tuple_sink_service_process.o and remote_execution_peer_transport_rdma.o; OBJS filters HOMER_SERVICE_OBJS out of DIST_EXTENSION_OBJS; SHLIB_LINK has libpq but not RDMA libraries

sudo -n -u dbcomm rm -f src/backend/distributed/citus.so
sudo -n -u dbcomm make -C src/backend/distributed citus.so
# completed successfully; printed link command included Homer frontend objects and did not include tuple_sink_service_process.o, remote_execution_peer_transport_rdma.o, -lrdmacm, or -libverbs

readelf -d src/backend/distributed/citus.so | rg "NEEDED|rdma|ibverbs|libpq"
# expected/current: libpq is present; librdmacm and libibverbs are absent

nm -D src/backend/distributed/citus.so | rg "rdma_|ibv_|HomerPeer"
# expected/current: no results

readelf -d build/homer/citus_tuple_sink_service | rg "NEEDED|rdma|ibverbs|libpq"
# expected/current: standalone service still depends on librdmacm and libibverbs

sudo -n -u dbcomm make -j8 service-bin client-bin
sudo -n -u dbcomm make -j8
# completed successfully; top-level targets were already up to date after the direct relink

git diff --check -- src/backend/distributed/Makefile
# completed successfully
```

Caveat / plan correction:

- Phase 9 did not build a separate `libhomer_frontend.a`. The frontend component is now explicit as `HOMER_FRONTEND_OBJS` and still links as part of `citus.so`, which is enough for the first mechanical boundary split. A real archive/library can be added later if another binary needs to link the PostgreSQL-facing frontend outside the extension.
- A normal top-level `make -j8` was not enough to prove the changed `citus.so` dynamic dependencies because the existing shared library was already up to date. The validation therefore removed and relinked only the generated `src/backend/distributed/citus.so` as `dbcomm`, then checked `readelf` and `nm` directly.

### 2026-06-27: Phase 8 tuple-view codec extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- created `src/backend/distributed/utils/homer/homer_tuple_view_codec.h` and `src/backend/distributed/utils/homer/homer_tuple_view_codec.c` for PostgreSQL-dependent tuple-view row encoding and decoding
- created private implementation glue header `src/backend/distributed/utils/homer/homer_tuple_queue_internal.h` for queue/batch handle structs and internal checks shared between the queue frontend and tuple-view codec
- moved tuple-row normalization, sizing, encoding, and decoding helpers out of `homer_tuple_queue_frontend.c`, including `SetTupleSinkNonNullBit`, `TupleSinkAttributeUsesLengthPrefix`, `NormalizeTupleSinkAttribute`, `TupleSinkTupleBytes`, `DecodeTupleViewFromTupleSinkRow`, `CopyOutTupleViewFromTupleSinkRow`, and `BorrowTupleViewFromTupleSinkRow`
- moved codec-facing batch helpers into `homer_tuple_view_codec.c`: `TryAppendTupleViewToCitusTupleSinkBatch`, `BorrowNextTupleViewFromCitusTupleSinkBatch`, and `CopyOutNextTupleViewFromCitusTupleSinkBatch`
- kept byte-ring reservation/publication, queue map/unmap, terminal state handling, public queue diagnostics, and batch borrow lifetime bookkeeping in `homer_tuple_queue_frontend.c`

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n "SetTupleSinkNonNullBit|NormalizeTupleSinkAttribute|TupleSinkTupleBytes|DecodeTupleViewFromTupleSinkRow|BorrowTupleViewFromTupleSinkRow|TryAppendTupleViewToCitusTupleSinkBatch|BorrowNextTupleViewFromCitusTupleSinkBatch|CopyOutNextTupleViewFromCitusTupleSinkBatch|PG_DETOAST_DATUM_PACKED|datumCopy|store_att_byval|fetch_att" src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c src/backend/distributed/utils/homer/homer_tuple_view_codec.c src/backend/distributed/utils/homer/homer_tuple_queue_internal.h src/backend/distributed/utils/homer/homer_tuple_view_codec.h
# expected/current: codec and Datum helpers live in homer_tuple_view_codec.c; the queue frontend only keeps the high-level append call site

rg -n "shm_open|mmap|munmap|ftruncate|remote_execution_control|peer_transport|rdma_|ibv_" src/backend/distributed/utils/homer/homer_tuple_view_codec.c src/backend/distributed/utils/homer/homer_tuple_view_codec.h
# expected/current: no results

sudo -n -u dbcomm make -j8
# first run exposed that the split codec needed a direct fmgr.h include for PG_DETOAST_DATUM_PACKED
# after adding fmgr.h, the build completed successfully

git diff --check -- src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c src/backend/distributed/utils/homer/homer_tuple_queue_internal.h src/backend/distributed/utils/homer/homer_tuple_view_codec.c src/backend/distributed/utils/homer/homer_tuple_view_codec.h
# completed successfully
```

Caveat / plan correction:

- `BuildTupleViewContractFromTupleDesc` remains in `homer_frontend_control.c`, where Phase 5 placed it, because it builds the `OPEN_SESSION` control-request tuple contract from PostgreSQL metadata. Phase 8 split only the tuple-row codec from the queue frontier mechanics. A later small contract-helper split can move this builder if we want all tuple-shape construction in one file, but that was not necessary for this mechanical pass.
- `homer_tuple_queue_internal.h` is private frontend implementation glue, not an installed/public API. It exists so whole codec functions can move mechanically while still accessing opaque queue/batch handle fields.
- `AppendTupleViewToCitusTupleSinkBatch` remains in `homer_tuple_queue_frontend.c` as the hard-fail public wrapper with queue capacity diagnostics; the actual row encoding is now in `TryAppendTupleViewToCitusTupleSinkBatch` in `homer_tuple_view_codec.c`.
- `ReleaseBorrowedTupleViewFromCitusTupleSinkBatch` remains queue-owned because it mutates batch borrow lifetime state rather than decoding tuple row bytes.

### 2026-06-27: Phase 7 tuple queue frontend rename complete

Completed in `/data/dbcomm/citus-dbcomm`:

- renamed `src/include/distributed/homer/tuple_sink_service.h` to `src/include/distributed/homer/homer_tuple_queue_frontend.h`
- renamed `src/backend/distributed/utils/homer/tuple_sink_service.c` to `src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c`
- updated direct include users to the new queue-frontend header name and removed the old `distributed/homer/tuple_sink_service.h` include from ordinary backend call sites
- moved frontend-visible prototype GUC extern declarations (`EnableExperimentalTupleSinkRouting`, `EnableExperimentalTupleSinkLoopbackValidation`, `ExperimentalTupleSinkSlotCapacityBytes`, `ExperimentalTupleSinkBatchTupleTarget`, and `ExperimentalTupleSinkUseExplicitBatchApi`) into public `homer_frontend.h`
- kept `tuple_sink_service_process.c` untouched; it remains the standalone service process and is not part of the Phase 7 frontend queue rename

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n "distributed/homer/tuple_sink_service\.h|tuple_sink_service\.h" src
# expected/current: no results

rg -n "distributed/homer/homer_tuple_queue_frontend\.h|homer_tuple_queue_frontend\.c|EnableExperimentalTupleSinkRouting" src/backend/distributed src/include/distributed/homer
# expected/current: queue substrate users include homer_tuple_queue_frontend.h; frontend GUC externs are declared in homer_frontend.h

sudo -n -u dbcomm make -j8
# first run exposed remote_execution_backend_bridge.c still used frontend GUC externs through the old queue header
# after adding homer_frontend.h there, the build completed successfully

git diff --check -- src/include/distributed/homer/homer_frontend.h src/include/distributed/homer/homer_tuple_queue_frontend.h src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c src/backend/distributed/commands/multi_copy.c src/backend/distributed/shared_library_init.c src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_citus_policy.c src/backend/distributed/utils/homer/homer_frontend_internal.h src/backend/distributed/utils/homer/remote_execution_backend_bridge.c
# completed successfully
```

Caveat / plan correction:

- `remote_execution_backend_bridge.c` is backend-side helper code that uses frontend-visible Homer GUC variables while also using queue substrate APIs. It now includes both `homer_frontend.h` and `homer_tuple_queue_frontend.h`; this keeps GUC declarations on the public frontend API boundary while preserving direct queue-substrate access where the bridge still needs it.
- `CitusTupleSinkEnabled` remains declared in `homer_tuple_queue_frontend.h` for now because it is implemented by the queue substrate and used by `homer_citus_policy.c` to implement `RemoteExecutionSessionPrototypeEnabled`. If later we want all policy gates to avoid the queue substrate header, add a frontend-policy wrapper instead of re-exporting queue internals.

### 2026-06-27: Phase 6 Citus policy and transaction glue extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- created `src/backend/distributed/utils/homer/homer_citus_policy.h` and `src/backend/distributed/utils/homer/homer_citus_policy.c` for host-side Citus/PostgreSQL policy and spec helpers
- moved public session/operation/command spec initializers, open-spec string formatting, and frontend feature gates out of `homer_frontend.c` into `homer_citus_policy.c`
- created `src/backend/distributed/utils/homer/homer_citus_xact.h` and `src/backend/distributed/utils/homer/homer_citus_xact.c` for transaction-finalization lifecycle glue
- moved `RemoteExecutionTxFinalizationEntry`, `RemoteExecutionTxFinalizationConnectionOrdinal`, `RemoteExecutionTxFinalizationList`, `DisarmRemoteExecutionTxFinalizationEntry`, `EnsureRemoteExecutionSessionReadyForFinalization`, `StartAndWaitRemoteExecutionTransactionCommand`, `RegisterRemoteExecutionSessionForTransactionFinalization`, `RemoteExecutionSessionsPrepareAtPreCommit`, `RemoteExecutionSessionsCommitAtCommit`, and `RemoteExecutionSessionsAbortAtAbort` into `homer_citus_xact.c`
- kept `RemoteExecutionControlRequestSequence` in `homer_frontend_shm.c`, matching the plan's request-correlation boundary

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n "InitRemote|RemoteExecutionSessionOpenSpecToString|RemoteExecutionSessionPrototypeEnabled|RemoteExecutionSessionLoopbackValidationEnabled|RegisterRemoteExecutionSessionForTransactionFinalization|RemoteExecutionSessionsPrepareAtPreCommit|RemoteExecutionSessionsCommitAtCommit|RemoteExecutionSessionsAbortAtAbort|RemoteExecutionTxFinalization" src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_citus_policy.c src/backend/distributed/utils/homer/homer_citus_xact.c
# expected/current: homer_frontend.c has only call sites; policy definitions live in homer_citus_policy.c and transaction-finalization state/callbacks live in homer_citus_xact.c

rg -n "shm_open|mmap|munmap|ftruncate" src/backend/distributed/utils/homer/homer_citus_policy.c src/backend/distributed/utils/homer/homer_citus_xact.c src/backend/distributed/utils/homer/homer_frontend.c
# expected/current: no results

sudo -n -u dbcomm make -j8
# first run exposed that TryConsumeRemoteExecutionPeerCommandCompletion was swept into the policy file by an over-broad extraction range
# after moving that peer-ring consumer back to homer_frontend.c, the build completed successfully

git diff --check -- src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_citus_policy.c src/backend/distributed/utils/homer/homer_citus_policy.h src/backend/distributed/utils/homer/homer_citus_xact.c src/backend/distributed/utils/homer/homer_citus_xact.h src/backend/distributed/utils/homer/homer_frontend_internal.h
# completed successfully
```

Caveat / plan correction:

- `TryConsumeRemoteExecutionPeerCommandCompletion` remains in `homer_frontend.c`. It consumes peer completion ring events and updates `RemoteExecutionSessionData` command-tracking fields; it is not Citus policy glue even though it sits near string/spec helpers in the current file order. Moving it into policy caused compile-time evidence of the wrong boundary, so it was moved back mechanically.
- `RemoteExecutionCommandKindName` and `RemoteExecutionCommandCompletedSuccessfully` are now declared in private `homer_frontend_internal.h` because `homer_citus_xact.c` needs them for transaction-finalization logging and validation. They remain private implementation helpers, not public frontend API.

### 2026-06-27: Phase 5 control request-builder extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- created `src/backend/distributed/utils/homer/homer_frontend_control.h` and `src/backend/distributed/utils/homer/homer_frontend_control.c` as the private frontend semantic control-request builder layer
- moved service request builders out of `homer_frontend.c`: `OpenTupleSinkSessionThroughLocalService`, `OpenCommandSessionThroughLocalService`, `CloseTupleSinkSessionThroughLocalService`, `CloseRemoteExecutionCompatibilitySessionThroughLocalService`, `ReportTupleSinkSessionPostCommandStateThroughLocalService`, `StartRemoteExecutionCommandThroughLocalService`, and `PollRemoteExecutionCommandCompletionThroughLocalService`
- moved fixed-width control conversion and command-completion conversion helpers into `homer_frontend_control.c`, including `RemoteExecutionControlDirectionCode`, `RemoteExecutionControlCommandKindCode`, `RemoteExecutionCommandKindFromControl`, `RemoteExecutionCommandStateFromControl`, `RemoteExecutionPostCommandStateFromControl`, `RemoteExecutionFillCommandCompletion`, and `RemoteExecutionControlPostCommandStateCode`
- moved the service-open supporting builders `BuildControlSessionKeyFromIntent`, `BuildPeerEndpointFromIntent`, `BuildTupleViewContractFromTupleDesc`, `BuildPlacementAccessDescriptorFromOperation`, and `BuildTupleSinkKeyFromOperation`
- kept public session/operation/command spec initializers, string formatting, feature gates, transaction-finalization callbacks, and tuple queue send/receive APIs in `homer_frontend.c`; those are Phase 6 and later targets

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n "shm_open|mmap|munmap|ftruncate" src/backend/distributed/utils/homer/homer_frontend_control.c src/backend/distributed/utils/homer/homer_frontend.c
# expected/current: no results

rg -n "CloseTupleSinkSessionThroughLocalService|CloseRemoteExecutionCompatibilitySessionThroughLocalService|ReportTupleSinkSessionPostCommandStateThroughLocalService|StartRemoteExecutionCommandThroughLocalService|PollRemoteExecutionCommandCompletionThroughLocalService" src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_frontend_control.c src/backend/distributed/utils/homer/homer_frontend_control.h
# expected/current: homer_frontend.c has call sites only; definitions live in homer_frontend_control.c and declarations in homer_frontend_control.h

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 5 state; homer_frontend_control.o linked into citus.so through the existing utils/homer wildcard object rule

git diff --check -- src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_frontend_control.c src/backend/distributed/utils/homer/homer_frontend_control.h
# completed successfully
```

Caveat / plan correction:

- `ValidateSqlCommandSessionOpenSpec`, `ValidateTupleSinkSessionOpenSpec`, and `RemoteExecutionOpKindName` moved with the control builder layer because the moved builders still call them and the remaining high-level formatter/open path still needs them. This is a mechanical dependency move, not a semantic redesign.
- `RemoteExecutionSessionOpenSpecToString` and the public `InitRemote*Spec` helpers still live in `homer_frontend.c` even though they are policy/spec glue. They should move in Phase 6 with the Citus policy split rather than being mixed into the control request-builder file.

### 2026-06-27: Phase 4 SHM frontend channel extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- created `src/backend/distributed/utils/homer/homer_frontend_shm.h` and `src/backend/distributed/utils/homer/homer_frontend_shm.c` as the private frontend POSIX-SHM channel layer
- moved local control-region mapping, unmapping, control-slot reservation, request publication, response waiting, response-header checking, atomic control helpers, and peer command-completion ring map/unmap helpers out of `homer_frontend.c`
- created `src/backend/distributed/utils/homer/homer_frontend_internal.h` for private session/batch structs shared by split frontend implementation files while keeping public `homer_frontend.h` opaque
- left the high-level `Open*ThroughLocalService`, `Start*ThroughLocalService`, and `Poll*ThroughLocalService` request builders in `homer_frontend.c`; those are the Phase 5 control-request-builder extraction target

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
grep -n "shm_open\|mmap\|munmap\|ftruncate" src/backend/distributed/utils/homer/homer_frontend.c
# expected/current: no results

rg -n "shm_open|mmap|munmap|ftruncate" src/backend/distributed/utils/homer/homer_frontend_shm.c
# expected/current: POSIX SHM syscalls are isolated in homer_frontend_shm.c

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 4 state; homer_frontend_shm.o linked into citus.so through the existing utils/homer wildcard object rule

git diff --check -- src/backend/distributed/utils/homer/homer_frontend.c src/backend/distributed/utils/homer/homer_frontend_shm.c src/backend/distributed/utils/homer/homer_frontend_shm.h src/backend/distributed/utils/homer/homer_frontend_internal.h
# completed successfully
```

Caveat / plan correction:

- Moving peer completion ring map/unmap helpers requires access to private fields in `RemoteExecutionSessionData`. The current implementation uses a private `homer_frontend_internal.h` header instead of exposing those fields through the installed/public `homer_frontend.h`. This keeps backend call sites opaque while allowing frontend implementation files to split mechanically.
- `TryConsumeRemoteExecutionPeerCommandCompletion` still lives in `homer_frontend.c` because it converts ring events into the public `RemoteExecutionCommandCompletion` shape and uses command-state tracking. It has no POSIX `shm_open`/`mmap`/`munmap` calls after Phase 4; moving it can be revisited when Phase 5 extracts command/completion conversion helpers.

### 2026-06-27: Phase 3i SHM control-region ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_shm_channel_abi.h` from a POSIX SHM name/prefix header into the owner of the current local POSIX-SHM control-channel ABI
- moved `CitusRemoteExecControlSlotState`, `CitusRemoteExecControlSlot`, `CitusRemoteExecReadyBitmapLine`, and `CitusRemoteExecControlRegion` out of `remote_execution_control_protocol.h` into `homer_shm_channel_abi.h`
- reduced `remote_execution_control_protocol.h` to a temporary compatibility wrapper that includes `homer_shm_channel_abi.h`
- removed `homer_shm_channel_abi.h` includes from `homer_queue_abi.h` and `homer_control_abi.h` so the ABI dependency direction remains acyclic: SHM channel depends on semantic control, semantic control depends on completion and queue declarations, queue depends on tuple/version declarations

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n 'CitusRemoteExecControlSlotState|CitusRemoteExecControlSlot|CitusRemoteExecReadyBitmapLine|CitusRemoteExecControlRegion' src/include/distributed/homer
# expected/current: local control-region declarations are owned by homer_shm_channel_abi.h; public client state may still reference CitusRemoteExecControlRegion through the compatibility wrapper

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 3i state
```

Caveat / plan correction:

- Direct local-SHM users still include `remote_execution_control_protocol.h` in a few places. That is intentional for this mechanical slice: it keeps old include names working while the concrete owner is now `homer_shm_channel_abi.h`. A later include audit should switch direct local-SHM users to `homer_shm_channel_abi.h` and then delete the compatibility wrapper.
- `homer_shm_channel_abi.h` must not be included by lower semantic ABI headers such as `homer_queue_abi.h` or `homer_control_abi.h`. Otherwise the split reintroduces a dependency cycle between local channel layout and semantic request/response records.

### 2026-06-27: Phase 3h semantic control ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_control_abi.h` from a Phase 3a adapter into the owner of semantic control-plane request/response messages, operation/session intent constants, command constants, command specs, and request/response unions
- moved semantic control declarations out of `remote_execution_control_protocol.h` while leaving local POSIX-SHM slot state, control slot, ready bitmap line, and control region in `remote_execution_control_protocol.h` for the next SHM-channel slice
- kept `remote_execution_control_protocol.h` as the compatibility/local-SHM header and changed it to include `homer_control_abi.h`
- changed direct local-control users that manipulate `CitusRemoteExecControlRegion` / `CitusRemoteExecControlSlot` to include `remote_execution_control_protocol.h` explicitly: `remote_execution_client.h`, `homer_frontend.c`, `remote_execution_backend_bridge.c`, and `tuple_sink_service_process.c`

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n 'typedef enum CitusRemoteExecControl(RequestKind|StatusCode|OpKind)|typedef struct CitusRemoteExec(SessionKey|PlacementAccessDescriptor|PeerEndpoint|.*CommandSpec|ControlRequestHeader|ControlResponseHeader|OpenSessionRequest|OpenSessionResponse|CloseSessionRequest|CloseSessionResponse|ReportPostCommandStateRequest|ReportPostCommandStateResponse|StartCommandRequest|StartCommandResponse|PollCommandCompletionRequest|PollCommandCompletionResponse)|typedef union CitusRemoteExecControl(RequestUnion|ResponseUnion)|#define CITUS_REMOTE_EXEC_(OP_|ACCESS_|TX_|FRESHNESS_|OWNERSHIP_|LANE_|METADATA_POLICY_|SCOPE_|POST_COMMAND_STATE_|COMMAND_|SQL_RESULT_|COMMAND_STATE_)' src/include/distributed/homer
# expected/current: semantic control declarations are owned by homer_control_abi.h

rg -n 'CitusRemoteExecControlSlotState|CitusRemoteExecControlSlot|CitusRemoteExecReadyBitmapLine|CitusRemoteExecControlRegion' src/include/distributed/homer
# expected/current for Phase 3h: local SHM control declarations remain in remote_execution_control_protocol.h

sudo -n -u dbcomm make -j8
# first run exposed local-SHM users that had relied on homer_control_abi.h transitivity
# after adding explicit remote_execution_control_protocol.h includes to local-SHM users, the build completed successfully
```

Caveat:

- Public semantic users should include `homer_control_abi.h`; local SHM channel users must still include `remote_execution_control_protocol.h` until `CitusRemoteExecControlSlotState`, `CitusRemoteExecControlSlot`, `CitusRemoteExecReadyBitmapLine`, and `CitusRemoteExecControlRegion` move into `homer_shm_channel_abi.h`.
- `remote_execution_control_protocol.h` is now primarily a local-SHM compatibility wrapper, not the owner of semantic control messages.

### 2026-06-27: Phase 3g completion ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_completion_abi.h` from a Phase 3a adapter into the owner of command-completion records, result descriptor side tables, frontend completion mailboxes, peer command-completion rings, and pure completion/descriptor helper inlines
- moved `CITUS_REMOTE_EXEC_RESULT_FLAG_*`, `CitusRemoteExecCommandCompletion`, compact completion records, result descriptor records, completion mailbox/ring records, and the completion helper inlines out of `remote_execution_control_protocol.h`
- changed `remote_execution_control_protocol.h` to include `homer_completion_abi.h` for compatibility before its start/poll response structs reference `CitusRemoteExecCommandCompletion`
- added `homer_completion_abi.h` to the standalone service/client Makefile dependency lists

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n '^#define CITUS_REMOTE_EXEC_RESULT_FLAG|^#define HOMER_COMPLETION_INLINE_DETAIL_BYTES|^#define HOMER_RESULT_DESCRIPTOR_SLOTS|^#define CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS|^#define CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_RING_SLOTS|typedef (struct|enum) (CitusRemoteExecCommandCompletion|CitusRemoteExecCommandCompletionHot|CitusRemoteExecClientCompletionSeal|CitusRemoteExecClientCompletionSlot|HomerResultDescriptorSlot|HomerResultDescriptorTable|HomerResultDescriptorReadStatus|CitusRemoteExecClientCompletionMailbox|CitusRemoteExecPeerCommandCompletionEvent|CitusRemoteExecPeerCommandCompletionRing)' src/include/distributed/homer
# expected/current: completion records and mailbox/ring declarations are owned by homer_completion_abi.h

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 3g state
```

Caveat:

- `CitusRemoteExecPeerCommandCompletionRingDescriptor` remains in `remote_execution_control_protocol.h` for now. It is an open-session response descriptor naming the requester-side completion ring, not the completion ring payload/control record itself. Move it later only if the SHM-channel or completion attachment boundary is made more explicit.

### 2026-06-27: Phase 3f queue and byte-ring ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_queue_abi.h` from a Phase 3a adapter into the owner of queue-control, queue-descriptor, payload-ring, byte-ring, payload-head mirror, legacy registry, queue attachment, and result-stream binding declarations
- moved the remaining queue/ring declarations out of `tuple_sink_protocol.h`, leaving `tuple_sink_protocol.h` as a compatibility umbrella over the split ABI headers
- moved `CitusTupleSinkQueueAttachment` and `CitusResultStreamBinding` out of `remote_execution_control_protocol.h` into `homer_queue_abi.h`, matching the plan's queue ABI classification
- changed `remote_execution_control_protocol.h` to include `homer_queue_abi.h` instead of relying on the old tuple protocol umbrella for queue attachment types
- added `homer_queue_abi.h` to the standalone service/client Makefile dependency lists

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n '^#define CITUS_(TUPLE_SINK_PAYLOAD|HOMER_PAYLOAD|TUPLE_SINK_QUEUE_DESCRIPTOR|TUPLE_SINK_ENTRY_STATE)|typedef struct Citus(TupleSinkQueueControl|TupleSinkPayloadRingControl|TupleSinkQueueDescriptor|TupleSinkPayloadRingDescriptor|HomerPayloadByteRingControl|HomerPayloadByteRingDescriptor|TupleSinkPayloadHeadMirrorDescriptor|TupleSinkRegistryHeader|TupleSinkRegistryEntry|TupleSinkQueueAttachment|ResultStreamBinding)' src/include/distributed/homer
# expected/current: queue/ring/attachment declarations are owned by homer_queue_abi.h

sudo -n -u dbcomm make -j8
# first run exposed direct basebackup users that had relied on tuple_sink_protocol.h transitivity
# after adding direct homer_basebackup_abi.h includes to homer_client.c and tuple_sink_service_process.c, the build completed successfully
```

Caveat / plan correction:

- `homer_client.c` and `tuple_sink_service_process.c` are direct basebackup object-protocol users. They should include `homer_basebackup_abi.h` directly rather than depending on `tuple_sink_protocol.h` transitively. This became visible only after `tuple_sink_protocol.h` stopped including concrete queue declarations and stopped serving as an accidental basebackup umbrella for those files.
- `tuple_sink_protocol.h` remains only as a compatibility umbrella for now. Direct users should continue moving toward the split headers, but deleting the old header should wait until `homer_control_abi.h` and `homer_completion_abi.h` are concrete owners and direct include users have been audited.

### 2026-06-27: Phase 3e tuple semantic ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_tuple_abi.h` from a Phase 3a adapter into the owner of tuple-stream semantic fixed-width declarations
- moved tuple direction and record-kind flags, tuple transport flags, tuple identity/schema-contract structs, terminal state/status structs, tuple batch/error records, attribute flags, and the common tuple transport envelope from `tuple_sink_protocol.h` into `homer_tuple_abi.h`
- kept queue/ring descriptors, payload-ring flags, registry entries, and queue attachment details in `tuple_sink_protocol.h` for the later `homer_queue_abi.h` extraction
- changed `tuple_sink_protocol.h` to include `homer_tuple_abi.h` as a compatibility surface while queue ABI extraction is still pending
- corrected `tuple_sink_service.h` to include `homer_queue_abi.h` instead of `homer_tuple_abi.h`, because its prototypes expose `CitusTupleSinkQueueDescriptor`

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n '^#define CITUS_TUPLE_SINK_(DIRECTION|QUEUE_FLAG|TRANSPORT_FLAG|RECORD_KIND|ATTRIBUTE_FLAG)|typedef (struct|enum) CitusTuple(SinkTransactionId|SinkKey|TupleContractAttribute|TupleViewContract|TupleSinkTerminalState|TupleSinkFailureCode|TupleSinkTerminalStatus|TupleSinkBatchHeader|TupleSinkErrorRecord|TupleSinkTransportHeader)' src/include/distributed/homer
# expected/current: tuple semantic declarations are owned by homer_tuple_abi.h

sudo -n -u dbcomm make -j8
# first run exposed tuple_sink_service.h using the wrong split ABI adapter for queue descriptors
# after switching tuple_sink_service.h to homer_queue_abi.h, the build completed successfully
```

Caveat:

- `CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_CLOSED` and `CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_FAILED` remain in `homer_tuple_abi.h` because the original plan classified them with tuple/data-plane semantic ABI, even though their names mention queue state. Queue descriptor/control structs still move in the queue ABI slice.
- `tuple_sink_protocol.h` still exists as a compatibility umbrella and as the temporary owner for queue/ring declarations. It should shrink further when `homer_queue_abi.h`, `homer_control_abi.h`, and `homer_completion_abi.h` become concrete owners.

### 2026-06-27: Phase 3d SHM name/prefix ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_shm_channel_abi.h` from a Phase 3a adapter into the owner of current POSIX SHM object names and completion-ring prefixes
- moved `CITUS_REMOTE_EXEC_CONTROL_SHM_NAME`, `CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_PREFIX`, `CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_PREFIX`, and the previously omitted `CITUS_TUPLE_SINK_REGISTRY_SHM_NAME` into `homer_shm_channel_abi.h`
- changed `tuple_sink_protocol.h` and `remote_execution_control_protocol.h` to include `homer_shm_channel_abi.h` for compatibility while their remaining concrete declarations are split in later subphases
- added `homer_shm_channel_abi.h` to the standalone service/client Makefile dependency lists

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n '^#define CITUS_(REMOTE_EXEC_CONTROL_SHM_NAME|REMOTE_EXEC_CLIENT_COMPLETION_SHM_PREFIX|REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_PREFIX|TUPLE_SINK_REGISTRY_SHM_NAME)' src/include/distributed/homer
# expected/current: only homer_shm_channel_abi.h owns these names/prefixes

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 3d state
```

Caveat / plan correction:

- The earlier SHM-channel list duplicated `CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES` and `CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES`, which are already fixed-size ABI limits in `homer_abi_version.h`. Leave those byte limits in `homer_abi_version.h`; `homer_shm_channel_abi.h` should own the string names/prefixes and, after `homer_control_abi.h` is extracted, the SHM control-region structs.
- `CITUS_TUPLE_SINK_REGISTRY_SHM_NAME` was missing from the original known SHM-channel list even though it is also a POSIX SHM object name. It is now explicitly included in the target list.

### 2026-06-27: Phase 3c version/capacity ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- converted `homer_abi_version.h` from a Phase 3a adapter into a standalone PostgreSQL-free ABI header
- moved tuple-sink protocol version, tuple queue default capacities, tuple shared-route/contract/name byte limits, remote-exec control protocol version, control slot/session/sink capacities, host/name byte limits, completion SHM name byte limits, invalid session sentinel, and fixed command payload byte limits into `homer_abi_version.h`
- changed `tuple_sink_protocol.h` and `remote_execution_control_protocol.h` to include `homer_abi_version.h` for compatibility while their remaining concrete declarations are split in later subphases
- added `homer_abi_version.h` to the standalone service/client Makefile dependency lists, because those manual binary rules do not rely on generated header dependency tracking

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n '^#define CITUS_(TUPLE_SINK_PROTOCOL_VERSION|TUPLE_SINK_DEFAULT_SLOT_CAPACITY_BYTES|TUPLE_SINK_DEFAULT_BATCH_TUPLE_TARGET|TUPLE_SINK_DEFAULT_SLOT_COUNT|TUPLE_SINK_MAX_SHARED_ROUTES|TUPLE_SINK_MAX_CONTRACT_ATTRIBUTES|TUPLE_SINK_SHM_NAME_BYTES|REMOTE_EXEC_CONTROL_PROTOCOL_VERSION|REMOTE_EXEC_CONTROL_ERROR_BYTES|REMOTE_EXEC_CONTROL_SLOT_COUNT|REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS|REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS|REMOTE_EXEC_CONTROL_HOST_BYTES|REMOTE_EXEC_CONTROL_NAME_BYTES|REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES|REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES|REMOTE_EXEC_CONTROL_INVALID_SESSION_INDEX|REMOTE_EXEC_TX_BEGIN_ATTACH_REPLAY_BYTES|REMOTE_EXEC_SQL_COMMAND_BYTES)' src/include/distributed/homer
# expected/current: only homer_abi_version.h owns these constants

sudo -n -u dbcomm make -j8
# completed successfully from the Phase 3c state
```

Caveat:

- SHM object names and prefixes, such as `CITUS_REMOTE_EXEC_CONTROL_SHM_NAME` and completion-ring prefixes, intentionally remain outside `homer_abi_version.h`. They belong in the later `homer_shm_channel_abi.h` extraction, not in the version/capacity header.

### 2026-06-27: Phase 3b basebackup ABI extraction complete

Completed in `/data/dbcomm/citus-dbcomm`:

- moved `CITUS_REMOTE_BASEBACKUP_*` constants, `CitusRemoteBaseBackupMessageHeader`, and the basebackup object helper inlines from `tuple_sink_protocol.h` into `homer_basebackup_abi.h`
- changed `tuple_sink_protocol.h` to include `homer_basebackup_abi.h` as a compatibility surface, so existing transitive users still see the same basebackup names
- added `homer_basebackup_abi.h` to the standalone service/client Makefile dependency lists while the manual binary rules do not have automatic header dependency tracking

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -j8
# completed successfully from the Phase 3b state

rg -n 'CITUS_REMOTE_BASEBACKUP|CitusRemoteBaseBackup' \
  src/include/distributed/homer/tuple_sink_protocol.h \
  src/include/distributed/homer/homer_basebackup_abi.h
# expected/current: concrete declarations live in homer_basebackup_abi.h; tuple_sink_protocol.h has only a comment reference

rg -n 'postgres\.h|executor/tuptable\.h|utils/rel\.h|Datum|TupleDesc|TupleTableSlot' \
  src/include/distributed/homer/homer_basebackup_abi.h \
  src/include/distributed/homer/homer_*_abi.h
# expected/current: no results
```

Caveat:

- `tuple_sink_protocol.h` still includes `homer_basebackup_abi.h` for compatibility. That is intentional until direct users have been moved to split ABI headers and the old monolithic protocol headers can be retired.

### 2026-06-27: Phase 3a ABI include-boundary adapters complete

Completed in `/data/dbcomm/citus-dbcomm`:

- created PostgreSQL-free adapter headers `homer_abi_version.h`, `homer_tuple_abi.h`, `homer_queue_abi.h`, `homer_control_abi.h`, `homer_completion_abi.h`, `homer_shm_channel_abi.h`, and `homer_basebackup_abi.h`
- mechanically switched direct include users of `remote_execution_control_protocol.h` and `tuple_sink_protocol.h` to include the relevant `homer_*_abi.h` adapter names
- kept `tuple_sink_protocol.h` and `remote_execution_control_protocol.h` as the source of concrete fixed-width declarations for this subphase
- kept the old protocol headers in the standalone service/client explicit Makefile dependencies because the adapter headers still include them transitively

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -j8
# completed successfully; extension, service binary, and homer client archive built

rg -n '#include "distributed/homer/(tuple_sink_protocol|remote_execution_control_protocol)\.h"' src Makefile
# expected/current after Phase 3a: direct old-protocol includes remain only inside the new adapter headers and inside remote_execution_control_protocol.h's tuple-protocol dependency

rg -n 'postgres\.h|executor/tuptable\.h|utils/rel\.h|Datum|TupleDesc|TupleTableSlot' src/include/distributed/homer/homer_*_abi.h
# expected/current after Phase 3a: no results
```

Caveat:

- Phase 3 is **not complete** after this subphase. The new `homer_*_abi.h` headers are currently include-boundary adapters, not yet the owners of the concrete declarations listed below. This avoids duplicating or hand-reordering ABI structs before the split is mechanically safe. The next Phase 3 work should move declarations out of the old protocol headers section by section, with compile gates after each move, then retire the old monolithic headers.

### 2026-06-27: Phase 1-2 mechanical rename complete

Completed in `/data/dbcomm/citus-dbcomm`:

- moved `src/include/distributed/homer/remote_execution_session.h` to `src/include/distributed/homer/homer_frontend.h`
- updated the public header guard to `HOMER_FRONTEND_H`
- updated ordinary frontend API callers to include `distributed/homer/homer_frontend.h`
- moved `src/backend/distributed/utils/homer/remote_execution_session.c` to `src/backend/distributed/utils/homer/homer_frontend.c`
- updated file-name comments and local include references from `remote_execution_session.*` to `homer_frontend.*`
- kept function names, type names, control protocol structs, queue substrate names, and runtime behavior unchanged

Validation:

```bash
cd /data/dbcomm/citus-dbcomm
rg -n 'remote_execution_session\.(c|h)|distributed/homer/remote_execution_session\.h|REMOTE_EXECUTION_SESSION_H' src Makefile
# expected/current after Phase 1-2: no results

sudo -n -u dbcomm make -j8
# completed successfully; extension, service binary, and homer client archive built
```

Caveat:

- `git clang-format HEAD -- <moved frontend files>` treats the full moved files as changed and rewrites broad formatting. For the mechanical rename phase, preserve the small rename/comment/include diff instead of accepting that churn. Revisit formatting once extraction phases touch narrower line ranges or after staging a pure rename boundary.

## Clarification: what is the new API?

The new API is **not** a new semantic API in the first pass.

The first-pass public API is the current `remote_execution_session.h` surface moved to a new source-of-truth header named:

```text
src/include/distributed/homer/homer_frontend.h
```

That means the first pass may keep existing type/function names such as:

```c
RemoteExecutionSession
RemoteSendBatch
RemoteRecvBatch
RemoteTupleView
RemoteExecutionSessionIntentSpec
RemoteExecutionOperationSpec
RemoteExecutionCommandSpec
OpenRemoteExecutionSession
CloseRemoteExecutionSession
StartRemoteExecutionCommand
AppendTupleViewToRemoteSendStream
PollRemoteRecvBatch
BorrowNextRemoteTupleView
ReleaseRemoteRecvBatch
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

The first-pass API change is the **header and compilation boundary**, not a broad rename. A later cleanup may rename these to `HomerSession`, `HomerOpenSession`, etc., but doing that during the boundary refactor would make the diff larger without improving DPU readiness.

There should be no temporary `remote_execution_session.h` compatibility shim in this plan. The old header should be moved/deleted and all call sites should be updated to include `homer_frontend.h` directly. Any missed include should fail to compile, which is useful during this refactor.

Recommended mechanical first step:

```bash
git mv src/include/distributed/homer/remote_execution_session.h \
       src/include/distributed/homer/homer_frontend.h
```

Then update the include guard and comment block inside the moved header.

## Current host-backend path, summarized

Today, PostgreSQL/Citus backend code calls the public-ish session API from `remote_execution_session.h`. The implementation in `remote_execution_session.c` does all of the following directly:

1. constructs Citus/Homer session intent and tuple-sink operation specs
2. maps the well-known local control shared-memory object
3. claims a control slot
4. writes open/close/start/poll/report control requests into that slot
5. spins until the standalone service publishes a response
6. maps the service-owned peer completion ring
7. opens a local tuple queue endpoint from a queue descriptor returned by the service
8. encodes `TupleTableSlot` values into tuple-view records
9. publishes byte-ring tails for send batches
10. polls receive records, decodes tuple views, and provides borrow/copy-out APIs to the worker
11. owns Citus transaction-finalization callbacks for session-owned worker transactions

The refactor should split these concerns without changing behavior.

## Target high-level layout

```text
PostgreSQL/Citus call sites
  multi_copy.c
  worker_tuple_sink_insert.c
  transaction_management.c
  other ordinary backend callers
        |
        v
Public frontend API
  src/include/distributed/homer/homer_frontend.h
        |
        v
Host frontend implementation/library
  homer_frontend.c
  homer_frontend_control.c
  homer_frontend_shm.c
  homer_tuple_queue_frontend.c
  homer_tuple_view_codec.c
  homer_citus_policy.c
  homer_citus_xact.c
        |
        v
Shared ABI headers
  homer_abi_version.h
  homer_tuple_abi.h
  homer_queue_abi.h
  homer_control_abi.h
  homer_completion_abi.h
  homer_shm_channel_abi.h
  homer_basebackup_abi.h
        |
        v
Homer service/backend
  tuple_sink_service_process.c first, later split into service modules
  remote_execution_peer_transport_rdma.c service-side transport
  service-owned session/sink tables
  service scheduler
  service-side tuple decode/rebuild
```

## New and renamed files

### 1. Public frontend API

#### `src/include/distributed/homer/homer_frontend.h`

This is the only public API header ordinary PostgreSQL/Citus backend callers should include.

First-pass contents are the current contents of `remote_execution_session.h`, with these edits:

- update the header comment to describe the host-side Homer frontend API
- update the include guard to `HOMER_FRONTEND_H`
- include only host-backend dependencies needed by the API, currently `postgres.h`, `executor/tuptable.h`, `utils/rel.h`, and shared Homer ABI headers
- keep existing public type/function names for the first mechanical pass
- add no raw SHM types, no fd/mmap fields, no RDMA types, and no service-internal table structs

Current public declarations to keep here initially:

```text
RemoteExecOpKind
RemoteExecAccessKind
RemoteExecTxPolicy
RemoteExecFreshnessPolicy
RemoteExecOwnershipPolicy
RemoteExecExecutionLane
RemoteExecMetadataPolicy
RemoteExecPlacementScope
RemoteExecTupleSinkDirection
RemoteExecTupleSinkOperationSpec
RemoteExecutionOperationSpec
RemoteExecutionSessionIntentSpec
RemoteExecutionSession
RemoteSendBatch
RemoteRecvBatch
RemoteTupleView
RemoteExecutionSendReserveStatus
RemoteExecutionSendAppendStatus
RemoteExecCommandKind
RemoteExecCommandState
RemoteExecCopyIngestCommandSpec
RemoteExecTxBeginAttachCommandSpec
RemoteExecTxPrepareCommandSpec
RemoteExecSqlResultMode
RemoteExecSqlExecuteCommandSpec
RemoteExecutionCommandSpec
RemoteExecPostCommandState
RemoteExecutionCommandCompletion
```

Current public functions to keep here initially:

```text
InitRemoteTupleSinkSendSessionIntent
InitRemoteTupleSinkReceiveSessionIntent
InitRemoteSqlCommandSessionIntent
InitRemoteClientSqlSessionIntent
InitRemoteTupleSinkSendOperationSpec
InitRemoteTupleSinkReceiveOperationSpec
InitRemoteCopyIngestFromTupleSinkCommandSpec
InitRemoteTransactionBeginAttachCommandSpec
InitRemoteClientSqlTransactionBeginCommandSpec
InitRemoteTransactionPrepareCommandSpec
InitRemoteTransactionCommitCommandSpec
InitRemoteTransactionAbortCommandSpec
InitRemoteSqlExecuteCommandSpec
RemoteExecutionSessionOpenSpecToString
RemoteExecutionSessionPrototypeEnabled
RemoteExecutionSessionLoopbackValidationEnabled
OpenRemoteExecutionSession
CloseRemoteExecutionSessionDataPlane
CloseRemoteExecutionSession
StartRemoteExecutionCommand
PollRemoteExecutionCommandCompletion
WaitForRemoteExecutionCommandCompletion
ReportRemoteExecutionSessionPostCommandState
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
TryReserveRemoteSendBatch
ReserveRemoteSendBatch
TryAppendTupleViewToRemoteSendBatch
AppendTupleViewToRemoteSendBatch
SubmitRemoteSendBatch
DiscardRemoteSendBatch
AppendTupleViewToRemoteSendStream
FlushRemoteSendStream
PollRemoteRecvBatch
RemoteExecutionSessionReceivePeerClosed
RemoteRecvBatchTupleCount
BorrowNextRemoteTupleView
ReleaseBorrowedRemoteTupleView
CopyOutNextRemoteTupleView
ReleaseRemoteRecvBatch
```

Move the frontend-visible tuple-sink GUC declarations here as well, so ordinary call sites do not include the queue substrate header just to read a frontend toggle:

```text
EnableExperimentalTupleSinkRouting
EnableExperimentalTupleSinkLoopbackValidation
ExperimentalTupleSinkSlotCapacityBytes
ExperimentalTupleSinkBatchTupleTarget
ExperimentalTupleSinkUseExplicitBatchApi
```

Longer-term rename is optional and should be a separate patch after the boundary is clean.

### 2. Frontend implementation coordinator

#### `src/backend/distributed/utils/homer/homer_frontend.c`

Recommended mechanical step:

```bash
git mv src/backend/distributed/utils/homer/remote_execution_session.c \
       src/backend/distributed/utils/homer/homer_frontend.c
```

After the move, gradually delete moved-out static helpers as later phases extract them.

This file should end up containing the frontend session coordinator only:

```text
OpenRemoteExecutionSession
CloseRemoteExecutionSessionDataPlane
CloseRemoteExecutionSession
StartRemoteExecutionCommand
PollRemoteExecutionCommandCompletion
WaitForRemoteExecutionCommandCompletion
ReportRemoteExecutionSessionPostCommandState
TryReserveRemoteSendBatch
ReserveRemoteSendBatch
TryAppendTupleViewToRemoteSendBatch
AppendTupleViewToRemoteSendBatch
SubmitRemoteSendBatch
DiscardRemoteSendBatch
AppendTupleViewToRemoteSendStream
FlushRemoteSendStream
PollRemoteRecvBatch
RemoteExecutionSessionReceivePeerClosed
RemoteRecvBatchTupleCount
BorrowNextRemoteTupleView
ReleaseBorrowedRemoteTupleView
CopyOutNextRemoteTupleView
ReleaseRemoteRecvBatch
RemoteExecutionRaiseTupleSinkErrorRecord
RemoteExecutionSessionCpuRelax
RemoteSendBatchTupleCount
RemoteExecutionSessionCurrentCopyCommandFailed
WaitForRemoteSendBatchReserve
FlushRemoteSendStreamInternal
AppendTupleViewToRemoteSendStreamInternal
RemoteExecutionCommandCompletedSuccessfully
WaitForRemoteExecutionCommandSuccess
```

It should **not** contain, after extraction:

```text
shm_open
mmap
munmap
ftruncate
close of SHM fd fields
CitusRemoteExecControlRegion slot-scanning logic
CitusRemoteExecControlSlot claim/publish/wait logic
peer completion ring mmap logic
TupleDesc-to-contract builder internals
tuple row encode/decode internals
Citus transaction-finalization list internals
RDMA headers or RDMA symbols
```

#### `src/backend/distributed/utils/homer/homer_frontend_internal.h`

Private header included only by frontend implementation files.

Move private structs here:

```text
RemoteExecutionSessionData
RemoteExecutionBatchData
```

But adjust fields as extraction progresses:

- replace raw peer completion ring fd/mapping fields with a private `HomerPeerCompletionConsumer` struct from `homer_frontend_shm.h`
- replace direct `CitusTupleSinkHandle` naming with the queue-frontend handle type once `tuple_sink_service.c` is renamed
- keep service ids and high-level session state here because they are frontend session state, not public API

### 3. Frontend SHM channel

#### `src/backend/distributed/utils/homer/homer_frontend_shm.h`

Private frontend header for the current POSIX shared-memory host-backend-to-service channel.

First-pass structs:

```c
typedef struct HomerShmControlClient
{
    int shmFileDescriptor;
    Size mappingBytes;
    void *mappingAddress;
    CitusRemoteExecControlRegion *controlRegion;
} HomerShmControlClient;

typedef struct HomerPeerCompletionConsumer
{
    int fileDescriptor;
    Size mappingBytes;
    void *mappingAddress;
    CitusRemoteExecPeerCommandCompletionRing *ring;
    char shmName[CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES];
} HomerPeerCompletionConsumer;
```

First-pass function surface:

```text
HomerShmControlClientOpen
HomerShmControlClientClose
HomerShmControlReserveSlot
HomerShmControlPublishRequest
HomerShmControlWaitForResponse
HomerShmControlCheckResponseHeader
HomerShmControlRoundTrip
HomerShmPeerCompletionRingDescriptorValid
HomerShmPeerCompletionConsumerMap
HomerShmPeerCompletionConsumerUnmap
HomerShmTryConsumePeerCompletion
```

For the first patch, the function bodies can be direct moves/renames of the existing helpers from `remote_execution_session.c`.

#### `src/backend/distributed/utils/homer/homer_frontend_shm.c`

Move these existing helpers from `remote_execution_session.c`:

```text
RemoteExecutionControlState
RemoteExecutionControlAtomicLoadU32
RemoteExecutionControlAtomicLoadU64
RemoteExecutionControlAtomicStoreU64
RemoteExecutionControlAtomicFetchOrU64
RemoteExecutionControlAtomicStoreU32
RemoteExecutionControlCompareExchangeU32
MapRemoteExecutionControlRegion
UnmapRemoteExecutionControlRegion
ReserveRemoteExecutionControlSlot
PublishRemoteExecutionControlRequest
WaitForRemoteExecutionControlResponse
RemoteExecutionControlCheckResponseHeader
RemoteExecutionPeerCommandCompletionRingDescriptorValid
MapRemoteExecutionPeerCommandCompletionRing
UnmapRemoteExecutionPeerCommandCompletionRing
TryConsumeRemoteExecutionPeerCommandCompletion
```

Acceptable first-pass approach:

- keep old static names during the first move if that reduces risk
- rename to `HomerShm*` in a follow-up cleanup
- do not add a transport vtable yet

This file is the exact place a later `homer_frontend_dma.c` should replace. The high-level frontend should call a small internal channel surface, not call POSIX SHM functions directly.

### 4. Frontend control message builders

#### `src/backend/distributed/utils/homer/homer_frontend_control.h`

Private header for building semantic control requests and converting control responses into frontend API structs.

Expected functions:

```text
BuildTupleSinkKeyFromOperation
BuildPlacementAccessDescriptorFromOperation
BuildControlSessionKeyFromIntent
BuildPeerEndpointFromIntent
RemoteExecutionControlDirectionCode
RemoteExecutionControlCommandKindCode
RemoteExecutionCommandKindFromControl
RemoteExecutionCommandStateFromControl
RemoteExecutionPostCommandStateFromControl
RemoteExecutionFillCommandCompletion
RemoteExecutionControlPostCommandStateCode
OpenTupleSinkSessionThroughLocalService
OpenCommandSessionThroughLocalService
CloseTupleSinkSessionThroughLocalService
CloseRemoteExecutionCompatibilitySessionThroughLocalService
ReportTupleSinkSessionPostCommandStateThroughLocalService
StartRemoteExecutionCommandThroughLocalService
PollRemoteExecutionCommandCompletionThroughLocalService
```

#### `src/backend/distributed/utils/homer/homer_frontend_control.c`

Move those helpers from `remote_execution_session.c`.

This file should call `homer_frontend_shm.c` functions in the first pass. It should not know about DMA and should not expose a runtime vtable.

Boundary rule:

- `homer_frontend_control.c` knows the shared control ABI structs
- `homer_frontend_shm.c` knows how those structs are transported through the current SHM control region
- `homer_frontend.c` knows session semantics and calls control helpers

### 5. Citus/Postgres policy helpers

#### `src/backend/distributed/utils/homer/homer_citus_policy.h`

Private or semi-private header for constructing frontend API specs from Citus/Postgres backend state.

#### `src/backend/distributed/utils/homer/homer_citus_policy.c`

Move from `remote_execution_session.c`:

```text
ValidateTupleSinkSessionOpenSpec
ValidateSqlCommandSessionOpenSpec
InitRemoteExecutionSessionIntentDefaults
InitRemoteTupleSinkSendSessionIntent
InitRemoteTupleSinkReceiveSessionIntent
InitRemoteSqlCommandSessionIntent
InitRemoteClientSqlSessionIntent
InitRemoteTupleSinkSendOperationSpec
InitRemoteTupleSinkReceiveOperationSpec
InitRemoteCopyIngestFromTupleSinkCommandSpec
InitRemoteTransactionBeginAttachCommandSpec
InitRemoteClientSqlTransactionBeginCommandSpec
InitRemoteTransactionPrepareCommandSpec
InitRemoteTransactionCommitCommandSpec
InitRemoteTransactionAbortCommandSpec
InitRemoteSqlExecuteCommandSpec
RemoteExecutionSessionOpenSpecToString
RemoteExecutionSessionPrototypeEnabled
RemoteExecutionSessionLoopbackValidationEnabled
RemoteExecutionSessionDirectionName
RemoteExecutionOpKindName
RemoteExecutionExecutionLaneName
RemoteExecutionMetadataPolicyName
RemoteExecutionCommandKindName
```

This file may include Citus/Postgres headers such as transaction management, metadata cache, node lookup, user/database identity, and GUCs. It is host-frontend code, not shared ABI and not service code.

### 6. Citus transaction-finalization integration

#### `src/backend/distributed/utils/homer/homer_citus_xact.h`

Declares:

```text
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

These functions are also declared in `homer_frontend.h` for ordinary callers during the first pass, but the implementation should live in the xact file.

#### `src/backend/distributed/utils/homer/homer_citus_xact.c`

Move from `remote_execution_session.c`:

```text
RemoteExecutionTxFinalizationEntry
RemoteExecutionTxFinalizationConnectionOrdinal
RemoteExecutionTxFinalizationList
DisarmRemoteExecutionTxFinalizationEntry
EnsureRemoteExecutionSessionReadyForFinalization
StartAndWaitRemoteExecutionTransactionCommand
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

Do **not** move `RemoteExecutionControlRequestSequence` to this file. It is
control-channel request correlation state used by SHM control-slot reservation,
not transaction-finalization state. Move it with the control-slot reservation
helpers to `homer_frontend_shm.c`.

### 7. Frontend tuple queue attachment

The current `tuple_sink_service.h/.c` name is misleading for the frontend library. It is not the service process; it is the host-side tuple queue/batch substrate used by the frontend after the service returns a queue descriptor.

Recommended mechanical moves:

```bash
git mv src/include/distributed/homer/tuple_sink_service.h \
       src/include/distributed/homer/homer_tuple_queue_frontend.h

git mv src/backend/distributed/utils/homer/tuple_sink_service.c \
       src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c
```

#### `src/include/distributed/homer/homer_tuple_queue_frontend.h`

First-pass contents are the current queue/batch substrate API, with the header comment changed to make clear this is host frontend queue attachment code.

Keep current names initially to reduce risk, or rename only after the file move compiles.

Current declarations to keep/move:

```text
CitusTupleSinkDirection
CitusTupleSinkReserveStatus
CitusTupleSinkAppendStatus
CitusTupleSinkRecordStatus
CitusTupleSinkHandle
CitusTupleSinkBatchHandle
CitusTupleSinkEnabled
InitExplicitCitusTupleSinkKey
OpenCitusTupleSink
ResetCitusTupleSinkSendHandle
CloseCitusTupleSink
TryReserveCitusTupleSinkBatch
ReserveCitusTupleSinkBatch
TryAppendTupleViewToCitusTupleSinkBatch
AppendTupleViewToCitusTupleSinkBatch
SubmitCitusTupleSinkBatch
DiscardCitusTupleSinkBatch
PublishCitusTupleSinkErrorRecord
FinalizeCitusTupleSinkGeneration
MarkCitusTupleSinkPeerClosed
PollCitusTupleSinkRecord
PollCitusTupleSinkBatch
CitusTupleSinkRecordError
CitusTupleSinkReceivePeerClosed
CitusTupleSinkReceivePeerFailed
CitusTupleSinkReceiveTerminalState
CitusTupleSinkBatchTupleCount
BorrowNextTupleViewFromCitusTupleSinkBatch
ReleaseBorrowedTupleViewFromCitusTupleSinkBatch
ReleaseCitusTupleSinkBatch
CopyOutNextTupleViewFromCitusTupleSinkBatch
```

Move the frontend-visible GUC declarations out to `homer_frontend.h` and define them in `homer_citus_policy.c` or `homer_tuple_queue_frontend.c`. Ordinary callers should not include the queue substrate header just to read GUCs.

Suggested later rename map, after the mechanical move is stable:

```text
CitusTupleSinkHandle                    -> HomerTupleQueueEndpoint
CitusTupleSinkBatchHandle               -> HomerTupleBatch
OpenCitusTupleSink                      -> HomerTupleQueueOpen
CloseCitusTupleSink                     -> HomerTupleQueueClose
TryReserveCitusTupleSinkBatch           -> HomerTupleQueueTryReserveSendBatch
TryAppendTupleViewToCitusTupleSinkBatch -> HomerTupleQueueTryAppendTuple
SubmitCitusTupleSinkBatch               -> HomerTupleQueueSubmitBatch
PollCitusTupleSinkRecord                -> HomerTupleQueuePollRecord
BorrowNextTupleViewFromCitusTupleSinkBatch -> HomerTupleBatchBorrowNext
ReleaseCitusTupleSinkBatch              -> HomerTupleBatchRelease
```

#### `src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c`

First-pass contents are the current `tuple_sink_service.c` body, minus helpers later extracted into `homer_tuple_view_codec.c`.

This file owns:

```text
host-side mapping of queue attachment descriptors
byte-ring control pointer setup
send reservation against consumedHead/publishedTail
send batch submit/publication into the local queue
receive polling from the local queue
receive batch release and consumedHead advancement
terminal state reads from the local queue control block
borrow/release bookkeeping at the batch-handle level
```

This file should not own:

```text
local control-slot request/response mechanics
peer RDMA transport
service-owned session/sink tables
service scheduler
ordinary Citus transaction finalization
```

### 8. Tuple-view codec

#### `src/backend/distributed/utils/homer/homer_tuple_view_codec.h`

Private host-frontend header for local tuple-view row encode/decode.

This first-pass codec is **not** a shared frontend/service ABI header. Its
helpers use PostgreSQL executor concepts such as `TupleDesc`, `TupleTableSlot`,
`Datum`, attribute metadata, and datum copy/borrow rules. Keep it out of the DPU
or standalone service build unless a separate PostgreSQL-free service codec is
designed later.

The first pass can keep helper names private. Do not expose these to ordinary Citus call sites.

#### `src/backend/distributed/utils/homer/homer_tuple_view_codec.c`

Move from current `tuple_sink_service.c` / future `homer_tuple_queue_frontend.c`:

```text
TupleSinkAttributeUsesLengthPrefix
NormalizeTupleSinkAttribute
TupleSinkTupleBytes
SetTupleSinkNonNullBit
DecodeTupleViewFromTupleSinkRow
CopyOutTupleViewFromTupleSinkRow if present as a separate helper
```

This file owns:

```text
TupleTableSlot -> sequential tuple-view row
sequential tuple-view row -> Datum/isnull arrays
borrow-by-reference vs datumCopy policy
null bitmap handling
dropped/generated attribute handling
varlena detoast/cstring length handling
```

Implementation correction from Phase 8:

```text
BuildTupleViewContractFromTupleDesc remains in homer_frontend_control.c because it builds the OPEN_SESSION control-request contract.
TupleSinkTupleDescriptorsCompatible remains in homer_tuple_queue_frontend.c because it validates queue handle reuse against the cached TupleDesc.
```

This file should not know about:

```text
control SHM
service sessions
peer endpoints
RDMA
queue file descriptors
mmap addresses except through caller-provided buffers
```

## Shared ABI header split

The shared ABI headers must compile in both the host frontend library and the Homer service. They should stay fixed-width and should avoid PostgreSQL-only types.

Do **not** include in shared ABI headers:

```text
postgres.h
executor/tuptable.h
utils/rel.h
Datum
TupleDesc
TupleTableSlot
MemoryContext
elog/ereport
libpq
ibverbs/rdmacm
```

Use standard C headers only where possible:

```c
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
```

First-pass policy: keep the existing `Citus*` struct and macro names to avoid a protocol rename diff. Move them into clearer headers first; rename later only if needed.

### `src/include/distributed/homer/homer_abi_version.h`

Move shared version/capacity constants here:

```text
CITUS_TUPLE_SINK_PROTOCOL_VERSION
CITUS_TUPLE_SINK_DEFAULT_SLOT_CAPACITY_BYTES
CITUS_TUPLE_SINK_DEFAULT_BATCH_TUPLE_TARGET
CITUS_TUPLE_SINK_DEFAULT_SLOT_COUNT
CITUS_TUPLE_SINK_MAX_SHARED_ROUTES
CITUS_TUPLE_SINK_MAX_CONTRACT_ATTRIBUTES
CITUS_TUPLE_SINK_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION
CITUS_REMOTE_EXEC_CONTROL_ERROR_BYTES
CITUS_REMOTE_EXEC_CONTROL_SLOT_COUNT
CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS
CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS
CITUS_REMOTE_EXEC_CONTROL_HOST_BYTES
CITUS_REMOTE_EXEC_CONTROL_NAME_BYTES
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_CONTROL_INVALID_SESSION_INDEX
CITUS_REMOTE_EXEC_TX_BEGIN_ATTACH_REPLAY_BYTES
CITUS_REMOTE_EXEC_SQL_COMMAND_BYTES
```

Leave SHM object names/prefixes in `homer_shm_channel_abi.h`, not here.

### `src/include/distributed/homer/homer_tuple_abi.h`

Move tuple/data-plane semantic ABI from `tuple_sink_protocol.h`:

```text
CITUS_TUPLE_SINK_DIRECTION_SEND_CODE
CITUS_TUPLE_SINK_DIRECTION_RECEIVE_CODE
CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_CLOSED
CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_FAILED
CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS
CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_FIRST
CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_LAST
CITUS_TUPLE_SINK_RECORD_KIND_DATA
CITUS_TUPLE_SINK_RECORD_KIND_EOS
CITUS_TUPLE_SINK_RECORD_KIND_ERROR
CITUS_TUPLE_SINK_RECORD_KIND_PADDING
CITUS_TUPLE_SINK_TRANSPORT_FLAG_WRAP
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_NULL
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_DROPPED
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_GENERATED_OMITTED
CitusTupleSinkTransactionId
CitusTupleSinkKey
CitusTupleContractAttribute
CitusTupleViewContract
CitusTupleSinkTerminalState
CitusTupleSinkFailureCode
CitusTupleSinkTerminalStatus
CitusTupleSinkBatchHeader
CitusTupleSinkErrorRecord
CitusTupleSinkTransportHeader
```

The full `CitusTupleViewContract` remains shared because the frontend builds it and the service validates/installs it.

### `src/include/distributed/homer/homer_queue_abi.h`

Move queue and byte-ring attachment/control ABI:

```text
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_RECEIVER_OWNED
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_LOGICAL_DESCRIPTOR_ONLY
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_RDMA_REGISTERED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_RECEIVER_OWNED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_RDMA_REGISTERED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PRODUCER_OWNED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_CLOSED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_FAILED
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_SENDER_OWNED
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_LOGICAL_DESCRIPTOR_ONLY
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_RDMA_REGISTERED
CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING
CitusTupleSinkQueueControl
CitusTupleSinkPayloadRingControl
CitusTupleSinkQueueDescriptor
CitusTupleSinkPayloadRingDescriptor
CitusHomerPayloadByteRingControl
CitusHomerPayloadByteRingDescriptor
CitusTupleSinkPayloadHeadMirrorDescriptor
CitusTupleSinkRegistryHeader
CitusTupleSinkRegistryEntry
CitusTupleSinkQueueAttachment
CitusResultStreamBinding
```

The current `CitusTupleSinkQueueDescriptor` contains `queueShmName`, so it is not the final DMA attachment shape. Keep it for the current SHM model. Later, introduce a separate `HomerQueueAttachmentDescriptor` with an attachment kind or add a DMA-specific descriptor in a new ABI version.

### `src/include/distributed/homer/homer_control_abi.h`

Move semantic control-plane request/response ABI from `remote_execution_control_protocol.h`:

```text
CitusRemoteExecControlRequestKind
CitusRemoteExecControlStatusCode
CitusRemoteExecControlOpKind
CITUS_REMOTE_EXEC_OP_*
CITUS_REMOTE_EXEC_ACCESS_*
CITUS_REMOTE_EXEC_TX_*
CITUS_REMOTE_EXEC_FRESHNESS_*
CITUS_REMOTE_EXEC_OWNERSHIP_*
CITUS_REMOTE_EXEC_LANE_*
CITUS_REMOTE_EXEC_METADATA_POLICY_*
CITUS_REMOTE_EXEC_SCOPE_*
CITUS_REMOTE_EXEC_POST_COMMAND_STATE_*
CITUS_REMOTE_EXEC_COMMAND_*
CITUS_REMOTE_EXEC_SQL_RESULT_*
CITUS_REMOTE_EXEC_COMMAND_STATE_*
CITUS_REMOTE_EXEC_COMMAND_FLAG_WAIT_FOR_TERMINAL_COMPLETION
CITUS_REMOTE_EXEC_COMMAND_FLAG_RESULT_BINDING_PREARMED
CitusRemoteExecSessionKey
CitusRemoteExecPlacementAccessDescriptor
CitusRemoteExecPeerEndpoint
CitusRemoteExecCopyIngestCommandSpec
CitusRemoteExecTxBeginAttachCommandSpec
CitusRemoteExecTxPrepareCommandSpec
CitusRemoteExecSqlExecuteCommandSpec
CitusRemoteExecControlRequestHeader
CitusRemoteExecControlResponseHeader
CitusRemoteExecOpenSessionRequest
CitusRemoteExecOpenSessionResponse
CitusRemoteExecCloseSessionRequest
CitusRemoteExecCloseSessionResponse
CitusRemoteExecReportPostCommandStateRequest
CitusRemoteExecReportPostCommandStateResponse
CitusRemoteExecCommandCompletion
CitusRemoteExecStartCommandRequest
CitusRemoteExecStartCommandResponse
CitusRemoteExecPollCommandCompletionRequest
CitusRemoteExecPollCommandCompletionResponse
CitusRemoteExecControlRequestUnion
CitusRemoteExecControlResponseUnion
```

This header should describe the semantic messages. It should not contain the local SHM control-region slot array.

### `src/include/distributed/homer/homer_completion_abi.h`

Move completion mailbox/ring and result descriptor ABI:

```text
CITUS_REMOTE_EXEC_RESULT_FLAG_NONE
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_READY
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_EOS
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_FAILED
HOMER_COMPLETION_INLINE_DETAIL_BYTES
HOMER_RESULT_DESCRIPTOR_SLOTS
CitusRemoteExecCommandCompletionHot
CitusRemoteExecClientCompletionSeal
CitusRemoteExecClientCompletionSlot
HomerResultDescriptorSlot
HomerResultDescriptorTable
HomerResultDescriptorReadStatus
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS
CitusRemoteExecClientCompletionMailbox
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_RING_SLOTS
CitusRemoteExecPeerCommandCompletionEvent
CitusRemoteExecPeerCommandCompletionRing
```

Move pure inline helpers here:

```text
CitusRemoteExecFillClientCompletionSeal
CitusRemoteExecClientCompletionSealMatches
CitusRemoteExecResultDescriptorSlotIndex
CitusRemoteExecCompletionDetailBytes
CitusRemoteExecFillQueueAttachmentFromDescriptor
CitusRemoteExecFillStreamBindingFromDescriptor
CitusRemoteExecFillQueueDescriptorFromAttachment
CitusRemoteExecFillHotCompletionFromLegacy
CitusRemoteExecPublishResultDescriptor
CitusRemoteExecReadResultDescriptorStable
```

### `src/include/distributed/homer/homer_shm_channel_abi.h`

Move current local POSIX-SHM channel ABI:

```text
CITUS_REMOTE_EXEC_CONTROL_SHM_NAME
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_PREFIX
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_PREFIX
CITUS_TUPLE_SINK_REGISTRY_SHM_NAME
CitusRemoteExecControlSlotState
CitusRemoteExecControlSlot
CitusRemoteExecReadyBitmapLine
CitusRemoteExecControlRegion
```

Do not duplicate fixed byte-limit constants here. `CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES` and `CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES` belong in `homer_abi_version.h` with the other fixed-width protocol capacities.

This header is shared between the host frontend SHM implementation and the host service implementation in the current model. It is not the final DPU/DMA semantic ABI. The current implementation has now moved `CitusRemoteExecControlSlotState`, `CitusRemoteExecControlSlot`, `CitusRemoteExecReadyBitmapLine`, and `CitusRemoteExecControlRegion` here after `homer_control_abi.h` became the owner of request/response unions. Keep the dependency one-way: `homer_shm_channel_abi.h` may include semantic ABI headers, but semantic ABI headers must not include `homer_shm_channel_abi.h`.

### `src/include/distributed/homer/homer_basebackup_abi.h`

Move basebackup object protocol from `tuple_sink_protocol.h`:

```text
CITUS_REMOTE_BASEBACKUP_PROTOCOL_VERSION
CITUS_REMOTE_BASEBACKUP_NAME_BYTES
CITUS_REMOTE_BASEBACKUP_OBJECT_*
CITUS_REMOTE_BASEBACKUP_HEADER_FIXED_BYTES
CITUS_REMOTE_BASEBACKUP_HEADER_MAX_BYTES
CitusRemoteBaseBackupMessageHeader
CitusRemoteBaseBackupObjectKindValid
CitusRemoteBaseBackupObjectKindCarriesName
CitusRemoteBaseBackupHeaderBytesForName
```

This keeps tuple-stream ABI and basebackup object ABI separate.

## Include updates at call sites

Because this plan does not keep a `remote_execution_session.h` shim, update call sites directly.

Use a mechanical search. This search is authoritative; the files listed below
are only the current seed list:

```bash
rg -n 'distributed/homer/(remote_execution_session|tuple_sink_service)\.h' src
```

After the public frontend-header rename, `remote_execution_session.h` should
have no remaining include users. After the queue-substrate rename,
`tuple_sink_service.h` should have no remaining include users.

Current call sites to update:

### `src/backend/distributed/commands/multi_copy.c`

Current includes include both:

```c
#include "distributed/homer/remote_execution_session.h"
#include "distributed/homer/tuple_sink_service.h"
```

Target ordinary backend caller include:

```c
#include "distributed/homer/homer_frontend.h"
```

Remove the direct queue-substrate include once `ExperimentalTupleSinkUseExplicitBatchApi` and related frontend GUC declarations are available through `homer_frontend.h`.

### `src/backend/distributed/worker/homer/worker_tuple_sink_insert.c`

Current include:

```c
#include "distributed/homer/remote_execution_session.h"
```

Target:

```c
#include "distributed/homer/homer_frontend.h"
```

The worker should remain an ordinary consumer of the frontend API: open receive session, poll receive batches, borrow tuple views, release tuple views/batches.

### `src/backend/distributed/transaction/transaction_management.c`

Current include:

```c
#include "distributed/homer/remote_execution_session.h"
```

Target:

```c
#include "distributed/homer/homer_frontend.h"
```

This file calls the transaction-finalization hooks declared by the frontend API.

### `src/backend/distributed/worker/homer/remote_exec_pgbench_transaction.c`

Current include:

```c
#include "distributed/homer/remote_execution_session.h"
```

Target:

```c
#include "distributed/homer/homer_frontend.h"
```

This prototype SQL-callable pgbench driver remains an ordinary command-session
frontend API caller.

### `src/backend/distributed/shared_library_init.c`

Current include:

```c
#include "distributed/homer/tuple_sink_service.h"
```

Target after the frontend-visible GUC declarations move:

```c
#include "distributed/homer/homer_frontend.h"
```

This file registers the hidden tuple-sink/Homer GUCs. It should not include the
queue-substrate header only to reach `EnableExperimentalTupleSinkRouting`,
`ExperimentalTupleSinkBatchTupleTarget`, or related declarations.

### `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`

This file is not an ordinary public frontend API caller. It is PostgreSQL-side service/backend bridge code for socketless backend spawn and command execution.

Current include:

```c
#include "distributed/homer/tuple_sink_service.h"
```

Target after queue rename:

```c
#include "distributed/homer/homer_tuple_queue_frontend.h"
#include "distributed/homer/homer_control_abi.h"
#include "distributed/homer/homer_completion_abi.h"
#include "distributed/homer/homer_queue_abi.h"
```

Do not force this file through `homer_frontend.h` unless it truly calls the session frontend API. Its role is closer to host-side service integration and local backend command execution.

### `src/bin/homer_client.c` and `src/include/distributed/homer/remote_execution_client.h`

These are client-facing, not ordinary PostgreSQL backend frontend API. They should be updated to include the split shared ABI headers instead of the old monolithic protocol headers.

Do not merge this API into `homer_frontend.h` in the first pass. Keep the external client API separate.

## Build plan

### First build milestone: file separation with current extension build

The current Citus build gathers `*.c` files under `SUBDIRS` and filters out `tuple_sink_service_process.o`. Keep that mechanism initially.

After file moves, update `src/backend/distributed/Makefile` only as needed for filenames:

```make
# Still included in citus.so in the first build milestone:
utils/homer/homer_frontend.o
utils/homer/homer_frontend_control.o
utils/homer/homer_frontend_shm.o
utils/homer/homer_tuple_queue_frontend.o
utils/homer/homer_tuple_view_codec.o
utils/homer/homer_citus_policy.o
utils/homer/homer_citus_xact.o
```

Keep excluding the standalone service process object from the extension:

```make
OBJS = $(filter-out utils/homer/tuple_sink_service_process.o,$(DIST_EXTENSION_OBJS))
```

If `remote_execution_peer_transport_rdma.o` is not referenced by frontend objects after the refactor, exclude it from `citus.so` too:

```make
HOMER_SERVICE_OBJS = \
    utils/homer/tuple_sink_service_process.o \
    utils/homer/remote_execution_peer_transport_rdma.o

OBJS = $(filter-out $(HOMER_SERVICE_OBJS),$(DIST_EXTENSION_OBJS))
```

Then remove RDMA libraries from `SHLIB_LINK` for `citus.so` once no extension object needs them:

```make
# Remove from citus.so once service-only:
# -Wl,--no-as-needed -lrdmacm -libverbs -Wl,--as-needed
```

### Second build milestone: named frontend component/library

After the file split compiles, define the frontend object group explicitly:

```make
HOMER_FRONTEND_OBJS = \
    utils/homer/homer_frontend.o \
    utils/homer/homer_frontend_control.o \
    utils/homer/homer_frontend_shm.o \
    utils/homer/homer_tuple_queue_frontend.o \
    utils/homer/homer_tuple_view_codec.o \
    utils/homer/homer_citus_policy.o \
    utils/homer/homer_citus_xact.o
```

Two acceptable implementation choices:

1. keep `HOMER_FRONTEND_OBJS` linked directly into `citus.so`, but grouped and documented as the frontend component
2. build a static archive, `libhomer_frontend.a`, and link `citus.so` against that archive

Preferred target if the build system supports the archive cleanly:

```make
HOMER_FRONTEND_LIB = utils/homer/libhomer_frontend.a

$(HOMER_FRONTEND_LIB): $(HOMER_FRONTEND_OBJS)
	$(AR) rcs $@ $^

OBJS = $(filter-out $(HOMER_SERVICE_OBJS) $(HOMER_FRONTEND_OBJS),$(DIST_EXTENSION_OBJS))
SHLIB_LINK += $(HOMER_FRONTEND_LIB)
```

If PostgreSQL/Citus extension linking resists `.a` archives in `SHLIB_LINK`, keep the explicit object group as the intermediate state and document it as the frontend component. The separation still matters because service-only and frontend-only objects are no longer mixed.

### Service build milestone

The service build should include:

```text
tuple_sink_service_process.o
remote_execution_peer_transport_rdma.o
service-side helpers split later
shared ABI headers
```

The service build should link RDMA libraries:

```text
-lrdmacm
-libverbs
```

The extension/frontend build should not link RDMA libraries after `remote_execution_peer_transport_rdma.o` leaves `citus.so`.

## Mechanical migration order

### Phase 0: branch hygiene

Work on:

```text
postgres-dbcomm:homer-dpu-migration
citus-dbcomm:homer-dpu-migration
```

Keep KB edits in `postgres-dbcomm`. Code changes belong in `citus-dbcomm`.

### Phase 1: create the public frontend header

1. `git mv remote_execution_session.h homer_frontend.h`
2. update include guard and file comment
3. update known call-site includes to `homer_frontend.h`
4. run `rg -n 'distributed/homer/remote_execution_session\.h' src` and remove all hits
5. do not add a compatibility shim

Expected outcome: ordinary backend callers now depend on the new header name, but behavior and function names are unchanged.

### Phase 2: move/rename frontend implementation coordinator

1. `git mv remote_execution_session.c homer_frontend.c`
2. update the include inside the moved C file from `remote_execution_session.h` to `homer_frontend.h`
3. leave helper functions in place for this phase
4. compile

Expected outcome: source filenames now match the intended component boundary, but behavior remains unchanged.

### Phase 3: split shared ABI headers

Phase 3a has already created PostgreSQL-free `homer_*_abi.h` adapter headers and moved direct include users to those names while leaving concrete declarations in the old protocol headers. Continue from there by moving declarations section by section.

1. create `homer_abi_version.h` - done as Phase 3a adapter
2. create `homer_tuple_abi.h` - done as Phase 3a adapter
3. create `homer_queue_abi.h` - done as Phase 3a adapter
4. create `homer_control_abi.h` - done as Phase 3a adapter
5. create `homer_completion_abi.h` - done as Phase 3a adapter
6. create `homer_shm_channel_abi.h` - done as Phase 3a adapter
7. create `homer_basebackup_abi.h` - done as Phase 3a adapter
8. update includes in frontend, service, bridge, client, and transport files - direct users updated in Phase 3a
9. move concrete declarations from the old monolithic protocol headers into the new ABI headers section by section
10. keep compile checkpoints after each declaration move
11. delete or empty old monolithic protocol headers only after all includes and declarations are updated

Expected outcome: both frontend and service include shared ABI headers, concrete ABI ownership lives in the split headers, and runtime behavior is unchanged.

### Phase 4: extract SHM control channel

1. create `homer_frontend_shm.h/.c` - done
2. move SHM control-state, `RemoteExecutionControlRequestSequence`, atomic, map/unmap, control-slot, request-publish, wait-response, response-check, and completion-ring mapping helpers from `homer_frontend.c` - done
3. add private `homer_frontend_internal.h` for session/batch structs shared by split frontend implementation files - done
4. update `homer_frontend.c` to call these helpers - done
5. compile - done

Expected outcome: `homer_frontend.c` has no direct `shm_open`, `mmap`, `munmap`, or `ftruncate` calls. This is now true after Phase 4.

### Phase 5: extract control request builders

1. create `homer_frontend_control.h/.c` - done
2. move session-key, sink-key, placement-descriptor, peer-endpoint, enum-conversion, command-completion conversion, and `*ThroughLocalService` helpers out of `homer_frontend.c` - done
3. have these helpers call the SHM channel functions directly - done
4. compile - done

Expected outcome: `homer_frontend.c` owns high-level session semantics; `homer_frontend_control.c` owns message construction; `homer_frontend_shm.c` owns the current channel. This is now true after Phase 5, with public spec/string/policy glue intentionally left for Phase 6.

### Phase 6: move Citus policy and transaction glue

1. create `homer_citus_policy.h/.c` - done
2. move init/spec/string/feature-gate helpers there - done
3. create `homer_citus_xact.h/.c` - done
4. move transaction-finalization list and callbacks there - done
5. leave `RemoteExecutionControlRequestSequence` in `homer_frontend_shm.c`; it is request correlation for SHM control slots, not xact state - done
6. compile - done

Expected outcome: PostgreSQL/Citus policy and transaction lifecycle glue are clearly host-side frontend files. This is now true after Phase 6; `homer_frontend.c` still owns high-level session coordination and tuple queue send/receive APIs.

### Phase 7: rename tuple queue frontend substrate

1. `git mv tuple_sink_service.h homer_tuple_queue_frontend.h` - done
2. `git mv tuple_sink_service.c homer_tuple_queue_frontend.c` - done
3. update includes in `homer_frontend.c`, `remote_execution_backend_bridge.c`, and any other queue-substrate users - done
4. move frontend-visible GUC declarations to `homer_frontend.h` - done
5. run `rg -n 'distributed/homer/tuple_sink_service\.h' src` and remove all hits - done
6. compile - done

Expected outcome: the host-side queue attachment code is no longer named as if it were the service process. This is now true after Phase 7; `tuple_sink_service_process.c` remains the standalone service process name.

### Phase 8: extract tuple-view codec

1. create `homer_tuple_view_codec.h/.c` - done
2. move tuple row encode/decode helpers there - done
3. keep `BuildTupleViewContractFromTupleDesc` in `homer_frontend_control.c` for now, because it is tied to `OPEN_SESSION` control-request construction rather than tuple-row byte encoding - done
4. keep this as host-frontend codec code because the first-pass helpers still depend on PostgreSQL executor types and Datum semantics - done
5. keep queue endpoint code focused on byte-ring reservation/publication and batch-handle lifetime - done
6. use private `homer_tuple_queue_internal.h` only as implementation glue between the queue frontend and codec, not as a public API - done
7. compile - done

Expected outcome: tuple row/Datum encoding is separated from queue frontier mechanics. This is now true after Phase 8; tuple contract construction remains with the control-request builder pending a possible later small split.

### Phase 9: update build boundary

1. define `HOMER_FRONTEND_OBJS` - done
2. define `HOMER_SERVICE_OBJS` - done
3. exclude service-only objects from `citus.so` - done
4. remove RDMA libraries from `citus.so` link once possible - done
5. optionally build `libhomer_frontend.a` - deferred; object grouping is enough for this first mechanical pass
6. ensure service binary links service-only/RDMA objects - done

Expected outcome: frontend and service are identifiable build components; ordinary PostgreSQL backend linkage is not polluted by service/RDMA objects. This is now true after Phase 9; `citus.so` links the frontend object group and has no RDMA dynamic dependency, while the standalone service retains RDMA linkage.

### Phase 10: service-file split in a later pass

Do not split `tuple_sink_service_process.c` during the first frontend separation unless necessary for compile hygiene. It is too large and service-internal. Once the frontend boundary is clean, split it into service modules in a separate design pass.

Candidate later service split:

```text
homer_service_main.c
homer_service_control.c
homer_service_session.c
homer_service_sink.c
homer_service_peer_control.c
homer_service_transport_rdma.c
homer_service_scheduler.c
homer_service_command_dispatch.c
homer_service_tuple_decode.c
```

## Acceptance checks

### Header boundary

```bash
rg -n 'distributed/homer/remote_execution_session\.h' src
# expected: no results

rg -n 'distributed/homer/tuple_sink_service\.h' src
# expected after queue rename: no results
```

Ordinary backend call sites should include:

```c
#include "distributed/homer/homer_frontend.h"
```

Service code should not include `homer_frontend.h`.

### Frontend implementation boundary

```bash
grep -n "shm_open\|mmap\|munmap\|ftruncate" src/backend/distributed/utils/homer/homer_frontend.c
# expected after SHM extraction: no results

grep -n "rdma_\|ibv_" src/backend/distributed/utils/homer/homer_frontend*.c
# expected: no results
```

### ABI boundary

Shared ABI headers should compile without PostgreSQL backend headers:

```bash
grep -R "postgres.h\|executor/tuptable.h\|utils/rel.h\|Datum\|TupleDesc\|TupleTableSlot" \
    src/include/distributed/homer/homer_*_abi.h
# expected: no PostgreSQL-only API dependencies
```

### Build boundary

```text
citus.so includes frontend objects or libhomer_frontend.a
citus.so excludes tuple_sink_service_process.o
citus.so excludes remote_execution_peer_transport_rdma.o once frontend no longer references it
citus.so does not need -lrdmacm/-libverbs after RDMA object exclusion
Homer service binary links service/RDMA objects and RDMA libraries
```

### Behavior regression checks

Run existing tuple-sink/Homer validation after each phase that moves code:

```text
experimental COPY tuple-sink worker insert path
loopback validation path
explicit reserve/append/submit path
worker borrow/use/release insert path
transaction PREPARE/COMMIT/ABORT finalization hooks
peer completion ring command-completion path
semantic ERROR+EOS record propagation
receive terminal state / peer-closed handling
```

## Later DMA transition

After the mechanical separation is complete, add:

```text
homer_frontend_dma.h
homer_frontend_dma.c
```

Only then decide whether to introduce a runtime abstraction such as:

```c
typedef struct HomerFrontendTransportOps HomerFrontendTransportOps;
```

A vtable is justified only if:

- SHM and DMA must coexist in one binary
- runtime selection is required
- unit tests need to swap the channel implementation without recompiling

If the DPU migration is a build-time or deployment-time replacement, a concrete `homer_frontend_dma.c` implementing the same internal channel functions as `homer_frontend_shm.c` is sufficient and less invasive.
