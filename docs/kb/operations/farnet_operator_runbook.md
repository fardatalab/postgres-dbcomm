# Farnet operator runbook

<!-- kb-summary: Canonical topology, artifact preparation, role lifecycle, workload, evidence, and teardown procedures for manual operation of the farnet Homer rig while deterministic live runner execution remains disabled. -->

## Authority and workflow

This is the canonical manual runbook for the farnet two-host, two-DPU Homer rig. Before live validation, read this
document in full, then read:

- [operational hazards](farnet_operational_hazards.md), especially process, artifact, and validation-claim identity;
- the [runner plan](farnet_validation_runner_plan.md) **Status** and **Current next step**;
- the implementation plan and narrowest relevant
  [`CONTRACTS.md`](../implementations/citus/transport/CONTRACTS.md).

The deterministic runner is not yet allowed to orchestrate live state. Its checked-in helpers and checkers are
evidence-producing building blocks; the operator remains responsible for provenance, candidate bracketing, ordered
role lifecycle, and cleanup.

Workflow:

1. [Confirm environment and topology](#environment-and-storage-layout).
2. [Build, install, and prove artifacts](#build-install-and-artifact-proof).
3. [Deploy the peer prefix and DPU snapshots](#peer-host-artifact-deployment).
4. [Establish a clean baseline](#clean-baseline).
5. [Start roles and prepare the workload](#start-roles-and-prepare-workload).
6. [Run the applicable acceptance profile](#acceptance-profile).
7. [Bracket evidence, tear down, and decide the verdict](#teardown-and-verdict).

## Environment and storage layout

| role | logical paths and ownership |
|---|---|
| farnet1 host | source `/data/dbcomm/{postgres-citus,citus-dbcomm}`; prefix `/data/dbcomm/pg-citus`; real `/data` filesystem |
| farnet0 host | same logical paths; `/data/dbcomm` is a compatibility symlink to `/home/dbcomm/data-dbcomm` |
| both DPUs | run-scoped source/build roots under `/tmp/farnet-validation-<run-id>` |

The stable configuration interface is `/data/dbcomm/...` on both hosts. Process identity is canonical: farnet0's
`/proc/PID/exe` reports `/home/dbcomm/data-dbcomm/...`, so compare canonical prefix and executable paths. Do not move
farnet1's prefix into `/home`; the filesystems do not have comparable capacity.

Build directories, installed artifacts, generated Citus `.deps`/`build-cdc-*` trees, service files, and workload
outputs are `dbcomm`-owned. Source, docs, and `.git` remain human-owned. Before correcting ownership, inspect it and
repair only the generated side of that split:

```sh
sudo -n chown -R dbcomm:dbcomm \
  /data/dbcomm/postgres-citus/build /data/dbcomm/citus-dbcomm/build /data/dbcomm/pg-citus
find /data/dbcomm/citus-dbcomm/src -type d \( -name .deps -o -name 'build-cdc-*' \) \
  -exec sudo -n chown -R dbcomm:dbcomm {} +
```

Some branches use suffixed sibling worktrees. Confirm both active trees before building:

```sh
git -C /data/dbcomm/postgres-citus worktree list
git -C /data/dbcomm/citus-dbcomm worktree list
git -C /data/dbcomm/postgres-citus status --short --branch
git -C /data/dbcomm/citus-dbcomm status --short --branch
```

## Topology and role identity

| role | lane 0 | lane 1 | device/interface |
|---|---|---|---|
| farnet0 host | `10.10.1.100` | `10.10.2.100` | `mlx5_0`/`enp33s0f0np0`; `mlx5_1`/`enp33s0f1np1` |
| farnet1 host | `10.10.1.101` | `10.10.2.101` | `mlx5_0`/`enp33s0f0np0`; `mlx5_1`/`enp33s0f1np1` |
| farnet0 DPU | `10.10.1.200` | — | `enp3s0f0s0` |
| farnet1 DPU | `10.10.1.201` | — | `enp3s0f0s0` |

IPv4 RoCE v2 uses GID index 3. Same-lane RDMA is the validated shape. Cross-lane traffic remains an unresolved
switch-side L2-domain failure, not a verified full mesh; use the [network diagnostic battery](farnet_diagnostics_and_baselines.md#2-network--rdma-diagnostic-battery)
only after reboot/renumber, on failure, or before a fresh absolute link claim.

| endpoint | value |
|---|---|
| Our PostgreSQL | farnet1 `/data/dbcomm/pg-citus/data`, port `5433` |
| Foreign PostgreSQL; never touch | farnet1 `/home/dbcomm/pg/data`, port `5432` |
| Homer DPU peer RDMA | `9717` |
| DPU TCP setup / spawn doorbell | `9727` / `9728` |
| DPU DOCA device | `0000:03:00.0` |
| Host mmap-export device | `0000:21:00.0` |

Farnet1's `postgresql.conf` was verified on 2026-07-16 to pin port 5433. Farnet0 currently has an installed prefix but
no PostgreSQL data directory; only the explicit peer missing-data capability may treat that as clean.

Each host frontend targets its **own** DPU: farnet1→`10.10.1.201`, farnet0→`10.10.1.200`. The gate's
`--homer-peer-host` is different: it names the **remote** farnet1 DPU `10.10.1.201`.

Host↔DPU setup uses TCP for cold descriptor bytes; hot command, completion, and payload movement remains DOCA DMA.
The current `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` is **5**, verified from
`/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h` on 2026-07-16. Re-read the header for
every run. Any bridge ABI change requires both DPUs rebuilt. The frontend arena name remains host-only and must be
sourced from `homer_frontend_agent.h`, never copied from prose.

## Build, install, and artifact proof

### Load-bearing install order

Run builds and installs as `dbcomm`. For acceptance, force the complete non-diagnostic Citus target set first. Only
after that final Citus build may Citus install the archive/shared object and PostgreSQL relink and install against it:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -B -j8 all service-bin client-bin \
  dpu-comch-transport-smoke-bin dpu-tcp-transport-smoke-bin \
  frontend-dma-smoke service-dpu-dma-smoke tuple-deform-smoke \
  service-dpu-dma-doca-smoke service-dpu-comch-smoke-bin \
  CPPFLAGS='-D_GNU_SOURCE'

proof=$(mktemp)
strings build/homer/citus_tuple_sink_service > "$proof"
if grep -E 'starve-diag|progress_machine_baseline_stats' "$proof"; then
  rm -f "$proof"
  echo 'diagnostic instrumentation remains in performance binary' >&2
  exit 1
fi
rm -f "$proof"

sudo -n -u dbcomm make install-headers install-service-bin install

cd /data/dbcomm/postgres-citus
sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build
sudo -n -u dbcomm meson install -C build --no-rebuild

nm -D /data/dbcomm/pg-citus/bin/postgres | grep HomerDpuFrontend
```

The negative scan must find no diagnostic strings. Diagnostic builds and their exit procedure are in
[diagnostics §1](farnet_diagnostics_and_baselines.md#1-diagnostic-builds).

### Complete target build

Every validation-bearing build names the full target set in the load-bearing command above. Never replace it with a
single `service-bin` build.

`service-dpu-dma-smoke` is the only caller of `HomerDpuDmaClaimNextMirroredByteRange`; do not classify the API as dead
from service-source search alone.

### Installed closure

The required installed closure includes `postgres`, `pgbench`, `pg_basebackup`, `citus_tuple_sink_service`,
`citus.so`, installed Homer headers, and `libhomer_client.a`. After a protocol/control-region change, prove consumers
agree:

```sh
strings /data/dbcomm/pg-citus/bin/pgbench | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/bin/citus_tuple_sink_service | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/libhomer_client.a | grep citus_remote_execution_control
```

Record SHA-256 digests of the installed closure and exact build argv/configuration. Matching peer binaries without a
source→build receipt are only parity, not provenance.

### clangd compilation databases

PostgreSQL Meson refreshes `build/compile_commands.json`; keep a local untracked symlink. Citus requires a forced,
non-ccache Bear capture:

```sh
ln -sfn build/compile_commands.json /data/dbcomm/postgres-citus/compile_commands.json
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm bash -c 'CCACHE_DISABLE=1 bear --output compile_commands.json -- make -B -j8 all'
```

Clangd indexes only the active build configuration. Cross-check inactive `#ifdef` branches textually.

## Peer-host artifact deployment

Do not rsync either source checkout. Deploy only the installed runtime prefix, dry-run with checksums first, reject
every nonzero status, then prove the required closure on both hosts. The receiving process remains `dbcomm`; SSH uses
the human account because `dbcomm` has no peer key:

```sh
rsync -azn --delete --checksum --itemize-changes \
  --exclude data/ --exclude '*.log' --exclude logfile --exclude stage_tmp/ \
  -e 'ssh -l jasonhu' --rsync-path='sudo -n -u dbcomm rsync' \
  /data/dbcomm/pg-citus/ farnet0:/data/dbcomm/pg-citus/

rsync -az --delete --checksum \
  --exclude data/ --exclude '*.log' --exclude logfile --exclude stage_tmp/ \
  -e 'ssh -l jasonhu' --rsync-path='sudo -n -u dbcomm rsync' \
  /data/dbcomm/pg-citus/ farnet0:/data/dbcomm/pg-citus/
```

For each required artifact, record local and peer SHA-256 plus rsync argv/return code. Treat checksum stderr or an
unreadable required file as failure. A subsequent checksum dry-run must report no required-content changes.

### Run-scoped DPU source snapshot and build

The old shared `~/dbcomm/citus-dbcomm` rsync tree is not an acceptance source. Use the checked-in snapshot primitive,
which hashes content/mode/symlink identity before and after archive creation and rejects implicit untracked inputs:

```sh
RUN_ID="candidate-$(date +%Y%m%d-%H%M%S)"
RUN_ROOT="/tmp/farnet-validation-$RUN_ID"
CITUS=/data/dbcomm/citus-dbcomm
mkdir -p -m 0700 "$RUN_ROOT"
cd /data/dbcomm/postgres-citus
python3 -c 'import json,sys; from pathlib import Path; from tools.farnet_validation.sources import capture_snapshot; m=capture_snapshot(Path(sys.argv[1]),Path(sys.argv[2])); Path(sys.argv[3]).write_text(json.dumps(m,sort_keys=True,indent=2)+"\n")' \
  "$CITUS" "$RUN_ROOT/citus.tar" "$RUN_ROOT/citus-manifest.json"
```

Do not silently omit untracked source/configuration. If an untracked file is a required runtime input, pass its exact
repo-relative path through `capture_snapshot(..., authorized_untracked=(...))` in a reviewed local script and retain
that script/argv with the manifest; otherwise refuse the current-source claim.

Stage checked-in helpers atomically under `/tmp/farnet-validation-$RUN_ID/helpers`, verify each digest, transfer the
archive, then use `dpu_build.sh` on both DPUs. Run the following from the PostgreSQL repository root. On the direct
farnet1 DPU:

```sh
cd /data/dbcomm/postgres-citus
DPU_HELPERS=(dpu_build.sh dpu_supervisor.sh dpu_process_cleanup.sh \
             dpu_process_probe.sh dpu_tcp_smoke.sh)
ssh dpu install -d -m 0700 "/tmp/farnet-validation-$RUN_ID/helpers"
for helper in "${DPU_HELPERS[@]}"; do
  scp "tools/farnet_validation/remote/$helper" "dpu:/tmp/farnet-validation-$RUN_ID/helpers/.$helper.upload"
  ssh dpu install -m 0700 "/tmp/farnet-validation-$RUN_ID/helpers/.$helper.upload" \
    "/tmp/farnet-validation-$RUN_ID/helpers/$helper"
  local_hash=$(sha256sum "tools/farnet_validation/remote/$helper" | awk '{print $1}')
  remote_hash=$(ssh dpu sha256sum "/tmp/farnet-validation-$RUN_ID/helpers/$helper" | awk '{print $1}')
  [[ "$local_hash" == "$remote_hash" ]] || exit 1
done
scp "$RUN_ROOT/citus.tar" "dpu:/tmp/farnet-validation-$RUN_ID/citus.tar"
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_build.sh" "$RUN_ID" \
  "/tmp/farnet-validation-$RUN_ID/citus.tar" service-bin dpu-tcp-transport-smoke-bin
```

For farnet0's DPU, stage `dpu_dispatch.sh` and the DPU helpers on farnet0, then invoke that checked-in file by path;
never inline the second hop:

```sh
REMOTE="/tmp/farnet-validation-$RUN_ID"
HOST_HELPERS=(dpu_dispatch.sh postgres_stop.sh host_process_cleanup.sh host_process_probe.sh \
              project_shm_cleanup.sh gate.sh basebackup_consumer.sh)
ssh farnet0 install -d -m 0700 "$REMOTE/helpers"
for helper in "${HOST_HELPERS[@]}" "${DPU_HELPERS[@]}"; do
  scp "tools/farnet_validation/remote/$helper" "farnet0:$REMOTE/helpers/.$helper.upload"
  ssh farnet0 install -m 0700 "$REMOTE/helpers/.$helper.upload" "$REMOTE/helpers/$helper"
  local_hash=$(sha256sum "tools/farnet_validation/remote/$helper" | awk '{print $1}')
  remote_hash=$(ssh farnet0 sha256sum "$REMOTE/helpers/$helper" | awk '{print $1}')
  [[ "$local_hash" == "$remote_hash" ]] || exit 1
done
scp "$RUN_ROOT/citus.tar" "farnet0:$REMOTE/citus.tar"
ssh farnet0 "$REMOTE/helpers/dpu_dispatch.sh" prepare
for helper in "${DPU_HELPERS[@]}"; do
  ssh farnet0 "$REMOTE/helpers/dpu_dispatch.sh" install "$helper"
done
ssh farnet0 "$REMOTE/helpers/dpu_dispatch.sh" install-data "$REMOTE/citus.tar"
ssh farnet0 "$REMOTE/helpers/dpu_dispatch.sh" dpu_build.sh "$RUN_ID" \
  "$REMOTE/citus.tar" service-bin dpu-tcp-transport-smoke-bin
```

Record archive/helper/configuration/output digests from both DPUs. A bridge/ABI change is not deployed until both DPU
build receipts match the same source archive and the expected outputs exist.

## Clean baseline

Use one shell-safe `RUN_ID` throughout. Stage host helpers on farnet0 with the same upload/install/digest pattern used
above. Stop only farnet1's intended data directory; farnet0's absent data is clean only through its explicit flag:

```sh
bash tools/farnet_validation/remote/postgres_stop.sh /data/dbcomm/pg-citus
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/postgres_stop.sh" \
  /data/dbcomm/pg-citus --allow-missing-data
```

Reap host clients/services and socketless backends only through canonical process identity. The helper fails closed
on an unreadable user process:

```sh
bash tools/farnet_validation/remote/host_process_cleanup.sh /data/dbcomm/pg-citus
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/host_process_cleanup.sh" /data/dbcomm/pg-citus
```

Reap both DPU-native services and prove ports 9727/9728 have no listener:

```sh
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_process_cleanup.sh" "$RUN_ID"
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_process_cleanup.sh "$RUN_ID"
```

Inventory, then remove only project-owned shared memory. The checked-in helper refuses foreign ownership:

```sh
ls -lh /dev/shm | grep -E 'citus|homer|remote_exec'
bash tools/farnet_validation/remote/project_shm_cleanup.sh
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/project_shm_cleanup.sh"
```

The owned families are `citus_remote_execution_control_*`, `citus_remote_exec_{cmd,cpl,client_cpl}_*`,
`citus_remote_exec_backend_spawn_v*`, `citus_homer_frontend_arena_*`, and `citus_res_*`. If the backend-spawn region
is removed after PostgreSQL starts, restart PostgreSQL. Source the current arena name from
`homer_frontend_agent.h`; never hard-code its version.

## Start roles and prepare workload

### PostgreSQL with frontend agent

Farnet1's port 5433 is pinned in `postgresql.conf`. The frontend environment must be inherited by the postmaster:

```sh
sudo -n -u dbcomm env \
  HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.201 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
  HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
  /data/dbcomm/pg-citus/bin/pg_ctl -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log \
  -o "-c citus.enable_homer_dpu_frontend_agent=on" start -w
```

Confirm the exact instance:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select current_setting('data_directory')"
```

The only accepted output is `/data/dbcomm/pg-citus/data`.

### DPU services

Selected-DPU workloads, including every Homer basebackup target, require both native DPU services. Use the run-scoped
supervisors so process start time, binary digest, listener ownership, and actual wait status are recorded:

```sh
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_supervisor.sh" \
  start "$RUN_ID" 10.10.1.201
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_supervisor.sh start "$RUN_ID" 10.10.1.200
```

Each child must own both 9727 and 9728. `HOMER_SERVICE_ENABLE_DPU_DMA=1` requires the machine-baseline progress
policy. Do not start either host `citus_tuple_sink_service` for the selected-DPU gate.

### Schema and identities

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5433 -U dbcomm -i -s "${SCALE:-1}" postgres

DBOID=$(sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select oid from pg_database where datname = 'postgres'")
USEROID=$(sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select oid from pg_roles where rolname = 'dbcomm'")
printf 'DBOID=%s USEROID=%s\n' "$DBOID" "$USEROID"
```

Before each paired performance group, reset the insert-only history and refresh table statistics:

```sh
for sql in "truncate pgbench_history" "vacuum analyze pgbench_accounts" \
           "vacuum analyze pgbench_branches" "vacuum analyze pgbench_tellers"; do
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm \
    -v ON_ERROR_STOP=1 postgres -c "$sql"
done
```

### Candidate preflight

Immediately before every candidate, use the canonical helpers on both hosts and DPU supervisor status on both DPUs.
A quick `ps` listing is only an eyeball aid, never identity proof:

```sh
ps -eo pid,ppid,psr,comm,args | \
  grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' | grep -v grep
bash tools/farnet_validation/remote/host_process_probe.sh /data/dbcomm/pg-citus running
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/host_process_probe.sh" \
  /data/dbcomm/pg-citus stopped
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_supervisor.sh" status "$RUN_ID"
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_process_probe.sh" "$RUN_ID" one
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_supervisor.sh status "$RUN_ID"
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_process_probe.sh "$RUN_ID" one
```

The clean selected-DPU start has farnet1 PostgreSQL, both intended DPU supervisors/services, and no host service,
old clients, walsenders, or socketless backends. Record the database query, process identities, DPU service
PID/start/digest, listener ownership, and candidate log marks.

## Acceptance profile

Gate and basebackup are complementary and both mandatory for transport acceptance. Add the DPU TCP smoke before and
after DPU DMA, byte-ring, or bridge/ABI changes. Broken host-service pgbench, backend-to-backend COPY, standalone
`--homer-dpu`, and the obsolete mixed-workload shape are not operator procedures; see
[diagnostics §4](farnet_diagnostics_and_baselines.md#4-broken-command-shapes-kept-so-they-stay-recognisable) and the
[host-service removal contract](../implementations/citus/transport/copy_path_revival_contract.md).

### DPU TCP transport smoke

Run the checked-in server supervisor and host client in one uninterrupted launch window. The helper supplies all six
server expectation flags; the host command supplies the same six leg flags plus the host-only POSIX-shm/fork-reader
proof. Ensure no DPU service owns port 9727 first, and retain the helper's server log and real wait status:

```sh
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_tcp_smoke.sh" start "$RUN_ID"

/data/dbcomm/citus-dbcomm/build/homer/homer_dpu_tcp_transport_smoke \
  --client --host 10.10.1.201 --dev-pci 0000:21:00.0 --port 9727 --timeout-ms 40000 \
  --export-posix-shm --fork-reader \
  --expect-backend-command-publish --expect-response-publish \
  --expect-backend-completion-pull --expect-byte-ring-pull \
  --expect-dpu-to-host-payload --expect-loop2-backpressure

ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_tcp_smoke.sh" wait "$RUN_ID"
```

Both endpoints must print `homer_dpu_tcp_transport_smoke: ok`. The client exit code alone is not the verdict. Follow
the launch/lifetime traps in [hazards §7](farnet_operational_hazards.md#7-smoke-test-hazards-homer_dpu_tcp_transport_smoke).

### Selected-DPU command gate

Roles:

| role | process |
|---|---|
| farnet0 host | `pgbench`; no host service |
| farnet0 DPU | service bound `10.10.1.200:9717` |
| farnet1 DPU | service bound `10.10.1.201:9717` |
| farnet1 host | PostgreSQL with frontend agent; no host service |

Bracket both DPU logs before each candidate:

```sh
ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_supervisor.sh" mark "$RUN_ID" debug-gate
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_supervisor.sh mark "$RUN_ID" debug-gate
```

Stage and invoke the checked-in `gate.sh` on farnet0. Its local frontend targets `10.10.1.200`; its peer target is
the remote DPU `10.10.1.201`:

```sh
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/gate.sh" \
  /data/dbcomm/pg-citus "$DBOID" "$USEROID" 5 1
```

A debug gate requires four independent proofs:

1. client transport line is `homer-dpu-command (implies dpu result relay)`;
2. five distinct decoded `abalance` values, `5/5` processed, zero failed;
3. farnet1 DPU candidate interval contains backend spawn begin and completed with launched PID;
4. canonical process probes prove both host services absent, making host-service fallback impossible.

Collect both candidate intervals and run every applicable checker from `tools/farnet_validation/checks.py` over the
recorded device/inode/offset ranges. The fallback alarm scan must include untagged fatal strings:

```sh
grep -aiE 'ALARM|semantic validation failed|fatal error state|PE drain failed|pool exhausted|peer-open failed' \
  candidate-dpu-service.log
```

The byte-ring pool currently permits at most eight gate clients; prefer four for headroom. Size backend and client CPU
sets one-per-busy-poller and keep them disjoint. The capacity and failure blast radius are owned by
[`dpu_gate_concurrency_limits.md`](../implementations/citus/transport/dpu_gate_concurrency_limits.md).

After any failed, timed-out, aborted, or interrupted gate, restart **both** DPU services before retrying. The failure
can leak service-owned arena/session state that OS-process cleanup alone cannot release. Read both DPU logs before
opening source; node A drives commands and node B executes them.

For measured candidates, run a stripped warmup and then the full repeat set without restarting roles. Do not compare
debug output with stripped performance results, and do not copy a TPS number into this runbook; use the SHA-stamped
[measurement history](farnet_diagnostics_and_baselines.md#3-measurement-history).

### Four-role basebackup

This is the only validated basebackup topology and the wrap exerciser:

| role | process |
|---|---|
| farnet1 host | PostgreSQL source plus sender |
| farnet1 DPU | sender-side service |
| farnet0 DPU | receiver-side service |
| farnet0 host | `pg_basebackup --homer-receive`; no PostgreSQL required |

Use `slots=4,bytes=524288`, the same unique tag on both roles, and `-X none`. Start and prove the consumer supervisor
first using the checked-in helper:

```sh
TAG=t$(printf '%s' "$RUN_ID" | tr -cd 'A-Za-z0-9' | tail -c 11)
# The leading letter is LOAD-BEARING: an all-numeric tag is parsed as int32 by the basebackup
# `tag=` target option (pg_strtoint32, basebackup_homer.c:~233) and a 12-digit run-derived tag
# overflows it -- the sender aborts and the still-waiting receiver hangs (observed 2026-07-17).
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/basebackup_consumer.sh" \
  start "$RUN_ID" /data/dbcomm/pg-citus "$DBOID" "$USEROID" "$TAG" 4 524288
```

Then run the farnet1 sender:

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5433 -U dbcomm -X none -c fast \
  -t "homer:mode=rdma,host=10.10.1.200,port=9717,node=2,slots=4,bytes=524288,tag=$TAG" -v
```

Finally wait for and emit the receiver's real status:

```sh
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/basebackup_consumer.sh" wait "$RUN_ID"
```

Acceptance requires sender and consumer success, matching geometry/tag, expected semantic byte totals, and candidate
evidence that the DPU relay crossed the ring wrap without fatal/reset/frontier or teardown-ledger failure. Run the
applicable `basebackup_check`, frontier, alarm, and teardown checkers over explicitly bracketed role intervals. A host
receiver address or oversized record geometry belongs only in the broken-shape archive, not this procedure.

## Measured candidates

- Warm once after every restart, sync, geometry change, or peer setup; never quote warmup.
- Keep roles running for the complete warmed repeat set and report every repeat.
- Keep stats macros and verbose logging off. Use a separate debug candidate for decoded-value proof.
- Record run ID, both source identities, artifact receipts/digests, DPU receipts, exact argv/env, CPU sets, client and
  transaction counts, log intervals, return statuses, and full checker output.
- A timed-out/interrupted/manually cleaned attempt is diagnostic-only; return to clean/start/preflight.

## Regression sweep

For transport changes, execute in this order:

1. full target build and artifact receipt;
2. conditional pre-change DPU TCP smoke receipt when the task changes DMA/byte-ring/bridge behavior;
3. clean/start/preflight;
4. debug gate, stripped warmup, full gate repeat set;
5. four-role basebackup;
6. conditional post-change DPU TCP smoke;
7. post-workload setup-listener liveness with frontend agent still attached;
8. ordered teardown and final process/listener scan.

The frontend agent creates persistent imported state. Acceptance of the import is not enough: after a workload, prove
port 9727 remains listening with `Recv-Q 0` and that a subsequent DPU setup connection succeeds.

When adding scheduler enums or collectors, run the manual audit required by repository policy:

```sh
grep -n 'switch ((HomerProgressSourceKind)\|switch (sourceKind)' \
  /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c
grep -n 'FindCollector(candidateSet, HOMER_PROGRESS_COLLECTOR_' \
  /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c
```

## Teardown and verdict

Normal teardown order is load-bearing:

1. finish the workload and capture live candidate intervals;
2. stop farnet1 PostgreSQL by its exact data directory;
3. reap verified host socketless backends/clients;
4. stop/wait both DPU supervisors and capture their actual statuses;
5. collect full DPU logs and run teardown/frontier/alarm ledgers;
6. remove project shared memory when required;
7. rerun host/DPU process and listener preflight.

```sh
bash tools/farnet_validation/remote/postgres_stop.sh /data/dbcomm/pg-citus
bash tools/farnet_validation/remote/host_process_cleanup.sh /data/dbcomm/pg-citus
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/host_process_cleanup.sh" /data/dbcomm/pg-citus

ssh dpu "/tmp/farnet-validation-$RUN_ID/helpers/dpu_supervisor.sh" stop "$RUN_ID"
ssh farnet0 "/tmp/farnet-validation-$RUN_ID/helpers/dpu_dispatch.sh" \
  dpu_supervisor.sh stop "$RUN_ID"
```

Do not kill a live socketless backend before the postmaster stops. Do not infer clean service exit from a vanished PID;
capture the supervisor's real wait status. A final report must answer:

- Did the intended Homer path run on candidate-owned artifacts and candidate-bracketed logs?
- Was the process/listener/shared-memory state clean before and after the candidate?
- Did correctness, frontier, alarm, and teardown predicates all pass without missing or contradictory evidence?

Return `PASS` only when all selected-profile predicates and identity/provenance requirements pass. A directly
reproduced correctness/path failure is `FAIL`; missing identity, contaminated state, incomplete evidence, or uncertain
cleanup is `INCONCLUSIVE`.

For an unexpected live state, stop later phases, preserve a bounded mode-0700 takeover manifest, and follow the
repository policy. Never clean away first-frontier evidence before the bounded capture.

## Related knowledge

- [Operational hazards](farnet_operational_hazards.md)
- [Diagnostics and measurement history](farnet_diagnostics_and_baselines.md)
- [Deterministic runner plan](farnet_validation_runner_plan.md)
- [Transport contracts](../implementations/citus/transport/CONTRACTS.md)
- [Selected-DPU lifecycle](../implementations/citus/transport/selected_dpu_session_rings_and_lifecycle.md)
- [Host-service removal boundary](../implementations/citus/transport/copy_path_revival_contract.md)
