# Farnet operational hazards: checks that lie

## Purpose

This is the **evidence archive behind the rules in the repo `CLAUDE.md`/`AGENTS.md`.** Every entry here is a
check that was *exactly right for the case its author thought about, and silently wrong for the one they did
not*. Each cost at least one debugging cycle, and several cost days.

`CLAUDE.md` carries the one-line imperative. This doc carries **why**, so the next reader does not
"simplify" a rule back into the bug. A rule without its incident is a rule that gets undone.

> **THE UNIFYING PATTERN.** Every hazard below is an *identity check* — "is this the right process / the
> right binary / the right shared-memory object / the right database / the right build?" — whose negative
> answer is indistinguishable from "I could not tell." **Prefer checks whose failure mode is LOUD.** A check
> that reports "clean" when it could not look is strictly worse than no check, because it manufactures
> confidence.

## Status

Living. Add to it whenever a check lies to you. Stamp entries with a **date and a commit SHA** — a dated
fact reads like a standing truth, but it is a timestamped observation.

---

## 1. Process identity

### 1.1 `pkill -f <substring>` matches its own command line

**Applies to:** every process in this project, on hosts and DPUs.

From an `ssh` one-liner or a `bash -c` wrapper, the pattern you pass to `pkill -f` **sits in the calling
shell's own argv**. So `pkill -f` matches its own caller. This has failed in *both* directions:

- It **silently killed the ssh session** (the ssh command line contained the pattern).
- It **silently did NOT kill the target**, leaving a stale service that then failed the next run with
  `Address already in use` — which reads exactly like a bind bug and is not one.

`pgrep -f` inside a `$(...)` has the identical defect: the pattern is in the calling shell's argv, so the
pgrep matches its own parent and the subsequent kill takes the caller down. **Writing the loop into a
HEREDOC does not save you** — the heredoc body is part of the `bash -c` command string, so the pattern is in
the caller's argv anyway. (Both happened July 10, 2026; each presented as a silent no-output failure.)

**Do:** create the script with a **file-write tool** and invoke it by path.

### 1.2 `pkill -x <name>` does not rescue you

`-x` matches `/proc/<pid>/comm`, which the kernel truncates to **15 characters**. `citus_tuple_sink_service`
is `citus_tuple_sin` there, so `pkill -x citus_tuple_sink_service` matches nothing **and exits successfully**.
Verify with `cat /proc/<pid>/comm`.

### 1.3 The `ps | grep` preflight lies in the *other* direction

`ps -eo args | grep -E '...'` matches the calling `bash -c` **and its own grep**, so a clean machine reports
three "leftover" processes. Bracket tricks (`[c]itus_...`) do not help — the caller's argv contains the
brackets too.

### 1.4 What actually works: `/proc/<pid>/exe`

No shell can fake it:

```sh
for d in /proc/[0-9]*; do
  exe=$(readlink -f "$d/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$d/exe" 2>/dev/null)
  case "$exe" in */pg-citus/bin/citus_tuple_sink_service*) sudo -n kill -9 "${d#/proc/}";; esac
done
```

Three parts of that are **not optional**:

- **The trailing `*` in the pattern.** After a rebuild, the old process's `/proc/<pid>/exe` reads
  `".../citus_tuple_sink_service (deleted)"`, so an exact-suffix `case` stops matching **exactly the process
  you just replaced**. On July 10, 2026 this left a stale DPU service holding port 9727; the new instance
  died on `bind()` — but only *after* its `>` redirect had **truncated the log**, so the old process kept
  appending at its old file offset and the log filled with NULs (`grep: binary file matches`; `grep -a` was
  needed to read it at all). The service looked freshly started and was seven minutes old.
  **Tell:** internal counters that should start at 1 read `handle=2 slot=17 session=2`.
- **The `sudo -n readlink` fallback.** `readlink /proc/<pid>/exe` on another user's process returns `EACCES`,
  and everything here runs as `dbcomm` while your interactive shell probably is not. Without the fallback the
  preflight prints "clean" **while the entire stack is up** — a false negative, strictly worse than the
  `ps | grep` false positive it was written to replace. **Count the pids you could not identify and say so**,
  rather than letting an unreadable `/proc` masquerade as an empty one.
- **`sudo -n` on the kill itself.** A plain `kill`/`pkill` against a `dbcomm`-owned process reports
  `Operation not permitted` and leaves it alive.

### 1.5 Never `kill -9` a LIVE `postgres: remote exec backend` mid-run

It triggers a **full postmaster crash-restart**. Reap socketless backends only at end-of-run, after stopping
PostgreSQL.

This matters more since **S3.3b+S3.2c** (citus `30dfc9e9a`): the DPU-spawned backend no longer dies at the
control region — it binds a frontend-arena slot and **survives, idling in its command loop**. It does **not**
respond to `pg_ctl stop -m fast`/`SIGTERM`, because socketless backends have no clean-shutdown path yet
(S4's `CLIENT_SQL_SESSION_CLOSE` / S3.4's doorbell-EOF teardown will add one). So after any
`--homer-dpu-command` run the clean baseline **must** reap it by `/proc/<pid>/exe` + `kill -9`; a plain
`pg_ctl stop` leaves it alive and the *next* preflight trips on a stale backend. Pre-S3.2c the backend died
instantly, so it never survived to need reaping — do not read older notes with the new meaning.

---

## 2. Which PostgreSQL am I talking to? (**farnet1 has two**)

**Discovered July 12, 2026. This one had been silently wrong in `CLAUDE.md` for its whole life.**

farnet1 runs **two** PostgreSQL instances under the **same `dbcomm` UNIX user**:

| instance | prefix | data dir | owner |
|---|---|---|---|
| **FOREIGN — never touch** | `/home/dbcomm/pg` | `/home/dbcomm/pg/data` | someone else's experiment |
| **OURS** | `/data/dbcomm/pg-citus` | `/data/dbcomm/pg-citus/data` | this project |

**Both have `port` commented out in `postgresql.conf`** (default `5432`) and both default to
`unix_socket_directories = '/tmp'`. They therefore **race for `/tmp/.s.PGSQL.5432`**, and whichever starts
first wins it. Our July 10 start won it and logged `listening on Unix socket "/tmp/.s.PGSQL.5432"`.

Why every ownership check passes: the socket is `dbcomm`-owned, the postmaster is `dbcomm`-owned, `ps` shows
`postgres`. The only field that distinguishes them is the **`-D` data dir** in the postmaster's argv, or the
prefix in `/proc/<pid>/exe`.

**Consequence if you use the old `-p 5432` runbook while the foreign instance holds the socket:**

- `pgbench -i -s 1 postgres` **drops and recreates `pgbench_accounts`/`_branches`/`_tellers`/`_history`
  inside their database.**
- `DBOID` / `USEROID` come back from **their** catalog, so every subsequent Homer run is parameterised with
  OIDs that do not name our database.
- Nothing errors. Nothing warns.

`pg_ctl` is *not* affected — it names `-D /data/dbcomm/pg-citus/data` explicitly, and starting ours while the
foreign one holds 5432 fails **loudly** (`Is another postmaster already running on port 5432?`). The danger is
entirely on the **client** side.

**The rule:** ours runs on **5433**. Start it with `-o "-p 5433"`; give every `psql`/`pgbench`/`pg_basebackup`
`-p 5433`. Confirm before a run:

```sh
sudo -n readlink -f /proc/$(sudo -n head -1 /data/dbcomm/pg-citus/data/postmaster.pid)/exe
# want: /data/dbcomm/pg-citus/bin/postgres
```

A permanent fix (uncommenting `port = 5433` in **our** `postgresql.conf`) is available and has **not** been
applied — it changes runtime state on a shared machine and needs an explicit decision.

---

## 3. Build-artifact identity

### 3.1 A plain `make` does NOT undo a `make -B` stats build

`-B` leaves **every** object newer than its source, so the next `make service-bin CPPFLAGS='-D_GNU_SOURCE'`
finds nothing to do and **relinks the stats-instrumented objects**. `touch`ing the files you edited is not
enough either — it misses the TUs you did not touch (e.g. `remote_execution_peer_transport_rdma.o` keeps
`-DHOMER_SERVICE_PEER_TRANSPORT_STATS=1`).

**Always exit a diagnostic build with another `make -B`, then prove it:**

```sh
strings build/homer/citus_tuple_sink_service | grep -c 'starve-diag\|progress_machine_baseline_stats'   # want 0
```

> An artifact that is newer than its input is **not** evidence that it was built from that input *with the
> flags you now want*.

### 3.2 `rsync -a` preserves mtime, so `make` skips the file you just edited

The same trap in a different costume, on the DPU trees. After rsyncing sources to a DPU, `touch` them (or use
`make -B`) before building — otherwise the copied file's preserved mtime is older than the existing object and
`make` declares it up to date.

### 3.3 `strings <binary> | grep MACRO_NAME` proves nothing

A macro name is compiled out of **both** variants, so the grep returns nothing whether or not the flag was
set — and "no output" reads like a pass in both directions. The same applies to
`strings <binary> | grep CITUS_TUPLE_SINK_PROTOCOL_VERSION`.

**Do:** grep for a **string literal that only the enabled build emits** (e.g. `starve-diag`), or — to prove a
deploy landed on a DPU — for a literal you know is in the new code (`DPU setup-listener action selected`).
**Verify a protocol/ABI deployment through the installed HEADER, not through a binary.**

### 3.4 Install order is load-bearing (since command-plane S2)

`citus.so` is built `-fvisibility=hidden` and leaves `HomerDpuFrontend*` **undefined**; those symbols resolve
at `dlopen` time out of the **`postgres` executable**, which statically links `libhomer_client.a` and is linked
`--export-dynamic`. Installing a new `citus.so` against an old `postgres` fails **every** backend with
`undefined symbol: HomerDpuFrontendOpenDevice`. See `CLAUDE.md` for the ordered recipe; check with
`nm -D /data/dbcomm/pg-citus/bin/postgres | grep HomerDpuFrontend`.

### 3.5 Stale installed binaries silently map an old shared-memory name

After changing shared Homer protocol headers or client/service control-region names, a stale `pgbench` or
`libhomer_client.a` maps the **old** control shared-memory name while the service uses the new one. Verify:

```sh
strings /data/dbcomm/pg-citus/bin/pgbench                                | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/bin/citus_tuple_sink_service               | grep citus_remote_execution_control
strings /data/dbcomm/pg-citus/lib/x86_64-linux-gnu/libhomer_client.a     | grep citus_remote_execution_control
```

---

## 4. Shared-memory object identity: the version is in the NAME

`/dev/shm/citus_homer_frontend_arena_v3` — **the version is part of the object name**, so a
clean-baseline step that names the wrong version **silently cleans nothing**.

`CLAUDE.md` said `_v2` until July 12, 2026 while the code had already moved to `_v3`
(`homer_frontend_agent.h:97`). The documented hard-clean procedure was therefore deleting an object that no
longer existed and **leaving the real arena in place** — a cleanup step that reported success and did nothing.

**Do:** delete `citus_homer_frontend_arena_*` **by glob**, and confirm the current version from the header
rather than from any doc:

```sh
grep HOMER_FRONTEND_ARENA_SHM_NAME \
  /data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_agent.h
```

The arena is created by the postmaster **only** when `citus.enable_homer_dpu_frontend_agent=on`. It is
neither `shm_unlink`ed at shutdown nor re-zeroed on a postmaster crash-restart, so a stale one can leave arena
slots marked `BOUND`. That fails **loudly** at the next claim (`arena slot N is already bound`), never
silently — but remove it as part of a hard clean baseline anyway. The arena ABI bump is **host-only** and
needs no DPU redeploy. (`_v1` was 59,767,936 B; `_v2` was 59,774,080 B ≈ 57 MiB. Both orphaned.)

---

## 5. Compile-time coverage that isn't

### 5.1 `-Wswitch` protects nothing in `tuple_sink_service_process.c`

Nearly every switch over `HomerProgressSourceKind` / `...CollectorKind` / `...ActionKind` has a `default:`
arm, so **adding an enum member compiles clean while silently falling into it**. When you add one, enumerate
the switches by hand:

```sh
grep -n 'switch ((HomerProgressSourceKind)\|switch (sourceKind)' tuple_sink_service_process.c
# and repeat for the other two enums
```

S3.1b found a live instance this way: `HomerServiceDefaultCpuClassForProgressSource` is the **only** thing
that sets `sourceCore->cpuClass`, and its default arm is `HOMER_PROGRESS_CPU_CLASS_INVALID`.

### 5.2 A new collector needs FOUR edits, and the fourth is not a switch

Beyond the enums, the collector→source map, and the collector→action map, you must **NAME it in
`HomerMachineBaselineCompileExecutionPlan`'s phase body** — a **hand-enumerated** sequence of
`HomerServiceMachineBaselineFindCollector` + `...AppendCollectorAction` calls.

A collector that `HomerServiceAppend*CollectorCandidate()` **arms** but that body never **names** is armed on
*every* pass and granted on *none*. S3.3 shipped exactly that: the DPU spawn began, the peer response
deferred, and the service then sat silent forever — **not even its own 60 s timeout fired, because the timeout
lived inside the action that never ran.** An idle service and a wedged one look identical.

```sh
grep -n 'FindCollector(candidateSet, HOMER_PROGRESS_COLLECTOR_' tuple_sink_service_process.c
```

### 5.3 `HOMER_SERVICE_ENABLE_DPU_DMA=1` requires the machine-baseline progress policy

No source-plan policy admits *any* DPU collector — `HomerServiceBuildCoarseProgressReadySet` never appends a
DPU source — so under any other policy the DPU service **starts, binds 9727, and never accepts**. A startup
tripwire now `exit(1)`s; it cannot fire today because `ProgressPolicy` is a compile-time static with no
runtime selector.

---

## 6. Agent and tooling hazards

### 6.1 Never run a write-capable subagent concurrently with your own edits in the same tree

On July 10, 2026 a worker was told "do NOT touch `tuple_sink_service_process.c`", saw the file dirty, and
**restored** it — silently discarding ~30 hand-applied edits.

**The tell:** `git status` reported the file **unmodified** while its mtime was seconds old. Content matched
HEAD, so it was a *checkout*, not a write.

**Do:** give the worker `isolation: "worktree"`, or hold your own edits until it reports.

### 6.2 Apply multi-site mechanical edits from a script FILE with per-hunk `assert count == 1`

That is what made recovering those 30 edits a one-minute replay instead of an afternoon. (And a heredoc inside
`bash -c` leaks its body into the caller's argv — see §1.1.)

### 6.3 Never inline a double-hop ssh command string

`ssh farnet0 ssh dpu "cd ~/..."` — **farnet0's shell expands `~` to farnet0's home**, not the DPU's. Ship a
script and invoke it by path.

### 6.4 Backticks inside `git commit -m "..."` trigger command substitution

Use `-F <file>` or a quoted heredoc.

### 6.5 A subagent's claims about ownership, permissions, or mount state are unverified

Inside the Codex bubblewrap sandbox, files owned by another user render as `nobody:nogroup` and every `.git` is
read-only, so Codex will confidently report permission bugs and stale artifacts that **do not exist on the
host**. Confirm from the main agent (`stat`, `findmnt`, a real write probe) before acting. Claims about file
*contents* and code *structure* are unaffected.

---

## 7. Smoke-test hazards (`homer_dpu_tcp_transport_smoke`)

Validated July 9, 2026, citus `b2c3471b6`. Each of these cost a cycle.

- **Leg flags gate BOTH ends.** The server checks `options->expectLoop2BackPressure` too. Passing
  `--expect-loop2-backpressure` only to the client makes the client **hang** waiting for a leg the server never
  runs. Pass every leg flag to **both** processes (the server ignores the ones it does not use).
- **The client exits 0 on legs it wasn't told to expect.** A *server*-side failure surfaces only as
  `client TCP close exchange failed`. **Always read the server log; the exit code is not the verdict.** Both
  ends print `homer_dpu_tcp_transport_smoke: ok` on a real pass.
- **Give the server a generous `--timeout-ms`.** It counts from **process start**, not from accept, so a slow
  client launch eats the window and you get a bare `could not connect`. **Launch the server and client from the
  SAME shell invocation.** If the client runs in a later tool call, minutes elapse and the server has already
  exited with `server failed setup_done=0` — which reads exactly like a bind failure and is not one.
- **`setsid` alone does not survive `ssh` exit.** Use `nohup setsid … </dev/null &`, invoked from a small
  script on the DPU. Without `nohup` the server dies when the ssh channel closes, and the only symptom is the
  client's `could not connect`.

This smoke is the **only single-node host↔DPU DMA regression net**, and it silently rotted through the
byte-ring pool migration — it compiled; nobody ran it.

---

## 8. Validation-claim hazards

### 8.1 "Accepted" is not "still works afterwards"

The frontend agent puts a **permanent 49-ring mmap import** into the local DPU service — a state no
default-off run ever reaches. That state **permanently wedged the DPU's setup listener for four days without a
single log line** (July 10, 2026). The reason nobody saw it: *"the arena import was accepted"* had been
recorded as if it meant *"the service still works afterwards."*

> **An import being accepted proves nothing about the next connection.**

The liveness signal is not the import line. It is `ss -ltn | grep 9727` still showing `Recv-Q 0` **after** a
workload has run. Cheapest end-to-end check: with the agent on, run the 4-role basebackup — its sender opens a
*second* DPU setup connection, so it fails immediately if the listener is starved.

### 8.2 Stamp every baseline with a commit SHA, not just a date

A dated baseline **reads like a standing fact**; it is a timestamped observation. The June-6 COPY baseline was
cited as evidence *against* a correct diagnosis. With a SHA,
`git merge-base --is-ancestor <sha> HEAD` answers "does this still hold?".

### 8.3 A contaminated run is not a slow run — discard it

If a candidate times out, is interrupted, or needs manual process cleanup, it is **diagnostic-only**. Do not
use it for an acceptance/rejection claim. Return to the clean baseline, rerun the preflight, and collect fresh
warmed measurements.

### 8.4 "No error" is not "the intended path ran"

Confirm from the service logs that the Homer path actually executed, rather than a silent libpq/built-in
fallback. For COPY, `published_tail` advancing in the service log is the proof; for `pgbench --homer-dpu`,
the `--debug` `homer_last_abalance` line is the end-to-end decode proof (note `-d` is `--dbname`, **not**
`--debug`).

---

## Related

- `../../../CLAUDE.md` (→ `AGENTS.md`): the machine setup, the runbook, and the one-line form of every rule
  here.
- `farnet_diagnostics_and_baselines.md`: the diagnostic batteries (RDMA link checks, `starve-diag`, stats
  builds) and the historical measurement records these rules were learned from.
- `../implementations/citus/transport/`: the transport implementation checkpoints these hazards surfaced during.
