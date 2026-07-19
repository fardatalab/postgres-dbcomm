# Homer transport — CONTRACTS & INVARIANTS
<!-- kb-summary: Current high-signal ownership, identity, lifecycle, geometry, and teardown contracts for Homer transport. -->

> **This file is an INDEX, not a narrative.** Everything else in this directory tells a story. This one tells
> you **what a symbol MEANS, what it does NOT mean, and the rule it carries** — so you find out *before* you
> write the line, not after it costs you a day.
>
> **⛔ IT IS A POINTER AND A WARNING — NEVER A SUBSTITUTE FOR READING THE CODE.** A stale entry misdirects *with
> authority*: a lying diagnostic costs more than the silence it replaced. **Go read the code. Verify.**

## ⛔ EVERYTHING HERE IS TRUE **NOW**. NO HISTORY, NO CHANGELOG.

That promise makes this file a focused **starting map for source search**, never a replacement for it. **If a
contract changes, REWRITE the entry** — never add *"used to mean X"*. Two near-identical statements sitting
adjacent, one of them wrong, **is
the exact bug this file exists to prevent** (see the very first entry). History belongs in
[`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) and the commit.

*(A **tempting-wrong-move** warning and a **scoped-out** note are **not** history — they are current facts about
live traps, and they stay. So is a **COST** line: it is calibration — how hard to respect the rule — and it can
never be mistaken for a definition.)*

**Line numbers drift; symbol names do not.** Every entry names the **symbol** as its stable key and gives a line
as a *hint*. Grep the symbol.

**Maintenance is part of the change.** Adding a call site to an `ENFORCES` list belongs in the **same commit** as
the call site. If it is not on the list, **it has not been checked.**

**Repo:** `citus-dbcomm`. Paths below are relative to `src/backend/distributed/utils/homer/`.

---

# SYMBOL ENTRIES

## `HomerPayloadStreamState.byteRingBytes` / `maxRecordBytes` — negotiated geometry survives without local shm queues

- **MEANS:** the immutable byte-ring modulus and maximum record footprint negotiated when
  `HomerServiceCreatePayloadStreamEntry()` creates the stream (`tuple_sink_service_process.c:~26790`).
- **DOES NOT MEAN:** geometry of `sendQueue`, `receiveQueue`, `localReceiveByteRing`, `dpuMirrorRing`, or the
  tuple-source ring. Those are distinct storage/transport objects and the two POSIX shm queues are intentionally
  absent on a DPU service.
- **CONTRACT:** every control-path geometry check/publication that can execute on a DPU-served stream reads these
  stream scalars (using `HomerServiceStreamNegotiatedByteRingBytes()` for the modulus), never a queue mapping.
  Actual host-queue mapping validation may still inspect `TupleSinkServiceQueueMapping.byteRingBytes` because it
  is validating the mapped object rather than discovering negotiated geometry.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** strict peer/local/async reopen checks; tuple receive landing-ring
  sizing in `HomerServiceEnsureLocalReceiveByteRing()`; `HomerServiceFillPayloadStreamQueueDescriptor()`;
  `TupleSinkServiceCompleteStreamOpenAsyncOp()`; and `TupleSinkServiceStartServiceResultPeerOpen()`.

## `CitusRemoteExecSessionKey` — 13-field REUSE/RENDEZVOUS key; base-compat reads only a 5-field SUBSET; 6 fields are RESERVED-in-ABI

- **MEANS:** the 13 fixed-width `uint32` reuse-compatibility key (`homer_control_abi.h:165`) built from all 12
  `RemoteExecutionSessionIntentSpec` fields by `BuildControlSessionKeyFromIntent()` (`homer_frontend_control.c:311`).
  It is the connection-compatibility domain, mirroring Citus connection selection.
- **DOES NOT MEAN:** (1) a UNIQUE session identity — it is **NON-unique by design** (four pgbench clients on one
  db/user/node share ONE key). **THREE DISTINCT IDENTITIES, do not confuse them:**
  - **`sessionKey`** (this) — *non-unique* compatibility/rendezvous key; answers "may these two sessions be
    reused for each other?"; **never** an identity or a binding key.
  - **`serviceSessionId`** — *node-local UNIQUE*, minted monotonically per session on that node
    (`tuple_sink_service_process.c:5727-5765`); answers "which session on THIS node?"; it is what rejects a stale
    role-6 event after slot re-tenancy (fresh id per session).
  - **`sessionUID`** — *cross-node UNIQUE*, client-minted, threaded through open/peer-open and **stamped on
    `HomerDpuBridgeRingDescriptor`**; the ONLY correct key to **bind a ring** to one specific live session.
  ⇒ **never bind a ring by `sessionKey` or by `serviceSessionId`; use `sessionUID`.** (2) that all 13 fields gate
  reuse — **base-compat (`HomerServiceSessionKeysBaseCompatible:24040`, the SINGLE base-compat definition) reads
  only 5**: `protocolVersion`, `destinationNodeId`, `effectiveUserId`, `databaseId`, `executionLane`.
- **CONTRACT:** reuse decision = base-compat (5-field subset) **AND** `TupleSinkServiceSessionAllowsReuse:24626`
  (dynamic: `freshnessPolicy` FORCE_FRESH/REQUIRE_CLEAN, `ownershipPolicy` PIN_EXCLUSIVE, post-command terminal
  state, command-in-flight, placement-access clean-history). The other 6 key fields — `opKind` (used for
  validation/basebackup pairing, not base-compat), `accessKind`, `txPolicy`, `metadataPolicy`, `placementScope`,
  `transactionCritical` — are **carried in the wire ABI but NOT read by the reuse predicate today (RESERVED for
  the future COPY re-plumb).** Activating one is a **PREDICATE change (extend base-compat/`AllowsReuse`), NEVER an
  ABI change** — the 13-field key is frozen.
- **TEMPTING WRONG MOVE:** (a) pruning a "dead" reserved field — it is a wire-ABI field; deleting it breaks the
  protocol. **Keep all 13.** (b) ⛔ **routing the selected-DPU COMMAND open through `FindReusableSession`.** It
  BYPASSES reuse by design — `TupleSinkServiceProgressCommandOpenAsyncOp` `COMMAND_CREATE` allocates a fresh
  session (`tuple_sink_service_process.c:40280`), one-export-per-open; only the **tuple-stream** path reuses
  (`:39326`). **WHY it must stay bypassed:** a command session is **SINGLE-TENANT** (it *is* the tenant unit — one
  command sequence, one role-1/6/7, one backend txn; no per-op sink to isolate owners, unlike a multi-tenant tuple
  compatibility session). N clients share the 5-field base-compat key, so `FindReusableSession` could hand two
  live clients one session ⇒ colliding sequence-1 commands. And there is **no retained command-session pool**
  anyway (close destroys via `TupleSinkServiceResetSession` memset `:25277`; the backend is re-spawned per open),
  so the call would only ever return a live-held session. **Command-open allocates fresh; do NOT wire it through
  `FindReusableSession` until COPY-era backend-pooling + claim/release + sequence-continuation land** (design:
  unified-session-core plan §11.6). (c) binding a ring by sessionKey/serviceSessionId instead of `sessionUID`.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** `BuildControlSessionKeyFromIntent` (all 12 intent→key, keep in sync
  with the struct); `HomerServiceSessionKeysBaseCompatible` (the 5-field subset — the sole base-compat def);
  `TupleSinkServiceSessionAllowsReuse`; `TupleSinkServiceFindReusableSession` callers (`:29783`/`:39326`/`:40711`).
- **Design that promotes this to a unified core:** [`../../../future-directions/citus/transport/homer_unified_session_core_plan.md`](../../../future-directions/citus/transport/homer_unified_session_core_plan.md) §5.6.

## `homer_session_spec.h` — FRONTEND-SAFE (postgres-free) in-process intent spec; a COMPILE-TWICE header, so `_t` types ONLY
- **MEANS:** the frontend-safe header (created in unified-core Stage 1) that owns the **in-process** session-intent
  enums (`RemoteExecOpKind` … `RemoteExecPlacementScope`) + `RemoteExecutionSessionIntentSpec`. It includes only
  `<stdint.h>`/`<stdbool.h>` so it can be compiled into **both** link contexts of the future unified core (the
  postgres server via `citus.so` AND the standalone client library `libhomer_client.a`). `homer_frontend.h`
  re-includes it, so ① compiles unchanged.
- **DOES NOT MEAN:** it is the wire ABI. **Three near-neighbors, do not confuse:**
  - `RemoteExecutionSessionIntentSpec` (here) — *in-process*, enum-typed; backend adapter fills it from live PG
    state, frontend adapter from explicit options.
  - `CitusRemoteExecSessionKey` (`homer_control_abi.h:165`) — the fixed-width `uint32_t` **wire** reuse key this
    intent projects INTO via `BuildControlSessionKeyFromIntent`.
  - `CITUS_REMOTE_EXEC_*` value macros (`homer_control_abi.h:55+`) — the wire-facing enum-value **mirror** the
    standalone service reads; the service does **not** include this header. Keep the mirror tracking these enums.
- **CONTRACT / THE RULE (money line):** a header linked into both link contexts (this one, and the `homer_*_abi.h`
  family) MUST use `<stdint.h>`/`<stdbool.h>` types ONLY — **NEVER** the PostgreSQL `c.h` aliases `uint32` /
  `int32` / `uint64` / `int64` / `Oid` / char-based `bool`. Those aliases live in postgres `c.h`; using even one
  **drags in `postgres.h` and silently destroys frontend-safety**. (`Oid` IS `unsigned int`, so `Oid`→`uint32_t`
  is value-lossless.) **Test = a standalone TU that includes only the header must compile with neither `postgres.h`
  nor `postgres_fe.h`, and `gcc -H` must show no postgres header in its include tree.** Learned this session: the
  tentative "`Oid`→`uint32`" projection was wrong; it must be `uint32_t`.
- **SCOPE (as of Stage 1):** only the intent spec + enums live here. The tuple-sink `RemoteExecTupleSinkOperationSpec`
  (dead host-SHM code, deleted Stage 4) and the `RemoteExecutionCommandSpec` family (deferred to Stage 3, core
  representation undetermined) intentionally stay in the PostgreSQL-facing `homer_frontend.h`.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** any new type added to a compile-twice header — run the standalone
  postgres-free compile test above; `homer_frontend.h` must keep re-including this; the service's
  `CITUS_REMOTE_EXEC_*` macro mirror must track these enum values.
- **Design:** [`../../../future-directions/citus/transport/homer_unified_session_core_plan.md`](../../../future-directions/citus/transport/homer_unified_session_core_plan.md) §10.1/§11.7.

## Byte-ring SLOT size vs RING size — **slots are HOMOGENEOUS (DOCA arena); the wire equality is PER-PAIR host==remote / source==host, NOT slot==host**

- **MEANS:** the pool sizes every slot at `slotStorageBytes` = `HOMER_SQL_RESULT_BYTE_RING_STORAGE_BYTES`
  = 10 MiB, and slots are HOMOGENEOUS — same size + `[control | storage]` layout — because the multi-region
  DOCA arena requires it (one DOCA mmap per region of N uniform slots; `doca_buf_inventory_buf_get_by_addr`
  resolves any slot within its region's mmap). A slot is only a CONTAINER: a stream's LOGICAL ring lives inside
  it, may be SMALLER, and may differ by stream-type — basebackup 8 MiB (`HOMER_PAYLOAD`), SQL 10 MiB
  (`HOMER_SQL_RESULT`). "Independent relay chains may use different capacities" (`tuple_sink_service_process.c:35020`).
- **DOES NOT MEAN:** slot storage == the stream's logical ring, and NOT slot == host. The sender MIRROR is a
  byte-verbatim, position-preserving STAGING buffer: the pull clamps each copy to cross neither the host wrap
  nor the mirror-slot wrap (`homer_service_dpu_dma.c:4698-4710`), and egress ships slices at matching absolute
  offsets — so a record may be split across the mirror-slot STAGING wrap (rejoined at the far end by absolute
  offset) but is NEVER split across a RING wrap (the wrap-gap protocol keeps records off the ring wrap, carried
  byte-identically). The mirror slot need only be `>=` the in-flight credit window (capacity), NOT `==` the host ring.
- **⚠ TEMPTING WRONG MOVE:** do NOT add a `slot == host` (or `slot == host == remote`) assertion. The mirror is
  size-decoupled, so it would FALSE-FIRE on every basebackup (slot 10 MiB, host 8 MiB — both correct). An earlier
  revision wrongly called the 10-vs-8 slot/host mismatch a "silent corruption / CURRENTLY VIOLATED" bug; it is
  not — VALIDATED 2026-07-17 (4-role basebackup: 44,356 ring laps at 10-MiB slot / 8-MiB host, no corruption / no
  failure signature).
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** the real invariant is PER PAIR, fail-closed —
  sender `host frontend ring == remote receiver ring` (`hostRingBytes == remoteRingBytes`,
  `tuple_sink_service_process.c:35031`); receiver `source ring == host role-7 ring`
  (`hostRingStorageBytes == sourceStorage`, `:37968`). Slot homogeneity is set once at the
  `HomerDpuByteRingPoolInit` call (`homer_service_dpu_dma.c:~15664`). Records never straddle a ring wrap.

## `HomerServiceStreamNeedsLocalShmQueues()` — service-level local-backend test

- **MEANS:** this service has no active DPU DMA engine and therefore may have a local backend process polling
  `stream.sendQueue` / `stream.receiveQueue`.
- **DOES NOT MEAN:** `HomerServicePayloadStreamUsesDpuMirrorSource()`. That predicate also applies a per-stream
  tuple-view relay discriminator and is not the queue-allocation contract.
- **CONTRACT:** when `HomerServiceDpuDmaSchedulerEnabled(dpuDmaState) && dpuDmaState->engine != NULL`, neither
  POSIX shm queue is mapped for any newly created stream. Host services retain both mapping arms; basebackup still
  independently omits `receiveQueue`. Landing, tuple-source, and DMA-mirror rings are unaffected.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** byte-ring and fixed-slot `sendQueue` mapping arms and byte-ring and
  fixed-slot `receiveQueue` mapping arms in `HomerServiceCreatePayloadStreamEntry()`; informational descriptor
  publication in `HomerServiceFillPayloadStreamQueueDescriptor()`.
- **SCOPED LIMITATION:** unflagged tuple/COPY streams require a host service without an active DPU DMA engine. The
  §31 service-level decision intentionally does not preserve a mixed legacy-shm/DPU-service mode; runtime validation
  in §31 covers selected-DPU SQL results and four-role basebackup, not that unsupported combination.

## `descriptor->serviceSessionId` (`homer_service_dpu_dma.c`) — the host's COLD-SETUP DECLARATION

- **MEANS:** what the host **declared** about a ring when it exported the mmap, once, at cold setup.
- **DOES NOT MEAN:** the session **currently bound** to that ring. For the frontend agent's **shared arena it is
  `0` for EVERY ring** — the arena is exported once at postmaster start and its slots are handed to backends
  *later*, one at a time.
  ⚠ **Not to be confused with `ringRuntime[i].boundServiceSessionId`**, which is what the DPU has actually BOUND.
- **CONTRACT:** ⛔ **NEVER use it to answer "which session owns this ring."** There is exactly **ONE** way to ask:
  **`HomerDpuDmaRingBoundSessionId(import, ringIndex)`** (`homer_service_dpu_dma.c:757`). It returns `0` for an
  unbound ring; callers resolving *by session* never pass `0`.
- **ENFORCES:** `HomerDpuDmaSetupContainsServiceSession()` (`homer_service_dpu_dma.c:~10874`) and its three
  callers in `tuple_sink_service_process.c` (`:~42443` payload setup-drain, `:~42530` setup-close semantic
  drain, `:~42583` **selected-session release**).
- **COST:** violated at `homer_service_dpu_dma.c:10874`, against a rule stated **in its own file, 10,000 lines
  above**. The predicate silently answered *"not mine"* for every session ⇒ the close-finalizer `continue`d past
  all of them ⇒ **node A never released a single selected session**; its 64-slot table filled; it could no longer
  stage commands; node B spawned backends that received **zero** commands. Gate died at session 65. **~1 day.**
  Fixed citus `f12a96112`. Audit §44–§45.
- ⚠ **The function's doc comment said it mapped identities back to "*descriptor* service sessions."**
  **That phrasing is where the bug came from.** *A doc comment that names the wrong field is not a comment — it
  is an instruction to write the bug.*

## `completion.commandState` — **MANUFACTURED on the selected-DPU egress. DO NOT TRUST IT.**

- **MEANS:** the terminal state of a command, *as published to the peer*.
- **DOES NOT MEAN:** what the **backend** actually did. `HomerServiceDpuEgressOneSelectedCompletionEvent()`
  **overwrites an otherwise-COMPLETED completion's `commandState` to `FAILED`** when the **result relay's**
  peer-open failed (`dpuResultPeerOpenFailed`, `tuple_sink_service_process.c:~43873`) — **while the backend is
  still ALIVE**, and while `dpuBackendTeardownStarted` is still `false`.
  ⚠ **Not to be confused with `commandKind`**, which the conversion **never touches** (it writes only
  `commandState`, `resultFlags`, `detail`).
- **CONTRACT:** ⛔ **Never key a backend-lifecycle decision on `commandState`. KEY ON `commandKind`.**
  **RULE, GENERALIZED: key on the field that is never manufactured.**
- **ENFORCES:** the L1 terminal bookkeeping in `HomerServiceDpuEgressOneSelectedCompletionEvent()` keys on
  `commandKind == CITUS_REMOTE_EXEC_COMMAND_CLIENT_SQL_SESSION_CLOSE`.
- **COST (avoided):** keying on the synthetic `FAILED` would clear `backendLoopActive` with
  `dpuBackendTeardownStarted == false` ⇒ DPU command **landing** rejects the session
  (`tuple_sink_service_process.c:~43363`) ⇒ the client's `TX_ABORT`/`CLOSE` can **never land** ⇒ **permanent
  wedge**. Same shape as the §35 fence-and-return bug, nearly re-created. Audit §40.2.

## `sessionState->activeSinkCount` — **ONE DECREMENT POINT. GO THROUGH THE FUNNEL.**

- **MEANS:** how many payload sinks a service session still owns.
- **DOES NOT MEAN:** a number you may adjust locally. **It is not a plain refcount you can `--`.**
- **CONTRACT:** **`TupleSinkServiceMaybeReclaimSinkAndSession()`** (`tuple_sink_service_process.c:~27726`) is the
  **single decrement point for every sink that ever went LIVE.** It guards inactive streams, checks underflow,
  decrements **exactly once**, resets the stream, **and retires the session if that was its last sink.**
  **THE ONE SANCTIONED EXCEPTION:** `TupleSinkServiceUndoDpuResultStreamIdentity` (`:~22701`) decrements
  directly — it unwinds an EAGER reservation that never reached SUCCESSFUL BACKEND ACTIVATION (spawn start
  failure, deferred-response queueing failure, async `spawnOk == false`). "Live" here means activated-backend
  ownership, NOT the stream entry's `active` flag — reservation sets that flag immediately. Do not remove
  that rollback (the count pins) and do not add funnel accounting to it (double release/underflow, guarded at
  `:~29996`).
  ⛔ **Never call `HomerServiceResetPayloadStreamEntry()` directly on a path that owns a sink reference** — that
  frees the stream and leaves the count pinned.
- **⚠ WHY ONE MISS IS CATASTROPHIC:** **every** retirement path is gated on `activeSinkCount == 0` — the
  sink-release hook, the connection-reset retire, **and `AllocateSession`'s table-full reclaim**
  (`tuple_sink_service_process.c:~24273`). **One missed decrement makes ALL of them refuse** — and it presents
  not as a refcount bug but as *three independent retirement paths being mysteriously unreachable*.
- **⚠ THE REFERENCE IS TAKEN EARLIER THAN YOU THINK:** the selected-DPU result stream takes its `++` at **eager
  identity reservation** — *before* `SERVICE_RESULT_OPEN` installs the synthetic `sendHandleOpen`. **Any release
  helper must tolerate `sendHandleOpen == false`**, or it skips exactly the case that leaks (a result-open that
  never bound).
- **ENFORCES:** `TupleSinkServiceReleaseServiceOwnedDpuResultSinkAfterBackendTeardown()` (called from **T4** and
  from **`HomerServiceDpuAbandonArenaTeardown`**); the `peerResetAfterCleanClose` branch
  (`tuple_sink_service_process.c:~30098`); a reservation that never reached successful backend activation
  rolls back through `TupleSinkServiceUndoDpuResultStreamIdentity` instead.
- **⚠ KNOWN TRAP — REQUESTER ABANDONMENT (recorded, unfixed; coalescing plan D-S2c item 33):** after a
  SUCCESSFUL spawn, release eligibility requires normal **or abandonment** backend teardown — and **nothing
  triggers either when the REQUESTER vanishes** (its connection resets mid-spawn *and the spawn then
  succeeds and activates the backend* — a failed spawn undoes its own reservation — or it disconnects after
  OPEN/reusable commands but before CLOSE or a `DO_NOT_REUSE`/`FAILED` terminal). The never-closed backend
  produces no terminal completion, so the reference is held forever and all three retirement paths refuse —
  session + backend orphan. Owner-accepted Stage-2c boundary; the fix (connection loss cancels an in-flight
  spawn / initiates backend teardown) belongs to the §37/P2-L abandonment-reclaim item.
- **COST:** the clean-close branch used to free the stream and `return`, bypassing the funnel ⇒ node-B sessions
  never retired; the 64-slot session table filled. Fixed citus `494751b7b`. Audit §39.1, §43.
- ⚠ **The comment above that branch carefully listed three functions it was "deliberately NOT calling", each
  with a reason — and never noticed it was also skipping the sink accounting.**
  ***A carefully-reasoned exclusion list is silent about the thing it forgot.***

## `sessionState->backendLoopActive` — **also a SCHEDULER input, not just a backend flag**

- **MEANS:** this session still owns a live socketless backend.
- **DOES NOT MEAN:** merely "a command is in flight". It is a **disjunct of the session-machine gate**
  (`tuple_sink_service_process.c:~45717`), so a **stale `true` mints a `COMMAND_SESSION`/`WAIT_BACKEND`
  scheduler machine FOREVER**, for a backend that exited long ago.
- **CONTRACT — when you may clear it** (the host arm's predicate, `tuple_sink_service_process.c:~21503`): only
  when the command is **terminal** *and* it is `CLIENT_SQL_SESSION_CLOSE`, **or** `FAILED`, **or**
  `TX_COMMIT`/`TX_ABORT` **for a non-`CLIENT_SQL_SESSION` opKind**. Its own comment: *"For persistent client SQL
  sessions, COMMIT/ABORT are no longer terminal."*
  ⛔ **NEVER clear it on every terminal completion.** The gate runs **~14,000 commands on ONE session**; DPU
  landing rejects a non-teardown session whose `backendLoopActive` is false (`:~43363`) ⇒ **the session would
  stop admitting commands at statement #2.**
- **⚠ SCOPED OUT — DELIBERATELY NOT DONE (a CURRENT fact, not history):** the selected-DPU **FAILED arm is not
  implemented.** A genuine backend `FAILED` collides — no-backend cleanup (`:~24618`) vs teardown-fenced landing
  (`:~43378`): **two owners for one mailbox record**, resolved by scheduler order. **Anyone "completing" this
  naively re-creates that race.** It needs a lifecycle design, not a flag write. Audit §40.2.
- **ENFORCES:** L1 in `HomerServiceDpuEgressOneSelectedCompletionEvent()` (CLOSE only); the host arm at `:~21503`.
- **COST:** 63 zombie `WAIT_BACKEND` machines filled the 64-slot machine candidate set and **evicted the one live
  session's own machine** every pass, forever. Fixed citus `9ef06f224`. Audit §41.

## `dpuDmaState->selectedSessions[]` / `selectedSessionCount` — **BOTH DPUs have one**

- **MEANS:** the DPU-side record of a client SQL session selected onto the DPU command plane. **Capacity 64.**
- **DOES NOT MEAN:** a node-B-only structure. **Node A (client-side DPU) allocates these too** — it is where
  commands are staged before being routed to node B.
- **CONTRACT:** released by `HomerServiceDpuResetSelectedSessionForClose()`, reached from **two** owners:
  **node B's T4 arena teardown** *and* **the setup-close finalizer**
  (`HomerServiceDpuResetSelectedSessionsForClosingSetup`, registered **without a node-role condition** —
  **node A's only release path**).
- **⚠ IF NODE A STOPS RELEASING, THE SYMPTOM APPEARS ON NODE B:** node A's table fills ⇒ it cannot stage the
  client's command ⇒ **node B spawns a backend and then lands ZERO commands.** You will debug node B and find
  nothing. See the cross-cutting entry *"Which DPU log?"* below.
- **COST:** the setup-close finalizer was defeated by the `descriptor->serviceSessionId` bug (first entry). Node
  A leaked **100%** of its selected sessions. Audit §44–§45.

## `homer_mode` / `homer_dpu_selected_command` / `homer_dpu_result_relay` — every pgbench Homer session is selected-DPU

- **MEANS:** `--homer` enables Homer semantics, while `--homer-dpu` or `--homer-dpu-command` selects the only
  surviving client command plane. Both selected spellings imply the role-7 SQL-result relay.
- **DOES NOT MEAN:** plain `--homer` selects a dormant host-service fallback. S7.1 retired the host control,
  named-mailbox, and host result-sink client APIs and state; option validation rejects plain `--homer` before
  any session opens.
- **CONTRACT:** `homer_mode` implies `homer_dpu_selected_command`, which implies `homer_dpu_result_relay`.
  `openHomerSession()` unconditionally calls `HomerClientOpenSqlSessionSelectedDpu()`; command completion drains
  only through `HomerDrainPendingDpuResultRelay()` and `HomerClientPollSqlResultDpuReceive()` into role 7.
  Selected session close remains guarded by `commandDpuStreamOpen` so a partially opened session cannot run the
  semantic close protocol against an absent export.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** option fail-close in
  `/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:~8610`; session open/close and relay drain in
  `openHomerSession()`, `finishHomerSession()`, and `HomerDrainPendingResultSink()` in the same file; selected-only
  command/completion guards in `/data/dbcomm/citus-dbcomm/src/bin/homer_client.c` at
  `HomerClientStartCommandWithCompletionFlags()`, `HomerClientPeekNextCompletionEvent()`,
  `HomerClientTryCommandCompletion()`, and `HomerClientWaitCommandCompletion()`.
- **TEMPTING WRONG MOVE:** do not recreate a host-shm result fallback when role-7 setup or drain fails. That would
  make intended-path proof ambiguous and reintroduce APIs whose ownership/lifetime was deleted in S7.1(a).

## `HOMER_SERVICE_LOG(...)` and the stats macros — **COMPILED OUT of a perf build**

- **MEANS:** verbose development logging. `HOMER_SERVICE_VERBOSE_LOGGING` **defaults to 0**.
- **DOES NOT MEAN:** anything you can rely on in a measured run. Same for
  `TupleSinkServiceLogPeerTransportStatsRdma` (`#if HOMER_SERVICE_PEER_TRANSPORT_STATS`).
- **CONTRACT:** ⛔ **Never anchor a validation proof on a `HOMER_SERVICE_LOG` string.** New validation-bearing
  cold/terminal sites use the always-on `HOMER_EVENT(...)` contract indexed by `../CONTRACTS.md`; during the narrow
  migration, keep the existing plain `fprintf(stderr, ...)` line beside it so manual validation and legacy parsers
  remain useful. (Operational hazards §9.)

## `HOMER_EVENT event=peer_host_spawn_retired` — **the RETIRED peer host-spawn tripwire. Its ABSENCE is the per-run proof.**

- **MEANS:** a peer (cross-node) command handler reached the retired host-service backend-spawn arm and
  **fail-closed with `exit(1)`** (S6 Track A / S7.1 pulled forward, 2026-07-17). Emitted from the peer OPEN `else`
  and the peer START `!backendLoopActive` arm in `tuple_sink_service_process.c`; `site=open|start` distinguishes.
- **DOES NOT MEAN:** positive proof that the DPU command path ran — that is the `dpu_spawn` events
  (`checks.py:103`). This is a **negative tripwire**: on a healthy run it is **ABSENT**, and that absence is the
  load-bearing proof that the DPU spawn arm was active (doorbell attached) before the first OPEN.
- **CONTRACT — the two arms are ASYMMETRIC (do NOT "simplify" them together):** peer OPEN fires when
  `TupleSinkServiceDpuBackendSpawnArmActive()` is false — a **config+doorbell** predicate (`:22491` requires
  `attached && !fatalError && bridgeGeneration!=0`), reachable on a **cold-start doorbell race**, not only a host
  service. Peer START fires on `!backendLoopActive` — a **per-session** flag the `FAILED` clear can drop (see the
  `backendLoopActive` entry above).
- **⚠ WHY `exit(1)` IS SAFE (both reachability paths checked, plan §1.4.1):** the peer-START crash is
  **unreachable for pgbench** (a failed command is non-retryable → `CSTATE_ABORTED`, so the client never re-STARTs
  on the long-lived session); the peer-OPEN cold-start crash **replaces a pre-gut SILENT HANG** (the old arm fell
  through to the host-service `TupleSinkServiceSubmitBackendSpawnRequest` `shm_open` that no postmaster reads; that
  submitter + its two LOCAL callers are now DELETED wholesale in S7.1(b), so this fail-close is the only surviving
  behavior on that peer arm).
- **ENFORCES:** every healthy validation run must assert this alarm is **ABSENT in BOTH DPU logs** (it can be
  caused on one node and observed on the other). A present alarm = the doorbell was not attached before OPEN = the
  run is contaminated, **NOT a pass**.

## `RemoteExecSqlResultDestReceiver.currentBatchHandle` — **MUST be NULL-initialized; the receiver's Init memset is PARTIAL.**

- **MEANS:** the backend's currently-reserved SQL result-sink batch. `RemoteExecSqlResultReserveBatch`
  (`remote_execution_backend_bridge.c`) reserves a new batch ONLY when this is `NULL`; a **non-NULL value means
  "reservation already held, reuse it"** and it returns without reserving, handing the pointer straight to
  `TryAppendTupleViewToCitusTupleSinkBatch` → `TupleSinkCheckBatchWritable`/`TupleSinkCheckDirection`.
- **DOES NOT MEAN:** a field that Init or Startup zero for you. `InitRemoteExecSqlResultDestReceiver` `memset`s
  ONLY `offsetof(RemoteExecSqlResultDestReceiver, resultQueueDescriptor)` bytes (deliberately, to skip clearing
  the ~33KB embedded `resultTupleViewContract`). `RemoteExecSqlDestStartup` writes `resultQueueDescriptor` /
  `resultTupleViewContract` / `resultSinkHandle` / `resultCopySlot` on the row path but **NOT** the rest of the
  trailing tail — `currentBatchHandle`, `resultCopyTupleDesc`, `completionMailbox` are written by neither.
- **CONTRACT / ENFORCES:** `InitRemoteExecSqlResultDestReceiver` MUST explicitly initialize the ENTIRE trailing
  pointer tail after `resultQueueDescriptor` (it now NULLs all five). **TEMPTING WRONG MOVE:** trusting "the
  partial memset plus Startup writes it before use" — Startup does not write `currentBatchHandle`. **Any NEW
  pointer field added after `resultQueueDescriptor` MUST be NULL'd in Init too**, or it inherits stack garbage.
- **THE BUG THIS PREVENTS (fixed 2026-07-18, S7.1 e-COPY round):** the uninitialized `currentBatchHandle` fed a
  garbage pointer into the codec → hard segfault on the first row-producing command (`SELECT abalance`). It stayed
  DORMANT for months only because an adjacent large stack local (`RemoteExecCompletionPublishContext
  publishContext`, `memset` each iteration) happened to zero that frame region; deleting it (host-service COPY
  retirement) removed the accidental cushion. ⚠ The crash site (`TupleSinkCheckDirection`/`BatchWritable`) is the
  VICTIM, not the bug — the defect is the uninitialized INPUT. Found by revert-bisect + static localization.

---

## `signalCommandWrite` / `TupleSinkServiceClientSqlCommandWriteShouldSignal` — **a REAL decision since Stage 2c: terminal ‖ pool ‖ transport. Most command writes are UNSIGNALLED.**

- **MEANS:** the unit signals iff `commandKind == CLIENT_SQL_SESSION_CLOSE` (terminal) ‖
  `acceptedEpoch - retiredEpoch >= (CITUS_REMOTE_EXEC_LOCAL_COMMAND_MAILBOX_SLOTS-1)/2` (mailbox source pressure) ‖
  `TupleSinkServicePeerSendCheckpointDueRdma` (forceNextSignalCheckpoint from a deferred reset, or the per-QP
  half-SQ interval against `lastSignalledPostFrontier`). The decision is made AFTER `unitWrCount` is known and the
  SAME boolean flows into the unit preflight and every post of the unit; an unsignalled unit's owner is popped by a
  later checkpoint's frontier sweep. Sibling funnels: peer-client-completion
  (`TupleSinkServicePeerClientCompletionPublishShouldSignal` — terminal = SESSION_CLOSE **or**
  `postCommandState` DO_NOT_REUSE/FAILED, the teardown-inducing completions; pool = source ring ≥ 8) and control
  (inside `TupleSinkServicePublishControlMessage` — terminal = CLOSE_SINK either kind; TWO independent OR-ed pool
  terms: requests also signal at op-slot occupancy `outstandingControlOps` ≥ 32, and ANY control post signals at
  control-FIFO population ≥ 32).
- **THE REAL INVARIANT (this is the violable rule):** ⛔ **an unsignalled command owner may be released only after a
  later CQE or QP destruction proves its WR retired.** The terms above carry the successor guarantee: terminals
  self-signal at every point the protocol promises nothing further; past each pool threshold EVERY post of that lane
  signals, so a source pool cannot exhaust with nothing signalled outstanding; a deferred reset latches
  `forceNextSignalCheckpoint` (ANY owner class — widened from 2b's command-only latch); and an abandonment below all
  thresholds retains its owners/MRs until a covering checkpoint or QP-death purge — it must not manufacture
  completion. Half-SQ alone is NOT a successor guarantee: traffic can stop below the interval. Normal close/abort
  and continuing traffic have a protocol or pressure successor; accepted abnormal sub-threshold abandonment may
  instead retain an unsignalled tail until a later checkpoint or verified QP destruction supplies retirement proof.
- **DOES NOT MEAN** a tunable interval — there is deliberately NO `-D` macro (the old `SIGNAL_INTERVAL` tripwires
  died with the flip; the policy lives in one function per lane), and inlining does not preclude signalling —
  `IBV_SEND_INLINE` (source in WQE) and `IBV_SEND_SIGNALED` (CQE) are **orthogonal**.
- **THE TRAP (learned the hard way):** the deleted `useInlineCommandPost` fast path made BOTH command WRs
  **unsignalled with no successor guarantee at all** (no ≤½ checkpoint, no terminal flush), so on a compact-only
  stream or at quiesce the tail never retired → SQ leak / `exit(1)`. DELETED 2026-07-14 (audit §32); the surviving
  `TupleSinkServicePostRemoteClientSqlCommandRecord` posts an unsignalled body + a readySeq tail carrying the unit's
  decision, and the §29(b) admission reserve plus the Stage-2b unit preflight guarantee a SIGNALLED tail is always
  postable after its unsignalled body. Releasing an unsignalled owner without either a covering successor or
  verified QP-death proof is the bug; retaining an abnormal quiet tail is deliberate, bounded proof deferral.

---

# CONTROL-FLOW / LIFECYCLE CONTRACTS

## T4 (arena teardown) — releases the backend, **RETAINS the service session**

- **THE ORDER:** drain the arena slot → `TupleSinkServiceReleaseDpuArenaBinding()` →
  `HomerServiceDpuResetSelectedSessionForClose()` → set `teardownCompletedCleanly` → **release the service-owned
  result sink** → *(session retires later, via the funnel, on the peer disconnect)*.
- **WHAT BREAKS IF REORDERED:** releasing the synthetic SEND owner **at OPEN** instead of at T4 would let an
  ordinary semantic close reach the reclaim funnel **before** teardown — **reclaiming a stream the DPU byte-ring
  mirror may still be writing into.**
- **⚠ THE TEMPTING WRONG MOVE — ALREADY TRIED AND REJECTED (twice), see the commented-out lines:**
  `TupleSinkServiceResetSession(serviceSession)` **at T4**. The peer/session lifetime can legitimately outlive
  its DPU backend; `ResetSession`'s close guards must stay with the lifecycle owner. **Do not re-propose it
  without addressing why it was rejected.**
- **⚠ `ResetSession`'s GUARD IS AT THE TOP — keep it there; add no mutation above it.**
  `TupleSinkServiceClientSqlPeerCommandMailboxMayDeregister` is the FIRST thing after the NULL check
  (`tuple_sink_service_process.c:~25040`), *before* the prologue that disables `dpuPeerCommandLandingActive`,
  retires send completions, and clears scheduler state. On refusal it returns `TUPLE_SINK_SESSION_RESET_DEFERRED`
  and **mutates nothing** (PURE DEFER — in particular does NOT set `dpuBackendTeardownStarted`); on pass it resets
  fully and returns `COMPLETED`. It USED to sit *after* those mutations and `exit(1)` on refusal — a guard placed
  after the mutations it guards is not a guard (it can only choose how to die), and disabling landing first would
  wedge the session; the `exit(1)` killed the teardown before `HomerDpuDmaDestroy` drained. **Any new mutation
  added to this function MUST go below the guard.** (Audit §36 PART 2 / §36.10; §48a.)
- **`TupleSinkServiceResetSession` RETURNS `TupleSinkServiceSessionResetOutcome`, not `void`** — `DEFERRED` means
  "left active/bound; lifecycle owns the reset." **ENFORCES — a caller that acts on the reset having happened MUST
  check for `COMPLETED`:** `TupleSinkServiceAllocateSession` (`:~25194`) **continues scanning** on non-`COMPLETED`
  (never hands out a deferred slot); `TupleSinkServiceHandleCloseSession` (`:~41762`) keeps its SUCCESS response on
  `DEFERRED` (does NOT convert to ERROR — §36.2); `main()`'s shutdown sweep and every other caller may ignore it.

## `streamEntry->stream.peerResetAfterCleanClose` — **ONCE CLEAN, ALWAYS CLEAN**

- **MEANS:** the owner asked for a **clean close**, and stamped that decision **onto the stream itself**.
- **DOES NOT MEAN:** anything about whether the owning *session* still exists.
  ⚠ **`sessionState == NULL` IS NOT EVIDENCE OF FAILURE.** On **node A** the owner session is routinely **gone**
  by the time the reset runs — the client's `CLOSE_SESSION` destroyed it milliseconds earlier. **A missing owner
  is the EXPECTED state here.** The flag lives on the stream *precisely so it survives the owner's departure*.
- **CONTRACT:** **cleanliness is decided the moment the owner asks to close. A later lookup that cannot find the
  session must NOT un-decide it.** ⛔ **Never publish a peer FAILURE on a stream whose `peerResetAfterCleanClose`
  is set** — check it **before** any orphan/failure branch, not after.
- **ENFORCES — every site that resolves an owner and may publish a failure:**
  - `HomerServiceMarkPayloadStreamAbortingOnConnectionReset()` (`tuple_sink_service_process.c:~29309`) — the
    **marking** pass. *Already obeyed it; the rule is stated here.*
  - `HomerServiceClearAbortedPayloadStreamAfterReset()` (`tuple_sink_service_process.c:~30048`) — the
    **clearing** pass. **VIOLATED IT until citus `<this commit>`.**
  - *(A new site that resolves an owning session and can publish a failure belongs on this list.)*
- **COST:** the clearing pass took the orphan-**failure** branch on `sessionState == NULL` *before* consulting the
  flag ⇒ **every clean close on node A was published as a REAL PEER FAILURE, on every successful run** (64× in a
  75-session run: `abort payload clear could not find owning session` + `reason=peer-reset-orphan`).
  **A lying diagnostic, on the happy path, at exactly the place a future debugger looks first.**
- ⚠ **THE SHAPE, FOR THE THIRD TIME THIS WEEK:** *the rule was written down, enforced at ONE site, and violated at
  the other.* Same as Wall 5 (`descriptor->serviceSessionId`). **This entry's `ENFORCES` list is the thing that
  catches it — nothing else does.**

## `streamEntry->stream.localSenderHeadMirror` — **SCENARIO C. Retire it on EVERY teardown route, or reset FAIL-STOPS.**

- **MEANS:** an 8-byte heap counter **we own**, registered as an RDMA MR so the **remote CONSUMER** can WRITE its
  consumed-head into it. We publish its `remoteRkey` + address in the peer OPEN request. **We can never see the
  peer's send CQEs** — so deregistering it needs a *proof* the peer will not write again.
- **DOES NOT MEAN:** the peer's head mirror.
  ⚠ **Not to be confused with `peerSenderHeadMirror`**, which is the *sender's* descriptor as held by a
  **receiver** — not ours to free. A **receiver never registers a `localSenderHeadMirror` at all**: the only
  registration site is `TupleSinkServiceEnsureLocalSenderHeadMirror()`, reached ONLY from the **outgoing** async
  open's `STREAM_REGISTER_MIRROR` phase. *(MEASURED: node A, a pure receiver, reports `registered=0` across 75 gate
  sessions and a 23 GB basebackup.)*
- **CONTRACT:** **the ownership test is "do I hold a registered handle?" — `memoryRegionHandle != NULL`. It is NOT
  `peerBindingLocallyInitiated`.** That bit is set at the peer-open **BIND**, which happens strictly **LATER** than
  the registration it appears to guard, so between those points the mirror is live while every "am I the sender?"
  bit still reads **false**.
  **Every route that tears a stream down must retire the mirror BEFORE
  `HomerServiceResetPayloadStreamEntry()`**, which MEMSETs the descriptor and **fail-stops** on a live handle.
- **ENFORCES — every teardown route, each with its own NAMED reason and counter:**
  - `HomerServiceClearPayloadStreamPeerBinding()` (`tuple_sink_service_process.c:~27485`) —
    `BINDING_CLEAR`. **The only PROOF-BACKED one**: it `exit(1)`s unless `HomerServicePayloadCloseCanClearBinding()`
    holds (RECLAIMABLE ⇒ the sender *observed* the peer's final head WRITE, and RDMA WRITEs complete **in order on a
    QP**, so every earlier head-ACK already landed; or reconciled ABORTING ⇒ the QP/CQs are destroyed).
  - `TupleSinkServiceMaybeReclaimSinkAndSession()` (`tuple_sink_service_process.c:~28068`) — `OPEN_FAILED`.
    Proof-free, but the mirror was **never bound**, so it was never peer-writable.
  - the shutdown stream loop in `main()` (`tuple_sink_service_process.c:~49960`) — `SERVICE_EXIT`. Proof-free; safe
    **only** because the peer transport (and every QP/MR) is destroyed a few statements later, so a poisoned QP has
    **no next user**.
  - `HomerServiceResetPayloadStreamEntry()` (`tuple_sink_service_process.c:~26050`) — the **FAIL-STOP TRIPWIRE**.
  - ⛔ **A NEW TEARDOWN ROUTE BELONGS ON THIS LIST. If the tripwire fires, ADD A RETIREMENT — do not relax it.**
- **⚠ THE TEMPTING WRONG MOVE — *DEFERRING* the dereg.** A comment used to say: *"a receiver may still have an older
  consumed-head ACK WR in flight after the sender reclaimed the stream; keep the mirror valid until the peer
  connection resets, or a late WRITE fails `IBV_WC_REM_ACCESS_ERR` and poisons the QP."* **The hazard was real —
  someone got a poisoned QP.** But the "deferral" was **never implemented**: the flag was set in one place, read in
  two (both merely *skipping* the cleanup), cleared **nowhere**, and then `memset` erased the only pointers. **It
  was not a deferred cleanup, it was an ABANDONED one**, leaking one MR + 8 bytes **per session**. It is excluded
  today by the `BINDING_CLEAR` proof above.
- **COST / EVIDENCE:** the tripwire has caught **two** real bugs, and both looked like "no error" beforehand:
  1. **mid-transfer SIGTERM** — shutdown resets `active` streams *before* destroying the transport, so a stream open
     at SIGTERM hit reset with a live mirror ⇒ `exit(1)`, **teardown never reached `HomerDpuDmaDestroy`**.
  2. **the byte-ring-pool refusal at `-c > 8`** — the documented capacity failure. The refused peer-open landed in
     the reclaim funnel with a registered-but-unbound mirror ⇒ **`-c 10` KILLED the DPU service** instead of
     partially failing. Before the tripwire this had been a **silent MR leak per refused session**.
- **HOW TO PROVE IT, don't assume it:** the service prints, at teardown,
  `head-mirror MR accounting: registered=N released_on_rebind=N retired_on_clear=N retired_at_exit=N
  retired_on_open_failure=N live=N`. **`live` MUST be 0.**
  ⚠ **The tripwire's SILENCE is a ZERO COUNT and proves nothing on its own** — it cannot distinguish *"every mirror
  was retired"* from *"none was ever registered, so the test was vacuous."* Only `registered` vs `retired` can.
  ⚠ **`registered == retired` is the WRONG invariant**: `TupleSinkServiceEnsureLocalSenderHeadMirror()` can
  dereg-and-**RE-register** in place when the peer connection changed (`released_on_rebind`). That is a
  *replacement*, not a retirement; folding it in manufactures a phantom leak on every reconnect.

## The clean-close payload reclaim — **funnel, then check you were not declined**

- **THE ORDER:** clear the peer binding → `TupleSinkServiceMaybeReclaimSinkAndSession()` → **verify the stream is
  no longer `active`**.
- **WHAT BREAKS IF SKIPPED:** the funnel has **three** early-return guards (local handles, peer binding, deferred
  failure). If it **declines**, the stream is never reset and you have swapped a sink leak for a **stream-table
  leak, silently.** The code now ALARMs and **names which guard held** — *a single shared message across three
  conditions is a decoy, not a diagnostic.*

---

# CROSS-CUTTING *(no single symbol owns these)*

## Q: "It failed at 64 — which resource ran out?" — **WRONG QUESTION. SEVEN of them are 64.**

| resource | where |
|---|---|
| node-B **service session table** | `homer_abi_version.h:50` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS` |
| **selected-DPU session table** ⚠ *(BOTH nodes)* | `tuple_sink_service_process.c:~42953` |
| scheduler **machine candidate set** | `HOMER_PROGRESS_PLAN_MAX_GRANTS`, `tuple_sink_service_process.c:2551` |
| scheduler **flat ready set** | same constant |
| **payload stream table** | `homer_abi_version.h:51` `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS` |
| peer transport **outgoing** connections | `remote_execution_peer_transport_rdma.c:70` |
| peer transport **incoming** connections | `remote_execution_peer_transport_rdma.c:71` |

- **⛔ THE TRAP: ONE EXHAUSTION MASKS THE NEXT.** Node B's session table and node A's selected-session table both
  cap at 64; node B's cliff fired at session **64**, so **in the entire life of this code nobody ever reached 65
  to discover node A's table was full too.** Fix one and the next steps straight into its place — **and looks
  exactly like a regression your fix just caused.** It was there all along.
- **TELL THEM APART BY HOW THEY FAIL:** session table → `"peer service ran out of free command sessions"`
  *(a peer **response** — **never logged on the DPU**; grep the **client**)*. Machine candidate set → a **30 s
  command timeout**. Selected-session table → `"selected-DPU session table is full"` **on node A**.
- **NOT 64:** DPU byte-ring pool = **8** (`homer_service_dpu_dma.h:41`); frontend arena = **16** slots; import
  table = **1024**.

## Q: "The client failed. Which DPU log do I read?" — **BOTH. ALWAYS.**

- **Node A (farnet0 DPU) DRIVES the command plane; node B (farnet1 DPU) EXECUTES it.**
  **A failure OBSERVED on one node is routinely CAUSED on the other.**
- **COST:** node B spawned a backend and landed zero commands. I read node B's log, found nothing, and built an
  entire suspect list out of node-B code — **every suspect wrong, because the bug was not on node B at all.**
  **Node A's log was 9.4 MILLION lines**, 9,416,076 of them `selected-DPU session table is full`. **Nobody had
  ever opened it.**
- **HOW:** node A's log is on **farnet0's** DPU — `ssh farnet0` → `ssh dpu`. **Ship a script, never a double-hop
  one-liner**, and **grep it ON the DPU**: a 9.4-million-line `cat` across a double hop simply times out.

## Q: "Nothing is in the log, so nothing went wrong?" — **NO. THESE FAIL SILENTLY.**

- **`HomerProgressMachineCandidateSet` has SEVEN overflow counters — all were WRITE-ONLY.** The *ready set* had a
  report function; **the machine candidate set had none**, so the drop that actually killed the run emitted
  **nothing**, while its noisy neighbour printed an overflow that produced an entire *wrong* root-cause chain.
  ⇒ now `HomerServiceReportMachineCandidateOverflow()` — **per PHASE**, with a **kept-kind histogram**.
- **DOWNSTREAM POLICY ADMISSION is a tighter, also-silent boundary:** **16 grants / 12 machines / 2 PAYLOAD**
  (`tuple_sink_service_process.c:182`). ⇒ now `HomerServiceReportPolicyAdmissionDrops()`.
  ⚠ The code names this failure itself: *"**ARMING A CANDIDATE IS NOT ENOUGH** … armed on every pass and granted
  on none … **Nothing warns you** … **the service looks idle**."*
- **RULE: NO SILENT CAPS.** A bounded set that drops work must say **what it dropped — and what it KEPT.**
  *The victim is an accident of ordering; the occupants are the bug.*

---

## `TupleSinkServiceDrainTaggedSendCompletionsInternal(callbacks=...)` — **NO CQ CONSUMER MAY POLL WITHOUT OWNER DISPATCH. The choke point enforces it.**

- **MEANS:** the single funnel through which every nonblocking send-CQ drain runs. Since D-S0 it **REFUSES
  (`callbacks == NULL` → once-latched ALARM + false) before the first `ibv_poll_cq`** — dequeuing a CQE is
  destructive (it is the owner's ONLY retirement event, F7/§25), so a blind poll is never a degraded mode,
  it is a lost resource.
- **DOES NOT MEAN** a per-caller convention. It used to be one, and two callers passed NULL (the
  control-response exhaustion drain, the peer-pump SEND_CQ phase) while the blocking waiter EXEMPTED
  `CRITICAL_CONTROL` — an exemption written before the command-plane migration put typed lanes on that QP.
  All three are gone; do not reintroduce a caller-side "we know this CQ is empty of typed CQEs" argument —
  that is exactly the assumption that rotted.
- **CONTRACT — `HomerServicePersistentSendCompletionCallbacks` is RETIRE-ONLY** (`tuple_sink_service_process.c`,
  comment at the struct): these callbacks now run from mid-operation transport drains NOT bracketed by
  `HomerServiceSendCqDrainDepth`. A callback that posts a WR or polls a CQ requires extending that guard to
  every dispatch site FIRST.
- **ENFORCES:** the guard in `...DrainTaggedSendCompletionsInternal` (transport); the public drain wrapper and
  every exact scheduled, pressure, and post-checkpoint pull pass
  `transportState->sendCompletionCallbacks/-Context`; registration happens once in service init
  (`TupleSinkServiceRegisterPeerSendCompletionCallbacksRdma`) before any peer connection exists.

## `connectionState->admittedSendWr` — **the CLAMPED admission ceiling. NOT the provider grant.**

- **MEANS:** `min(provider-granted cap.max_send_wr, CITUS_REMOTE_EXEC_PEER_SEND_QUEUE_DEPTH)`, stored at QP
  creation. Admission (`TupleSinkServiceAdmitSendChainRdma`) bounds outstanding WRs by THIS, which is what
  keeps the 256-entry `signalledFrontierMark[]` table (sized by the REQUESTED depth) safe from overwrite.
- **DOES NOT MEAN** what the provider granted. The raw grant may be larger; it is visible in the
  once-per-connection `peer QP caps` log line (and the payload staging-pool setup check deliberately still
  tests the RAW grant — stricter, never weaker). The field was **renamed from `grantedSendWr`** precisely so
  the name cannot re-imply the raw grant; if you need the raw number, read the log, not this field.
- **ENFORCES:** the clamp at QP creation (`TupleSinkServiceInitConnectionResources`); every admission /
  diagnostic reader of `admittedSendWr`; mark-table safety follows from this clamp alone (NOT from any
  CQ-size equality — the granted CQ depth may also exceed the request and is only logged).

## `HomerServiceSendOwnerShard` — **one dynamically allocated 256/256 owner-capacity domain per ready `CRITICAL_CONTROL` QP generation.**

- **MEANS:** the service-side lifecycle context for one exact local QP generation: 256 command owners, 256
  peer-client-completion owners, one intrusive lane/cursor/counter set per class, and exact connection identity
  (`tuple_sink_service_process.c:HomerServiceSendOwnerShard`). Multiple sessions sharing a QP share its shard;
  additional peers/directions allocate independent shards during cold ready handoff.
- **DOES NOT MEAN:** 256 entries per session, a global owner table, or a peer-count quota. The WR-ID's 16-bit index
  is interpreted only after the CQ/post path resolves the generation-bound shard from the connection handle.
- **CONTRACT / INVARIANT:** per shard,
  `linked command + completion owners <= postedSendWrs-retiredSendWrs <= admittedSendWr <= 256`. Total-unit
  preflight runs before the one transient `active && !linked` reservation, so normal table exhaustion is impossible.
  Control owners remain transport-owned and consume WQEs without consuming shard slots, which only tightens the
  inequality.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** `HomerServicePeerConnectionReady`,
  `HomerServiceResolveSendOwnerShard`, both reserve/link/sweep/validator families,
  `HomerServiceDestroySendOwnerShardAfterQpDeath`, and the transport's unified exact-QP CQ-demand index. Every
  reserve/clear/sweep/inactive-pop/zero-post-failure/QP-purge transition updates shard-local and aggregate counters
  exactly once.
- **DELIBERATELY NOT DONE:** no 4096/1024 global size-out, startup preallocation of 128 heavy shards, global quota,
  hot-path hash/linear lookup, or WR-ID ABI expansion. A ready control-only `CRITICAL_CONTROL` QP may receive an
  unused cold shard by design.

## `HomerPeerConnectionLifecycleObserver.onReady` / `lifecycleContext` — **atomic ready ownership transfer; detach before reset-complete.**

- **MEANS:** transport invokes `onReady` after bootstrap/notification RECV setup but before READY/canonical/
  `bootstrapComplete` visibility. Context-out starts NULL and is stored only after callback success; a successful
  owner-capable QP requires a complete non-NULL shard.
- **DOES NOT MEAN:** every reset has a shard. `HomerPeerConnectionIdentity.lifecycleReadyNotified == false` is an
  expected pre-ready/setup-allocation failure and reset-complete accepts NULL; a notified ready
  `CRITICAL_CONTROL` QP with NULL context is corruption.
- **CONTRACT / INVARIANT:** callback failure undoes partial allocation/list state and leaves NULL. Reset destroys
  QP/CQs, detaches the pointer so accessors fail, then passes the local pointer to reset-complete; service
  purges/rearms/unlinks/frees before any helper can reset a session or the fixed connection slot is reused.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** `TupleSinkServiceNotifyPeerConnectionReady`,
  `TupleSinkServiceFinishPeerConnectionSetup`, `TupleSinkServiceResetPeerConnectionInternal`,
  `HomerServicePeerConnectionResetComplete`, and transport destroy's active incoming/outgoing reset loops.

## `TupleSinkServicePreflightSendUnitRdma` / `...WithPressureRdma` — **non-mutating total-unit verdict; pressure adds one exact pull and one rescan.**

- **MEANS:** exact 1-or-2-WR admission using the QP's clamped outstanding arithmetic. The single-threaded caller
  must execute the frozen branch immediately with no poll/callback/alternate post/yield before its final post.
- **DOES NOT MEAN:** durable reserved-but-not-posted WQE credit. Existing per-post admission remains a backstop;
  `sendAdmissionRefusalSerial` makes a refusal after successful preflight an observable ALARM/invariant failure,
  while genuine zero-WR provider failures retain ordinary classification.
- **CONTRACT / INVARIANT:** preflight precedes CQ-owner reservation. WOULD_BLOCK is retryable local send-resource
  pressure; the pressure wrapper performs at most one exact-QP bounded pull and one second arithmetic verdict.
  It never polls between successful preflight and the final post. Body-accepted/tail-failed is non-rollbackable
  and fail-stops in this research prototype.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** command, peer-client-completion, payload batch/byte-ring, and
  receiver-head-ACK post units,
  `TupleSinkServiceAdmitSendChainRdma`, `HomerServiceFailIfSendAdmissionBackstopDisagreed`, and completion
  transport-credit blocked-session rearm from the exact QP's next successful send CQE.

## `entry->frontierMark` (command-write / completion-publish owners) — **QP-wide retirement coordinate. Class-local ordinals are GONE.**

- **MEANS:** the connection's cumulative `postedSendWrs` as of the owner's unit's LAST post (signalled only
  when the Stage-2c decision said so), stamped at the post site immediately after the unit's final post
  succeeds (`TupleSinkServicePeerConnectionPostedSendWrsRdma`). A later successful CQE whose retired
  frontier `F` reaches the mark proves this WR — and everything posted before it on the same QP —
  completed; the sweep pops per-connection post-order lanes while `mark <= F`, so retirement is
  CROSS-CLASS (a payload or control CQE retires command/completion owners on that QP for free), and an
  UNSIGNALLED owner is popped exclusively by a later checkpoint's sweep.
- **DOES NOT MEAN** a per-class sequence number (`postOrdinal` and the array lane FIFOs are deleted), and
  `0` is NOT a posted mark: it flags "reserved, unit not yet posted."
- **ENFORCES:** stamp+link ONLY after the unit's posts all succeed (command post fn, completion publish fn
  in `tuple_sink_service_process.c`); `TupleSinkServiceAccountPostedSendChainRdma` increments
  `postedSendWrs` BEFORE stamping the mark table, which is what makes the service-side stamp equal the
  signalled WR's own mark; comparisons assume the u64 frontier never wraps.

## `entry->linked` vs `entry->active` — **a wrong-neighbor pair. List membership vs accounting liveness.**

- **`linked` MEANS:** threaded on its connection's post-order sweep lane (`nextIndex` is LIVE list state).
  Reserve MUST skip `active || linked` slots — re-reserving a linked slot threads one table slot into a
  lane twice and corrupts the intrusive list. Only the sweep pop (or a stale-generation purge) unlinks.
- **`active` MEANS:** the entry still holds owner accounting. Normal live-QP reset retains both `active` and
  `linked` until a CQE proves retirement; QP death purges the shard using destruction as that proof. The only
  inactive-but-linked shape is defensive: `Clear...Index` was incorrectly called for a linked entry, so its
  guard deactivates in place, retains list state and signalled demand, and ALARMs. A later sweep or QP-death
  purge discards it. A posted-but-unlinked entry is forbidden: lane capacity is structurally at least owner
  capacity, and a post-success link failure ALARMs and fail-stops so process teardown fences the QP. This
  deliberately simple research-prototype failure policy avoids a second orphan scheduler.
- **ENFORCES:** both reserve probe loops; `Clear...Index` (linked guard: deactivate-in-place + ALARM, never
  memset); the sweep pop unlink-before-retire order (the retire memsets); the QP-death shard purge.

## The service owner signalled counts (`...SignaledCompletionActiveCount`) — **proof counters, no longer scheduler discovery.**

- **MEANS:** "signalled send CQEs not yet consumed on this service owner lane"; they remain useful for ownership
  validation and diagnostics.
- **DOES NOT MEAN:** complete QP-wide CQ demand. Control, payload, ACK, bootstrap, and blocking owners share the
  physical send CQ, so scheduler discovery keys on the transport ordinal/index contract below instead.
- **DOES NOT MEAN** "active signalled owners": the defensive inactive-but-linked shape may have a CQE still
  in flight and therefore retains demand until the sweep or QP-death purge frees its linked slot. Releasing
  demand at the linked-clear guard would starve the consumption that reclaims capacity. The per-lane
  `...SweepLaneHasSignaledOwner` predicates are deliberately NOT gated on `active`.
- **ENFORCES:** release points are exactly: `Clear...Index` UNLINKED path (post-failure, or retire after a
  sweep pop of an ACTIVE entry), the command retire's inline decrement, the sweep-pop/purge INACTIVE arms
  (via `TupleSinkServiceRelease...SignaledDemand`), and the QP-death shard purge. The `Clear...Index` LINKED
  guard deliberately does not touch it.

## `resetOwnedSessions` / `resetRunnableSessions` — **proof-backed indexed session-reset continuation.**

- **MEANS:** owned says destructive reset is still owed; runnable says all command/completion/partial-post owners
  reached zero or exact QP destruction supplied physical retirement proof. `RETRY_SESSION_RESET` is the only
  scheduler action that consumes runnable reset work.
- **DOES NOT MEAN:** one retired owner is enough, nor may a live-QP timeout abandon owners/MRs. Nonconvergence
  retains the session, sources, and exact `{handle,generation}` latch; send-CQ callbacks only arm readiness.
- **CONTRACT / INVARIANT:** QP-death purge/rearm precedes reset-complete helpers that may call `ResetSession`.
  Shutdown performs initial reset -> transport destroy -> final reset continuation -> zero-state assertions ->
  control unmap. Completion publication blocked on transport credit similarly waits on an indexed bitmap and is
  rearmed only by a successful send CQE on its exact QP generation; exact QP death instead cancels the staged
  publication before session reset classification, never retries it against the dead generation.
- **ENFORCES:** `TupleSinkServiceRetireSessionSendCompletions`,
  `HomerServiceRearmPendingResetIfOwnerProofComplete`, `HOMER_PROGRESS_ACTION_RETRY_SESSION_RESET`,
  `HomerServiceRearmCompletionTransportCreditForConnection`,
  `HomerServiceCancelCompletionTransportCreditForDeadConnection`, reset-complete, and service shutdown.

## The shard's `identity.connectionGeneration` — **bind-time identity; QP-death callback owns purge.**

- **MEANS:** the exact connection incarnation under which every owner in the shard was reserved and posted.
  Outgoing and incoming session bindings both snapshot generation; no post path rereads a recyclable handle's
  current generation as proof.
- **DOES NOT MEAN:** stale-generation lazy reclamation. The shard is detached only after QP/CQ destruction, then
  reset-complete purges active/inactive linked owners, releases retained demand/accounting, rearms exact pending
  resets, and frees the whole shard before slot reuse.
- **ENFORCES:** generation-checked lifecycle-context lookup at every reserve/link/sweep/validator path;
  `HomerServiceDestroySendOwnerShardAfterQpDeath`; outgoing bind beside `clientSqlPeerConnectionHandle`; reset
  continuation's exact `{handle,generation}` latch.

## `sendCheckpointSweep` / `TupleSinkServiceHandleTaggedSendCompletion` — **every successful CQE retires its QP frontier; service-owner sweep is only for owner-bearing QPs. NO raw consumers.**

- **MEANS:** the scalar retire → control-FIFO sweep → `sendCheckpointSweep` sequence runs inside the
  dispatch's SUCCESS block for EVERY class including UNTAGGED; the class-typed callbacks afterwards are
  VALIDATORS (their contract comment in `remote_execution_peer_transport_rdma.h`), best-effort healing only
  a surviving linked owner / frontier-skew anomaly. `sendCheckpointSweep` must return immediately for every
  non-`CRITICAL_CONTROL` traffic-class QP: those QPs correctly carry NULL service lifecycle context and have no
  command/completion shard. On an owner-bearing `CRITICAL_CONTROL` QP it sweeps both service lanes through the
  retired frontier. Post-success ownership insertion failures fail-stop.
- **DOES NOT MEAN** "every traffic-class QP has a service shard." The transport callback is universal across
  QPs; the service owner domain is not. Resolving a service shard before the traffic-class guard turns the
  expected NULL context of a payload QP into a false generation/context ALARM (caught by the Stage-2b debug gate).
- **DOES NOT MEAN** the bootstrap poll is exempt: `TupleSinkServicePollBootstrapSendCompletion` consumes an
  accounted-signalled CQE and therefore RETIRES (validated against the stashed
  `bootstrapSendWorkRequestId`). Before that fix the mark pairing was shifted by one for the QP's life and
  the sweep would strand the LAST owner of every burst — `SESSION_CLOSE`'s included (D-S2.3; the reason the
  bootstrap fix moved from 2b into 2a).
- **ENFORCES:** the success block in `HandleTaggedSendCompletion`; the traffic-class guard at the top of
  `HomerServiceHandleSendCheckpointSweepFromDrain`; both drain paths reject errored CQEs before dispatch; the
  D-S0 choke point guarantees no NULL-callback consumer; the send-CQ poll inventory is
  exactly three (bootstrap poll, blocking waiter, drain loop) — a NEW `ibv_poll_cq` on a send CQ must
  either dispatch through `HandleTaggedSendCompletion` or retire explicitly, else owners leak and the
  validators ALARM.

## `connectionState->controlSendOwners[]` — **the control class's post-order sweep FIFO. Appended ONLY by `TupleSinkServicePublishControlMessage`.**

- **MEANS:** transport-internal ring of in-flight CONTROL sends — signalled or, since Stage 2c, mostly
  unsignalled — `{responsePublish, slotIndex, slotGeneration(0 for responses), frontierMark}`, swept by
  `TupleSinkServiceSweepControlSendOwners` on every successful CQE; release runs the decode-free core
  `TupleSinkServiceReleaseControlSendOwner` (old per-CQE retire semantics preserved: response slot
  `inUse=false`; request `sendCompletionRetired` + conditional op release). Its population
  (`controlSendOwnerCount`) is the RESPONSE-side pool-pressure term of the control signalling decision.
- **CONTRACT / INVARIANT:** each FIFO entry records the actual `signalled` post decision.
  `controlSignalledSendOwnerCount` increments only after that exact successfully posted entry is appended and
  decrements only when it is swept; it is always `<= controlSendOwnerCount`. It is a control-specific proof
  counter, not the generic pressure authorization predicate.
- **DOES NOT MEAN** a service-visible structure, and NOT the request-op pool term — an op slot OUTLIVES its
  send owner (WAIT_RESPONSE after the owner swept; RETIRING after the response was consumed first), so
  request pressure reads `outstandingControlOps` (exact op-slot occupancy: ++ at reserve, -- only in
  `TupleSinkServiceReleasePeerControlOp`). Confusing those two produced a real reviewed-out wedge: a table
  of 33 WAIT_RESPONSE + 31 RETIRING ops with an empty-enough FIFO forces reservation failure below every
  signalling threshold. The CONTROL dispatch arm does NOT normally release: it validates that the entry is
  gone after its own CQE's sweep and best-effort sweeps a surviving linked entry. There is no dropped-entry
  slot-state fallback.
- **ENFORCES:** the append after the post in `PublishControlMessage` (all three control encode sites funnel
  there — the WR-ID decode now runs BEFORE the post; decode failure ALARMS with the `control_wr_id_refused`
  stable event and FAIL-STOPS, because it is structurally impossible and the one-shot publication retry would
  otherwise re-enter the site every scheduler pass forever); the connection-reset memset zeroes it with the
  accounting; depth = op+response slot counts, so overflow is structurally impossible. Overflow after a
  successful post still ALARMS and fail-stops; process exit destroys the QP rather than continuing with an
  untracked live source.

## `sendCqDemandIndexed` / transport send-CQ demand index — **QP-wide exact demand, separate from control readiness.**

- **MEANS:** `signalledPostOrdinal-signalledRetiredOrdinal != 0` for one exact ready QP generation. The transport
  arms one direction/traffic-class bit when the delta transitions 0→1 and clears it on 1→0; the global count is
  the O(1) scheduler readiness predicate. The indexed action carries generation, direction, class, and connection
  index; execution resolves that identity back to the current table handle and revalidates it immediately before
  polling.
- **DOES NOT MEAN:** control-mailbox readiness, a service owner-shard member, or proof that a CQE is already
  available. The index is independent of `HomerControlReadyIndex`; command, completion, control, payload, and ACK
  checkpoints on every traffic class all share this one demand domain.
- **CONTRACT / INVARIANT:** every normal mutation changes the per-QP mirror bit, direction/class bitset, and
  aggregate count together. Common post
  accounting arms the bit before returning but never polls; common scalar retirement clears it, including
  bootstrap and blocking waiters. Reset/destroy clears the live bit before connection memset. Enumeration retains
  one flat cursor so a 64-action page cannot starve a stable suffix or one owner family. Its bounded consistency
  fence fail-stops when the aggregate count is nonzero, the caller has action capacity, and a complete flat-index
  scan emits no action: that proves the scheduler-visible count has no enumerable bit. This is deliberately NOT a
  hot-path full-popcount reconciliation and does not claim to detect arbitrary memory corruption.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** `TupleSinkServiceAccountPostedSendChainRdma`, common successful-CQE
  scalar retirement, connection reset/destruction, `TupleSinkServicePendingSendCqConnectionCountRdma`,
  `TupleSinkServiceAppendPendingSendCqActionsRdma`, and exact action validation/drain.

## `TupleSinkServicePullPeerSendCqRdma(reason)` — **one canonical bounded exact-QP pull after commit or under pressure.**

- **MEANS:** typed owner-dispatch on one generation-checked QP using the persistent RETIRE-ONLY callbacks.
  `POST_CHECKPOINT` asks `ibv_poll_cq` for at most one CQE; `PRESSURE` and scheduled actions request at most one
  bounded batch. The canonical drain caps the actual poll request to remaining `maxCqes`; it never dequeues a
  larger array and silently discards the suffix.
- **DOES NOT MEAN:** a post retry, a polling admission predicate, or permission to poll before semantic owner
  installation. A nonzero ordinal delta authorizes one useful attempt but does not prove CQ arrival or that the
  particular source/owner pool will free.
- **CONTRACT / INVARIANT:** post-checkpoint hooks run only at the semantic commit tail: command after sweep link,
  outstanding/pending/accepted/terminal/stats bookkeeping; peer completion after owner link, READY_POSTED and
  published-state bookkeeping; control only at the three outer request/sync/deferred tails; payload after owner
  POSTED plus all frontier/progress writes; ACK after owner POSTED plus token/tail/progress writes. A post-success
  pull failure is not retryable and crosses the reset/fail-stop error boundary. A stream/session-local SEND_CQ
  grant uses `SCHEDULED` on only that captured connection/generation; local source/owner pressure uses `PRESSURE`
  on the same exact QP, performs at most one pull and one rescan, then returns blocked/still-ready when unchanged.
  Only proof-backed session reset convergence may repeat exact pulls, and its wall-clock deadline remains mandatory.
- **CONTRACT / INVARIANT — diagnostic correlation:** an empty POST latch belongs to one generation and signalled
  ordinal. A later scheduled/pressure pull may log and clear it only when generation still matches and
  `signalledRetiredOrdinal >= emptyPostCheckpointOrdinal`; productive earlier CQEs leave it armed. A productive
  POST that covers the ordinal clears it without falsely attributing the coverage to another reason.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** the command/completion/payload/ACK semantic tails in
  `tuple_sink_service_process.c`; the three outer control tails and control-op/response-slot pressure sites in
  `remote_execution_peer_transport_rdma.c`; all pressure-aware send-unit preflights.

## `HOMER_PROGRESS_COLLECTOR_COMMAND_SEND_CQ` — **typed exact-QP drain across every owner and traffic class; the name is historical.**

- **MEANS:** scheduler fallback for the transport-global exact demand index. Readiness is the O(1) pending-QP
  count; execution enumerates fair generation-bearing actions and drains those exact QPs with typed callbacks.
  The machine-baseline grant carries `maxItems=1,maxPolls=1`; the executor preserves that outer grant instead of
  rebuilding a 64-action plan. The legacy/global fallback likewise requests one cursor item per pass. Readiness
  has explicit phase ownership: only the machine COLLECTOR phase mutates the machine-baseline cooldown and can
  grant this collector; SEMANTIC observes raw pending count only as its scan trigger; RESET and LOCAL_IPC ignore
  send-CQ demand; and the non-machine fallback samples its separate cooldown exactly once per fallback pass.
- **DOES NOT MEAN:** command owners only, `CRITICAL_CONTROL` only, or the deleted broad peer-control CQ scan.
  `HOMER_PROGRESS_WAIT_PEER_SEND_CQ` maps here for exact send-CQE liveness, while the surviving
  `HOMER_PROGRESS_SOURCE_PEER_SEND_CQ` belongs only to the two explicitly indexed DPU egress collectors.
- **ENFORCES:** the policy cooldown remains armed until the global/per-QP checkpoint delta reaches zero. Coarse
  fallback construction uses `peerTransportState` as both the transport-global lookup and CQ source owner; an
  uninitialized session registry must not substitute `sessionStates` for that identity. The
  deleted collector/action tokens and `TUPLE_SINK_SERVICE_PEER_PUMP_PHASE_SEND_CQ` must remain textually absent;
  a zero/ALL peer-pump phase mask never polls a send CQ.

## FB-2 peer-send source/feedback slots — **exact cardinality two and one canonical bijection.**

- **MEANS:** `DPU_PEER_COMMAND_EGRESS -> 0` and `DPU_PEER_COMPLETION_EGRESS -> 1`; every other collector maps to
  NONE. Registry state stores two independently indexed source cores and two independent feedback slots.
- **DOES NOT MEAN:** one scalar peer-send source/feedback object shared by both collectors. Source stamps,
  feedback writer attribution, and feedback reader resolution all derive their slot from the same canonical map.
- **CONTRACT / INVARIANT:** registry initialization validates range, injectivity, exact count two, and full slot
  coverage. Both egress executors require the mapped source index and the DPU scheduler-state owner before doing
  work. Per-slot writer ownership rejects a source stamped by one egress collector updating the other's feedback.
  The starvation diagnostic legend therefore names each slot separately and contains no `+` alias.
- **ENFORCES:** `HomerServicePeerSendCqFeedbackSlotForCollector`,
  `HomerServiceValidatePeerSendCqFeedbackSlotMap`, registry initialization,
  collector-to-source resolution, source-id core/feedback writers, and collector feedback readers.

## `connectionState->pendingSyncResponse*` — **the ONE-SHOT synchronous response continuation. NOT the deferred-response ticket.**

- **MEANS:** a latched "the request handler for the CURRENT uncommitted mailbox head already ran; the
  response PUBLICATION is still owed from its reserved slot (which stays `inUse` for the latch's whole
  life)." The next drain of the SAME message resumes at `TupleSinkServiceFinishSyncResponsePublication`,
  never at the handler. Only publish WOULD_BLOCK retries toward success; a non-retryable publish failure
  latches AND requests a connection reset (the latch only blocks handler replay until the reset destroys
  mailbox + latch together); a ticket-handler failure after a successful publish FAIL-STOPS
  (`response_post_ticket_failed` — every failure mode is a permanent invariant violation with the response
  already on the wire), so no post-publish latch state exists.
- **DOES NOT MEAN** `HomerPeerDeferredResponseTicket` — that is a HANDLER'S choice to answer later from
  service-owned state, releases the pre-reserved slot immediately, and posts through
  `TupleSinkServicePostDeferredPeerResponseRdma`; the latch is the TRANSPORT's involuntary continuation for
  publication backpressure (ring full, or Stage 2c admission refusing an unsignalled response the reserve
  WQE). It also does NOT mean the mailbox message was consumed: the message stays uncommitted, which is
  exactly what re-presents it.
- **ENFORCES:** `TupleSinkServiceProcessIncomingMailboxRequest` validates the latch BEFORE
  reserving/dispatching — sequence mismatch, slot index out of range, or a latched slot not `inUse` is a
  structurally impossible state and FAIL-STOPS (`sync_response_latch_mismatch`; dropping the latch would
  replay handler side effects or abandon an owed response). Writers are exactly
  `TupleSinkServiceFinishSyncResponsePublication` (latch/clear) and the connection-reset memset. Commit
  failure after a fully processed REQUEST fail-stops (`control_commit_failed`) — a replay there would have no
  latch left to stop it — and so does commit failure after a consumed RESPONSE (`arm=response`):
  `TupleSinkServiceCompletePeerControlOp` irreversibly moves the op out of WAIT_RESPONSE before commit, so a
  replayed response could never match again and would wedge the mailbox head. The drain also applies the
  piggybacked `senderConsumedHead` credit AT DISPATCH (not only at commit) — the latch's ring-full retry
  deadlocked otherwise, because the credit that would un-fill the ring rode on the very message that could
  not commit — and BOUNDS it first: `senderConsumedHead` must not exceed `outgoingControlPublishedTail`
  (the peer cannot have consumed more than we published; a larger value would poison the monotonic mirror
  and wrap the unsigned occupancy into a permanently-full ring — `control_credit_rejected` + connection
  reset). `FinishSyncResponsePublication` short-circuits untouched whenever `resetRequested/resetInProgress`
  is up: `PublishControlMessage` mutates the message source before its own reset cutoff, so a latched retry
  after a PARTIAL post must never reach it; the reset memset owns latch and slot. The reason this machinery
  exists: re-running a request handler duplicates non-idempotent side effects (OPEN allocating session
  state) — Stage 2c review round 2 proved selective signalling made that window real.

---

## `HOMER_PAYLOAD_CLOSE_FLAG_ABORT_RESET` — **staged-discard terminal flush on a SURVIVING connection. NOT the ABORTING failure machinery.**

- **MEANS:** the sender is closing WITHOUT a drain contract (backend ERROR/cancel, cleanup without
  OBJECT_END). Receiver: stop relay submissions, wait out the in-flight DOCA chain, publish the **FAILED**
  credit-line terminal at the stopped frontier, respond with the ABORT_RESET echo (`peerBindingStillActive
  =0`), tombstone `ABORTED_RESET`. Exactly one of {`NORMAL_DRAIN`, `ABORT_RESET`} per request.
- **DOES NOT MEAN** `QUIESCED` (drained-to-final-tail + final head posted — an abort can never claim it;
  the response shapes are mutually exclusive and separately latched). Does NOT mean
  `HOMER_PAYLOAD_CLOSE_ABORTING` / `HomerPayloadFailureState` — that is the CONNECTION-RESET machinery,
  whose clear-proof requires QP destruction; ABORT_RESET completes with the QP alive.
- **ONLY `BASE_BACKUP_STREAM`**: the tuple-view relay has no discard implementation (its terminal is
  gated on in-band EOS); the accept arm rejects other families loudly rather than wedge unscheduled.
- **ENFORCES:** accept arm + family gate in `TupleSinkServiceHandlePeerCloseSinkRequest`; the exact-shape
  tombstone arm in `HomerServiceTryBuildRetiredCloseResponse` (`closeFlags == ABORT_RESET`, because the
  tombstone check runs BEFORE the live flag-shape validation); the abort echo arm in
  `HomerServiceValidatePayloadCloseResponse`; the abort fact-arms in
  `HomerServiceMarkPayloadCloseReclaimable` (exit(1) on unproven facts — never satisfy them by faking
  `peerQuiesced`/`finalHeadObserved`).

## `abortLocalSendDiscard` vs `abortResetDiscardActive` — **a wrong-neighbor pair. Sender pump gate vs receiver relay gate.**

- **`abortLocalSendDiscard` MEANS (SENDER):** the local client abort-closed; the outgoing payload pump
  (`HomerServicePumpOutgoingPayloadStream` — verified the SINGLE posting funnel) posts nothing further, so
  the posted tail is FROZEN. **ORDER IS LOAD-BEARING:** it must be set BEFORE the close continuation
  freezes the final tail (`HomerServiceFreezePayloadCloseFinalTail` exit(1)s on a moved tail);
  `TupleSinkServiceBeginPayloadAbortReset` owns that order — always go through it.
- **`abortResetDiscardActive` MEANS (RECEIVER):** the peer's CLOSE_SINK carried ABORT_RESET; the relay
  pump submits no new DOCA segments and publishes FAILED once the in-flight chain retires. Landed bytes
  beyond the stopped frontier are deliberately discarded, so `writtenTail < publishedTail` can hold
  FOREVER — every drain-shaped predicate needs an abort arm (see the scheduler-trap entry below).
- **DOES NOT MEAN** each other's side, and neither means `closeState->abortReset` (the close MODE flag,
  set on both sides). Both are latches; the stream-entry reset clears them.

## The abort scheduler rule — **a terminal path must be walked through ready-predicate → grant → executor, not just its own state machine**

- Three predicates assumed drain progress an abort explicitly abandons, and each was a silent
  forever-wedge: `HomerServicePayloadCloseActionReady` (receive-drain gate; abort arm returns
  `relayTerminalPublished`), `HomerServicePayloadMustProgressBeforeClose` (early-false under either abort
  flag), and the relay pump wake-up (graceful rides the final payload doorbell; the abort accept arm
  latches a SYNTHETIC `receivePayloadDoorbellPending`).
- **ENFORCES:** any future terminal/close variant (Stage 2's checkpoint flush included) must audit these
  three sites plus `HomerServicePayloadStreamSourceReadyForScheduler`.

## The SENDER failure channel — **`byteRingFailedCreditArmed/Published` + the credit line's `entryState`. The flags channel is DEAD for selected-DPU.**

- **MEANS:** a selected-DPU SENDER learns of stream death ONLY through its credit line: the DPU arms a
  one-shot FAILED credit terminal (`HomerDpuDmaArmFailedCreditForServiceSink`, ring resolved by the
  RUNTIME binding — D5) on abort/reset teardown; the consumed-head publish sweep publishes it (equal or
  ZERO frontier allowed — the stuck/never-credited abort has no new credit); the host's
  `StillOpenForReserve` reads `entryState==FAILED` off the line `HomerClientDpuRefreshBaseBackupCredit`
  already polls every reserve iteration.
- **DOES NOT MEAN** the `byteRingControl->flags`/`terminalStatus` channel: that one is HOST-SERVICE-era —
  `MarkPayloadStreamSendFailed` writes local queue controls a selected-DPU sender never maps; nothing
  DPU-side writes the host's flags. Validation round 1 watched a walsender spin at 99% CPU >138 s on
  exactly that assumption. Do not confuse this SENDER channel with the RECEIVER's FAILED terminal
  (`SubmitByteRingWriteTerminal`, role-7 ring, entry above) — same entryState value, different ring
  direction, different publisher family (`CREDIT_BODY/PUBLISH` vs `WRITE_PRODUCED_TAIL_*`).
- **ONE-SHOT + SUPPRESSION:** `byteRingFailedCreditPublished` permanently suppresses later ACTIVE
  publications (an ACTIVE re-stamp would erase the terminal the producer's exit decision reads).
- **ENFORCES:** `HomerDpuDmaSubmitByteRingConsumedHeadPublication` (renamed from the LYING
  `...SmokePublication` name — it was always the production Loop-1 submitter): ACTIVE keeps
  strict-advance + nonzero; FAILED allows equal/zero. The sweep's FAILED eligibility arm; the ordinary
  arm's `byteRingFailedCreditPublished` refusal; `HomerServiceArmSenderFailedCredit`'s two call sites
  (`BeginPayloadAbortReset`, reset-complete sender real-failure arm).

## `HomerClientPollBaseBackupReceive` terminal split — **FAILED is drain-INDEPENDENT and a poll FAILURE; CLOSED is drain-gated SUCCESS**

- **MEANS:** `entryState==FAILED` (honored only once `dpuLastCreditEpoch != 0`) returns false with a
  distinct truncation error — even with a torn tail holding `consumedHead < producedTail` forever.
  `CLOSED` still requires the full drain before `*streamComplete`.
- **DOES NOT MEAN** both states exit alike: before Stage 1 they both exited SUCCESS, which reported
  aborted backups as good (the silent-truncation bug). Never re-merge them.
- **ENFORCES:** the FAILED arm in `HomerClientPollBaseBackupReceive`;
  `HomerDpuDmaSubmitByteRingWriteTerminal` accepts ONLY CLOSED/FAILED (in-function guard).

---

## HOST-CLIENT SQL session (Stage-3 Push-1). Paths below are `citus-dbcomm/src/bin/` (NOT the service dir above).

## `HomerSqlSession` — OPAQUE, HEAP-OWNED SQL session handle (`homer_session_core.h`); the A-state core lives in `homer_session_core.c`

- **MEANS:** an incomplete-typed handle to the private `struct HomerSession` (the SQL session state machine: command
  sequence + one-outstanding-START, the completion peek/apply/ack lease + terminal latch, retained intent/sessionUID,
  and the result payload decode). `HomerClientOpenSqlSessionSelectedDpu` **RETURNS** it (heap-allocated); Close **frees** it.
- **DOES NOT MEAN** the old by-value `HomerClientSession` struct (RETIRED — removed from `remote_execution_client.h`).
  Callers cannot size or stack-allocate it, and Open no longer fills a caller-provided struct. pgbench holds
  `HomerSqlSession *` (`pgbench.c:712`), opens via the returned handle, and sets it NULL after Close.
- **DOES NOT MEAN** POSTGRES/DOCA-dependent: `homer_session_core.c` compiles POSTGRES- and DOCA-independent (stdint/
  stdbool + ABI headers only) and is a SECOND object in `libhomer_client.a` — it links into BOTH the client and the
  backend (dead code until Stage 4). DOCA handles stay `void *` in the embedded carrier.
- **ENFORCES:** the opaque type is declared once (guarded `HOMER_SQL_SESSION_HANDLE_DECLARED` in both
  `homer_session_core.h` and `remote_execution_client.h`). Field partition: A-state → `struct HomerSession`;
  transport carrier (`HomerClientBaseBackupStream`) is EMBEDDED as its substrate.

## `HomerSqlTransport*` ops take an EXPLICIT `serviceSessionId` — **NEVER read `carrier->serviceSessionId` for a SQL session**

- **MEANS:** the B-side transport ops the core calls (`HomerSqlTransportPublishStart` / `PollCompletion` /
  `LifecycleClose`, `homer_client.c`) receive the session identity as an explicit scalar the core passes
  (`core->serviceSessionId`).
- **DOES NOT MEAN** the carrier holds the SQL identity: the SQL open path deliberately leaves
  `carrier->serviceSessionId == 0` — the identity is adopted into the core, not the carrier. (basebackup, sharing the
  same carrier struct, DOES set `carrier->serviceSessionId = dpuBridgeGeneration`.) A transport op that read the
  carrier field would compare every SQL completion against 0 — a wrong-neighbor bug the explicit-arg contract prevents.
- **ENFORCES:** every identity-needing `HomerSqlTransport*` body uses its `serviceSessionId` parameter; `PollCompletion`'s
  role-6 identity check keys on it.

## `struct HomerSession.receiveExpectedCommandSequence` (core, A) vs `carrier->receiveExpectedOrdinal` (carrier, B) — the result-decode A/B split

- **MEANS:** the 1-based command-local sequence is owned by the CORE (reset to 1 at each in-band EOS by the core's
  ConsumeResult); the 0-based transport recordOrdinal is owned by the CARRIER/transport (advanced/reset by the
  `AcceptAndRelease` op). Accept order is A-command-seq FIRST, then B-ordinal, then credit release.
- **DOES NOT MEAN** both live in the carrier: `receiveExpectedCommandSequence` was **MOVED OUT** of
  `HomerClientBaseBackupStream` into the core (it was SQL-result-only; basebackup never read it). `receiveExpectedOrdinal`
  STAYS in the carrier (basebackup shares it for wrap-gap detection).
- **ENFORCES:** the core decode reads `core->receiveExpectedCommandSequence`; `HomerSqlTransportResultAcceptAndRelease`
  is the only writer of `carrier->receiveExpectedOrdinal` on the SQL path.

---

## Related

- [`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) — the narrative and evidence
  behind every entry here (§34–§45, D-S0 §50, Stage-1 abort §51), including **six refuted mechanisms** and why each was tempting.
- [`selected_dpu_session_rings_and_lifecycle.md`](selected_dpu_session_rings_and_lifecycle.md) — the selected-DPU
  architecture of record.
- `docs/kb/operations/farnet_operational_hazards.md` — the **operational** traps. *This file is the **code** traps.*
