# Farnet diagnostics and measurement history

<!-- kb-summary: Diagnostic builds and network batteries, SHA-stamped measurement history, and intentionally non-runnable broken command shapes for the farnet Homer rig. -->

## Purpose

Two things that do **not** belong in the runbook but must not be lost:

1. **Diagnostic batteries** — the checks you run when something is *already* broken, or when you are about to
   make a fresh absolute performance claim. They are **not** routine preconditions; running them on every
   iteration is waste.
2. **Measurement history** — what we actually measured, when, and on which commit. Every entry is a
   **timestamped observation, not a standing fact**, and several are now known stale. They are kept because a
   number you cannot date is a number you cannot retire.

The always-on guards live in repository `AGENTS.md`; exact current preflight, correctness anchors, and intended-path
procedure live in `farnet_operator_runbook.md`.

## Status

Living. See `farnet_operational_hazards.md` for the traps; this doc is the instruments and the readings.

---

## 1. Diagnostic builds

### 1.1 The stats macros

```sh
# payload / basebackup counters
CPPFLAGS='-D_GNU_SOURCE -DHOMER_SERVICE_PAYLOAD_STATS=1 -DHOMER_CLIENT_BASEBACKUP_STATS=1'

# scheduler / action-grant accounting
CPPFLAGS='-D_GNU_SOURCE -DHOMER_SERVICE_PROGRESS_STATS=1 -DHOMER_SERVICE_PEER_TRANSPORT_STATS=1'

# the positive "bound frontend arena slot=<N>" line in a socketless backend
CPPFLAGS='-D_GNU_SOURCE -DHOMER_REMOTE_EXEC_TRACE=1'
```

**Exit rule (load-bearing):** a stats build is *sticky*. `make -B` in, `make -B` out, and **prove** the
instrumentation is gone. See `farnet_operational_hazards.md` §3.1 — a plain `make` will happily relink the
instrumented objects.

### 1.2 The THREE starvation diagnostics — pick the right one

⚠ **Three similarly-named macros answer DIFFERENT questions. The first two both print lines prefixed
`[starve-diag]`, so the log prefix does not tell you which build you are reading.**

| macro | answers | emits | prefix |
|---|---|---|---|
| `HOMER_DPU_SETUP_STARVE_DIAG` | "is the service wedged / is the listener frozen?" | a per-pass counter line for **four** actions: PE_DRAIN, SETUP_LISTENER, DOORBELL, SPAWN | `[starve-diag]` |
| `HOMER_COLLECTOR_STARVE_DIAG` | "**WHICH** collector is being dropped, and by **which gate**?" | a `(kind, reason)` line per drop for **every** collector, plus a service-exit census | `[starve-diag]` |
| `HOMER_CONTROL_MAILBOX_STARVE_DIAG` | "is a peer-control TRAFFIC CLASS being enumerated but never planned?" | a per-traffic-class service-exit census of ready vs starved passes | `[mailbox-diag]` |

**If you are asking which collector is starved, you want `HOMER_COLLECTOR_STARVE_DIAG` (§1.2b).** The
setup diagnostic is structurally blind to every collector outside its four, which is the exact defect that
made it useless during the FB-0 hunt.

**⛔ THE COLLECTOR CENSUS CANNOT SEE CONTROL-MAILBOX STARVATION, AND WILL LOOK CLEAN WHILE IT HAPPENS.**
Control-mailbox actions are truncated to `HOMER_CONTROL_MAILBOX_ACTIONS_PER_PASS` (2 of 8 enumerated)
**before** budget accounting, so a starved traffic class raises **no** drop in `[starve-diag]` and appears in
the admission report as neither granted nor dropped — identical to healthy idle. Use §1.2c for that question.

#### 1.2a `HOMER_DPU_SETUP_STARVE_DIAG` — when the DPU service "does nothing"

**Symptom:** the DPU service spins at 100% CPU, logs nothing, and stops accepting setup connections
(`ss -ltn` on the DPU shows a **nonzero `Recv-Q`** on the `9727` LISTEN socket).

```sh
CPPFLAGS='-D_GNU_SOURCE -DHOMER_DPU_SETUP_STARVE_DIAG=1'
```

```
[starve-diag] pass=46500001 pedrain_grants=428773 listener_grants=90831 imported=1 rings=49 inflight=0 \
              tcp{due=0 active=0 polls=90831 accepted=4 acks=3 errs=1}
```

⚠ This macro emits **grant counters only**. The `… dropped: <reason>` lines belong to
`HOMER_COLLECTOR_STARVE_DIAG` (§1.2b) — do not expect them from this build.

**How to read it (post-S3.2, citus `52fd1ab3d`):**

- **`listener_grants`** must climb at **one grant per `HOMER_SERVICE_DPU_SETUP_TCP_IDLE_POLL_INTERVAL_PASSES`
  (512) passes** when idle, and keep roughly that cadence while `pedrain_grants` is climbing fast.
  **A frozen `listener_grants` IS the wedge.**
- **`doorbell_grants`** climbs at **one per 512 passes while unattached** and **one per 4096 while attached**
  (`bell{attached=1}`). If it climbs *every* pass, someone copied the setup listener's interval rule into a
  persistent connection — a silent per-pass `recv()`/`EAGAIN` on the hot loop.
- **`polls == grants` exactly, for BOTH listeners** (`tcp{polls=…}` and `bell{polls=…}`). Any drift means a
  grant retired a poll obligation **without polling**.
- **`SETUP_LISTENER dropped` and `DOORBELL dropped` must never print** — both draw from the reserved lifecycle
  grant line. ⚠ Those lines come from the **collector** diagnostic (§1.2b), not from this build; to check the
  rule you must build with `HOMER_COLLECTOR_STARVE_DIAG`.
- **`bell{rej=N>0}`** means an ATTACH named a bridge generation with no live ACTIVE import — normally a
  restarted agent racing a stale import; resolves on retry.
- **`pedrain_grants` frozen while idle is now CORRECT.** PE_DRAIN is armed only by in-flight DOCA tasks or a
  detached import awaiting reclaim. *Before S3.1b a frozen `pedrain_grants` was the bug, because the accept
  loop lived inside it.* **Do not read old notes with the new meaning.**
- **`spawn_grants`** and the `spawn{pend= done= hw= begun= ok= fail= rung= deferred=}` block are S3.3.
  **All zero is the correct idle state** — the spawn collector is armed by an *exact count*, not by a poll
  obligation. A nonzero `pend=` that never falls is a stuck phase machine; `deferred=` stuck above 0 is a peer
  that will hang in `WAIT_PEER_OPEN` forever.

See `../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md` §0/§0b/§0c.

#### 1.2b `HOMER_COLLECTOR_STARVE_DIAG` — WHICH collector was dropped, and by which gate

**Symptom it serves:** progress stops for one workload or one session while the service looks healthy and
idle — no `ALARM`, no `pool exhausted`, no `fatal error state`, no error on either side. Also the correct
tool whenever `PROGRESS POLICY-ADMISSION DROPS` shows `granted collectors=6 | DROPPED collectors=N`: that
line reports a **count, not an identity**, and this diagnostic supplies the identity.

Build it through the validation helper, which asserts the instrumentation actually landed:

```sh
dpu_build.sh "$RUN_ID" "<citus.tar>" --define=HOMER_COLLECTOR_STARVE_DIAG=1 \
    service-bin dpu-tcp-transport-smoke-bin
```

**Four drop reasons are distinguished, and the distinction is the whole point:**

| reason | meaning |
|---|---|
| `budget` | lost to a grant quota (`maxPlanGrants` / `maxCollectorGrants` / payload / blind) |
| `feedback-backoff` | suppressed after an empty streak |
| `no-source` | no entry in the collector→source map — stale scaffolding; consumes **no** budget |
| `already-planned` | benign double-consideration in one pass |

**The deliverable is the SERVICE-EXIT CENSUS, not the streamed lines.** The stream is rate-limited (first 8,
then powers of two) per `(kind, reason)`; the census is the complete total. Two tables print:

1. **collector drop totals** — `collector | reason | count`.
2. **backoff boundedness** — `collector | max_backoff_since_grant | grants`.

⚠⚠ **In table 2, read the `grants` column FIRST, and read `max=0 grants=0` as "THIS COLLECTOR NEVER RAN".**
That is a far worse state than a large `max`, and the two are indistinguishable without the `grants` column —
which is precisely why it is printed beside it.

⛔ **The census prints AS THE SERVICE EXITS. Stop the DPU service with `SIGTERM` and let it exit. A `kill -9`
destroys the entire deliverable and the run must be repeated.**

Current investigation using this instrument:
[`../implementations/citus/transport/dpu_collector_admission_starvation_mixed_workload.md`](../implementations/citus/transport/dpu_collector_admission_starvation_mixed_workload.md).

#### 1.2c `HOMER_CONTROL_MAILBOX_STARVE_DIAG` — is a peer-control TRAFFIC CLASS never planned?

**Symptom it serves:** a peer-control request is published and its response never observed — the owning async
op sits in `STREAM_WAIT_PEER_OPEN` forever — while **both** DPUs look healthy, the peer serviced the request
normally, and `[starve-diag]` shows nothing wrong. **This is the one starvation the collector census cannot
see:** control-mailbox actions are enumerated up to 8 and truncated to
`HOMER_CONTROL_MAILBOX_ACTIONS_PER_PASS` (**2**) *before* budget accounting, so the 6 discarded actions raise
no drop anywhere.

```sh
dpu_build.sh "$RUN_ID" "<citus.tar>" --define=HOMER_CONTROL_MAILBOX_STARVE_DIAG=1 \
    service-bin dpu-tcp-transport-smoke-bin
```

Emits one `[mailbox-diag]` census at service exit:

```
[mailbox-diag] ===== control-mailbox class admission (cap=2/pass over 4 classes) =====
[mailbox-diag]   traffic_class              ready_passes   starved_passes   starved%
[mailbox-diag]   CRITICAL_CONTROL              12409722                0       0.00%
[mailbox-diag]   FOREGROUND_PAYLOAD             8123441          8123441     100.00%   <-- STARVED: ...
```

⚠⚠ **READ `starved%`, NEVER `starved_passes` ALONE.** A six-figure raw count is meaningless without its
denominator — this project lost an afternoon to a 271,707-drop counter that sat beside 12,409,722 grants (2%,
i.e. healthy). **A ratio near 1.0 on a non-`CRITICAL` class is the defect; a small ratio is ordinary
contention.**

⛔ Same exit requirement as §1.2b: **`SIGTERM` and let it exit.** `kill -9` destroys the census.

⚠ **A clean census does not exonerate the fix.** The three-pass admission in
`TupleSinkServiceAppendReadyControlMailboxActionsRdma()` is *supposed* to keep `starved%` low; on a build that
already carries it, low numbers mean the fix is working, **not** that starvation was never possible. To
observe the original defect you would have to revert that function.

Contract and rationale:
[`../implementations/citus/transport/CONTRACTS.md`](../implementations/citus/transport/CONTRACTS.md)
(`TupleSinkServiceAppendReadyControlMailboxActionsRdma()`).

### 1.3 Reading a socketless backend's arena bind without the trace build

The positive `remote exec backend: bound frontend arena slot=<N>` line is **compiled out** at the default
`HOMER_REMOTE_EXEC_TRACE=0`. Validate the arena arm by **absence of the four FATALs** —

- `could not open Homer control region`
- `could not bind|attach frontend arena`
- `invalid selected-DPU startup identity`
- `invalid result queue descriptor`

— **plus** the DPU's `DPU backend spawn COMPLETED` **plus** a **live** `remote exec backend`. A live
selected-DPU backend cannot exist without a successful bind: both the attach and `BindArenaSlot` are
FATAL-on-failure *before* the command loop.

### 1.4 `PROGRESS POLICY-ADMISSION DROPS` → completion never publishes (observed ONCE, non-reproducing, July 18 2026)

Observed once during Stage-2 facade validation (a gate `-c4` measured repeat run against RESIDUAL state — no
clean-baseline restart after an orphaned prior attempt). Shape, so it stays recognisable:

- **Host symptom:** all clients `timed out after 30 s waiting for Homer completion of sql_execute (kind=6
  sequence=5)`, then `Homer session is terminal (backend exited)`, then `ALARM lifecycle CLOSE_SESSION blocked
  because semantic close still owns role 1 ... slot_state=2`, and `0/N` processed / `Run was aborted`.
- **DPU cause (farnet1 spawn DPU):** the service HAD `landed peer command` seq 1–5 and `finalized pending DPU
  result stream sequence=5`, started P3 result peer-open + byte-ring bind, then logged `PROGRESS
  POLICY-ADMISSION DROPS` and went quiet — it **never published the seq-5 completion** back to the host.
- **Reading it:** the host-side ALARM is the close-readiness gate firing CORRECTLY — an un-ACKed terminal START
  leaves role-1 `REQUEST_READY` (state 2), so `CLOSE_SESSION` refuses loudly. It is a downstream SYMPTOM of the
  DPU non-publication, NOT a host bug. The failure is DPU admission-scheduler side.
- **Non-reproducing:** a full clean-baseline restart (both DPU services restarted, backends reaped, project shm
  removed, PostgreSQL restarted) cleared it; the identical retry completed `8000/8000`. Consistent with the
  standing rule that a run against residual state is diagnostic-only — restart from a clean baseline. If this
  recurs on a CLEAN baseline it becomes a real DPU-side admission bug worth localizing; the one observation to
  date does not establish that.

---

## 2. Network / RDMA diagnostic battery

**Run these only** after a reboot or renumber, when a run actually fails, or before a fresh absolute
(e.g. line-rate) performance claim. In steady state the operator runbook is the source of truth — go straight to
the preflight and workload.

### 2.1 Addressing and link state

```sh
ip -br addr
ip route
ip rule
ip route get 10.10.1.100 from 10.10.1.101
ip route get 10.10.2.100 from 10.10.2.101
ssh farnet0 "ip rule; ip route get 10.10.1.101 from 10.10.1.100"
ssh farnet0 "ip rule; ip route get 10.10.2.101 from 10.10.2.100"
ethtool enp33s0f0np0 | egrep 'Speed:|Lanes:|Link detected:'
ethtool enp33s0f1np1 | egrep 'Speed:|Lanes:|Link detected:'
ibdev2netdev
show_gids
ping -c 1 -W 1 -I 10.10.1.101 10.10.1.100
ping -c 1 -W 1 -I 10.10.2.101 10.10.2.100
ping -c 1 -W 1 -I 10.10.1.101 10.10.2.100
ping -c 1 -W 1 -I 10.10.2.101 10.10.1.100
ssh farnet0 "ip -br addr; ip route; ip rule"
ssh dpu "ip -br addr show enp3s0f0s0; ip route"
ssh farnet0 "ssh dpu 'ip -br addr show enp3s0f0s0; ip route'"
ssh dpu "sudo -n ping -c 1 -W 1 -I enp3s0f0s0 10.10.1.200"
ssh farnet0 "ssh dpu 'sudo -n ping -c 1 -W 1 -I enp3s0f0s0 10.10.1.201'"
```

Both host fast-link ports and both DPU fast-link ports reported `400000Mb/s`, 4 lanes, full duplex, link
detected (June 2026). **Recheck link speed before a new performance claim.**

`mlx5_0` → `enp33s0f0np0`; `mlx5_1` → `enp33s0f1np1`; IPv4 RoCE v2 uses **GID index 3** on both hosts.

### 2.2 `ib_read_bw` same-lane sanity

```sh
# Lane 0: farnet1 10.10.1.101/mlx5_0 -> farnet0 10.10.1.100/mlx5_0
ssh farnet0 "ib_read_bw -d mlx5_0 -i 1 -x 3 -F --report_gbits -s 1048576 -D 5 -p 18550"
ib_read_bw -d mlx5_0 -i 1 -x 3 -F --report_gbits -s 1048576 -D 5 \
  -p 18550 --bind_source_ip 10.10.1.101 10.10.1.100

# Lane 1: farnet1 10.10.2.101/mlx5_1 -> farnet0 10.10.2.100/mlx5_1
ssh farnet0 "ib_read_bw -d mlx5_1 -i 1 -x 3 -F --report_gbits -s 1048576 -D 5 -p 18551"
ib_read_bw -d mlx5_1 -i 1 -x 3 -F --report_gbits -s 1048576 -D 5 \
  -p 18551 --bind_source_ip 10.10.2.101 10.10.2.100
```

### 2.3 The cross-lane failure (OPEN — switch-side, June 12, 2026)

**Status: unresolved. Not a Homer bug.** Cross-port `ib_read_bw` fails with
`transport retry counter exceeded`:

- farnet1 lane 0 → farnet0 lane 1: fails
- farnet1 lane 1 → farnet0 lane 0: fails
- retrying with **GID index 2** also failed, so this is **not** only a RoCE v2 GID-index issue
- with source-policy routes + ARP controls applied, the test fails **earlier**, at ARP level: `tcpdump` on
  farnet0 showed the ARP request from farnet1 **port0** for the peer **lane-1** IP **arriving on farnet0
  port0**, while farnet0 port1 saw no packet — and the reverse cross direction was symmetric on port1.

**Conclusion:** switch/VLAN/fabric forwarding keeps same-index lanes in **separate L2 domains**. Fix the
switch-side L2 domain before expecting cross-lane RDMA to pass. The current setup is **not** a verified
four-port full mesh.

```sh
ssh farnet0 "ib_read_bw -d mlx5_1 -i 1 -x 3 -F --report_gbits -s 1048576 -n 1000 -p 18556"
ib_read_bw -d mlx5_0 -i 1 -x 3 -F --report_gbits -s 1048576 -n 1000 \
  -p 18556 --bind_source_ip 10.10.1.101 10.10.2.100

ssh farnet0 "ib_read_bw -d mlx5_0 -i 1 -x 3 -F --report_gbits -s 1048576 -n 1000 -p 18557"
ib_read_bw -d mlx5_1 -i 1 -x 3 -F --report_gbits -s 1048576 -n 1000 \
  -p 18557 --bind_source_ip 10.10.2.101 10.10.1.100
```

**For raw reproduction, leave host routing state unmodified.** The source-specific routes and ARP controls
below are a *diagnostic isolation step* — they remove the host reply-route ambiguity, and they are **runtime
state**, not configuration:

```sh
# farnet1
sudo -n ip route replace 10.10.1.0/24 dev enp33s0f0np0 src 10.10.1.101 table 1100
sudo -n ip route replace 10.10.2.0/24 dev enp33s0f1np1 src 10.10.2.101 table 1101
sudo -n ip rule del priority 1100 2>/dev/null || true
sudo -n ip rule del priority 1101 2>/dev/null || true
sudo -n ip rule add priority 1100 from 10.10.1.101/32 table 1100
sudo -n ip rule add priority 1101 from 10.10.2.101/32 table 1101
for i in enp33s0f0np0 enp33s0f1np1; do
  sudo -n sysctl -w net.ipv4.conf.$i.arp_ignore=1
  sudo -n sysctl -w net.ipv4.conf.$i.arp_announce=2
  sudo -n sysctl -w net.ipv4.conf.$i.arp_filter=1
  sudo -n sysctl -w net.ipv4.conf.$i.rp_filter=0
done

# farnet0: identical, with 10.10.1.100 / 10.10.2.100
```

**Revert** with `ip rule del priority 1100`, `ip rule del priority 1101`, `ip route flush table 1100`,
`ip route flush table 1101` on both hosts to expose the raw failure again. If same-subnet multi-NIC testing
ever becomes the default, move the chosen policy into persistent host network configuration.

---

## 3. Measurement history

> Every number below is a **timestamped observation on a specific commit**, not a standing fact. Check
> `git merge-base --is-ancestor <sha> HEAD` before citing one.

### 3.1 RDMA link capability (June 12, 2026 address plan — **superseded**)

Measured on the *earlier* same-subnet shape (`10.10.1.102`/`10.10.1.103` on farnet1); that address plan is
**stale** for current validation.

| link | result |
|---|---|
| `mlx5_0`, farnet1 lane 0 → farnet0 lane 0 | single-QP read ≈ **62 Gbit/s** |
| `mlx5_1`, farnet1 lane 1 → farnet0 lane 1 | single-QP read ≈ **90 Gbit/s**; 8-QP read ≈ **259 Gbit/s** |
| cross-port, either direction | **FAILED** — `transport retry counter exceeded` (see §2.3) |

These prove both RDMA ports are usable. They are **not** a line-rate acceptance result: MTU was `1024`,
duration short, no CPU/NUMA tuning. Collect a separate tuned benchmark before making any 400G claim.

### 3.2 `pgbench --homer` remote RDMA (pre-P7)

- warmed farnet0→farnet1 **c1**: ≈ **4.2k TPS**, p99 ≈ **0.25 ms** — correct.
- real-RDMA **c4**: correctness holds, scaling is poor — ≈ **4.8k TPS**, p95 ≈ **4 ms**. Treat as a known
  shared RDMA command/completion transport bottleneck, **not** a final multi-client result.

### 3.3 P7 — the RNR-NAK bootstrap stall (citus `d33f6ded3`, postgres `0021633a44a`, July 12, 2026)

The accepting side posted its bootstrap RECV **after** `rdma_accept()`, so the initiator's SEND could land on
a QP with an empty receive queue. With `rnr_retry_count = 7` (**infinite** retry) the transfer never *failed* —
it was silently retried after the default `min_rnr_timer` (~655 ms). **The defect expressed itself only as
latency, never as an error**, and was strictly bimodal: 0 ms (won the race) or ~1.3–1.8 s.

Fix: post receives **before** `rdma_accept()`/`rdma_connect()` (the canonical librdmacm rule).

Measured on a **fully stripped** stack (no `cmtrace`, no `p3trace`, no `p2diag`, both DPUs, both hosts):

| | before | after |
|---|---|---|
| peer connection setup | ~1450 ms | **~10 ms** |
| pgbench latency avg (`-t 5`, setup-dominated) | 290–435 ms | **7.5–7.8 ms** |
| steady state (`-t 2000`) | — | **3.14 ms/tx, 318 tps** |

> ## ⛔ THE `318 tps` FIGURE IS **POST-P7 (citus `d33f6ded3`)**. IT IS NOT THE CURRENT BAND. DO NOT COMPARE AGAINST IT.
>
> **On 2026-07-13 a validation dutifully reported an "8% regression" against this number. There was no
> regression.** The figure predates the *entire* P0 resource-retirement series. Quoting it as *the* band is
> **exactly what Non-negotiable #8 forbids** — *"a dated baseline reads like a standing fact; it is a
> timestamped observation."* It was stamped, and it still misled, because it was the number the runbook
> **pointed at**.
>
> ### The `-t 2000` gate band, by SHA — ⚠ **AND IT IS A `-c 1` BAND**
>
> **🔴 EVERY ROW BELOW IS `-c 1 -j 1 -t 2000`.** The client count was NEVER recorded here, and on 2026-07-14 a
> validation ran the gate at **`-c 4`**, got **404–424 tps**, and reported it against this band — which reads as a
> **40% improvement** and is nothing of the kind. **pgbench reports AGGREGATE tps**, so `-c 4 → ~420` is
> **~105 tps/client** against ~300 at `-c 1`: a 3× per-client fall under 4-way concurrency, i.e. the known
> command/control **scaling** problem. Not a change, not a regression, not comparable.
>
> > **RULE. A performance band is only a band AT ITS CLIENT COUNT.** Record the client count WITH the band, or
> > the next comparison is a coin flip. *(This is the SECOND phantom this one table has produced: the first was
> > the stale `318` row. Non-negotiable #8 says stamp the SHA — that is necessary and it is not sufficient.)*
>
> | when | citus SHA | clients | tps | note |
> |---|---|---|---|---|
> | post-P7 | `d33f6ded3` | `-c 1` | **318** | **STALE.** Predates the entire P0 series. |
> | after the P0 series, pre-P-START | ~`e85abe531` | `-c 1` | 286.7 / 285.1 / **282.4** | ⚠ measured with the START-wait instrumentation ON |
> | after P-START | `91107bc11` | `-c 1` | 290.2 / 295.5 / 292.2 | |
> | after P1-f | `91482c840` | `-c 1` | 291.0 / 295.8 / 304.1 | stripped stack, no host service (S7.0) |
> | after P2-i | `2b37e8701` | `-c 1` | 285.6 / 291.0 / 299.1 | monotone within-set decline (299→291→286) ⇒ machine drift after an hour of 23 GB transfers, not a step change. |
> | after coalescing Stage 2a | `bdcb7eb70` | `-c 1` | 303.4 / 298.3 / 295.4 | `-t 2000`, logging off; frontier sweep, signalling still always-on |
> | after coalescing Stage 2b | `6b1d77553` | `-c 1` | 300.1 / 298.0 / 294.6 | `-t 2000`; per-QP shards + preflight, signalling still always-on |
> | **after coalescing Stage 2c (CURRENT)** | **`7e08343f2`** | **`-c 1`** | **290.6 / 295.9 / 293.9 / 295.2 / 299.4** | `-t 2000`, logging off, warmup discarded, **five** repeats each stamped with farnet0 1-min load 0.44–0.62. Selective signalling live (~3.1% of command units signalled). An earlier same-binary set (279.4/212.9/265.8) was measured under load-average **5.5–11.5** foreign contention and is recorded only as a contamination example — do not quote it. ⚠ Note for the ~10% open item below: 2c cuts send-CQE volume ~30× yet the `-c 1` band did NOT move up ⇒ evidence AGAINST the P0-i more-CQEs suspect at this client count. |
> | *(reference, NOT comparable)* | `91482c840` | `-c 4` | 404–424 **aggregate** | ≈105 tps/**client**. Kept here only so nobody re-derives the phantom. |
>
> Compare against the CURRENT row **at the same client count**, and add a row when you move it.
>
> ### 🔴 OPEN: an unattributed ~10% between `318` and `~285`, and NOBODY NOTICED IT
>
> The P0 safety series cost roughly **10%** and **no stage ever saw it**, because every stage compared itself to
> the stage *immediately before* it. The drop is only visible end-to-end. **Suspects (UNVERIFIED — none has been
> measured):**
> - **P0-i** (`67ebc0167`) deleted the opportunistic signal interval ⇒ **more signalled WRs ⇒ more CQEs**.
> - **§24** (`1d379b333`) added a WR-ID decode on **every** completion.
> - **§29(b)** (`e85abe531`) added a per-connection admission predicate on **every** post.
>
> **This is a real, open performance question — not a pass/fail criterion, and not being chased now** (perf is
> deferred until the migration lands). It is recorded so it is not "discovered" later as a mystery regression.
>
> > **The methodology rule this earns: a stage-by-stage comparison can hide an arbitrarily large cumulative
> > drift. Every stage was green against its predecessor, and the series lost 10%.** Re-anchor to the *milestone*
> > baseline periodically, not only to the last stage.

**The steady-state 3.14 ms/tx (≈450 µs/command) is still far from the microsecond target — that is a NEW and
separate performance question.**

⚠ **The "≈450 µs/command" here is ARITHMETIC and it is 2× WRONG** (3.14 ms ÷ ~7 statements). The **measured**
per-command control wait is **235.4 µs** — see
[`remove_synchronous_start_round_trip.md`](../future-directions/citus/transport/remove_synchronous_start_round_trip.md).

Two methodological notes worth keeping:

- **An external control beat instrumentation.** `rping` does a raw `rdma_cm` connect+ping+teardown between the
  **same two DPU ports** over the **same fabric** in **31 ms**, against our ~1500 ms. One command, no rebuild —
  and it eliminated the fabric, ARP, RoCE/GID config and the DPU kernel outright.
- **One probe line answered the whole question:** on each setup-phase transition, print **both**
  `calls_in_phase` *and* `ms_in_phase`. `calls=383991 / ms=1428` says the pump ran 270k times a second
  throughout — so it was **not** starved (refuting the leading hypothesis), and the 1.4 s sat entirely inside
  `BOOTSTRAP_WAIT`. **Elapsed time alone could not have told those apart.**

### 3.4 Backend-to-backend COPY, 10M rows (June 6, 2026 — **STALE, and the workload is BROKEN at HEAD**)

> ⚠ **These numbers predate citus `7a2eaed53` and DO NOT reproduce today.** The workload hangs at HEAD; see
> `../implementations/citus/transport/byte_ring_slot_capacity_regression.md` "Problem 2". They were once cited
> as evidence *against* a correct diagnosis — do not repeat that.

- Input: `/tmp/homer_tuple_sink_copy_10m.csv`, 10,000,000 rows, ≈ 323 MB.
- Correctness anchors (still the anchors): `count=10000000`, `min=1`, `max=10000000`, `sum=500000050000000`.
- Default Homer tuple-sink geometry: `8192` tuples, `524288` bytes.

| run group | artifact dir | times (s) |
|---|---|---|
| Homer, clean recheck | `/tmp/homer_tuple_copy_investigate_baseline_1780796369` | 5.39, 5.04, 5.20, 5.95 |
| Homer, recheck 2 | `/tmp/homer_tuple_copy_investigate_recheck2_1780796468` | 5.13, 5.23, 5.07, 5.17 |
| **Vanilla Citus**, same runtime state | `/tmp/citus_vanilla_copy_recheck_1780796433` | 5.20, 5.22, 5.27, 6.32 |
| Homer, older post-peer-push | `/tmp/homer_b2b_copy_10m_default_after_batch_default_1780784020` | 7.46, 7.62, 7.32 |
| W4 scheduler-facts | `/tmp/homer_w4_copy_10m_trim3_1780785961` | 7.71, 7.98, 8.05 |
| W4 parent-commit A/B (`7be63cdb8`) | `/tmp/homer_parent_copy_10m_ab_1780786118` | 8.01, 8.30, 7.99 |

**Interpretation at the time:** Homer was at **parity** with vanilla Citus. The service logs confirmed this was
the real byte-ring path (`published_tail=512005120`), **not** a silent fallback to libpq COPY.

**Parity is a no-regression checkpoint, NOT the target.** The tuple-view path *should* beat vanilla here — it
avoids full libpq COPY serialization/deserialization and uses a lighter Homer-owned payload format. If a future
run is only at parity, investigate tuple-view materialization, byte-ring transport progress, worker insert
cost, and service-progress scheduling before accepting that as the ceiling.

The W4 slow band (7.7–8.1 s) did **not** reproduce in the clean paired recheck and was never a confirmed
regression.

---

## 4. Broken command shapes (kept so they stay recognisable)

These are in the KB, **not** the runbook, precisely so nobody copy-pastes them. Each looks plausible.

### 4.1 Basebackup to the farnet0 **host** service — hangs above ~8 MiB

```sh
# ✗ BROKEN. host=10.10.1.100 is the farnet0 HOST service.
-t 'homer:mode=rdma,host=10.10.1.100,port=9717,node=2'
```

⚠ **Read the direction of causation carefully — it is the opposite of what it looks like.** This is **not** a
stale path that the DPU work has yet to reach. It is a path that the DPU work **broke**, and the *host
service* is the leftover.

`pg_basebackup` is now **mandatorily a selected-DPU producer** (`HomerClientOpenBaseBackupStreamSelectedDpu`,
unconditional, from `bbsink_homer_begin_backup`). So the producer **always** speaks the DPU-relay wrap
protocol now, no matter who the receiver is. On wrap it writes a pre-wrap transport header whose advertised
record length **deliberately exceeds** the trailer — that overlong length *is* the wrap signal
(`homer_client.c:4576-4583`).

Two receivers, opposite readings of the **same bytes**:

| receiver | rule | verdict |
|---|---|---|
| **DPU relay** | `recordBytes > trailerBytes` ⇒ wrap proven, re-parse at offset 0 | ✅ |
| **host service** `HomerServicePumpIncomingByteRingPayload` (`tuple_sink_service_process.c:32357`) | `recordBytes > contiguousBytes` ⇒ **fatal** | ❌ |

The host service only takes its skip-trailer path when `!headerReady` — and the pre-wrap header makes
`headerReady` **true**, so it falls into the fatal arm instead. It aborts at the first wrap with
`byte-ring record unexpectedly crossed ring boundary session=… sequence=128`, emits a peer reset and
`CM event=DISCONNECTED`, and the client then **hangs with no error** (`rc=124` under `timeout`; needs a manual
kill).

**The bitter part:** a **shared** drain core, `HomerByteRingSinkDrain` (`homer_byte_ring_sink.h:217-224`),
already implements the *correct* geometric rule —
`gap iff contiguousBytes < maxRecordBytes && consumedHead + contiguousBytes <= producedTail` — and
**neither receiver uses it.** Both rolled their own.

Confirmed July 9, 2026 by A/B on both the current and the reverted tree, so it is **not** from the byte-ring
pool refactor; it dates to whenever basebackup became mandatorily selected-DPU. "Problem 3" in
`../implementations/citus/transport/byte_ring_slot_capacity_regression.md`, where the fix shape is recorded
(teach the host service the shared rule) but **not yet implemented**.

**Use the 4-role DPU-relay topology** (`host=10.10.1.200`, the farnet0 **DPU**) in the operator runbook.

### 4.2 `bytes=8388608` — cannot work

Since citus `7a2eaed53` (July 6, 2026) the byte ring is a **fixed 8 MiB** and a 2× headroom guard rejects any
record footprint above **half** of it. `bytes=8388608` yields a footprint of `8388848` (payload + 240, where
240 = 56-byte transport header + 184-byte max basebackup header) and fails with
`basebackup record footprint 8388848 too large for byte ring storage 8388608`.

**Use `bytes=524288`** (the validated value) or omit `bytes=` for the 1 MiB default.

### 4.3 `publish=N` — accepted, but inert

basebackup uses a producer-owned byte ring and publishes each committed record immediately, to preserve
pipeline overlap. The `publish=N` target-detail knob is retained for command-line compatibility but **no
longer controls producer batching** on the byte-ring basebackup path.

### 4.4 `--homer-dpu` — never completed end to end

`--homer-dpu` layers on `--homer` and moves **only RESULT-tuple delivery** onto the DPU two-ring deform relay
(the command/completion path is unchanged). It requires `--homer`, a remote peer, and `-M simple`. As of
July 9, 2026 it has **never** completed end to end; see
`../implementations/citus/transport/byte_ring_slot_capacity_regression.md`.

**Do NOT confuse it** with the backend **COMMAND** channel through the DPU (the native selected-DPU command gate,
`pgbench --homer --homer-dpu-command`) — a different axis entirely. (The old `citus_remote_exec_pgbench_transaction`
UDF + `citus.enable_experimental_homer_dpu_frontend` GUC that used to drive that command channel were retired in the
S7 host-service retirement.)

---

## Related

- `farnet_operational_hazards.md`: the traps — checks whose failure mode is silent.
- `../../../AGENTS.md`: concise policy, triggers, and workload-selection rules.
- `farnet_operator_runbook.md`: canonical machine setup and operator workflow.
- `../future-directions/citus/transport/dpu_scheduler_arm_execute_mismatch.md`: the scheduler arm/execute
  mismatch that `starve-diag` instruments.
- `../implementations/citus/transport/byte_ring_slot_capacity_regression.md`: the open regressions behind the
  broken command shapes in §4.
