# DPU command-plane migration — detailed plan

> **Naming.** A SEPARATE workstream from the byte-ring pool plan, not its "Stage 3". The pool plan owns
> data-plane *ring ownership*. This doc owns the *command plane*. Stages here are numbered
> independently — cite them as "command-plane S1a", etc.

**Status (July 9, 2026): IN PROGRESS. S0 ✅, S0b ✅, S1a ✅, S1b ✅. S5 turned out to be already working —
see "S5 IS ALREADY WORKING" below. The remaining critical path is S2 → S3 (arena + frontend agent;
spawn trigger). This is a PREREQUISITE for `pgbench --homer-dpu`, not a follow-on.**

> **Two decisions supersede the original plan. Read both before implementing S2/S3.**
> - **D2′ — the doorbell agent bgworker.** The doorbell socket *and* the DOCA export live in a
>   `shared_preload_libraries` background worker, **not** the postmaster. **Zero PostgreSQL core
>   changes**, because `postmaster_sigusr1_hook` already exists in our fork and is Tier 1. The original
>   D2 (postmaster owns the fd; new hook into `pm_wait_set`) is **retracted**.
> - **D4′ — the arena is named POSIX shm, addressed by offset.** Backends `shm_open` it; they need not
>   inherit it by `fork()`. Exporter (agent) and readers (postmaster, backends) are different processes
>   at different VAs aliasing the same physical pages. **That aliasing is the single load-bearing
>   assumption of the design — prove it in S0b before writing a line of S2.**

**Runbook for the S1a gate** (all four roles, farnet0 as the client node):
```sh
# farnet0 DPU (client's local DPU) and farnet1 DPU (peer), each as `ubuntu`:
setsid nohup env HOMER_SERVICE_ENABLE_DPU_DMA=1 HOMER_SERVICE_ENABLE_DOCA_DMA=1 \
  HOMER_SERVICE_DPU_SETUP_PORT=9727 HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0 \
  HOMER_SERVICE_PEER_BIND_HOST=10.10.1.200 HOMER_SERVICE_PEER_PORT=9717 \
  ~/dbcomm/citus-dbcomm/build/homer/citus_tuple_sink_service </dev/null > /tmp/svc.log 2>&1 &
#   (farnet1 DPU: PEER_BIND_HOST=10.10.1.201)

# farnet0 host: a host service must still run -- pgbench's HomerClientOpenControl maps its
# control SHM at thread start. It handles ZERO sessions. S7.1 removes this dependency.

# farnet0 host, the client:
env HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
    HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
  pgbench -h /tmp -p 5432 -U dbcomm --homer --homer-dpu-command \
    --homer-database-oid 5 --homer-user-oid 10 \
    --homer-peer-host 10.10.1.201 --homer-peer-port 9717 --homer-peer-node 1 \
    --client-cpu=3 -n -M simple -c 1 -j 1 -t 1 postgres
```
The peer host is the **peer DPU** (`10.10.1.201`), not the peer host service — same rule as the
validated 4-role basebackup topology. Each frontend points at its OWN local DPU.

---

## Intent (stated by the user)

> The client runs on a separate node from the PG server/backend. Both the client binary and the server
> Postgres binary use the **Homer frontend** to send and receive commands / completions / tuple results.
> No libpq. Two nodes talking to each other **through their own DPU**, which performs DMA to and from
> the host (Homer frontend).

The Homer *service* IS the DPU-native service. **Host Homer services are bring-up scaffold, not the
target** (plain `--homer`, the non-DPU path, keeps them).

## The wall, and why it is architectural

The DPU DMA engine's mirrored ranges come ONLY from `engine->hostMmapImports`, populated solely by
`HomerDpuDmaImportHostMmapDescriptorForSetup` (`homer_service_dpu_dma.h:245`) — i.e. by a **host
frontend exporting its ring to the engine over the DPU TCP setup socket**. The engine exists to import
HOST memory across PCIe. **A host-resident service has nothing to import.**

Today `pgbench` opens its command session via `HomerClientOpenSqlSession(HomerClientControl *)`
(`pgbench.c:9535` -> `homer_client.c:1518`), a mapped HOST-service control SHM. Only the SQL *result*
receive ring is selected-DPU, bound after the command open (`pgbench.c:9559`). Payload streams are
created by the service that owns the session, so the `--homer-dpu` result stream is born in the HOST
service, whose engine can never hold an import for it. Observed three times: farnet1 binds a
`purpose=0` MIRROR slot, then silence — `HomerDpuDmaMirroredByteRangeReadyForServiceSink` iterates an
empty import table, egress never fires, and the client hangs on its first `SELECT`.

**A Homer frontend is, definitionally, the host process that exports its rings to the DPU beside it.**
The host service was never a frontend. That is the whole bug.

## Trust tiers — "compiles" is not "works"

Classify every component before depending on it. (Fourth instance of this failure mode today, after the
`mode=rdma` runbook example, the June-6 COPY baseline, and the "cache is advisory" comment: *a written
artifact describing a path was trusted as evidence the path works*.)

**TIER 1 — EXERCISED.** Some validated workload runs it today; breaking it would be noticed.
- Client-library selected-DPU machinery in `homer_client.c` (`HomerClientOpenBaseBackupStreamSelectedDpu`,
  `...ReceiveStreamSelectedDpu`, `HomerClientOpenSqlResultReceiveStreamSelectedDpu`) — cross-node DPU
  basebackup moves ~23 GB through it.
- The DPU service's setup-TCP + `HomerDpuDmaImportHostMmapDescriptorForSetup` import path.
- The DPU<->DPU RDMA peer transport for payload, and the `CRITICAL_CONTROL` class its peer-opens ride.
- The backend-spawn region and its postmaster hook (every `--homer` run forks socketless backends
  through it).

**TIER 2 — EXISTS, UNEXERCISED.** Reachable only from a smoke or the deprecated UDF. Documentation of a
mechanism, not a component. `homer_frontend_dma.c`, `homer_frontend_dma_lifecycle.c`, the GUC-gated
sites in `homer_frontend_control.c`, `citus_remote_exec_pgbench_transaction`, **and the
`backendChannelMode = SELECTED_DPU_DMA` handling in `remote_execution_backend_bridge.c`**
(`:2572`, `:2625`, `:2637`, `:2646`, `:2655`, `:2669`).

An earlier revision of this doc said `HomerFrontendDmaOpenCommandSession` should be "kept and promoted".
**Retracted.** It compiles; nothing validated drives it; its GUC help text still claims the path is
not-implemented. Build on the Tier-1 client template instead.

---

## Target architecture — symmetric frontends

**Every descriptor role this architecture needs ALREADY EXISTS** (`homer_dpu_bridge_abi.h:78-95`) —
a strong sign the design was anticipated. Nothing new is needed in the bridge ABI:

| Role | Meaning | Direction |
|---|---|---|
| 1 | `FRONTEND_CONTROL_SLOT` | DPU pulls the client's control/command requests |
| 2 | `BACKEND_COMMAND_MAILBOX` | DPU writes commands into the backend |
| 3 | `BACKEND_COMPLETION_MAILBOX` | DPU reads completions from the backend |
| 5 | `SQL_RESULT_BYTE_RING` | host-produced; DPU pulls/mirrors the backend's result bytes |
| 6 | `FRONTEND_COMPLETION_EVENT` | DPU writes completions back into client memory |
| 7 | `PAYLOAD_BYTE_RING_DPU_TO_HOST` | DPU writes decoded tuples into the client's result ring |

| Process | Exports to its LOCAL DPU |
|---|---|
| `pgbench` (node A) | role 1 (control slot), role 6 (completion events), role 7 (result ring) |
| socketless PG backend (node B) | role 2 (command mailbox), role 3 (completion mailbox), role 5 (result byte-ring) |
| postmaster (node B) | the backend-spawn region (once, at startup) |

The two DPU services talk RDMA to each other. No host Homer service is in the `--homer-dpu` path.

> **CORRECTED on review.** An earlier draft said the client exports a "command ring" and a
> "completion ring". **There is no such role.** The client's commands are pulled from its role-1
> control slot; its completions are written back via role-6 completion events. Read the role enum,
> not my summary of it.

---

## Design decisions

> ## ⚠ INVARIANT — DMA BEFORE DOORBELL (correctness, not style)
>
> The spawn-slot fields travel over **PCIe DMA**. The doorbell travels over **TCP**. **Nothing orders
> two different channels for you.** If the doorbell overtakes the DMA, the postmaster scans a slot
> whose fields have not landed and forks a backend with garbage `dbOid`/`userOid`.
>
> **DPU side, in this exact order:**
> 1. DMA the request fields → **await DMA completion**
> 2. DMA `state = REQUEST_READY` (release) → **await DMA completion**
> 3. *Only then* send the doorbell frame.
>
> **Host side:** read `slot->state` with **acquire** semantics before reading any field. This is the
> same contract today's submitters already rely on (`tuple_sink_service_process.c:18833`/`:18845`
> write fields, then the state word, then signal).
>
> Assert both halves with debug checks in S3. This is the single easiest thing to get wrong in the
> whole plan, and it fails intermittently and unreproducibly.



### D1 — the spawned backend becomes a Homer frontend (INVERTS the Tier-2 arrangement)
Under today's (Tier-2) `SELECTED_DPU_DMA`, the **frontend** creates and exports the mailboxes
(`homer_frontend_dma_lifecycle.c:103`, shm created at `:137`/`:146`/`:155`; exported at
`homer_frontend_dma.c:1269`/`:1273`/`:1277`) and the **backend merely `shm_open`s them**
(`remote_execution_backend_bridge.c:2637`-`:2669`).

Cross-node that cannot work: the frontend is on node A, the backend on node B. So **invert it** — the
backend CREATES its mailboxes and its result byte-ring, and EXPORTS them to its own local DPU. Since
the Tier-2 code is unexercised, rewriting costs little and buys the symmetry above.

*Rejected:* have node A's client create node B's mailboxes. Impossible — POSIX shm is node-local, and
the backend must `shm_open` by name on its own host.

### D2 — spawn trigger: postmaster exports the region; DPU DMA-writes the slot; doorbell over the existing setup socket
**The postmaster OWNS the spawn region.** It creates it: `shm_open(O_CREAT|O_RDWR)`
(`remote_execution_backend_bridge.c:1709`), `ftruncate` (`:1730`), `mmap` (`:1740`), initialises
`slotCount`/`postmasterPid` (`:1758`). Region name `/citus_remote_exec_backend_spawn_v15` (bumped from
`_v14` in S3.1, when the request grew `arenaSlotIndex`; the log quotes further down predate the bump)
(`remote_execution_backend_protocol.h:28`), fixed slot count (`:30`), slot states (`:47`).
**So there is no "DOCA-export a region you do not own" problem** — an earlier note claimed there was;
corrected. The postmaster calls a Homer frontend API and exports its own region.

**The poller is a SIGUSR1 hook, not a poll loop.** Installed at `:3111`; entered at `:3081`; scans
slots at `:3083`; waits on `slot->state == CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_REQUEST_READY` at
`:3091`; forks via `ProcessSpawnRequestSlot` at `:3097`. Today's submitters write the fields, store
`REQUEST_READY`, then `kill(postmasterPid, SIGUSR1)` (`tuple_sink_service_process.c:18833`, `:18845`,
`:18924`, `:18927`).

**A DPU cannot signal a host process.** But the postmaster already blocks in a `select()`-style main
loop, and it is already the TCP *client* of its local DPU's setup listener. So the DPU can wake it by
making a watched fd readable.

#### The doorbell needs a PROTOCOL, not "a byte"

The setup socket already speaks a framed, typed protocol: `HomerDpuComchSetupHeader`
(`homer_dpu_comch_abi.h:62`) = `{protocolVersion, messageKind, headerBytes, totalBytes, ...}` with
`messageKind` in `{SETUP=1, SETUP_ACK=2, CLOSE=3, CLOSE_ACK=4}` (`:33-36`). Two problems with writing a
raw byte onto it:

1. **Framing.** TCP is a byte stream. An unframed byte injected between framed messages is a corruption
   bug, not a notification.
2. **Direction.** The protocol is strictly host-initiated request/response — the host sends `SETUP` /
   `CLOSE`, the DPU only ever *replies* (`SETUP_ACK` / `CLOSE_ACK`). It never initiates. An unsolicited
   doorbell could arrive while the host is blocked waiting for a `CLOSE_ACK`, and the host reader would
   have to demultiplex an interleaved request/response + notification stream.

> ## ⚠ SUPERSEDED — see "D2′ — the doorbell agent bgworker" below
>
> Everything in this D2 section about the **postmaster** owning the doorbell socket and about adding a
> hook to `pm_wait_set` is **retracted**. The framing and the protocol survive; the process that holds
> the socket does not. Read D2′ before implementing.

**Chosen: a DEDICATED doorbell connection on its OWN LISTENER (Option B).**

> **CORRECTED on review.** An earlier draft said "a second connection to the same setup listener".
> **That would deadlock the DPU.** The setup server is a SERIAL state machine with a single
> `clientFd` (`homer_service_dpu_setup_tcp.c:48`, accept guarded by `if (server->clientFd < 0)` at
> `:223`): it accepts one client, reads, acks, closes, then accepts the next. Setup connections are
> **transient** — which is exactly why two concurrent basebackup senders work: each connects,
> exports, disconnects, and the *import* outlives the connection. A PERSISTENT doorbell connection
> would occupy `clientFd` forever and starve every subsequent setup accept.

- The DPU service opens a **separate doorbell listener** (its own port, default `9728`). The
  postmaster connects to it and sends `HOMER_DPU_COMCH_MESSAGE_DOORBELL_ATTACH` carrying
  `bridgeGeneration` + `clientInstanceId` + a `doorbellClass` (`SPAWN`). The DPU replies
  `DOORBELL_ATTACH_ACK`. Bump `HOMER_DPU_COMCH_PROTOCOL_VERSION`.
- Thereafter the connection carries **only** `HOMER_DPU_COMCH_MESSAGE_SPAWN_DOORBELL` frames,
  DPU -> host, unsolicited, unacknowledged: a fixed 16-byte header, no payload.
- **The receiver never parses.** On readable, the postmaster `recv()`s into a scratch buffer until
  `EAGAIN`, discards the bytes, and rescans ALL spawn slots. Rescan is idempotent, so coalescing N
  doorbells into one wake is correct by construction, and a framing bug cannot mis-drive the
  postmaster — it cannot mis-parse what it does not parse. The frame is defined on the wire for
  tooling and future extension, not for this receiver.
- **Connection loss** => close, rescan unconditionally, reconnect with bounded backoff. A lost doorbell
  is only possible if the connection dropped, and reconnect covers it.

*Rejected (Option A): a new `messageKind` on the EXISTING setup socket.* It forces the host reader to
demultiplex notifications interleaved with request/response traffic, puts a protocol parser inside the
postmaster, and — decisively — a persistent connection starves the serial setup server.
*Rejected (Option C): a bgworker or agent that owns the socket and raises SIGUSR1.* An extra process
and a poll loop to solve what an already-watched fd solves.

---

## D2′ — the doorbell agent bgworker (SUPERSEDES D2's process placement)

**Decided July 9, 2026, after scoping the postmaster seam. Zero PostgreSQL core changes.**

### Why D2 was wrong

D2 put the doorbell socket in the postmaster and had it wake on the fd. That needs **four** touch points
in `postmaster.c`, all new core surface:
1. `ConfigurePostmasterWaitSet` (`:1605`) sizes the set exactly:
   `CreateWaitEventSet(NULL, accept_connections ? (1 + NumListenSockets) : 1)`.
2. …so a hook is needed there to add extra events.
3. `WaitEvent events[MAXLISTEN]` (`:1632`) must grow.
4. `ServerLoop` handles only `WL_LATCH_SET` and `WL_SOCKET_ACCEPT` (`:1673`) — a new dispatch arm is
   needed for a readable, non-accept fd.

**But `postmaster_sigusr1_hook` ALREADY EXISTS in our fork** (`src/include/postmaster/postmaster.h:18`,
`src/backend/postmaster/postmaster.c:180`), invoked at the end of `process_pm_pmsignal()` (`:3977`),
which `ServerLoop` runs when the signal handler sets `pending_pm_pmsignal`. **It is TIER 1** — every
`--homer` run spawns socketless backends through it, driven by an *arbitrary non-child process* doing
`kill(postmasterPid, SIGUSR1)` (`tuple_sink_service_process.c:18927`).

So a process that owns the doorbell socket and raises `SIGUSR1` needs **no core change at all**.

*The original rejection of this ("Option C: an extra process and a poll loop") was wrong twice:* it is a
**blocking `recv()`**, not a poll loop; and the "already-watched fd" alternative costs a core patch that
upstream would never take and we would carry forever.

### The design

A `shared_preload_libraries` **background worker** — the **Homer frontend agent** — owns *both* the DOCA
export and the doorbell socket. The postmaster does **no DOCA and holds no socket**.

| Process | Does | Does NOT |
|---|---|---|
| **postmaster** | creates + `mmap`s the arena and spawn region (POSIX shm, as `EnsureSpawnRegionMapped` already does); runs its existing SIGUSR1 hook → existing slot scan | touch DOCA; hold any socket; gain any new core hook |
| **frontend agent** (bgworker) | `shm_open`s both regions, DOCA-exports them via the **S1b** API, sends the setup message, holds the doorbell connection, `recv()`s, `kill(PostmasterPid, SIGUSR1)` | fork backends; parse doorbell frames |
| **socketless backend** | `shm_open`s the arena, binds a mailbox slot | touch DOCA; open a setup connection |

Registration mirrors the existing citus daemon: `InitializeCitusBackgroundWorker` +
`RegisterBackgroundWorker` from `_PG_init` (`shared_library_init.c:512`, right beside
`CitusInstallRemoteExecutionBackendHooks()` at `:525`). Flags `BGWORKER_SHMEM_ACCESS`, **no database
connection**, `bgw_start_time = BgWorkerStart_PostmasterStart`, `bgw_restart_time = 5s`.

### Three properties this buys, beyond "no core patch"

1. **DOCA never runs in the postmaster.** The postmaster forks every backend; inheriting a DOCA device's
   fds and internal state (possibly threads) across `fork()` is exactly the kind of thing that fails
   rarely and mysteriously. The agent is a leaf process. *This is now a positive design property, not a
   hazard we tolerate.*
2. **Open question 6 dissolves.** "The postmaster's doorbell fd must be closed in every forked child" —
   there is no postmaster fd. Backends are siblings of the agent and inherit nothing from it.
3. **The doorbell connection is a DIAGNOSTIC liveness beacon** (downgraded — see below). Because the
   agent holds *both* the export and the persistent doorbell socket, **doorbell EOF ⇒ that exporter is
   gone.** Useful for attributing the failure; **not** a safety mechanism.

> **⚠ CORRECTED (July 9, 2026).** An earlier revision called this a *hard requirement*, on the grounds
> that an agent crash "unpins its pages while the DPU may still be DMA-ing into them — a
> memory-corruption class bug." **That was overstated, twice:**
> - **The postmaster keeps the arena mapped**, so its pages cannot be freed and reused. A stale DMA
>   would land in the arena's *own* pages, not in unrelated memory.
> - With the IOMMU on, a torn-down registration makes the DPU's write **fault**, not land silently.
>
> The real consequence of an agent crash is a **fatal engine error on the DPU service** — and that is
> the behaviour we want. **Decided with the user: the agent must not crash; if it does, that is a bug,
> and failing loudly and fatally is correct.** No graceful re-export, no recovery path.
>
> **Consequently RETRACTED:** an earlier draft proposed reclassifying `DOCA_ERROR_IO_FAILED` on
> command/response/completion tasks from fatal to "export lost, recover". That would weaken a
> *deliberate* guard. `HomerDpuDmaRetireStaleGroupedControlImport`
> (`homer_service_dpu_dma.c:8342`) says so explicitly: *"Treat only that discovery-read failure as
> stale import removal; command, response, and backend-completion task failures remain fatal
> correctness bugs."* Leave it fatal.

### The sequence

1. **Postmaster** (`_PG_init`, `!IsUnderPostmaster`): create + `mmap` the spawn region (unchanged) and
   the new arena. Register the agent bgworker.
2. **Agent**: `shm_open` + `mmap` both regions; `HomerDpuFrontendExportRegion` each (S1b); build one setup
   message carrying **two** mmap exports; `HomerDpuFrontendSendSetup`; then connect the doorbell listener
   and send `DOORBELL_ATTACH`.
3. **DPU**: DMA the spawn slot (fields → await; then `state = REQUEST_READY` release → await), **then**
   send one `SPAWN_DOORBELL`. *(The ⚠ DMA-BEFORE-DOORBELL invariant at the top is unchanged and still
   the easiest thing here to get wrong.)*
4. **Agent**: `recv()` returns; drain to `EAGAIN` **without parsing**; `kill(PostmasterPid, SIGUSR1)`.
5. **Postmaster**: existing hook, existing slot scan, existing `ProcessSpawnRequestSlot`. Rescan is
   idempotent, so coalescing N doorbells into one wake is correct by construction.
6. **Agent dies** → socket closes → DPU sees EOF → invalidates the import. Postmaster restarts the agent
   → fresh export, fresh `bridgeGeneration`.

**Cost:** one signal hop (µs) on a path whose next step is a `fork()` (hundreds of µs). Irrelevant.

**Hygiene.** The agent is an ordinary backend-like process; a failed `recv()` degrades to close / rescan
/ reconnect with bounded backoff. Keep the receiver **parse-free**: it cannot mis-drive the postmaster
with input it never parses.

---

## D4′ — the arena is POSIX shm, addressed by offset (REFINES D4)

D4 said backends inherit the arena "by `fork()` after the postmaster `mmap`s it". **Not required, and it
was the wrong reason.** The arena is a **named POSIX shm object**, exactly like the spawn region, so any
process maps it by name — postmaster, agent, and backends alike. What D4 actually buys survives intact:
**one DOCA context per node, one export, and backends that do no DOCA.**

The load-bearing consequence is that **the exporter and the readers are different processes at different
virtual addresses.** Bridge descriptors already address as `hostRingAddress` (the exporter's base VA) +
`hostRingOffset`, so the DPU writes at the *agent's* VA and the postmaster/backends observe it through
their own mappings of the same physical pages. **That aliasing is the single load-bearing assumption of
this whole design — see S0b.**

### Sizing (measured July 9, 2026)

| Component | bytes |
|---|---|
| `CitusRemoteExecBackendSpawnRegion` (32 slots) | 17,424 |
| `CitusRemoteExecLocalCommandMailbox` (role 2) | **3,202,584** (3.05 MiB) |
| `CitusRemoteExecLocalCompletionMailbox` (role 3) | 270,552 (264 KiB) |
| SQL result byte ring (role 5, 256 × 1024) | 262,144 (256 KiB) |
| **per backend slot** | **≈ 3.56 MiB** |

A **single DOCA-registered region has a ~100 MB ceiling** (recorded in
`dpu_byte_ring_pool_per_session_plan.md:175` — raising the landing region to 100 MB crashed DOCA). So:

| N backend slots | arena size | verdict |
|---|---|---|
| 8 | ≈ 28.5 MiB | fine |
| **16** | **≈ 57 MiB** | **chosen** — ample headroom, ≫ pgbench `-c 4` |
| 32 | ≈ 114 MiB | **over the ceiling** — would need splitting |

**Decision: N = 16.** If it must grow, split the arena across several DOCA mmaps — **the setup ABI already
supports this**: `HomerDpuComchSetupHeader.mmapExportCount` (`homer_dpu_comch_abi.h:75`),
`HomerDpuComchSetupMessageBytesForExports(ringCount, mmapExportCount, ...)` (`:177`), and each descriptor
names its mapping through `mmapExportId` (`:99`). `HomerDpuComchSetupMessageBytes` is just the
`count == 1` convenience wrapper. **This is also why the agent can export the spawn region and the arena
as two mmaps in one setup message.**

> Note the 3.05 MiB command mailbox is the same bloat as the 66 KiB control slot: 64 slots of a
> fixed-size record. Shrinking the ABI unions (S4 perf item, fix 3) shrinks the arena too.

---

## S0b — spike: exporter ≠ reader ✅ **PASSED (July 9, 2026, citus `37cc74b06`)**

**The question.** S0 proved DOCA can export a tmpfs `MAP_SHARED` mapping and DMA into it — but its
exporter and its reader were the **same process**. D2′/D4′ need them to differ: process **A**
DOCA-exports its mapping of a POSIX shm object, the DPU DMA-writes into it, and process **B** — which
mapped the same object by name at a different VA and never touched DOCA — observes the write.

Standard `MAP_SHARED` aliasing says this works. **This project's discipline is not to trust "says."**
It was the single load-bearing assumption of D2′/D4′: had it failed, the whole doorbell-agent design
collapses back to a postmaster-owned export plus the four-point core patch.

**Result.**
```
FORK-READER: exporter_va=0x7f3794bc6000 reader_va=0x7f3794865000 distinct_va=YES
FORK-READER: observed the DPU's DMA write through its OWN mapping state=4 command_seq=7001
             -- exporter != reader CONFIRMED
homer_dpu_tcp_transport_smoke: ok    (rc=0, all six legs, no /dev/shm leak)
```
A process holding **no DOCA handle**, mapping the object at a **different virtual address**, observed
the DPU's PCIe DMA write. The DMA lands in the **page-cache pages of the shm object**; the exporter's
host VA is only an offset key. **D2′/D4′ stand.**

**How to run it:** `--export-posix-shm --fork-reader` on the TCP transport smoke. The child `munmap`s
the mapping it inherited, reserves the vacated range with `MAP_FIXED|PROT_NONE` so the next `mmap`
cannot reuse the exporter's address, `shm_open`s the object by name, maps it at its own VA, and
verifies the DPU's response publication. **The child's exit status gates the smoke's exit code.**

**Two bugs in the first cut of this spike, both instructive:**
- The child got the **same** virtual address — the kernel handed back the hole the `munmap` had just
  left. It "passed" while proving nothing about VA independence. Hence the `MAP_FIXED` placeholder, and
  landing on the exporter's VA is now **INCONCLUSIVE, not a pass**.
- The child's verdict line **never printed**: `_exit()` does not flush stdio. *A spike whose evidence
  can silently vanish is exactly the failure mode this smoke already had once.* The child now flushes
  explicitly; the parent `fflush(NULL)`s before forking so the child cannot duplicate its buffer.

**Caveat.** The spike's reader is a *descendant* of the exporter; in D2′ the reader (postmaster) is the
*parent* of the exporter (agent). Aliasing is symmetric, and the child re-opens the object **by name**
rather than relying on inheritance, so provenance is clean either way. The child also `closefrom(3)`s
before mapping, so it holds **no inherited DOCA descriptor** — matching the postmaster, which never has
one. (`fork()` copies the descriptor *table*; the parent's registration is untouched.)

### The mechanism, from the DOCA 3.2.0118 headers

Read alongside the empirical result. **Documentation alone would NOT have sufficed** — it explains *why*
it works but never promises it.

- `doca_mmap_set_memrange` takes "the start address of the memory range." Its documented restrictions
  concern object state, one-time configuration, and `addr + len` overflow — **nothing about anonymous vs
  file-backed vs tmpfs vs hugepages** (`doca_mmap.h:404-427`).
- `doca_mmap_start()` can fail because "the kernel failed to pin the requested amount of memory"
  (`:124-152`) — so registration **pins pages**.
- The export blob is **opaque**: DOCA calls it a serialized mmap representation and never says whether it
  carries virtual, physical, or IOVA addresses (`:252-261`).
- The DPU-side import is "not backed by local memory" (`:354-398`), and
  `doca_buf_inventory_buf_get_by_addr()` forwards `(addr, len)` into a lookup against the imported range,
  rejecting an address with "no suitable memory range" (`doca_buf_inventory.h:138-204`).
- **No PASID / ATS / SVA / per-process IOMMU domain / bounce buffer** appears anywhere in this path. The
  exposed model is a device *protection domain* (`doca_dev.h:160-181`) plus a device-associated mmap
  memory key (`doca_mmap.h:567-584`) — conventional pinned registered memory.
- NVIDIA's own `dma_copy` sample transports `host_addr` **separately** from the export blob and hands it
  to `buf_get_by_addr` on the DPU (`applications/dma_copy/dma_copy_core.c:709-729`, `:1194-1215`); and
  `file_compression` registers a `MAP_SHARED` **file** mapping (`file_compression_core.c:536-560`).

**Our own ABI already said this**: `hostRingAddress` is *"only an address token for DOCA buffer
construction against the imported mmap"* (`homer_dpu_bridge_abi.h:190-194`).

**Graceful teardown is documented**: `doca_mmap_stop()` invalidates every mmap created from its export
(`doca_mmap.h:154-160`) and `doca_mmap_destroy()` implicitly stops (`:104-122`). **Ungraceful exporter
death is not documented at all** — see the R3 decision in D2′: the agent must not crash, and a fatal DPU
engine error is the correct outcome if it does.

**What DOCA still does not promise:** that `doca_mmap_start()` pins the *tmpfs page-cache* pages rather
than some process-affine representation, nor anything about cross-process cache visibility. That is
precisely the gap S0b closed.

### Prior art: the Tier-2 path already implements exporter ≠ reader

Found while investigating. The GUC-gated, unexercised `SELECTED_DPU_DMA` path is **already exactly this
shape**:
- process A creates named shm objects and maps them `MAP_SHARED`
  (`homer_frontend_dma_lifecycle.c:475-501`) and DOCA-exports those mailbox mappings
  (`homer_frontend_dma.c:1267-1279`);
- process B independently `shm_open`s the same names and maps them `MAP_SHARED`
  (`remote_execution_backend_bridge.c:2610-2674`);
- B polls the **DPU-written publication epoch** through its own mapping (`:423-476`).

So a third piece of the architecture — after the descriptor roles and the transport-agnostic control
dispatcher — turns out to have been anticipated. But it is **Tier 2**: source expressing an assumption is
not evidence the hardware honours it, which is exactly why S0b was worth thirty minutes.

> **Read it before deleting it.** S2.5 retires that path. It is currently the *only* implementation of
> the exporter ≠ reader shape we are about to build — mine it for patterns first.

---

### D4 — the postmaster exports a per-node ARENA; backends inherit it by `fork()`

Setup connections are **transient and serialised** (see D2), so a per-backend export *would* work
mechanically: connect, export, disconnect; the import outlives the connection. But every socketless
backend would then need its own DOCA device context, opened on the session-open path, and every
concurrent session would consume one of the engine's `hostMmapImportCapacity` imports.

**Better, and symmetric with the DPU byte-ring pool we already built:** the postmaster creates ONE
arena at startup — the spawn region plus `N` preallocated sets of {command mailbox (role 2),
completion mailbox (role 3), result byte-ring (role 5)} — DOCA-exports it once, and declares all the
descriptors up front. Backends are `fork()`ed from the postmaster **after** the arena is `mmap`ed, so
they inherit the mapping for free and simply **bind a slot index**. Fatal on exhaustion, exactly as
`HomerDpuByteRingBind` does.

- One DOCA context per node, not per backend. One export. One import. Zero per-backend setup cost.
- **Dissolves open question 2** (does the backend need DOCA at startup? — no) **and open question 3**
  (who owns the result byte-ring? — the arena; the backend binds a slot).
- Session-open latency becomes just the fork.

*Cost:* descriptors must be preallocated for a maximum concurrent-session count, so `ringCount` and
`hostMmapImportCapacity` need sizing (`homer_dpu_bridge_abi.h:119`, `homer_service_dpu_dma.h:57`).
Same trade the byte-ring pool already makes, and the same fatal-on-exhaustion policy.

*Alternative (rejected for now):* per-backend export. Simpler conceptually, and it is closer to
"the backend is literally a Homer frontend", but it pays DOCA init per session and burns an import per
session. Revisit only if the arena's preallocation proves awkward.

> **D4 re-examined (July 9, 2026) — it still stands, but for a DIFFERENT reason than it was decided
> on.** D4 rested on two costs of per-backend export: (a) a DOCA context opened on the session-open
> path, and (b) one `hostMmapImportCapacity` import burned per session. **(b) has evaporated:**
> `hostMmapImportCapacity` defaults to **1024** (`homer_service_dpu_dma.c:785`), which is far above any
> concurrency we will reach. Had that been the only argument, per-backend export would now win.
>
> But a **stronger** argument appeared once S3's shape was settled: under D2 the postmaster **must**
> DOCA-export the spawn region anyway, and it **must** `mmap` it before any fork. Given it is already
> holding a DOCA device and an export, folding the per-session {role-2, role-3, role-5} descriptor sets
> into that same arena is nearly free — and it lets the socketless backend do **no DOCA at all**, which
> is what actually keeps the session-open path short. One DOCA context per node, not per backend.
>
> Recorded because a decision whose stated rationale has quietly become false is a trap for the next
> reader — and because it means the *cheap* thing to reconsider, if the arena's preallocation turns
> awkward, is (b), not the whole design.

### D3 — the client opener is built on the Tier-1 basebackup template
`HomerClientOpenSqlSessionSelectedDpu` in `homer_client.c`, cloned from the exercised selected-DPU
setup: env/default parsing (`:2551`), buffer layout with host publish lines / DPU credit lines / control
slot (`:2560`), aligned export buffer (`:2647`), bridge header (`:2670`), DOCA mmap export (`:2690`,
via `:2011`/`:2028`/`:2044`), descriptors (`:2716`, role-1 frontend control at `:2724`, `:2736`), setup
TCP (`:2145`, `:2782`), control-slot submit (`HomerClientDpuSubmitControlRequest`, `:2233`).

Add the SQL command-session request fields currently built only for host SHM:
`CITUS_REMOTE_EXEC_CONTROL_OP_COMMAND_SESSION` (`:1558`), `clientSqlResultDpuRelay` (`:1560`),
`sessionUID` (`:1567`), db/user/opKind (`:1573`-`:1575`), peer endpoint (`:1584`).

**`backendCpu` gap:** it is not part of `HomerClientOpenSqlSession` today; it appears only in spawn
requests (`remote_execution_backend_protocol.h:112`). The DPU-native open must either carry it and have
the DPU forward it into the spawn request, or fall back to service policy. **Decide in S1a.**

---

## Stages

### S0 — Spike ✅ **DONE (July 9, 2026, citus `4e91aaa63`+)**

**Question.** Every validated DOCA export in this tree exports **anonymous `posix_memalign` heap**
(`homer_client.c:2028`, `homer_dpu_tcp_transport_smoke.c:462`). The postmaster's spawn region is
`shm_open` + `mmap(MAP_SHARED, fd)` — a **tmpfs file-backed** mapping
(`remote_execution_backend_bridge.c:1709`-`:1745`). D2 needs the DPU to DMA into *that*. So the spike is
NOT "can a process DOCA-export memory" (proven); it is **"can DOCA export *this kind* of memory."**
Worth minutes because DOCA has surprised us before (the undocumented ~100 MB single-region ceiling).

**Method.** Rather than a standalone program, added `--export-posix-shm` to the existing TCP transport
smoke (`homer_dpu_tcp_transport_smoke.c`): the client's export buffer becomes `shm_open(O_CREAT|O_EXCL)`
+ `ftruncate` + `mmap(MAP_SHARED)` (unlinked right after mmap) instead of `posix_memalign`. Zero DPU-side
change; reuses the whole harness and its existing DPU→host publication assertions as the proof.

**Result: PASS — both directions, on tmpfs.**
```
client posix-shm export: name=/homer_tcp_smoke_export_3872836 bytes=3542200 addr=0x7f535c176000
client received TCP setup ack generation=1 rings=6 imported_bytes=283
client observed DMA backend command publication published_epoch=7001   <- DPU DMA-WROTE into tmpfs
client observed DMA response publication state=4 command_seq=7001      <- second DPU->host write
```
- **DPU reads from tmpfs**: grouped-control reads, the role-1 command pull, and two byte-ring pulls all
  completed (the run reaches the *later* write leg, so everything before it succeeded).
- **DPU writes into tmpfs**: the two publications above, observed by the host.
- **Control:** the server then failed at *byte-identical* the same point as the anonymous-heap run
  (see "smoke defects" below), so the shm switch is not implicated in that failure.

**Conclusion.** D2's export path is sound. `doca_mmap_set_memrange` + `doca_mmap_export_pci` pin and
export a tmpfs `MAP_SHARED` mapping exactly as they do anonymous heap. The postmaster can export the
spawn region it created. No `-lrt` needed (glibc ≥ 2.34 folds `shm_open` into libc).

#### S0 side-finding: the TCP transport smoke was itself broken (Tier 1.5 → repaired)

Running it — for the first time since it was fixed to *compile* — exposed that it had silently rotted
through the byte-ring pool migration. **A smoke nobody runs is Tier 2, not a test.**

1. **`HomerDpuDmaAcceptByteRingPull` rejected every smoke pull.** `HomerDpuDmaSubmitByteRingSmokePull`
   (`homer_service_dpu_dma.c:2529`) receives the mirror base/bytes/mmap as *parameters* but never
   populated `ringRuntime->dpuMirrorSlotResolved` — the cache that acceptance (`:9850`) demands and that
   egress (`HomerDpuDmaFillMirroredByteRange`, `:4470`) later reads as authoritative. The **production**
   caller `HomerDpuDmaSubmitMirroredByteRingPulls` (`:3553`) happened to resolve first at `:3670`
   (it needs `dpuRingBytes` for its own wrap clamp); the **smoke-only** driver bound its MIRROR slot
   directly and passed the handle in, so the cache stayed empty.
   → **Not a production bug** (basebackup goes through the production scheduler). A *smoke-fidelity* bug.
   **Fixed:** the submitter now resolves the slot itself — one resolution point, precondition true by
   construction — and cross-checks the caller-supplied window against the pool-resolved one, turning a
   silent mirror-aliasing bug into a loud submit-time rejection.
2. **Six failure causes shared one message.** The acceptance check was a 6-term `||` behind
   `"byte-ring pull owner does not match imported ring"`. Each cause has a different fix; the message
   named none of them. **Split into six named failures.** This is what made (1) diagnosable at all.
3. **STILL BROKEN (data plane, deferred):** the smoke's DPU→host write leg calls the retired
   `HomerDpuDmaGetLandingRegionMemory` engine singleton (`:3976`) and passes `dpuRingBase=NULL` to
   `HomerDpuDmaSubmitByteRingWrite` (`homer_dpu_tcp_transport_smoke.c:1741`), so it fails
   `"byte-ring write ring exceeds bound landing slot source"` (`:3195`). It must bind a `LANDING` pool
   slot per role-7 ref. Tracked as a follow-up — **not** on the command-plane critical path (the command/
   completion legs pass, and production basebackup exercises the write path).

**Trust reclassification.** The host↔DPU **command/completion DMA loop is now TIER 1** — genuinely
exercised, both directions, including into tmpfs. This materially de-risks S4: its command plane is
proven; what S4 adds is the real backend and the real client, not new DMA mechanics.

### S1a — client-side `HomerClientOpenSqlSessionSelectedDpu` (clone the Tier-1 template)

> ## ✅ PLAN CORRECTION (July 9, 2026) — S1a is CLIENT-SIDE ONLY
>
> S1a.2/.3/.4 were written against **drifted line numbers** and a false premise: that the DPU
> control-slot path and the host-SHM control path are **two dispatchers**. They are **one**.
> Verified in code (see below). The service already admits an `OP_COMMAND_SESSION` open arriving
> over the role-1 DPU control slot and routes it into the async command-open state machine.
> **Nothing to relax. S1a is the client opener plus pgbench wiring.**
>
> This is the second time reasoning from a remembered line number rather than the code produced a
> wrong plan (cf. the "divisor's third job is tiling" retraction). Re-derive before planning.

**Evidence for the correction:**
- `TupleSinkServiceDispatchLocalControlSlot` (`tuple_sink_service_process.c:37840`) switches **only on
  `requestKind`** — no op-kind allow-list, no transport check. Both callers use it:
  the host-SHM slot pump at `:38058` and the DPU staged-command executor at `:39654`.
- The DPU staged executor builds a `DPU_STAGED_COMMAND` response owner (`:39653`) and
  `...ResponseOwnerCanRegisterAsync` accepts it (`:36240`-ish), so async continuations publish back
  through the Stage-8 pending-response DMA queue.
- `TupleSinkServiceHandleOpenSession` routes `opKind == OP_COMMAND_SESSION` straight to
  `TupleSinkServiceHandleOpenCommandSession` (`:33888`), transport-agnostic.
- `TupleSinkServiceOpenRequestNeedsAsyncLocalControl` (**now `:34904`**, not `:34817`) already returns
  **true** for `OP_COMMAND_SESSION` + `sessionKey.opKind == OP_CLIENT_SQL_SESSION` +
  `destinationNodeId > 0` (`:34918`). It is a *routing* predicate, never a rejection.
- The DMA layer accepts `OPEN_SESSION`/`CLOSE_SESSION`/`REPORT_POST_COMMAND_STATE`/`START_COMMAND`/
  `POLL_COMMAND_COMPLETION` request kinds off the control slot (`homer_service_dpu_dma.c:9825`).

**Sub-step dispositions:**
- **S1a.1** — **THE WORK.** New entry point in `src/bin/homer_client.c`. See "S1a.1 decisions" below.
- **S1a.2** — ~~admit `OP_CLIENT_SQL_SESSION` over the DPU control slot~~ **NO-OP, already admitted.**
- **S1a.3** — ~~`REGISTER_MEMORY` requires `commandMailbox.mailbox`~~ **NO-OP.**
  `TupleSinkServiceCreateSession` calls `TupleSinkServiceEnsureSessionMailboxes` **unconditionally**
  (`:22116`, fatal on failure) and `...EnsureClientCompletionMailbox` for `OP_CLIENT_SQL_SESSION`
  (`:22127`). The mailbox is never NULL for a live session, so the `:35337` check passes by
  construction. (What it is *used for* matters — see the S4 note below.)
- **S1a.4** — **RESOLVED, open question 1 closed.** `backendCpu` is **service-chosen**, not carried by
  the client: `reservedSlot->request.backendCpu = TupleSinkServiceChooseBackendCpu()`
  (`:18907`; chooser at `:871`). It is absent from `CitusRemoteExecOpenSessionRequest`
  (`homer_control_abi.h:297`) and lives only in the spawn struct
  (`remote_execution_backend_protocol.h:112`). **Keep service policy. Do not add a client field.**
- **S1a.5** — pgbench: select the new opener under `--homer-dpu`.

#### S1a.1 decisions (made within the plan's direction; recorded per the "note your reasoning" rule)

1. **Clone, do not extract.** `HomerClientDpuSubmitControlRequest` (`homer_client.c:2233`) and its
   siblings are typed on `HomerClientBaseBackupStream *` but touch only a small control-channel
   substruct. Extracting now would edit the Tier-1 basebackup path, our only regression net. **S1b
   exists precisely to extract.** Duplication is temporary and deliberate.
2. **Reuse `HomerClientBaseBackupStream` as the selected-DPU export carrier.** The name is a misnomer
   — it is really "an exported host region + a role-1 control slot" — but the codebase *already*
   reuses it that way for the SQL result ring (`HomerClientSession.sqlResultDpuStream`,
   `remote_execution_client.h:257`). Adding a second carrier type would duplicate five helpers.
   **S1b renames/extracts.**
3. **Export role 1 only; leave role 7 to its existing opener.** `HomerClientOpenSqlResultReceiveStreamSelectedDpu`
   **already exports a role-1 control slot AND the role-7 ring** (descriptors `[0]`/`[1]`). It is
   Tier-1 and MUST NOT be touched. Two exports per client is fine: `hostMmapImportCapacity = 1024`
   (`homer_service_dpu_dma.c:785`) — which also **softens open question 4** considerably.
   Consolidating the two exports belongs in S1b/S2, not here.
4. **Do NOT call `HomerClientOpenBackendMailboxes`.** Today's `HomerClientOpenSqlSession` `shm_open`s
   the backend command mailbox **by name on its own host** (`homer_client.c:1627`), because the
   client-side host service created it. That is precisely the node-local assumption the target
   architecture destroys. The DPU-native client's commands ride the role-1 control slot
   (`START_COMMAND`), which the smoke already proves the DPU pulls and answers.

#### What S1a does NOT solve (deliberately deferred to S4/S5)

`commandMailbox` is registered at `REGISTER_MEMORY` as a **local RDMA send source**
(`remoteWritable=false`, `:35346`). On a DPU-resident service its `shm_open` still succeeds — it
simply becomes DPU-local memory nobody else maps. **It likely survives the move unchanged.**

> **"Scratch" is a misnomer — and the migration is what would make it true.** The field is called
> `clientSqlCommandScratchRegionHandle` (`:1303`), but it is not a scratch buffer: it is the
> `ibv_reg_mr` registration *of the command mailbox itself*. `sourceSlot = &mailbox->commandSlots[i]`
> and `sourceRecord = &sourceSlot->record` (`:19801`); the RDMA WRITE posts **from that exact address**
> (`:19895`-`:19898`) into the peer's mailbox. An RDMA source needs an lkey — that is all the handle is.
> `remoteWritable=false` because nothing writes into it remotely.
>
> So today there is **no staging copy at all**: the client writes the command into the mailbox (same
> host, shared memory) and the service RDMAs out of those bytes. **Zero copies.**
>
> **The DPU migration spends that property.** Once the mailbox is DPU-local, a command must go
> client control slot → **DMA pull across PCIe** into the DPU mailbox → RDMA out. That pull is work the
> current design does not do, and only *then* would "scratch" be an accurate name.
>
> **The removal is "direct PCI-source RDMA"**: register the *imported host mapping* as an RDMA memory
> region on the DPU so the local QP's send SGE points straight at host memory. The NIC then reads the
> command over PCIe as part of the RDMA WRITE — no `doca_dma` task, no DPU-resident copy. Recorded as
> decided but never mapped out. The reason it is unmapped is a **DOCA/verbs interop question, not a
> Homer design one**: the DPU holds a `doca_mmap` reconstructed from the host's PCI export, and it is
> not established that an `ibv_mr`/lkey usable by the posting QP can be derived from it.
>
> > **⚠ CORRECTED (July 9, 2026).** An earlier revision of this note called direct PCI-source RDMA "a
> > correctness-of-performance requirement, not an optimization," and said to reconsider it **in S4**.
> > **Both claims were wrong.**
> > - **S4 is single-node — it contains no RDMA at all**, so direct PCI-source RDMA cannot apply there.
> >   S4 does introduce a DMA pull, but nothing today runs a single-node command through a DPU, so there
> >   is no baseline for it to regress against.
> > - **The cost is small.** A PCIe DMA read is order 1–2 µs against a warmed c1 transaction of ~238 µs
> >   (~4.2k TPS). Under 1%. It is not a parity threat per operation.
> >
> > What is actually true: the pull begins mechanically at **S5/S6**, and **S6** — cross-node DPU vs
> > today's cross-node host-service `--homer` — is the first comparison against a real number. The risk
> > it poses is to **scaling**, not per-op latency: DMA task slots and PE polling are shared on the DPU,
> > and c4 already shows a shared-transport bottleneck. So: **measure at S6, do not pre-optimize.**
> > This entry exists so that an S6 number below the host-service baseline is not mysterious.
>
> Two smaller notes: the MR covers the whole 64-slot mailbox, registered once per session (cheap). And
> for commands ≤ `max_inline_data` (124 bytes on this fabric) the code takes `useInlineCommandPost` and
> `IBV_SEND_INLINE` (`:19883`-`:19894`), where the HCA copies the payload into the WQE and the MR is
> nearly vestigial — but a pgbench `UPDATE`/`SELECT` record exceeds 124 bytes, so the normal path is the
> registered one.

### 📌 S4-stage perf item: the command pull moves 67,912 bytes to carry a few hundred

**Measured, July 9, 2026.** `sizeof(CitusRemoteExecControlSlot) == 67912`. `HomerDpuDmaSubmitOneCommandPull`
DMAs the **entire slot** every time — `doca_buf_set_data(srcBuf, srcAddress, sizeof(CitusRemoteExecControlSlot))`
— regardless of the request in it:

| | bytes |
|---|---|
| `CitusRemoteExecControlSlot` (what we DMA) | **67,912** |
| ├─ request union | 33,784 (all of it is `OpenSessionRequest`) |
| └─ response union | 34,112 (`CommandCompletion` is 33,816 of it) |
| `PollCommandCompletionRequest` (actual payload) | 40 |
| a pgbench `START_COMMAND` | a few hundred meaningful bytes |

Harmless today — control slots carry only `OPEN`/`CLOSE`. **From S4 on, `START_COMMAND` is per
transaction**, so this becomes a ~66 KiB PCIe read on the measured path. Same family as
"direct PCI-source RDMA" above: *don't move bytes you don't need.*

**Do not pre-optimize. Measure at S4/S6.** If it bites, the fix ladder, cheapest first:
1. **Two-phase pull.** DMA a small fixed prefix (slot header + request header, ~512 B). The request
   header carries `payloadBytes`; issue a second pull only if the request exceeds the prefix. For
   pgbench that is one ~512-byte DMA instead of 67,912. **No ABI change.**
2. **Advertise the length up front.** The grouped-control read of the host publish line *already*
   precedes every pull, so the client could stamp the request byte length there and the DPU could pull
   exactly that much — **no extra round trip.** Costs a field in `HomerDpuBridgeHostPublishLine`.
3. **Shrink the unions.** 33.8 KiB per side is fixed-size arrays (`CommandCompletion` alone is 33,816).
   An ABI change, but it shrinks the control slot, the client's export buffer, *and* the mailboxes.

#### Why the per-ring scratch buffer is part of THIS item, not a separate one

The obvious-looking cleanup — give each ring its own pull buffer and delete
`commandPullBufferInUse[]` + `HomerDpuDmaFindFreeCommandPullBuffer` — is **not a perf win and is
currently a memory regression**:

- The free-list scan is 64 `bool`s = **one cache line**, a few ns, against a **67,912-byte** PCIe DMA on
  the same request. Three orders of magnitude apart. Optimising the scan is optimising the wrong thing.
- Per-ring ownership costs `67,912 × rings`. At `hostMmapImportCapacity = 1024` that is **66 MiB** of DPU
  memory versus **4.1 MiB** for the 64-slot pool. Lazy allocation fixes the memory but puts an allocation
  on the control path.
- It touches **three** pools (`commandPullBufferInUse`, `commandResponseBufferInUse`, `controlCellInUse`,
  all sized by `commandPullBufferCount`) plus the `localBufferIndex` validation in
  `HomerDpuDmaAcceptCommandPullSlot`.

**Strong suspicion: the global pool exists BECAUSE the slot is 66 KiB.** Per-ring is the natural design —
`ringRuntime->lastCommandBufferIndex` is already per-ring — and whoever wrote it hit the memory wall and
pooled instead. **Shrink the slot (fix 3) and per-ring ownership becomes nearly free**, deleting a shared
mutable pool, an exhaustion mode, and an artificial 64-concurrent-session cap that is inconsistent with
`hostMmapImportCapacity = 1024`.

**So: if the 66 KiB DMA turns out to matter, solve both together.** Do not do the per-ring change alone.
>
> **Rename** `clientSqlCommandScratchRegionHandle` → `clientSqlCommandMailboxRegionHandle` as part of
> the pending scoped rename (see `byte_ring_slot_capacity_regression.md`). A name that describes a
> design we do not have is worse than an unclear one.

`clientCompletionMailbox` does **not**: it is registered **remote-writable** (`:35365`), the peer
RDMA-writes completions into it, and today pgbench reads it directly out of shared memory
(`HomerClientOpenCompletionMailbox`). Cross-node that must become a **DPU→host DMA into role 6**
(`FRONTEND_COMPLETION_EVENT`).

> **⚠ CORRECTED (July 9, 2026).** An earlier revision said *"Nothing writes role 6 today."*
> **False, and it made S4 look bigger than it is.** The service already writes role 6, end to end,
> in the **single-node** shape — which is exactly S4's shape:
>
> ```
> backend writes its role-3 completion mailbox
>   -> HomerServiceDpuAcceptOneBackendCompletion       (DMA-pulls it)      :39348
>   -> HomerServiceDpuEnqueueSelectedCompletionEvent                        :39007
>   -> HomerServiceDpuPublishOneSelectedCompletionEvent                     :39478
>   -> HomerDpuDmaSubmitFrontendCompletionEventPublication  (DMA into role 6)
> ```
>
> Two things are genuinely missing, and they split cleanly across the stages:
> - **S4:** no **Tier-1 client exports role 6.** The only exporter is the deprecated
>   `homer_frontend_dma.c:1617`. `HomerClientOpenSqlSessionSelectedDpu` must export it and drive
>   `START_COMMAND` down its role-1 control slot; the service-side push then already works.
> - **S5:** no **cross-node bridge.** A remote completion lands in `clientCompletionMailbox` via peer
>   RDMA, and nothing turns it into a selected-session completion event. The role-6 publisher is fed
>   only by *locally* DMA-pulled backend completions.
>
> So the push machinery is **TIER 2 — exists, unexercised by any production client**, not absent.
> Classify before depending on it: it compiles and the smoke touches parts of it, which is exactly
> the trust level that has bitten this project repeatedly.

**Why the normal path never tripped the POLL wedge.** Exercised client SQL is *push*-based — it waits
on the pushed completion, never polls (`TupleSinkServiceHandlePollCommandCompletion` says so itself:
*"Normal pgbench client SQL waits on the pushed completion ring directly; this handler is retained for
debug/legacy local-control callers."*). That is precisely why the wedge stayed latent, and why the fix
was hygiene for the deprecated frontend plus the invariant, not a repair of the measured path.

**Gate:** compiles; the DPU service logs an `OP_COMMAND_SESSION` open arriving over the DPU control
slot. No end-to-end claim yet.

### ✅ S1a DONE (July 9, 2026 — citus `3662f60f7`, postgres `1a81c6710ea`)

Landed: `HomerClientOpenSqlSessionSelectedDpu` / `...CloseSqlSessionSelectedDpu` (`homer_client.c`),
`HomerClientSession.commandDpuStream` (`remote_execution_client.h`), pgbench `--homer-dpu-command`,
and a control-path-only gate instrument in `HomerServiceDpuStageOneCommandForDispatch`.
**No service-side change was required**, exactly as the correction above predicted.

**Topology:** pgbench on farnet0 → its LOCAL DPU `10.10.1.200` (setup TCP 9727) → RDMA → peer DPU
`10.10.1.201:9717`. farnet0's HOST service handled **zero** sessions (it is mapped only so
`HomerClientOpenControl` succeeds; S7.1 removes even that).

```
[homer-service] dpu control slot: OPEN_SESSION opKind=2 sessionKeyOpKind=5 destNode=1
                peer=10.10.1.201:9717 sessionUID=11095041325112745 dpuRelay=0
tuple-sink service: established persistent outgoing RDMA peer transport
                host=10.10.1.201 port=9717 node=1 traffic_class=1
tuple-sink service: async local open failed phase=5
                detail=could not open backend spawn region /citus_remote_exec_backend_spawn_v14
```
(`opKind=2` = `OP_COMMAND_SESSION`, `homer_control_abi.h:45`; `sessionKeyOpKind=5` =
`REMOTE_EXEC_OP_CLIENT_SQL_SESSION`, `homer_frontend.h:49`.)

## 🔑 S5 IS ALREADY WORKING — the only blocker is S2+S3

**`phase=5` is `TUPLE_SINK_SERVICE_LOCAL_CONTROL_ASYNC_COMMAND_WAIT_PEER_OPEN`**
(`tuple_sink_service_process.c:34620`-`:34627`; `UNUSED=0` so `WAIT_PEER_OPEN=5`). The error text
came back **from the peer**, over RDMA, and was then DMA'd into pgbench's control slot as readable
text. **No hang.** So the farnet0 DPU had already completed, in order:

| Phase | What it proved |
|---|---|
| `COMMAND_CREATE` | session allocated; `TupleSinkServiceCreateSession` created BOTH mailboxes on the DPU — S1a.3's dissolution confirmed **empirically** |
| `ENSURE_CONNECTION` | DPU↔DPU `CRITICAL_CONTROL` RDMA connection established |
| `REGISTER_MEMORY` | `commandMailbox` + `clientCompletionMailbox` RDMA-registered — they work as **DPU-local memory**, as predicted |
| `START_PEER_OPEN` | peer `OPEN_COMMAND_SESSION` shipped over RDMA |
| `WAIT_PEER_OPEN` | peer DPU **received and handled it**, reaching `TupleSinkServiceSubmitBackendSpawnRequest` (`:37623`) — i.e. it passed every peer command-open gate (`:37385`, `:37395`, `:37406`) |

The peer failed **only** because `TupleSinkServiceSubmitBackendSpawnRequest` `shm_open`s
`/citus_remote_exec_backend_spawn_v14` **on the DPU**, where no postmaster ever created it.

**Consequences for the plan:**
- **S5 needs no allow-list relaxation.** Its gates already pass; `sessionUID` already threads through
  (`:35401`). What S5 still owes is S5.3 (the receiving DPU triggers the spawn on ITS host) — which
  is S3. Re-scope S5 to "confirm a completion comes back", after S3.
- **S2 + S3 are the whole remaining critical path.** Everything on either side of them is validated.
- The DPU-native command open **propagates errors correctly**. The silent-hang failure mode does not
  afflict this path; it afflicts the byte-ring data plane.

**Next: S2 (postmaster arena, D1+D4) then S3 (spawn trigger, D2).** S0 already proved DOCA can export
the tmpfs `MAP_SHARED` mapping the spawn region is made of.

### S1b — extract the shared Homer-frontend export module
Now load-bearing, because **three** processes need it: the client library, the socketless backend (D1),
and the postmaster (D2). Extract from the Tier-1 `homer_client.c` machinery — **not** from the Tier-2
`homer_frontend_dma.c`.

Surface: "open a control channel to my local DPU; export these regions; submit this control slot;
receive this response." Must be linkable into both `libhomer_client.a` and the PostgreSQL backend.

**✅ Linkability confirmed — no new build plumbing needed.** The PostgreSQL *backend* already links
`libhomer_client.a` (`src/backend/meson.build:48-50`), because `basebackup_homer.c:296` calls
`HomerClientOpenBaseBackupStreamSelectedDpu`. `nm` on the installed `postgres` shows 38 `HomerClient*`
symbols. So the postmaster can call the extracted API **directly**; nothing has to move files.

#### S1b design decision — parameter extraction, NOT struct surgery

The natural-looking extraction is to factor the twelve `dpu*` fields of `HomerClientBaseBackupStream`
into a `HomerClientDpuExportChannel` substruct and retype the helpers on it. **Rejected.** The struct's
layout is load-bearing (`remote_execution_client.h:153-157`: backend and frontend objects must agree on
its size), it has ~50 field references across the Tier-1 basebackup paths, and basebackup is our only
end-to-end regression net.

**Chosen:** make the four `static` helpers public with **explicit parameters** and no carrier type —
`HomerDpuFrontendExportRegion` / `...DestroyExport` / `...SendSetup` / `...SendClose` — and leave the
existing `HomerClientDpu*` helpers in place as thin wrappers that pass the stream's fields. Zero struct
change, zero call-site change, and the postmaster gets a surface that needs no client/session type.
Raw `void *` for the DOCA handles, exactly as the struct already stores them, so the header does not
drag DOCA into every translation unit that includes it.

**Gate:** the selected-DPU basebackup regression still passes (the module's only validated consumer at
this point). Build all targets, including the smokes.

### S2 — the arena + the frontend agent (D1 + D4′)

> **New source files, not more of `tuple_sink_service_process.c` (43,090 lines).** Everything below
> lands in dedicated translation units. The existing 43k-line hub gets *call sites only*.

**Two new TUs, one per side** (decided with the user — four was over-split):

| New file (citus `src/backend/distributed/utils/homer/`) | Owns |
|---|---|
| `homer_frontend_agent.{c,h}` | **host side.** Arena ABI + layout; create (postmaster), map (agent/backend), slot bind/unbind. The bgworker itself: registration, DOCA export via the S1b API, setup send, doorbell connect/backoff/recv, `kill(PostmasterPid, SIGUSR1)`. |
| `homer_service_dpu_doorbell.{c,h}` | **DPU side.** The doorbell listener (its own port, **separate** from the serial setup listener), one persistent connection per attached host, **EOF ⇒ invalidate that import**; and the DMA-the-spawn-slot + ring-the-doorbell path (S3). |

- **S2.1** Postmaster creates the arena at `_PG_init` (`!IsUnderPostmaster`, beside
  `EnsureSpawnRegionMapped`): a named POSIX shm object holding `N = 16` slots of
  {role-2 command mailbox, role-3 completion mailbox, role-5 result byte-ring} — **≈ 57 MiB**, under the
  ~100 MB DOCA single-region ceiling. The spawn region stays its **own** object, unchanged, because the
  non-DPU `--homer` host service still `shm_open`s it by name.
- **S2.2** Postmaster registers the frontend-agent bgworker (`InitializeCitusBackgroundWorker` +
  `RegisterBackgroundWorker`, mirroring `maintenanced.c:205`-`:207`).
- **S2.3** Agent maps both regions, DOCA-exports each with `HomerDpuFrontendExportRegion` (S1b), and sends
  **one setup message carrying two mmap exports** (`mmapExportCount = 2`; descriptors keyed by
  `mmapExportId`). *S1b's carrier-free API is precisely what makes this possible — it was worth doing.*
- **S2.4** The spawned backend `shm_open`s the arena and binds a slot index (prefer-own / adopt-free /
  fatal-on-exhaustion, mirroring `HomerDpuByteRingBind`). **No DOCA, no setup connection, no fork
  inheritance required.**
- **S2.5** Retire the Tier-2 `SELECTED_DPU_DMA` mailbox-open path in
  `remote_execution_backend_bridge.c` (`:2572`-`:2669`) — superseded, and never exercised.
  **But read it first:** it is currently the only implementation of the exporter ≠ reader shape (see
  "Prior art" under S0b). Mine it for patterns before deleting it.

**Gate:** the DPU service's engine shows host mmap imports for the arena and the spawn region
(`HomerDpuDmaImportHostMmapDescriptorForSetup`) — the exact thing whose absence caused the three-run hang.

### ✅ S2 DONE — GATE PASSED (July 9, 2026)

**The gate was not observable when it was written.** `homer_service_dpu_setup_tcp.c`'s import loop logged
only on *failure*, so "the engine shows host mmap imports" could only ever be inferred from the ack the
host received — i.e. from the DPU's own word, read on the far side of the wire. Added a success line, one
per exported mapping, on the cold path. Now the gate is a direct service-side observation:

```
tuple-sink service: DPU TCP setup mmap import accepted bridge_generation=321564234350815
  client_instance_id=550566416136833 export_index=0 mmap_export_id=1 descriptor_first=0 descriptor_count=48
tuple-sink service: DPU TCP setup mmap import accepted bridge_generation=321564234350815
  client_instance_id=550566416136833 export_index=1 mmap_export_id=2 descriptor_first=48 descriptor_count=1

homer frontend agent: exported arena (region=59767936 bytes, 48 rings, export_blob=283 bytes) and spawn
  region (region=17424 bytes, 1 ring, export_blob=279 bytes) to DPU 10.10.1.201:9727,
  accepted_rings=49 imported_blob_bytes=562
```
48 arena rings under `mmapExportId=1`, the role-8 spawn region under `mmapExportId=2`, zero DPU errors.
`/dev/shm/citus_homer_frontend_arena_v1` = 59,767,936 bytes. The agent stays resident and idle.

**Regressions, all green on the same binaries:**
- 4-role DPU-relay basebackup: `delivered_bytes=23233747354`, `CLOSE_ACK`, sender rc=0, consumer rc=0,
  19.42 s (prior band ~20.8 s).
- TCP transport smoke, all six legs + `--export-posix-shm --fork-reader`: both ends `ok`, `distinct_va=YES`.
- **GUC default-off verified**: with `citus.enable_homer_dpu_frontend_agent` unset, no agent process and no
  `/dev/shm` arena. Every existing `--homer` / basebackup deployment is untouched.

**What of D5 is actually validated, and what is not:**
- `HomerDpuDmaFindDescriptorRef` matching on `boundServiceSessionId` — **exercised**. The transport smoke
  calls it at five sites (`homer_dpu_tcp_transport_smoke.c:1431`, `:1527`, `:1657`, `:1847`, `:1983`) and
  is green. Basebackup does not touch it.
- The role-3 `boundServiceSessionId == 0` skip — **proven not to fire spuriously** (0 polls over 15 s with
  16 unbound rings imported), and, per the correction above, **proven not to be load-bearing until S4**,
  because the action is never granted without a session. It stays in: from S4 it saves 15 dead-ring polls
  per granted pass, and it costs nothing.
- Everything the arena's 48 rings will eventually do (bind, DMA, publish) is **still Tier 2**. S2 proves the
  *import*, not the *use*.

Commits: citus `008f4f20b`.

---

#### S2 — implementation design, settled by reading the code (July 9, 2026)

Six facts, each verified in source, that together make S2 much **smaller** than the sketch above.
Read these before touching anything; several contradict what a reasonable person would assume.

**F1. `mmapExportCount > 1` is genuinely implemented, not merely declared.**
`homer_service_dpu_setup_tcp.c:749`-`:781` loops `for (exportIndex < parts.mmapExportCount)` and calls
`HomerDpuDmaImportHostMmapDescriptorForSetup` per export, **with rollback** on partial failure. Each
export becomes its **own** `hostMmapImports[]` slot with its own descriptor slice and `ringRuntime[]`
(`homer_service_dpu_dma.c:5593`-`:5653`). *Caveat:* `HomerDpuComchSetupParts.mmapExport` /
`.mmapExportBytes` (`homer_dpu_comch_abi.h:571`-`:572`) surface **only export [0]** — a convenience
shortcut, not the import path. Do not mistake it for a one-export limit.

**F2. The bridge control block's `hostPublishOffset`/`dpuCreditOffset` are host-side bookkeeping.**
The DPU **never reads them.** All DPU addressing is per-descriptor:
`hostRingAddress + hostControlOffset` for the control line (`homer_service_dpu_dma.c:8472`, `:9047`,
`:9189`) and `+ hostRingOffset` for the data. So two exports with completely different internal layouts
coexist under one bridge header. This is what makes the arena and the spawn region exportable together.

**F3. Roles 2/3/5 do NOT need bridge publish/credit lines.** The Tier-2 prior art proves it:
`homer_frontend_dma.c:1640`/`:1661`/`:1682` all set `hostControlOffset = 0` and
`controlBytes = offsetof(mailbox, slots)`. **The mailbox's own epoch header IS its control block.**
So the arena needs no `HostPublishLine[]`/`DpuCreditLine[]` arrays at all.

**F4. Grouped-control discovery is enrolled BY ROLE, and only for roles 1, 4, 7.**
`HomerDpuDmaDescriptorUsesHostPublishLine` (`homer_service_dpu_dma.c:639`, body ~`:2270`) returns true
only for `FRONTEND_CONTROL_SLOT`, `PAYLOAD_BYTE_RING`, `PAYLOAD_BYTE_RING_DPU_TO_HOST`. Everything else
is skipped at `:1055`. **The arena's 48 rings therefore cost ZERO discovery DMAs**, and a new
spawn-region role is inert until the DPU explicitly writes to it. (Also worth knowing: "grouped" control
reads are currently `controlCount = 1` — one 64-byte DMA per ring, `:8467`. The name is aspirational.)

**F5. There is NO descriptor role that fits the spawn region, and one is required.**
Every mmap export must cover ≥ 1 descriptor (`homer_dpu_comch_abi.h:508`, `:554`), every descriptor must
pass `HomerDpuBridgeDescriptorRoleMatchesShape` (roles 1-7 only, `homer_dpu_bridge_abi.h:283`-`:325`) and
`HomerDpuDmaValidateDescriptorForImport`'s per-role `minControlBytes`/`minSlotBytes` switch, whose
`default:` arm errors with *"host mmap descriptor role is unknown"* (`homer_service_dpu_dma.c:5410`).
Reusing role 2 would make the DPU treat the spawn region as a backend command mailbox. **Role 8 it is.**

**F7. ⚠ THE PLAN HAD A HOLE — pre-exported arena descriptors are UNADDRESSABLE as written.**
This is the finding that mattered. The DPU resolves a backend mailbox by the descriptor's *declared*
session id:
- **Role 2:** `HomerDpuDmaFindDescriptorRef` (`homer_service_dpu_dma.c:5079`) matches
  `descriptor->serviceSessionId == serviceSessionId && descriptor->descriptorRole == role` (`:5123`),
  under a matching `bridgeGeneration` (`:5114`). The key is
  **`(bridgeGeneration, descriptor.serviceSessionId, descriptorRole)`** — not `serviceSinkId`, not
  `sessionUID`, not `(importIndex, ringIndex)`. First match wins; duplicates are not detected (`:5142`).
- **Role 3:** `HomerDpuDmaSubmitBackendCompletionPulls` (`:1205`) scans **every ring of every active
  import** (`:1240`), filters **only by `descriptorRole`** (`:1257`), and submits a control-read DMA per
  match. The session is recovered afterwards from `descriptorRef.serviceSessionId` (`:1284`) via
  `HomerServiceDpuFindSelectedSession` (`tuple_sink_service_process.c:38761`).

But the agent declares all 48 arena descriptors **at startup, before any session exists**, so they carry
`serviceSessionId = 0`. Consequences:
1. Role 2 is unresolvable: `FindDescriptorRef(gen, S, ROLE_2)` never matches a slot declared with 0.
   **This is the real blocker, and it is fatal to S4.**
2. Role 3 polls dead rings: the enumerator would issue a completion-mailbox control-read DMA for each of
   the 16 arena slots, of which at most a few are ever bound.

There is no late-binding key in the descriptor ABI (no `arenaSlotIndex`, no opaque tag), and `ringIndex`
is pinned to the descriptor's table position (`:5365`), so it cannot carry one.

> ### ⚠ CORRECTED BY EXPERIMENT (July 9, 2026) — item 2 as first written was WRONG
>
> The first revision of this section claimed: *"Role 3 breaks the S2 gate itself... From the instant the
> arena is imported, the DPU would submit 16 completion-mailbox control-read DMAs per scheduler pass...
> Importing the arena is enough to trigger it; no session required."*
>
> **Both halves are false, and a probe build proved it.** With the arena imported (48 rings, 16 of them
> role-3, none bound) and the D5 skip **removed**, a temporary counter at the top of
> `HomerDpuDmaSubmitBackendCompletionPulls` recorded **zero calls** across a 15-second window. The
> scheduler never *grants* the `DPU_BACKEND_COMPLETION_PULL` action while no selected-DPU session exists,
> so the enumerator body never runs. The S2 gate is not at risk.
>
> Also wrong: *"each landing in `FindSelectedSession(0)`."* An unbound slot's mailbox is all zeros, so
> `publishedEpoch (0) != consumedEpoch + 1 (1)` and **nothing ever stages**. The semantic layer is never
> reached; the "unknown selected-DPU session" error (`tuple_sink_service_process.c:39380`) cannot fire.
>
> **What is actually true.** From S4 on — as soon as ONE selected-DPU session exists and the action is
> granted — the enumerator iterates **every ring of every import** filtering only on `descriptorRole`
> (`:1250`, `:1257`), so it polls all 16 arena role-3 mailboxes and finds 15 of them dead. That is a
> **silent, perpetual PCIe control-read tax proportional to arena size**, not a startup storm and not an
> error. Silent is worse: it would have surfaced at S6 as an unexplained throughput gap, with nothing in
> any log to point at it.
>
> **This is exactly the failure mode the plan's standing risk #1 warns about**, and I nearly wrote it into
> the plan as a fact. The probe cost four minutes. *Reason from the code; then check the reasoning against
> the machine.*

**F6. ⚠ LANDMINE — `EnsureSpawnRegionMapped()` unconditionally `memset`s the whole region.**
`remote_execution_backend_bridge.c:1709` opens `O_CREAT|O_RDWR` **without `O_EXCL`**, `ftruncate`s to
`sizeof(CitusRemoteExecBackendSpawnRegion)` if the size differs, and `memset`s at `:1756`. It is
idempotent *within* a process (`if (SpawnRegion != NULL) return`), which is why the postmaster's second
call from the SIGUSR1 hook (`:3083`) is harmless. **The agent must never call it** — it would wipe the
postmaster's `postmasterPid`/`slotCount` and any in-flight request. The agent needs an attach-only
helper. The same `ftruncate` is why the arena must be a **separate object**: a single fused object would
be truncated back down by whichever process called `EnsureSpawnRegionMapped` first.

##### Measured sizes (July 9, 2026, `sizeof` on this tree)

| Component | bytes | `controlBytes` | `slotBytes` |
|---|---|---|---|
| `CitusRemoteExecLocalCommandMailbox` (role 2) | 3,202,584 | 24 | 50,040 |
| `CitusRemoteExecLocalCompletionMailbox` (role 3) | 270,552 | 24 | 33,816 |
| SQL result byte ring (role 5) = 64 + 256×1024 | 262,208 | 64 | 1 |
| **per arena slot** (each component 64B-aligned) | **3,735,424** (3.562 MiB) | | |
| **arena, N = 16** | **≈ 57.0 MiB** | | ~43 MB headroom under the ceiling |
| `CitusRemoteExecBackendSpawnRegion` (role 8) | 17,424 | 16 | 544 |

##### Decisions made within the plan's direction (per the "record your reasoning" rule)

1. **New bridge role 8 `BACKEND_SPAWN_REGION`** — shape identical to role 2
   (`{COMMAND, DPU_TO_HOST, FIXED_SLOT, COMMAND_SLOT|FIXED_SLOT_RING}`), distinguished only by
   `descriptorRole`, and deliberately **not** grouped-control enrolled (F4). Its `minControlBytes =
   offsetof(SpawnRegion, slots)` (16), `minSlotBytes = sizeof(SpawnRegionSlot)` (544). Bumps
   `HOMER_DPU_BRIDGE_PROTOCOL_VERSION` 2→3 and `HOMER_DPU_COMCH_PROTOCOL_VERSION` 2→3. An old DPU
   receiving role 8 answers `BAD_DESCRIPTOR` — a clean rejection, not silent corruption.
   *Rejected:* smuggling the spawn-region offset through a `reserved` field of the setup header, to avoid
   the bump. Every other addressable thing in this ABI is self-describing via a descriptor; this would be
   the one exception, and the ABI's own bounds checks would not cover it.
2. **The spawn region's ABI is UNCHANGED in S2.** It is only *exported*, not restructured — no bridge
   prefix/suffix, no version bump, no `_v15` rename. This falls out of F3: its own 16-byte header is a
   legal `controlBytes`. The `arenaSlotIndex` field that `CitusRemoteExecBackendSpawnRequest` will need
   (see below) lands in **S3**, where it has a consumer, together with the `v14 → v15` name bump.
3. **The agent is GUC-gated, default OFF:** `citus.enable_homer_dpu_frontend_agent` (`PGC_POSTMASTER`).
   Without this, every existing `--homer` and basebackup postmaster would start an agent that DOCA-exports
   57 MiB and then fails to reach a DPU setup listener, restarting every 5 s. The arena is created only
   when the GUC is on. Setup host/port/device keep reusing the existing `HOMER_FRONTEND_*` env vars.
4. **Build the `BackgroundWorker` struct directly; do not use `InitializeCitusBackgroundWorker`.**
   That helper hardcodes `bgw_flags = BGWORKER_SHMEM_ACCESS | BGWORKER_BACKEND_DATABASE_CONNECTION`
   (`background_worker_utils.c:47`) and defaults `bgw_start_time = BgWorkerStart_ConsistentState`
   (`background_worker_utils.h:48`). The agent wants **neither**: no database connection, and
   `BgWorkerStart_PostmasterStart` so it is attached before any client opens a session. Ten lines of
   struct fill beats adding a flags field to a shared Citus helper for one Homer-specific caller.
5. **S2.4's backend-side bind: helper in S2, CALL SITE in S3.** Nothing spawns a `SELECTED_DPU_DMA`
   backend until S3 exists, so the call site cannot be exercised in S2 and would be dead-on-arrival code
   validated by nothing. `HomerFrontendAgentBindArenaSlot()` ships in S2 with the arena ABI; S3 calls it.
   Corollary: **the DPU allocates the slot index and stamps it into the spawn request** — it must, since
   it is the party that DMAs into that slot's mailboxes. The backend then does a *checked* claim
   (`FREE → CAS to mine`, fatal otherwise), which also catches a DPU/host allocator disagreement after a
   DPU restart. Slot release is `on_proc_exit` (covers `ereport(FATAL)`); a hard backend crash leaks a
   slot, but that already triggers a postmaster crash-restart cycle which recreates the arena.
6. **S2.5 moves to S7.3.** Retiring the Tier-2 `SELECTED_DPU_DMA` mailbox-open path now would delete the
   only worked example of exporter ≠ reader while S3/S4 are still unwritten — and it is dead code that
   costs nothing to keep. It retires alongside the rest of `homer_frontend_dma*`, which is where it
   belongs. **The plan's instruction to read it first was worth following:** F3 (mailbox-header-as-control-
   block) and F1's multi-export composition both came straight out of it
   (`homer_frontend_dma.c:1261`-`:1304`, `:1590`-`:1697`, `:1704`-`:1735`).

7. **D5 — the DPU BINDS a ring to a session; `HomerDpuDmaRingRuntime.boundServiceSessionId`.**
   *Forced by F7. This is the one place S2 must add real DMA-engine surface.*

   The descriptor says what the **host declared** at setup. A ring of a shared arena has no session at
   that moment. So the DPU needs somewhere to record what it **bound** — and it already has exactly the
   right home: `import->ringRuntime[]`, a DPU-private per-ring array `calloc`'d beside the descriptor
   copy at `homer_service_dpu_dma.c:5593`-`:5594`.

   Add `uint64_t boundServiceSessionId`, **initialised at import from `descriptor->serviceSessionId`**,
   meaning *"the session this ring currently serves, as the DPU understands it."* Then:
   - `HomerDpuDmaSubmitBackendCompletionPulls` skips a ring whose `boundServiceSessionId == 0`
     (an unbound arena slot has no session; do not poll it). **This is the fix for F7's item 2.**
   - `HomerDpuDmaFindDescriptorRef` matches on `boundServiceSessionId` rather than
     `descriptor->serviceSessionId`. **Fix for F7's item 1.**
   - **S3** adds `HomerDpuDmaBindRingSession()` / `...UnbindRingSession()`, called by the slot allocator
     when it stamps `arenaSlotIndex` into the spawn request, and at session close.

   Both changes are **behavioural no-ops today**: every existing exporter declares a nonzero
   `serviceSessionId` (client role-1 at `homer_client.c:2875`; Tier-2 roles 2/3/5 at
   `homer_frontend_dma.c:1647`/`:1668`/`:1694`). So the S2 basebackup + smoke regressions prove the
   plumbing is inert before S3 gives it teeth.

   *Rejected: mutate `import->descriptors[i].serviceSessionId` in place.* It is a DPU-private copy, so it
   would work and needs zero resolver changes — but the descriptor is documented as "what the host sent
   over the cold setup channel" (`homer_dpu_bridge_abi.h:191`-`:194`), and silently making it mean
   something else is a trap for the next reader, and for any diagnostic that prints it.
   *Rejected: add an `arenaSlotIndex` lookup key to the descriptor ABI.* Bigger ABI change, touches every
   resolver, and does nothing about the role-3 polling storm — which is the half that actually breaks.
   *Rejected: per-session export by the backend.* That is the D4 alternative; it reintroduces DOCA init on
   the session-open path, which is the entire thing D4 exists to avoid.

##### Two pre-existing bugs in `TupleSinkServiceSubmitBackendSpawnRequest`, found while tracing (fix in S3)

Not on S2's path; do not fix them here. But S3 rewrites this submitter's DPU twin and keeps the host one
(S3.5), so fix both then, in both:
1. **Slot reservation is load-then-store, not CAS** (`tuple_sink_service_process.c:18913`). Two concurrent
   submitters can claim one slot. Latent today only because one service is single-threaded — but a host
   service and `homer_frontend_dma_lifecycle`'s own submitter can both be live.
2. **The success path stores `state = FREE` before reading `response.launchedPid`**
   (`:19003` then `:19018`). A racing producer may re-claim the slot between the two, and the postmaster
   `memset`s `slot->response` when it processes the new request. Narrow, real, and it would present as an
   impossible `launchedBackendPid`.
3. (Lesser) The wait loop at `:18976` has no timeout and no postmaster-liveness check.

##### Arena region layout (`/citus_homer_frontend_arena_v1`)

```
+0      HomerDpuBridgeControlBlockHeader   64 B   -- advisory: the DPU takes its copy from the
                                                     setup message (homer_service_dpu_dma.c:5643).
                                                     Kept in-region for gdb/`hexdump` attribution.
+64     HomerFrontendArenaSlot slots[16]          -- each: { commandMailbox | completionMailbox |
                                                     byteRingControl + byteRing }, components 64B-aligned
```
Descriptors 0..47 = arena (`mmapExportId = 1`), ring index `3*i + {0,1,2}` for slot `i`;
descriptor 48 = spawn region (`mmapExportId = 2`). `ringIndex` must equal the descriptor's position
**globally across the message** (`homer_dpu_comch_abi.h:487`), and each export covers a **contiguous**
descriptor slice (`:505`-`:529`).

##### S2 file-by-file

| File | Change |
|---|---|
| `homer_dpu_bridge_abi.h` | role 8 + shape arm; `PROTOCOL_VERSION` 2→3 |
| `homer_dpu_comch_abi.h` | `PROTOCOL_VERSION` 2→3 |
| `homer_service_dpu_dma.c` | one `case` arm in the `minControlBytes` switch (`~:5380`); **plus D5**: `HomerDpuDmaRingRuntime.boundServiceSessionId` (init at `:5594`), the `== 0` skip in `SubmitBackendCompletionPulls` (`:1257`), and `FindDescriptorRef` reading it (`:5123`) |
| `homer_frontend_agent.{c,h}` | **new.** Arena ABI + create/attach/bind/release; the bgworker (register, export both regions, compose the 2-export setup message, send, log). |
| `remote_execution_backend_bridge.c` | attach-only spawn-region helper (F6); call arena-create + bgworker-register from the `!IsUnderPostmaster` branch (`:3119`) |
| `shared_library_init.c` | define the GUC |
| `src/backend/distributed/Makefile` | `utils/homer/homer_frontend_agent.o` into the `citus.so` list (`:50`-`:58`) |

**Deferred out of S2, tracked:** the 3.05 MiB role-2 mailbox is 64 slots × a 50,040-byte record — the same
fixed-size-union bloat as the 66 KiB control slot. Shrinking the ABI unions (S4 perf item, fix 3) shrinks
the arena by roughly 8×. Do not do it here.

##### Found during implementation and review (July 9, 2026)

- **⚠ INSTALL ORDER IS NOW LOAD-BEARING.** `citus.so` is built `-fvisibility=hidden` and has **four
  undefined** `HomerDpuFrontend{OpenDevice,CloseDevice,ExportRegionOnDevice,DestroyRegionExport}` symbols;
  they resolve at `dlopen` time from the **`postgres` executable**, which statically links
  `libhomer_client.a` and is linked `--export-dynamic`. So:
  **citus `make install` (installs the archive + citus.so) → postgres `ninja -C build` (RELINKS postgres
  against the new archive) → `meson install` → only then start PostgreSQL.**
  Starting PostgreSQL with a new `citus.so` against an old `postgres` fails every backend with
  `undefined symbol: HomerDpuFrontendOpenDevice`. (`HomerFrontendAgentMain` survives `-fvisibility=hidden`
  because `PGDLLEXPORT` marks it default-visible — verified with `nm -D`.)

- **The S1b API needed a device-scoped variant.** `HomerDpuFrontendExportRegion` opens a `doca_dev` **per
  call**; the agent exports two regions and they must share one device — opening the same PCI device twice
  from one process is not something DOCA promises, and the Tier-2 path has always shared one device across
  its four exports (`homer_frontend_dma.c:1267`-`:1279`). Added `HomerDpuFrontendOpenDevice` /
  `...ExportRegionOnDevice` / `...DestroyRegionExport` / `...CloseDevice`; the original call is now a thin
  open+export wrapper, so no existing caller changed.

- **`RLIMIT_MEMLOCK` checked, not assumed.** `doca_mmap_start()` pins pages and can fail with "the kernel
  failed to pin the requested amount of memory". `dbcomm` has `ulimit -l unlimited` on farnet1, so a 57 MiB
  arena is fine. Re-check on any new host before blaming DOCA.

- **⚠ A FREE arena slot must always already be zeroed.** The DPU chose `slotIndex` and stamped it into the
  spawn request *before* the postmaster forked the backend, so it knows the index all along; once it binds
  its ring it may DMA-read that slot's completion mailbox at any time. If the backend zeroed a dirty slot at
  bind, the DPU could observe the previous tenant's `publishedEpoch` and then watch it go **backwards**.
  Therefore `HomerFrontendAgentReleaseArenaSlot` zeroes the three components **before** publishing `FREE`,
  and `...BindArenaSlot` zeroes again defensively. *The comment the first draft carried — "no consumer reads
  this slot until the DPU is told its index strictly after this function returns" — was simply false.*

- **Arena slot leak across a postmaster crash-restart (known, loud, deferred to S3).** `_PG_init` does not
  re-run on crash-restart, so `HomerFrontendAgentCreateArena`'s whole-arena `memset` does not re-run either,
  and slots left `BOUND` by SIGKILL'd backends stay `BOUND`. The DPU's allocator table *does* reset (new
  import, new generation), so it will re-offer slot 0 and the backend's **checked claim fails loudly** with
  `arena slot 0 is already bound` — attributable, not silent aliasing. That is the checked claim earning its
  keep. **S3 adds an `ownerPid` reaper to the agent's idle loop** (`kill(pid, 0)`), which also covers a
  hard-crashed backend.

- **`ereport(ERROR)` in the agent IS the retry loop.** ERROR → bgworker `sigsetjmp` → `proc_exit(1)` →
  postmaster restarts after `bgw_restart_time` (5 s) → a **new pid mints a new `bridgeGeneration`**, and
  process exit already tore down every DOCA object. So a DPU service that starts *after* PostgreSQL is
  handled with no extra code. The consequence — the graceful `shutdown:` path is skipped, no `CLOSE` is
  sent, and the DPU keeps a stale import until its next DMA against it fails fatally — is the **decided R3
  behaviour**, not an oversight.

- **New `/dev/shm` object to clean:** `/dev/shm/citus_homer_frontend_arena_v1` (57 MiB). Neither it nor the
  spawn region is `shm_unlink`ed at shutdown, matching existing practice. Added to `AGENTS.md`.

---

## D6 — the forward index: a session holds handles to its rings (decided July 9, 2026)

**Supersedes D5's `continue`. Keeps D5's field.**

Both consumers of a session's rings perform a **reverse lookup by linear search**, because the forward map
was never built:

| consumer | has | needs | does |
|---|---|---|---|
| role-2 publish (`...StageOneBackendCommandForPublish`, `:39260`) | the session | its command mailbox ring | `HomerDpuDmaFindDescriptorRef` — scan all imports × rings comparing `descriptor->serviceSessionId` — **once per `START_COMMAND`, i.e. per transaction** |
| role-3 pull (`HomerDpuDmaSubmitBackendCompletionPulls`, `:1205`) | nothing | any ring with a completion | scan all imports × rings filtering on `descriptorRole` (`:1250`, `:1257`), then reverse-map ring → session via `HomerServiceDpuFindSelectedSession` (`:39377`) |

There is exactly one moment when both identities are in hand: **the bind**. So build the map there.

**D6.** When the DPU hands arena slot `k` to session `S` (S3's allocator), it stamps
`boundServiceSessionId = S` on rings `3k+0..2` **and** resolves and caches three
`HomerDpuDmaDescriptorRef`s on `HomerServiceDpuSelectedSessionState`:
`backendCommandMailboxRef`, `backendCompletionMailboxRef`, `resultByteRingRef`.

Consequences:
- role-2 publish becomes a struct field read instead of a per-transaction scan.
- role-3 pull iterates **selected sessions awaiting a completion** — exactly the set that armed the grant
  (`selectedBackendCompletionWaitCount`, `:39966`) — instead of every ring of every import.
- `HomerDpuDmaFindDescriptorRef` survives at **one** call site: bind. A reverse lookup is legitimate
  precisely where the binding is established, and it is then once per session open, not per transaction.
- **D5's `continue` becomes unreachable**, because no ring enumeration remains. Demote it to a one-shot
  loud diagnostic: *"a role-3 ring was enumerated with no binding — grant and iteration bookkeeping
  disagree."* Same shape as the staged-head liveness guard (citus `584e01dcb`).
- **D5's `boundServiceSessionId` field STAYS.** It is the map's key and it is what makes an unbound arena
  ring unresolvable by `FindDescriptorRef`. D6 removes the scan, not the binding.

**This is not an arena workaround.** Every selected session gains the same index — Tier-2, the S1a client,
and the arena alike. The arena only made the missing index impossible to ignore.

**Stale refs are safe by construction.** A `HomerDpuDmaDescriptorRef` carries `bridgeGeneration` and
`ringGeneration`, and every submit revalidates both (`homer_service_dpu_dma.c:1771`, `:2031`). A cached ref
that outlives its import fails loudly instead of addressing the wrong host memory.

**Layering.** `HomerDpuDmaSubmitBackendCompletionPulls` lives in the DMA **engine**, which knows nothing
about sessions — so from where it sits, scanning by role is the only thing it *can* do. The fix moves the
choice up a layer: the engine offers `HomerDpuDmaSubmitBackendCompletionPullForRef(engine, ref)`, and the
service decides which refs. The TCP transport smoke already builds refs via `FindDescriptorRef` at five
sites (`homer_dpu_tcp_transport_smoke.c:1431`, `:1527`, `:1657`, `:1847`, `:1983`), so it converts for free.

*Rejected: a ring-level ready queue.* `HOMER_DPU_DMA_READY_QUEUE_HOST_TO_DPU_BACKEND_COMPLETION` is
**already declared** (`homer_service_dpu_dma.c:113`) and mapped by role (`:5857`), and is **never pushed
and never popped** — dead scaffolding, a fourth instance of "the design was anticipated, the implementation
took a shortcut." The shape does not transfer: for command pull (`:1195`) and payload pull (`:3734`) the
*host* announces readiness by advancing a publish line, and grouped-control discovery pushes the ref. For
backend completion the host announces nothing the DPU can see without a DMA read — **the poll IS the
discovery** — so the queue would have to be refilled every pass with exactly {rings whose session awaits a
completion}. That is a materialization of the session set, plus a queue. Session iteration is that set,
without the queue.

*Rejected: arm per ring instead of per session.* The arming is already correct. A session awaits at most
one completion at a time, so the per-session armed set and the per-ring armed set are the same set. The
arming was never the problem; the execution ignores it.

**Sequencing.** S3 builds the map (bind lives there). **S4.1** switches the consumers, deletes the scan, and
demotes the skip. Do not switch consumers in S3: role-3 iteration by session is only testable once a
session exists.

**Also retracted here:** an earlier note said "S4 will need a `boundServiceSinkId` twin" for the arena's
role-5 ring. That was an assumption. With the ref cached at bind, no `(session, sink)` lookup is needed for
the arena ring at all. What must still be checked in S4 is the **teardown** scans —
`HomerDpuDmaByteRingFrontierForServiceSink` and `HomerDpuDmaMarkImportSenderCloseReleased` search by
`(serviceSessionId, serviceSinkId)` and an arena role-5 descriptor carries `serviceSinkId = 0`. Either they
take the cached ref, or a sink binding is stamped. Decide it there, with the code in view.

---

## D7 — spawn-slot ownership is PARTITIONED BY PRODUCER (contract, decided July 9, 2026)

> ### ⚠ CONTRACT — `slots[0..15]` are the HOST's. `slots[16..31]` are the DPU's.
>
> `CITUS_REMOTE_EXEC_BACKEND_SPAWN_SLOT_COUNT` is 32 (`remote_execution_backend_protocol.h:30`). The range
> is split by **producer**:
>
> - **`slots[0 .. 15]` — host-resident producers.** They contend (the host `citus_tuple_sink_service`
>   submitter, and `HomerFrontendDmaLifecycleSubmitBackendSpawnRequest`,
>   `homer_frontend_dma_lifecycle.c:188`), so they **must claim by CAS**.
> - **`slots[16 .. 31]` — the DPU service.** It is a single-threaded remote producer that reaches this
>   region **only by DOCA DMA**, which gives `memcpy` semantics across PCIe and **no compare-and-swap on
>   host memory**. It therefore *cannot* perform an atomic claim, and it does not need to: its range is its
>   own, and its free list is DPU-local.
>
> The postmaster consumer (`CitusRemoteExecPostmasterSigusr1Hook` → `ProcessSpawnRequestSlot`) is
> **range-agnostic**: it scans all 32 and dispatches on `state`. No ABI change.
>
> **Violating this split reintroduces a race that no code on the DPU side is able to detect.** Each
> producer asserts that the slot it reserved lies in its own range.

**Two pre-existing bugs, fixed in S3 while this contract lands:**

1. **Reservation is load-then-store, not CAS** (`tuple_sink_service_process.c:18909`-`:18921`). Two
   producers can both read `FREE` and both store `CLIENT_OWNED`. Fix: CAS `FREE → CLIENT_OWNED`, and on a
   lost CAS continue to the next slot rather than `break`. **Applies to the host range only** — the DPU
   cannot CAS, which is the whole reason for D7.

2. **The success path publishes `FREE` before it reads the response** (`:19004` stores `FREE`; `:19012` and
   `:19018` then read `reservedSlot->response.launchedPid`). This is **not a writer race — it is a
   use-after-release.** The release store *is* the act of handing the slot away; the postmaster `memset`s
   `slot->response` at the top of `ProcessSpawnRequestSlot` (`remote_execution_backend_bridge.c:2980`) once
   a new owner publishes `REQUEST_READY`. The symptom is an impossible `launchedBackendPid`.

   **The fix is NOT a copy.** Hoist one `int32` read above one store:
   ```c
   int32 launchedPid = reservedSlot->response.launchedPid;   /* take it while we still own the slot */
   TupleSinkServiceAtomicStoreU32(&reservedSlot->state, ..._SLOT_FREE);
   sessionState->launchedBackendPid = launchedPid;
   ```
   The value was already being loaded — just too late. The **error path directly above already does this
   correctly**: it `snprintf`s `response.errorMessage` into the caller's buffer *before* releasing
   (`:18991`-`:18997`). Only the success path is inverted.

   **General rule, worth stating once:** a release-store publishes everything before it and *unpublishes*
   everything after it. Once you store the word that says "this resource is available", every field of that
   resource belongs to someone else. Extract before you publish. Ownership is a property of the protocol
   word, not of the address.

   **The rule gets stricter on the DPU side**, where the read and the release are two DMA tasks: the DPU
   must DMA-read the response, **await it**, and only then DMA-write `FREE`. Same family as the
   ⚠ DMA-BEFORE-DOORBELL invariant — nothing orders two DMA tasks for you unless you await them.

3. (Lesser) The response wait loop (`:18976`) spins with no timeout and no postmaster-liveness check.

---

### S3 — spawn trigger (D2′)
- **S3.1** Doorbell protocol: add `DOORBELL_ATTACH` / `DOORBELL_ATTACH_ACK` / `SPAWN_DOORBELL` message
  kinds to `homer_dpu_comch_abi.h` (`:33`-`:36`) and bump `HOMER_DPU_COMCH_PROTOCOL_VERSION`. Default
  doorbell port **9728**, its own listener — *never* the setup listener, whose single `clientFd` serial
  accept loop a persistent connection would starve (`homer_service_dpu_setup_tcp.c:48`, `:223`).
- **S3.2** Agent: connect the doorbell lazily with bounded backoff (the local DPU service may not be up
  at `_PG_init`), `DOORBELL_ATTACH`, then block in `recv()`. On readable: drain to `EAGAIN` **without
  parsing**, then `kill(PostmasterPid, SIGUSR1)`. **No postgres core change.**
- **S3.2b** Agent idle loop: an **`ownerPid` reaper**. Walk the arena's `BOUND` slots and release any whose
  `ownerPid` is gone (`kill(pid, 0)` → `ESRCH`). Covers a hard-crashed backend and the crash-restart leak
  (`_PG_init` does not re-run, so `CreateArena`'s whole-arena `memset` does not either). A recycled pid can
  give a false positive; acceptable for a prototype, and the checked claim still catches disagreement.
- **S3.2c** Backend: call `HomerFrontendAgentBindArenaSlot(arena, request.arenaSlotIndex, sessionId, ...)`
  in the `SELECTED_DPU_DMA` branch of the socketless backend bootstrap, and register
  `HomerFrontendAgentReleaseArenaSlot` with `on_proc_exit`. Fatal on a failed claim — a collision means the
  DPU and the host disagree about allocation.
- **S3.3** DPU service: replace `TupleSinkServiceSubmitBackendSpawnRequest`'s `shm_open`
  (`tuple_sink_service_process.c:18833`+) with a DMA write into the imported spawn region — **fields
  first, then `REQUEST_READY` with a release barrier, each awaited** — then one `SPAWN_DOORBELL` frame.
  Body in `homer_service_dpu_doorbell.c`; the 43k-line hub keeps only the call.
  Per **D7** the DPU reserves from its own range `slots[16..31]` using a DPU-local free list; it never CASes
  host memory (it cannot). Per **D7 bug 2**, it DMA-reads the response and **awaits it** before DMA-writing
  `FREE`.
- **S3.3b** DPU service: **allocate an arena slot and bind it** (this is where **D6**'s forward index is
  built). Before submitting the spawn request, pick a free arena slot `k` from a DPU-local table, stamp
  `arenaSlotIndex = k` into `CitusRemoteExecBackendSpawnRequest`, stamp
  `ringRuntime[3k+{0,1,2}].boundServiceSessionId = serviceSessionId` on the arena import, and cache the
  three resolved `HomerDpuDmaDescriptorRef`s on the selected session. Release both at session close.
  New engine API: `HomerDpuDmaBindRingSession()` / `...UnbindRingSession()`.
  **Consumers keep using the scans in S3** — the switch is S4.1, where a session exists to test it.
- **S3.3c** ABI: bump `CITUS_REMOTE_EXEC_BACKEND_PROTOCOL_VERSION` 14 → 15 (shm name `_v15`), add
  `uint32_t arenaSlotIndex` to `CitusRemoteExecBackendSpawnRequest` **and**
  `CitusRemoteExecBackendStartupData`, and copy it in `ProcessSpawnRequestSlot`. Host-service producers set
  it to `UINT32_MAX` (unused). Update `AGENTS.md`'s `/dev/shm` list for the `_v15` name.
- **S3.4** DPU service: on doorbell **EOF**, log loudly that the frontend agent for that
  `bridgeGeneration` is gone, and begin the existing import teardown
  (`HomerDpuDmaBeginHostMmapImportTeardown`, `homer_service_dpu_dma.c:6964` — the same path a graceful
  setup `CLOSE` drives). **Diagnostics and clean shutdown, not a recovery path**: a subsequent DMA
  against a dead export is *supposed* to be fatal (see D2′ correction). Do **not** reclassify
  `DOCA_ERROR_IO_FAILED` as recoverable.
- **S3.5** Keep the host-service `shm_open` submitter intact for the non-DPU `--homer` path.

**Gate:** a DPU service causes a socketless backend to be forked on its own host. Assert the ordering
(fields before state word) with a debug check.

#### Risks, named

| | Risk | Mitigation |
|---|---|---|
| R1 | ~~**exporter ≠ reader** aliasing (load-bearing)~~ | **CLEARED — S0b passed** (citus `37cc74b06`) |
| R2 | DOCA ~100 MB single-region ceiling | N = 16 ⇒ ≈ 57 MiB; growth path is `mmapExportCount > 1`, already in the ABI |
| R3 | agent death mid-DMA | **DOWNGRADED.** The agent must not crash; if it does that is a bug and a fatal DPU engine error is the correct outcome. Postmaster keeps the arena mapped, so pages cannot be recycled. Doorbell EOF is logged for attribution (S3.4), not for recovery. |
| R4 | DOCA state across `fork()` | agent is a leaf process; postmaster never touches DOCA |
| R5 | agent `shm_open` races postmaster creation | postmaster creates in `_PG_init`, before the agent starts; agent retries with backoff regardless |
| R6 | bridge-generation churn on agent restart | R3 closes the old import; new export mints a new generation |
| R7 | **trust boundary**: after S3 the DPU DMA-writes `dbOid`/`userOid` into a spawn slot | acceptable for a prototype; named, not discovered |

### S4 — SINGLE-NODE end-to-end (the de-risking gate)
Client, PostgreSQL, and ONE DPU on the same host. **No peer leg, no RDMA, no second DPU.** The client
opens its session on the local DPU; the DPU triggers the spawn; the backend exports its mailboxes to the
same DPU; commands, completions and tuple results all flow host<->DPU by DMA.

**Smaller than written.** The single-node command/completion loop **already exists in the service**:
`START_COMMAND` off the role-1 control slot is DMA-published into the backend's role-2 mailbox by
`HomerServiceDpuStageOneBackendCommandForPublish`, and the backend's role-3 completion mailbox is
DMA-pulled and republished into the client's role-6 line by `...AcceptOneBackendCompletion` →
`...EnqueueSelectedCompletionEvent` → `...PublishOneSelectedCompletionEvent`. The TCP transport smoke
exercises both directions (`--expect-backend-command-publish`, `--expect-backend-completion-pull`),
now including into a tmpfs mapping (S0).

So S4's own work is:
- **S4.0 — cash in D6.** Switch the consumers to the forward index S3.3b built, and delete the scans:
  role-2 publish reads `session->backendCommandMailboxRef` instead of calling `FindDescriptorRef` per
  `START_COMMAND`; role-3 pull iterates selected sessions awaiting a completion, via a new engine entry
  point `HomerDpuDmaSubmitBackendCompletionPullForRef(engine, ref)`, instead of scanning every ring of
  every import. `HomerDpuDmaSubmitBackendCompletionPulls` (the scan) is then deleted or reduced to the
  smoke's use. **Demote D5's `continue` to a one-shot loud diagnostic** — it is now unreachable, and if it
  ever fires, the grant and the iteration bookkeeping disagree.
  Also decide the role-5 teardown question D6 leaves open (`ByteRingFrontierForServiceSink` /
  `MarkImportSenderCloseReleased` search by `(serviceSessionId, serviceSinkId)`; the arena ring has
  `serviceSinkId = 0`).
- **S4.1** Export **role 6** from `HomerClientOpenSqlSessionSelectedDpu` (today the only role-6 exporter
  is the deprecated `homer_frontend_dma.c:1617`), alongside role 1.
- **S4.2** Drive `START_COMMAND` down the control slot from the client, and consume role-6 completion
  events instead of `HomerClientWaitCommandCompletion`'s shm mailbox.
- **S4.3** Depends on S2+S3 for the backend to exist at all.

**Gate:** a `SELECT` returns correct decoded values. Validates D1+D2+D3 together with the smallest
possible blast radius, and is the first proof the architecture works at all.

> The command/completion machinery it leans on is **TIER 2 → 1.5**: it exists, the smoke drives it, no
> production client does. Expect bugs on first real contact — that is what this gate is for.

### S5 — DPU<->DPU command peer-open — **RE-SCOPED: mostly already done**

> **Observed working during the S1a gate run**, before any S5 work was attempted. The farnet0 DPU
> established a `CRITICAL_CONTROL` RDMA connection to the farnet1 DPU and shipped an
> `OP_COMMAND_SESSION` peer-open; the farnet1 DPU received it, passed every gate, and reached
> `TupleSinkServiceSubmitBackendSpawnRequest` (`:37623`). Its error travelled back over RDMA and was
> DMA'd into pgbench's control slot.

- **S5.1** ~~Relax the peer command-open gates by allow-list (`:37385`, `:37395`, `:37406`)~~
  **NO-OP.** They already pass — the peer handler ran past them to the spawn call.
- **S5.2** ~~Thread `sessionUID` through the DPU<->DPU command open~~ **ALREADY DONE**, at
  `tuple_sink_service_process.c:35401` (`peerRequest.openCommandSessionRequest.sessionUID =
  asyncOp->request.sessionUID`). `clientSqlResultDpuRelay` is carried alongside at `:35398`.
- **S5.3** The receiving DPU triggers the spawn on ITS host. **This is S3, and it is the blocker.**

**Remaining gate:** after S3, a cross-node `OP_COMMAND_SESSION` spawns a backend on the peer's host
and a completion comes back. (The "reaches the remote DPU" half is already evidenced.)

### S6 — `pgbench --homer --homer-dpu -c 1`, cross-node
**Gate:** 200 transactions, zero failures, sane / non-constant decoded `abalance`, and no host Homer
service anywhere in the command path. Then `-c 2`, which unblocks byte-ring pool Stage 2
(`tupleSourceRing` per-session).

### S7 — retire the superseded paths
Only after S6.
- **S7.1** `HomerClientOpenSqlSession` (host-SHM command path) for pgbench.
- **S7.2** `citus_remote_exec_pgbench_transaction` + its UDF/extern/build refs (checkpoint "step 8").
- **S7.3** The Tier-2 `homer_frontend_dma*` path and its GUC, now genuinely superseded by S1b.
- **S7.4** Re-examine whether the `sessionUID` rendezvous is still needed. **Expectation: YES, keep it**
  — the role-7 ring is still exported by a host process and imported across PCIe, a boundary that does
  not disappear when the session table moves. Confirm in code; record the reasoning.

**MUST NOT touch, at any stage** (basebackup calls the first unconditionally; `--homer-dpu` result
delivery rides the third): `HomerClientOpenBaseBackupStreamSelectedDpu` (`homer_client.c:2551`, from
`basebackup_homer.c:289`), `HomerClientOpenBaseBackupReceiveStreamSelectedDpu` (`:2922`),
`HomerClientOpenSqlResultReceiveStreamSelectedDpu` (`:3522`).

---

## ✅ FIXED — the staged-command wedge (citus `0efd12c50`, `584e01dcb`)

### The bug, and why it was worse than "a client hangs"

#### The staging pipeline (needed to understand the bug)

Not a ring of staged commands. The chain is **single-slot** most of the way:

```
host role-1 control slot (slotCount = 1)
  --DMA pull--> one of commandPullBufferCount (=64) engine pull buffers
  --accept-->   ONE staged slot per (import, ring):  ringRuntime->stagedCommandSlotValid (a bool)
  --stager-->   a SERVICE-owned queue (copy out, then release the engine pull buffer)
  --action-->   DMA the command / run the semantic dispatcher
```

While a ring's staged slot is set, the pull scheduler issues **no further pull for that ring**
(`homer_service_dpu_dma.c:1154`: skip if `commandPullInFlight || stagedCommandSlotValid`).

**Why 64 buffers when a session runs one command at a time?** Because *"one at a time" is per session,
and the pool is global.* The buffers are a shared free list (`commandPullBufferInUse[]`, linear
`HomerDpuDmaFindFreeCommandPullBuffer`), and a ring holds at most one. So
`commandPullBufferCount = 64` is **how many sessions can have a control command in flight at once** —
not a pipelining depth within a session. It is over-provisioned and harmlessly so: the service-side
queues are tighter (`stagedCommandDispatchSlots[]` and `pendingResponseSlots[]` are **16** each), while
`hostMmapImportCapacity` is **1024**. Nothing here is tuned; if 64 ever binds it is a one-constant
change, and it produces backpressure rather than error.

**Why copy out at all — this is the wedge, seen from the other side.** `CopyNextStagedCommand` peeks
the *first valid ring in index order*. If a stager dispatched in place, session A's `OPEN_SESSION`
— which sits in the async peer-open state machine across an RDMA CM connect and a peer
request/response — would **remain the head for that whole time**, and session B's staged command would
never be seen. **The copy-out-and-release is the act that advances the head.** A kind that no stager
copies out is therefore a *permanent* head: the same mechanism, read in the other direction.

Two further reasons, one of which the code states outright:
- **Durability.** The async op carries a response owner (`importIndex`, `ringIndex`, generations,
  `commandOrdinal`, `requestSequence`, `acceptedPublishedEpoch`). A client disconnect recycles engine
  staging mid-async. Per the code: *"so later stages cannot accidentally depend on engine staging
  storage for response-owner metadata."*
- **Returning the scarce buffer** to the shared pool for other sessions.

**Two stagers, two destination queues** — both copy out and release, they differ in downstream work:

| Kind | Staged-slot owner | Service queue | Then |
|---|---|---|---|
| `START_COMMAND` | `...StageOneBackendCommandForPublish` (releases at `:39264`) | `backendCommandPublishSlots[]` | DMA the command record into the backend's role-2 mailbox |
| everything else | `...StageOneCommandForDispatch` (releases at `:39623`) | `stagedCommandDispatchSlots[]` | run `TupleSinkServiceDispatchLocalControlSlot` |

#### The bug

`HomerDpuDmaCopyNextStagedCommand` takes a **`const` engine**: it **PEEKS**, scanning
(`importIndex`, then `ringIndex`) in **index order** and returning the first ring whose
`stagedCommandSlotValid` is set. Only `HomerDpuDmaReleaseStagedCommandSlot` pops, and the two release
sites above are the only ones.

The DMA layer accepts five kinds (`homer_service_dpu_dma.c:9825`): `OPEN_SESSION`, `CLOSE_SESSION`,
`REPORT_POST_COMMAND_STATE`, `START_COMMAND`, `POLL_COMMAND_COMPLETION`. The dispatch stager excluded
**both** `START` and `POLL`. **Two exclusions, one alternate owner.**

The stagers normally unwedge each other — publish leaves the head if it is not START, dispatch leaves
it if it is — *because every kind has exactly one staged-slot owner*. A staged `POLL` had none. It sat
at the head forever, **starving every ring that sorts after it, permanently**. (Rings *before* it in
scan order drain normally; a wedge on the first ring stalls the whole service, which is what the
validation run saw — its only client was import 0, ring 0.)

> **"No consumer" conflated two roles, and only one was missing.**
> - **Staged-slot owner** — who releases the engine's pull buffer. `POLL` had **none**. *That was the bug.*
> - **Semantic handler** — who answers the request. `POLL` **always had one**:
>   `TupleSinkServiceHandlePollCommandCompletion` (`:37407`), reachable from the dispatcher the whole
>   time. It looks the session up by id and, if `currentCommandSequence` matches and the state is
>   `COMPLETED`/`FAILED`, fills the completion into the response.
>
> The request simply never arrived at its handler.

The old comment claimed START/POLL were "owned by the explicit backend-command and backend-completion
actions". The START half was true. **The POLL half described an action that was never written.**

Latent, not live — but **closer than "latent" suggests.** The deprecated
`homer_frontend_control.c:1255` already branches on `useDpuControlChannel` before building its POLL
request, and the Tier-2 `homer_frontend_dma.c:899` exports a role-1 `FRONTEND_CONTROL_SLOT`. **The
wedge was one GUC away.** And S4/S6 make the DPU control slot the primary command channel.

### The fix: close the partition, don't special-case POLL

The dispatch stager now skips **only `START`** and becomes the **catch-all**. Everything else falls
through to `TupleSinkServiceDispatchLocalControlSlot`, which already handles `POLL`
(`TupleSinkServiceHandlePollCommandCompletion`, `:37942`) and whose `default:` arm turns any *future*
accepted-but-unhandled kind into an error response rather than a wedge.

*Rejected:* rejecting `POLL` at the DMA accept sets `engine->fatalError` — kills the service for a
request that is legal on the host-SHM path. *Rejected:* synthesizing an error response hard-codes
"the DPU cannot POLL" when the dispatcher plainly can.

> **⚠ STAGED-COMMAND OWNERSHIP INVARIANT.** The set of request kinds the DMA layer accepts must be
> PARTITIONED, exactly one owner each: `START_COMMAND` → the backend-command publish action;
> **everything else → the dispatch stager (catch-all)**. A kind skipped by both is never released and
> stalls the engine-wide head. **If you ever add an explicit `POLL` consumer** (the "backend-completion
> action" the old comment imagined), you MUST re-exclude `POLL` from the dispatch stager *in the same
> commit*, or it will be dispatched twice. This is stated at both stagers in code.

### Two clarifications worth keeping

- **The dispatcher's `default:` is NOT an assertion.** `TupleSinkServicePumpControlSlots` CASes a
  `REQUEST_READY` host-SHM slot and dispatches it **without validating `requestKind` first**, so a host
  client writing garbage into its control slot reaches `default:` today. The layering is: the DMA
  accept's own `default:` is the **hard** guard (`engine->fatalError`, an unrecognised kind never
  stages); the dispatcher's `default:` is the **soft net** (error response, not a wedge). In a service
  that must not die on bad input, "assert it never happens" would be the wrong shape.
- **Why a stager AND a dispatcher.** The DMA engine pulls into a small fixed pool of command-pull
  staging buffers (`commandPullBufferCount`) — scarce, engine-owned, transient. The **stager** copies
  the request into the service's own `stagedCommandDispatchSlots[]` and **releases the engine buffer
  immediately**, so a slow async dispatch (an `OPEN_SESSION` can sit in the peer-open state machine for
  many scheduler passes) cannot hold a pull buffer hostage and stall all command pulls. The
  **dispatcher** then runs the semantic handler against the durable copy. They are also separate
  grantable scheduler actions with independent capacity checks.

### Does this constrain the eventual Citus-backend COPY overhaul? No — it helps.

That path (`homer_frontend_control.c`) talks to the **host-SHM** control region and reaches the
dispatcher via `TupleSinkServicePumpControlSlots`. This change touches only what the *DPU-staged* path
forwards. If that backend is later moved onto a DPU control slot, its `POLL` will now be **dispatched
instead of wedging the service**. The only cost is the one-line re-exclusion above, if a dedicated POLL
staged-slot owner is ever written.

**Will COPY want a dedicated POLL action, the way START has one? Almost certainly not.**

The asymmetry is about *data movement*, not about importance:
- `START_COMMAND` needs its own action because it must **DMA a command record into the backend's
  role-2 mailbox** — it consumes a `backendCommandPublishSlots[]` entry, a DMA task, and a
  `pendingResponseSlots[]` entry for the ack.
- `POLL_COMMAND_COMPLETION` moves nothing. It reads local session state and fills a response; the
  generic dispatch path already publishes that response. A dedicated action would buy nothing.

The only justification would be POLL becoming **hot**, and the direction of travel is the opposite:
pushed completions (the peer completion ring) *replaced* polling precisely because each poll costs a
dispatch slot, a pending-response slot, and a response DMA. `TupleSinkServiceHandlePollCommandCompletion`
says so itself — *"Normal pgbench client SQL waits on the pushed completion ring directly; this handler
is retained for debug/legacy local-control callers."*

**Recommendation for the COPY overhaul:** leave `POLL` on the generic dispatch path, or let it wither
in favour of pushed completions. If it ever does go hot, write the dedicated action **and re-exclude
`POLL` from the dispatch stager in the same commit**, per the invariant above.

### A liveness guard for the CLASS of bug (citus `584e01dcb`)

Nothing would have caught the *next* partition hole: a staged command with no consumer is accepted,
valid, and immortal. So the stager now tracks the head's identity
(`bridgeGeneration`, `importIndex`, `ringIndex`, `commandOrdinal`) across consecutive observations and
**warns once**, naming the `requestKind`, if it survives
`HOMER_SERVICE_DPU_STAGED_HEAD_STALL_WARN_OBSERVATIONS` (default 1,000,000). Diagnostic only — it never
fails the service — and it counts only after a free dispatch slot is secured, i.e. only when it *could*
have consumed the head.

**Validated by manufacturing a real wedge, not by inspection.** A farnet0-DPU-only build whose dispatch
stager also skipped `OPEN_SESSION` (a kind the S1a gate really sends), threshold lowered to 1000:

```
tuple-sink service: WARNING staged-command head has not been released after 1000 observations:
  requestKind=1 import=0 ring=0 ordinal=0 bridge_generation=11224976018495102. ...
```
`requestKind=1` is `OPEN_SESSION` — the sabotaged kind. It warned once, and **before** the client's own
timeout. Then the DPU source was restored and both regressions confirmed **zero** warnings (S1a gate;
4-role basebackup `delivered_bytes=23233738650`, `CLOSE_ACK`, rc=0).

**Evidence for the original bug**, from an A/B with a temporary client probe that emitted one `POLL`
over a DPU control slot before the session open (probe reverted, not committed):

```
PRE-FIX   POLL-PROBE: no usable response: timed out waiting for ... probe response
          pgbench: could not open ...: timed out waiting for ... open ... response
          farnet0 DPU log: ZERO "dpu control slot" lines
          -> the POLL never dispatched AND the OPEN behind it never dispatched.

POST-FIX  POLL-PROBE: ... poll completion referenced an unknown compatibility session id
          [homer-service] dpu control slot: POLL_COMMAND_COMPLETION session=999999 sequence=1
          [homer-service] dpu control slot: OPEN_SESSION opKind=2 ...
          -> a real semantic response, DMA'd back; the OPEN behind it proceeded.
```

## Open questions

1. ~~**`backendCpu`** (D3): carry it through the DPU control slot, or use service policy?~~
   **CLOSED (S1a): service policy.** `TupleSinkServiceChooseBackendCpu()`
   (`tuple_sink_service_process.c:871`) is called service-side at `:18907`; `backendCpu` is not a field
   of `CitusRemoteExecOpenSessionRequest` at all. No client-side change.
2. ~~Does the socketless backend need DOCA at startup?~~ **Dissolved by D4** — no. Only the postmaster
   does DOCA.
3. ~~Result byte-ring ownership.~~ **Dissolved by D4** — the arena owns it; the backend binds a slot.
   Still audit what reads `stream.sendQueue` on the service side.
4. **Arena sizing.** `ringCount` per import (`homer_dpu_bridge_abi.h:119`) and
   `hostMmapImportCapacity` (`homer_service_dpu_dma.h:57`) bound how many sessions one export can
   declare. Size for the target concurrency; fatal on exhaustion.
   **Partly answered:** `hostMmapImportCapacity` defaults to **1024** (`homer_service_dpu_dma.c:785`),
   so the *import* count is a non-issue at our concurrency — two exports per pgbench client is nothing.
   What still needs sizing is `ringCount` per export and the arena's per-session descriptor sets.
5. ~~**Postmaster wait-set surgery** (S3.2): confirm where `ServerLoop` builds its fd set.~~
   **CLOSED by D2′ — we do not touch the wait set at all.** For the record, the seam was located and
   sized: `ServerLoop` (`postmaster.c:1628`) waits on `pm_wait_set`, built by
   `ConfigurePostmasterWaitSet` (`:1605`) with an exactly-sized `CreateWaitEventSet(NULL,
   1 + NumListenSockets)`; `ServerLoop` handles only `WL_LATCH_SET` and `WL_SOCKET_ACCEPT` (`:1673`)
   into a `WaitEvent events[MAXLISTEN]` (`:1632`). Adding a doorbell fd would have meant **four** new
   core touch points. The agent bgworker needs **none**.

6. ~~**fd hygiene.** The postmaster's doorbell fd must be closed in every forked child.~~
   **DISSOLVED by D2′** — the postmaster holds no doorbell fd. The agent is a sibling of the backends;
   they inherit nothing from it.

7. **Trust boundary.** After S3 the DPU DMA-writes `dbOid`/`userOid` into a spawn slot, so the DPU is
   now inside the trust boundary for backend spawn (previously a local host process was). Acceptable
   for a prototype; name it rather than discover it.
8. ~~Can DOCA export a POSIX-shm (tmpfs) mapping?~~ **Answered by S0 — YES**, both DMA directions.

## Follow-ups opened by S0

- **Repair the smoke's DPU→host write leg** (S0 side-finding 3): bind a `LANDING` pool slot for each
  role-7 descriptor ref instead of calling `HomerDpuDmaGetLandingRegionMemory`, and thread the handle
  into `HomerDpuDmaSubmitByteRingWrite`. Both write legs (`--expect-dpu-to-host-payload`,
  `--expect-loop2-backpressure`) use `SOURCE_LANDING`, so **this does NOT depend on byte-ring pool
  Stage 2** (only `SOURCE_TUPLE_SOURCE` still reads `engine->tupleSourceRing`, `:3184`).
- **`engine->landingRegion` singleton survived the pool migration** (`:3976` still returns it). Audit
  whether anything besides the smoke reads it; if not, retire it with pool Stage 2.
- The smoke's client exits 0 on legs it wasn't told to expect, so a **server-side** failure surfaces
  only as `client TCP close exchange failed`. That is how this rotted unnoticed. Consider having the
  server's failure reason travel back in the CLOSE_ACK.

## Standing risks

1. **"Working code with no validated consumer" is not dead code, and it is not trustworthy either.**
   Audit by "what would call this when the migration finishes", then check whether anything calls it
   *today*. Both answers matter.
2. **Regression sweep.** Three regressions landed July 6-8 unnoticed because each validation built one
   target and ran one workload. Before and after every stage: build ALL targets (`service-bin`,
   `client-bin`, every smoke) and run basebackup + `--homer` + COPY.
3. **Baselines need a commit SHA**, not a date.
4. **Error propagation is a debuggability tax.** An aborted byte-ring stream hangs the client with no
   error; it has now cost three separate investigations. Fix before the next hard bug.

## Related
- [byte_ring_slot_capacity_regression.md](byte_ring_slot_capacity_regression.md) — the descriptor split,
  and the three `--homer-dpu` bring-up runs that exposed the wall.
- [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) — pool Stage 2 is
  blocked behind S6.
- [session_identity_and_pairing.md](../../../future-directions/citus/transport/session_identity_and_pairing.md)
  — `sessionUID` as the cross-boundary binding key; survives this migration.
