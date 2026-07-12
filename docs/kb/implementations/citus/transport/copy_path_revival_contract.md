# The COPY revival contract — what to uphold when you come back to fix backend-to-backend COPY

**Status: LIVE NOTE, written 2026-07-12, while COPY is BROKEN and we are actively rebuilding the substrate
underneath it. Keep current as P0 lands.**

## Why this document exists

Backend-to-backend COPY (`citus.enable_experimental_tuple_sink_routing=on`) has **hung at HEAD since
~2026-07-08**, and we are not fixing it now — it needs dedicated effort. Meanwhile we *are* rebuilding what it
stands on: the RDMA send-WR-ID encoding (§24), the send-CQ delivery guarantee (§25/F7), payload send-owner
retirement, and next the staging pools (§23/P0-c). **None of those can be validated against COPY, because COPY
does not run.**

The good news, established by a dedicated coverage trace on 2026-07-12 and **contrary to what we assumed**:
**the shared payload/retirement substrate is NOT accumulating COPY-only debt.** Other workloads cover it. What
is uncovered is COPY's **control plane**. This document says precisely which is which, so the revival effort
aims at the right thing.

Related: [`byte_ring_slot_capacity_regression.md`](byte_ring_slot_capacity_regression.md) "Problem 2" records
the failure. [`resource_retirement_contract_audit.md`](resource_retirement_contract_audit.md) is the P0 work.

---

## 1. What is broken — and the suspect that is NOT the cause

⚠ **Do not fuse "the COPY WORKLOAD is broken" with "the payload MACHINERY is untested."** They are different
claims and the second one is FALSE. (Audit §24.9 records me making exactly that error.)

| | status |
|---|---|
| the **COPY workload** (coordinator → socketless worker backend, via both host services) | 🔴 **HANGS.** No `remote exec backend` ever spawned on the peer. |
| the **payload/retirement machinery** COPY would use | ✅ **exercised every run** — by the DPU result relay, the 4-role basebackup, and `pgbench --homer` |

### Where the hang is bounded to (VERIFIED)

Peer `START_COMMAND` handling runs in this order in `tuple_sink_service_process.c`:

1. find the service session — `:40990`
2. **COPY requires the exact stream to belong to that session** — `:41000`
3. publish the command — `:41022`
4. **only then** `TupleSinkServiceSubmitBackendSpawnRequest` — `:41050` → publishes `REQUEST_READY` and signals
   the postmaster (`:20789`, `:20822`)

**No backend is ever spawned ⇒ execution never reaches `:41050`.** So the hang is in **peer-open / session /
sink binding, or START_COMMAND admission** — i.e. **COPY's control plane.** The RDMA payload pumps, send-owner
retirement, staging pools, receiver consumption and ACKs **have not begun and are not implicated.**

### ⚠ `a3cdd5f3c` IS NOT THE CAUSE — this attribution is REFUTED. Do not chase it.

`byte_ring_slot_capacity_regression.md` names `a3cdd5f3c` (*"delete the pending-binding registry"*) as the
leading suspect, on the reasoning that it is a session-pairing change and the symptom is "the session never
pairs". **That is a plausible story, and it is wrong.** Evidence:

- **The tombstone in the code says what the registry was FOR** (`tuple_sink_service_process.c:619-626`):
  > *"It existed **only because the basebackup relay stream** was owned by a throwaway base-compat session with
  > no sessionUID; **the relay resolved the target role-7 ring** by looking the uid up in this side table."*

  It was the **basebackup DPU relay's role-7 ring resolution**, not COPY session pairing.
- **`a3cdd5f3c` touched exactly one file** — `tuple_sink_service_process.c`. It touched **neither**
  `multi_copy.c` **nor** `worker_tuple_sink_insert.c`. **The deleted registry had no COPY call site.**
- **COPY pairs by a different mechanism entirely**: `TupleSinkServiceFindExactReceiveAttachableSink`
  (`:25285`), selected for non-basebackup RECEIVE at `:36930`. Basebackup's `(node, tag)` pairing
  (`HomerServiceBaseBackupSessionKeysPairable` `:22791`) is selected **only** for `baseBackupOpen` (`:27868`).

**➜ THE BISECT ACROSS `7a2eaed53..HEAD` STILL HAS TO BE RUN. It never was.** 50 commits touch the transport in
that range. Do not start by reverting `a3cdd5f3c`.

### The reasoning error in that commit is STILL worth remembering (just not as the cause)

Its message says:
> *"The e2e PASS confirmed the registry was never consulted (its WARN never fired), **so this deletion is a
> no-op at runtime.**"*

**That is a necessity argument that only counted the cases it observed** — the same shape as every self-refutation
in the audit doc. It happened to be true here. **It is the shape to distrust, not this instance.**

⚠ **COPY also has the byte-ring geometry problem (Problem 1).** It is a `TUPLE_VIEW_BATCH` byte-ring stream and
is affected; it simply never gets far enough to fire it. **Confirm Problem 1's status before bisecting**, or
the bisect's "good" commits will fail for a different reason.

---

## 2. THE COVERAGE LEDGER (traced 2026-07-12)

The workloads: **W1** `pgbench --homer --homer-dpu-command` (the DPU gate) · **W2** 4-role DPU basebackup ·
**W3** `pgbench --homer` host-service RDMA · **W4** local blackhole basebackup · **W5** b2b COPY (**broken**).

### 2.1 The fork that decides the payload source: DPU service vs HOST service

`HomerServicePumpOutgoingPayloadStream` (`:32036`) selects by **whether a DPU DMA engine is present**:

- **DPU service** (`:32079`) → `DPU_MIRROR`, and it is **mirror-ONLY** (`:32103`):
  > *"Selected-DPU byte rings are mirror-only … **falling through would silently resurrect the legacy
  > host-process sendQueue source.**"*
- **HOST service** (`:32112`) → `SEND_QUEUE`, via `HomerServicePumpOutgoingByteRingPayload` (`:30660`) for
  byte-ring streams, or the legacy fixed-slot tail (`:32129+`) otherwise.

### 2.2 ✅ What IS covered (and by whom) — the substrate is NOT COPY-only

| substrate | covered by |
|---|---|
| `SEND_QUEUE` source kind; the **host** byte-ring pump (`:30660`); `HomerServiceApplyTrackedPayloadCompletionFrontier`'s `SEND_QUEUE` arm and its `sourceControl->consumedHead` credit (`:17071`, `:17082`) | **W3** — `pgbench --homer` host-service RDMA |
| `DPU_MIRROR` source kind; `HomerServiceAttachPayloadSendOwnerDpuMirror` (`:16731`); the mirror retirement arm (`:16609`) | **W1**, **W2** |
| §24's `OWNER_SLOT` + `OWNER_INCARNATION` resolution, both owner tables | **W1**, **W2** (proven: audit §24.9), and **W3** for the `SEND_QUEUE` side |
| receiver-head ACK owner table (`HomerServicePostReceiverHeadAck` `:33204`) | **W1**, **W2**, **W3** — **not COPY-only** |

**➜ SEND_QUEUE IS NOT COPY-ONLY. `pgbench --homer` covers it.**

### 2.3 ⚠ BUT WE HAVE NOT BEEN RUNNING W3 — and that is a gap we can close TODAY

`CLAUDE.md`'s regression sweep says to run `pgbench --homer`. **The P0-b, §25/F7 and §24 validations ran only
the DPU gate.** The gate drives `DPU_MIRROR` and never touches the `SEND_QUEUE` arm — which is exactly the arm
§24 rewrote (owner handles) and F7 fixed (CQE destruction).

**➜ ACTION, not a note: add the `pgbench --homer` remote-RDMA smoke to the per-stage sweep.** It is cheap, it
already works, and it covers a whole source-kind arm the gate cannot. This stops the substrate debt
accumulating **without needing COPY at all.**

### 2.4 🔴 What is covered by NOTHING

- **The legacy fixed-slot / "sequence" payload path.** Every accepted operation maps to a byte-ring family
  (`:4458`), so no W1–W5 reaches the non-byte-ring outgoing body (`:32129+`),
  `HomerServiceApplySequencePayloadCompletionFrontier` (`:17106`), its `SEND_QUEUE` validation (`:17141`), its
  fixed-slot credit (`:17152`), or the fixed-slot receiver's ACK (`:35570`). **Effectively dead at HEAD** —
  and it is still being maintained through every substrate change. **Consider deleting it rather than
  carrying it.**

### 2.5 What is genuinely COPY-ONLY (the real revival surface)

**All control-plane, none of it the shared payload substrate:**

| area | code |
|---|---|
| coordinator COPY routing | `EnableExperimentalTupleSinkRouting` (`shared_library_init.c:2540`); `multi_copy.c:2468`; `MaybeOpenExperimentalRemoteExecutionSession` (`multi_copy.c:2883`, gate `:2892`, SEND intent `:2910`, open `:2934`, worker insert protocol `:2947`); worker ownership `:2625`; TX attach `:2693`; ingest `:2718`; tuple publication `:3157` |
| COPY pairing / receive attachment | `TupleSinkServiceHandlePeerOpenRequest` (`:27715`, opKind guard `:27761`); session resolve/reuse/allocate (`:27902`, `:27927`, `:27959`, `:27964`); **`TupleSinkServiceFindExactReceiveAttachableSink` (`:25285`)** — exact sink key + tuple contract + active binding + live parent + base-compatible session key; selected at `:36930` |
| COPY command admission | peer `START_COMMAND` exact-sink requirement (`:41000`) |
| worker ingestion | `ExecuteRemoteExecBackendCommand` (`remote_execution_backend_bridge.c:2671`, TX-attach gate `:3151`); `WorkerTupleSinkInsertExecuteCopyIngestCommand` (`worker_tuple_sink_insert.c:213`, shard open `:271`, RECEIVE session `:311`/`:334`, drain `:348`) |

Note the socketless-backend **launch** itself is shared (`TupleSinkServiceSubmitBackendSpawnRequest` `:20674`,
`RemoteExecBackendMain` in `postgres-citus/src/backend/postmaster/launch_backend.c:188`) — **not** COPY-only.

---

## 3. Invariants the substrate has NEWLY imposed since COPY last ran

A revived COPY must satisfy all of these. None existed on 2026-07-08.

### 3.1 §0 — THE RESOURCE CONTRACT, and the 4-phase protocol
> A resource may be released / reused / reset / deregistered **only** when its own outstanding physical
> operations have been retired. **DECIDE → QUIESCE → DRAIN → RELEASE.**

For a **SOURCE** buffer — which is what every COPY payload WR reads from — phase 3 means **"my own CQE is
retired"**. Not "a later WR completed", and not "the object is being torn down anyway".

### 3.2 P0-i — the signalling rule
Defer a WR's retirement to a later signalled WR **ONLY IF that later WR is GUARANTEED** (same logical unit, or
already committed). Never on a hope. COPY's byte-ring publish already has the right shape (N unsignalled body
WRs + one signalled tail in the same `ibv_post_send` chain — a guaranteed successor). **Do not break it.**

### 3.3 §25 / F7 — "NO SEND CQE MAY BE DESTROYED" ⚠ *this fix is FOR COPY's path, and COPY has never run it*

The blocking send-CQ wait used to pass **NULL callbacks** into the tagged dispatcher, so a payload / command /
peer-client CQE arriving while it polled was **destroyed** — dequeued by `ibv_poll_cq`, then dropped. That
destroys the owner's only retirement event.

The bug was reachable **only on non-`CRITICAL_CONTROL` lanes**. Its one in-tree caller is
`TupleSinkServicePublishPeerCommandCompletion` (`:19579`) — the terminal command-completion epoch write —
whose **own comment** says it *"serializes terminal command completion across **all tuple COPY shard
sessions**."*

Proof the fix is live, in every service log at startup:
```
tuple-sink service: registered persistent send-completion owner dispatch callbacks=... context=...
```
**If that line is ever ABSENT, every internal/blocking send-CQ drain is running without owner dispatch — the F7
bug in its original form.**

### 3.4 §24 — the WR-ID contract

COPY's payload sends now carry: a disjoint 3-bit `CLASS`; a `KIND` that **selects which owner table** the
handle names; **`OWNER_SLOT` + `OWNER_INCARNATION`** (a **handle**, replacing the absolute 40-bit token); and
`RESERVED_TAIL_SLOTS`.

1. **The 2^40 token wrap ceiling is GONE** — COPY (10M-row runs) was the workload most likely to approach it.
2. **`HomerServiceResolvePayloadSendOwnerToken` ALARMs** on an out-of-range slot, a **non-`POSTED`** owner, or
   a **recycled incarnation**. Those detectors have **never fired**.
3. The `SEND_QUEUE` retirement arm COPY uses **is covered by W3** — *provided we actually run W3* (§2.3).

### 3.5 §23 / P0-c — the staging pools ⚠ IN FLIGHT: read before reviving COPY

The staging pools (`payloadPublishTailBuffers[128]`, `payloadFragmentHeaderBuffers[1024]`) are handed out by a
**blind modulo cursor that counts ATTEMPTS while safety depends on COMPLETIONS** — a `WOULD_BLOCK` retry storm
laps the pool and scribbles over the live RDMA source of an in-flight WR. **COPY is the heaviest producer into
those pools and the workload most likely to trigger that storm.**

P0-c adds rollback of un-posted reservations, a tail frontier retired by the completion's
`RESERVED_TAIL_SLOTS`, and **`WOULD_BLOCK` backpressure**.

**➜ A revived COPY must handle `WOULD_BLOCK` from reservation, and must NOT drain the CQ internally to make
room** — *the owner drains; the reserver reports.* ⚠ And a **PARTIAL** `ibv_post_send` must **NOT** roll back
(some WRs are live); only a **zero-WR** failure rolls back.

---

## 4. What will LOOK like new COPY bugs but are LATENT ones

Expect the first revived runs to surface things that are **not** regressions from the fix:

1. **Any §24 ALARM firing.** Those detectors have never had traffic. A `named a RECYCLED owner slot` or
   `named a non-POSTED owner` on COPY's first run is a **finding** — quite possibly a *pre-existing* bug that
   §24 merely made visible. **Do not assume the revival caused it.**
2. **The ACK one-WR post helper discards `bad_wr`** and reports every failure as zero-posted, so callers roll
   back. Under a nonconforming provider that accepts the WR then returns failure, the ACK owner is freed while
   a CQE is still possible — **§24's incarnation check now catches the resulting stale CQE loudly** instead of
   mis-retiring. Pre-existing; tracked as P1.
3. **CONTROL response retirement ignores the slot generation** (the request path validates it). Pre-existing.
4. **The byte-ring geometry problem (Problem 1).**

---

## 5. The COPY-specific PERFORMANCE landmine (found during F7; unfixed)

`TupleSinkServicePublishPeerCommandCompletion` (`:19579`) publishes the completion record unsignalled, then
does a **blocking synchronous send-CQ wait** on the epoch write. Its own comment:

> *"doing two synchronous writes here **serializes terminal command completion across all tuple COPY shard
> sessions**."*

This is simultaneously **(a)** the site F7 had to fix and **(b)** the leading suspect for the unexplained
**~450 µs/command** on the DPU gate. **A multi-shard COPY will hit it as a hard scaling wall.** Do not
re-baseline COPY throughput without addressing it, or the number will describe this serialization rather than
the transport.

---

## 6. How to validate a revived COPY

**Correctness anchors** (10M-row `\copy`): `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`.
**Intended-path proof:** `published_tail` advancing in the service logs — otherwise it silently fell back to
vanilla libpq COPY and the run proves nothing.

**Plus every P0 gate, none of which existed when COPY last ran:**

- `send WR-ID layout self-test PASSED (5 classes vs GOLDEN ids, plus field boundaries)` — **exactly once** in
  **every** service log. Absence ⇒ the WR-ID layout was never verified.
- `registered persistent send-completion owner dispatch` — present in every service log (F7).
- **`ALARM` count = 0** in every host and DPU service log.
- **Clean SIGTERM** of every service.

⚠ **And the thing this document exists to prevent: do NOT validate the fix against COPY alone either.** Run the
DPU gate, the 4-role basebackup, `pgbench --homer`, AND COPY. The regression we are living with was created by
validating a change against one workload and reasoning that the others were unaffected.
