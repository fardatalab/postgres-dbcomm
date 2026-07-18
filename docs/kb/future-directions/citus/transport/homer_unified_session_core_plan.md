<!-- kb-summary: Design plan to merge the divergent Homer session abstractions (backend RemoteExecutionSession + frontend HomerClientSession + basebackup) into ONE intent-driven frontend-safe portable core with typed per-kind handles (compile-time op legality) over a link-time transport module seam, on the kept selected-DPU transport. Authoritative plan = §12 (build) + §13 (interface); §1–§11 are design evolution. Gates S7.2. -->

# Homer unified session core — merging RemoteExecutionSession (①) and HomerClientSession (②)

> ## ⚠ STATUS — read first
>
> **AUTHORITATIVE CURRENT PLAN = §12 (build plan) + §13 (the unified interface).** §1–§11 record the design's
> *evolution* (problem → Approach A → the audit + three review rounds that refined it); read them for rationale and
> rejected approaches, not as the current spec. In particular, where §1–§11 say **"vtable"** that was the earlier
> dispatch mechanism — the current decision is a **link-time module seam (core↔transport) + typed opaque handles
> (core↔caller, compile-time op legality)**, vtable deferred to COPY (§12.1, §13.1).
>
> **EXECUTION: Stage 1 DONE + validated + committed** (frontend-safe `homer_session_spec.h`; citus `5cadf1c9c`+
> `265b6630d`, postgres `dc750a261b2`). **Next: Stage 2** (in-`homer_client.c` SQL transport facade). Full staging
> in §12.4/§12.5/§12.5b: Stage 2 facade → Stage 3 intent-driven core (SQL) → **Stage 3.5 basebackup migration
> (multi-consumer proof, before retirement)** → Stage 4 final retirement → Stage 5 COPY (deferred).
>
> **DESIGN NOTE (opened 2026-07-18).** This gates **S7.2** in
> [`../../../implementations/citus/transport/s7_host_service_retirement_plan.md`](../../../implementations/citus/transport/s7_host_service_retirement_plan.md):
> S7.2 was about to *delete* the `RemoteExecutionSession` frontend API (its last consumer, the UDF
> `citus_remote_exec_pgbench_transaction`, is retiring). The owner **paused S7.2** because that API is the
> *cleaner, intent-driven* design and deleting it would discard design capital the future backend-to-backend
> COPY re-plumb needs. Decision: instead of deleting ①, **promote its design into a single frontend-safe core
> that ② also uses.**
>
> **Owner COMMITTED to Approach A (true single core), and to BUILDING IT NOW** (2026-07-18): execute Stages 1–4
> (§11.3); **Stage 5 (COPY producer / backend pooling / reuse activation) is DEFERRED** until the COPY path is
> actually (re)designed — endpoint of this work = *host-service retirement complete* (end of Stage 4). The two
> previously-deferred design questions are settled: (1) completion-ownership = **lease + terminal-abandon**
> ("backend error ⇒ session terminal", §11.1); (2) reuse = **carry the intent seam, build no reuse now**
> (§11.2/§11.6). All three adversarial-review rounds are complete and their corrections verified in code (§9/§11).
> See §11.7 for the execution-entry decision record and the Stage-1 grounding.
>
> **Verified at citus `e9dd4676c` / postgres `6f969fd0dfd`** (branch `homer-dpu-migration`). The frontend-safety
> audit (§3) and the "sole-caller"/dead-surface facts (§1) were read in code by the main agent, not inherited.
>
> **Relationship to existing docs (this doc does NOT duplicate them):**
> - Realizes the abstraction in
>   [`../connection-management/remote_execution_session_control_plane.md`](../connection-management/remote_execution_session_control_plane.md)
>   (the *WHAT* — intent vs operation split, four semantic facets). ⚠ That doc still cites the pre-refactor
>   `remote_execution_session.h/.c`; the code has since moved to `homer_frontend.c` / `homer_citus_policy.c` /
>   `homer_frontend.h` — treat its file pointers as stale, its design as canonical.
> - Extends [`homer_frontend_service_separation_plan.md`](homer_frontend_service_separation_plan.md)
>   (the *HOW* — frontend as a separately-linkable component + fixed-width ABI headers). That plan deferred a
>   `HomerFrontendTransportOps` vtable "until SHM and DMA need to coexist"; unification is precisely when the
>   vtable becomes necessary (host-SHM + selected-DPU + a future COPY producer all behind one core).
> - Keeps the typed command/completion plane separate per
>   [`../data-movement/command_dispatch_completion_plane.md`](../data-movement/command_dispatch_completion_plane.md).
> - Session identity / peer-pairing decisions interact with
>   [`session_identity_and_pairing.md`](session_identity_and_pairing.md).

---

## 1. The problem: two divergent session abstractions

There are two parallel "Homer session" types that grew for different consumers:

- **① `RemoteExecutionSession` (backend)** — `struct RemoteExecutionSessionData`
  (`citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_internal.h:25`), API in
  `homer_frontend.c` + spec initializers in `homer_citus_policy.c`, decls `homer_frontend.h`. **Intent-driven**:
  carries `sessionIntentSpec` (session compatibility/reuse domain) + `operationSpec` (tuple-flow bootstrap).
  Clean 3-step result consumer (`PollRemoteExecutionCommandResultBatch` → `BorrowNextRemoteTupleView` →
  `ReleaseBorrowedRemoteTupleView`/`ReleaseRemoteRecvBatch`, `homer_frontend.c:852`/`:1492`/`:1523`/`:1563`).
  Dispatches through a lower "local service" layer (`homer_frontend_control.c` `*ThroughLocalService`) to
  host-SHM `CitusTupleSink` OR Tier-2 DMA (`homer_frontend_dma.c`). **Sole surviving external consumer: the UDF**
  `worker/homer/remote_exec_pgbench_transaction.c` (verified — every one of ①'s API functions, plus the
  result/recv/borrow APIs and the `homer_citus_policy.c` spec initializers, is called only by that UDF; the one
  apparent exception, `PollRemoteRecvBatch`, appears outside the UDF only in a doc comment). COPY was ①'s other
  consumer and is retired/broken. There is also **dormant COPY-sender residue** in `homer_frontend.c`
  (`TryReserveRemoteSendBatch` `:1172`, statics `WaitForRemoteSendBatchReserve` `:999` /
  `RemoteExecutionSessionCurrentCopyCommandFailed` `:965`, `FlushRemoteSendStream`, etc.) with **zero callers
  outside `homer_frontend.c`** — e-COPY (S7.1) removed the COPY *drivers* but left the sender API.

- **② `HomerClientSession` (frontend)** — `remote_execution_client.h:187`, API = the `HomerClient*` functions in
  `src/bin/homer_client.c` (the client-binary library for pgbench + pg_basebackup). Its header states it
  *deliberately avoids backend-only APIs (no palloc/ereport)* (`homer_client.c:9`). Drives the **selected-DPU
  byte-ring** directly (role-1 control, role-6 completion, role-7 result) via the byte-ring sink core
  (`homer_byte_ring_sink.h`) + `CitusTupleViewContract`. **Fused** result consumer: one
  `HomerClientPollSqlResultDpuReceive` call (`homer_client.c:3544`) that polls+decodes+releases and returns a
  scalar + rowcount + EOS. Consumers: the pgbench gate + pg_basebackup.

**Why they diverged** (verified):
1. **Backend↔frontend linkage wall.** `homer_frontend.c` uses backend idioms (91 `palloc`/`ereport`/`elog`/
   `TupleTableSlot` sites). `homer_client.c` is a `src/bin/` frontend library (0 such). A frontend binary can
   *never* link ①; a backend *can* link frontend-safe code. **⚠ This is why the client can't use ① today — but
   it is NOT a hard architectural blocker** (see §3): the deps are cosmetic + adapter-shaped, not protocol.
2. **Transport topology.** ① assumes a co-located host-SHM `CitusTupleSink`; ② is on the far side of a cross-node
   DPU byte-ring relay with credit-line back-pressure. "Poll a local tuple sink" is not expressible on the client.
3. **Expedient fusing.** ②'s single-call decoder was tuned for the gate's minimal need (extract one `abalance`),
   collapsing ①'s clean 3-step consumer. This one is a *choice*, not a constraint.

S6 (selected-DPU migration) built the gate on **②** and bypassed **①**. So today the *cleaner* abstraction has
one retiring user, over a retiring transport, unlinkable by the surviving clients — which is why S7.2 pointed at
deleting it, and why that felt wrong.

---

## 2. Approach A — one frontend-safe core + transport vtable + thin adapters

```
                 ┌─────────────────────────────────────────────────────────┐
   backend       │  PORTABLE FRONTEND-SAFE CORE  (linkable by both)         │   frontend
   consumers ──▶ │  • session / command / result STATE MACHINES            │ ◀── consumers
  (UDF today;    │  • fixed-width specs: intent, operation, command         │  (pgbench,
   COPY re-plumb)│  • RemoteTupleView {Datum*,bool*,count}  (duck-typed)    │   pg_basebackup)
        │        │  • completion model (ONE — see §5.1)                     │        │
        │        └───────────────────────┬─────────────────────────────────┘        │
        ▼                                 ▼ transport vtable                          ▼
  BACKEND ADAPTER               HomerFrontendTransportOps                    FRONTEND ADAPTER
  • snapshot live PG state      open/close · start · completion             • explicit options
    into fixed-width specs        peek/ack · reserve/publish ·              • caller-owned memory
    (homer_citus_policy.c)        result borrow/release · terminal         • error-string returns
  • memory context + PG_TRY    ┌──────────┬───────────┬──────────────┐     (homer_client.c today)
  • ereport / TupleDesc bind   │ host-SHM │  Tier-2   │ selected-DPU │
                               │ (retire) │  (retire) │   (KEPT)     │
                               └──────────┴───────────┴──────────────┘
```

The core holds everything that is already fixed-width and transport-agnostic. Each transport is a vtable impl.
Backend and frontend differ only in a thin adapter (state acquisition, memory/error idiom, tuple-view binding).
`homer_client.c` is *already* the frontend adapter — it just doesn't yet sit on a shared core.

---

## 3. Frontend-safety audit — Approach A is sound (VERIFIED)

The load-bearing claim ("nothing in ① is a hard frontend-safety blocker") **survives, but qualified** (see §9
review): there ARE hard backend deps, all adapter-shaped, none in the session *protocol*. Negative hunt over the
session protocol (`homer_frontend.c` + `homer_citus_policy.c`): **no SPI, no fmgr/type-cache/output-function, no
relation open, no `CurrentMemoryContext`.** ⚠ **CORRECTION (review):** the "no catalog lookup" was scoped too
narrowly — the ①-open path calls `LookupNodeByNodeIdOrError()` via `BuildPeerEndpointFromIntent()`
(`homer_frontend_control.c:414`, used `:727`/`:817`). It is still adapter-shaped (it immediately snapshots node
id/host/port into a fixed `CitusRemoteExecPeerEndpoint` at `:421`), so the portable core takes an explicit
endpoint. ⚠ **CORRECTION (review):** the specs are NOT already frontend-safe — `RemoteExecTupleSinkOperationSpec`
embeds a backend `TupleDesc` (`homer_frontend.h:143`), and intent/command specs use `Oid`/`Datum`. So the split
needs a **portable ABI projection** of the specs, not merely moving declarations. Net: no unportable protocol,
but the extraction boundary is bigger than "header split" (endpoint lookup + tuple-contract construction + a
portable spec ABI all live in the backend adapter).

| Hard dep (verified `file:line`) | class | disposition in unified design |
|---|---|---|
| `GetUserId`/`MyDatabaseId` `homer_citus_policy.c:141-142`; `EnsureDistributedTransactionId`/`GetCurrentDistributedTransactionId` `:104-105`,`:376`,`:439`; `XactIsoLevel`/`BeginXact*` `:391-393`; `GetLocalGroupId`/`MyProcPid` `:448-449`; `BuildCurrentTransactionBeginReplayText` `:398` | **state-snapshot** | backend adapter materializes fixed-width intent/command specs; frontend supplies same values via options (`HomerClientSessionOptions`, `remote_execution_client.h:241-267`) |
| `MemoryContextAllocZero(TopTransactionContext)` `homer_frontend.c:352`,`:414`; `PG_TRY/PG_CATCH` `:358`,`:432`; `MemoryContextSwitchTo` `:830`; `AmRemoteExecBackendProcess` `:427` | **lifetime/error** | backend wrapper (context + `ereport`); frontend uses caller-owned storage + error-string returns |
| `TupleDesc`/`TupleTableSlot` at append/decode boundary; `ExperimentalTupleSinkBatchTupleTarget` GUC `:1370`; header includes `postgres.h`/`tuptable.h`/`rel.h` `homer_frontend.h:21-24` | **tuple-view + header split** | core deals only in `CitusTupleViewContract` + `{Datum*,bool*,count}`; batching policy passed as a param; portable types move to a frontend-safe header |

Conclusion: split ① **below its adapters**. Moving `homer_citus_policy.c` or the current `homer_frontend.h`
wholesale into a frontend-safe library would NOT be sound; extracting the state machines + specs beneath them is.

---

## 4. Retained core vs. transport plumbing (from the exhaustive diff)

**Transport-AGNOSTIC → the retained core:** intent fields, operation kind/direction, typed command spec
(`RemoteExecutionCommandSpec`, `homer_frontend.h:344`), `serviceSessionId`, monotonic command sequence +
one-outstanding-START invariant (both structs: `homer_frontend_internal.h:46-47` ≙ `remote_execution_client.h:206-207`;
enforced `homer_frontend_control.c:1192-1207` ≙ `homer_client.c:4374-4392`), completion state/counts,
result descriptor + tuple-view contract, explicit terminal/error state, result borrow/consume semantics, **and
`sessionUID`** (cross-node identity that binds role-7 rings; distinct from the reuse key; client rejects zero,
`remote_execution_client.h:259`, `homer_client.c:1640` — this is S7.4's "confirm it stays", now a core-identity
field). ⚠ These are **mostly** fixed-width but NOT all frontend-safe today (§3): the operation spec embeds
`TupleDesc` and specs use `Oid`/`Datum`, so the retained core needs a portable spec ABI projection.

**Transport-SPECIFIC → per-transport vtable impls:**
- ① host-SHM/local-service: `serviceSinkId`, tuple-sink handle/key/descriptor, `peerCommandServiceSessionId`,
  the six `peerCommandCompletionRing*` fields (`homer_frontend_internal.h:30-31`,`:59-76`); local-service
  dispatch calls; Tier-2 `dpuControlChannel`; `currentSendBatch`/`RemoteExecutionBatchData`.
- ② selected-DPU: `serviceSessionIndex`, completion epoch/generation/lease, `commandDpuStream`
  (embedded `HomerClientBaseBackupStream` carrying DOCA handles + role-1/6/7 lines,
  `remote_execution_client.h:119-147`), byte-ring drain + credit publication.

The vtable surface: open/close, start, completion peek/ack, reserve/publish, result borrow/release, terminal
query. The core must NOT embed either `CitusTupleSinkHandle` or `HomerClientBaseBackupStream`.

---

## 5. Reconciliation decisions (the "don't drop something useful" list)

Beyond the two known differences (explicit intent lost on ②; fused vs 3-step result), the diff surfaced four
substantive divergences. Each is a design decision for this doc + adversarial review.

### 5.1 Completion-ownership model — DEFERRED (owner, decide here)
- **②: observe → apply → ACK lease.** Peek copies the event into a session lease without freeing role-1
  (`homer_client.c:4576-4629`); ACK validates epoch + `completionSessionGeneration` before releasing role-1 for
  the terminal completion (`:4695-4763`). Repeatable only before ACK.
- **①: consume-immediately + latch terminal for replay** (`homer_frontend.c:301-314`,`:693-699`); Tier-2 poll
  releases role-1 inside the poll (`homer_frontend_dma.c:1212-1232`).
- **⚠ REVIEW REFUTED "lease as canonical, as-is" (VERIFIED, §9).** A terminal ACK is what frees role-1 +
  clears `outstandingStartCommandSequence` (`homer_client.c:4756-4758`); an `ereport(ERROR)` between peek/apply
  and ACK **strands the physical role-1 slot**, and close *refuses* to run while role-1 is non-free
  (`:2127`, comment: "a missed ACK must fail loudly"). Also `completionSessionGeneration` is a **no-op today** —
  zero-inited at open, never incremented (only read at `:4585`/`:4714`). ①'s immediate-consume+latch is *safer*
  under backend longjmp (it advances `consumedEpoch` before returning, `homer_frontend.c:301-308`).
- **✅ CORRECTED by §11.1 (second review):** lease IS canonical, made safe under backend unwind by a
  `completion_abandon` = **TERMINAL-teardown + service close-drain** (NOT a host force-free-and-reuse, which
  the DPU-pull-by-ordinal + service in-flight check make unsafe). Semantics: **backend error ⇒ session terminal
  (no reuse)** — exactly what the existing `sqlSessionTerminal` + close-drain already do. Stale-EVENT freshness
  comes from the fresh-per-session `serviceSessionId`, not a generation counter. See §11.1.

### 5.2 Reuse/pinning scope — DEFERRED (owner, decide here). ⚠ TWO LAYERS.
The owner's observation was that resource-level reuse ("the bottom half") already exists — TRUE. **⚠ But the
review REFUTED the "two clean layers" framing (VERIFIED, §9): a SEMANTIC reuse "top half" ALSO already exists,
and the kept path BYPASSES it.**
- **Bottom half (resource pooling), exists:** arena-slot tenancy (release → next tenant), per-session ring
  runtimes, egress claim state (`homer_service_dpu_dma.c:303`/`:615`/`:656`/`:700`); indexed session-slot claim
  (`serviceSessionIndex` vs `CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS`, `homer_client.c:1957`). ⚠ Arena
  tenancy is **session-AFFINE, not a fungible pool**: `HomerDpuDmaBindRingSession()` stamps all three rings with
  one `serviceSessionId` (`homer_service_dpu_dma.c:6444`/`:6478`/`:6545`), bound until full session teardown.
- **Top half (semantic reuse), ALSO exists:** `HomerServiceSessionKeysBaseCompatible()` (5-field key,
  `tuple_sink_service_process.c:24033`), `TupleSinkServiceSessionAllowsReuse()` (freshness/post-command/in-flight/
  clean-placement/exclusive-ownership, `:24610`), `TupleSinkServiceFindReusableSession()` (`:25043`). The
  **tuple-stream** open path USES it (`:29783`, `:39326`).
- **The gap:** **selected-DPU COMMAND open BYPASSES the reuse selector** — `TupleSinkServiceProgressCommandOpenAsyncOp`'s
  `COMMAND_CREATE` phase goes straight to `TupleSinkServiceAllocateSession` + `TupleSinkServiceCreateSession`
  (`:40280`/`:40288`) and binds the client export 1:1 to the new `serviceSessionId` (`:40327`/`:40334`). So the
  kept path is deliberately one-export-per-new-session.
- **Corrected design implication:** unification is NOT "build a top half on the bottom half." It is **reconcile
  the existing semantic-reuse predicate with the session-affine selected-DPU export**, and decide whether
  selected-DPU command open should start reusing an existing service session/export or stay one-per-new-session.
  Resource affinity and semantic reuse currently *meet* at service-session create/teardown.
- **⚠ CORRECTED by §11.2 (second review):** do NOT wire the command path through `FindReusableSession` now — N
  pgbench clients share the 5-field base-compat key and would collide on one reused session (each inits sequence
  at 1; arena is one-export-per-session). **Carry the intent SEAM but keep the selected-DPU command path
  one-export-per-session;** activating reuse needs owner/export-scoping + sequence arbitration → deferred to COPY.
  The seam (the intent contract, §5.6) is still designed and frozen now.

### 5.3 Peer-session pairing
① validates a paired peer/local identity (`peerCommandServiceSessionId`, `homer_frontend.c:278-289`); ② dropped
it (role-6 validates only its own `serviceSessionId` + sequence, `homer_client.c:4615-4624`). COPY's
coordinator↔worker may want pairing. Interacts with [`session_identity_and_pairing.md`](session_identity_and_pairing.md).
Decide whether the core keeps a peer-pair seam.

### 5.4 Error-surfacing gap (fix during the merge)
②'s `HomerClientPollSqlResultDpuReceive` *detects* SQL result ERROR records but does not surface them
(`homer_client.c:3660-3665`); ① raises them (`RemoteExecutionRaiseTupleSinkErrorRecord`, `homer_frontend.c:45-84`).
The unified core closes this gap (error-return in the core; backend adapter re-raises as `ereport`).

### 5.5 Newly-required core primitives (surfaced by review — do NOT ship the vtable without these)
- **Abandon / force-release** — a vtable op that releases the role-1 slot + session ownership WITHOUT a terminal
  ACK, for backend `PG_CATCH` unwind (see §5.1). Current close cannot do this (`homer_client.c:2127` refuses).
- **Generation lifecycle** — mint/increment on session (re)use, define wrap/exhaustion + stale-callback
  behavior. Today `completionSessionGeneration` is inert (§5.1); the core must make it real to protect
  slot/object reuse and stale ACKs.
- **COPY producer export-binding** — the selected-DPU export carries roles 1/6/7 but **no producer payload ring**
  (`remote_execution_client.h:209`,`:220`). The COPY (ingest, SEND-direction) producer needs an explicit
  stream/export-binding design; a `reserve/publish` vtable name is not enough.
- **Producer cancellation** — the reserve/backpressure loop must take an interrupt callback or it becomes
  unkillable under backpressure (the existing selected-DPU producer needed exactly this, `homer_client.c:3918`/`:3932`).

### 5.6 Intent contract — ✅ SETTLED + APPROVED (owner, 2026-07-18)
The intent is **already designed, built, and a portable ABI** — not greenfield. `RemoteExecutionSessionIntentSpec`
(12 fields, `homer_frontend.h:173`) → `BuildControlSessionKeyFromIntent` (`homer_frontend_control.c:311-325`,
maps ALL 12) → `CitusRemoteExecSessionKey` (13 fixed-width `uint32` fields, `homer_control_abi.h:165`) → reuse.
The reuse decision deliberately mirrors Citus: base-compat key match + dynamic `TupleSinkServiceSessionAllowsReuse`
(`tuple_sink_service_process.c:24626`, whose clean-placement rule uses "the same predicate Citus uses in
`ConnectionAccessedDifferentPlacement()`").

**KEY ARCHITECTURAL PROPERTY (why designing it now is enough):** the 13-field key is the frozen ABI, but the reuse
*predicate* reads only a subset. So **activating a RESERVED field's reuse role later (for COPY) is a PREDICATE
change, not an ABI/protocol change.** The ABI is the commitment made now; the predicate is the deferrable behavior.

| intent field | meaning / Citus analog | reuse role TODAY |
|---|---|---|
| `destinationNodeId` | target worker node | **LIVE** (base-compat, `HomerServiceSessionKeysBaseCompatible:24040`) |
| `effectiveUserId` (Oid) | effective user | **LIVE** (base-compat) |
| `databaseId` (Oid) | database | **LIVE** (base-compat) |
| `executionLane` | DEFAULT vs REPLICATION | **LIVE** (base-compat) |
| `freshnessPolicy` | FORCE_FRESH / REQUIRE_CLEAN — Citus freshness/clean-connection | **LIVE** (`AllowsReuse`: FORCE_FRESH⇒no reuse; REQUIRE_CLEAN⇒placement-clean) |
| `ownershipPolicy` | PIN_EXCLUSIVE / shared — Citus connection claiming | **LIVE** (`AllowsReuse`: PIN_EXCLUSIVE + active sinks⇒no reuse) |
| `opKind` | SQL_COMMAND / CLIENT_SQL_SESSION / BASE_BACKUP / (COPY) | validation + basebackup pairing (not base-compat) |
| `accessKind` | placement access SELECT/DML/DDL | **RESERVED** (carried in ABI, not read yet) |
| `txPolicy` | distributed-txn attach policy | **RESERVED** |
| `metadataPolicy` | metadata-sync connection | **RESERVED** |
| `placementScope` | co-location scope (placement safety uses a separate `CitusRemoteExecPlacementAccessDescriptor` today) | **RESERVED** |
| `transactionCritical` | connection must not be dropped mid-txn | **RESERVED** |

**Settled rules (owner-approved):**
1. **Freeze the 13-field key as the stable portable ABI. Do NOT prune RESERVED fields** — retrofitting is the ABI
   change we are avoiding. (Field set may still be *extended* later if COPY needs a facet not present.)
2. **Activation of a RESERVED field = a predicate change** (extend base-compat / `AllowsReuse`), never an ABI bump.
3. **pgbench supplies a degenerate intent** (single node/user/db, DEFAULT lane, no FORCE_FRESH); reuse no-op
   because it holds one session (§5.6-lifecycle: `openHomerSession` once/client at `pgbench.c:8993`). **COPY
   supplies a real intent** and the same intent→key→reuse path lights up.
4. **Adapters populate intent differently:** frontend adapter from explicit options (`HomerClientSessionOptions`);
   backend adapter snapshots live Citus state (`homer_citus_policy.c` initializers).

---

## 6. Interaction with S7 (the retirement plan)

The host-SHM transport, the Tier-2 DMA frontend, and the UDF are retiring under **every** unification outcome —
so that S7 work is not wasted. What the unification decision changes is only the *fate of ①'s abstraction* after
its old transport is gone: **promoted into the core (A)** rather than deleted.

- **HOLD S7.2** (UDF + `RemoteExecutionSession` API deletion): it is the one step that would foreclose promotion.
  The Round-C map is preserved in the S7 plan; resume only after this design settles what is promoted vs deleted.
- **S7.3 (Tier-2 DMA frontend removal)** can still proceed as a *dead-transport* retirement — it becomes one
  vtable impl to drop, independent of the core promotion.
- The dormant COPY-sender residue (§1) is COPY scaffolding; whether it is deleted (S7 §9 Option A) or refactored
  into the core's send/reserve vtable is now part of *this* decision, not a blind delete.

---

## 7. Next steps

1. Adversarial review of THIS design (codex), told not to defer to framing. Load-bearing claims to attack:
   (a) "①'s state machines + specs can be extracted into a frontend-safe core with only thin adapters" (the §3
   audit); (b) "the completion lease model serves backend COPY consumers cleanly" (§5.1); (c) "the top-half
   reuse layer composes with the existing resource pool without reinventing it" (§5.2).
2. Settle §5.1 and §5.2 (+ §5.3) with the review results and the COPY re-plumb requirements.
3. Decide the concrete S7 scope: what deletes now vs. what is carried into the core.
4. Only then: extraction plan (frontend-safe header split first, per the separation plan; then core + vtable +
   adapters), staged and validated per the operator runbook.

## 8. Cross-links

- WHAT (abstraction): [`../connection-management/remote_execution_session_control_plane.md`](../connection-management/remote_execution_session_control_plane.md)
- HOW (component/ABI split + anticipated vtable): [`homer_frontend_service_separation_plan.md`](homer_frontend_service_separation_plan.md)
- Typed command/completion plane: [`../data-movement/command_dispatch_completion_plane.md`](../data-movement/command_dispatch_completion_plane.md)
- Session identity/pairing: [`session_identity_and_pairing.md`](session_identity_and_pairing.md)
- COPY re-plumb surface (the future backend consumer this preserves ① for): [`../../../implementations/citus/transport/copy_path_revival_contract.md`](../../../implementations/citus/transport/copy_path_revival_contract.md)
- Retirement plan this gates: [`../../../implementations/citus/transport/s7_host_service_retirement_plan.md`](../../../implementations/citus/transport/s7_host_service_retirement_plan.md)

---

## 9. Adversarial review outcome (2026-07-18) — verdict + what changed

A codex-explore review attacked the three load-bearing claims; the main agent then re-verified each in code
(not inherited). **Verdict: Approach A is still viable, but with a bigger boundary and three hard sub-problems.**

| claim | verdict | what changed (both sides recorded) |
|---|---|---|
| §3 core is extractable with thin adapters | **SURVIVES, qualified** | I claimed "no catalog lookup / specs already fixed-width." WRONG on both: `LookupNodeByNodeIdOrError` is in the open path (`homer_frontend_control.c:414`, adapter-shaped — snapshots to a fixed endpoint), and the operation spec embeds `TupleDesc` (`homer_frontend.h:143`). Still no hard *protocol* blocker, but the boundary includes endpoint lookup + tuple-contract construction + a **portable spec ABI projection**, not just a header split. |
| §5.1 lease as canonical | **REFUTED as-is** | ACK frees role-1 (`homer_client.c:4756`); an `ereport` mid-lease strands the slot and close refuses (`:2127`); generation is a no-op (never incremented). ①'s immediate-consume+latch is safer under longjmp. → lease needs an explicit **abandon/force-release** primitive + a real generation lifecycle, else immediate-consume wins. |
| §5.2 two clean reuse layers | **REFUTED** | Semantic reuse ALREADY exists (`FindReusableSession`/`AllowsReuse`) and the tuple-stream path uses it, but selected-DPU **command open bypasses it** (`tuple_sink_service_process.c:40280`/`:40288`) and arena tenancy is session-affine. → reconcile with the existing predicate, don't invent a new layer. |

**The three hard sub-problems to settle before S7.2 resumes** (each is now a gating design item, not a detail):
1. **Backend unwind contract** for the completion model (abandon/force-release + real generation lifecycle), or
   pick immediate-consume+latch as canonical instead.
2. **Reuse reconciliation** — ✅ SETTLED (§5.2/§5.6): wire selected-DPU command open through the intent→key→reuse
   machinery; behavior degenerate for pgbench, live for COPY; RESERVED intent fields carried-in-ABI, activated
   later by a predicate change. Remaining mechanical question (extraction-time): how the reuse selector composes
   with session-affine arena tenancy (`HomerDpuDmaBindRingSession`) when reuse actually fires for COPY.
3. **Portable spec ABI projection** + the COPY producer export-binding + producer cancellation contract (§5.5).

**The review also validated the core thesis:** there is no unportable session *protocol* — the merge is an
adapter/ABI/lifecycle problem, not a "can't be done" problem. Approach A stands.

---

## 10. Extraction plan (staged) — SETTLED 2026-07-18

Build/link facts (VERIFIED in the Makefiles): `homer_client.c` → `build/homer/homer_client.o` → `libhomer_client.a`
(`citus-dbcomm/Makefile:16-18,136`), installed to PG `$(libdir)` + headers to `$(includedir_server)` (`:288-316`).
That archive is linked into **BOTH** the client binaries (`pgbench/Makefile:30-36`; Meson `pg_basebackup`) **AND the
postgres server** (`backend/Makefile:28` — because `basebackup_homer.c` calls `HomerClient*`). There is an in-tree
**"compile the same `.c` twice" precedent**: `homer_frontend_dma_lifecycle.c` compiles into `citus.so` via the
extension wildcard AND standalone (`citus-dbcomm/Makefile:198-207`), and it is `postgres.h`-free. PG's canonical
model is `src/common` (frontend + frontend-PIC + backend objects).

### 10.1 Build model for the shared core (the load-bearing mechanical decision)
A new frontend-safe `homer_session_core.c` (+ public header under `src/include/distributed/homer/`), compiled **twice**:
- into **`citus.so`** via the extension wildcard (PIC extension object), and
- into **`libhomer_client.a`** via the top-level Citus Makefile (beside `homer_client.o`), which the client binaries
  and the postgres server already link.
**⛔ Do NOT embed the existing archive wholesale into `citus.so`** — its objects are built with plain `CFLAGS`, not
`CFLAGS_SL` (`-fPIC`) (`citus-dbcomm/Makefile:132` vs `Makefile.shlib:102`); compile the core separately for the
extension. **⛔ The core MUST be STATELESS / caller-owned** — no module globals, no `__thread` state — because it is
linked into two contexts (postgres executable + `citus.so`) and duplicated mutable state would silently diverge.
(The client's current `HomerClientCompletionEventsAcked` `:130`, `HomerClientInterruptCheck` `:264`, and `__thread`
wait-timing `:1371-1374` are diagnostics/callbacks — keep them out of the core, or pass via caller-owned handles.)
The core public header must be independent of **both** `postgres.h` and `postgres_fe.h` (client TUs include
`postgres_fe.h` themselves; the archive is compiled with neither).

### 10.2 Piece 1 — portable spec ABI projection (LOW RISK, mostly exists)
- `CitusRemoteExecSessionKey` (13×`uint32`) and `CitusTupleViewContract` are **already fixed-width, `postgres.h`-free**.
- `RemoteExecutionSessionIntentSpec`: `Oid`→a portable `uint32` alias (Oid *is* uint32). Trivial.
- `RemoteExecTupleSinkOperationSpec`: replace `TupleDesc tupleDescriptor` with `CitusTupleViewContract` (the backend
  adapter already converts both ways: `BuildTupleViewContractFromTupleDesc` `homer_frontend_control.c:330`, reverse
  `homer_frontend.c:116`); `Oid relationId`→uint32.
- `RemoteExecutionCommandSpec`: already mostly fixed-width; `Oid`→uint32.
- Move the projected specs into the frontend-safe core header.

### 10.3 Piece 2 — core / vtable / adapter boundaries
- **CORE (stateless, frontend-safe):** the caller-owned unified session struct; the command-sequence +
  one-outstanding-START state machine; the completion-lease state machine (peek/apply/ack/**abandon**/generation);
  result borrow/release over an opaque batch handle; the error-return convention (no `ereport`).
- **VTABLE `HomerFrontendTransportOps`:** `open` / `close_dataplane` / `close_session` / `start_command` /
  `completion_peek` / `completion_ack` / `completion_abandon` / `result_poll_batch` / `result_borrow_next` /
  `result_release` / `producer_reserve` / `producer_append` / `producer_flush` (COPY future) / `terminal_query`.
  Impls: **selected-DPU (KEPT)**; host-SHM + Tier-2 (retiring — Tier-2 gone at Stage 0).
- **BACKEND ADAPTER:** state-snapshot (the `homer_citus_policy.c` initializers → fill portable specs), TupleDesc↔contract
  binding, memory-context + `PG_TRY`/`ereport` wrapping, `LookupNode`→endpoint (`homer_frontend_control.c:414`),
  and registers `completion_abandon` in its `PG_CATCH`.
- **FRONTEND ADAPTER:** options→intent, `malloc`/error-string, caller-owned memory. `homer_client.c` is already this.

> ⚠ **§10.4 and §10.5 below are SUPERSEDED by §11 (second review + owner decisions, 2026-07-18).** The abandon
> primitive is terminal-teardown (not host-free+reuse); the staging order is **CORE-FIRST, retirement-LAST** —
> ① stays live as a reference, so S7.2+S7.3+D7′ bundle into the final stage; reuse is not wired in. Read §11.

### 10.4 Piece 3 — abandon primitive + generation lifecycle (closes the §5.1 refutation)
- **`completion_abandon(session)`** — idempotent: if a lease is active or the role-1 slot is non-free, force the
  slot state to `FREE`, clear `outstandingStartCommandSequence` + `completionLeaseActive`, **without** a terminal
  ACK; no-op if nothing is outstanding. The backend adapter calls it from `PG_CATCH` before close, so an `ereport`
  mid-lease can no longer strand the slot (today close *refuses*, `homer_client.c:2127`).
- **generation:** mint a fresh monotonic `completionSessionGeneration` at every session **open** (today zero-inited,
  never incremented — inert). `completion_peek` stamps it into the lease; `completion_ack` rejects a lease whose
  generation ≠ the session's (stale event after abandon / struct reuse). `uint64` ⇒ no practical wrap.

### 10.5 Piece 4 — staging (each stage ends gate-green; validate per the operator runbook)
| stage | scope | validation |
|---|---|---|
| **0** | **S7.3**: Tier-2 host DMA-frontend + host claimant removal → assert D7′ (DPU sole claimant, all-slots) | gate + 4-role basebackup + **DPU TCP smoke** (spawn/bridge geometry) |
| **1** | Portable ABI header split (Piece 1); ①/② unchanged, just include the new header | gate + basebackup |
| **2** | Define `HomerFrontendTransportOps`; wrap the EXISTING selected-DPU paths behind it (no merge yet) | gate + basebackup |
| **3** | Extract stateless `homer_session_core.c` (compile-twice build); add abandon + generation; reimplement ②'s client on the core (frontend adapter) | gate + basebackup + **DPU TCP smoke** (client transport) |
| **4** | Reimplement ①'s backend on the core (backend adapter); route selected-DPU command open through reuse; retire ①'s host-SHM path. **S7.2 resolves here** (UDF + old ① API delete-vs-promote) | gate + basebackup |
| **5** *(future, not now)* | COPY producer export-binding + activate RESERVED intent fields | (COPY re-plumb workload) |

### 10.6 Residual risks (VERIFIED)
- **`pg_basebackup` Make wiring is Meson-only** (`Makefile` lacks the Homer archive var though `pg_basebackup.c`
  calls `HomerClient*`) — a *pre-existing* gap; Stage 3 must not assume Make-built `pg_basebackup` links the core.
- **Duplicate mutable state** if the core carries globals → §10.1 stateless rule is mandatory, not stylistic.
- **No explicit archive-rebuild dependency** in `pgbench/Makefile:35` / `meson.build:43` — a stale-archive trap;
  Stage 3 should add the dependency or document a forced clean.
- Hard-coded installed archive paths (`HOMER_CLIENT_LIB ?=`).

---

## 11. Second review outcome (2026-07-18) — three VERIFIED breaks + the corrected plan

The §10 extraction plan was refuted; the main agent re-verified each break in code. **Approach A and the core
thesis still stand; the mechanics and staging change.**

### 11.1 Abandon is TERMINAL-TEARDOWN, not host-free-and-reuse (was §5.1 "lease + abandon", §10.4)
**VERIFIED break:** the DPU pulls the depth-1 role-1 slot by *ordinal frontier*, independent of host slot state
(`homer_service_dpu_dma.c:1997-2008`); a host-only `FREE`+overwrite races that DMA. AND the service rejects a
second command while `commandInFlight` / `completionEventCount > 0` (`tuple_sink_service_process.c:44793-44808`),
so a host-only free does not even clear the service's in-flight view. The old command stays live and can still
publish role-6.
**Corrected contract:** `completion_abandon` = **mark the session TERMINAL + run the service-coordinated
close-drain** (`tuple_sink_service_process.c:44211-44268`/`43593-43633`); **no slot reuse.** On a backend
`ereport` mid-lease, the session DIES (it does not resume) — which is exactly what the existing
`sqlSessionTerminal` latch + close-drain already do. So the lease model IS safe under backend unwind, via
terminal-teardown-on-error, not via a reuse-enabling abandon. **Net: "backend error ⇒ session terminal" is the
accepted semantics.**
**Generation:** stale-EVENT protection already comes from the **fresh-per-session `serviceSessionId`**
(`tuple_sink_service_process.c:5727-5765`), not a generation counter; generation only guards stale lease
HANDLES. Making it wire-visible would be a role-6 ABI change (`homer_dpu_bridge_abi.h:260-275`) — out of scope.
Drop "generation as stale-event protection."

### 11.2 Reuse cannot be WIRED into the command path now — even degenerately (was §5.2 "wire through now", §10.5 Stage 4)
**VERIFIED break:** pgbench opens one session **per client**, but N clients share the SAME 5-field base-compat
key (same db/user/node/lane). An idle zero-sink session is reusable, so routing command-open through
`FindReusableSession` could hand **two clients one service session** — and each client inits its command sequence
at 1 while the service requires one monotonic sequence per reused session (`homer_client.c:4484-4497` vs
`tuple_sink_service_process.c:44826-44840`) ⇒ collision. Arena binding is one-export-per-session
(`homer_service_dpu_dma.c:6601-6640`).
**Corrected:** **carry the intent SEAM (the key is built, the predicate exists) but keep the selected-DPU command
path ONE-EXPORT-PER-SESSION — do NOT call `FindReusableSession` for it.** Activating reuse requires
owner/export-scoping + shared-sequence arbitration, deferred to the COPY re-plumb. (This restores "seam now,
behavior deferred" and corrects §5.2: even *wiring* is deferred, not just behavior.)

### 11.3 Staging order: CORE FIRST (keep ① live), retirement LAST (owner, 2026-07-18)
**VERIFIED break:** ①'s command-session open dispatches through Tier-2 (`HomerFrontendDmaOpenCommandSession`,
`homer_frontend_control.c:806`), so removing Tier-2 (S7.3) breaks ①'s only consumer, the UDF.
**Owner decision:** **keep ① a LIVE reference until the core lands.** Consequence (resolved tension): keeping ①
runnable ⇒ Tier-2 stays live ⇒ the two-claimant window persists ⇒ **the D7 partition, S7.3, and the D7′ all-slots
assertion all defer to the FINAL retirement stage**, bundled with ①'s deletion. The core is built on ②'s base
while ① runs alongside as a working comparison; ①'s *design* (§5.6 + the diff + git) guides the build.
| stage | scope | validation |
|---|---|---|
| **1** | Portable ABI header split — **NOT header-only**: operation spec carries `CitusTupleViewContract`; the backend adapter rebuilds a local `TupleDesc` for `OpenCitusTupleSink` (`homer_frontend.c:439-447`). ① + ② both stay live. | gate + basebackup |
| **2** | `HomerFrontendTransportOps` vtable over the surviving selected-DPU path (②). ① + ② live. | gate + basebackup |
| **3** | Extract stateless `homer_session_core.c` (compile-twice) from ② + frontend adapter; add lease + **terminal-abandon** lifecycle; carry the intent seam (one-export-per-session). ② now on the core; ① still live as reference. | gate + basebackup + DPU TCP smoke |
| **4 — FINAL RETIREMENT** | **S7.2 (delete UDF + old ① API surface + dormant COPY-sender) + S7.3 (Tier-2 removal) + assert D7′ (all-slots)**, all together now that the core has landed and ① is no longer needed as a reference. | gate + basebackup + DPU TCP smoke |
| **5** *(future)* | Backend adapter on the core + COPY producer export-binding + **activate reuse (owner/export-scoped)** + reserved intent fields | COPY re-plumb workload |
**⚠ Cost of this order (owner-accepted):** carries the ① stack (UDF + API + Tier-2 + dormant COPY-sender) as
dead-end reference code through Stages 1-3, and defers the D7′ assertion to Stage 4. Benefit: ① stays a runnable
comparison for the core's behavior until the replacement is proven.

### 11.4 Also corrected / confirmed
- **Stateless core SURVIVES** with one constraint: the DOCA device can't be opened twice per process, so the
  caller-owned transport context must share one device across concurrent sessions (`homer_client.c:868-873`).
- **Stage 4 validation must include a backend-① exerciser** IF any ① consumer still exists at that point — but
  since Stage 0 deletes the UDF, there is no backend-① consumer until COPY (Stage 5), so the gate (②) suffices.

### 11.6 What "reuse" actually is, and the plan NOW (owner-settled, 2026-07-18)
Grounded in the pooled-vs-respawned lifecycle:

| resource | across opens | evidence |
|---|---|---|
| RDMA connection | **POOLED** (free-list, stays established) | `tuple_sink_service_process.c:25264` "stays established for one-step reuse" |
| Arena slot | **POOLED** (slot handed to next backend) | `:25272` "so the DPU can hand it to the next backend" |
| Byte-rings | **POOLED** (8-slot pool) | gate 8-client cap |
| **Socketless backend PROCESS** | **RE-SPAWNED per open** (fork+exec+attach + txn re-attach) | per-session `TupleSinkServiceBeginDpuBackendSpawn`; `launchedBackendPid` per-session, cleared on reset `:25283` |
| Session object | destroyed/recreated per open | `TupleSinkServiceResetSession` memset `:25277` |

**Terminology (owner):** temporal "many commands on one held session" is **persistent-session usage, NOT reuse**;
concurrent sharing of a command session is **illegal, NOT reuse**. **Reuse = cross-open** (release → a *different*
open reclaims). Its real payload is **keeping the backend process alive** (the only expensive thing not already
pooled) — i.e. **backend pooling.**

**Command sessions are SINGLE-TENANT** (the session *is* the tenant unit — one command sequence, one role-1/6/7,
one backend txn; no per-operation sink to isolate owners, unlike a multi-tenant tuple compatibility session whose
tenants each get their own `serviceSinkId`). ⇒ command-session reuse is **sequential-only, claim-enforced**.

**THE PLAN NOW (Stages 1-4): carry the seam, build no reuse.**
- Intent carried in portable specs + recorded as `sessionKey` on the session (data present, nothing matches on it).
- **Command-open stays allocate-fresh** (today's `FindReusableSession` bypass) — inherently single-tenant-safe, so
  **no claim bit needed yet**. Resource pooling (RDMA/slot/rings) unchanged. Tuple-stream reuse machinery untouched.
- **Defensive guard:** a comment at the command-open site + a CONTRACTS `ENFORCES` line — "command-open allocates
  fresh by design; do NOT route through `FindReusableSession` until COPY-era backend-pooling + claim/release land."
- pgbench (persistent-session, backend spawned once/client, pooled resources) is **already optimal — nothing to build.**

**DEFERRED to COPY (the real reuse):** retain-session-on-close → **backend pooling** + claim/ownership +
release-to-pool + sequence continuation (OPEN-response ABI addition) + result-routing UID re-targeting, then wire
COPY's open through the claim-checked selector. Justified there because frequent open/close makes the per-open
backend re-spawn the cost worth eliminating.

### 11.5 Reopened decisions for the owner
1. **S7.2 timing** — delete ①'s code at **Stage 0** (clean tree, smaller extraction surface; design preserved in
   KB + git) vs. **keep ① as a live reference** until the core lands and delete at the end (safety net, but
   carries dead host-coupled code through the extraction — the tax §9 Option A rejected). *Recommend Stage 0.*
2. **Abandon semantics** — confirm **"backend error ⇒ session terminal (no reuse)"** is acceptable (it matches
   current behavior). *Recommend yes.*

---

## 11.7 Execution-entry decision record + Stage-1 grounding (2026-07-18)

### Decisions taken this session (resolves §11.5 + scopes the build)
- **§11.5 #1 (S7.2 timing) — RESOLVED: keep ① as a live reference, retire at Stage 4.** The Stage-0-delete
  *recommendation* in §11.5 was NOT taken; the owner had already chosen "keep ① live until the core lands"
  (the core is built on ②'s base, ①'s *design* is preserved in this doc + git, so ①'s later deletion forecloses
  nothing). §11.3's core-first / retire-last staging stands.
- **§11.5 #2 (abandon) — RESOLVED: yes**, "backend error ⇒ session terminal (no reuse)".
- **Scope — BUILD THE CORE NOW; DEFER COPY.** The owner considered the alternative (skip the core, do a minimal
  host-service retirement now — delete UDF+①+Tier-2+host-SHM, flip D7′ — and rebuild the core from this doc when
  COPY is picked up) and chose to **build the unified core now (Stages 1–4)**. Stage 5 (COPY producer /
  backend pooling / reuse activation) is deferred until the COPY path is (re)designed. Recorded trade-off: through
  Stages 1–4 the core has a single live consumer (②) and its intent/reuse machinery is carried **inert** (pgbench
  exercises only the degenerate intent), so that machinery is compile-covered but not runtime-exercised until COPY.
  Accepted because building the extraction while ① is still a runnable reference is cheaper than reconstructing it
  later, and it lands the ② cleanups (the §5.4 error-surfacing gap; clean lease/terminal semantics) now.

### Stage-1 grounding — VERIFIED in code this session (citus `e9dd4676c`)
The Stage-1 spec projection is **lower-risk than "NOT header-only" suggests**, because the postgres-typed fields
split cleanly along the live/dead boundary:
- `OpenRemoteExecutionSession` branches at
  [`homer_frontend.c:338`](../../../implementations/citus/transport/../../../../src/backend/distributed/utils/homer/homer_frontend.c):
  **live UDF path = `REMOTE_EXEC_OP_SQL_COMMAND` / `REMOTE_EXEC_OP_CLIENT_SQL_SESSION`** (command session via
  `OpenCommandSessionThroughLocalService`, `dataPlaneClosed=true`, **never touches the operation spec's
  `TupleDesc`**). The **only reader** of `RemoteExecTupleSinkOperationSpec.tupleDescriptor` is the **retired
  tuple-sink/COPY branch** at `homer_frontend.c:446` (`OpenCitusTupleSink`), deleted in Stage 4 anyway.
- Consequences: the intent-spec **`Oid → uint32`** projection (`effectiveUserId`, `databaseId`) is a **binary
  no-op** on the live path (`Oid` ≡ `unsigned int`); the operation-spec **`TupleDesc → CitusTupleViewContract`**
  projection touches **dead code only**. A contract→`TupleDesc` reverse builder already exists and is in
  production on the result path (`RemoteExecutionBuildTupleDescFromContract`, `homer_frontend.c:116`; synthesizes
  `column%u` names, comment `:113-114` states the sink layer needs no real attnames), so the round-trip is
  known-good if the dead branch is ever revived. `CitusTupleViewContract` (`homer_tuple_abi.h:121`) carries
  atttypid/typmod/collation/attlen/attbyval/attalign/dropped/generated + natts — everything the sink binds on
  (no attname; not needed).

### Stage-1 tactical refinement — DECIDED (b), and NARROWED (command spec deferred too)
Choice **(b)**, confirmed by the surface map: leave `RemoteExecTupleSinkOperationSpec` (with its `TupleDesc`) in
the backend header as ①-tuple-sink-only retiring code — the map found its initializers
(`InitRemoteTupleSinkSend/ReceiveOperationSpec`) have **no call sites in either tree**, i.e. the operation spec
is already dead; projecting it would be churn on Stage-4-delete code (and would drag in the dropped-column
round-trip hazard below). Also **narrowed** vs §10.2: the `RemoteExecutionCommandSpec` family is **deferred to
Stage 3**, not moved now — its core representation (in-process spec vs the already-portable wire
`CitusRemoteExecStartCommandRequest`) is undetermined, and it is live (the UDF issues SQL_EXECUTE/TX_*), so
moving it now is speculative churn on live code. Stage 1 therefore moves only the **enums + the intent spec**, the
one seam the core unambiguously needs (§11.6).

### Stage-1 IMPLEMENTED (2026-07-18) — pending validation
- **New** `src/include/distributed/homer/homer_session_spec.h` (frontend-safe; `<stdint.h>`+`<stdbool.h>` only):
  the 8 session-intent enums + `RemoteExecutionSessionIntentSpec`, projected `int32`→`int32_t`, `Oid`→`uint32_t`,
  `bool`→stdbool `bool` (enum fields unchanged). **Proven frontend-safe**: a standalone TU including only this
  header compiles+links and its full include tree contains **no postgres header** (correction-1 followed — `_t`
  types, never the c.h `uintNN`/`Oid` aliases, which would drag in `postgres.h`).
- **Edited** `homer_frontend.h`: removed those enums + the intent spec, `#include`s the new header; retains the
  operation spec, command specs, completion, and the ① API. `RemoteExecTupleSinkDirection` stays (it belongs to
  the retained operation spec).
- **Edited** `homer_control_abi.h`: refreshed the stale "mirrored from homer_frontend.h" comment to point at
  `homer_session_spec.h` (which is now itself frontend-safe; the wire macros predate it and remain the mirror).
- **Verified**: all 8 backend TUs including `homer_frontend.h` compile with real flags; all intent-spec users
  reach the new header via the include chain; **② (`homer_client.c`) is binary-identical** (does not include any
  changed header); postgres-citus (pgbench/pg_basebackup/basebackup_homer) has zero references and is unaffected.
- **NO .c changes.** The Stage-1 change is compile-time only; ①'s runtime behavior is byte-identical (width-
  identical types, verbatim enums). Validation = clean full build in BOTH link contexts + selected-DPU gate as a
  regression net; basebackup not required (non-transport, ②-identical).

### Known hazard recorded for Stage 3 / COPY (NOT a Stage-1 regression)
The existing `RemoteExecutionBuildTupleDescFromContract` (`homer_frontend.c:116`) — the contract→`TupleDesc`
reverse builder used on the live result path — **breaks on DROPPED columns**: a dropped attribute carries
`atttypid = InvalidOid`, and `TupleDescInitEntry(InvalidOid)` errors on the type-cache lookup. The current result
path works only because result contracts do not carry dropped attributes. This is pre-existing and out of Stage
1's path (Stage 1 does not touch this rebuild), but the core/COPY work in Stage 3+ that leans on TupleDesc↔contract
must handle dropped attributes (skip/placeholder) before it can carry arbitrary relation schemas.

---

## 12. Implementation plan — module seam, transport interface, Stage 2/3 detail (2026-07-18)

Grounded in a VERIFIED codex map of ②'s structure + a refutation pass, both re-checked in code by the main agent
(per-line re-verification of each op is repeated when that op is implemented). Structural facts that shape this
plan: no dispatch seam exists today (`HomerClientSession` has no ops pointer); the A (session-logic) /
B (transport-mechanics) split runs THROUGH each function (`open`/`start`/`peek`/`ack`/`close` are each mixed); the
result poll is fused (drain + decode + credit); the DOCA device is opened PER-SESSION today; producer ops belong
only to the separate `HomerClientBaseBackupStream`, not to `HomerClientSession`.

### 12.1 Dispatch mechanism — a link-time MODULE SEAM now; function-pointer vtable deferred to COPY
The transport boundary is a **link-time module interface** — a header of transport ops + exactly ONE
implementation (selected-DPU), called **directly** by the core; the linker binds it. **Not** a function-pointer
vtable, **not** a template — zero polymorphism now. Reason: after Stage 4 there is exactly ONE live transport;
runtime dispatch has nothing to choose over until COPY (Stage 5). **Upgrade path:** if COPY needs runtime
per-session transport selection, promote the interface to a function-pointer vtable — a LOCALIZED change (add
`const HomerSessionTransportOps *ops` to the session, set it in each adapter). The **A/B separation** (the value,
and the paving for COPY) lands now; only the dispatch mechanism defers. (This is the current decision; §2/§10.3's
earlier "vtable" wording is superseded here.)

**Op-legality is enforced at COMPILE TIME via typed opaque handles (owner, 2026-07-18):** one internal core
`HomerSession` + DISTINCT opaque handle types per kind (`HomerSqlSession`, `HomerBaseBackupSend`, …) all aliasing
the core; each op takes its kind's handle, so a wrong-op-on-wrong-kind (e.g. `result_borrow` on a basebackup-send
session) is a COMPILE error, at zero runtime cost. This is a SEPARATE boundary from the module seam above: the
module seam is core↔transport; the typed handles are core↔caller. Full interface: §13.

### 12.2 The selected-DPU transport interface (the pure-B op surface the core calls)
DOCA/byte-ring types stay INTERNAL to the impl (frontend-safe signatures). Each op carries the status returns,
correlation inputs, and opaque tokens needed so the A/B cut does NOT leak:

| op | contract | ② source |
|---|---|---|
| `transport_open(sessionUID, intent-ids…) -> status` | `sessionUID` is an INPUT (stamped into 3 descriptors + open request); must NOT `memset` the whole session (that wipes A state the core owns); returns transport status/identity | `HomerClientOpenSqlSessionSelectedDpu:1560`; sessionUID `:1813`/`:1846`/`:1874`/`:1920`; offending memset `:1658` |
| `arm_initial_result_credit()` | B post-open: publish the first role-7 consumed credit (distinct from the core's `init_decode_identity` at `:1980`) | `:1982` |
| `publish_start(minted_sequence, portable_command_spec) -> status` | B owns the request slot and rejects physical-busy internally (or exposes `control_slot_available`); A finishes semantic validation BEFORE B touches the slot | one-outstanding reads logical+physical `:4379`; publish `:4491`; A-side slot pokes to remove `:4457`/`:4467`/`:4481` |
| `poll_completion() -> {classification, copied_completion}` | classification = not-ready / overrun / identity-mismatch / visibility-pending / ready; returns a COPIED immutable completion AFTER transport identity passes; semantic validation stays in the core | role-6 read `:4589`; transport identity `:4615`; semantic `:4631`/`:4639`; opaque token replaces `slotIndex` `remote_execution_client.h:66`/`:4626` |
| `release_start_slot(correlation_sequence) -> status` | releases the retained role-1 START slot only when the terminal completion sequence == the correlation sequence (role-6 has no host consumed word — hence "start slot", not "completion slot") | `:4749`/`:4756`; correlation `:4727` |
| `lifecycle_close() -> status` / `dataplane_close()` | SEPARATE ops; the core owns terminal policy + semantic-close ordering; close-readiness returns WHICH predicate blocked | today fused: semantic-close `:2072` + CLOSE_SESSION `:2202` + teardown `:2234`; readiness `:2127` |
| `result_borrow_next` / `result_release` / `result_credit_flush` | the split of the fused result poll — see §12.5 step 3 | fused poll `:3544` |

Open question to settle at impl time: timeout policy = core-policy vs transport-config (today `getenv` inside
start, `:4394`).

**A state machine → the core (§12.5):** command sequence + one-outstanding-START invariant (`:4374`/`:4484`);
completion lease observe/apply/ack (`:4576`/`:4695`) + terminal latch (`:4657`); intent (the seam); `sessionUID`
(NEW core-retained field — today options/descriptors only); result DECODE including the decoded command-sequence
identity (`receiveExpectedCommandSequence:3490`). The transport-record ordinal (`receiveExpectedOrdinal:3390`)
stays B. The one-way **core→transport** dependency requires the raw-lease result API (§12.5 step 3), NOT an inline
decode callback (which would invert the dependency and break the seam).

### 12.3 DOCA device + the transport context — ONE shared device per process
- **Host-side DOCA device is real** (not DPU-only): the host opens a `doca_dev` to EXPORT host memory
  (`doca_mmap`) for the DPU to DMA the byte-rings (roles 1/6/7). The DPU opens its own device for the DMA.
- **Intended design = ONE device per process, shared across regions** (`homer_client.c:868-873`: the
  `HomerDpuFrontendOpenDevice` split exists so "one exporter can publish several disjoint regions against a SINGLE
  device -- opening the same PCI device twice from one process is not something DOCA promises"; Tier-2 shared one
  device across four exports).
- **CURRENT state = per-SESSION device** (`:853` via `:1765`). The hazard is **multi-session-per-process = pgbench
  only** (`-j` threads → N sessions/process → N device opens). pg_basebackup and `basebackup_homer` are
  one-stream/process → no hazard. The gate passes at 4 clients today, but on undefined-by-DOCA behavior.
- **FIX (Stage 3): a process-owned transport CONTEXT holds ONE shared `doca_dev`.** Requirements:
  - NOT embedded in `HomerClientSession`; owned by an explicit process/context owner.
  - thread-safe cold-path lifecycle (pgbench is multithreaded; concurrent opens `pgbench.c:8983`/`:8991`) — a
    per-thread context would still open the device N×/process, so it must be process-wide.
  - refcount ONE reference per live OR intentionally-retained mmap (rule "all mmaps before device",
    `remote_execution_client.h:330`); the failed-close path deliberately RETAINS a live mmap/device when DPU
    setup-close is unacked (`:4280`) — the context must hold that ref, not close the device under it.
  - per-session close (`:4296`) becomes **mmap-only**; device close is owned by the context.
  - DOCA concurrent mmap create/destroy thread-safety is unproven → serialize lifecycle in the cold path; the data
    path stays lock-free.
  - device sharing is import-safe: the DPU wire ABI carries NO host-device identity (import keyed by
    generation/client/export-id on the DPU's own device, `homer_service_dpu_dma.c:8872`/`:8967`).
- **mmap granularity:** one DEVICE throughout, one `doca_mmap` PER session (a device supports many mmaps; within a
  session the 3 roles already share one buffer/mmap, `:1765`). A single shared-arena mmap (pooling all sessions'
  rings) is an OPTIONAL later optimization, not this cleanup. (Neither pgbench nor the backends pool host export
  buffers today — both allocate per-session; the pooled arena is service-side on the DPU, not the host frontend.)
- **Transport unification (settled — see §13):** the transport-leaf mechanics are ALREADY shared across the SQL
  session and both basebackup directions (same `static` helpers). The end-state is ONE transport substrate +
  context serving all host-frontend users, under **ONE intent-driven session abstraction** (§13) whose intent
  gates which rings bind (§13.2) and which op planes are legal (§13.3). A bulk stream simply has no
  command/completion plane, so those ops are **compile-time illegal on its typed handle** (§13.1) — that is the
  unification mechanism, not a reason for a separate session type. Stage 3 builds the shared context for the SQL
  opKind; **basebackup migrates onto it in Stage 3.5** (§12.5b) as the second, differently-shaped consumer —
  BEFORE final retirement, while ① is still a live reference — the multi-consumer proof, not a deferred cleanup.

### 12.4 Stage 2 — a SYMBOL-boundary transport FACADE inside `homer_client.c` (behavior-preserving)
Stage 2 is a symbol-boundary facade, **NOT a file/module move**: the transport leaf helpers are shared with both
basebackup directions and mostly `static` (export `:1016`, layout `:1116`, setup-TCP `:1285`, role-1
publish/wait/free `:1460`, receive-credit `:2578`, close body `:4190`, drain), and
`HomerClientDpuSubmitControlRequest:1476` has a dual START-fire-and-forget (retains role-1, `:1503`) /
open-close-wait-and-free (`:1508`) contract that breaks if casually generalized. Steps:
1. Declare the §12.2 transport ops as narrow SQL-specific entry-point functions IN `homer_client.c`, wrapping the
   existing selected-DPU blocks (frontend-safe signatures; DOCA stays internal).
2. Redirect ONLY the SQL A-logic through those entry points; A-logic stays inline (no core yet). The fused result
   poll stays a ② function for Stage 2 (split in Stage 3).
3. Leave the shared leaf helpers + `HomerClientBaseBackupStream` + basebackup callers UNTOUCHED; NO device
   consolidation (that is a behavior change → Stage 3). Strictly behavior-preserving indirection.
- **Validation: gate + basebackup** (② shares transport leaves with basebackup — prove basebackup unaffected).

### 12.5 Stage 3 — extract the stateless core, the transport module, and the shared device context
Realizes the **SQL opKind** of the unified intent-driven interface in §13 (typed opaque handles; one core).
1. Add `homer_session_core.c` (stateless, POSTGRES-INDEPENDENT, caller-owned) holding the A state machine (§12.2)
   for the SQL opKind: sequence + one-outstanding invariant, completion lease + terminal latch, intent,
   `sessionUID`, result decode/borrow/release. The core is opened via a typed `HomerSqlSession` handle (§13.1) and
   calls the §12.2 transport interface via DIRECT link-time calls.
2. Physically move the selected-DPU transport mechanics out of the Stage-2 facade into a transport module (the
   shared helpers change anyway now); ② becomes the thin **frontend adapter** (options→intent, `malloc`,
   error-string).
3. **Split the fused result poll into borrow/release + batched credit:** `result_borrow_next -> {ptr,bytes,token}
   | non-record-progress | none` (B derives record bounds from the transport header `homer_byte_ring_sink.h:271`/
   `:281`, skips wrap-gap trailer bytes `:256`/`:304`); the core DECODES; `result_release(token)` advances the
   local consumed head AFTER decode (credit must not advance before decode validates, `:327`/`:338`, or the DPU
   reuses the bytes); `result_credit_flush()` publishes the DPU credit once per pass (preserve the batch-coalescing
   `:3637`/`:3642`). The decoder result is **TRI-STATE** (success/release, visibility-pending/no-release,
   fatal-malformed/no-release) plus a separately-surfaced semantic **command-ERROR** — this closes §5.4 AND the
   bigger "malformed record retries forever" gap (`:3385`/`:3437`/`:3470`/`:3490`; ERROR detected `:3440`, not
   returned `:3660`). The raw-result lease NESTS inside the completion lease (`pgbench.c:3637`/`:4070`): a result
   failure must not ACK completion / release role-1 before terminal-abandon. **This same lease-aware
   `HomerByteRingSinkDrain` refactor (borrow-without-advancing) is what basebackup RECEIVE also needs (§13.3), so
   build it as the shared sink-lease mechanism — one refactor, two consumers (SQL result-consume + BB bulk-consume).**
4. Add the completion lease + **TERMINAL-ABANDON** (§11.1): the core owns the lease; the frontend adapter's
   abandon = mark terminal (no reuse), matching current `sqlSessionTerminal`; the backend adapter (Stage 5)
   registers abandon in `PG_CATCH`.
5. The shared DOCA device CONTEXT (§12.3): process-owned, refcounted, thread-safe; per-session close becomes
   mmap-only; device close owned by the context.
6. **BUILD:** give `libhomer_client.a` a MULTI-OBJECT recipe — add `homer_session_core.o` as a SEPARATE member
   (today `citus-dbcomm/Makefile:136` archives only `homer_client.o`). Separate objects keep the (backend-linked)
   archive from making the unreferenced core a backend consumer. The core is frontend-USED only through Stage 4
   but must be postgres-INDEPENDENT, because the backend already links the archive (`basebackup_homer.c` →
   `libhomer_client.a`, `basebackup_homer.c:273`/`:299`/`:353`/`:465`; `src/backend/Makefile:28`/`:42`,
   `meson.build:47`).
- **Validation: gate + basebackup + DPU TCP smoke** (client transport + the device-model change).

### 12.5b Stage 3.5 — migrate basebackup onto the unified session (the multi-consumer proof) (owner, 2026-07-18)
Migrate `pg_basebackup` (RECEIVE client) + `basebackup_homer.c` (SEND backend) onto the unified core/interface
(§13) as the SECOND, differently-shaped consumer — **before final retirement, while ① is still a live reference.**
This proves the abstraction generalizes across workload kinds (client + backend, command + bulk stream), not just
SQL. Realizes the **BASE_BACKUP opKind**: adds the typed `HomerBaseBackupSend`/`HomerBaseBackupRecv` handles, the
**producer plane** (reserve/commit, role-4) and the **bulk-consume plane** (borrow/release/credit, role-7, on the
shared sink-lease from Stage 3), and the BASE_BACKUP operation-open spec (direction, geometry,
`launchDiscriminatorTag`, SEND peer endpoint). basebackup terminal keeps its FAILED(credit-line)/CLOSED split
(§13.4). Design is grounded in the VERIFIED basebackup map (§13). **Validation: four-role basebackup (the
mandatory transport-acceptance workload) + gate + DPU TCP smoke.**

---

## 13. The unified intent-driven session interface (designed against SQL + basebackup, 2026-07-18)

Grounded in the VERIFIED ② and basebackup codex maps. §12 is the *build plan*; this section is *what* the stages
build toward. The owner's design intent: **one Homer session abstraction — the intent declaratively says what the
user will do, and gates which rings bind and which ops are legal.** SQL is the first consumer (Stage 3), basebackup
the second (Stage 3.5), COPY the third (Stage 5).

### 13.1 Model — one core, typed opaque handles, two-part open (intent + operation spec)
- **One internal core `HomerSession`** (the implementation) + **DISTINCT opaque handle types per kind**
  (`HomerSqlSession`, `HomerBaseBackupSend`, `HomerBaseBackupRecv`, later `HomerCopy*`), all aliasing the core.
  Each op takes its kind's handle → wrong-op-wrong-kind is a COMPILE error (owner decision); thin typed forwarders
  cast to the core; zero runtime cost. Unified implementation + compile-time legality.
- **Two-part open (①'s design, INDEPENDENTLY VALIDATED by basebackup):** a compat **INTENT**
  (`RemoteExecutionSessionIntentSpec`, the reuse/compat domain, `opKind` ∈ {SQL_COMMAND, CLIENT_SQL_SESSION,
  BASE_BACKUP, COPY_*}) **+ a per-kind OPERATION-OPEN spec** (per-op bootstrap: direction, payload/max-record
  geometry, `launchDiscriminatorTag`, peer endpoint). The intent gates reuse/compat; the operation spec bootstraps
  the concrete rings. basebackup's needs (direction/geometry/tag; SEND peer, RECEIVE none —
  `homer_client.c:2303`/`:2673`) ARE this operation spec, and `homer_session_spec.h:120` already states bootstrap
  identity belongs OUTSIDE the compat intent. (This generalizes ①'s `RemoteExecutionOperationSpec` union beyond
  its current tuple-sink-only member — a NEW frontend-safe per-kind spec, not ①'s dead tuple-sink struct.)

### 13.2 Ring binding by opKind + direction (VERIFIED)
| kind / direction | rings bound | source |
|---|---|---|
| SQL command/result | role-1 (control) + role-6 (completion) + role-7 (result) | `homer_client.c:1795`/`:1828`/`:1856` |
| BASE_BACKUP SEND | role-1 (lifecycle) + role-4 (host→DPU payload) | `:2422`/`:2442` |
| BASE_BACKUP RECEIVE | role-1 (lifecycle) + role-7 (DPU→host payload) | `:2797`/`:2818` |
| COPY (future) | reuses the SQL command/result + producer planes | — |

**role-1 is COMMON** (every kind binds it for synchronous OPEN/CLOSE, `:1460`/`:1508`; SQL additionally uses it for
fire-and-forget START). **role-6 (completion) is SQL/COPY-only.** The open path binds the ring SET selected by
`opKind`+direction.

### 13.3 Op planes, gated by intent (compile-time via the typed handle)
| plane | ops | legal for | notes |
|---|---|---|---|
| lifecycle | `open` / `lifecycle_close` / `dataplane_close` | ALL | role-1 synchronous; close-readiness reports which predicate blocked |
| command | `publish_start` / `poll_completion` / `release_start_slot` | SQL, COPY | role-1 START (fire-and-forget) + role-6 completion; one-outstanding invariant |
| result-consume | `result_borrow_next` / `result_release` / `result_credit_flush` + core decode | SQL, COPY-result | role-7; the shared sink-lease drain |
| producer | `producer_reserve` / `producer_commit` (append = caller `memcpy`) | BB-send, COPY-ingest | role-4; one reservation at a time (`:3788`), commit publishes immediately (`:4167`) |
| bulk-consume | `consume_borrow_next` / `consume_release` / `consume_credit_flush` | BB-recv | role-7; **SAME shared sink-lease drain as result-consume** |

**result-consume and bulk-consume share ONE lease-aware `HomerByteRingSinkDrain` refactor** (borrow without
advancing `consumedHead`) — built once in Stage 3, reused in Stage 3.5. Today both are fused poll/validate/
discard-or-decode/credit (SQL `:3544`, BB-recv `:3095`) and cannot withhold the consumed cursor (`:327`/`:338`).

### 13.4 Terminal model — common concept, kind-specific realization
"Terminal ⇒ no reuse" is the shared contract. **SQL:** a persistent `sqlSessionTerminal` latch after completion
application (`remote_execution_client.h:228`). **basebackup:** direction-specific FAILED via the credit line
(`homer_client.c:3199`/`:3209`), immediate and drain-independent; CLOSED is drain-gated success (`:3218`). The core
exposes a common `terminal` query; terminal-abandon (§11.1) is per-kind (SQL: mark latch; BB: FAILED/ABORT_RESET
path `:4190`/`:4236`).

### 13.5 Shared vs kind-specific (from the A/B maps)
- **SHARED transport (B), one context** (Stage 3): the DOCA device (§12.3), byte-ring mechanics, role-1 lifecycle,
  setup/mmap, epochs. `HomerClientBaseBackupStream` is ALREADY a shared carrier for SQL+BB transport state — its
  own header calls the name a misnomer (`remote_execution_client.h:209`) — Stage 3 cleans it into the shared
  context.
- **KIND-SPECIFIC core (A):** SQL = command sequence + completion lease + result decode; BB = record
  sequence/ordinal (`nextSequence`/`submittedSequence`/`receiveExpectedOrdinal`). These live in per-kind core
  state tagged by `opKind`, not the shared carrier.

### 13.6 Deferred / not now
- **COPY (Stage 5):** `opKind` reserved; reuses command + result-consume (result) + producer (ingest) planes; op
  shapes finalized against COPY's real code when implemented.
- **`blackhole` target mode:** currently inconsistent (PG parsing accepts `mode=blackhole`
  `basebackup_homer.c:179`, but both selected-DPU openers reject non-RDMA `homer_client.c:2303`/`:2678`;
  pg_basebackup's blackhole opens RDMA and discards `pg_basebackup.c:2403`/`:2428`). A pre-existing wart — noted,
  out of scope, do NOT fold a fix into this work.
