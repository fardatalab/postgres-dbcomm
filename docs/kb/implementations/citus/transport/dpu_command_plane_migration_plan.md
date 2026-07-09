# DPU command-plane migration — detailed plan

> **Naming.** A SEPARATE workstream from the byte-ring pool plan, not its "Stage 3". The pool plan owns
> data-plane *ring ownership*. This doc owns the *command plane*. Stages here are numbered
> independently — cite them as "command-plane S1a", etc.

**Status (July 9, 2026): IN PROGRESS. S0 ✅ done. S1a next. This is a PREREQUISITE for
`pgbench --homer-dpu`, not a follow-on.**

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
`slotCount`/`postmasterPid` (`:1758`). Region name `/citus_remote_exec_backend_spawn_v14`
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

#### The sequence

1. Postmaster: at startup, DOCA-mmap its own spawn region, export it over the setup TCP, and open the
   dedicated doorbell connection.
2. Postmaster: add the doorbell fd to its main-loop wait set.
3. DPU: DMA the slot (fields, then `REQUEST_READY`, each awaited), then send one `SPAWN_DOORBELL`.
4. Postmaster: wakes on the fd, drains it, runs the **existing** slot scan + `ProcessSpawnRequestSlot`.

*No polling, no sleep, no background worker; the SIGUSR1 hop is replaced rather than emulated.*

**Postmaster hygiene (decided, not open).** Upstream PostgreSQL keeps the postmaster minimal out of
extreme engineering caution; we do not have to follow that strictly. **The bar is: the normal path must
never fatally break the postmaster** — and if it does, that is our bug, not a reason to redesign. A
failed `recv()` degrades to close / rescan / reconnect. Keep the receiver parse-free so there is no
input a malformed stream can exploit.

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

`commandMailbox` is used at `REGISTER_MEMORY` as a **local RDMA scratch source**
(`remoteWritable=false`, `:35346`). On a DPU-resident service its `shm_open` still succeeds — it
simply becomes DPU-local staging memory nobody else maps. **It likely survives the move unchanged.**

`clientCompletionMailbox` does **not**: it is registered **remote-writable** (`:35365`), the peer
RDMA-writes completions into it, and today pgbench reads it directly out of shared memory
(`HomerClientOpenCompletionMailbox`). Cross-node that must become a **DPU→host DMA into role 6**
(`FRONTEND_COMPLETION_EVENT`). Nothing writes role 6 today. **This is the real S4/S5 work item.**

**Gate:** compiles; the DPU service logs an `OP_COMMAND_SESSION` open arriving over the DPU control
slot. No end-to-end claim yet.

### S1b — extract the shared Homer-frontend export module
Now load-bearing, because **three** processes need it: the client library, the socketless backend (D1),
and the postmaster (D2). Extract from the Tier-1 `homer_client.c` machinery — **not** from the Tier-2
`homer_frontend_dma.c`.

Surface: "open a control channel to my local DPU; export these regions; submit this control slot;
receive this response." Must be linkable into both `libhomer_client.a` and the PostgreSQL backend.

**Gate:** the selected-DPU basebackup regression still passes (the module's only validated consumer at
this point). Build all targets, including the smokes.

### S2 — the node's Homer frontend is the postmaster; backends bind arena slots (D1 + D4)
- **S2.1** Postmaster creates the arena at startup: spawn region + `N` x {role-2 command mailbox,
  role-3 completion mailbox, role-5 result byte-ring}. `mmap` it BEFORE any fork.
- **S2.2** Postmaster DOCA-exports the whole arena once via the S1b module, declaring every descriptor.
- **S2.3** The spawned backend binds a slot index (prefer-own / adopt-free / fatal-on-exhaustion,
  mirroring `HomerDpuByteRingBind`). It performs **no DOCA and no setup connection**; it inherited the
  mapping through `fork()`.
- **S2.4** Retire the Tier-2 `SELECTED_DPU_DMA` mailbox-open path in
  `remote_execution_backend_bridge.c` (`:2572`-`:2669`) — superseded, and never exercised.

**Gate:** the DPU service's engine shows a host mmap import for the backend's result ring
(`HomerDpuDmaImportHostMmapDescriptorForSetup`) — the exact thing whose absence caused the three-run
hang.

### S3 — spawn trigger (D2)
- **S3.1** Postmaster: at startup, DOCA-mmap its own spawn region and export it over the setup TCP;
  keep the socket.
- **S3.2** Postmaster: add the setup socket fd to its main-loop wait set; on readable, run the existing
  slot scan.
- **S3.3** DPU service: replace `TupleSinkServiceSubmitBackendSpawnRequest`'s `shm_open`
  (`tuple_sink_service_process.c:18833`+) with a DMA write into the imported spawn region — **fields
  first, then `REQUEST_READY` with a release barrier** — then one doorbell byte on the setup socket.
- **S3.4** Keep the host-service `shm_open` submitter intact for the non-DPU `--homer` path.

**Gate:** a DPU service causes a socketless backend to be forked on its own host. Assert the ordering
(fields before state word) with a debug check.

### S4 — SINGLE-NODE end-to-end (the de-risking gate)
Client, PostgreSQL, and ONE DPU on the same host. **No peer leg, no RDMA, no second DPU.** The client
opens its session on the local DPU; the DPU triggers the spawn; the backend exports its mailboxes to the
same DPU; commands, completions and tuple results all flow host<->DPU by DMA.

**Gate:** a `SELECT` returns correct decoded values. Validates D1+D2+D3 together with the smallest
possible blast radius, and is the first proof the architecture works at all.

### S5 — DPU<->DPU command peer-open
Smaller than feared: `CRITICAL_CONTROL` is already the class basebackup's DPU<->DPU peer-opens ride
(`remote_execution_peer_transport_rdma.h:73-75`; validation at `.c:2459`; command peer-open uses it at
`tuple_sink_service_process.c:35316`, `:35427`, `:35430`). The transport is Tier-1; what is unvalidated
is an `OP_COMMAND_SESSION` over it.

- **S5.1** Relax the peer command-open gates by allow-list: opKind rejection (`:37385`) and the
  following mailbox checks (`:37395`, `:37406`).
- **S5.2** Thread `sessionUID` through the DPU<->DPU command open exactly as the host<->host path does,
  so the result relay's existing rendezvous keeps working unchanged.
- **S5.3** The receiving DPU triggers the spawn on ITS host via S3.

**Gate:** a cross-node `OP_COMMAND_SESSION` reaches the remote DPU, spawns a backend, and a completion
comes back.

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

## Latent hazard found while mapping the DPU control path (record, do not fix yet)

**`POLL_COMMAND_COMPLETION` is DMA-accepted off the DPU control slot but never dispatched.**
`homer_service_dpu_dma.c:9825` accepts it as a valid staged request kind, but **both** staged
consumers drop it:
- `HomerServiceDpuStageOneBackendCommandForPublish` (`tuple_sink_service_process.c:39135`) takes only
  `START_COMMAND`, returning at `:39185` for anything else;
- `HomerServiceDpuStageOneCommandForDispatch` (`:39537`) explicitly **skips** both `START_COMMAND`
  and `POLL_COMMAND_COMPLETION` at `:39569`.

So a client that submits `POLL_COMMAND_COMPLETION` over the DPU control slot has its slot consumed and
never answered — **a silent hang**, the same failure signature that has now cost four investigations.
Benign today only because the peer completion ring superseded `POLL` as the normal completion path
(the old `POLL_COMMAND_COMPLETION` peer path is retained as fallback/debug). **Before S4**, either
reject it loudly at the DMA accept, or dispatch it. Do not leave a request kind that is accepted and
then silently discarded.

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
5. **Postmaster wait-set surgery** (S3.2) is a Postgres-fork change. Confirm where `ServerLoop` builds
   its fd set. The bar is "never fatally break the postmaster on the normal path"; upstream's stricter
   minimalism is not a constraint we adopt.
6. **fd hygiene.** The postmaster's doorbell fd must be closed in every forked child, or a backend
   outliving the postmaster keeps the DPU's doorbell connection alive.
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
