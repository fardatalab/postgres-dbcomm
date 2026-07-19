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

**Concrete Stage-2 facade — SEAM VERIFIED IN CODE 2026-07-18** (each A/B cut re-read line-by-line in `src/bin/homer_client.c` by the main agent AND cross-checked by an independent codex-explore seam map; divergences resolved below). SIX in-file `static` entry points, frontend-safe signatures, all declared ABOVE the untouched shared leaves; the SQL A-logic in Start/Peek/Ack/Close is redirected through them:

| entry point | wraps (current lines) | callers | notes |
|---|---|---|---|
| `SqlTransportControlSlotAvailable(session, *outSlotState)` | slot-state atomic load `== FREE` (`:4380`) | Start (`:4379`), Close-readiness (`:2127`) | A keeps its own `outstandingStartCommandSequence` check; B answers only the physical slot word, and writes the observed state out for A's diagnostic. Handles NULL slot (→ `UINT32_MAX`, false) to also serve Close's `:2131` NULL-guarded read |
| `SqlTransportPublishStart(session, timeoutMs, cmdName, err…)` | fire-and-forget submit `…,retainRole1=true,…` (`:4491`) | Start | thin: preserves the `true` dual-contract arg of `HomerClientDpuSubmitControlRequest`. Request CONSTRUCTION (`:4413`–`:4483`) stays inline in Start (A writes into the B slot) — moving it behind a portable_command_spec is Stage 3 |
| `SqlTransportPollCompletion(session, expectedEpoch, *classification, *copiedCompletion, err…)` | role-6 read + epoch classify + transport-identity + COPY (`:4586`–`:4627`) | Peek | classification enum uses ONLY the statuses Peek actually emits: NOT_READY/READY/OVERRUN/MISMATCH (NOT `BODY_VISIBILITY_PENDING`, which Peek never emits — adding it would be a behavior change). Copies straight into `&session->completionLease.completion` (zero extra copy); A keeps `slotIndex=0` (`:4626`), `BuildCompletionViewFromLegacy` (`:4628`), semantic validation (`:4631`–`:4648`), terminal latch (`:4650`–`:4687`) |
| `SqlTransportReleaseStartSlot(session)` | atomic store slot→FREE (`:4756`) | Ack | correlation decision (`:4728`) + `outstandingStartCommandSequence=0` stay A |
| `SqlTransportLifecycleClose(session, timeoutMs, err…)` | build CLOSE_SESSION + wait-and-free submit `…,false,…` (`:2202`–`:2224`) | Close step 2 | A owns the readiness gate (`:2127`–`:2146`, reusing `SqlTransportControlSlotAvailable` for the physical bit) + step ordering. Called only when A has decided `lifecycleControlSlotReady` |
| `SqlTransportDataplaneClose(session, err…)` | thin wrapper over shared `HomerClientCloseBaseBackupStream(&session->commandDpuStream,…)` (`:2235`) | Close step 3 | shared leaf UNTOUCHED; wrapper only names the SQL call site |

**Divergences from the codex-explore map, resolved toward minimal indirection (main-agent decision, verified in code):**
- **`publish_start` granularity.** Codex proposed a fuller `publish_start` that builds the request internally AND reorders semantic validation before the slot write (to delete the error-path releases). REJECTED for Stage 2: §12.4 scopes it as behavior-preserving indirection with A-logic inline; the portable-command-spec + validation-reorder is the Stage-3 core→transport shape. Stage 2 keeps construction inline and wraps only the submit.
- **Error-path `HomerClientReleaseControlSlot` (`:4457`/`:4467`/`:4481`).** VERIFIED these are DEFENSIVE NO-OPS: between the FREE-check (`:4380`) and the error paths nothing sets the slot non-FREE (state only changes inside `HomerClientDpuSubmitControlRequest:1492`), and the helper (`:388`) just stores FREE. §12.2 already marks them "to remove" (Stage 3, with the reorder). So they are NOT a facade op — they stay inline; the 7th wrapper is dropped.
- **`lifecycle_close` consolidation.** Codex proposed folding the readiness POLICY into the transport op via a result enum. REJECTED: §12.2 says A owns close-readiness policy. The op wraps only build+submit (`:2202`–`:2224`); A keeps the readiness gate.
- **Open + `arm_initial_result_credit`.** Codex maps `transport_open`/`arm_initial_result_credit` cuts; per the Open-deferral decision below they are NOT extracted in Stage 2 (Open's B ops are one linear bootstrap sequence, not state-machine-dispatched; Stage 3 restructures Open wholesale, so wrapping now is throwaway).

**DECISION (owner-improvised, recorded 2026-07-18): Open's bootstrap is NOT wrapped in Stage 2 — deferred to Stage 3.**
`HomerClientOpenSqlSessionSelectedDpu` (`:1560`–`:1985`) is ~320 lines of LINEAR transport bootstrap
(layout → `posix_memalign` → 3 ring descriptors → mmap export → setup-TCP → OPEN submit → id read-back → arm
credit). It DOES call B leaves (the OPEN `HomerClientDpuSubmitControlRequest`; the initial role-7 credit arm), but
as one linear setup sequence, not as ops dispatched from an interleaved A state machine the way Start/Peek/Ack are
— from the caller's/state-machine's view "open" is a single atomic step.
Its embedded A-state (intent→`sessionKey` projection `:1918`–`:1937`; id read-back `:1948`; `commandDpuStreamOpen`
latch `:1966`; `init_decode_identity` `:1981` + `arm_initial_result_credit` `:1982`) moves WITH open in Stage 3,
where the core-open / transport-open split, `sessionUID` stamping, and the `memset(session,0)` fix (`:1658`, the
op §12.2 says `transport_open` must NOT do) happen together. Rationale: wrapping a linear block in Stage 2 only to
re-cut it in Stage 3 is churn with no seam value; the Stage-2 win is the command-plane + close cuts, which ARE
genuine A↔B call seams and are exactly what proves basebackup-shared leaves stay untouched. (Options considered:
(a) wrap all of open now — rejected, double-extraction churn; (b) this — deferred to Stage 3; both keep behavior
identical. Revisit only if Stage 3 finds it needs the open seam earlier.)

**Adversarial refutation result (codex-explore, 2026-07-18) — IMPLEMENTED + reviewed.** The 6-facade diff was
attacked op-by-op against the pre-facade line map. Verdict: NO state-machine, wire-protocol, shared-leaf, or
missing-side-effect regression; Peek/Ack/publish/lifecycle/dataplane confirmed byte-identical; NULL-slot readiness
cases exactly preserved. One BENIGN, intentional difference: Start's one-outstanding check and Close's readiness
gate now take ONE coherent slot-state snapshot, where the pre-facade code re-read the atomic in the condition and
again in each diagnostic — so under an async DPU state change the old code could LOG differing `slot_state` values.
Accept/reject logic is unchanged; the single snapshot is if anything cleaner. Documented at the Start readiness
comment and the facade block header.

**VALIDATION: PASS (2026-07-18, working tree on citus `265b6630d`).** Gate + four-role basebackup, per §12.4.
- Gate (`pgbench --homer --homer-dpu`, 4 clients): debug 20/20 with 20 DISTINCT decoded `abalance` values; 3
  measured repeats 8000/8000, 0 failed. Transport `homer-dpu` confirmed; DPU spawn events (legacy + `HOMER_EVENT`)
  agree; `gate_check`/`alarm_check` PASS over every bracketed interval; `peer_host_spawn_retired` and all
  alarm/fatal anchors ABSENT. All six facade ops exercised (24k+ completions read via `SqlTransportPollCompletion`).
- Basebackup (four-role, mandatory transport acceptance): `full_laps=44364` (byte-ring WRAP proven), delivered
  bytes match, full drain, `teardown_checks`/`frontier_checks` balanced on both DPUs → the SQL-side indirection
  leaves the SHARED transport leaves unaffected (the whole point of the gate+basebackup pairing).
- Perf: INCONCLUSIVE — no current-SHA `-c4` reference band exists in `farnet_diagnostics_and_baselines.md` (only
  `-c1` bands + one flagged-non-comparable `-c4`). Behavior-preserving indirection expects no perf change; 3
  repeats agree within ~1.3% (~71–72 tps `-c4`). Not an acceptance criterion for this stage.
- **One non-reproducing anomaly, MECHANISTICALLY EXCLUDED from this change:** one measured-repeat attempt
  ("repeat-2b", run against residual state with no clean-baseline restart) saw all 4 clients time out on
  `sql_execute` seq-5 completion (0/8000). Root cause is DPU-SIDE: the farnet1 DPU stalled at `PROGRESS
  POLICY-ADMISSION DROPS` and never published the seq-5 completion — although it HAD landed peer commands 1–5 and
  finalized the result stream, proving the host START publishes (`SqlTransportPublishStart`) all worked. A
  host-frontend-only change (DPU source + bridge `5U` unchanged) cannot make the DPU not publish. The host-side
  `CLOSE_SESSION blocked … slot_state=2` ALARM is my refactored close-readiness gate firing CORRECTLY (seq-5 START
  never ACKed → role-1 stayed REQUEST_READY=2 → close refuses lifecycle-close loudly, as designed). Did NOT
  reproduce after a clean-baseline restart (retry 8000/8000). See `farnet_diagnostics_and_baselines.md` for the
  observed DPU admission-drop shape.

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

### 12.5a Stage-3 CONCRETE DESIGN — grounded in code 2026-07-18 (main agent reads + codex-explore inventory, reconciled)

Six components. Build order (internal; validated once per the owner's "one push"): A (sink-lease) → B (core) →
C (typed handle) → D (transport module + adapter) → E (device context) → F (archive). The **carrier
`HomerClientBaseBackupStream` stays shared with basebackup** (Stage 3 does NOT dissolve it; 3.5 does its half).

**A. Shared sink-lease** (`homer_byte_ring_sink.h`). Split the fused drain at the callback boundary (verified
seam: framing `:256`/`:281`/`:297`/`:318` is pure geometry; delivery is callback `:327` + advance `:338`):
- `HomerByteRingSinkBorrow(control, byteStorage, producedTail, min/maxRecordBytes, *lease) -> {PROGRESS+lease |
  NOT_READY | VISIBILITY_PENDING}`: runs the framing loop, SKIPS fully-published wrap-gap trailers internally
  (advancing `consumedHead` past dead bytes — never leased, idempotent), returns the next WHOLE fully-published
  record `{recordBuffer, recordBytes}` WITHOUT the callback and WITHOUT advancing past the record.
- `HomerByteRingSinkRelease(control, *lease)`: the `:338` release-store (`consumedHead += recordBytes`). Called
  only after the consumer's decode succeeds — preserves "credit must not advance before decode validates, or the
  DPU reuses the bytes." **GUARD (refutation): assert `consumedHead == lease.recordStart` before the store** so a
  duplicate/stale release cannot advance over a *following* record. Release does NOT accumulate bytes itself; it
  exposes `lease.recordBytes` and the CALLER adds it to its own accumulator (SQL `receiveDeliveredObjectBytes`
  `:3809`, BB `:3308`) — Release's signature carries no accumulator.
- Keep `HomerByteRingSinkDrain` as a COMPATIBILITY wrapper so nothing else breaks mid-stage. **Its contract must
  reproduce the current status EXACTLY (refutation):** it loops borrow → callback → (release + accumulate) and
  tracks `madeProgress` = any `consumedHead` advance in the pass (trailer-skip inside borrow OR a release), so a
  trailer-skip-then-pending pass still returns `PROGRESS` (matching `:226`/`:264`/`:318`); on an accepted
  **EOS** record it releases + accumulates + sets `sawEos` + returns `PROGRESS` immediately (the `:340-345` stop
  rule); a callback-false with no prior progress returns `VISIBILITY_PENDING` (or the borrow's `NOT_READY`). The
  consumer decode result becomes TRI-STATE: success/release, visibility-pending/no-release, fatal-malformed/
  no-release (fixes the current conflation where every callback-false → VISIBILITY_PENDING forever, `:327-332`).
  Both SQL result-consume AND basebackup bulk-consume (3.5) use borrow/release verbatim.

**B. `homer_session_core.c`** (NEW, POSTGRES-INDEPENDENT — `_t`/stdbool only, per Stage-1 rule). Holds the SQL
opKind A-state machine. Core struct = the A fields (`serviceSessionId/Index`, completion-lease quartet, sequence
pair, `sqlSessionTerminal`, session-open flag) + **NEW retained `sessionUID` + intent** (today discarded after
open, `:2100`) + **`receiveExpectedCommandSequence` moved out of the carrier** (`:177`; SQL command-local decode
identity). `completionSessionGeneration` is carried but stays **VESTIGIAL (0)** in Stage 3 (never minted today,
`:4767`/`:4877`; 0==0 always passes — minting for stale-lease rejection is a Stage-5/reuse item). Core functions
call the §12.2 transport interface via DIRECT link-time calls: open-orchestration, `start`, `poll_completion`,
`ack`, close-orchestration, and result-consume.

**Result-consume layering (RESOLVED from refutation — the ordinal is NOT purely B):** the SQL decode reads AND
advances `receiveExpectedOrdinal` (`:3560`/`:3487`), so the split layers rather than partitions:
`borrow`(transport framing) → **envelope-validate + ordinal** (proto/kind/headerBytes/`recordOrdinal` vs
`receiveExpectedOrdinal`/reserved — TRANSPORT-owned, B, `receiveExpectedOrdinal` stays in the carrier) → **batch
decode + command-seq + capture + ERROR** (core, A, vs `receiveExpectedCommandSequence`) → **accept** advances BOTH
identities together (transport advances the carrier ordinal via a transport op; core advances command-seq; EOS
resets both, mirroring today's fused `HomerClientSqlResultAdvanceCommandIdentity` `:3487`) → `release` → once-per-pass
`credit_flush`. Tri-state applies to BOTH the envelope-validate and the batch-decode.

Re-review resolutions on the result-consume contract:
- **accept is INFALLIBLE** — the "advance both identities" step is pure single-consumer counter work (ordinal++ /
  reset + command-seq++ / reset, keyed on the retained `header->flags` EOS bit `:3487`); no I/O, no allocation, no
  failure path between core capture and the two increments (today `:3686-3699` has none). This prevents a split
  where one counter advanced and the other did not.
- **`credit_flush` runs once-per-pass on ANY `consumedHead` movement**, INCLUDING trailer-only progress with no
  delivered record — matching today's "publish if the head moved" (`:3813`); it is NOT gated on an accepted
  record. (borrow can advance the head over a trailer and then return pending.)
- **ERROR-body leniency PRESERVED (not a behavior change):** Stage 3 keeps the current ERROR handling — accept the
  record, read `errorCode` only if the fixed body is present, surface the command-error, advance+EOS, release
  (`:3610-3630`). Adding fatal-malformed validation of the ERROR body (min-size/protocol, as the retired tuple
  path did) is a behavior change, OUT of scope.
- **fatal-malformed batch decode is bounded by the existing cursor:** `HomerDecodedTupleCursorNext` has no
  per-tuple bounds checks (`homer_decoded_tuple_abi.h:180-220`), so tri-state fatal-malformed covers the
  currently-DETECTABLE malformations (envelope, cursor-init, command-seq mismatch); full per-tuple bounds
  validation is a PRE-EXISTING cursor gap, out of Stage-3 scope.

**Terminal-abandon** (§11.1) — refined from refutation:
- Triggered at EVERY unknowable/broken-state edge, not just the `:5022` timeout: post-publish transport/identity/
  protocol failure (`:4779`), ACK correlation failure (`:4875`), and fatal-malformed result decode. All take the
  same transition.
- Abandon = **mark `sqlSessionTerminal` ONLY; it PRESERVES outstanding ownership** — it does NOT clear
  `outstandingStartCommandSequence` or release role-1 (the START may still be in flight; the DPU could still touch
  the slot). Close then correctly refuses lifecycle-CLOSE while ownership remains and falls to the safe
  setup-close/DOCA teardown (`:2300-2331`), exactly as today.
- Core `start` must **check `sqlSessionTerminal` and refuse** (today it does not, `:4531`; no-reuse depends on
  pgbench suppressing commands `:9297` — the core makes it robust regardless).
- The raw-result lease is a pointer INTO `byteStorage`, which dataplane-close frees (`:4462-4472`); the core must
  hold no outstanding borrow across close/abandon (it is borrow→decode→release within one poll, so this is an
  assertion, not a new mechanism).

**C. Typed handle** `HomerSqlSession` (opaque, aliases the core). Thin typed forwarders cast to the core; wrong-op
-wrong-kind is a COMPILE error (owner decision, §13.1). Zero runtime cost. Frontend adapter holds/deals in the
typed handle; basebackup's typed handles come in 3.5.

**D. Transport interface + thin adapter.** The transport "module" is a LOGICAL seam for Stage 3 — the §12.2
interface HEADER that the core calls — whose IMPLEMENTATION (the Stage-2 `SqlTransport*` facade + the transport-leaf
helpers) STAYS in `homer_client.c` (NOT hoisted to a separate file/`.o` this stage; see the D-scope note under F).
`homer_client.c`'s SQL entry points become the thin **frontend adapter**: options→intent projection, `malloc`,
error-string, typed-handle open/close. The shared leaves stay basebackup's too — basebackup keeps calling them
(carrier shared) until 3.5. Physically hoisting transport into its own file is a later, optional cleanup.

**E. Process-shared DOCA device CONTEXT.** Replace per-carrier `dpuDocaDev` (`:125`) with ONE process-owned
device (the current sole opener is `HomerClientDpuOpenDevice:794`→`HomerDpuFrontendOpenDevice:875`; per-session
acquisition at SQL `:1945`, BB-send `:2566`, BB-recv `:2941`).

**SCOPE (re-review resolution): the context governs ONLY the client-session-export path** (the
`HomerClientBaseBackupStream` streams — SQL + BB send/recv). The **frontend agent's device ownership is SEPARATE
and UNCHANGED**: it keeps its explicit `HomerDpuFrontendOpenDevice → N mmaps → CloseDevice` lifetime
(`homer_frontend_agent.c:1545`/`:1772-1786`) — the public `OpenDevice`/`CloseDevice` primitives and their
explicit-owner contract are NOT altered (converting the agent to mmap-refs would make its trailing `CloseDevice` a
double-free). The context is a NEW layer that USES the device-scoped primitives internally, not a replacement of
their semantics. **Acquisition switches from the combined opener to the device-scoped variant:** today
`HomerClientDpuExportMmap:1016` calls `HomerDpuFrontendExportRegion` (opens+closes a device per call, and closes
the device directly on export failure `:853-861`); the context instead opens the shared device once
(`HomerDpuFrontendOpenDevice`) and exports each session's region via `HomerDpuFrontendExportRegionOnDevice` (the S2
API `:319-334`, exactly as the agent does), so a per-session mmap-create failure destroys only that mmap and
releases the constructing hold through the context (never a direct device close).

Requirements (VERIFIED sites):
- process-wide (not per-thread — pgbench `-j` opens N×/process today, the DOCA-undefined hazard this fixes);
- **refcount = one ref per live OR intentionally-retained mmap** (`dpuDocaMmap` stays PER-SESSION, `:126`).
  **PLUS a transient "constructing" hold (refutation):** `HomerDpuFrontendOpenDevice` returns a live device with
  ZERO mmaps (`:875-900`; the frontend agent holds exactly that before its first export,
  `homer_frontend_agent.c:1545`), so the acquire path must hold a device ref DURING open-device+first-mmap-create,
  converting it to the mmap ref on success and RELEASING it (closing the device if last) on mmap-create failure —
  the ref cannot be "per-mmap" alone or the open-but-no-mmap window has no owner;
- **failed-close retains the FULL resource set, not just a ref (refutation):** the unacked-CLOSE path (`:4450-4460`)
  returns before mmap-destroy/dev-close/buffer-free, and the exported buffer must stay mapped until mmap-destroy
  (`remote_execution_client.h:330`). The retained set = mmap + its `dpuExportBuffer` + `dpuSetupPayload` + the DOCA
  export blob + the device ref (the failed-close return at `:4450` precedes BOTH `free(dpuSetupPayload)` and
  `free(dpuExportBuffer)` at `:4470-4471`). **Accept that this is a process-lifetime tombstone:** the SQL adapter clears `commandDpuStreamOpen`/
  identity even on dataplane-close failure (`:2404-2411`) with no library-owned later reclaim (current code
  delegates to process teardown, `:4450-4460`), so the tombstone pins the shared device open for the process. That
  is benign and matches today's per-session leak-to-teardown — NOT a new leak, just shared;
- **mmap-destroy failure must NOT drop the last ref / close the device (refutation):** current code ignores
  `doca_mmap_destroy` results (`:988`/`:4462`); the "all mmaps before device" invariant means a failed destroy
  keeps its ref (device stays open) rather than closing the device with a live mmap under it;
- per-session close becomes **mmap-only** (`:4462-4464` stays; `:4466` dev-close MOVES to the context's
  last-ref transition);
- thread-safe COLD path only (serialize device/mmap create+destroy+ref transitions+last-close as ONE sync domain;
  VERIFIED no client data-path use of `dpuDocaDev` — poll/credit use mapped ring pointers `:3767-3815` — so the
  DATA path stays lock-free);
- import-safe (DPU import keys on generation/client/export-id on its OWN device — no host-device identity on the
  wire), so ONE host device serving N sessions changes nothing the DPU sees.

**F. Multi-object archive** (`citus-dbcomm/Makefile`). `libhomer_client.a` archives only `homer_client.o`
(`:136-137`, `ar rcs $@ $<` = first prereq only). Add `homer_session_core.o` as a SEPARATE member (compile rule
mirrors `:118-134`; archive rule must enumerate BOTH objects, not `$<`). **CORRECTION (refutation): object
separation does NOT isolate the core from the backend.** The backend references basebackup symbols from
`homer_client.o` (`basebackup_homer.c:273`/`:299`/`:353`/`:465`) and links the archive; once Stage-3 SQL functions
in that same `homer_client.o` call external core symbols, the linker resolves them by pulling `homer_session_core.o`
into PostgreSQL too. So the core WILL link into the backend (as dead code until Stage 4) — which is why the core
being **postgres-INDEPENDENT is MANDATORY, not optional** (VERIFIED achievable: the intent header, completion
lease/view, completion ABI, and decoded-tuple ABI are all `_t`/stdbool-only, no c.h aliases). Separate objects are
still worth it for build clarity, but the isolation is provided by postgres-independence, not by the object split.

**D scope note (refutation):** the "transport module" is a LOGICAL seam — the §12.2 interface header — whose
IMPLEMENTATION STAYS in `homer_client.c` for Stage 3 (only the CORE moves to a new file). This keeps the stage to
two archive objects (`homer_client.o` ↔ `homer_session_core.o`, which cross-reference each other — static linking
resolves the cycle) and avoids a third transport `.o` that F would otherwise have to enumerate. Physically hoisting
transport into its own file is a later, optional cleanup.

**Traps to honor (codex-verified):** `completionLease.slotIndex` (`:66`) is B-token leakage in an A object
(hardcoded 0 `:4790`) — core drops it; `serviceSessionId`/`commandDpuStreamOpen` are transport-SOURCED but
core-OWNED (origin ≠ ownership); the completion timeout (`:5022`) currently returns false without latching
terminal — abandon fixes it; the carrier's `receiveExpectedOrdinal`(B)/`receiveExpectedCommandSequence`(A) are
advanced TOGETHER today (`HomerClientSqlResultAdvanceCommandIdentity`), so the split fn updates one field on each
side of the boundary.

#### 12.5a-impl — implementation-phase findings + decisions (2026-07-18, IN PROGRESS)

**Component A (sink-lease) — IMPLEMENTED + refuted-clean + fixed.** `homer_byte_ring_sink.h` now splits the fused
`HomerByteRingSinkDrain` into `HomerByteRingSinkBorrow` (pure framing, carries the TORN-HEADER PROOF verbatim,
skips fully-published wrap-gap trailers internally and reports it via `*skippedTrailer`, returns the next whole
record in a `HomerByteRingSinkLease` without callback/advance) + `HomerByteRingSinkRelease` (the single credit
release-store, GUARDED by `head == lease->recordStart`), with `HomerByteRingSinkDrain` kept as a byte-for-byte
compat wrapper (borrow → callback → release; `madeProgress` = trailer-skip ∨ release).
- **Refutation finding (codex, VERIFIED by me against the old body, FIXED):** the lease's `recordBytes` must be
  **`uint64_t`, not `uint32_t`.** The pre-split loop computed `recordBytes` as a `uint64_t` local and used it at
  FULL width for both the `deliveredObjectBytes` accumulate AND the `consumedHead` release-store — it truncated to
  `uint32_t` ONLY at the `validateAndDeliver` argument. A `uint32_t` lease field silently narrows the
  accounting/credit for a malformed record advertising `payloadBytes` near `UINT32_MAX`. Unreachable in today's
  MiB-scale rings (the geom-gap branch defers such a record long before delivery — BB ring is 8 MiB, SQL result
  ring smaller), but it is a latent trap in this shared primitive and a divergence from the old defined behavior
  the compat wrapper promises to reproduce. FIX = widen the field to `uint64_t` and cast to `uint32_t` only at the
  callback call site (exactly as the old code did). NOT the alternative (reject `>UINT32_MAX`) — that is a
  behavior change §12.5a explicitly scopes out.
- Refutation also confirmed, per-case, that every requested normal geometry (nothing-new, min/geom-gap
  skip/pending, no-torn defer, callback-false, accepted non-EOS/EOS, and all trailer-skip/multi-record
  combinations) matches the old loop byte-for-byte; the Release guard never wrongly no-ops for current callbacks
  (BB touches only `receiveExpectedOrdinal`; SQL touches identity/capture — neither writes `consumedHead`); the
  "callback mutates `consumedHead` via ctx → guard no-ops" edge is the guard doing its job and is unreachable.

**DECISION (owner-improvised, recorded): the Stage-3 core EMBEDS the carrier; transport ops take the CARRIER, not
the session.** The core `HomerSession` struct embeds `HomerClientBaseBackupStream commandDpuStream` as its
transport substrate (one allocation, exactly like today's `HomerClientSession`), and every §12.2 transport op
takes `HomerClientBaseBackupStream *carrier` (+ explicit scalar inputs / out-params), NOT the whole session — so
component B/transport never sees A-state. Rationale: the carrier STAYS shared with basebackup in Stage 3 (§12.5a;
3.5 dissolves it into the shared context), so embedding-and-passing-carrier is the minimal clean seam. Rejected
alt: adapter holds the carrier and threads it into core ops — needless indirection when the core is the natural
owner (mirrors today's embedding).

**DECISION (USER, 2026-07-18): the typed handle is OPAQUE + HEAP-OWNED (literal §13.1), NOT a transparent
by-value wrapper.** `HomerSqlSession` is a DISTINCT INCOMPLETE struct tag (never defined as a real struct in the
header); the core `HomerSession` struct is FULLY PRIVATE to `homer_session_core.c`. `HomerSqlSessionOpen()`
`malloc`s the core and returns the handle; `HomerSqlSessionClose()` frees it. This forced a real design fork,
surfaced to the user because it touches the GATE harness: **pgbench embeds `HomerClientSession homer_session` BY
VALUE** in `CState` (`pgbench.c:711`) and passes `&st->homer_session` at every call site (`:3681`/`:3905`/`:3972`/
`:4077`/`:9338`/`:9416`). Opaqueness and caller-owned-by-value are mutually exclusive in C (an incomplete type has
no known size), so the literal-§13.1 choice moves pgbench off by-value onto a heap-held `HomerSqlSession *` and
RESHAPES the public API (open RETURNS a handle instead of filling a caller struct). Rejected alt (transparent
distinct-type wrapper, caller-owned by-value): smaller gate-harness blast radius and still delivers §13.1's
compile-time op-legality (distinct type per kind ⇒ wrong-op-wrong-kind is a compile error), but sacrifices
opaqueness (callers keep seeing/poking core fields) — the user chose the clean opaque end-state over the smaller
diff. **Consequences to honor:** (a) the core owns its allocation (adapter's "malloc" per step 2 is the session
alloc) and its free on close; (b) the public API keeps the `HomerClient*` names but is re-typed to
`HomerSqlSession *` handles (adapter maps public names → core ops; the options→intent projection stays adapter-side);
(c) pgbench's `CState.homer_session` becomes a pointer, opened via the returned handle and freed at close — a
mechanical ~7-site change in the validated gate harness that MUST be re-validated by the gate.

**⚠ IDENTITY IS AN EXPLICIT CORE-SUPPLIED ARG, NOT read from the carrier (VERIFIED — corrects an earlier wrong
assumption).** The transport ops that need the session identity — `poll_completion` (the role-6 identity check +
error strings, `homer_client.c:1650`) and `lifecycle_close` (stamps `request->serviceSessionId`, `:1698`) — take
`serviceSessionId` as an EXPLICIT scalar the core passes (`core->serviceSessionId`). They must NOT read it from the
carrier: in the SQL open path the carrier's `serviceSessionId` is deliberately left **0** (`homer_client.c:1875`;
the identity is stored only in the session at `:2128`), whereas the BASEBACKUP paths DO set
`carrier->serviceSessionId = dpuBridgeGeneration` (`:2519`/`:2893`). So `carrier->serviceSessionId` means different
things per opKind and is 0 for SQL — a `poll_completion` that read it would compare every SQL completion against 0.
(This strikes the earlier note that claimed the check "comes from `carrier->serviceSessionId (:86)`" — that field
is basebackup-only.) The result-consume ops need NO serviceSessionId: their identity check is the credit line's
generation/ringIndex (`:3770`), which IS carrier-resident.

**Result-consume op set — SETTLED (codex refinement, all four corrections VERIFIED in code by me):** a PASS
BRACKET mirroring the fused poll's once-per-pass structure, because `producedTail` is a per-PASS snapshot (resolved
once at `:3767`, validated `:3770`/`:3781`), NOT a per-record quantity:
- `ResultBeginPass(carrier, *pass, err) -> bool`: resolve + validate the acquire-gated produced frontier ONCE;
  snapshot it (+ consumedHead-at-begin) into the opaque `pass`. false = transport identity/regress failure
  (`:3778`/`:3786`) → core terminal-abandons.
- `ResultBorrowNext(carrier, *pass, *lease, err) -> {RECORD | NOT_READY | VISIBILITY_PENDING | FATAL_MALFORMED}`:
  FOLDS framing (sink `Borrow` over the snapshot) THEN envelope-validate+ordinal internally (both B-owned; a
  separate op is a per-record cross-call for no boundary gain). Envelope mismatch after a successful (acquire-gated,
  fully-published) Borrow is DETERMINISTIC → `FATAL_MALFORMED`, not pending (this closes the old "retry-forever on
  malformed" bug, `:3530`/`:3607`). `VISIBILITY_PENDING` stays for Borrow's partial-record frontier case only. The
  lease SURFACES the typed transport header (via the embedded `HomerByteRingSinkLease.header`) plus a normalized
  `lease->eos`. **CORRECTION (2026-07-18, code-verified — an earlier note here was WRONG):** the DATA/EOS/ERROR
  `recordKind` lives in the TRANSPORT header (`CitusTupleSinkTransportHeader.recordKind`, `homer_tuple_abi.h:238`,
  confirmed by its own comment `:202-203`), NOT in the payload batch header (`HomerDecodedTupleBatchHeader`,
  `homer_decoded_tuple_abi.h:67-76`, which carries only `commandSequence`/`tupleCount`/`attributeCount`). So the
  lease DOES expose `recordKind` — the core branches on it (`:3557`/`:3610`) to pick the DATA/EOS batch-decode path
  vs the ERROR-record path. Both sides recorded: codex's original draft put `recordKind` in the lease (RIGHT); I
  rejected it claiming DATA/ERROR is decoded from the batch header (WRONG — that misread which header owns the
  field). The `eos` bool is a SEPARATE signal, normalized from `header->flags & CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS`
  (`:3626`/`:3695`) — the last-record-of-command marker that can ride a DATA, EOS, or ERROR record and drives the
  identity reset + pass stop; it is distinct from `recordKind == EOS` (a 0-tuple batch, `:3528`).
- [core decodes the payload batch → command-seq (`:3660`) + capture (`:3686`) + ERROR (`:3610`, short-body LENIENT,
  accepted not fatal); tri-state, but visibility-pending is UNREACHABLE post-valid-lease so decode is effectively
  success/fatal-malformed]
- `ResultAcceptAndRelease(carrier, *lease)`: INFALLIBLE; core advances/resets its command-seq FIRST (nothing
  between), then this op advances/resets `carrier->receiveExpectedOrdinal` from `lease->eos` and releases
  consumedHead — merging accept+release makes "release without transport accept" unrepresentable and preserves
  A-command-seq → B-ordinal → B-release order (`:3694` before `:338`). EOS resets ordinal→0 / command-seq→1 (`:3470`)
  and the core must STOP the pass after an EOS accept (`:340`), not consume the next command's record.
- `ResultFinishPass(carrier, *pass, deliveredRecordBytes, *streamState)`: called EXACTLY ONCE after every successful
  BeginPass (incl. pending/fatal exits, so a trailer-only credit flush is never skipped). Accumulates bytes into
  the carrier (`:3809`), publishes DPU credit iff consumedHead moved this pass (`:3813`, computed from the pass
  snapshot — NOT from a remembered skippedTrailer), and returns the B-owned terminal classification
  {ACTIVE | DRAINED_CLOSED | DRAINED_FAILED} from `dpuLastCreditEpoch != 0 && drained && entryState CLOSED/FAILED`
  (`:3845`). This B-owned terminal detection was missing from the first op draft.

**Verified current-line map (post Stage-2 `bc4f2d02a`; codex-explore + my struct/facade reads).** Core A-fields
(all move to core unless noted), `HomerClientSession` (`remote_execution_client.h:187-239`): `serviceSessionId:189`,
`serviceSessionIndex:190`, `lastConsumedCompletionEpoch:191`, `completionSessionGeneration:197` (VESTIGIAL, no
writer, always 0), `completionLeaseActive:198`, `completionLease:199` (drop `.slotIndex:66`), `lastIssuedCommandSequence:206`,
`outstandingStartCommandSequence:207`, `commandDpuStream:225` (STAYS transport — the embedded carrier),
`commandDpuStreamOpen:226`, `sqlSessionTerminal:238`. Carrier split: `receiveExpectedOrdinal:165` STAYS (B),
`receiveExpectedCommandSequence:177` MOVES to core (A). `sessionUID` is in options (`:266`), never retained in the
session — new core field (last open read = OPEN stamp `homer_client.c:2100`; descriptor stamps `:1993/:2026/:2054`).
The `memset(session,0)` `transport_open` must NOT do is at `homer_client.c:1838`. Facade defs
`homer_client.c:1574/1595/1613/1674/1686/1717`; call sites Start `:4562/:4675`, Peek `:4779`, Ack `:4919`, Close
`:2314/:2390/:2405`. Fused result poll `:3714`; deliver callback `:3533` (envelope+ordinal B `:3555/:3560`; ERROR
`:3610`, cursor `:3640`, command-seq `:3660`, capture `:3686`); ordinal+command-seq reset together `:3470`, advance
together `:3487`, fused advance called `:3625/:3694`. E device: opener `:794` (`doca_dev_open:820`),
`HomerDpuFrontendOpenDevice:875`, combined `HomerDpuFrontendExportRegion:836` (opens `:853`, closes-on-fail `:860`,
transfers `:864`), combined destroy `:1004`, device-scoped `HomerDpuFrontendExportRegionOnDevice:919`, carrier
`HomerClientDpuExportMmap:1016` (uses combined `:1019`), acquire sites SQL `:1945` / BB-send `:2566` / BB-recv
`:2941`, per-session teardown mmap-destroy `:4464` + dev-close `:4468`, failed-close return `:4460` before free
`:4470`. **NEW E requirement (codex, VERIFIED):** `HomerDpuFrontendDestroyRegionExport:988` is `void` and DISCARDS
`doca_mmap_destroy`'s result (`:992`), so the device context cannot learn whether a destroy failed to decide
whether to keep its device ref — the context needs a status-returning destroy primitive (or a direct checked
destroy). Frontend agent's SEPARATE device ownership (UNCHANGED): open `homer_frontend_agent.c:1546`, exports
`:1554/:1568`, destroys `:1775/:1781`, dev-close `:1786` — never uses the carrier path.

**Header refutation ROUND 1 (2026-07-18, codex-explore; every finding VERIFIED in code by me, then FIXED).** The
two Stage-3 contract headers (`homer_session_transport.h`, `homer_session_core.h`) were written and then attacked
before any body. Codex CONFIRMED clean: include closure is frontend-safe (reaches only stdint/stdbool + ABI
headers; DOCA stays `void *` in the carrier), all named types are reachable, `HomerByteRingSinkLease.header` is a
typed `CitusTupleSinkTransportHeader *`, the SQL-carrier-`serviceSessionId`-is-0 asymmetry holds, and
`HomerSqlCommandSpec` matches the START inputs. Defects found + fixed (both sides recorded):
- **CLOSE CONTRACT WAS WRONG (the important one).** My `HomerSqlSessionClose` doc said a terminal session skips the
  lifecycle CLOSE. FALSE: only the SEMANTIC close (`CLIENT_SQL_SESSION_CLOSE`, a START) is terminal-gated
  (`homer_client.c:2251`); the LIFECYCLE `CLOSE_SESSION` is gated on ROLE-1 OWNERSHIP/READINESS (`:2387`), and the
  prior terminal-skip guard was DELETED at `:2360-2385` precisely because the happy path latches DO_NOT_REUSE→
  terminal yet MUST still issue lifecycle close to reclaim the service session. Fixed: the two closes documented as
  distinct gates (terminal ⇒ skip semantic only; role-1 ownership ⇒ gate lifecycle).
- **Completion-apply + tuple-contract ownership was MISSING (design omission).** pgbench captures the tuple-view
  contract FROM the completion during apply (`pgbench.c:3800-3803`) and RETAINS it across the ACK (the drain
  outlives the lease). Fixed: added `HomerSqlSessionApplyCompletion` (latch terminal + capture+retain the
  per-command contract + report result-stream-bound); `ConsumeResult` now takes NO contract arg (uses core-retained
  state); `StartCommand` resets the per-command capture.
- **`serviceSessionIndex` had no accessor** (pgbench logs it at open, `pgbench.c:9428`) → added
  `HomerSqlSessionServiceSessionIndex` (this was the "first compile break").
- **`DRAINED_FAILED` was collapsed** into `streamComplete` (transport FinishPass distinguishes it, `:3849`) → added
  `streamFailed` to `HomerSqlResultOutcome`; `ConsumeResult` terminal-abandons on it.
- **`errorCode` validity unrepresentable** (fixed body may be absent, `:3621`) → added `haveErrorCode`.
- **`AcceptAndRelease` "infallible" overclaimed:** `HomerByteRingSinkRelease` silently no-ops when `consumedHead !=
  recordStart` (`:470-481`). Kept no-failure-return (true for the single-consumer pass discipline) but documented
  the guard as a stale/dup backstop that the body asserts/traces — not a supported path.
- **`transport_open` intent-field legality** documented (SQL hardcodes opKind/txPolicy/lane/metadata/scope at
  `:2105-2116`; projects only the identity fields) and the `docaDevicePci` one-device-per-process rule cross-linked
  to component E; **credit-arm ordering** strengthened (both `receiveExpectedOrdinal=0` AND
  `receiveExpectedCommandSequence=1` before `ArmInitialResultCredit`, mirroring `:2161/:2162` before `:1982`).
- **`slotIndex` B-token (SCOPED OUT, accepted):** the caller-facing `HomerClientCompletionLease` still carries the
  vestigial hardcoded-0 `slotIndex` (`:66`/`:4790`). "Core drops slotIndex" is honored for the core's INTERNAL
  state; retyping the caller lease to a slotIndex-free view is churn for a 0 field and deferred (the user chose the
  clean opaque handle over eliminating every smell this stage). Callers MUST NOT read `lease->slotIndex`.

**Header refutation ROUND 2 (2026-07-18, re-review of the round-1 fixes; all VERIFIED in code by me, then FIXED).**
Round 1's corrections were themselves unreviewed claims; round 2 caught three MORE blockers, the first serious:
- **STOP CONDITION WAS WRONG (serious).** My `ConsumeResult` doc said "loop until `streamComplete`." The role-7
  stream is PERSISTENT — it resets command identity at each in-band EOS and stays active (carrier comment
  `remote_execution_client.h:171-175`; reset `homer_client.c:3470`/:3482) — so `streamComplete` (whole-stream
  CLOSED/FAILED) only fires at SESSION close, while the per-command boundary is `sawEos`. pgbench stops on
  `sawEos || streamComplete` (`pgbench.c:3703`). Looping on `streamComplete` alone SPINS FOREVER on every
  successful row-producing command. Fixed: `sawEos` documented as the per-command boundary, `streamComplete`/
  `streamFailed` as whole-stream; stop condition is `sawEos || streamComplete`.
- **AUTO-ABANDON ON `streamFailed` WAS A NEW DEFECT.** The authoritative result failure is carried by the ROLE-6
  completion (the service manufactures FAILED/TUPLE_SINK_FAILED, `tuple_sink_service_process.c:45576`/:45586,
  possibly with the backend still alive `:45704`). Today CLOSED/FAILED collapse into `streamComplete`, the drain
  finishes, and the completion carries the failure (`pgbench.c:3861`). Abandoning on `streamFailed` would leave the
  completion unACKed, pin role-1, and suppress close — breaking the designed failure/cleanup path. Fixed:
  `streamFailed` is INFORMATIONAL; abandon reserved for UNKNOWABLE states (BeginPass failure / FATAL_MALFORMED /
  timeout / ACK-correlation).
- **INITIAL-IDENTITY OWNERSHIP contradiction + a FALSE citation of mine.** My transport `ArmInitialResultCredit`
  doc called `receiveExpectedOrdinal=0` "A-owned," contradicting the A/B split (ordinal is B — transport advances
  it in AcceptAndRelease). And my `:1982` citation was descriptor GEOMETRY, not the credit arm (VERIFIED: `:1982`
  is `recordGeometry`; the real init+arm are `HomerClientSqlResultBeginCommandIdentity:2161` then
  `HomerClientDpuPublishReceiveConsumedHead:2162`). Fixed: ordinal init = B, command-seq init = A, both before the
  arm; citations corrected to :2161/:2162 in both headers.
- Also confirmed by round 2: the CLOSE split, by-value contract retention (`CitusTupleViewContract` is pointer-free,
  `homer_tuple_abi.h:121`/:127; Ack destroys the lease `:4924`), `haveErrorCode`, the index accessor, vestigial-0
  `slotIndex` (no SQL caller reads it; ACK correlates epoch/generation `:4875`), and the stale-release backstop are
  all correct. Added: `ApplyCompletion` IDEMPOTENCY under a repeatedly-returned active lease (`:4758`) is now
  documented (capture guarded, terminal latch monotonic).

**Header refutation ROUND 3 → SETTLED (2026-07-18).** Round 3 VERIFIED all four round-2 substantive corrections
correct (per-command EOS stop, informational stream-failure, split identity ownership, repeatable Apply) and found
only THREE stale-WORDING contradictions (leftover "loop until `streamComplete`" text at the outcome-struct header,
the result-consume section banner, and the Apply doc, contradicting the corrected `sawEos || streamComplete`
contract; a too-strong Peek ACK-order summary that ignored the STARTED-vs-terminal drain/ACK ordering difference,
`pgbench.c:3715`/:4024/:4077; and an Apply-idempotency clause that could be read as skipping its `*outResultStreamBound`
write). All three fixed (pure comment-consistency edits, no semantic change) and re-grepped clean. Round 3's verdict:
"no further semantic blocker is evident." **The two Stage-3 contract headers (`homer_session_transport.h`,
`homer_session_core.h`) are SETTLED** — sound to implement bodies against. Refutation arc: 8 → 3 → 3-wording,
descending severity = genuine convergence.

**DECISION (USER, 2026-07-18): SPLIT the Stage-3 landing into TWO validated pushes** (supersedes the earlier
"one push" — the split isolates the sole behavior change so a validation failure localizes to one of two things,
not both):
- **PUSH 1 — behavior-preserving A/B extraction:** components B (core `homer_session_core.c`) + C (typed handle) +
  D (transport op bodies extracted into `homer_client.c` + thin adapter rewire) + F (multi-object archive) + the
  pgbench `CState.homer_session` -> `HomerSqlSession *` pointer migration. The DOCA DEVICE MODEL STAYS UNCHANGED
  (per-session device, as today -- `transport_open`'s body keeps the current per-session acquisition; NO E
  consolidation). Validate: gate + four-role basebackup + DPU TCP smoke -> commit.
- **PUSH 2 -- the behavior change, isolated:** component E (process-shared refcounted DOCA device context;
  status-returning destroy; device-scoped acquisition; mmap-only per-session close). Validate again -> commit.
- Execution (REFINED 2026-07-18 during body grounding — supersedes the earlier "core.c first, THEN a D worker for
  transport bodies+adapter"): **the main agent writes the ENTIRE citus side itself** (core.c + ALL `HomerSqlTransport*`
  op bodies + the adapter rewire in homer_client.c + the carrier-field move + F), and delegates ONLY the pgbench
  `CState.homer_session` -> `HomerSqlSession *` migration (postgres-citus repo, fully isolated, cleanly mechanical
  against the final public API) to a codex-worker; then main-agent refutes that diff. WHY the boundary moved: two
  reasons surfaced only when the bodies were grounded. (1) **The result-consume pass bracket is ONE decomposition
  spanning both files** — today's fused `HomerByteRingSinkDrain`+`HomerClientSqlResultDeliverDecoded` splits into
  BorrowNext (B, envelope-validate) / core-decode (A) / AcceptAndRelease (B, ordinal+release), with the A/B line
  running THROUGH the old fused body. Splitting its authorship (core's drive vs the four transport pass-bracket op
  bodies) would put a seam-mismatch risk on the single most bug-historied path in the file (run-39 "reject every row
  forever"). (2) Keeping all citus-tree writes in one hand sidesteps the Safety-Rule-5 serialization dance entirely
  (the worker now touches only the postgres-citus tree). The plan's INTENT is preserved (main = design-sensitive +
  coupled; worker = isolated mechanical); only the file boundary moved.

**PIVOTAL STRUCTURAL FINDING (2026-07-18, body grounding — the settled header under-specified this).** The two
Stage-3 headers fixed the LOGICAL A/B contract but silently assumed the PHYSICAL linkage was free. It is NOT: the
core is a SEPARATE TU compiled POSTGRES-independent into `libhomer_client.a`, and **nearly every helper it needs is
file-`static` in `homer_client.c` and declared in no header** — `HomerClientSetError:231` (119 refs),
`HomerClientCommandName:319` (8), `HomerClientCopyCommandCompletion:435` (4),
`HomerClientMaterializeLegacyCompletionFromView:479` (3), `HomerClientBuildCompletionViewFromLegacy:545` (2), plus
the DPU submit/export/atomic statics. A separate TU cannot call a file-static. **LINKAGE POLICY (decided, recorded):**
- **Expose** `HomerClientBuildCompletionViewFromLegacy` (remove `static`, declare in `remote_execution_client.h`): the
  core's `PeekCompletion` owns the completion-VIEW build (A, per the settled header, transport.h:256) and this is the
  one A-helper it must reach that is NOT behind a transport op. Safe: it operates on frontend-safe types and lives in
  `homer_client.o`, which is already in BOTH link contexts (external client lib AND the backend, via basebackup's
  refs).
- **Core-local `static` duplicate** for `HomerClientSetError` ONLY (a ~10-line vsnprintf wrapper): it is file-static
  in homer_client.c, so the core carries its own `SessionSetError`; duplicating it avoids touching 119 call sites
  and keeps the core TU self-contained (two same-named file-statics in different TUs do not collide). **CORRECTION
  (2026-07-19): `HomerClientCommandName` is NOT static** — it is public (declared `remote_execution_client.h:289`),
  so the core calls it DIRECTLY, no duplicate. (An earlier draft of this note wrongly listed it among the
  duplicates; the code was always correct — core.c uses the public symbol.) The monotonic clock is likewise
  duplicated (`SessionMonotonicMillis`, mirroring the static `HomerClientMonotonicMillis`).
- **Everything else via the `HomerSqlTransport*` op surface.** The completion COPY/MATERIALIZE helpers stay `static`
  in `homer_client.c` (used only by the B transport op `PollCompletion`@1661 which does the copy internally, and by
  the old `Try` adapter); the DPU submit/export/atomic statics stay behind the transport op bodies. The core touches
  NO carrier atomic directly — every frontier/credit/consumedHead atomic is inside a B pass-bracket op; the core only
  decodes borrowed lease bytes (plain reads, ordered by the acquire in BeginPass).

**`receiveExpectedCommandSequence` MOVE — CONFIRMED SAFE (grep-verified 2026-07-18).** Every reference is in the SQL
path (`homer_client.c:3471/3488/3660/3675`, all inside the SQL identity/deliver helpers); basebackup's receive NEVER
reads it. So the settled header's "core owns its OWN command-sequence" is a genuine ownership move with zero
basebackup risk: it migrates into the private `HomerSession` core struct (removed from the carrier at
`remote_execution_client.h:177`), while `receiveExpectedOrdinal:165` STAYS carrier-resident and B-owned (basebackup
shares it for wrap-gap detection). This shrinks `sizeof(HomerClientBaseBackupStream)` by 8 bytes — a coordinated
recompile of both trees, which Push 1 does anyway.

**POST-VALIDATION CLEANUP — DONE (2026-07-19, after Push-1 validated).** Both items below were completed in
`homer_client.c` (behavior-neutral; −1309 net lines): the `#if 0` dead-reference blocks removed, `<assert.h>` +
all libc `assert()`s removed, and the AcceptAndRelease stale-release guard replaced with a bounded
`HOMER_DPU_P2_DIAG`-gated trace (OFF in perf builds — the hot path no longer does an atomic-load-per-record). Proven
behavior-neutral by whitespace-normalized function-body diff vs the Push-1 commit (BeginPass/BorrowNext/Open/
PublishStart/PollCompletion byte-identical; FinishPass differs only by its removed null-check assert; AcceptAndRelease
only by the guard conversion) + clean rebuild/install and both consumers relinking. No re-gate (provably
behavior-neutral). Original deferral record follows.

**DEFERRED POST-VALIDATION CLEANUP (Push 1, 2026-07-19 — user decision: do these AFTER Push-1 validation passes; no hurry).** The citus-side surgery (worker + main-agent refutation) came back sound and BUILDS CLEAN (re-verified by the main agent: `HomerClientSession` the type removed, tree compiles as dbcomm with only pre-existing basebackup `-Wunused-but-set-variable` warnings). Two non-blocking hygiene items were consciously deferred so they do not perturb the validation candidate:
1. **Remove the `#if 0` dead reference blocks** left in `homer_client.c` by the extraction (old close orchestration ~:2339, old SQL-result A-helpers `BeginCommandIdentity`/`AdvanceCommandIdentity`/`DeliverDecoded`/`HomerClientSqlResultDpuSinkCtx` ~:3648, old completion helpers ~:4973, the six removed public wrappers `StartCommandWithCompletion`/`StartCommand`/`StartAndWaitCommand`/`TryCommandCompletion`/`PollCommand`/`WaitCommandCompletion` ~:5163-5646). They are fenced out of the active object (verified: build succeeds with the carrier field + `HomerClientSession` type both removed, so every dangling `receiveExpectedCommandSequence`/`HomerClientSession` reference inside them is dead), but they now reference removed symbols and will not re-compile if re-enabled — pure reference cruft to delete once the extraction is validated.
2. **Remove / debug-gate the worker-introduced libc `assert()`s** in `homer_client.c` (`#include <assert.h>` at :25; asserts at ReleaseStartSlot :1758, AcceptAndRelease :4248/:4249, FinishPass :4266). **CONFIRMED active in the perf build** (the real compile line is `-g -O2` with NO `-DNDEBUG`, so libc `assert()` is not compiled out). The load-bearing one is AcceptAndRelease:4249 — an atomic-load-per-result-record on the gate's hot path, which contaminates any MEASURED throughput number (harmless to functional correctness: the guard is always-true in the single-consumer pass discipline, so it never fires). Post-validation, replace the stale-release guard with a `HOMER_DPU_P2_DIAG`-gated check+trace (matching the header's "asserts (debug) / traces the guard" intent, homer_session_transport.h:306-312) and drop the per-pass/per-command asserts, restoring homer_client.c to assert-free. Until then, DO NOT quote performance numbers from a build carrying these asserts.

**PUSH-1 VALIDATION ATTEMPT #1 (2026-07-19, run `candidate-20260719-004251`) — gate FAIL root-caused to an OPERATIONAL frontend-agent doorbell desync, NOT Push-1 code.** Full manual-runbook validation (build+install both trees, peer deploy, no DPU redeploy since host-only). Results:
- **DPU TCP transport smoke — PASS** (host↔DPU DMA + byte-ring; exactly the path the one DPU-rebuilt Push-1 header `homer_byte_ring_sink.h` affects — so component A's Borrow/Release split is validated on the DMA path).
- **Four-role selected-DPU basebackup — PASS** (23,259,288,507 bytes, 44,363 full laps of the 512 KiB ring → wrap proven; frontier balanced posted==retired+flushed). Exercises the shared carrier (post `receiveExpectedCommandSequence` removal), the refactored byte-ring consume core, and the peer OPEN/CLOSE transport — all green.
- **Selected-DPU command GATE — FAIL**, reproduced identically on a clean Rule-12 retry. Client-0 fails at SQL session OPEN (`peer async op connection became inactive`). farnet1 DPU raised `HOMER_EVENT ... event=peer_host_spawn_retired ... op=5`, preceded by SIX `DPU doorbell ATTACH rejected: status=4` at service start (before any client).
- **ROOT CAUSE (localized + verified, Push-1-INDEPENDENT):** `status=4 = HOMER_DPU_COMCH_SETUP_BAD_BRIDGE` (`homer_service_dpu_doorbell.c:688`). `HomerServiceDpuDoorbellValidateAttach` (`tuple_sink_service_process.c:46169`) accepts the frontend-agent doorbell ONLY if `HomerDpuDmaHasActiveHostMmapImport(engine, bridgeGeneration, clientInstanceId)` — so the farnet1 frontend agent (`citus.enable_homer_dpu_frontend_agent=on`, runbook §"PostgreSQL with frontend agent":329) must establish a host-mmap import with a MATCHING generation BEFORE its doorbell attaches (runbook :467: "doorbell attached before the first OPEN"). Six BAD_BRIDGE rejects ⇒ no matching import when the attach arrived ⇒ `TupleSinkServiceDpuBackendSpawnArmActive` (:21688, needs `doorbellFacts.attached`) stays false ⇒ gate `OPEN_COMMAND_SESSION` correctly rejected. This is a frontend-agent import/doorbell LIFECYCLE/ORDERING condition (consistent with the run's reported multi-restart supervisor churn), in the DPU-DMA-scheduler/doorbell/spawn-arm subsystem.
- **Why NOT Push 1 (all VERIFIED):** `homer_frontend_agent.c` + `homer_frontend_dma_lifecycle.c` untouched by the Push-1 diff; the `HomerDpuFrontend*` export helpers in `homer_client.c` untouched (grep of the diff empty); doorbell/spawn code does not include `homer_byte_ring_sink.h`; the two Push-1-EXERCISED workloads (basebackup, smoke) PASS; the gate PASSED on the base SHA (`7210421e0d1` "S6 Track A … validate -c1"); and the host SQL client's A/B logic is never reached (peer rejects OPEN before any transaction). **Conclusion: the gate FAIL does not implicate the Push-1 host-side refactor; it is an operational frontend-agent doorbell-attach desync to be resolved rig-side (clean, correctly-ordered frontend-agent arming: DPU service up → farnet1 postgres with the frontend-agent GUC → verify doorbell ATTACHED → then gate), then re-run the gate.** Push 1 remains UNCOMMITTED pending a green gate. (Also noted, checker-raw, not diagnosed: `teardown_checks` stage2b `peak_shards=0` FAIL on the basebackup-only interval — the checker's `bad_ledger` predicate may not apply to a no-host-service-shard session; unrelated to Push 1.)

**PUSH-1 GATE DESYNC — DEEP ROOT CAUSE (2026-07-19, user-directed investigation; VERIFIED in code + logs; Push-1-INDEPENDENT).** The gate BAD_BRIDGE is a frontend-agent doorbell/import GENERATION-STRANDING across a DPU-service restart:
- The frontend agent (bgworker in farnet1 postgres, `citus.enable_homer_dpu_frontend_agent=on`) mints `bridgeGeneration = (MyProcPid<<32) ^ time(NULL) ^ arena_ptr` ONCE per instance (`homer_frontend_agent.c:1516`), and `clientInstanceId = bridgeGeneration ^ const` (:1521).
- It exports the arena + spawn region and sends SETUP (the DPU DMA engine imports generation G) ONCE per instance (:1554-1579), THEN enters the doorbell steady state which, on a DPU restart, ONLY **reconnects the doorbell every second — it does NOT re-export** (:1622-1624). Re-export happens ONLY on the agent's OWN ERROR→proc_exit→bgworker-restart→fresh-generation path (:1526-1544); that path even notes the agent exits WITHOUT CLOSE, so "the DPU keeps a stale import" (:1537).
- The DPU accepts a doorbell ATTACH only if `HomerDpuDmaHasActiveHostMmapImport(engine, bridgeGeneration, clientInstanceId)` is already true (`HomerServiceDpuDoorbellValidateAttach`, `tuple_sink_service_process.c:46169`; reject = `BAD_BRIDGE`, status=4).
- **Therefore: restart the DPU service WITHOUT recycling the farnet1 frontend agent, and the agent's doorbell reconnects with its old generation G into a FRESH DPU that never imported G → BAD_BRIDGE forever → spawn arm never arms → gate OPEN_COMMAND_SESSION rejected.** Log proof (run `candidate-20260719-004251`, farnet1 DPU): the only IMPORTED generation is `17583322268413212` (5 arena exports accepted), but the doorbell ATTACH presents `4175513958737582` — two distinct bgworker lifetimes stranded across a DPU-service restart. The run's own Rule-12 DPU-service restarts (which do not cycle postgres) + the reported supervisor-state churn are exactly this trigger.
- **OPERATIONAL FIX — now lives in the OPERATOR RUNBOOK + HAZARDS, not here** (it is a runner-facing procedure, not part of this design): operator runbook §"DPU services" (the frontend-agent ⇄ DPU-service co-arming invariant) + §"Selected-DPU command gate" (the gate-readiness precondition), with the incident rationale in `farnet_operational_hazards.md` §11. In one line: whenever a DPU service is (re)started, recycle the farnet1 frontend-agent PostgreSQL (5433 only) so it re-exports, and verify the doorbell is attached with a generation matching the DPU's accepted import before the gate.
- **PUSH-1 VALIDATION COMPLETE — GREEN (2026-07-19, re-run under corrected co-arming).** Gate re-run `pgbench --homer --homer-dpu -c4 -j4 -t5` (20 tx) PASSED all four proofs: transport line present; 20/20 processed, 0 failed; 4 correlated `DPU backend spawn begin/COMPLETED` pairs (bridge_generation matching the agent's export `4157110881348830`); `peer_host_spawn_retired` ABSENT; 20 distinct decoded `abalance` values; `gate_check`→PASS `{transactions:20, spawn_pairs:4, decoded_values:20}`, `alarm_check`→PASS. The readiness precondition confirmed the frontend agent's export+doorbell both at gen `4157110881348830` matched the DPU's accepted import — proving the diagnosis + fix by construction (arming ORDER changed, zero code changed). Combined with attempt#1's smoke PASS + four-role basebackup PASS (same build/deploy, run `candidate-20260719-004251`), **all three required Push-1 workloads are green against the same artifacts.** Push 1 is CLEARED to commit (citus code + postgres pgbench + KB).

#### 12.5a-E-impl — Component E CONCRETE IMPL DESIGN (Push 2, 2026-07-19, grounded in current tree post-cleanup `f92d39025`)

Re-grounded against HEAD (codex-explore inventory + main-agent verification of every load-bearing site). The
§12.5a "E" REQUIREMENTS stand; this section is the concrete realization. **All client-tree line numbers below are
CURRENT** (the §12.5a "E" numbers predate Push 1's −1309-line cleanup and are stale — see the site-map correction
at the end).

**Two grounding findings that shrink the design vs the §12.5a "E" sketch (both VERIFIED in code):**
1. **The status-returning destroy is a LOCAL change, not a helper resignature.** Per-session teardown destroys the
   mmap and closes the device INLINE — `doca_mmap_destroy` at `homer_client.c:4240`, `doca_dev_close` at `:4244`,
   both results discarded — NOT via `HomerDpuFrontendDestroyRegionExport` (`:990`, mmap-only, `void`) or the unused
   combined `HomerDpuFrontendDestroyExport` (`:1006`). So "the context needs a status-returning destroy" = capture
   the return of the EXISTING inline `doca_mmap_destroy` at `:4240`. No `HomerDpuFrontend*` signature change. (The
   §12.5a "E" note calling `:988` the "combined destroy" was doubly wrong: `:990` is mmap-only and the teardown
   doesn't call it at all.)
2. **All three carriers switch AT ONCE via two SHARED functions** — the acquire wrapper
   `HomerClientDpuExportMmap` (`:1018`, called by SQL open `:2039`, BB-send open `:2494`, BB-recv open `:2869`) and
   the teardown body `HomerClientCloseBaseBackupStreamInternal` (`:4136`, reached by SQL via `DataplaneClose:1805`
   and by both BB directions directly). Changing these two is the WHOLE switch. **DECISION (owner-improvised,
   recorded): E covers all three carriers in Push 2** (matches the §12.5a "E" scope). Rationale beyond the plan's:
   SQL-only would require FORKING the shared exporter/teardown — MORE work than all-three, not less; and four-role
   basebackup is already the required Push-2 workload that covers the BB carriers. basebackup gains no *sharing*
   benefit (one stream/process) but a uniform one-device-per-process model and the death of the last combined
   open+close-per-call caller. BB's core/session migration remains a SEPARATE concern (Stage 3.5); Push 2 switches
   only BB's device ACQUISITION, not its session shape.

**⚠ ROUND-0 DESIGN REJECTED by refutation (2026-07-19, codex-explore refuting; all 4 blockers VERIFIED by me in
code + vendor headers). Both sides recorded.** The round-0 design (a narrow `Acquire`(open+`refcount++`)/`Release`
(`refcount--`+close) pair with a "transient constructing hold," mmap create/destroy OUTSIDE the lock, and
"frees+memset unchanged") was UNSAFE:
1. **Lock too narrow** — `doca_mmap_destroy` is "not thread safe even if thread safety is enabled"
   (`/opt/mellanox/doca/include/doca_mmap.h:109`) and distinct-mmap concurrent create/destroy on one device is
   NOT promised. Round-0 left the mmap create (`:948-971`) and destroy (`:4240`) OUTSIDE the lock. The plan
   (`:924`) requires serializing the WHOLE mmap lifecycle, not just the ref transitions. FIX: hold the mutex across
   the ENTIRE export wrapper body and the ENTIRE teardown device/mmap block.
2. **Destroy-failure frees live memory** — a failed `doca_mmap_destroy` returns `NOT_PERMITTED` when "memory
   deregistration failed" (`doca_mmap.h:118`), i.e. the mapping is STILL LIVE. Round-0 kept the ref but still ran
   `free(dpuExportBuffer)` (`:4247`) + `memset` (`:4248`) → live mapping over freed memory + lost handle. FIX: on
   destroy failure take the FULL-retain tombstone (no free, no memset, keep mmap+buffers+ref), report failure.
3. **Partial-export cleanup can leak an untracked mmap** — `HomerDpuFrontendExportRegionOnDevice` destroys its
   partial mmap but DISCARDS the result (`:977`); if that failed and this was the sole opener, an unconditional
   device close would run with a live mmap under it. FIX: checked device-close on the export-failure path; retain
   the device on `IN_USE`.
4. **Last-close failure forgotten** — `doca_dev_close` can return `DOCA_ERROR_IN_USE` (`doca_dev.h:192`); the DPU
   engine treats exactly this as a leaked device (`homer_service_dpu_dma.c:16189`). Round-0 nulled `device`/
   `refcount` after close unconditionally → a later `Acquire` opens a SECOND handle. FIX: check `doca_dev_close`;
   on failure RETAIN the device pointer, and gate device-open on **`device == NULL`** (not `refcount == 0`) so a
   pinned-open leaked device is REUSED, never re-opened.

Two SIMPLIFICATIONS fall out of the wider lock: (a) the "transient constructing hold" DISAPPEARS — with the whole
body locked, nothing interleaves, so `refcount` only ever counts SUCCESSFUL live mmaps and is incremented AFTER a
successful export; (b) no separate `Acquire`/`Release` helpers — the export wrapper and the teardown block lock the
singleton directly around their whole DOCA sequence.

**The context (file-static process singleton in `homer_client.c`, guarded by `HOMER_CLIENT_DPU_WITH_DOCA`):**
```c
/* Component E: ONE host doca_dev per process, shared across all CLIENT-SESSION-EXPORT
 * mmaps (SQL + BB send/recv carriers). NOT global: the frontend agent and the legacy
 * HomerFrontendDmaOpenDocaDevice own SEPARATE devices (out of scope). refcount = #
 * live-or-retained per-session mmaps. INVARIANT: device != NULL iff a device is open
 * (a device may sit open with refcount==0 ONLY after a failed last doca_dev_close,
 * pinned awaiting reuse/process-exit). Cold-path serialized; hot data path never
 * touches the device (VERIFIED: no dpuDoca* ref outside acquire :1021 / teardown :4238-4244). */
typedef struct HomerClientSharedDevice {
    pthread_mutex_t lock;         /* held across the WHOLE open+export / destroy+close sequence */
    struct doca_dev *device;      /* non-NULL iff a device is open */
    char            pci[64];      /* diagnostic: PCI the open device serves (mismatch => WARN, not reject) */
    int             refcount;     /* # live-or-retained per-session mmaps */
} HomerClientSharedDevice;
static HomerClientSharedDevice g_homerSharedDevice = { PTHREAD_MUTEX_INITIALIZER, NULL, {0}, 0 };
```
`#include <pthread.h>` is added (absent today; include block starts `:25`). `PTHREAD_MUTEX_INITIALIZER` needs no
init fn; pthread is linked in both consumers (pgbench + postgres backend).

**Acquire wrapper (`HomerClientDpuExportMmap:1018`) — REWRITTEN; WHOLE BODY under the lock:**
```c
static bool HomerClientDpuExportMmap(stream, devPci, err, errBytes) {
    bool ok = false;
    pthread_mutex_lock(&g.lock);
    if (g.device == NULL) {                                  /* gate on device==NULL, NOT refcount==0 */
        if (!HomerDpuFrontendOpenDevice(devPci, &g.device, err, errBytes)) { unlock; return false; }
        snprintf(g.pci, sizeof g.pci, "%s", devPci ? devPci : "");   /* DIAGNOSTIC ONLY (no reuse compare) */
    }
    /* NO PCI compare on reuse (round-2 resolution): the context EMBODIES "one local DPU per
     * process" (safety rule 9); the public SQL adapter pins the PCI (:2327); a differing devPci
     * is either a DOCA spelling alias (doca_dev.h:357, harmless) or an out-of-contract multi-DPU
     * violation. A warn-and-reuse would be wrong for a genuine A!=B; a strcmp reject would false-
     * reject an alias -- so we do NEITHER and simply commit to the invariant. CAVEAT (diff-refutation
     * correction, was WRONG before): a genuine different-PCI request is SILENTLY served by the
     * already-open device -- the setup wire carries NO host-PCI identity (homer_dpu_comch_abi.h), so
     * NOTHING detects the mismatch at runtime. Undefined; ruled out ONLY by safety rule 9. */
    if (HomerDpuFrontendExportRegionOnDevice(g.device, stream->dpuExportBuffer, stream->mappingBytes,
                                             &stream->dpuDocaMmap, &stream->dpuMmapExport,
                                             &stream->dpuMmapExportBytes, err, errBytes)) {
        g.refcount++; ok = true;                            /* one ref for this live mmap */
    } else if (g.refcount == 0) {                           /* WE opened it just now and export failed */
        if (doca_dev_close(g.device) == DOCA_SUCCESS) { g.device = NULL; g.pci[0] = 0; }
        /* else IN_USE (e.g. the partial mmap :977 leaked, untracked) -> RETAIN g.device for reuse;
         * never a 2nd handle. refcount==0 counts only TRACKED carrier mmaps; an untracked partial-
         * export leak is not counted and is backstopped by this IN_USE-retain (round-2 finding 2). */
    }
    pthread_mutex_unlock(&g.lock);
    return ok;
}
```

**Teardown (`HomerClientCloseBaseBackupStreamInternal`, `:4238-4245`) — REWRITTEN; device/mmap block under the lock,
inline dev-close removed:**
```c
if (stream->dpuDocaMmap != NULL) {
    pthread_mutex_lock(&g.lock);
    doca_error_t rc = doca_mmap_destroy((struct doca_mmap *)stream->dpuDocaMmap);
    if (rc != DOCA_SUCCESS) {                 /* mmap STILL live (doca_mmap.h:118) */
        pthread_mutex_unlock(&g.lock);
        /* HomerClientSetError (NOT bare fprintf) so the caller's error buffer is populated -- pgbench
         * :9331 / backend basebackup_homer.c:465 print it on a false return (round-2 finding 3). */
        HomerClientSetError(err, errBytes, "shared device mmap_destroy failed: %s", HomerClientDpuDocaError(rc));
        return false;   /* FULL-retain tombstone: skip frees+memset, keep mmap+buffers+ref, report failure */
    }
    stream->dpuDocaMmap = NULL;
    bool devLeaked = false;
    /* underflow guard (trace, not libc assert -- Push-1 de-assert rule): refcount>0 expected here */
    if (--g.refcount == 0) {
        if (doca_dev_close(g.device) == DOCA_SUCCESS) { g.device = NULL; g.pci[0] = 0; }
        else { devLeaked = true; /* IN_USE -> RETAIN g.device (refcount 0, device!=NULL); next Acquire REUSES it */ }
    }
    pthread_mutex_unlock(&g.lock);
    if (devLeaked)   /* deferred (post-unlock) loud log -- never write stderr under g.lock (round-2 finding 5) */
        fprintf(stderr, "homer client: *** shared device doca_dev_close failed (IN_USE) -- device leaked to "
                        "process teardown; session close still SUCCEEDS ***\n");
}
```
**dev-close-failure POLICY (round-2 finding 6e, RESOLVED):** a per-session close returns SUCCESS even if the
last-ref `doca_dev_close` failed. Rationale: the SESSION closed correctly (its mmap destroyed, buffers freed, DPU
told); a failed device close is a PROCESS-singleton health issue, not a per-session-close failure, and the leak is
benign (device pinned open, reused by the next Acquire or reclaimed at process exit). It is made OBSERVABLE by the
loud deferred `fprintf` (mirroring the DPU engine's P2-i alarm severity, `homer_service_dpu_dma.c:16194`). The
UNVERIFIED risk (DOCA does not promise a failed-close handle stays valid for later mmap-create, `doca_dev.h:183`)
is near-unreachable (it requires the partial-export-destroy corner) and degrades to a CLEAN export failure on reuse,
never corruption.
/* dpuDocaDev block DELETED. Reached only when mmap was NULL (tombstone/2nd close) or destroy succeeded. */
free(stream->dpuSetupPayload); free(stream->dpuExportBuffer); memset(stream, 0, sizeof(*stream)); return ok;
```

**Carrier field change:** `dpuDocaMmap` (`remote_execution_client.h:143`) STAYS (one mmap/session). `dpuDocaDev`
(`:142`) is **DROPPED** — the context is sole device owner; `dpuDocaMmap != NULL` is the proof the carrier holds a
context ref. Removing it kills the wrong-neighbor trap (a future reader closing `carrier->dpuDocaDev` under the
shared device) at the source. Grep-VERIFIED referenced ONLY at acquire `:1021` + teardown `:4242-4244` + decl
`:142`, all rewritten. Shrinks the shared carrier; a coordinated both-tree recompile (Push 2 does anyway).

**Double-close is release-safe (finding C — my round-0 "no re-entry" rationale was WRONG, but the conclusion holds
via a DIFFERENT mechanism, VERIFIED):** BB-SEND DOES re-enter close — `bbsink_homer_end_backup`'s graceful close
failure raises ERROR (`basebackup_homer.c:468`, before `stream_open=false:469`), so `PG_FINALLY`→
`bbsink_homer_cleanup` calls `HomerClientAbortBaseBackupStream` on the SAME carrier (`:500`). Release-safety comes
from **`refcount` decrementing ONLY on a SUCCESSFUL `doca_mmap_destroy`** + the carrier field tracking liveness:
(a) unacked-close first return (`:4236`) leaves the carrier intact → 2nd close retries cleanly; (b) a first close
that reached teardown either succeeded-destroy+`memset` (2nd close sees `dpuDocaMmap==NULL` → no-op) or
failed-destroy+FULL-retain (2nd close retries the destroy on the still-valid handle — a retry, not a double-free).
No path decrements twice. SQL/BB-RECV do not retry (`homer_session_core.c:530`, `pg_basebackup.c:2444/2463` are
fatal).

**Threading (VERIFIED):** device is cold-path-only — hot path reads only `dpuExportBuffer`-relative pointers
(control slot `:1602`, completion `:1699`, result ring `:3409/:3473`, credit `:3587/:3590`, BB publish `:4113`), so
the wider mutex still leaves the DATA path lock-free. It IS the only lock in the acquire/teardown domain (no
lock-ordering risk). pgbench `-j N` = N worker threads opening their assigned clients concurrently
(`pgbench.c:8978/:8980` → SQL open `:9407`); `-c4 -j4` = 4 sessions / 4 threads / 4 concurrent opens = 4
devices/process TODAY. Framing correction: per-process device count tracks `-c`, not `-j`; `-j` only makes opens
CONCURRENT. Each carrier is opened+closed by a SINGLE owning thread (pgbench: one `CState`/thread; BB:
single-threaded) — the mutex protects only CROSS-carrier device sharing, not same-carrier concurrent close (which
does not occur).

**Documented caller-discipline assumptions (refutation, not enforced at runtime):**
- **No reopen on a live carrier:** the three opens `memset` the carrier before export (`:1936/:2437/:2811`), so a
  second public open on a live carrier would erase its mmap/ref (leak a ref, pin the device). Current callers open
  once per carrier lifetime. A runtime guard would sit before the memset at 3 sites — not added; recorded in
  CONTRACTS.md as an invariant.
- **fork:** no `pthread_atfork`. Safe for current callers (pgbench does not fork after threads start; backends fork
  from a postmaster that never touches the singleton — VERIFIED: postmaster only registers the agent bgworker
  `remote_execution_backend_bridge.c:2878-2892`; the agent opens its device inside `HomerFrontendAgentMain`). A
  future fork holding the lock would give the child a locked mutex — documented limitation.
- **PCI mismatch = NO compare (round-2 resolution):** reuse the open device unconditionally; `pci[]` is
  diagnostic-only. A warn-and-reuse is wrong for a genuine A!=B (exports against the wrong device); a strcmp reject
  false-rejects a DOCA spelling alias (`doca_dev.h:357`). Both wrong, so do neither — commit to "one local DPU per
  process" (safety rule 9; the public SQL adapter pins the PCI `:2327`). CAVEAT (diff-refutation correction): an
  out-of-contract multi-DPU request is SILENTLY served by the already-open device — the setup wire carries NO
  host-PCI identity (`homer_dpu_comch_abi.h`), so nothing detects it at runtime. Undefined; ruled out ONLY by
  safety rule 9 (an earlier "fails cleanly at DPU import" claim here was WRONG).

**Warn primitive (VERIFIED 2026-07-19):** `homer_client.c` uses `fprintf(stderr, ...)` (5+ sites) and
`HOMER_DPU_P2_DIAG`-gated traces (3 sites); it has ZERO `HOMER_EVENT` uses and does not include `homer_event.h`.
So the two E warnings use `fprintf(stderr)` (bounded, allocation-free, cold path — one-shot per session close, NOT
a hot/retry loop), matching the TU. Today's device teardown (`:4240/:4244`) emits NOTHING (silently discards
results), so even a bare `fprintf` is strictly more observable. `HOMER_EVENT` is a backend/DPU-service API, not
used in this frontend client lib; not introduced here.

**Import-safety (VERIFIED, unchanged):** DPU import keys on generation/client/export-id on its OWN device — no
host-device identity on the wire — so one host device serving N sessions changes nothing the DPU sees.

**Frontend agent + legacy frontend-DMA — UNTOUCHED (VERIFIED):** `HomerFrontendAgentMain` keeps its own explicit
open(`homer_frontend_agent.c:1546`)→N exports(`:1556/:1568`)→destroys(`:1775/:1781`)→close(`:1786`) lifetime and
never uses the `static` carrier wrapper; the legacy `HomerFrontendDmaOpenDocaDevice` (`homer_frontend_dma.c:1435/
:1462`) is a THIRD separate host-device owner. So "one device per process" is true ONLY for the carrier path — the
context is a NEW internal LAYER over the device-scoped primitives, not a replacement of their explicit-owner
semantics (converting the agent to context refs would double-free its trailing `CloseDevice`).

**Validation (per §12.5 step 5):** gate + four-role basebackup + DPU TCP smoke. The gate (`-c4 -j4`) is the
DIRECT proof of shared-device correctness under concurrent opens; basebackup covers the BB acquire/teardown switch;
smoke covers the host↔DPU DMA net. **Perf:** device open/close moves off the per-session path onto per-process
first-open/last-close — a COLD-path win only; no hot-path change expected.

**Current site-map (VERIFIED HEAD, supersedes §12.5a "E" stale numbers):** carrier fields
`remote_execution_client.h:142`(`dpuDocaDev`, TO DROP)/`:143`(`dpuDocaMmap`, STAYS); primitives
`HomerClientDpuOpenDevice:796`, `HomerDpuFrontendOpenDevice:877`, `HomerDpuFrontendCloseDevice:907`, combined
exporter `HomerDpuFrontendExportRegion:838` (open `:855`, on-device `:859`, close-on-fail `:862`), device-scoped
`HomerDpuFrontendExportRegionOnDevice:921` (mmap-create `:948`, partial-destroy-on-fail `:977`), mmap-only destroy
`HomerDpuFrontendDestroyRegionExport:990` (void, discards `:994`), unused combined `HomerDpuFrontendDestroyExport:1006`,
carrier wrapper `HomerClientDpuExportMmap:1018` (calls combined `:1021`); acquisitions SQL `:2039` / BB-send `:2494`
/ BB-recv `:2869`; teardown body `:4136` (unacked-close early return `:4236`, inline mmap-destroy `:4240`, inline
dev-close `:4244`, frees `:4246/:4247`, memset `:4248`).

**REFUTATION ROUND 2 → design SETTLED (2026-07-19; corrected mechanisms attacked; all findings VERIFIED by me,
then resolved). Both sides recorded.** Round 2 CONFIRMED sound: the widened lock covers every shared-object DOCA
call in export (`:808/:817/:822/:825/:948-971/:977`) and teardown (`:4238/:4242`); the export blob copy outside the
lock is safe (blob lifetime tied to the live mmap, `:916`); ordinary two-export interleaving balances; the underflow
claim holds (`dpuDocaMmap` set only at `:1022`, destroyed only at `:4238`); `homer_event.h` is source-level
frontend-safe. Remaining findings + resolutions:
- **REAL FIX (finding 3):** the destroy-failure `return false` must populate `errorMessage` via `HomerClientSetError`
  (pgbench `:9331` / backend `:465` print it) — not a bare `fprintf`. APPLIED in the pseudocode above.
- **PCI (findings 6b/6c) RESOLVED → no compare** (see the wrapper + assumption note): warn-and-reuse is wrong for
  a genuine A!=B; strcmp-reject false-rejects a DOCA alias. Commit to safety-rule-9 instead.
- **dev-close IN_USE (finding 6e) RESOLVED → policy** (see the dev-close-failure POLICY note): per-session close
  SUCCEEDS; the device leak is a separately-logged process concern; reuse-after-IN_USE is a documented near-
  unreachable UNVERIFIED risk that degrades to a clean failure.
- **warn primitive (finding 5) RESOLVED:** `HOMER_EVENT` is frontend-safe but (a) not frequency-bounded, (b) does a
  `write` that must NOT run under `g.lock`, (c) would need a `Makefile:124` prereq add. So E uses `HomerClientSetError`
  (contract) + a DEFERRED post-unlock `fprintf` (operator visibility) — matching the TU (0 `HOMER_EVENT` uses today).

**OUT-OF-SCOPE / pre-existing hazards DISCOVERED by the refutation (recorded; NOT Push-2 blockers — E does not
introduce or worsen them):**
- **Setup-ACK loss ⇒ ambiguous DPU import (finding 6a) — a genuine PRE-EXISTING correctness hazard.** The DPU
  imports the setup payload (`homer_service_dpu_setup_tcp.c:586`) BEFORE writing the ACK (`:750`); if the client
  read fails, open falls to ordinary close, but a missing ACK leaves `bridgeGeneration==0` so
  `HomerDpuFrontendSendClose` returns true WITHOUT contacting the DPU (`homer_client.c:1258`) — host mmap destroyed
  despite a possibly-live DPU import. E does NOT touch the setup/close protocol; sharing the device makes it
  marginally SAFER (a co-tenant ref keeps the device alive). Recorded for a separate fix decision; see the CONTRACTS
  entry.
- **Partial-export-destroy-failure untracked mmap (finding 2):** `HomerDpuFrontendExportRegionOnDevice` discards its
  partial `doca_mmap_destroy` (`:977`); a failure leaves an untracked mmap. Backstopped by the acquire-path
  IN_USE-retain; realistically unreachable (a freshly-created never-DMA'd mmap destroys cleanly). Documented.
- **Reopen-on-live-carrier (finding 4):** caller-discipline invariant (the 3 opens `memset` before export); current
  callers open once/carrier. In CONTRACTS.md, not enforced at runtime.
- **Public combined `HomerDpuFrontendExportRegion` (finding 6d):** after the rewrite it has NO in-tree caller but
  stays exported (`remote_execution_client.h:263`) and opens its OWN device (`:855`) — a bypass of the context. Add
  a definition-site comment warning it must not be used for carrier exports; leave the symbol (public API).

**The Component-E design is SETTLED — sound to implement.** Refutation arc: round-0 (4 hard blockers, REJECTED) →
round-1 (widened lock + checked destroy/close + device==NULL gate) → round-2 (1 real fix + policy resolutions +
out-of-scope discoveries) = genuine convergence.

**IMPLEMENTATION + DIFF REFUTATION (2026-07-19, codex-explore on the actual diff; findings VERIFIED by me).**
Component E implemented in the citus tree (`homer_client.c` + `remote_execution_client.h`, +183/−9). Diff-refutation
verdict: **"no ordinary lock leak, double-free, or double-decrement exists"** — lock released on every exit path;
the double-close state machine sound in all three cases (success+memset → 2nd close sees zeroed `selectedDpu`;
destroy-fail tombstone → retry decrements at most once; unacked-close early return → retries retained state); return
contract correct for all callers; ABI shrink 656→648 B with NO wire/`sizeof`/`offsetof` dependency (contained
recompile). Fixes applied from the diff refutation:
- **Underflow-branch `fprintf` was UNDER the lock** — inconsistent with the deferred-log policy 2 lines below;
  latched to a post-unlock log (a `refcountBug` flag beside `devLeaked`).
- **LYING comments corrected** (the money fixes — a lie here writes the next bug): the bypass comment claimed
  `HomerDpuFrontendExportRegion` is "kept for the frontend agent" — FALSE (the agent uses `OpenDevice` +
  `ExportRegionOnDevice`; the combined helper has NO caller); my PCI comment claimed a genuine mismatch "fails
  cleanly at DPU import" — FALSE (the setup wire carries no host-PCI identity, so it is SILENTLY served — corrected
  in code AND above); stale line-number refs (`:855`/`:977`) and "Acquire" terminology (no Acquire helper) reworded
  to be symbol-based / stale-proof.
- **Documented (not fixed, VERIFIED pre-existing / near-unreachable):** the partial-export-destroy-failure corner
  (`ExportRegionOnDevice` discards its partial `doca_mmap_destroy`; pre-existing, backstopped by the IN_USE-retain)
  and the `INVALID_VALUE` mmap-destroy classification (degrades safely — retry re-fails → leak-to-teardown).
The Component-E CODE is settled — ready to build + validate.

**PUSH-2 VALIDATION COMPLETE — GREEN (2026-07-19, run `candidate-20260719-031152`; base citus `f92d39025` +
postgres `d386a2ecbef` + the uncommitted host-only E diff).** All three required tiers PASS against one freshly
built+installed both-tree artifact set (Citus first → PostgreSQL relink; SHA-256 peer parity; run-scoped DPU
snapshot of the UNCHANGED DPU source — no bridge/ABI bump, version still 5):
- **DPU TCP smoke — PASS** (both ends `ok`; drain `outstanding=0 fatal=false`).
- **Four-role selected-DPU basebackup — PASS** (delivered 23,259,553,812 B; `full_laps=44364` wrap proven;
  `published_tail==consumed_head` fully drained; `basebackup_check` PASS; frontier `posted==retired+flushed` and
  `teardown-ledgers` balanced on BOTH DPUs) — the BB send/recv carriers exercised through the shared device.
- **Selected-DPU command GATE — PASS at BOTH shapes:** `-c1 -t5` (5/5, 5 distinct abalance, `gate_check`+
  `alarm_check` PASS) AND **`-c4 -j4 -t5` = the shared-device concurrency proof** (20/20 processed, 0 failed; 4
  Homer SQL sessions opened CONCURRENTLY (ids 2,3,4,5) sharing ONE host `doca_dev`; 4 distinct launched PIDs;
  `spawn_pairs=4`, `decoded_values=20`; `peer_host_spawn_retired` ABSENT).
- **ALL component-E stderr markers ABSENT** across every log (no `doca_dev_close(IN_USE)`, no `refcount BUG`, no
  `mmap_destroy` failure, no session-OPEN failure across the 1+4 concurrent sessions) — the shared-device paths
  behaved exactly as designed.
- Co-arming: the pre-recycle stale generation produced the documented transient `status=4` (hazards §11), then the
  recycled frontend agent's fresh generation was accepted with ZERO rejects — the Push-1 lesson held by
  construction. DPU setup listener (:9727) confirmed LIVE post-workload with the agent attached throughout.
- Perf: NOT quoted (debug/contended-CPU correctness run; component E is a COLD-path change — no hot-path effect
  expected). Foreign 5432 (pid 1308997) untouched throughout.

**⇒ Component E is validated. Stage 3 (the SQL opKind of the unified core) is COMPLETE after this commit.** Next:
Stage 3.5 (basebackup onto the unified core).

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

#### 12.5b-impl — CONCRETE DESIGN, re-grounded 2026-07-19 (codex-explore basebackup map, current HEADs citus `31cc8edd4` / postgres `db6a17e90ce`)

**Grounding findings that reshaped the scope (all VERIFIED):**
- BB ALREADY shares the substrate post-Stage-3: the SAME `HomerClientBaseBackupStream` carrier, the SAME cold leaves
  (`HomerClientDpuExportMmap` [now the Push-2 shared device], setup-TCP `HomerClientDpuSendSetupMessage`, role-1
  `HomerClientDpuSubmitControlRequest`, dataplane `HomerClientCloseBaseBackupStream`), and the SAME sink `HomerByteRingSinkDrain`.
- The **producer plane already EXISTS as functions**: `HomerClientReserveBaseBackupRecordForObjectPayload`
  (`homer_client.c:3804`, rejects a 2nd live reservation) + `HomerClientSubmitBaseBackupRecord` (`:4031`, commit =
  publish immediately, `nextSequence++`). SEND sets `carrier->serviceSessionId = dpuBridgeGeneration` (`:2553`).
- The **bulk-consume already drains via the shared sink**: `HomerClientPollBaseBackupReceive` is a FUSED pass
  (resolve role-7 frontier from the credit line → shared `HomerByteRingSinkDrain` `:3343` → publish consumed-head
  credit if moved). Terminal split matches §13.4 (FAILED credit-line drain-independent `:3413`; CLOSED drain-gated `:3427`).
- **⚠ BB-recv is a BLACKHOLE byte-counter** (`HomerClientBaseBackupDeliverReceiveObject:3245` "payload discarded;
  the real object-byte count is the core's tally") — it validates the envelope + ordinal inline and counts, it does
  NOT decode/reconstruct. So it needs NO lease-return and NO deferred credit (the reasons SQL needs the split
  borrow/release plane). The plan's "bulk-consume plane (borrow/release/credit)" for BB is a re-expression of the
  existing `Drain`, at zero functional gain.
- Ring binding VERIFIED (§13.2): SQL role-1/6/7 (`:2183/:2216/:2244`); BB-SEND role-1 + role-4 HOST_TO_DPU
  (`:2634/:2651`); BB-RECV role-1 + role-7 DPU_TO_HOST (`:3010/:3033`). THREE duplicated openers currently determine
  the ring set. BB handles do not exist yet (`homer_session_core.h:20` names the future tags only).

**SCOPE DECISION (USER, 2026-07-19): Option B = typed-handle WRAP + unified OPERATION-OPEN. The bulk-consume lease
plane (plan-as-written) is SCOPED OUT.** Both sides recorded:
- **Chosen (B):** (1) opaque `HomerBaseBackupSend`/`HomerBaseBackupRecv` handles (compile-time op-legality across
  kinds — a SEND handle cannot call consume ops; a BB handle cannot call SQL command ops); (2) ONE unified two-part
  operation-open (§13.1 intent + op spec) that binds the ring set by opKind+direction, replacing the 3 duplicated openers.
- **Scoped OUT (was plan §12.5b "bulk-consume plane"):** splitting BB-recv's fused poll into an explicit
  borrow/release/credit bracket. REASON: BB-recv decodes NOTHING (blackhole counter), so the lease/deferred-credit
  machinery buys no function; it only re-expresses the existing `Drain` while touching the most-bug-historied bulk
  path (run-39 "reject every row forever"). The multi-consumer proof is delivered by the typed handles + unified open;
  the lease plane is not needed to prove generalization for a command-less bulk consumer. (If a future consumer needs
  to DECODE the bulk stream — e.g. a real disk-writing pg_basebackup, or COPY in Stage 5 — the split is revisited then.)

**TWO PUSHES (mirrors Stage 3's Push-1/Push-2 split — behavior-preserving first, then the structural change):**

**PUSH A — typed-handle wrap (behavior-preserving). HANDLE MODEL = BY-VALUE DISTINCT-TYPE (USER DECISION
2026-07-19; supersedes the earlier "opaque heap" framing + the (a)/(b) core sub-decision).** WHY BB differs from
SQL: SQL's opaque-heap handle hides a genuinely-PRIVATE `struct HomerSession`; BB has NO private struct to hide —
its "core" is the `HomerClientBaseBackupStream` carrier, which MUST stay in a public header because the SQL core
embeds it by value (`homer_session_core.c:125`). So opaque-heap for BB would need a header split (to hide a
carrier that's already public) PLUS the re-entrant-close heap-ownership hazard (finding 2), for near-zero opacity
payoff. By-value distinct-type gives the SAME compile-time op-legality with none of that.
- **Model:** two DISTINCT one-field struct wrappers in the public header —
  `struct HomerBaseBackupSend { HomerClientBaseBackupStream s; }` and
  `struct HomerBaseBackupRecv { HomerClientBaseBackupStream s; }`. Callers (`bbsink_homer` /
  `pg_basebackup`'s `RunHomerReceiveConsume`) embed the wrapper BY VALUE exactly where the carrier lives today (NO
  heap, NO Free contract, NO header split). The re-entrant `PG_FINALLY` close works UNCHANGED (storage persists,
  freed with the sink's palloc / the stack frame).
- **Op-legality:** SEND ops take `HomerBaseBackupSend *`, RECV ops take `HomerBaseBackupRecv *` (distinct C types
  ⇒ wrong-op-wrong-kind is a COMPILE error). Each typed op forwards to the existing carrier-typed logic via `&h->s`.
  Op-legality is delivered at the CALLER boundary: the two migrated callers (the ONLY external callers, VERIFIED by
  grep) use the typed API, so a wrong-kind handle fails to compile. The carrier-typed BB logic functions stay as-is
  (additive Push A — lowest risk); STATIC-izing them to enforce against a (currently non-existent) internal bypass is
  a deferred hardening, not done here.
- **Distinct typed close/abort per direction** (`HomerBaseBackupSendClose`/`Abort`, `HomerBaseBackupRecvClose`/`Abort`)
  forwarding to the shared `HomerClientCloseBaseBackupStreamInternal(&h->s, abortReset,…)`.
- **Blast radius:** citus `homer_client.c` (add typed wrappers; static-ize the carrier-typed BB logic) +
  `remote_execution_client.h` (wrapper structs + typed decls) + postgres `basebackup_homer.c`
  (`bbsink_homer.stream` → `HomerBaseBackupSend`) + `pg_basebackup.c` (`RunHomerReceiveConsume` → `HomerBaseBackupRecv`).
  The shared carrier struct is UNCHANGED. Preserve: the process-global interrupt hook (do NOT clear on a failed
  close), the sink's reservation bookkeeping, the blackhole-mode rejection.
- **Validation:** four-role basebackup (PRIMARY — directly exercises both BB handles) + gate (prove SQL unaffected)
  + DPU TCP smoke.

**PUSH A IMPLEMENTED + VALIDATED GREEN (2026-07-19, run `candidate-<push-A>` on base citus `31cc8edd4` +
postgres `db6a17e90ce` + the uncommitted Push-A diff).** By-value typed wrappers as designed; BOTH trees compiled +
installed clean (citus `make` exit 0; postgres `ninja` recompiled `basebackup_homer.c.o` + `pg_basebackup.c.o` exit 0
— the mechanical migration is correct). Results:
- **Four-role basebackup — PASS, BEHAVIOR-IDENTICAL** (the whole point): `full_laps=44364` EXACT match to the last
  green run (`candidate-20260719-031152`), 7.05s vs ~7.06s, delivered ~23.26 GB, CLOSE_ACK on the receiver, balanced
  `posted==retired+flushed` frontier + balanced teardown ledgers on BOTH DPUs. Both typed handles' underlying carrier
  ran the real DMA/byte-ring path (not a fallback). (Delivered-byte delta ~107 KB = live data-dir size variance, NOT
  a wire invariant.)
- **Gate — PASS** (SQL unaffected by the BB-side wrap): 5/5, 5 distinct abalance, `spawn_pairs=1`,
  `peer_host_spawn_retired` absent. Ran `-c1 -j1` (validator's sound call: the `-c4` concurrency proof is Component
  E's, which Push A does not touch — a behavior-preserving BB wrap needs only the SQL path to still work end-to-end).
- **DPU TCP smoke — PASS**; Component-E shared-device markers absent throughout; DPU setup listener live post-gate;
  no DPU/bridge rebuild needed (version still 5). Co-arming produced the documented transient status=4 then clean
  acceptance (hazards §11).
- **⇒ Push A is validated; op-legality (compile-time SEND≠RECV) delivered with zero behavior change. Ready to commit.**

**PUSH B — unified operation-open dispatcher (WELL-FACTORED; USER DECISION 2026-07-19).** Replace the 3 duplicated
openers (`HomerSqlTransportOpen` / `HomerClientOpenBaseBackupStreamSelectedDpu` / `…ReceiveStreamSelectedDpu`) with
ONE operation-open, structured as **shared prologue + layout table + a per-opKind OPEN-OP set** — NOT a naive inline
`switch`. Concretely:
- **Shared PROLOGUE** (the genuinely-unifiable cold setup): `posix_memalign` the export buffer + `HomerClientDpuExportMmap`
  (Push-2 shared device) + `HomerClientDpuSendSetupMessage` (setup-TCP). One helper, called by all kinds.
- **Layout TABLE** (data-driven, per opKind+direction): `{roles[], ringBytes, lineCount, hasCompletionEvent,
  direction}` drives the descriptor-row construction (SQL 1/6/7 + 10 MiB + completion event; SEND 1/4 + 8 MiB;
  RECV 1/7 + 8 MiB). This layer IS pure data — collapses cleanly.
- **Per-kind OPEN-OP set** (the irreducibly-per-kind PROTOCOL, dispatched not merged — a small mini-vtable):
  `{ mint_identity, build_open_request, validate_open_response, activate_post_open }`. SQL = {adopt-from-response
  (serviceSessionId 0 pre-OPEN), COMMAND_SESSION+relay, validate index + core-adopt, open=false + arm result credit};
  SEND = {pre-mint from dpuBridgeGeneration, TUPLE_SINK+geometry/dir/sinkId/tag/key/placement, validate echo + copy
  queue descriptor, init producer seq + open=true + publish tail 0}; RECV = {pre-mint, TUPLE_SINK RECEIVE + role-7
  binding + peerEndpoint.protocolVersion set, validate echo, init frontier + open=true + publish consumed-head 0}.
- **DECISION RATIONALE (user):** cleaner for posterity + the true §13 capstone (one operation-open realizing the
  intent-driven model), and not overly complex given the protocol is already factored into a per-kind op-set. The
  payoff is architectural (§13 fidelity, one entry point); the naive inline-switch (which reads worse than 3 openers)
  is explicitly REJECTED — the value is entirely in the factoring.
- **Risk:** MEDIUM — touches all 3 open paths; the layout table + each open-op must reproduce its opener's CURRENT
  behavior EXACTLY (identity direction, wire union member, response checks, post-open publication, the `open` flag
  which gates whether shared close issues the tuple-sink CLOSE `homer_client.c:4280`). Behavior-preserving.
- **Validation:** four-role basebackup + gate + smoke (all three opKinds flow through the one dispatcher).

**Stale §13 items the grounding corrected (fixed in §13 below):** §13.5's "`receiveExpectedOrdinal` is BB
kind-specific A-state" — WRONG, Stage-3 code keeps it carrier-resident shared-B (basebackup + SQL share it for
wrap-gap detection); §13.3's "both are fused" — stale (SQL now borrows/releases directly; BB stays fused via `Drain`);
§13.5's carrier-name "misnomer" note — stale (the header now calls it "the transport substrate the core embeds").

**DESIGN REFUTATION (2026-07-19, codex-explore on the Push-A/Push-B design; findings VERIFIED by me in code). Both
sides recorded. Push A SOUND with corrections; Push B BROKEN as scoped.**

Push A corrections (accepted):
- **Core-sharing (a) is ILLUSORY → use per-kind handles over the CARRIER (was leaning (a) "reuse struct HomerSession").**
  VERIFIED `HomerSqlSessionOpen`/`Close` are entirely SQL-specific (identity adoption `homer_session_core.c:281`,
  credit arm `:294`, `CLIENT_SQL_SESSION_CLOSE` `:424`), so reusing the SQL core buys only `malloc` + carries dead
  SQL fields. BB handles become **distinct opaque tags aliasing the heap-owned `HomerClientBaseBackupStream` carrier
  directly** (the carrier IS BB's whole substrate; no `struct HomerSession`, no new core). Public ops cast the tag →
  `HomerClientBaseBackupStream *` inside `homer_client.c` (the carrier layout is PUBLIC there, unlike the private SQL core).
- **⚠ BLOCKER — retry-aware close ownership (was "Close frees the handle").** VERIFIED `HomerSqlSessionClose` frees
  UNCONDITIONALLY (`homer_session_core.c:531`, even after a failed dataplane close) — safe ONLY because pgbench never
  re-enters close. BB is the OPPOSITE: `bbsink_homer_end_backup`'s failed graceful close raises ERROR before
  `stream_open=false`, so `PG_FINALLY`→`bbsink_homer_cleanup` re-enters `HomerClientAbortBaseBackupStream` on the SAME
  carrier (`basebackup_homer.c:468/485/500`); and BB close can return false while RETAINING the carrier for retry
  (unacked CLOSE_ACK `homer_client.c:4326`; the component-E mmap-destroy tombstone `:4361`). So **BB Close must NEVER
  free.** CONTRACT: Open allocates the carrier + returns the typed handle; the CALLER holds it through the (possibly
  re-entrant) close lifecycle; a SEPARATE terminal `…Free`/`…Destroy` frees it ONCE, called by the owner AFTER all
  close/abort attempts (bbsink cleanup / pg_basebackup tail). Copying SQL's free-in-close is a use-after-free here.
- **Distinct typed close/abort PER DIRECTION** (compile-legality trap): `HomerClientCloseBaseBackupStream`/`Abort`
  today take one shared carrier type and are called from both directions (`remote_execution_client.h:413/427`).
  Retyping them to a common handle loses direction op-legality → need `HomerBaseBackupSendClose/Abort` +
  `HomerBaseBackupRecvClose/Abort`, each forwarding to the shared internal close body.
- Preserve (finding-6 constraints): the interrupt hook is PROCESS-GLOBAL (`HomerClientSetInterruptCheck`,
  `basebackup_homer.c:482`) — do NOT clear it on a failed close; the sink's reservation pointer/flag bookkeeping
  (`basebackup_homer.c:278/311`) stays sink-side; the blackhole-mode rejection (`homer_client.c:2507`) is preserved.
- Skip-lease rationale SHARPENED: the correct reason is "BB-recv validates + consumes one whole record SYNCHRONOUSLY
  before the `Drain` callback returns" (`homer_client.c:3245`), NOT "decodes nothing" (it DOES validate the BB header
  proto/lengths `:3216/:3238`). No lease/deferred-credit is needed because nothing is retained past the callback.

**Push B — "unified open" BROKEN as scoped (VERIFIED; DECISION DEFERRED TO USER).** The 3 openers differ in FAR more
than the ring set, and none of these fold into "one descriptor table": direction is operation-open state, NOT intent
(`homer_session_spec.h:120/136`); SQL needs `sessionUID`/`clientSqlResultDpuRelay`/DOCA-PCI (`homer_session_transport.h:188/204`);
layout differs (SQL 3 lines + completion event + 10 MiB ring vs BB 2 lines + 8 MiB); identity (SQL `serviceSessionId==0`
adopts from OPEN response vs BB pre-mints from `dpuBridgeGeneration` `:2553/:2927`); `sessionUID` binding (SQL stamps
all 3 descriptors; RECV mints+stamps role-7; SEND sets none); wire OPEN (SQL `COMMAND_SESSION`+relay vs BB
`TUPLE_SINK`+geometry/direction/sinkId/tag/key/placement); RECV must still set `peerEndpoint.protocolVersion`
(`:3076`); response handling (SQL adopts new identity+validates index vs BB validates pre-minted echoes+copies queue
descriptor); post-open activation (SQL `open=false`+ordinal+core-adopt+arm-credit vs SEND producer-seq+`open`+publish-tail-0
vs RECV ordinal/frontier+`open`+publish-consumed-0). **⇒ a "unified open" is a shared cold-setup PROLOGUE (allocate +
export + setup-TCP) plus an irreducible 3-way branch for identity/request/response/activation — NOT a clean
dispatcher; a monolithic version is "3 openers sharing a prologue," arguably worse than 3 clear openers.** This
materially changed the value of the user's "unified open" choice → taken back to the user.
**RESOLVED (USER, 2026-07-19): WELL-FACTORED full dispatch** (shared prologue + layout table + per-kind open-op set;
see the PUSH B paragraph above). The main-agent's initial "3-way not unifiable / full dispatch not ideal" framing was
CORRECTED as an OVERSTATEMENT (both sides recorded): the LAYOUT unifies as data, and the PROTOCOL — while per-kind —
dispatches CLEANLY as a small open-op vtable; only the NAIVE inline-switch reads worse than 3 openers. The user chose
the clean factoring for §13 fidelity + posterity; the extra cost is churn, not complexity.

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
| bulk-consume | fused `HomerClientPollBaseBackupReceive` (drain via shared `HomerByteRingSinkDrain`) | BB-recv | role-7; shares the sink FRAMING, NOT the split lease bracket (see below) |

**Stage-3 SETTLED + 3.5 scope (UPDATED 2026-07-19):** SQL result-consume was split into the lease-aware
`Borrow`/`Release` bracket in Stage 3 (`homer_client.c` ResultBorrowNext/AcceptAndRelease) because it DECODES tuples
and must withhold the DPU credit until the decode validates. **BB-recv is NOT getting that split (§12.5b-impl scope
decision, Option B):** it is a BLACKHOLE byte-counter (`HomerClientBaseBackupDeliverReceiveObject:3245`) — it decodes
nothing, so it needs no lease-return / deferred credit and stays on the fused `HomerByteRingSinkDrain` (which itself
loops `Borrow`→callback→`Release` internally, so BB already shares the sink FRAMING primitives, just not the caller-
visible bracket). The split lease plane is revisited only if a future consumer must DECODE the bulk stream
(disk-writing pg_basebackup, or COPY in Stage 5).

### 13.4 Terminal model — common concept, kind-specific realization
"Terminal ⇒ no reuse" is the shared contract. **SQL:** a persistent `sqlSessionTerminal` latch after completion
application (`remote_execution_client.h:228`). **basebackup:** direction-specific FAILED via the credit line
(`homer_client.c:3199`/`:3209`), immediate and drain-independent; CLOSED is drain-gated success (`:3218`). The core
exposes a common `terminal` query; terminal-abandon (§11.1) is per-kind (SQL: mark latch; BB: FAILED/ABORT_RESET
path `:4190`/`:4236`).

### 13.5 Shared vs kind-specific (from the A/B maps)
- **SHARED transport (B), one context** (Stage 3, DONE): the DOCA device (§12.3, Push-2 shared), byte-ring
  mechanics, role-1 lifecycle, setup/mmap, epochs. `HomerClientBaseBackupStream` is the shared carrier for SQL+BB
  transport state — its header now describes it as "the transport substrate the core embeds"
  (`homer_session_transport.h:63`; the older "misnomer" note is gone).
- **KIND-SPECIFIC core (A):** SQL = command sequence + completion lease + result decode (moved into
  `struct HomerSession` in Push 1). BB = record `nextSequence` (SEND) — but note (grounding correction) BB's
  `nextSequence`/reservation fields and `receiveExpectedOrdinal` are CURRENTLY carrier-resident: `receiveExpectedOrdinal`
  is deliberately SHARED-B (basebackup + SQL both use it for wrap-gap / envelope-ordinal, so it STAYS in the carrier,
  NOT per-kind A-state), and Push A leaves BB's producer fields carrier-resident (the carrier is BB's own substrate).
  So "per-kind core A-state" for BB is thin — the typed handle, not a large field migration.

### 13.6 Deferred / not now
- **COPY (Stage 5):** `opKind` reserved; reuses command + result-consume (result) + producer (ingest) planes; op
  shapes finalized against COPY's real code when implemented.
- **`blackhole` target mode:** currently inconsistent (PG parsing accepts `mode=blackhole`
  `basebackup_homer.c:179`, but both selected-DPU openers reject non-RDMA `homer_client.c:2303`/`:2678`;
  pg_basebackup's blackhole opens RDMA and discards `pg_basebackup.c:2403`/`:2428`). A pre-existing wart — noted,
  out of scope, do NOT fold a fix into this work.
