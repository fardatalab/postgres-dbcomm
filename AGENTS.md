# Homer / farnet project guide

This file is the **machine setup and the runbook**. It answers "what are the facts" and "what do I run".

It deliberately does **not** carry the incident narratives, the diagnostic batteries, or the historical
numbers. Those live in the KB, because a runbook you have to skim past is a runbook people stop reading:

- **`docs/kb/operations/farnet_operational_hazards.md`** — every check in this project that lies, and the
  incident that proved it. **Read this once, in full.** Every ⚠ rule below is a one-line summary of an entry
  there.
- **`docs/kb/operations/farnet_diagnostics_and_baselines.md`** — the diagnostic builds (`starve-diag`, stats
  macros), the RDMA/RoCE link battery, the measurement history, and the broken command shapes kept only so
  they stay recognisable.

## Project setup background

Two source trees:

- PostgreSQL fork: `/data/dbcomm/postgres-citus`
- Citus/Homer fork: `/data/dbcomm/citus-dbcomm`

Installed runtime prefix `/data/dbcomm/pg-citus`; runtime user `dbcomm`. Build dirs, installed
executables/libraries/headers, and workload-created runtime files are all owned by `dbcomm`. Run build,
install, service, and workload commands **as `dbcomm`** (`sudo -n -u dbcomm ...` if your shell is another
user) — never produce root-owned build or install artifacts. If ownership drifts, normalize it explicitly
before continuing rather than mixing privilege styles:

```sh
sudo -n chown -R dbcomm:dbcomm \
  /data/dbcomm/postgres-citus*/build /data/dbcomm/citus-dbcomm*/build /data/dbcomm/pg-citus
```

Some branches use `-separate-comm-stack` suffixed trees. **Confirm the active tree for the current task**;
do not assume a path from an example.

The project is a database systems research prototype for communication-stack efficiency and offload. New
Homer code lives under a `homer/` subdirectory in the Postgres or Citus tree.

---

## ⚠ Non-negotiables

Each of these has already cost at least one debugging cycle, and several cost days. The reason is one click
away — follow the link before you "simplify" a rule.
(→ `docs/kb/operations/farnet_operational_hazards.md`)

1. **farnet1 runs TWO PostgreSQL instances under the same `dbcomm` user. Ours is on port `5433`.**
   `/home/dbcomm/pg` (port `5432`) belongs to a *different* Postgres project — **never stop, kill, or query
   it.** Both defaulted to 5432 + `/tmp`, so they used to race for the same socket; ours is now pinned to
   `5433` in `postgresql.conf` on **both** hosts. Every `psql`/`pgbench`/`pg_basebackup` needs **`-p 5433`**,
   or OID lookups silently hit the foreign catalog and `pgbench -i` **recreates tables in their database**.
   (§2)
2. **Never `pkill -f` / `pgrep -f` from an `ssh` one-liner or `bash -c`** — the pattern sits in the caller's
   own argv, so it matches itself. It has killed the ssh session, and it has silently *not* killed the
   target. `pkill -x` does not save you (`comm` truncates to 15 chars). Resolve identity from
   `/proc/<pid>/exe`, with a `sudo -n readlink` fallback and a **trailing `*`** in the pattern (for
   `(deleted)` exes). (§1)
3. **Never `kill -9` a LIVE `postgres: remote exec backend` mid-run** — it triggers a full postmaster
   crash-restart. Reap only at end-of-run, after stopping PostgreSQL. Since S3.3b these backends **survive
   `pg_ctl stop -m fast`** and must be reaped by `/proc/<pid>/exe`. (§1.5)
4. **Write multi-line scripts to a FILE and invoke by path.** A heredoc inside `bash -c` leaks its body into
   the caller's argv, which re-arms trap #2. (§1.1, §6.2)
5. **Never inline a double-hop ssh command** (`ssh farnet0 ssh dpu "cd ~/..."`) — farnet0's shell expands `~`
   to *farnet0's* home. Ship a script; invoke it by path. (§6.3)
6. **Never run a write-capable subagent concurrently with your own edits in the same tree.** One silently
   *reverted* ~30 hand-applied edits. Use `isolation: "worktree"`, or hold your edits until it reports. (§6.1)
7. **A `make -B` diagnostic build is sticky.** A plain `make` will relink the instrumented objects. Exit a
   diagnostic build with another `make -B`, and **prove** it. (§3.1)
8. **Stamp every recorded baseline with a commit SHA, not just a date.** A dated baseline reads like a
   standing fact; it is a timestamped observation. (§8.2)
9. **Backticks inside `git commit -m "..."` trigger command substitution.** Use `-F <file>`. (§6.4)

---

## Farnet topology

`farnet1` is the primary development and benchmark driver host; `farnet0` is the peer. Both have the same
`/data/dbcomm` layout and the same `dbcomm` user. Reach the peer with `ssh farnet0`; reach each host's DPU
with `ssh dpu` **from that host**.

`/data` is local ext4 on both hosts. **Do not rely on NFS** — farnet1 may still advertise an old `/data`
export; use explicit `rsync`. Never sync a live database data directory unless PostgreSQL is stopped and
replacing it is the explicit goal.

### Addresses (verified June 17 / June 28, 2026)

| role | fast-link lane 0 | lane 1 | notes |
|---|---|---|---|
| farnet0 host | `10.10.1.100` | `10.10.2.100` | `enp33s0f0np0` / `enp33s0f1np1` |
| farnet1 host | `10.10.1.101` | `10.10.2.101` | `enp33s0f0np0` / `enp33s0f1np1` |
| farnet0 DPU | `10.10.1.200` | — | `enp3s0f0s0` |
| farnet1 DPU | `10.10.1.201` | — | `enp3s0f0s0` |

`mlx5_0` → `enp33s0f0np0`; `mlx5_1` → `enp33s0f1np1`; IPv4 RoCE v2 uses **GID index 3**. Both host and both
DPU fast-link ports report 400 Gb/s, 4 lanes, full duplex.

**Cross-lane RDMA does NOT work** (lane 0 ↔ lane 1 fails with `transport retry counter exceeded`). This is a
switch-side L2-domain problem, not a Homer bug, and it is unresolved. Do not treat this as a verified
four-port full mesh.
(→ `docs/kb/operations/farnet_diagnostics_and_baselines.md` §2.3)

### Ports and devices

| thing | value | override |
|---|---|---|
| **our PostgreSQL** | **`5433`** (both hosts) | in `postgresql.conf` |
| foreign PostgreSQL — do not touch | `5432` (farnet1 only) | — |
| Homer service peer RDMA | `9717` | `HOMER_SERVICE_PEER_PORT` |
| DPU service TCP **setup** listener | `9727` (bind `0.0.0.0`) | `HOMER_SERVICE_DPU_SETUP_{BIND_HOST,PORT}` |
| DPU service spawn **doorbell** | `9728` | `HOMER_SERVICE_DPU_DOORBELL_PORT` |
| DPU DOCA DMA device | `0000:03:00.0` | `HOMER_SERVICE_DOCA_DEV_PCI` |
| Host DOCA mmap-export device | `0000:21:00.0` | `HOMER_FRONTEND_DOCA_DEV_PCI` |
| Host frontend → DPU setup target | its **own local** DPU | `HOMER_FRONTEND_DPU_SETUP_{HOST,PORT}`, `..._TIMEOUT_MS` |

⚠ **Each host's frontend must point at its OWN local DPU** — farnet1 → `10.10.1.201`, farnet0 →
`10.10.1.200`. Pointing a farnet0-resident client at `10.10.1.201` fails with
`DPU setup rejected frontend export status=5`. The DOCA PCI id is the same on both hosts (symmetric
hardware).

### Host ↔ DPU setup uses TCP, not DOCA COMCH

DOCA COMCH repeatedly failed below Homer after firmware/driver resets, while DOCA DMA still validated. The
TCP setup socket carries the same cold-path bytes (host PCI mmap export descriptor, bridge header, ring
descriptors, setup ack). **Hot command/completion/payload movement remains DOCA DMA.** DMA does not take a
representor.

`HOMER_DPU_BRIDGE_PROTOCOL_VERSION` is currently **`3`** (role 8, `BACKEND_SPAWN_REGION`). **A bridge/COMCH
ABI bump means BOTH DPUs must be resynced and rebuilt**, or the setup handshake fails with `BAD_PROTOCOL`.
The **frontend arena** ABI (`citus_homer_frontend_arena_v3`) is **host-only** and needs no DPU redeploy.

---

## Build and install

When source changes are involved, assume **both** trees must be rebuilt and reinstalled before validation.

### ⚠ Install order is load-bearing (since command-plane S2)

`citus.so` is built `-fvisibility=hidden` and leaves `HomerDpuFrontend*` **undefined**; those symbols resolve
at `dlopen` time out of the **`postgres` executable**, which statically links `libhomer_client.a`. Installing
a new `citus.so` against an old `postgres` fails **every** backend with
`undefined symbol: HomerDpuFrontendOpenDevice`.

```sh
# 1. citus first -- installs the new libhomer_client.a AND citus.so
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -j8
sudo -n -u dbcomm make install-headers install-service-bin install

# 2. postgres -- this RELINKS `postgres` against that archive
cd /data/dbcomm/postgres-citus
sudo -n -u dbcomm env CCACHE_DISABLE=1 ninja -C build

# 3. install
sudo -n -u dbcomm meson install -C build --no-rebuild

# 4. only now start
nm -D /data/dbcomm/pg-citus/bin/postgres | grep HomerDpuFrontend   # sanity
```

For a performance build, force the service/client binaries with no stats macros:

```sh
cd /data/dbcomm/citus-dbcomm
sudo -n -u dbcomm make -B -j8 service-bin client-bin CPPFLAGS='-D_GNU_SOURCE'
sudo -n -u dbcomm make install-headers install-service-bin install
```

Diagnostic builds (stats macros, `starve-diag`, `HOMER_REMOTE_EXEC_TRACE`) are in
`docs/kb/operations/farnet_diagnostics_and_baselines.md` §1. **They are sticky — exit with `make -B` and
prove the instrumentation is gone** (Non-negotiable #7).

### Build every target (there are seven smoke targets)

The DPU TCP transport smoke silently failed to compile for three days because the standing recipe named one
target. Copy this verbatim:

```sh
sudo -n -u dbcomm make -j8 all service-bin client-bin \
  dpu-comch-transport-smoke-bin dpu-tcp-transport-smoke-bin \
  frontend-dma-smoke service-dpu-dma-smoke tuple-deform-smoke \
  service-dpu-dma-doca-smoke service-dpu-comch-smoke-bin \
  CPPFLAGS='-D_GNU_SOURCE'
```

`service-dpu-dma-smoke` is the **only** caller of `HomerDpuDmaClaimNextMirroredByteRange` — an engine API
that looks dead if you grep only the service sources.

### DPU trees

The current DPU ARM tree is `~/dbcomm/citus-dbcomm` on **both** DPUs (a plain non-git rsync dir). After
rsyncing sources you **must re-run configure** — the copied `Makefile.global` pins `/data/dbcomm` paths that
do not exist there:

```sh
ssh dpu "cd ~/dbcomm/citus-dbcomm && PG_CONFIG=/usr/bin/pg_config ./configure --without-libcurl \
  && make -j8 service-bin CPPFLAGS='-D_GNU_SOURCE'"
```

⚠ `rsync -a` preserves mtime, so `make` will skip the file you just edited — `touch` it, or use `make -B`.
And **prove the deploy landed** by grepping the binary for a string literal only the new code emits; grepping
for a macro *name* proves nothing in either direction (hazards §3.2, §3.3).

### Installed artifacts

`/data/dbcomm/pg-citus/bin/{postgres,pgbench,pg_basebackup,citus_tuple_sink_service}` and
`/data/dbcomm/pg-citus/lib/x86_64-linux-gnu/postgresql/citus.so`.

After changing shared Homer protocol headers or control-region names, verify the installed binaries agree —
a stale `pgbench` or `libhomer_client.a` silently maps the **old** shared-memory name:

```sh
strings /data/dbcomm/pg-citus/bin/pgbench                  | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/bin/citus_tuple_sink_service | grep citus_remote_execution_control
```

### clangd / `compile_commands.json`

- **postgres-citus (Meson)** regenerates `build/compile_commands.json` on every build. Symlink it once:
  `ln -sfn build/compile_commands.json /data/dbcomm/postgres-citus/compile_commands.json`
  (keep the symlink out of git via `.git/info/exclude`).
- **citus-dbcomm (autotools)** needs `bear`. Regenerate only when the build graph or flags change:
  ```sh
  cd /data/dbcomm/citus-dbcomm
  sudo -n -u dbcomm bash -c 'CCACHE_DISABLE=1 bear --output compile_commands.json -- make -B -j8 all'
  ```
  `-B` is required (bear only records commands that actually run — a no-op build yields an **empty** DB);
  `CCACHE_DISABLE=1` keeps entries as plain `gcc ...` (bear may skip `ccache`-wrapped commands, which clangd
  then cannot parse). To add a few targets to an existing DB without rebuilding everything:
  `bear --append -- make -B <targets>`.

⚠ clangd indexes only the **active build configuration** — code behind inactive `#ifdef`s (non-DOCA, stats
builds) is **not** indexed and `findReferences` will silently miss it. Cross-check with textual search.

---

## Sync farnet1 → farnet0

Dry-run first (`-n`), then remove it. Exclude local logs — they are not needed for validation and can make
rsync return `23` even after the important artifacts copied.

```sh
rsync -azn --delete --itemize-changes /data/dbcomm/postgres-citus/ farnet0:/data/dbcomm/postgres-citus/
rsync -azn --delete --itemize-changes /data/dbcomm/citus-dbcomm/   farnet0:/data/dbcomm/citus-dbcomm/

rsync -az --delete \
  --exclude data/ --exclude '*.log' --exclude logfile --exclude stage_tmp/ \
  --rsync-path='sudo -n -u dbcomm rsync' \
  /data/dbcomm/pg-citus/ farnet0:/data/dbcomm/pg-citus/
```

---

## Clean runtime baseline

Unless the goal is specifically to study warm reuse, start every validation from a clean baseline **on both
hosts**.

1. **Stop PostgreSQL by data dir** (never by port — that is how you hit the foreign instance):

   ```sh
   sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl -D /data/dbcomm/pg-citus/data stop -m fast || true
   ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl -D /data/dbcomm/pg-citus/data stop -m fast || true"
   ```

2. **Reap Homer processes by `/proc/<pid>/exe`** — never `pkill -f` (Non-negotiable #2). Write this to a
   script file and invoke it by path:

   ```sh
   for d in /proc/[0-9]*; do
     exe=$(readlink -f "$d/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$d/exe" 2>/dev/null)
     case "$exe" in
       */pg-citus/bin/citus_tuple_sink_service*) sudo -n kill -9 "${d#/proc/}" ;;
       */pg-citus/bin/postgres*)  # only socketless backends; the postmaster is already stopped
         args=$(sudo -n tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
         case "$args" in *"remote exec backend"*) sudo -n kill -9 "${d#/proc/}" ;; esac ;;
     esac
   done
   ```

   **Count the pids you could not identify and say so** — an unreadable `/proc` must not masquerade as an
   empty one.

3. **Remove only OUR shared memory**, by glob:

   ```sh
   ls -lh /dev/shm | grep -E 'citus|homer|remote_exec'
   ```

   | object | created by |
   |---|---|
   | `citus_remote_execution_control_*` | `citus_tuple_sink_service` |
   | `citus_remote_exec_{cmd,cpl,client_cpl}_*` | per-client/per-session command + completion rings |
   | `citus_remote_exec_backend_spawn_v*` | the PostgreSQL backend bridge, at postmaster start |
   | `citus_homer_frontend_arena_*` | the postmaster, **only** with `citus.enable_homer_dpu_frontend_agent=on` |
   | `citus_res_*` | payload/result queues of active or recent sessions |

   ⚠ **The arena's version is in its NAME** (`_v3` today). Delete it **by glob**, and confirm the current
   version from the header, not from any doc — this note said `_v2` for a month while the code was on `_v3`,
   so the documented cleanup was deleting a nonexistent object and leaving the real arena in place
   (hazards §4):
   `grep HOMER_FRONTEND_ARENA_SHM_NAME /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_agent.h`

   If you remove `citus_remote_exec_backend_spawn_v*` **after** PostgreSQL has started, **restart
   PostgreSQL** — otherwise clients fail to open the backend-spawn region.

   **Do not remove shared memory belonging to other users' experiments.**

---

## Process preflight (before EVERY measured run)

Do this even after a successful restart. Aborted pgbench/basebackup runs and timed-out peer experiments leave
client and socketless-backend processes behind, and **any result collected on top of that state is
contaminated**.

⚠ `ps -eo args | grep -E ...` matches the calling shell and its own grep, so a clean machine reports
"leftovers". Resolve identity from `/proc/<pid>/exe` (Non-negotiable #2). A quick eyeball version, understood
as approximate:

```sh
ps -eo pid,ppid,psr,comm,args | \
  grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' | grep -v grep
```

A clean start has exactly: the intended `citus_tuple_sink_service` on each host, plus the PostgreSQL
postmaster/background processes on hosts where PostgreSQL is intentionally running. **Nothing else.** If an
old `pgbench`, `pg_basebackup`, `walsender`, or `postgres: remote exec backend` is present, clean first,
rerun the preflight, and record that it was clean.

**Confirm you are talking to OUR PostgreSQL** — this query names the data dir, so it cannot lie:

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select current_setting('data_directory')"
# want: /data/dbcomm/pg-citus/data
```

**If a candidate run times out, is interrupted, or needs manual cleanup, it is diagnostic-only.** Do not use
it for an acceptance or rejection claim.

---

## Start PostgreSQL

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_ctl \
  -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log start -w
```

Port `5433` comes from `postgresql.conf`; no `-o` needed. PostgreSQL is required on farnet1 for pgbench and
the basebackup sender. `farnet0` needs PostgreSQL only when the experiment needs a database instance there
(backend-to-backend COPY does; the 4-role basebackup does not).

**With the DPU frontend agent** (required for at least one workload in the regression sweep — see below), the
frontend DPU env must be in the **postmaster's** environment, not just the client's:

```sh
sudo -n -u dbcomm env \
  HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.201 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
  HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
  /data/dbcomm/pg-citus/bin/pg_ctl -D /data/dbcomm/pg-citus/data \
  -l /data/dbcomm/pg-citus/data/postgres.log \
  -o "-c citus.enable_homer_dpu_frontend_agent=on" start -w
```

---

## Start Homer services

Use **code-level pinning**, not `taskset`. Keep CPU sets **disjoint** across the standalone service, the
socketless backends it spawns, and the pgbench worker threads.

```sh
# farnet1
sudo -n -u dbcomm sh -c 'env \
  HOMER_SERVICE_CPU=2 HOMER_REMOTE_EXEC_BACKEND_CPUS=4,5,6,7 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.101 HOMER_SERVICE_PEER_PORT=9717 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service \
  > /data/dbcomm/pg-citus/data/homer_service_farnet1.log 2>&1 &'

# farnet0 (same, with its own bind host)
ssh farnet0 "sudo -n -u dbcomm sh -c 'env \
  HOMER_SERVICE_CPU=2 HOMER_REMOTE_EXEC_BACKEND_CPUS=4,5,6,7 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.100 HOMER_SERVICE_PEER_PORT=9717 \
  /data/dbcomm/pg-citus/bin/citus_tuple_sink_service \
  > /data/dbcomm/pg-citus/data/homer_service_farnet0.log 2>&1 &'"
```

Both hosts may listen on `9717`. Two services on the *same* host need distinct `HOMER_SERVICE_PEER_PORT`,
`HOMER_SERVICE_CONTROL_SHM_NAME`, and `HOMER_SERVICE_SHM_TAG`.

### DPU services

Any selected-DPU workload (**all** Homer basebackup targets, including plain `mode=rdma`) needs the DPU
service running. It is a **mandatory** role, not optional — `bbsink_homer_begin_backup`
(`src/backend/backup/basebackup_homer.c`) calls `HomerClientOpenBaseBackupStreamSelectedDpu`
unconditionally, so `pg_basebackup` otherwise fails with
`could not connect to DPU setup listener 10.10.1.201:9727`.

Run the native aarch64 binary directly as `ubuntu`, under `nohup setsid … </dev/null &` **invoked from a
script on the DPU** (without `nohup` it dies when the ssh channel closes, and the only symptom is a client-side
`could not connect`):

```sh
env HOMER_SERVICE_ENABLE_DPU_DMA=1 HOMER_SERVICE_ENABLE_DOCA_DMA=1 \
    HOMER_SERVICE_DPU_SETUP_PORT=9727 HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0 \
    ~/dbcomm/citus-dbcomm/build/homer/citus_tuple_sink_service
```

The DPU binds **two** ports: `9727` (setup) and `9728` (spawn doorbell). Confirm the frontend attached with
`homer frontend agent: attached DPU spawn doorbell …` in `postgres.log`.

⚠ **`HOMER_SERVICE_ENABLE_DPU_DMA=1` requires the machine-baseline progress policy.** Under any other policy
the DPU service starts, binds 9727, and **never accepts** (hazards §5.3).

---

## Prepare the pgbench schema

On farnet1. Citus is not required for the single-node pgbench workload.

```sh
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench \
  -h /tmp -p 5433 -U dbcomm -i -s "${SCALE:-1}" postgres

DBOID=$(sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select oid from pg_database where datname = 'postgres'")
USEROID=$(sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm -AtX postgres \
  -c "select oid from pg_roles where rolname = 'dbcomm'")
printf 'DBOID=%s USEROID=%s\n' "$DBOID" "$USEROID"
```

⚠ **`-p 5433`, always.** On 5432 this initializes pgbench tables **inside another project's database** and
returns *their* OIDs, with no error (Non-negotiable #1).

Before each paired performance run group, reset the insert-only history table and refresh stats — a stale
`pgbench_history` with millions of rows has already produced a **false Homer regression signal**:

```sh
for sql in "truncate pgbench_history" "vacuum analyze pgbench_accounts" \
           "vacuum analyze pgbench_branches" "vacuum analyze pgbench_tellers"; do
  sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql -h /tmp -p 5433 -U dbcomm \
    -v ON_ERROR_STOP=1 postgres -c "$sql"
done
```

---

## Workloads

| workload | status | validates |
|---|---|---|
| **DPU TCP transport smoke** | ✅ | the **only** single-node host↔DPU DMA regression net |
| **`pgbench --homer`** (local) | ✅ | local control path, `CLIENT_SQL_SESSION`, backend spawn, local result sink |
| **`pgbench --homer`** (remote RDMA) | ✅ | + farnet0 startup, artifact sync, RDMA setup, peer control, remote command/result transport |
| **basebackup, 4-role DPU relay** | ✅ | the **only** validated basebackup topology |
| **backend-to-backend COPY** | 🔴 **BROKEN at HEAD** | hangs; unbisected since citus `7a2eaed53` |
| **`pgbench --homer-dpu`** | 🔴 **never completed e2e** | DPU result-tuple relay |

Broken command shapes — and why each *looks* plausible — are kept in
`docs/kb/operations/farnet_diagnostics_and_baselines.md` §4, deliberately **out** of this runbook so nobody
copy-pastes them.

### Performance measurement rules

- The **first** run after any restart, binary sync, geometry change, or peer-connection setup is a **warmup**.
  Never quote it.
- Quote **warmed steady state**: repeat without restarting PostgreSQL or either service, and report the
  **full repeat set**, not the best run.
- Prefer a workload lasting ≥ ~5 s, or aggregate several warmed repeats.
- **Keep verbose logging and stats macros OFF.** Logging on the measured path has caused misleading
  "regressions" in this prototype.
- **"No error" is not "the intended path ran."** Confirm from the service logs that Homer actually executed
  rather than a silent libpq/built-in fallback.

### DPU TCP transport smoke

Run it **before and after any DPU DMA, byte-ring, or bridge-ABI change.** It silently rotted through the
byte-ring pool migration — it compiled; nobody ran it.

⚠ **Launch the server and the client from the SAME shell invocation** (the server's `--timeout-ms` counts from
process start, not from accept), pass **every leg flag to BOTH ends** (the server checks them too), and
**always read the server log** — the client exits 0 on legs it wasn't told to expect
(hazards §7).

```sh
# DPU (server), under nohup setsid </dev/null & from a script on the DPU
./homer_dpu_tcp_transport_smoke --server --dev-pci 0000:03:00.0 --port 9727 \
  --timeout-ms 45000 --expect-loop2-backpressure

# farnet1 host (client) -- all six legs.
#   --export-posix-shm  exports a tmpfs MAP_SHARED region (the postmaster spawn region's shape)  [spike S0]
#   --fork-reader       forks a child that re-maps that object BY NAME at its own VA, touches no
#                       DOCA, and must observe the DPU's DMA write; gates the exit code          [spike S0b]
./build/homer/homer_dpu_tcp_transport_smoke --client --host 10.10.1.201 --dev-pci 0000:21:00.0 \
  --port 9727 --timeout-ms 40000 \
  --export-posix-shm --fork-reader \
  --expect-backend-command-publish --expect-response-publish \
  --expect-backend-completion-pull --expect-byte-ring-pull \
  --expect-dpu-to-host-payload --expect-loop2-backpressure
```

Both ends print `homer_dpu_tcp_transport_smoke: ok` on a real pass.

### `pgbench --homer`

Two modes, and they are **different validation tiers**:

- **farnet1-local** — maps the local service control shm, opens a local `CLIENT_SQL_SESSION`, drives a local
  socketless backend. A fast smoke and local scheduler-overhead check. It does **not** validate farnet0
  startup, artifact sync, RDMA setup, peer-control progress/retirement, or remote command/result transport.
- **farnet0 → farnet1 RDMA** — the real thing. **Any scheduler, peer-control, RDMA, or cross-node change needs
  at least the remote single-client smoke before correctness is claimed.** Use the remote multi-client run
  when the change can affect fairness, resource retirement, batching, or shared transport scaling.

CPU placement matters: frontend, service, and backend busy-poll shared cache lines. On farnet1, CPUs `2`,`3`,`4`
share one L3 domain (`0-5,48-53`); `8`,`9` are in another. For the best single-client local number use service
CPU `2`, backend CPU `4`, client CPU `3`. Use `--client-cpus=LIST` for multi-worker runs and **record the
placement with the result**; `--client-cpu=CPU` pins every worker to one CPU and suits only single-client
reproducibility.

```sh
# local single-client smoke (farnet1)
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -h /tmp -p 5433 -U dbcomm \
  --homer --homer-database-oid "$DBOID" --homer-user-oid "$USEROID" \
  --latency-percentiles --client-cpu=3 -n -M simple -c 1 -j 1 -t 1000 postgres

# remote RDMA single-client smoke (from farnet0)
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -h /tmp -p 5433 -U dbcomm \
  --homer --homer-database-oid '$DBOID' --homer-user-oid '$USEROID' \
  --homer-peer-host 10.10.1.101 --homer-peer-port 9717 --homer-peer-node 1 \
  --latency-percentiles --client-cpu=3 -n -M simple -c 1 -j 1 -t 20000 postgres"

# remote RDMA multi-client
ssh farnet0 "sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -h /tmp -p 5433 -U dbcomm \
  --homer --homer-database-oid '$DBOID' --homer-user-oid '$USEROID' \
  --homer-peer-host 10.10.1.101 --homer-peer-port 9717 --homer-peer-node 1 \
  --latency-percentiles --client-cpus=8,9,10,11 -n -M simple -c 4 -j 4 -t 10000 postgres"

# libpq baseline, same worker placement
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pgbench -h /tmp -p 5433 -U dbcomm \
  --latency-percentiles --client-cpus=8,9,10,11 -n -M simple -c 4 -j 4 -t 20000 postgres
```

Before a remote run: sync artifacts to farnet0, run the preflight on both hosts, start both services.

Current numbers and their caveats: `docs/kb/operations/farnet_diagnostics_and_baselines.md` §3.2–§3.3.

### Basebackup — the validated 4-role DPU relay (USE THIS)

The **only** validated topology. RDMA lands in farnet0 **DPU** memory and is relayed to a `--homer-receive`
consumer on the farnet0 **host**. Note the `host=` in the target selects the **receiver**, and choosing the
farnet0 *host service* (`10.10.1.100`) instead is **broken** above ~8 MiB (diagnostics §4.1).

| role | what runs there |
|---|---|
| farnet1 host | PostgreSQL (data source) + `pg_basebackup` **sender** |
| farnet1 DPU | sender-side Homer service |
| farnet0 DPU | receiver-side Homer service |
| farnet0 host | `pg_basebackup --homer-receive` **consumer** (no PostgreSQL needed) |

Peer RDMA is farnet1 DPU `10.10.1.201` → farnet0 DPU `10.10.1.200`. Geometry `slots=4,bytes=524288` — sender
and consumer **must match**. **Start the receivers first.** Use `-X none` (WAL streaming is not yet Homer).

```sh
# Sender (farnet1 host), tag N
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5433 -U dbcomm -X none -c fast \
  -t 'homer:mode=rdma,host=10.10.1.200,port=9717,node=2,slots=4,bytes=524288,tag=N' -v

# Consumer (farnet0 host), tag N -- frontend env points at its LOCAL farnet0 DPU
ssh farnet0 "sudo -n -u dbcomm sh -c 'env \
  HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
  HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
  /data/dbcomm/pg-citus/bin/pg_basebackup --homer-receive \
    --homer-node 2 --homer-database-oid \$DBOID --homer-user-oid \$USEROID \
    --homer-slots 4 --homer-bytes 524288 --homer-tag N'"
```

Local Homer blackhole smoke (producer/service baseline, no peer transport):

```sh
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/pg_basebackup \
  -h /tmp -p 5433 -U dbcomm -X none -c fast -t 'homer:mode=blackhole,node=1' -v
```

PostgreSQL's built-in `TARGET 'blackhole'` is **not** a Homer path. Warm the local and remote paths
**separately** before comparing them.

### Backend-to-backend COPY 🔴 BROKEN AT HEAD

> **This workload HANGS.** Both service logs go silent right after peer-transport setup, no `remote exec
> backend` is ever spawned on farnet0, and the COPY backend survives `pg_ctl stop -m fast`. Confirmed on
> binaries built after citus `7a2eaed53`. Leading suspect: `a3cdd5f3c` ("delete the pending-binding
> registry"), a session-pairing change validated only against basebackup. Tracked as "Problem 2" in
> `docs/kb/implementations/citus/transport/byte_ring_slot_capacity_regression.md`.
> **The June-6 baseline numbers are in the KB and must NOT be cited as evidence that COPY works today.**

Exercises the Citus coordinator backend on farnet1, both Homer services, and a socketless Citus worker
backend on farnet0 — the service-to-service tuple payload path. Needs PostgreSQL on **both** hosts.

```sh
# input (farnet1)
awk 'BEGIN { for (i = 1; i <= 10000000; i++) printf "%d,%d,payload_%08d\n", i, i * 10, i }' \
  > /tmp/homer_tuple_sink_copy_10m.csv

# distributed table -- verify the single placement lands on 10.10.1.100
sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
  -h /tmp -p 5433 -U dbcomm -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SET citus.shard_count=1;
DROP TABLE IF EXISTS homer_tuple_sink_copy_bench;
CREATE TABLE homer_tuple_sink_copy_bench (id int, v bigint, payload text);
SELECT create_distributed_table('homer_tuple_sink_copy_bench', 'id');
SELECT p.shardid, n.nodename, n.nodeport
FROM pg_dist_placement p JOIN pg_dist_node n ON p.groupid = n.groupid
WHERE p.shardid IN (SELECT shardid FROM pg_dist_shard
                    WHERE logicalrelid = 'homer_tuple_sink_copy_bench'::regclass)
ORDER BY p.shardid, n.nodename;
SQL

# the run -- warm once, then take repeats.
# NOTE the heredoc: `\copy` is a psql META-command and cannot be mixed with SQL in a
# single `-c`. Feed it on stdin, not as an argument.
/usr/bin/time -p sudo -n -u dbcomm /data/dbcomm/pg-citus/bin/psql \
  -h /tmp -p 5433 -U dbcomm -d postgres -v ON_ERROR_STOP=1 <<'SQL'
SET citus.enable_experimental_tuple_sink_routing=on;
SET citus.enable_experimental_tuple_sink_loopback_validation=off;
TRUNCATE homer_tuple_sink_copy_bench;
\copy homer_tuple_sink_copy_bench FROM '/tmp/homer_tuple_sink_copy_10m.csv' WITH (FORMAT csv);
SELECT count(*), min(id), max(id), sum(v) FROM homer_tuple_sink_copy_bench;
SQL
```

**Correctness anchors:** `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`.
**Intended-path proof:** `published_tail` advancing in the service logs — otherwise it silently fell back to
vanilla libpq COPY.

### Foreground pgbench + background basebackup ⚠ NEEDS RE-DERIVATION

The intended network-interference shape: transactions as the foreground workload, basebackup streams as
background traffic. The shape is: launch the basebackup in the background, `sleep 0.5`, run the pgbench in the
foreground, `wait` for the basebackup, then report **both** return codes and keep **both** logs.

> ⚠ **The two scripts that used to live here (c1 and c4) both drove basebackup at
> `host=10.10.1.100` — the farnet0 HOST service — which hangs above ~8 MiB** (diagnostics §4.1). They cannot
> run today and were deliberately not carried over.
>
> Re-deriving this against the **4-role DPU-relay** topology is **open work**: that topology needs a
> `--homer-receive` consumer on the farnet0 host, while the foreground pgbench *also* runs from farnet0. So
> the concurrent shape becomes five processes across four machines, and the CPU-set disjointness rule now has
> to cover the consumer too. Do not improvise it — design it, then record the validated command here.

Whatever it becomes: this is a **correctness** shape for the current milestone. Do not treat its throughput as
a solved scaling result until the command/control scaling issue is addressed.

---

## Regression sweep (before AND after any transport change)

Three regressions landed July 6–8, 2026 and went unnoticed for days because each validation **built one
target and ran one workload**. Cheap insurance:

1. **Build every target** — all seven smokes (recipe above), not just `service-bin`.
2. **Run all three workloads** — basebackup (4-role relay), `pgbench --homer`, backend-to-backend COPY. Two of
   the three are currently broken; fix or re-check them before trusting a "no regression" claim.
3. **Run at least one workload with `citus.enable_homer_dpu_frontend_agent=on`.** The agent puts a **permanent
   49-ring mmap import** into the local DPU service — a state no default-off run ever reaches, and one that
   **wedged the DPU setup listener for four days without a single log line.**

   > ⚠ **An import being accepted proves NOTHING about the next connection.** The liveness signal is
   > `ss -ltn | grep 9727` still showing `Recv-Q 0` **after** a workload has run. Cheapest end-to-end check:
   > with the agent on, run the 4-role basebackup — its sender opens a *second* DPU setup connection, so it
   > fails immediately if the listener is starved. (hazards §8.1)

4. **When you add a `HomerProgressSourceKind` / `...CollectorKind` / `...ActionKind` enum member, enumerate the
   switches BY HAND.** `-Wswitch` protects nothing — nearly every switch has a `default:` arm, so it compiles
   clean and silently falls into it. **A new collector needs FOUR edits**, and the fourth is not a switch: it
   must be **named** in `HomerMachineBaselineCompileExecutionPlan`'s phase body, or it is armed on every pass
   and granted on none — which looks exactly like an idle service. (hazards §5)

   ```sh
   grep -n 'switch ((HomerProgressSourceKind)\|switch (sourceKind)' tuple_sink_service_process.c
   grep -n 'FindCollector(candidateSet, HOMER_PROGRESS_COLLECTOR_' tuple_sink_service_process.c
   ```

---

## Post-run checklist

```sh
# 1. process state clean on both hosts (by /proc/exe -- see Non-negotiable #2)
# 2. results
grep -E 'transport:|tps|latency|p50|p95|p99|number of failed transactions' \
  /data/dbcomm/pg-citus/homer_runs/*/*.log 2>/dev/null || true
# 3. BOTH service logs -- the intended-path proof lives here, not in the exit code
tail -n 80 /data/dbcomm/pg-citus/data/homer_service_farnet1.log
ssh farnet0 "tail -n 80 /data/dbcomm/pg-citus/data/homer_service_farnet0.log"
```

Then ask the two questions that an exit code cannot answer:

- **Did the intended Homer path actually run**, or did it silently fall back?
- **Was the process set clean when the measurement started**, or is this number contaminated?

---

## Where the details went

| you want | read |
|---|---|
| why a rule exists; the trap it prevents | `docs/kb/operations/farnet_operational_hazards.md` |
| diagnostic builds, `starve-diag`, RDMA link battery, measurement history, broken command shapes | `docs/kb/operations/farnet_diagnostics_and_baselines.md` |
| the selected-DPU architecture of record | `docs/kb/implementations/citus/transport/selected_dpu_session_rings_and_lifecycle.md` |
| the open P0 resource-retirement work | `docs/kb/implementations/citus/transport/resource_retirement_contract_audit.md` |
| the command-plane migration plan (S0–S7) | `docs/kb/implementations/citus/transport/dpu_command_plane_migration_plan.md` |
| open regressions behind the broken workloads | `docs/kb/implementations/citus/transport/byte_ring_slot_capacity_regression.md` |
