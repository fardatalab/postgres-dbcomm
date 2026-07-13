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

## HAZARDS (each must be discharged before this lands)

- **H1 — an unenumerated reject path.** Enumerate **every** way the DPU can refuse a START and make each publish a
  `FAILED` completion. This is the one that hangs the client.
- **H2 — sequence desync.** `selectedSession->currentCommandSequence` has ~11 writers. If any advances it
  out-of-band on a selected-DPU CLIENT_SQL session, the client's prediction diverges. Step 2's validation converts
  that from silent corruption into a loud ALARM — but **enumerate the writers** and know which apply.
- **H3 — the control slot is shared with OPEN/CLOSE** (`homer_client.c:3357`, `:3632`). OPEN precedes all commands;
  CLOSE follows the last completion. Safe *by sequencing* — so **assert it**, do not assume it.
- **H4 — ABI bump.** Both DPUs rebuilt. A stale DPU must fail LOUDLY on `protocolVersion`, not silently misparse.
- **H5 — the basebackup shares this code.** `HomerClientDpuSubmitControlRequest` also carries basebackup OPEN/CLOSE
  and tail publishes. **Scope fire-and-forget to `START_COMMAND` only**; the 4-role basebackup must still pass.

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
