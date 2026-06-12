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

Useful knobs:

- `--payload-mode=full` validates every payload byte and checksum.
- `--payload-mode=header` still DMAs the full slot but validates only the
  sequence/checksum header, which is useful for DMA-throughput experiments.
- `--ready-delay-ms=N` makes the host pause after exporting descriptors so the
  DPU can import and wait before the host starts publishing.
- `--async-window=N` controls the number of reusable in-flight DMA-read tasks in
  the async DPU-pull modes.
- `--optimize-reports` requests deferred completion reporting for intermediate
  async data DMA tasks. Keep it off for correctness and reproducibility unless
  testing report-coalescing specifically; current stress runs showed final
  callbacks can be deferred enough to stall this harness.

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
