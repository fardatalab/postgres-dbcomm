# The post-gate basebackup open stall (P0, PRE-EXISTING)

**Status: OPEN. Root cause NOT established. Reproduces at `HEAD` (citus `ffe2c4ac9`).**
**⚠ This has been misfiled as "DOCA cold-start flakiness" for weeks. That excuse is dead twice over.**

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

## ⚠ PROVEN PRE-EXISTING — A/B against HEAD

Built git `HEAD` in a **separate worktree** and deployed it (proof: the working-tree-only symbol
`TupleSinkServiceAdmitSendChainRdma` and its string grep to **0** on the host **and both DPU binaries**). The gate
passed; the basebackup then failed **identically**, twice. **§29(b) neither causes nor masks this.**

## ⚠ THE DECISIVE ASYMMETRY (the strongest lead we have)

The two DPUs behave **differently**, and that is the whole clue.

**farnet0 DPU (the RECEIVER)** — processes its control request normally:
```
DPU TCP setup mmap import accepted ...
[homer-service] dpu control slot: OPEN_SESSION opKind=1 ... dest_node=2      <-- SEEN
tuple-sink service: basebackup receive-open sessionUID=... tag=<N> dest_node=2
tuple-sink service: created sink session=... send_queue=...
tuple-sink service: recorded placement history ...
```
...and then the log **ends**. It created the sink and waits.

**farnet1 DPU (the SENDER)** — accepts the new mmap import, and then **NOTHING**:
```
DPU TCP setup mmap import accepted ...
                                       <-- NO "dpu control slot: OPEN_SESSION" LINE. EVER.
(15s later, after the client gives up)
setup import closing -> host-detached -> reclaiming host-detached import
```

> ⚠ **THE SENDER DPU NEVER PROCESSES THE OPEN_SESSION CONTROL REQUEST AT ALL.** It is not failing the request —
> it never *sees* it. The host published it; the DPU never picked it up. **This is a DISCOVERY failure, not an
> open-handling failure**, and the receiver DPU proves the open handler itself is fine.

## Hypotheses, ranked (all INFERRED; none confirmed)

1. **Grouped-control discovery on the sender DPU is stuck for the NEW import.** A new publication is **silently
   ignored** when its observed epoch equals `acceptedPublishedEpoch`
   (`homer_service_dpu_dma.c:12875`) — no log, by design. The gate's imports may leave that state such that the
   basebackup's fresh import is never accepted.
2. **A stuck `controlReadInFlight`** — the scanner **skips** rings with a read in flight
   (`homer_service_dpu_dma.c:1422`). A leaked in-flight read on a ring would silently starve it forever.
3. **Staged-dispatch / pending-response starvation** — dispatch silently waits when all 16 pending-response slots
   are occupied (`tuple_sink_service_process.c:44064`). Silent, by design.

⚠ **Every one of these is SILENT BY DESIGN**, which is exactly why the logs are empty. *Silence must not be a valid
state* — that is the standing rule, and this is what it costs when it is violated.

## The decisive instrumentation (one run, cuts the whole space)

Probe every frontier the request must cross, and **the first one that does not report is the bug**:

| probe | at | if absent, the fault is |
|---|---|---|
| host publication | after `HomerClientDpuPublishHostLine()`, `homer_client.c:2585` — print bridge generation, request kind/sequence, host-line epoch/tail, slot state | the host never published |
| DPU discovery | `HomerDpuDmaAcceptGroupedControlSnapshot()`, `homer_service_dpu_dma.c:12835` — print import/ring index, tenancy generation, **observed epoch/tail vs `acceptedPublishedEpoch`/`acceptedPublishedTail`**, and **`controlReadInFlight`** | **import discovery / read scheduling — HYPOTHESES 1 & 2** |
| command pull | `HomerDpuDmaAcceptCommandPullSlot()`, `homer_service_dpu_dma.c:13086` | the pull never ran |
| service dispatch | `TupleSinkServiceDispatchLocalControlSlot()`, `tuple_sink_service_process.c:44078` — print staged/pending counts | staging starvation — HYPOTHESIS 3 |

⚠ Probe the **NEGATIVE** cases and print **the values that were compared** — an equality test that silently drops a
publication must say *which* epochs it compared.

## Why this matters beyond itself

**It makes back-to-back workloads unrunnable**, which is why the regression sweep has been quietly serialising
behind full restarts — and it is the reason the "run the gate, then the basebackup" shape kept looking flaky. It
also blocks the concurrent foreground-pgbench + background-basebackup shape that the runbook flags as open work.

## Related
- `farnet_operational_hazards.md` — the stale "cold-start flakiness" note that misdirected two investigations.
- `resource_retirement_contract_audit.md` §29 — the send-queue accounting, exonerated here by the A/B.
