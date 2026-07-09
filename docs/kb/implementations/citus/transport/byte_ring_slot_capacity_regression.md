# Byte-ring `slotCapacityBytes` has two meanings — and b2b COPY is broken at HEAD

**Status (July 9, 2026):** two distinct, independently-confirmed problems, found while bringing up
`pgbench --homer-dpu`. Neither is fixed. They are recorded together because investigating one
surfaced the other, but **they are not the same bug and probably do not share a cause.**

Related: [dpu_byte_ring_pool_per_session_plan.md](dpu_byte_ring_pool_per_session_plan.md) (Failure B),
[dpu_payload_byte_ring.md](dpu_payload_byte_ring.md) (the ring + wrap-gap protocol),
[cross_node_dpu_migration_checkpoint.md](cross_node_dpu_migration_checkpoint.md).

---

## Problem 1 — `slotCapacityBytes` now names two different quantities (CONFIRMED, reproducible)

### Symptom
Any byte-ring-backed **tuple-view** stream aborts on its first published batch:

```
tuple-sink service: byte-ring sender object validation failed session=2 sink=1 sequence=1
  detail=tuple-view batch header mismatch sequence=1 batch_protocol=11 batch_sequence=1
  batch_header_bytes=40 batch_payload_bytes=16 batch_slot_capacity=32712
  publish_bytes=56 expected_slot_capacity=1024
```
Reproduced on the intended split topology with `pgbench --homer --homer-dpu -c 1 -j 1 -t 200`
(farnet0 → farnet1), on the first `SELECT abalance`. Error site
`tuple_sink_service_process.c:27574` (`HomerServiceValidateOutgoingPayloadObject`).

### Mechanism
`HomerServiceValidateOutgoingPayloadObject` (`:27568`) requires

```
batchHeader->slotCapacityBytes == streamEntry->stream.slotCapacityBytes
```

but the two sides now compute *different things*:

- **`stream.slotCapacityBytes`** is the **negotiated semantic per-record capacity**, stored at
  `tuple_sink_service_process.c:23513`. SQL result sink: `REMOTE_EXEC_SQL_RESULT_SINK_SLOT_CAPACITY_BYTES
  = 1024` (`remote_execution_backend_bridge.c:77`, slotCount `256` at `:69`).
- **`batchHeader->slotCapacityBytes`** is stamped by the producer from `sinkHandle->slotCapacityBytes`
  (`homer_tuple_queue_frontend.c:1787`, set at `:959`), which is
  `queueDescriptor->slotCapacityBytes - slotReservedPrefixBytes`, and the descriptor's value is
  **synthesised** at `tuple_sink_service_process.c:33539`:
  `queueDescriptor->slotCapacityBytes = queueMapping->byteRingBytes / streamEntry->stream.slotCount`.

Arithmetic for the SQL result sink: `8388608 / 256 = 32768`, minus
`sizeof(CitusTupleSinkTransportHeader) = 56` ⇒ **32712**. Exactly as observed.

### Why the `:33539` division is DELIBERATE and must not simply be deleted
This was the trap. The division looks like leftover arithmetic from before `7a2eaed53` (2026-07-06),
which changed `TupleSinkServicePayloadRingBytes` (`:6246`) to return the constant
`HOMER_PAYLOAD_BYTE_RING_STORAGE_BYTES` (8 MiB) rather than `slotCount * payloadSlotBytes` — its own
comment says *"slotCount (the count) no longer sizes the ring; slotCount is retained only in the
signature for call-site stability and is intentionally unused here."*

But the **frontend's attach check depends on the division**:
`homer_tuple_queue_frontend.c:1169` rejects the attach unless
`sharedState->byteRingBytes == slotCount * queueDescriptor->slotCapacityBytes`
(and `:445` sizes a frontend-created ring the same way). So `:33539` divides the now-constant ring
size by `slotCount` precisely to keep that invariant true. Removing the division breaks the attach.

**Therefore the descriptor value is correct as a *ring-geometry* quantity, and the bug is that this
synthetic value flows into the batch header where it is compared, with `==`, against a *semantic*
per-record capacity.** Two different quantities wearing the same field name.

### There are no slots left anywhere — `slotCount` is vestigial
`HomerServicePayloadStreamUsesByteRing` (`:6309`) is true for `TUPLE_VIEW_BATCH` **and**
`BASE_BACKUP_STREAM`. The only other family is `WAL_STREAM`, which is unimplemented (`-X none`).
**Every live payload stream is a byte ring; no slot-queue payload path remains.** What the two names
mean today:

- **`slotCapacityBytes` = max single-record footprint.** Genuinely load-bearing: the 2x-headroom fit
  guard (`:6291`) rejects a record larger than half the ring "instead of deadlocking the producer at
  the first wrap." Real meaning, wrong name.
- **`slotCount` = vestigial.** It sizes nothing since `7a2eaed53`. Its only remaining jobs: echo and
  validate the requested value (`:25700`, `:34279`, `:35636`); be the divisor at `:33585`; and let the
  frontend *reconstruct* the ring size by multiplying it back (`frontend:1169`, mapping size `:1092`).

The ABI header already anticipates the cleanup: `homer_queue_abi.h:120-125` — *"Historical name kept
until the Stage 4 descriptor split."*

### Fix — the descriptor split (chosen July 9, 2026)
The whole defect is ONE number round-tripped through a multiply/divide pair: the service knows
`byteRingBytes` (8 MiB), divides by a vestigial `slotCount` to make `slotCapacityBytes`, and the
frontend multiplies them back to verify it attached to the right ring. That divisor game is the only
reason `slotCount` still exists, and it is what let a synthetic *geometry* number leak into the batch
header where a *semantic* capacity was expected.

1. Descriptor carries `byteRingBytes` (uint64) explicitly.
2. `slotCapacityBytes` → **`maxRecordBytes`**: the negotiated per-record footprint, carried through
   unchanged, never derived.
3. Frontend attach compares `sharedState->byteRingBytes == descriptor->byteRingBytes` — no
   reconstruction (`frontend:1169`, `:1092`).
4. Producer stamps `maxRecordBytes` into the batch header (`frontend:1787`); the validator (`:27568`)
   compares it to `stream.maxRecordBytes`. They agree BY CONSTRUCTION — the class of bug cannot recur.
5. `slotCount` leaves the byte-ring descriptor. `slots=` in basebackup target details becomes
   ignored/deprecated (it already does nothing).

ABI bump; no back-compat (prototyping rules). Validation gate: `--homer-dpu -c 1` must get past the
abort. **COPY cannot validate anything until Problem 2 is fixed.**

**Footprint subtlety (the trap for whoever touches this next).** `maxRecordBytes` INCLUDES
`slotReservedPrefixBytes`: the service sets it to `sizeof(CitusTupleSinkTransportHeader) + negotiated`
(`TupleSinkServiceLocalSendQueueSlotBytes`), the frontend subtracts the prefix back out, and the batch
header carries exactly the negotiated value the validator compares. Setting it to the bare negotiated
capacity silently reintroduces the abort, one transport header short.

### ATTEMPT 1 FAILED AND WAS REVERTED (July 9, 2026)

Implemented as citus `e379d2dbe` + postgres `36cd6ee2da5`; **both reverted** (`aadcab871`,
`495f94db95f`) after validation. Builds and installs were rolled back to the pre-split ABI.

**What worked:** the original abort (`tuple-view batch header mismatch ... 32712 vs 1024`) did NOT
reproduce anywhere. The split does fix Problem 1.

**What broke: basebackup**, our only validated workload —
`byte-ring record unexpectedly crossed ring boundary session=1 sink=1 sequence=128
record_bytes=1048688 contiguous=456286` (receiver service log).

**Root cause — the divisor had a THIRD job.** We knew `byteRingBytes / slotCount` (a) satisfied the
frontend attach check and (b) was the source of the double meaning. What we missed:
**it tiles the ring by construction.** `slotCount * (byteRingBytes / slotCount) == byteRingBytes`, so a
record can never run off the end. The split replaced it with
`HomerServiceBaseBackupProducerRecordBytes(stream.slotCapacityBytes)` = `56 + negotiated` — a number
with NO arithmetic relationship to the ring size. Records stopped tiling and one walked off the
boundary.

**And it bit basebackup specifically because of the SAME disease, a third time.**
`stream.slotCapacityBytes` means the PAYLOAD on the SQL path, but on the basebackup path it is sized
against the RING (via the `bytes=` target detail). Adding a prefix on top therefore double-counts.
A quantity we did NOT rename hid its own semantics from us.

**Two failures in that run were NOT ours:**
- `slots=8,bytes=8388608` → `basebackup record footprint 8388848 too large for byte ring storage
  8388608 (need 2x headroom)`. That guard landed with `7a2eaed53` on **July 6**. **CLAUDE.md's own
  documented basebackup example has been broken for three days.** Fix the doc (or the `bytes=`
  handling); it is not a regression from the split.
- `DPU setup socket exchange failed` (TCP connects, handshake never logs) — the known receiver-DPU
  DOCA cold-start flakiness.

**Correcting the validation report:** it blamed `payload_ring_slot_count=0 payload_ring_slot_bytes=0`
at the receive sink. That is a red herring — the split never touched `localReceivePayloadRing`, and
that log line dates to `cfe564251` (May 31); it prints before the ring is created.

### REVISED ORDER (decided with the user): rename FIRST, then re-do the split

1. **Scoped rename** (table below), which forces every quantity to declare whether it includes the
   prefix. Add the invariant `recordFootprintBytes == recordPrefixBytes + maxObjectBytes` as an
   **assertion at both ends** (descriptor fill + frontend consume), not merely a comment — three
   instances of this bug class is enough evidence that comments do not hold the line.
2. **Establish and document the tiling/wrap-gap relationship** between `recordFootprintBytes` and
   `byteRingBytes` before changing either. Whether records must tile, or the wrap-gap protocol should
   absorb a non-tiling footprint, is an OPEN question — read the producer's gap logic first.
3. **Re-do the split** on top, with basebackup's `bytes=` semantics made explicit (does it size the
   payload or the record footprint?).
4. Gate on BOTH basebackup regression AND `--homer-dpu -c 1`, in that order.

### DEPLOYMENT HAZARD (until the next validation run)
The failed run installed and synced the **ABI v12** binaries to farnet0 and to BOTH DPU trees before
Test A ran. farnet1 has been rebuilt/reinstalled at the reverted ABI, but **farnet0 and the two DPUs
still carry v12**. Any run before a full resync + DPU rebuild is a mixed-ABI cluster. Resync first.

### Follow-up (DECIDED July 9, 2026, not yet done): finish the rename

The split left `sinkHandle` carrying BOTH `localQueueSlotCapacityBytes` (= 1080, the footprint) and
`slotCapacityBytes` (= 1024, the payload) — two fields, one name-stem, differing by exactly the
transport prefix. **That is the same pattern as the bug this doc is about.** Renaming has direct
defect-prevention value, not merely aesthetic value; an earlier note here claiming "no correctness
benefit" was wrong.

**But `slot` is NOT dead, and must not be swept out wholesale.** There is a genuine, live, indexed
fixed-slot shm queue: `slotIndex = (slotSequence - 1) % queueControl->slotCount`, addressed at a stride
of `queueControl->slotCapacityBytes` (`tuple_sink_service_process.c:16640-16642`). That is
`CitusTupleSinkQueueControl`, the host-shm result sink used by plain `--homer` (non-DPU) pgbench — a
working, validated path. **Homer has TWO coexisting local transports** (fixed-slot shm queue + byte
ring), and the queue descriptor describes both. That shared vocabulary is the true origin of the
double meaning: when the byte ring stopped being `slotCount * slotCapacityBytes`, the borrowed names
stopped agreeing. The fix is to stop the byte-ring path borrowing the slot queue's words.

Scoped rename, reusing vocabulary the code already has (`objectBytes`,
`HomerServiceValidateOutgoingPayloadObject`):

| Now | Becomes | Meaning |
|---|---|---|
| `stream.slotCapacityBytes`, `sinkHandle->slotCapacityBytes`, `batchHeader->slotCapacityBytes`, `requestedSlotCapacityBytes` | `maxObjectBytes` | payload a producer may fill |
| `descriptor->maxRecordBytes`, `sinkHandle->localQueueSlotCapacityBytes` | `recordFootprintBytes` | prefix + object; what the ring reserves |
| `slotReservedPrefixBytes` | `recordPrefixBytes` | the reserved prefix |
| `stream.slotCount`, `requestedSlotCount` | **deleted** on the byte-ring path | vestigial since `7a2eaed53` |

`maxRecordBytes` vs `maxObjectBytes` would sit one letter apart while differing by 56 bytes — a naming
hazard introduced by the split itself. `recordFootprintBytes` removes it.

**KEEP `slot` where a slot exists:** `CitusTupleSinkQueueControl`, control-region slots, DMA task slots,
mailbox slots, backend-spawn slots, and the byte-ring POOL slots (an arena slot genuinely is a slot).

Blast radius ≈ 200 occurrences (`slotCapacityBytes` 81, `slotReservedPrefixBytes` 49,
`requestedSlotCount` 34, `requestedSlotCapacityBytes` 24, `localQueueSlotCapacityBytes` 10). Mechanical,
compiler-verified, zero behavior change. Do it as its own commit AFTER the `--homer-dpu -c 1` gate
passes, then re-run the same gate.

Rejected alternatives:
- *(i) descriptor carries the negotiated `payloadSlotBytes`* — **breaks** `frontend:1169`, which needs
  `slotCount * slotCapacityBytes == byteRingBytes`.
- *(ii) reconcile `stream.slotCapacityBytes` to the derived value* (i.e. give SQL/COPY the special-case
  basebackup already has at `:23516`/`:33543`) — cures the symptom but **cements the double meaning**.
  A patch, not a fix.
- *(iii) relax the validator to `<=`* — loses a real geometry check, and `objectBytes >
  stream.slotCapacityBytes` (`:27569`) would still reject.

---

## Problem 2 — backend-to-backend COPY through Homer HANGS at HEAD (CONFIRMED; cause NOT established)

### What was tested
100k-row `\copy` into a single-shard distributed table, coordinator farnet1, placement farnet0,
`citus.enable_experimental_tuple_sink_routing=on`. Installed binaries verified to postdate the
suspect commits: `citus_tuple_sink_service` / `citus.so` mtime `2026-07-08 22:57`, tree at
`a3cdd5f3c` (2026-07-08); `7a2eaed53` confirmed an ancestor of HEAD.

### Result
The COPY **hangs indefinitely**. Both service logs go silent immediately after peer-transport setup:
```
farnet1: established persistent outgoing RDMA peer transport host=10.10.1.100 port=9717 node=4 traffic_class=1
farnet0: accepted persistent incoming RDMA peer transport host=10.10.1.101 traffic_class=1
```
…and nothing more, for the full 90 s window. The coordinator's `COPY` backend survives
`pg_ctl stop -m fast` (needed `-m immediate`).

**So CLAUDE.md's COPY baseline (5.1–5.4 s, June 6, 2026) no longer reproduces.** That baseline was
measured a month before `7a2eaed53` and must not be cited as evidence that COPY works today.

### The geometry bug is NOT the demonstrated cause
It is tempting — and wrong — to blame Problem 1. The evidence rules it out as the *proximate* cause:

- **No `remote exec backend` was ever spawned on farnet0.** The coordinator never dispatched a
  command to the worker.
- **No payload object was ever published**, so `HomerServiceValidateOutgoingPayloadObject` never ran
  and the `tuple-view batch header mismatch` line never appeared.
- Problem 1 can only fire *at publish time*. Nothing was published.

The hang is therefore **upstream of the data plane**, in the command/session path.

### Leading suspect (unverified): `a3cdd5f3c`
`a3cdd5f3c` (2026-07-08) — *"homer: delete the pending-binding registry (Part 3.5.2 stage 4)"* — is a
session-pairing change, and the observed failure is precisely "the session never pairs, so no command
is dispatched and no stream is created." That work was validated against **basebackup**, whose pairing
path differs from SQL/COPY's.

**Next step: bisect COPY across `7a2eaed53..a3cdd5f3c`.** Do not assume either commit. Two regressions
in three days on paths nobody re-ran is entirely plausible.

### COPY has BOTH bugs
Problem 1 affects COPY too (it is a `TUPLE_VIEW_BATCH` byte-ring stream, `slotCount=8`,
`slotCapacityBytes=524288` ⇒ derived `8388608/8 - 56 = 1048520 != 524288`). It simply never gets far
enough to fire it. **Fixing the hang will walk COPY straight into Problem 1**, so fix Problem 1 first
or expect the bisect's "good" commits to fail for a different reason.

### Why nobody noticed
Validation pivoted to basebackup on July 7 (checkpoint "Step 7"). No COPY run has happened since.

---

## Process lessons (worth more than either bug)

1. **A dated baseline is a timestamped observation, not a standing fact.** CLAUDE.md's COPY baseline
   reads like "COPY works". It means "COPY worked at some commit on June 6." It was used, in this very
   investigation, as evidence *against* a correct diagnosis. **Baselines should record the commit SHA,**
   so `git merge-base --is-ancestor` answers "does this still hold?".
2. **Two silent-failure classes found in one day.** The `tupleSourceRing` aliasing corrupts bytes with
   no invariant to trip; an aborted byte-ring stream hangs the client with no error reaching it; and
   now COPY hangs with no log line at all. The DPU relay path has systematically weak error
   propagation. See the relay-resolve miss counter added in citus `00a2c7d1b` for the shape of the
   fix: make the hang *say something*.
3. **"Fix looks obvious" ⇒ check who else depends on the value.** `:33539` looked like dead arithmetic.
   It is load-bearing for `frontend:1169`. Deleting it would have traded a loud abort for a broken
   attach.
