# P-START: remove the synchronous START round trip on the selected-DPU command path

**Status: PLANNED, not implemented. Measured 2026-07-13.**
**Motivation is NOT performance tuning — we are not tuning yet. It is that this round trip is pure waste and we
should not carry it forward into pipelining, pooling, or the completion-ring work that all build on top of it.**

## THE MEASUREMENT (not arithmetic — this time it is measured)

`pgbench --homer --homer-dpu-command -c 1 -j 1 -t 2000`, three warmed runs, nanosecond instrumentation inside the
client's own spin:

| | run 1 | run 2 (median) | run 3 |
|---|---|---|---|
| START_COMMAND wait, mean | 241.1 µs | **235.4 µs** | 239.3 µs |
| samples (`n`) | 14002 | 14002 | 14002 |
| control-wait **per transaction** | 1.688 ms | **1.648 ms** | 1.675 ms |
| pgbench latency average | 3.488 ms | **3.507 ms** | 3.541 ms |
| **fraction of every transaction** | 48% | **47%** | 47% |
| tps | 286.7 | 285.1 | 282.4 |

7.001 statements/transaction; 2000/2000; 0 failed; correct `transport:` line; zero DPU alarms.

> ⚠ **The earlier "~450 µs/command" was ARITHMETIC (3.14 ms ÷ ~7 statements), and it was 2× wrong.** The real
> figure is ~235 µs. The conclusion got *stronger*, not weaker — but the lesson stands: *a prediction that matches
> is not a measurement.*

### ⚠ AND 235 µs IS **NOT** A DMA COST — READ THIS BEFORE ASSUMING THE FIX RECOVERS 47%

A DOCA DMA round trip over PCIe is **~2–5 µs**. We are **50–100× above the physical cost.** So the 235 µs is not
the wire — it is a **POLLING PERIOD**. The client is not waiting for a transfer; it is waiting for the DPU to
*look*.

The wait therefore decomposes as **D + R**:
- **D** — the DPU's *discovery* latency: how long until grouped-control notices the publication.
- **R** — pull + stage + DMA the response back + the client noticing it.

**Removing the round trip removes R. It does NOT remove D** — the DPU must discover the command before the backend
can run it, and that is on the critical path either way. **The split is UNMEASURED.** *(Suggestive: 235 µs of START
wait + 266 µs of everything-else ≈ two equal ~250 µs phases, which smells like one discovery quantum per hop. Do
not act on that until it is measured.)*

**So this plan is justified as WASTE REMOVAL, not as a 47% win.** The 47% figure must not be quoted as the expected
speedup. Decomposing D vs R is separate, later work — and if D dominates, *the DPU's poll loop is the real target*
and is a far more interesting finding than this one.

## What the round trip actually buys — all three items are removable

The client's **entire** use of the START response (`homer_client.c:6391-6414`):

| # | what it reads | why it can go |
|---|---|---|
| 1 | `*commandSequence = response->commandSequence` | it is just `currentCommandSequence + 1` (`tuple_sink_service_process.c:43001`). **No DPU information whatsoever.** The client already mints exactly this on the host-shm path — `HomerClientReserveDirectCommand`: `nextSequence = publishedEpoch + 1` (`homer_client.c:5905`). |
| 2 | `startCompletion` ← `response->commandCompletion` | **documented as always `PENDING`** (`tuple_sink_service_process.c:43008`). Synthesize it locally. |
| 3 | `HomerClientCheckResponseHeader()` → `statusCode` | a **rejection**. The caller *already* blocks on the completion event immediately afterwards; a rejection can ride that as `FAILED`. |

### ⚠ THE FIX ALREADY EXISTS — ON THE OTHER BRANCH OF THE SAME FUNCTION

`HomerClientStartCommandWithCompletionFlags` forks two ways (`homer_client.c:6266`):

| branch | taken when | shape |
|---|---|---|
| `HomerClientStartDirectCommand` | `commandMailbox != NULL` | sequence minted **locally**, **multi-slot** command ring, real backpressure. **No round trip.** |
| control-slot path | otherwise — **the selected-DPU path** | **depth-1, publish-and-block** |

And the code says so out loud: *"A selected-DPU command session **deliberately has `control == NULL`**; its
`commandDpuStream` owns the already-open **role-1 control slot** instead."*

**The DPU command plane was built on the COLD-PATH request/response mechanism, while the host path it replaced
already had the HOT-PATH ring with local sequence minting.** This is not new design work; it is bringing the DPU
path up to what the host path has had all along.

## THE DESIGN

1. **Client mints `commandSequence`** — per-session monotonic, and **carries it IN the request**. New field in
   `CitusRemoteExecStartCommandRequest` ⇒ `CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION` **32 → 33**, both DPUs
   rebuilt.
   *(We could have both sides count independently and transmit nothing — zero ABI cost. **Rejected:** today's P0
   was a **silent desync** between two halves of one operation. Pay the bump; buy the cross-check.)*
2. **DPU VALIDATES, does not mint.** `request->commandSequence == selectedSession->currentCommandSequence + 1`,
   else **loud ALARM + fail the command** via a `FAILED` completion. Never silently accept a mismatch.
3. **DPU stops writing the START response** for selected-DPU sessions — one fewer DMA per command, and it frees a
   pending-response slot (that pool is only 16 deep).
4. **Client does not wait.** `HomerClientDpuSubmitControlRequest` gains a fire-and-forget mode, **scoped to
   `START_COMMAND` only**.
5. **Slot reuse is gated on the COMPLETION, not on the response.** ✅ **DEPTH 1 STAYS SAFE AND NO RING DEEPENING
   IS NEEDED**: by the time the completion event arrives, the DPU has *provably* pulled the request slot (it could
   not have executed the command otherwise). pgbench already enforces one-command-at-a-time (`st->homer_command_pending`,
   `pgbench.c:4216`). **Do NOT release the slot at publish time** — a later `CLOSE_SESSION` could then clobber an
   un-pulled START.
6. `startCompletion` synthesized locally as `PENDING`.

### ⚠⚠ THE LOAD-BEARING INVARIANT

> **EVERY command the client issues MUST receive EXACTLY ONE completion event — success OR failure.**

Any DPU-side rejection that used to come back as a synchronous `statusCode` must now publish a **`FAILED`
completion for that exact sequence.** **Miss one reject path and the client hangs for 30 s.** This is precisely
libpq's `PGRES_PIPELINE_ABORTED` discipline (`fe-exec.c:3266`) — the server skips work, but *something* must still
produce one result per issued command, or the two sides lose lockstep forever.

## THE ADVERSARIAL REVIEW — what it changed (2026-07-13)

### ✅ H1 IS **NOT** A BLOCKER — and the reviewer's own finding is why

The reviewer called the missing FAILED-completion funnel a **BLOCKER**. **It is not**, and I verified why:

```
tuple_sink_service_process.c:42887  TupleSinkServiceSetOkResponse(&startResponse->header, ...START_COMMAND...)
tuple_sink_service_process.c:43016  TupleSinkServiceSetOkResponse(&startResponse->header, ...START_COMMAND...)
```
**Two `SetOkResponse` sites. ZERO `SetErrorResponse` sites.** A START response can *only ever* say OK. START also
bypasses the generic dispatcher (`:43956`), so it never reaches the generic error-response machinery.

> **THERE IS NO SYNCHRONOUS START REJECTION TODAY.** A failed START writes **no response at all**, and the client
> simply spins to its 30 s timeout. After the change, a failed START produces **no completion**, and the client
> spins to its 30 s timeout. **IDENTICAL BEHAVIOR. Removing the wait loses nothing, because nothing was there.**

So the FAILED-completion funnel is a **separate quality improvement** that fixes a hang which *already exists*
today — **not a prerequisite.** The reviewer called it a blocker because it assumed the synchronous path was
reporting errors. It never was. **Tracked separately; see "Deferred" below.**

### The reviewer's four REAL findings (all folded in)

- **R1 — THE SLOT LEASE MUST BE LIBRARY-OWNED.** My plan leaned on pgbench's `st->homer_command_pending`
  (`pgbench.c:4216`) — i.e. **relying on a CALLER to maintain a LIBRARY invariant.** The client library has no
  selected-DPU slot lease at all, and the completion ACK (`homer_client.c:6732`) advances only the event cursor,
  touching nothing on the role-1 slot. **The library must own an "outstanding START" identity and mark the slot
  FREE only when THAT command's terminal completion is acknowledged.**
- **R2 — DELETING THE RESPONSE IS NOT ENOUGH; DELETE ITS CAPACITY GATES TOO.** START currently defers on
  pending-response capacity (`:42805`, `:42950`) and the scheduler arms/clips on `pendingResponseFreeSlots`
  (`:44707`, `:44719`). Leave those and **START still silently stalls behind unrelated OPEN/CLOSE responses**, on a
  pool it no longer uses. ⚠ But **keep the pool itself** — non-START responses still need it — and **do not remove
  `publishSlot->frontendCommand`**; close-drain ownership matching reads it (`:42374`).
- **R3 — THERE ARE THREE OTHER START PRODUCERS.** `HomerFrontendDmaStartCommand` (`homer_frontend_control.c:1189`,
  the citus.so frontend-agent path) and **both transport smokes**
  (`homer_dpu_comch_transport_smoke.c:693`, `homer_dpu_tcp_transport_smoke.c:617`) construct a START request
  directly. **An unconditional `commandSequence != 0` validator breaks all three.** Every producer must fill the
  new field.
- **R4 — ⚠ A "CLOSE" IS A START.** The SQL **semantic close** is a `START_COMMAND` whose `commandKind` is
  `CLIENT_SQL_SESSION_CLOSE` (`homer_client.c:3530`) — a *verb the backend executes*, not a channel teardown. The
  **lifecycle close** (`CLOSE_SESSION`) is the separate control request that tears the session down. Consequences:
  **the semantic close CONSUMES a commandSequence** (miss it and every later sequence is off by one, so the DPU's
  validator ALARMs on every subsequent command); it inherits fire-and-forget automatically (same function); and it
  **already waits for its own completion** (`:3539`), so the role-1 slot is provably free before the lifecycle
  CLOSE reuses it. *The ordering constraint holds already — but only by luck, so ASSERT it.*

### ✅ Claims the review CONFIRMED

- **C2 — sequence minting is safe.** `selectedSession->currentCommandSequence` has **four** writers, not eleven
  (`:42925`, `:43044`, `:43352`, `:44456`). Three are consequences of client START admission. The one out-of-band
  writer (teardown's synthetic `BACKEND_SLOT_RELEASE`, `:44434`) is reached **only after a terminal completion has
  begun teardown** (`:43516`) and cannot desynchronize a live session. *(The other apparent writers act on
  `TupleSinkServiceSessionState`, a different struct.)*
- **C3 — no late READ of the request slot.** The DPU DMA-reads it once (`homer_service_dpu_dma.c:12177`), and all
  later work uses a **service-owned copy** (`:43002`, `:47125`). ⚠ **But there IS a late WRITE** — see the ABSOLUTE
  below.
- **C4 — nothing but the client reads the START response.** (`homer_client.c:6391` is its sole consumer.)
- **C5 — the basebackup does NOT issue START.** Scoping fire-and-forget to `START_COMMAND` is safe. *(Correction:
  basebackup tail publication doesn't even use this helper — it publishes its host line directly, `:2385`.)*
- **C6 — PENDING is correct on every explicit selected-DPU START path** (`:42885`, `:43014`). ⚠ **Preserve
  `commandFlags`**, currently copied at `:42894`/`:43023`.

### ⚠⚠ ABSOLUTE, from C3 — CEASING THE CLIENT'S WAIT IS NOT ENOUGH

> **The DPU's START response DMA must be DELETED, not merely ignored.**

The response is written as a body DMA + a `RESPONSE_READY` DMA (`homer_service_dpu_dma.c:1951`, `:2083`), and the
scheduler services completion events **before** pending responses (`:47434`, `:47458`). So if the client stops
waiting but the DPU still writes, **a late response DMA lands on a slot the client has already reused** for the
next START — or for the lifecycle CLOSE. **Silent memory corruption of the next request.** Delete the write.

## HAZARDS (each must be discharged before this lands)

- **H2 — sequence desync.** Resolved by C2, but keep step 2's validation: convert any divergence into a **loud
  ALARM**, never silent corruption. (Today's P0 was a silent desync between two halves of one operation.)
- **H3 — the control slot is shared with OPEN / semantic-close-as-START / lifecycle CLOSE.** Safe by sequencing —
  so **assert it**, do not assume it. R1's library-owned lease is what makes the assertion possible.
- **H4 — ABI bump.** `CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION` 32 → 33. Both DPUs rebuilt. A stale DPU must fail
  **LOUDLY** on `protocolVersion`, not silently misparse.
- **H5 — the basebackup shares this code.** Resolved by C5; still validate it.

## DEFERRED (real, tracked, NOT part of this change)

- **The FAILED-completion funnel.** Today, *dozens* of DPU-side paths accept or drop a START and produce neither a
  response nor a completion — pull-acceptance rejects (engine-fatal), stage-capacity deferrals that can defer
  forever, materialization failures, a cross-DPU egress "dropping" path (`:4046`), receiver-side silent rejection
  after backend teardown (`:43272`), and no DPU-side command deadline that manufactures a FAILED completion if the
  backend simply never publishes one (`:42420`). **Each is a 30-second client hang TODAY.** Worse, several leave
  the staged-queue head un-released (`homer_service_dpu_dma.c:5802`), which head-of-line-blocks every later ring —
  the same disease as the discovery P0. **This deserves its own centralized funnel: reject START → release the
  staged owner → advance/fence the sequence → publish exactly ONE FAILED completion.** It is not made worse by this
  change, and it should not be smuggled into it.

## VALIDATION

1. The gate passes (`-t 5`, `--debug`, all four proofs).
2. **Re-measure**: the `requestKind=4` control-wait must **vanish** from the timing dump, and latency must drop.
   *(Quote the measured delta. Do NOT pre-announce 47%.)*
3. **NEGATIVE TEST — H1's discharge:** force a DPU-side START rejection and confirm the client receives a `FAILED`
   completion **and does not hang**. An untested reject path is exactly the 30-second hang this plan can create.
4. The **4-role DPU-relay basebackup** still passes (H5).
5. Both DPU logs clean under the FULL grep (not just `ALARM` — see hazards §3.3b).

## Related
- `dpu_gate_concurrency_limits.md` — the 8-client byte-ring cap; the same client path.
- `post_gate_basebackup_open_stall.md` — the silent-desync P0 that justifies paying for the cross-check in step 2.
- Pipelining (a **separate, larger** design) — needs a deeper **completion-event** ring, which is depth-1 and
  **ABI-enforced** (`homer_service_dpu_dma.c:2005` rejects `slotCount != 1`), plus skip-until-sync. **This plan is
  a prerequisite for it, not a substitute.**
