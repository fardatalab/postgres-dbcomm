# DOCA Homer Validation Harness

This directory contains standalone host-DPU experiments for the Homer DPU
offload plan. The harness is intentionally separate from PostgreSQL/Citus and
from the existing Homer service so that DMA, publication, sync-event, and COMCH
assumptions can be validated in isolation.

Initial scope:

- DPU DMA-writes fixed-size ring slots into host memory.
- DPU publishes a monotonic epoch with a final small DMA write.
- Host polls the memory-resident epoch, validates slot sequence/checksum fields,
  and advances a consumed epoch.
- Host CPU writes fixed-size ring slots into host memory and release-publishes an
  epoch for DPU DMA-read validation.
- DPU can pull host-written slots with a window of asynchronous DMA reads.
- Host can publish a tail through a remote-PCI DOCA sync-event, and the DPU can
  wait on that sync-event before DMA-reading the host slots.
- DPU can combine sync-event waits with a windowed async DMA-read path, which is
  the closest standalone shape to the intended host-produce / DPU-pull Homer
  hot path.
- `early-publish` intentionally publishes before writing the slot so the host
  validator proves it can catch incorrect publication.

Build on either the host or the DPU:

```sh
make -C /data/dbcomm/postgres-citus/homer/doca_validation
```

## Reproducible Runbook

The commands below assume `farnet1` as the host and `ssh dpu` as its local
BlueField. They intentionally keep all descriptors and logs under one artifact
directory so the run can be repeated or attached to a KB note.

Build the host binary and copy/build the same source on the DPU:

```sh
cd /data/dbcomm/postgres-citus
make -C homer/doca_validation clean
make -C homer/doca_validation

rsync -az --delete homer/doca_validation/ dpu:/tmp/doca_validation/
ssh dpu "make -C /tmp/doca_validation clean && make -C /tmp/doca_validation"
```

If the host binary has already been built, either keep the DPU `make clean &&
make` step exactly as shown or exclude the executable during source sync:

```sh
rsync -az --delete --exclude doca_homer_validation \
  homer/doca_validation/ dpu:/tmp/doca_validation/
ssh dpu "make -C /tmp/doca_validation clean && make -C /tmp/doca_validation"
```

Otherwise an x86 host executable can overwrite the DPU executable and fail on the
DPU with `cannot execute binary file: Exec format error`.

Run a small full-validation smoke first. The host process exports host memory
and a sync-event descriptor, then waits for the DPU to import them and consume
the slots.

```sh
OUT=/tmp/hdv_repro_smoke_$(date +%s)
mkdir -p "$OUT"

./homer/doca_validation/doca_homer_validation \
  --role=host \
  --mode=sync-event-async-pull \
  --descriptor-path="$OUT/hdv.desc" \
  --buffer-info-path="$OUT/hdv.buf" \
  --sync-event-path="$OUT/hdv.se" \
  --slot-count=64 \
  --slot-bytes=4096 \
  --batch-size=16 \
  --async-window=16 \
  --payload-mode=full \
  --iterations=512 \
  --ready-delay-ms=10000 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HDV_HOST_READY' "$OUT/host.log"; do sleep 0.1; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/hdv.desc" "$OUT/hdv.buf" "$OUT/hdv.se" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode=sync-event-async-pull \
  --descriptor-path='$OUT/hdv.desc' \
  --buffer-info-path='$OUT/hdv.buf' \
  --sync-event-path='$OUT/hdv.se' \
  --slot-count=64 \
  --slot-bytes=4096 \
  --batch-size=16 \
  --async-window=16 \
  --payload-mode=full \
  --iterations=512" \
  > "$OUT/dpu.log" 2>&1
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
tail -20 "$OUT/host.log"
tail -20 "$OUT/dpu.log"
```

Use `--pci-addr=...` on either side to force a specific DOCA device. If omitted,
the harness opens the first device that reports DMA memcpy support.

Run the best-throughput shape observed so far. This uses header validation so
the DPU still DMA-reads the whole slot but avoids scanning every payload byte on
the DPU CPU.

```sh
OUT=/tmp/hdv_repro_best_$(date +%s)
mkdir -p "$OUT"

./homer/doca_validation/doca_homer_validation \
  --role=host \
  --mode=sync-event-async-pull \
  --descriptor-path="$OUT/hdv.desc" \
  --buffer-info-path="$OUT/hdv.buf" \
  --sync-event-path="$OUT/hdv.se" \
  --slot-count=1024 \
  --slot-bytes=1048576 \
  --batch-size=512 \
  --async-window=128 \
  --payload-mode=header \
  --iterations=32768 \
  --ready-delay-ms=10000 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HDV_HOST_READY' "$OUT/host.log"; do sleep 0.1; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/hdv.desc" "$OUT/hdv.buf" "$OUT/hdv.se" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode=sync-event-async-pull \
  --descriptor-path='$OUT/hdv.desc' \
  --buffer-info-path='$OUT/hdv.buf' \
  --sync-event-path='$OUT/hdv.se' \
  --slot-count=1024 \
  --slot-bytes=1048576 \
  --batch-size=512 \
  --async-window=128 \
  --payload-mode=header \
  --iterations=32768" \
  > "$OUT/dpu.log" 2>&1
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
grep -E 'HDV_HOST_DONE|HDV_DPU_SYNC_EVENT_ASYNC_DONE|TIMEOUT|FAIL' \
  "$OUT/host.log" "$OUT/dpu.log"
```

Expected June 12, 2026 farnet1 result band for the best-throughput run:

- DPU-side: about `35.5 GiB/s`
- host-observed producer/consumer: about `26.3-26.5 GiB/s`
- reference artifacts:
  `/tmp/hdv_se_async_1m_w128_b512_32g_1781302954` and
  `/tmp/hdv_se_async_1m_w128_b512_32g_repeat_1781302998`

Do not add `--optimize-reports` for normal reproduction. It is intentionally an
opt-in stress knob because it stalled this harness in
`/tmp/hdv_se_async_64k_w256_b512_32g_1781302552` and
`/tmp/hdv_se_async_4k_w256_b4096_4g_optreports_1781302857`.

Implemented modes:

- `write-publish`: DPU writes slots by DMA, then publishes a host-visible epoch
  by DMA.
- `write-publish-async`: DPU queues windowed async DMA writes into host slots,
  observes data-DMA completions, then publishes the completed contiguous
  frontier by DMA. This is the main no-sync-event stress mode for DPU-issued
  payload writes followed by a DMA publication word.
- `write-publish-split-completion`: DPU queues payload DMA writes through one
  DOCA DMA context, observes that context's completed contiguous frontier, and
  publishes `published_epoch` through a second DOCA DMA context. This validates
  that split contexts are usable when publication is explicitly completion
  gated.
- `write-publish-split-nowait`: DPU queues payload DMA writes through one DOCA
  DMA context, but publishes the submitted frontier through a second context
  before payload completions are observed. This is an unsafe negative/stress
  mode for testing whether same-thread submission across contexts accidentally
  preserves publication ordering.
- `early-publish`: DPU publishes the epoch before writing the slot; this should
  fail validation.
- `host-write-dpu-read`: host fills slots and publishes with a CPU release
  store; DPU DMA-reads and validates slots. `--async-window=N` enables the
  windowed DPU-pull implementation.
- `sync-event-publish`: host fills slots and publishes the tail with
  `doca_sync_event_update_set()`; DPU waits on the remote-PCI sync-event before
  DMA-reading and validating slots. Requires `--sync-event-path=PATH`.
- `sync-event-early-publish`: host publishes the sync-event before filling the
  slot; this should fail when the DPU is waiting before publication.
- `sync-event-async-pull`: host fills slots and publishes the tail with
  `doca_sync_event_update_set()`; DPU waits on the remote-PCI sync-event, then
  drains the published range with reusable async DMA-read tasks. Requires
  `--sync-event-path=PATH` and is the main throughput mode for Homer-like
  host-produce / DPU-pull experiments.
- `dpu-sync-event-publish`: DPU writes slots into host memory by DMA, then
  publishes the completed frontier with a remote-PCI sync-event. The host waits
  on the event value and validates host-resident slots.
- `dpu-sync-event-early-publish`: DPU publishes the sync-event before writing
  the matching slot. This should fail host validation and proves the DPU-push
  validator catches early publication.

Useful knobs:

- `--payload-mode=full` validates every payload byte and checksum.
- `--payload-mode=header` still DMAs the full slot but validates only the
  sequence/checksum header, which is useful for DMA-throughput experiments.
- `--size-pattern=fixed` uses `--slot-bytes` as both the ring stride and the DMA
  record length for every epoch.
- `--size-pattern=mixed-64-4k` uses a 4 KiB ring stride and alternates 64 byte
  and 4 KiB DMA records. Use it with `--slot-bytes=4096` to model mixed control
  and payload traffic in one ring.
- `--size-pattern=mixed-64-1k` uses a 1 KiB ring stride and alternates 64 byte
  and 1 KiB DMA records. Use it with `--slot-bytes=1024` to model small control
  records mixed with small payload records.
- `--ready-delay-ms=N` makes the host pause after exporting descriptors so the
  DPU can import and wait before the host starts publishing.
- `--async-window=N` controls the number of reusable in-flight DMA-read tasks in
  the async DPU-pull modes.
- `--optimize-reports` requests deferred completion reporting for intermediate
  async data DMA tasks. Keep it off for correctness and reproducibility unless
  testing report-coalescing specifically; current stress runs showed final
  callbacks can be deferred enough to stall this harness.
- `--flush-report-sentinel` is a narrower diagnostic than `--flush=all`: it
  leaves optimized intermediate data DMA tasks aggregateable, but submits each
  non-optimized async report sentinel with `DOCA_TASK_SUBMIT_FLAG_FLUSH`. Use it
  only with `--optimize-reports` when testing whether a flushed sentinel retires
  previously deferred callbacks.
- `--relaxed-ordering` attempts to add
  `DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING` to the host-exported mmap. On the
  current farnet1 DOCA 3.2 runtime, every PCI-exportable permission combination
  with this flag fails at `doca_mmap_set_permissions()` with `Invalid input`.
  Treat it as an unsupported diagnostic knob for this PCI-export DMA harness
  unless a later DOCA version changes that behavior.

Best throughput shape observed so far:

```sh
./doca_homer_validation \
  --role=host \
  --mode=sync-event-async-pull \
  --descriptor-path=/tmp/hdv.desc \
  --buffer-info-path=/tmp/hdv.buf \
  --sync-event-path=/tmp/hdv.se \
  --slot-count=1024 \
  --slot-bytes=1048576 \
  --batch-size=512 \
  --async-window=128 \
  --payload-mode=header \
  --iterations=32768

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode=sync-event-async-pull \
  --descriptor-path=/tmp/hdv.desc \
  --buffer-info-path=/tmp/hdv.buf \
  --sync-event-path=/tmp/hdv.se \
  --slot-count=1024 \
  --slot-bytes=1048576 \
  --batch-size=512 \
  --async-window=128 \
  --payload-mode=header \
  --iterations=32768"
```

This mode reached about 35.5 GiB/s DPU-side DMA-read throughput and about
26.3-26.5 GiB/s host-observed producer/consumer throughput on farnet1 in the
June 12, 2026 runs. Treat those numbers as standalone harness evidence, not as
a final Homer integration result.

## No-Sync-Event Publication Checks

Use these modes to test the two DMA directions without sync-event as the
publication mechanism. They are useful negative/contrast cases for the
sync-event runs:

- `write-publish`: DPU DMA-writes host slots synchronously, then DPU DMA-writes
  `published_epoch`.
- `write-publish-async`: DPU queues windowed async DMA writes into host slots,
  waits for data-DMA completions, then DPU DMA-writes `published_epoch`.
- `host-write-dpu-read`: host CPU-writes slots, release-stores
  `published_epoch`, and the DPU polls/pulls that frontier with DMA reads.

The modes are intentionally not symmetric: DPU-push uses DMA for both data and
publication, while host-push uses host CPU stores for data and publication and
uses DMA only on the DPU consumer side. Use `write-publish-async` for the
harsher DPU-push check; use `host-write-dpu-read` with `--async-window > 1` for
the host-produce / DPU-pull async check.

Completion-gated publication does not require a depth-1 payload DMA stream.
`write-publish-async` queues up to `--async-window` payload writes, observes
data-DMA completions, advances a contiguous completed frontier, and only then
DMA-writes `published_epoch`. Use `--async-window=1` only when you intentionally
want the depth-1 contrast case.

Split-context publication modes test whether payload and tail publication can
live on different DOCA DMA contexts. Use `write-publish-split-completion` as the
positive control: payload DMA is on ctx A, tail DMA is on ctx B, but the tail is
published only after ctx A reports the completed contiguous frontier. Use
`write-publish-split-nowait` only as the negative/stress case: it publishes the
submitted frontier from ctx B before ctx A reports payload completions.

The current Homer design rule from the June 27, 2026 validation is: keep payload
and its publication tail on the same DMA context, or explicitly gate the tail on
observed payload completion before using another context. Do not rely on
same-thread submission to different DOCA DMA contexts as a publication ordering
mechanism. In the 64 B stress run, `write-publish-split-nowait` failed host
validation at epoch 96, while `write-publish-split-completion` passed the same
1,000,000-record shape.

```sh
OUT=/tmp/hdv_nosync_$(date +%s)
mkdir -p "$OUT"

MODE=write-publish-async        # or host-write-dpu-read
SLOTS=1024
SLOT_BYTES=4096
BATCH=1024
WINDOW=128
ITERATIONS=1000000
PATTERN=mixed-64-4k             # or fixed

./homer/doca_validation/doca_homer_validation \
  --role=host \
  --mode="$MODE" \
  --descriptor-path="$OUT/hdv.desc" \
  --buffer-info-path="$OUT/hdv.buf" \
  --slot-count="$SLOTS" \
  --slot-bytes="$SLOT_BYTES" \
  --batch-size="$BATCH" \
  --async-window="$WINDOW" \
  --payload-mode=full \
  --size-pattern="$PATTERN" \
  --iterations="$ITERATIONS" \
  --timeout-sec=300 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HDV_HOST_READY' "$OUT/host.log"; do sleep 0.05; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/hdv.desc" "$OUT/hdv.buf" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode='$MODE' \
  --descriptor-path='$OUT/hdv.desc' \
  --buffer-info-path='$OUT/hdv.buf' \
  --slot-count='$SLOTS' \
  --slot-bytes='$SLOT_BYTES' \
  --batch-size='$BATCH' \
  --async-window='$WINDOW' \
  --payload-mode=full \
  --size-pattern='$PATTERN' \
  --iterations='$ITERATIONS' \
  --timeout-sec=300" \
  > "$OUT/dpu.log" 2>&1
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
grep -E 'PASS|DONE|FAIL|TIMEOUT|ERROR' "$OUT/host.log" "$OUT/dpu.log"
```

Split-context example:

```sh
OUT=/tmp/hdv_split_ctx_$(date +%s)
mkdir -p "$OUT"

MODE=write-publish-split-completion  # positive control
# MODE=write-publish-split-nowait    # negative/stress case
SLOTS=4096
SLOT_BYTES=64
BATCH=1
WINDOW=128
ITERATIONS=1000000

./homer/doca_validation/doca_homer_validation \
  --role=host \
  --mode="$MODE" \
  --descriptor-path="$OUT/hdv.desc" \
  --buffer-info-path="$OUT/hdv.buf" \
  --slot-count="$SLOTS" \
  --slot-bytes="$SLOT_BYTES" \
  --batch-size="$BATCH" \
  --async-window="$WINDOW" \
  --payload-mode=full \
  --iterations="$ITERATIONS" \
  --timeout-sec=180 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HDV_HOST_READY' "$OUT/host.log"; do sleep 0.05; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/hdv.desc" "$OUT/hdv.buf" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode='$MODE' \
  --descriptor-path='$OUT/hdv.desc' \
  --buffer-info-path='$OUT/hdv.buf' \
  --slot-count='$SLOTS' \
  --slot-bytes='$SLOT_BYTES' \
  --batch-size='$BATCH' \
  --async-window='$WINDOW' \
  --payload-mode=full \
  --iterations='$ITERATIONS' \
  --timeout-sec=180" \
  > "$OUT/dpu.log" 2>&1
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
grep -E 'PASS|DONE|FAIL|TIMEOUT|ERROR' "$OUT/host.log" "$OUT/dpu.log"
```

To compare synchronous DMA, async depth 1, and async windowed publication with a
small fixed record size, keep everything else fixed and vary only `MODE` and
`WINDOW`:

```sh
OUT=/tmp/hdv_completion_depth_$(date +%s)
mkdir -p "$OUT"

# Run these combinations with the launch pattern above:
#   MODE=write-publish       WINDOW=1
#   MODE=write-publish-async WINDOW=1
#   MODE=write-publish-async WINDOW=32
#   MODE=write-publish-async WINDOW=128
SLOTS=1024
SLOT_BYTES=4096
BATCH=1024
ITERATIONS=262144
PATTERN=fixed
PAYLOAD=header
```

June 26, 2026 farnet1 results under `PCI_WR_ORDERING=force_relax` showed the
expected distinction: `write-publish-async --async-window=1` was about
`2.1 GiB/s` for 4 KiB records, while `--async-window=32` was about
`14.8 GiB/s`. For 1 MiB records, `window=1` was about `9.9 GiB/s` and
`window=32` was about `29.4 GiB/s`. In other words, waiting for data-DMA
completion before publishing a frontier does not by itself collapse the payload
DMA pipeline; a synchronous per-task helper or `--async-window=1` does.

## OPTIMIZE_REPORTS Sentinel Flush Check

Use this check when evaluating `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`.
`--flush=all` is not the intended Homer shape because it flushes every payload
task. The hypothesis under test is more specific: optimized payload tasks are
followed by a non-optimized report sentinel, and only that sentinel is submitted
with `DOCA_TASK_SUBMIT_FLAG_FLUSH`.

For the sync-event DPU-pull path, the old optimized-report shape reproduced the
deferred-callback stall:

```sh
# Host and DPU use:
--mode=sync-event-async-pull \
--slot-count=8192 \
--slot-bytes=4096 \
--batch-size=4096 \
--async-window=256 \
--payload-mode=header \
--iterations=1048576 \
--optimize-reports
```

June 26, 2026 artifact:
`/tmp/hdv_optreports_baseline_opt_1782521551`. The DPU timed out at
`completed=4094 submitted=4096 published=4096 consumed=4094`, while the host
timed out producing epoch `8193` because the first batch never retired far
enough to publish consumed credit.

Adding the sentinel flush completed the same run:

```sh
# Add this to the DPU command:
--optimize-reports --flush-report-sentinel
```

Artifacts:

| Variant | Result | Artifact |
| --- | --- | --- |
| optimized reports, no sentinel flush | FAIL, deferred-callback stall at `4094/4096` | `/tmp/hdv_optreports_baseline_opt_1782521551` |
| optimized reports, sentinel flush | PASS, all `1048576` 4 KiB records consumed | `/tmp/hdv_optreports_flush_sentinel_no_order_1782521620` |
| optimized reports, sentinel flush, ordered completions | PASS, all `1048576` 4 KiB records consumed | `/tmp/hdv_optreports_flush_sentinel_ordered_1782521593` |
| no optimized reports | PASS control | `/tmp/hdv_optreports_no_opt_control_1782521631` |

Interpretation: a flushed non-optimized sentinel is enough to make the DPU-pull
callback bookkeeping live in this reproduced stall shape. It is not yet a
throughput win; these reruns were slow on the DPU side even for the no-opt
control, so use them as liveness evidence rather than bandwidth evidence.

DPU-push `write-publish-async` did not reproduce the same stall in the matching
1 GiB check. Optimized reports, sentinel flush, sentinel flush plus ordered
completions, and no optimized reports all completed, but all variants showed
hundreds of millions of PE progress calls and are diagnostic-only until the
DPU-push progress loop is retuned.

For no-sync-event DPU-pull, `host-write-dpu-read` lets the DPU poll the
memory-resident host frontier and then rely on DMA completions for pulled
payloads. On June 26, 2026, the small-message shape showed a clear DPU-side
benefit from completion-report coalescing:

```sh
# Host and DPU use:
--mode=host-write-dpu-read \
--slot-count=4096 \
--batch-size=1024 \
--async-window=128 \
--payload-mode=header \
--iterations=1000000

# DPU variants:
#   no extra flags
#   --optimize-reports --flush-report-sentinel
#   --optimize-reports --flush-report-sentinel --ordered-completions
```

Representative repeat results:

| Record shape | No optimized reports | Optimized + flushed sentinel | Optimized + flushed sentinel + ordered |
| --- | ---: | ---: | ---: |
| fixed 64 B | `197.64 MiB/s` | `380.61 MiB/s` | `334.93 MiB/s` |
| fixed 1 KiB | `3172.74 MiB/s` | `5917.03 MiB/s` | `5363.48 MiB/s` |
| mixed 64 B / 1 KiB | `1686.22 MiB/s` | `3000.86 MiB/s` | `2960.70 MiB/s` |

The result is a DPU-side elapsed-time improvement for small DMA records. It does
not reduce `doca_pe_progress()` calls in the current harness, so the likely
benefit is lower DOCA/HW completion-report overhead or less callback-report
pressure, not a simpler outer progress loop.

## DPU-Push Sync-Event Publication Check

Use this mode to test the reverse direction where the DPU DMA-writes host memory
and uses sync-event as the publication point. This is the closest standalone
test for whether sync-event can play the `WRITE_WITH_IMM`-like role for a
DPU-push path.

```sh
OUT=/tmp/hdv_dpu_push_$(date +%s)
mkdir -p "$OUT"

./homer/doca_validation/doca_homer_validation \
  --role=host \
  --mode=dpu-sync-event-publish \
  --descriptor-path="$OUT/hdv.desc" \
  --buffer-info-path="$OUT/hdv.buf" \
  --sync-event-path="$OUT/hdv.se" \
  --slot-count=1024 \
  --slot-bytes=4096 \
  --batch-size=1024 \
  --payload-mode=full \
  --size-pattern=mixed-64-4k \
  --iterations=1000000 \
  --timeout-sec=300 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HDV_HOST_READY' "$OUT/host.log"; do sleep 0.05; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/hdv.desc" "$OUT/hdv.buf" "$OUT/hdv.se" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_homer_validation \
  --role=dpu \
  --mode=dpu-sync-event-publish \
  --descriptor-path='$OUT/hdv.desc' \
  --buffer-info-path='$OUT/hdv.buf' \
  --sync-event-path='$OUT/hdv.se' \
  --slot-count=1024 \
  --slot-bytes=4096 \
  --batch-size=1024 \
  --payload-mode=full \
  --size-pattern=mixed-64-4k \
  --iterations=1000000 \
  --timeout-sec=300" \
  > "$OUT/dpu.log" 2>&1
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
grep -E 'PASS|DONE|FAIL|TIMEOUT|ERROR' "$OUT/host.log" "$OUT/dpu.log"
```

The negative control should fail host validation:

```sh
# Change both host and DPU --mode values to dpu-sync-event-early-publish and
# add --early-publish-delay-us=50000.
```
