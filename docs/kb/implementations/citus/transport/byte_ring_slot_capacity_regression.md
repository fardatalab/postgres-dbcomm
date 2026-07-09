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

### Fix direction (not yet chosen)
- **(ii) preferred:** for byte-ring streams, reconcile `stream.slotCapacityBytes` to the
  descriptor-derived value, so the validator compares like with like. Note basebackup **already** gets
  special byte-ring geometry reconciliation (`:23516` and `:33543`); the SQL/COPY byte-ring case falls
  to the `else` at `:23527` and never did. This fix is "give SQL what basebackup already has."
- (i) make the descriptor carry the negotiated `payloadSlotBytes` — **breaks** `frontend:1169`.
- (iii) relax the validator to `<=` — loses a real geometry check; also `objectBytes >
  stream.slotCapacityBytes` at `:27569` would then still reject.

Rename the two concepts while fixing. One of them is not a "slot capacity".

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
