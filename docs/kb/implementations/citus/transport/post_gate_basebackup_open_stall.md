# The post-gate basebackup open stall (P0)

**Status: FIXED AND VALIDATED (2026-07-13).** Root-caused statically, every link verified in code; fix implemented,
deployed to both DPUs, and validated on the shape that had failed **100% of the time**.
**Reproduced at `HEAD` (citus `ffe2c4ac9`). ⚠ Misfiled as "DOCA cold-start flakiness" for weeks — that excuse is
dead twice over.**

> **TL;DR.** `!submitted` from the grouped-control scanner meant two opposite things — *"skip this ring"* and
> *"stop the whole scan"* — and the caller implemented only the second. One released arena slot (which stays
> baseline-pending **forever**) therefore aborted every scan pass at a low import index, starving every import
> behind it. A second, independent bug in the same feature meant **12 of 16 arena slots can never clear that flag
> at all**, because the *completion* half of the clear DMA subscripts an **import-local** array with a **global**
> index — while the *submit* half of the same DMA gets it right.

## The symptom

Run the DPU gate to completion, then — **with no restart, no cleanup, no service bounce** — run the 4-role
DPU-relay basebackup. It **fails at stream open, deterministically**:

```
pg_basebackup: error: backup failed: ERROR:  Homer base backup target failed during open stream
DETAIL:  timed out waiting for selected-DPU open selected-DPU basebackup stream response
real 15.10
```

Reproduced back-to-back, identical to the centisecond. **ZERO error or ALARM lines in ANY service log**, on either
DPU or either host. The only `ERROR` is the client-visible one. **The silence is part of the signature.**

A full hard clean baseline + restart cures it; two basebackups then pass.

**PROVEN PRE-EXISTING** by an A/B against git `HEAD` built in a separate worktree (proof: the working-tree-only
symbol `TupleSinkServiceAdmitSendChainRdma` greps to **0** in the host binary and **both** DPU binaries). The gate
passed; the basebackup then failed identically, twice. §29(b) neither causes nor masks this.

---

## THE ROOT CAUSE

> **A released-and-never-respawned arena slot permanently and silently blocks grouped-control discovery for
> EVERY host mmap import at a higher table index.**

`!submitted` from `HomerDpuDmaSubmitOneGroupedControlRead()` is **overloaded with two opposite meanings**, and the
caller cannot tell them apart — so it picks one, and it picks the wrong one:

| meaning | site | correct control flow |
|---|---|---|
| *"this ring has nothing to say — skip it"* (tenancy baseline pending) | `homer_service_dpu_dma.c:11532` | **`continue`** to the next ring |
| *"the engine is out of snapshot buffers — back off"* | `homer_service_dpu_dma.c:11556` | **`return`** — real backpressure |

Both are encoded identically as `return true` with `*submitted` left `false`. The caller
(`HomerDpuDmaSubmitGroupedControlReads()`) implements only the second:

```c
/* homer_service_dpu_dma.c:11532 -- the callee (tenancy-pending skip) */
if (ringRuntime->tenancyBaselinePending)
{
    return true;                  /* success ... and *submitted stays FALSE */
}
```
```c
/* homer_service_dpu_dma.c:1435 -- the caller */
if (!submitted)
{
    if (progressResult != NULL) { progressResult->stillReady = true; }
    return true;                  /* <-- RETURNS FROM THE ENTIRE SCAN. Not `continue`. */
}
```

And the scan **restarts at import 0 on every pass** (`homer_service_dpu_dma.c:1377`, bounded by
`hostMmapImportCapacity`, not by a cursor). So one baseline-pending ring at a low import index is hit on every
single pass, forever, and **no import behind it is ever scanned again.**

### The five links, each read and VERIFIED in code

1. **The gate arms it.** The gate spawns a socketless backend into a frontend-arena slot on the farnet1 DPU. The
   session ends → the arena slot is released → `homer_service_dpu_dma.c:6594` calls
   `HomerDpuDmaResetRingTenancyState()`, which sets **`tenancyBaselinePending = true`**
   (`homer_service_dpu_dma.c:4620`). *(The flag is legitimate: between release and the next tenant's clear, the
   host publish line still holds the DEAD tenant's frontier — reading it would teach the engine a lie. The flag is
   right; only its **plumbing** is wrong.)*
2. **Nothing ever clears it.** There is **exactly one** clear site (`homer_service_dpu_dma.c:10684`), and it fires
   only in the completion of the `ARENA_PUBLISH_LINE_CLEAR` DMA issued **before the NEXT spawn into that same
   slot**. No next spawn ⇒ the flag stays set for the life of the service process.
3. **That ring IS enrolled in grouped discovery.** `HomerDpuDmaDescriptorUsesHostPublishLine()` returns `true` for
   the arena's role-5 ring via the `PUBLISH_LINE` flag branch (`homer_service_dpu_dma.c:7903`). *(⚠ Two scheduler
   comments still claim the arena has **zero** enrolled rings — `homer_service_dpu_dma.c:1238` and
   `tuple_sink_service_process.c:44643`. They are STALE, and that stale claim is exactly what hid this.)*
4. **The basebackup lands behind it.** The basebackup's sender is a PostgreSQL **replication backend** on farnet1
   (`basebackup_homer.c:281`, via `bbsink_homer_begin_backup`) — a *different process* from the postmaster, so it
   opens its **own** DPU setup connection and gets a **new import at a higher table index** than the postmaster's
   frontend-agent arena import (which was created at postmaster start).
5. **It is therefore never scanned.** Deterministic. Silent. Both the callee's `return true` and the caller's
   `return true` are silent by design.

### Why it explains every piece of evidence

| evidence | explanation |
|---|---|
| deterministic, not flaky | it is a state machine, not a race |
| **zero** log lines anywhere | both frontiers are silent by design |
| only ever **after the gate** | the gate is the only workload that spawns-then-releases an arena backend |
| **receiver DPU (farnet0) unaffected** | it only *relays* — it never spawns an arena backend, so no ring is ever baseline-pending |
| cured by a DPU service restart | the restart drops the arena import entirely |
| "mmap import accepted" logged, then nothing | the import is accepted by the **TCP setup path**, an entirely different code path from the scanner |

⚠ That last row is `farnet_operational_hazards.md` §8.1 verbatim — ***an import being accepted proves NOTHING about
it being scanned*** — and we walked into it anyway.

---

## Defect 2 — the clearer is broken TWO ways, and the second one is LIVE

### 2a — the sparse-index bound (latent)

```c
/* homer_service_dpu_dma.c:10674 -- the ONLY site that clears tenancyBaselinePending */
if (spawnOwner->importIndex < engine->hostMmapImportCount && ...)
```

`importIndex` indexes a **sparse** table — the proof is the scanner itself, which loops to
`engine->config.hostMmapImportCapacity` (`:1377`), not to the count. Allocation is **first-free** (`:9431`), and
clearing an import `memset`s that entry **in place, without compaction** (`:8635`), so holes are normal. The moment
the owner's sparse `importIndex >=` the remaining live count, a valid arena import **fails this test and never has
its flag cleared**. Latent today; **it detonates when connection pooling lands (P7b).**

### 2b — GLOBAL index used to subscript an IMPORT-LOCAL array ⚠ **LIVE, and the bigger half**

```c
/* homer_service_dpu_dma.c:10661 -- COMPLETION side.  arenaRingBase is the GLOBAL descriptor index... */
uint32_t arenaRingBase = spawnOwner->arenaSlotIndex * CITUS_REMOTE_EXEC_BACKEND_ARENA_RINGS_PER_SLOT;
...
arenaImport->ringRuntime[arenaRingBase + arenaRingOffset].tenancyBaselinePending = false;  /* ...on a LOCAL array */
```

The frontend arena is **not one export**. Measured from the real headers:

| constant | value |
|---|---|
| `HOMER_FRONTEND_ARENA_SLOT_COUNT` | 16 |
| `HOMER_FRONTEND_ARENA_RINGS_PER_SLOT` | 3 |
| `sizeof(HomerFrontendArenaSlot)` | **13.31 MiB** (a 10 MiB result byte ring dominates) |
| `HOMER_FRONTEND_ARENA_SLOTS_PER_EXPORT` | **4** (a 64 MiB export target) |
| **`HOMER_FRONTEND_ARENA_EXPORT_COUNT`** | **4** |
| rings per arena import | **12** |

So the arena is **four separate mmap imports of 12 rings each**, and each import's `ringRuntime[]` is indexed
**from zero**:

| arena slot | export | clearer's global base | import ringCount | effect |
|---|---|---|---|---|
| 0–3 | 0 | 0, 3, 6, 9 | 12 | **works by accident** (global == local) |
| 4–7 | 1 | 12, 15, 18, 21 | 12 | **≥ ringCount → clears NOTHING** |
| 8–11 | 2 | 24 … 33 | 12 | clears nothing |
| 12–15 | 3 | 36 … 45 | 12 | clears nothing |

**12 of the 16 arena slots can never have `tenancyBaselinePending` cleared** — so an arena slot ≥ 4 is broken on
its **second** tenancy (the flag is only *set* on release). The gate uses one client → slot 0 → which is exactly
why nobody ever saw it.

**The SUBMIT side of the very same DMA gets this right** (`:6951`): it calls `HomerDpuDmaFindArenaSlotImport()` for
the **import-local** base, keeps `globalBase` in a *separate* variable, and validates the descriptor's **global**
`ringIndex` against it. Its own comment warns that *"a **silent partial clear** would recreate the stale-tenant
regression this barrier exists to prevent."* **The completion then does precisely that.** A dedicated global→local
resolver exists and is used at **six** other call sites; the completion is the one place that skipped it.

⚠ **And the diagnostic LIES.** The out-of-range writes are swallowed by the bounds check while the code still sets
`arenaLinesCleared = true` and logs `"publish lines cleared (rings N..M) ... grouped-control discovery re-armed"`.
It reports re-arming rings it never touched.

The DMA itself is **fine** — the submit side computed correct addresses, so the host publish lines really are
zeroed. Only the DPU's *bookkeeping* fails. **Host memory is clean and the DPU refuses to read it, forever.**

### ⚠⚠ SEQUENCING — F1 WITHOUT F2b WOULD HAVE BEEN WORSE THAN THE BUG

Today a stuck flag on slot ≥ 4 yields a **loud, global** stall — everything behind it starves, which at least makes
it *visible*. With F1 alone it becomes a **silent, per-session hang**: only that slot's role-5 result ring is
skipped, so that one backend's results never egress while the whole service looks perfectly healthy. **Fixing the
head-of-line block first would have HIDDEN the deeper bug.** They must land together.

---

## THE FIX — as implemented (after an adversarial review that changed it materially)

### F1 — make the callee say WHICH rejection it is *(the P0 fix)* ✅ IMPLEMENTED

`HomerDpuDmaSubmitOneGroupedControlRead()`'s `bool *submitted` out-param becomes an explicit outcome enum, so the
two rejections stop being **unrepresentable**:

```c
typedef enum HomerDpuDmaGroupedControlOutcome
{
    HOMER_DPU_DMA_GROUPED_CONTROL_SUBMITTED = 0,
    HOMER_DPU_DMA_GROUPED_CONTROL_SKIP_RING,    /* nothing to say  -- SCAN THE REST */
    HOMER_DPU_DMA_GROUPED_CONTROL_NO_CAPACITY   /* backpressure    -- STOP the scan */
} HomerDpuDmaGroupedControlOutcome;
```

Caller: `SKIP_RING` → **`continue`**; `NO_CAPACITY` → `stillReady = true; return true` (today's behavior, which is
correct *for that case*). The **bool return is kept for errors** — ~12 paths use it (this corrects the plan's
original claim that only two producers existed; that was true only of the `return true` producers).

The out-param **defaults to `NO_CAPACITY`, not `SUBMITTED`**: it is the *conservative* outcome. A future edit that
adds a `return true` path and forgets to stamp the outcome then stalls one pass and retries — it does **not**
silently drop a ring from discovery forever. Fail toward "try again", never toward "skip forever".

**`SKIP_RING` deliberately does NOT set `stillReady`.** VERIFIED safe: `stillReady` means "this source is
immediately runnable again", and a baseline-pending ring is not runnable work. Discovery cannot be un-armed by
omitting it, because the collector is armed from `facts->groupedControlRingCount` — the **enrolled** ring count
(`:1250`, `tuple_sink_service_process.c:44635`) — which never filters on `tenancyBaselinePending`. *(This was the
claim I was least sure of, and the one I explicitly asked the reviewer to break. It held.)*

### F2 — use the resolver, on BOTH halves of the DMA ✅ IMPLEMENTED

The completion now calls **`HomerDpuDmaFindArenaSlotImport()`** — exactly as the submit side and six other call
sites do — instead of patching the bad bound. That fixes 2a and 2b in one stroke: the resolver returns a
*validated* import index **and** the **import-local** base, so neither the sparse-index bound nor the global/local
confusion can recur, and the two halves of one DMA can no longer disagree about what an index means.

Plus a **loud ALARM when the re-arm is partial** (`rearmedRings != RINGS_PER_SLOT`) — because after F1, a stuck
flag no longer stalls the service, it silently strands one session's results. That is *harder* to debug than what
it replaced, so it must shout.

### F3 — SILENCE MUST NOT BE A VALID STATE ✅ IMPLEMENTED — but **NOT the version originally planned**

> ❌ **The planned F3 was WRONG and was killed in review.** It would have alarmed on a ring "baseline-pending for
> more than ~1 s". But a released arena slot with **no next tenant** is *legitimately* baseline-pending
> **forever** — its only clear is the next spawn's clear-DMA completion. So that alarm would fire on **every
> normal idle slot**, and — worse — it would fire whether or not F1 was present, so it could never **negative-test**
> the fix. A diagnostic that fires when healthy is not a diagnostic.

**The invariant is not about the flag. It is about COVERAGE.** What is never correct is an *enrolled import the
scanner cannot reach*. So:

- per-import `groupedControlReadsSubmitted`, and engine-wide `groupedControlReadsSubmittedTotal`;
- in `HomerDpuDmaGetSchedulerFacts` (the one function that walks **every** import on **every** pass), latch one
  ALARM per import when it is ACTIVE, has `groupedControlRingCount > 0`, has received **zero** reads, **and** the
  engine has submitted ≥ `HOMER_DPU_DMA_GROUPED_CONTROL_STARVATION_READS` (100 000) reads against *other* imports.

Two properties that make this the right instrument:

1. **It compares the scanner against ITSELF, not against a clock.** No poll-rate guess, and it **cannot**
   false-positive on a genuinely quiet service (which submits no reads at all, so the total never crosses the
   threshold). The gap between "not yet" and "never" becomes enormous — precisely the gap the P0's silent failure
   lacked.
2. **It must NOT live in the scanner.** In the failure it exists to catch, the scanner **returns early and never
   reaches the starved import**. *A detector the bug can silence is not a detector.* This is why
   `HomerDpuDmaGetSchedulerFacts` had to drop its `const` — noted in the header.

### F4 — eager clear at RELEASE: ❌ DEFERRED, with a real hazard found

Issuing the publish-line clear at *release* rather than lazily at the next spawn would make
`tenancyBaselinePending` a genuinely short-lived transient. **But it is not a simple move of the call.** The arena
allocator considers a slot free the moment all three `boundServiceSessionId`s are zero
(`homer_service_dpu_dma.c:5993`) and **rebinds it immediately** (`:6036`) — so a clear DMA issued after unbind can
land **after the next tenant has published**, and erase it. It needs a distinct *"cleaning"* ownership state and an
ordering of quiesce → drain → clear → **await** clear → expose-as-free. Not required for correctness once F1 lands
(a skip is then just a skip). Left as a design option.

### Also corrected while here

- **The stale comment that hid this bug.** `homer_service_dpu_dma.c:1237` asserted the enrolled roles were a
  whitelist of `{1,4,7}` and that *"the Homer frontend arena imports 49 rings of roles 2/3/5/8, i.e. **ZERO
  enrolled**."* **False** since role-5 result rings gained the `PUBLISH_LINE` flag. That single sentence is why
  three separate investigations never looked at the arena at all.

### Known, still-open (found in review, NOT fixed here)

- `homer_service_dpu_dma.c:1726`/`:1745`: backend-completion scanning **assigns** rather than ORs
  `progressResult->stillReady` — a later class/ring can overwrite an earlier backpressure indication.
- `tuple_sink_service_process.c:46946`: the service adapter **discards** `dmaProgress.stillReady` and
  `budgetExhausted`, copying only `submittedTasks`. So `NO_CAPACITY`'s `stillReady` never actually reaches the
  scheduler. Harmless today; misleading to read.
- The KB's earlier claim that the basebackup import *necessarily* lands at a **higher** index than the arena
  import is **INFERRED, not guaranteed** — allocation is first-free (`:9431`), so with holes a new import can land
  earlier. The mechanism is "everything at a **higher** index than a baseline-pending ring is starved"; which
  imports those are is ordering-dependent.

### Validation — ✅ PASSED (2026-07-13, two independent sessions)

**gate → basebackup → gate → basebackup, with NOTHING restarted between any of them.** Before the fix, *every*
post-gate basebackup died at open after 15.10 s. After:

| session | basebackup 1 | basebackup 2 | ALARMs |
|---|---|---|---|
| first (F1+F2+F3 v1) | 23,245,298,903 B, **18.19 s** | 23,245,326,040 B, **18.18 s** | ⚠ 1 × F3 false positive (see below) |
| second (F3 v2) | 23,245,527,247 B, **17.64 s** | 23,245,546,191 B, **17.84 s** | ✅ **zero, on both DPUs** |

Both sessions: gate 5/5 processed / 0 failed with five distinct decoded `abalance` values; `transport:` line reads
`homer-dpu-command (implies dpu result relay)`; a **fresh** `DPU backend spawn ... COMPLETED launched_pid=N` per
gate run on the farnet1 DPU (distinct pids); the farnet0 host service log **banner-only** throughout (so no silent
fallback to the deprecated host-service arm). Landed-proof string confirmed in all three binaries before each run.

The farnet1 DPU's re-arm line, verbatim and identical on every gate run:
```
arena slot 0 publish lines cleared before spawn; grouped-control discovery re-armed
    (import=0 local_rings=0..2 global_rings=0..2)
```
⚠ **`local == global` here — because it is slot 0.** This line is *exactly* the case Defect 2b gets right by
accident. It is evidence the fix did not *break* the working case; it is **not** evidence that 2b is fixed.

### ⚠⚠ F3 v1 FALSE-POSITIVED ON ITS FIRST RUN — and the fix is a lesson worth more than the alarm

First session, farnet0 DPU, once, immediately before gate run 2:
```
ALARM grouped-control discovery has NEVER polled import=0 (enrolled_rings=2 ring_count=3 ...)
      although the engine has submitted 5703404 reads against other imports
```
**The alarm was WRONG, and the run's own evidence refuted it:** `ring_count=3` is not an arena import (those have
12 rings) but a client's control import — and the very next gate ran **5/5 through it**, so "never polled" was
false.

**Root cause of the false positive: the threshold was the engine's LIFETIME read total, which makes it a RACE.**
On a service that has been up a while that total is already millions, so **every newly-arrived import trivially
clears the threshold the moment it appears** — and any `GetSchedulerFacts` pass landing in the microseconds between
"import goes ACTIVE" and "the scanner's first read against it" fires on a perfectly healthy import.

That also explains why **only farnet0's DPU** alarmed: farnet1's DPU takes its import from the postmaster's
frontend agent, created **once at startup** while the total was ~0; farnet0's DPU takes a **fresh import per pgbench
run**, arriving when the total is already 5.7M.

**Fix (F3 v2):** snapshot the engine total at activation (`homer_service_dpu_dma.c:8207`, the *sole* site that sets
`..._ACTIVE`) and test `(total − baselineAtActivation) >= THRESHOLD` — *"the scanner has done a great deal of work
elsewhere **since you arrived** and still never touched you."* Re-validated: **zero ALARMs.**

> **This is the "a probe cannot tell NOT YET from NEVER" rule, violated one level up.** F3 was *designed* to escape
> it by comparing the scanner against itself instead of a clock — and then compared against its **lifetime** total
> rather than reads **since this import arrived**. Same trap, different hat.
>
> **The alarm caught ITSELF being wrong** — loudly, on a passing run, with every number needed to diagnose it, at a
> cost of one rebuild. That is the whole argument for building the detector even when the bug it targets is already
> fixed.

### Still NOT established (do not read the PASS as covering these)

1. **F3 has not been NEGATIVE-tested.** It is now genuinely negative-testable (revert F1 → the starved import gets
   no reads → it must fire). Until that is run, F3 is an **untested detector**, which by our own rule is decoration
   (hazards §3.2/§3.3).
2. **Defect 2b is verified by READING, not by a workload.** It breaks arena slot **≥ 4 on its SECOND tenancy**, and
   a single-client gate only ever touches slot 0 — every validated run above is in the one range the old code got
   right. Needs a multi-client / churned-session workload.
3. The full regression sweep (DPU TCP transport smoke in particular) has not been re-run against this change.

⚠ **Both DPUs must be rebuilt and redeployed** — this is DPU-side code. No ABI change (no struct layout touched),
so no protocol-version bump and no `BAD_PROTOCOL`; but a stale DPU binary would silently keep the bug, so **prove
the deploy landed** by grepping the binary for a string only the new code emits.

⚠ **`strings BIN | grep -qF <proof>` under `set -o pipefail` REPORTS FAILURE ON SUCCESS.** `grep -q` exits at the
first match, `strings` then takes `SIGPIPE`, and `pipefail` propagates that as a non-zero pipeline status. This bit
during *this* deploy: the landed-proof said FAILED on a binary that provably contained the string. Drop the `-q`
(plain `grep -F … >/dev/null` drains its input, so there is no SIGPIPE). **An instrument whose success is
indistinguishable from its failure is the same class of bug as the one being fixed here.**

---

## What I got WRONG, and why it is worth recording

1. **"The sender DPU never *sees* the request."** Right frontier, **wrong reasoning** — arrived at by luck. The
   `dpu control slot: OPEN_SESSION` line is printed by the *stager*
   (`tuple_sink_service_process.c:43972`), several hops **downstream** of discovery, *after* command-pull
   acceptance. Its absence never proved that discovery was the failing frontier; it was consistent with a failure
   at any of ~4 hops. **A downstream log's absence does not localize an upstream frontier** — that is precisely
   what the numbered-probe-at-every-frontier discipline exists to prevent, and I skipped it.
2. **Ranked hypothesis #1 (epoch equality at `:12875`) is REFUTED.** `acceptedPublishedEpoch`/`acceptedPublishedTail`
   live in `HomerDpuDmaRingRuntime` (`:437`, `:593`) — **per ring, per import** — and every accepted import gets a
   freshly zeroed runtime array (`:8038`). A new client's epoch can never collide with an old client's. The
   "shared accepted epoch" story was plausible, tidy, and wholly imaginary.
3. **Ranked hypothesis #2 (stuck `controlReadInFlight`) is REFUTED as the cause.** It is set only at submission
   (`:11653`) and cleared on *every* retirement path including error callbacks (`:10763`, `:14765`, `:14806`). And
   decisively: a set `controlReadInFlight` causes a **`continue`** (`:1422`) — it skips one ring. It is
   *structurally incapable* of the cross-import starvation observed. The bug needed the `return`, and only the
   tenancy path reaches it.
4. **Ranked hypothesis #3 (pending-response starvation) is REFUTED.** Pending responses are transient per-command
   records, not session-held resources, and that gate sits **downstream** of the missing log anyway.

**The common thread:** all three refuted hypotheses were about a *value being wrong*. The actual bug was about
**control flow** — a `return` where a `continue` belonged. Reading for bad data missed a bug that was entirely
about bad routing.

### And what the ADVERSARIAL REVIEW then broke in the FIX (this is why the loop is the default mode)

The root cause above was correct. **The fix plan built on it was not**, and a friendly review would have passed it:

5. **The planned F3 alarm was WRONG, and would have shipped a false-positive generator.** "Baseline-pending for
   > 1 s = stuck" is false: a released slot with no next tenant is *legitimately* pending forever. It would have
   fired on every healthy idle slot **and** could never have negative-tested F1. **The better design came out of
   the objection** — the invariant is *coverage* (an enrolled import the scanner never reaches), not the flag's age.
   That is the pattern to expect: *when a review breaks your design, the reason it broke is often the mechanism the
   correct design needs.*
6. **F1 alone would have been WORSE THAN THE BUG.** Without F2b, a stuck flag stops being a loud global stall and
   becomes a *silent per-session hang*. The sequencing constraint only became visible because the reviewer went
   looking for the *second* indexing defect after being told the first one was real.
7. **"Only two `!submitted` producers" was half-wrong** — two produce `return true`; about a dozen error paths also
   leave it false but return `false`. Enough to have mis-shaped the enum.
8. **The "higher import index" premise was INFERRED, not proven.** Allocation is first-free; with holes, a new
   import can land *earlier*. The mechanism survives, but the confident phrasing did not.

**And one from the deploy itself, which is the same bug in miniature:** the landed-proof
`strings BIN | grep -qF <proof>` reported **FAILURE on a binary that provably contained the string** — `grep -q`
exits early, `strings` takes SIGPIPE, `pipefail` turns that into a failure. *A diagnostic whose success is
indistinguishable from its failure* — the exact disease being cured, reproduced in four words of shell, while
curing it.

## Related
- `farnet_operational_hazards.md` §8.1 — *an import being accepted proves nothing*. Violated here.
- `farnet_operational_hazards.md` — the stale "cold-start flakiness" note that misdirected two investigations.
- `resource_retirement_contract_audit.md` §29 — the send-queue accounting, exonerated here by the A/B.
- `dpu_command_plane_migration_plan.md` — P7b connection pooling, which **Defect 2 would break**.
