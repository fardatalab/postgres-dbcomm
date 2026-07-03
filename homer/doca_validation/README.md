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

## PCI-Imported Source to RDMA Write Probe

`doca_pci_rdma_bridge_validation` is a focused two-process probe for the
possible zero-copy Homer result path:

```text
host producer memory exported with doca_mmap_export_pci()
  -> DPU imports that PCI mmap with doca_mmap_create_from_export()
  -> DPU uses the PCI-imported doca_buf as doca_rdma_task_write src_buf
  -> host/frontend memory exported with doca_mmap_export_rdma() receives bytes
```

This is intentionally separate from `doca_homer_validation` because the DOCA
headers and samples document RDMA writes from local source buffers to remote
RDMA-exported buffers, but do not show a PCI-imported mmap as the RDMA local
source buffer. A successful run means the direct DPU-controlled forwarding path
is at least accepted by the installed DOCA stack; a failure means the Homer DPU
payload plan should keep the DPU-local bounce/ring path unless a lower-level
verbs/DevX route is validated separately.

Build and copy to the DPU:

```sh
cd /data/dbcomm/postgres-citus
make -C homer/doca_validation clean
make -C homer/doca_validation

rsync -az --delete --exclude doca_homer_validation \
  --exclude doca_pci_rdma_bridge_validation \
  homer/doca_validation/ dpu:/tmp/doca_validation/
ssh dpu "make -C /tmp/doca_validation clean && make -C /tmp/doca_validation"
```

Run with the host process exporting both the PCI source buffer and RDMA target
buffer. The DPU process imports both descriptors and submits the RDMA write. Use
`--rdma-ibdev` or `--rdma-pci-addr` on the host if the first RDMA-capable device
is not the intended RoCE port.

```sh
OUT=/tmp/hbr_direct_pci_rdma_$(date +%s)
mkdir -p "$OUT"

./homer/doca_validation/doca_pci_rdma_bridge_validation \
  --role=host \
  --pci-addr=0000:21:00.0 \
  --rdma-ibdev=mlx5_0 \
  --gid-index=3 \
  --source-pci-desc="$OUT/source.pci" \
  --source-info="$OUT/source.info" \
  --target-rdma-desc="$OUT/target.rdma" \
  --target-conn-desc="$OUT/target.conn" \
  --dpu-conn-desc="$OUT/dpu.conn" \
  --bytes=4096 \
  --timeout-sec=30 \
  > "$OUT/host.log" 2>&1 &
HOST_PID=$!

while ! grep -q 'HBR_HOST_READY' "$OUT/host.log"; do sleep 0.1; done
ssh dpu "mkdir -p '$OUT'"
rsync -az "$OUT/source.pci" "$OUT/source.info" "$OUT/target.rdma" dpu:"$OUT/"

ssh dpu "/tmp/doca_validation/doca_pci_rdma_bridge_validation \
  --role=dpu \
  --pci-addr=0000:03:00.0 \
  --rdma-ibdev=mlx5_2 \
  --gid-index=1 \
  --source-pci-desc='$OUT/source.pci' \
  --source-info='$OUT/source.info' \
  --target-rdma-desc='$OUT/target.rdma' \
  --target-conn-desc='$OUT/target.conn' \
  --dpu-conn-desc='$OUT/dpu.conn' \
  --bytes=4096 \
  --timeout-sec=30" \
  > "$OUT/dpu.log" 2>&1 &
DPU_SSH_PID=$!

COPIED_TARGET_CONN=0
COPIED_DPU_CONN=0
while kill -0 "$DPU_SSH_PID" 2>/dev/null; do
  if [ "$COPIED_TARGET_CONN" = 0 ] && [ -s "$OUT/target.conn" ]; then
    rsync -az "$OUT/target.conn" dpu:"$OUT/"
    COPIED_TARGET_CONN=1
  fi
  if [ "$COPIED_DPU_CONN" = 0 ] && ssh dpu "test -s '$OUT/dpu.conn'"; then
    rsync -az dpu:"$OUT/dpu.conn" "$OUT/"
    COPIED_DPU_CONN=1
  fi
  sleep 0.05
done
wait "$DPU_SSH_PID"
DPU_RC=$?

wait "$HOST_PID"
HOST_RC=$?
printf 'artifact=%s host_rc=%s dpu_rc=%s\n' "$OUT" "$HOST_RC" "$DPU_RC"
tail -40 "$OUT/host.log"
tail -40 "$OUT/dpu.log"
```

Expected success markers:

- host log: `HBR_HOST_VALIDATED direct_pci_source_rdma_write`
- DPU log: `HBR_DPU_DONE mode=direct-pci-rdma`

Observed on farnet1 on July 2, 2026 with host `mlx5_0`/GID index `3`, DPU
`mlx5_2`/GID index `1`, host PCI device `0000:21:00.0`, and DPU DOCA device
`0000:03:00.0`: the direct bridge probe passed for both 64 B and 4 KiB records.
The DPU imported the host PCI mmap, built the RDMA write source buffer from that
imported mmap, completed the RDMA write task, and the host validated the
RDMA-exported target buffer.

One important source-buffer detail: the RDMA source uses
`doca_buf_inventory_buf_get_by_data()`, not `doca_buf_inventory_buf_get_by_addr()`.
The latter can create a buffer with no data length for RDMA write purposes; an
earlier harness attempt completed the DPU write task but did not make the host
validate the target bytes until the source was switched to `get_by_data()`.

The same binary also has benchmark modes:

- `--mode=direct-pci-rdma`: DPU RDMA writes from the PCI-imported host source.
- `--mode=dma-stage-rdma`: DPU first DMA-pulls the host source into DPU-local
  memory, then RDMA writes from that local source.
- `--iterations=N --window=N`: allocate a fixed window of task/buffer lanes and
  keep up to `window` operations in flight.

The benchmark uses the DOCA ownership model documented in the headers: when a
DMA or RDMA completion callback fires, the task object is back in user
ownership, and the callback may resubmit it. Each lane owns reusable `doca_buf`
handles. The harness repositions those handles with
`doca_buf_inventory_buf_reuse_by_data()` or
`doca_buf_inventory_buf_reuse_by_addr()`, updates the DMA/RDMA task fields, and
resubmits the same task object. This supersedes an earlier per-operation-task
harness shape that only reached a few thousand ops/s and incorrectly made task
reuse look broken.

For performance runs, pass `--ready=PATH` and copy that file to the DPU before
the DPU starts its timed loop. The host writes the ready file only after its RDMA
endpoint is connected. Without this gate, the DPU can start posting against a
peer that is not fully ready yet; the resulting retries produced false
~1 GiB/s large-transfer results. Also avoid polling the DPU with repeated SSH
commands during the timed section.

Observed ready-gated benchmark results on July 2, 2026, host `mlx5_0`/GID index
`3`, DPU `mlx5_2`/GID index `1`, host PCI `0000:21:00.0`, DPU DOCA device
`0000:03:00.0`:

| Mode | Bytes | Iterations | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| direct PCI-source RDMA | 64 | 1000000 | 64 | 4471605.19 | 272.93 | `/tmp/hbr_ready_direct-pci-rdma_64_1000000_w64_1782960090778` |
| DMA-stage then RDMA | 64 | 100000 | 128 | 2468488.33 | 150.66 | `/tmp/hbr_ready_dma-stage-rdma_64_100000_w128_1782960096763` |
| direct PCI-source RDMA | 1024 | 1000000 | 128 | 4622649.19 | 4514.31 | `/tmp/hbr_ready_direct-pci-rdma_1024_1000000_w128_1782960093913` |
| DMA-stage then RDMA | 1024 | 100000 | 128 | 2631545.29 | 2569.87 | `/tmp/hbr_ready_dma-stage-rdma_1024_100000_w128_1782960099623` |
| direct PCI-source RDMA | 1048576 | 1000 | 128 | 11820.71 | 11820.71 | `/tmp/hbr_ready_direct-pci-rdma_1048576_1000_w128_1782960061597` |
| DMA-stage then RDMA | 1048576 | 1000 | 128 | 15238.14 | 15238.14 | `/tmp/hbr_ready_dma-stage-rdma_1048576_1000_w128_1782960064897` |

The corrected interpretation is still size-dependent, but the absolute
throughput is no longer suspiciously low. Direct PCI-source RDMA is better for
small records because it issues one RDMA operation instead of DMA plus RDMA.
For 1 MiB transfers both paths are in the expected 10+ GiB/s range. The staged
path was faster in this first corrected run, but a later rerun showed the
direct path much closer, so do not treat the exact 1 MiB ranking as stable
without repeating the warmed comparison. The older non-ready-gated numbers
around 1 GiB/s are invalid as performance evidence.

`ibverbs_rdma_write_validation` is a companion baseline that uses DPU-local
source memory and ordinary RC RDMA writes with the same fixed-window, reusable
lane shape. It also has a `--ready=PATH` gate. Ready-gated observations:

| Mode | Bytes | Iterations | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| ibverbs spread-buffer RDMA write | 64 | 1000000 | 64 | 9772303.66 | 596.45 | `/tmp/hvr_ready_spread_64_1000000_w64_1782960121268` |
| ibverbs spread-buffer RDMA write | 1024 | 1000000 | 128 | 14066114.31 | 13736.44 | `/tmp/hvr_ready_spread_1024_1000000_w128_1782960124203` |
| ibverbs spread-buffer RDMA write | 1048576 | 2000 | 128 | 18013.11 | 18013.11 | `/tmp/hvr_ready_spread_1048576_2000_w128_1782960000843` |
| ibverbs single-buffer RDMA write | 1048576 | 2000 | 128 | 29001.29 | 29001.29 | `/tmp/hvr_ready_single_1048576_2000_w128_1782960003962` |

`ib_write_bw` on the same DPU-to-host path with 1 MiB messages and TX depth 128
reported `242.99` Gb/s average (`/tmp/hdr_ib_write_bw_1782959673`), and
`--use_old_post_send` still reported `240.75` Gb/s
(`/tmp/hdr_ib_write_bw_oldpost_1782959814`). This confirms the path is capable
of high bandwidth; the false low results came from harness synchronization, not
from an inherent DOCA RDMA or ibverbs bandwidth ceiling.

Small-message window-depth sweep on the same ready-gated harness, using no SSH
polling during the timed section:

| API/path | Bytes | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | --- |
| DOCA direct PCI-source RDMA | 64 | 1 | 390129.69 | 23.81 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w1_1782960961313` |
| DOCA direct PCI-source RDMA | 64 | 2 | 786883.96 | 48.03 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w2_1782960965474` |
| DOCA direct PCI-source RDMA | 64 | 4 | 1375496.31 | 83.95 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w4_1782960968794` |
| DOCA direct PCI-source RDMA | 64 | 8 | 2503369.92 | 152.79 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w8_1782960971795` |
| DOCA direct PCI-source RDMA | 64 | 16 | 4427745.66 | 270.25 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w16_1782960975315` |
| DOCA direct PCI-source RDMA | 64 | 32 | 4550669.28 | 277.75 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w32_1782960978084` |
| DOCA direct PCI-source RDMA | 64 | 64 | 4640550.18 | 283.24 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w64_1782960980875` |
| DOCA direct PCI-source RDMA | 64 | 128 | 4596548.14 | 280.55 | `/tmp/hbr_small_direct-pci-rdma_64_500000_w128_1782960983665` |
| ibverbs RDMA write | 64 | 1 | 482844.22 | 29.47 | `/tmp/hvr_small_64_500000_w1_1782960986465` |
| ibverbs RDMA write | 64 | 2 | 917377.52 | 55.99 | `/tmp/hvr_small_64_500000_w2_1782960990105` |
| ibverbs RDMA write | 64 | 4 | 1575344.88 | 96.15 | `/tmp/hvr_small_64_500000_w4_1782960993215` |
| ibverbs RDMA write | 64 | 8 | 2408812.71 | 147.02 | `/tmp/hvr_small_64_500000_w8_1782960996145` |
| ibverbs RDMA write | 64 | 16 | 3477475.99 | 212.25 | `/tmp/hvr_small_64_500000_w16_1782960998955` |
| ibverbs RDMA write | 64 | 32 | 5034385.20 | 307.27 | `/tmp/hvr_small_64_500000_w32_1782961001725` |
| ibverbs RDMA write | 64 | 64 | 10668658.83 | 651.16 | `/tmp/hvr_small_64_500000_w64_1782961004435` |
| ibverbs RDMA write | 64 | 128 | 19700724.71 | 1202.44 | `/tmp/hvr_small_64_500000_w128_1782961007135` |

Representative 256 B and 1 KiB points:

| API/path | Bytes | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | --- |
| DOCA direct PCI-source RDMA | 256 | 1 | 377492.19 | 92.16 | `/tmp/hbr_small_direct-pci-rdma_256_500000_w1_1782961009765` |
| DOCA direct PCI-source RDMA | 256 | 16 | 4217545.66 | 1029.67 | `/tmp/hbr_small_direct-pci-rdma_256_500000_w16_1782961016806` |
| DOCA direct PCI-source RDMA | 256 | 128 | 4663065.54 | 1138.44 | `/tmp/hbr_small_direct-pci-rdma_256_500000_w128_1782961022396` |
| ibverbs RDMA write | 256 | 1 | 452237.40 | 110.41 | `/tmp/hvr_small_256_500000_w1_1782961041255` |
| ibverbs RDMA write | 256 | 16 | 3258429.70 | 795.52 | `/tmp/hvr_small_256_500000_w16_1782961047876` |
| ibverbs RDMA write | 256 | 128 | 19240698.91 | 4697.44 | `/tmp/hvr_small_256_500000_w128_1782961053306` |
| DOCA direct PCI-source RDMA | 1024 | 1 | 374194.51 | 365.42 | `/tmp/hbr_small_direct-pci-rdma_1024_500000_w1_1782961025195` |
| DOCA direct PCI-source RDMA | 1024 | 16 | 3956571.09 | 3863.84 | `/tmp/hbr_small_direct-pci-rdma_1024_500000_w16_1782961032305` |
| DOCA direct PCI-source RDMA | 1024 | 128 | 4877135.35 | 4762.83 | `/tmp/hbr_small_direct-pci-rdma_1024_500000_w128_1782961038506` |
| ibverbs RDMA write | 1024 | 1 | 444085.90 | 433.68 | `/tmp/hvr_small_1024_500000_w1_1782961055965` |
| ibverbs RDMA write | 1024 | 16 | 3392750.80 | 3313.23 | `/tmp/hvr_small_1024_500000_w16_1782961062595` |
| ibverbs RDMA write | 1024 | 128 | 17967252.60 | 17546.15 | `/tmp/hvr_small_1024_500000_w128_1782961067995` |

For transaction-style low pipeline depth, DOCA direct is in the same ballpark as
ibverbs: at window 1, 64 B is `390k` vs `483k` ops/s and 1 KiB is `374k` vs
`444k` ops/s. DOCA direct plateaus around `4.5M-4.9M` ops/s once the window is
16 or larger, while ibverbs continues scaling to much higher operation rates at
windows 64/128. This suggests the current DOCA harness is paying per-completion
callback/progress/submit overhead that verbs amortizes better at high depth.

Staged DMA-then-RDMA remains worse for small command-like records because it
uses two operations per semantic record:

| Path | Bytes | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | --- |
| DOCA staged DMA-then-RDMA | 64 | 1 | 238224.15 | 14.54 | `/tmp/hbr_small_dma-stage-rdma_64_100000_w1_1782961070655` |
| DOCA staged DMA-then-RDMA | 64 | 128 | 2713635.13 | 165.63 | `/tmp/hbr_small_dma-stage-rdma_64_100000_w128_1782961079675` |
| DOCA staged DMA-then-RDMA | 1024 | 1 | 223458.22 | 218.22 | `/tmp/hbr_small_dma-stage-rdma_1024_100000_w1_1782961082546` |
| DOCA staged DMA-then-RDMA | 1024 | 128 | 2698371.52 | 2635.13 | `/tmp/hbr_small_dma-stage-rdma_1024_100000_w128_1782961091666` |

`doca_dma_ibverbs_rdma_validation` is the hybrid path the Homer design discussion
needed separately from the all-DOCA staged mode:

```text
host producer memory exported with doca_mmap_export_pci()
  -> DPU DOCA DMA pulls into DPU-local staging memory
  -> DPU uses ibverbs RC RDMA write from that staging memory
  -> host/frontend verbs MR receives bytes
```

The first implementation keeps ownership simple: each lane has one DPU-local
stage slice, one reusable DMA task, and one reusable verbs WR/SGE. A lane is not
reused for another DMA pull until the verbs RDMA write completion for that lane
arrives. The harness originally recovered the lane with `op_index % window`;
that was wrong once completions arrived out of order and could resubmit a DMA
task for a still-busy lane. The fixed harness encodes both lane and operation in
the DOCA task user data and verbs `wr_id`.

Fresh ready-gated direct-vs-hybrid comparison points:

| Path | Bytes | Iterations | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| DOCA direct PCI-source RDMA | 64 | 500000 | 1 | 397330.14 | 24.25 | `/tmp/hbr_compare_direct-pci-rdma_64_500000_w1_1782963037769270601` |
| DMA + ibverbs RDMA | 64 | 500000 | 1 | 260835.86 | 15.92 | `/tmp/hbi_dma_ibverbs_64_500000_w1_1782962962028111775` |
| DOCA direct PCI-source RDMA | 64 | 500000 | 16 | 4442943.64 | 271.18 | `/tmp/hbr_compare_direct-pci-rdma_64_500000_w16_1782963041947548614` |
| DMA + ibverbs RDMA | 64 | 500000 | 16 | 2268095.61 | 138.43 | `/tmp/hbi_dma_ibverbs_64_500000_w16_1782962966917798492` |
| DOCA direct PCI-source RDMA | 64 | 500000 | 128 | 4121379.81 | 251.55 | `/tmp/hbr_compare_direct-pci-rdma_64_500000_w128_1782963044768559097` |
| DMA + ibverbs RDMA | 64 | 500000 | 128 | 4124596.74 | 251.75 | `/tmp/hbi_dma_ibverbs_64_500000_w128_1782962969828828818` |
| DOCA direct PCI-source RDMA | 1024 | 500000 | 1 | 385865.28 | 376.82 | `/tmp/hbr_compare_direct-pci-rdma_1024_500000_w1_1782963047578651899` |
| DMA + ibverbs RDMA | 1024 | 500000 | 1 | 252602.96 | 246.68 | `/tmp/hbi_dma_ibverbs_1024_500000_w1_1782962972678628148` |
| DOCA direct PCI-source RDMA | 1024 | 500000 | 16 | 4215372.09 | 4116.57 | `/tmp/hbr_compare_direct-pci-rdma_1024_500000_w16_1782963051318576030` |
| DMA + ibverbs RDMA | 1024 | 500000 | 16 | 2197374.57 | 2145.87 | `/tmp/hbi_dma_ibverbs_1024_500000_w16_1782962977359016587` |
| DOCA direct PCI-source RDMA | 1024 | 500000 | 128 | 4485836.23 | 4380.70 | `/tmp/hbr_compare_direct-pci-rdma_1024_500000_w128_1782963054117932722` |
| DMA + ibverbs RDMA | 1024 | 500000 | 128 | 3872376.69 | 3781.62 | `/tmp/hbi_dma_ibverbs_1024_500000_w128_1782962980278617619` |
| DOCA direct PCI-source RDMA | 1048576 | 2000 | 128 | 14916.66 | 14916.66 | `/tmp/hbr_compare_direct-pci-rdma_1048576_2000_w128_1782963056928739579` |
| DMA + ibverbs RDMA | 1048576 | 2000 | 128 | 15152.49 | 15152.49 | `/tmp/hbi_dma_ibverbs_1048576_2000_w128_1782962983118233662` |

This confirms the expected small-record answer: direct DOCA RDMA from the
PCI-imported host source is the better low-depth/default candidate because the
hybrid path still pays a DMA pull plus RDMA write per semantic record. At
64 B/window 128 the two paths tied in this run, but that is a high-depth batch
case, not the dependent per-session command shape. At 1 MiB/window 128 the
hybrid and direct paths were effectively tied around 15 GiB/s in this fresh run.

Both DOCA harnesses now support `--optimize-reports`,
`--flush-report-sentinel`, and `--report-interval=N`. Non-sentinel DOCA tasks
are submitted with `DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS`; every `N`th task
and the final task are non-optimized sentinels, optionally submitted with
`DOCA_TASK_SUBMIT_FLAG_FLUSH`. The harness rejects `N > window` when optimized
reports are enabled because the initial window must contain a sentinel to avoid
deferring every completion callback with no report boundary.

Selected opt+flush results using `report_interval=window`:

| Path | Bytes | Window | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | --- |
| DOCA direct opt+flush | 64 | 16 | 4491492.85 | 274.14 | `/tmp/hbr_direct_opt_64_500000_w16_r16_1782963246821934871` |
| DOCA direct opt+flush | 64 | 128 | 4459228.59 | 272.17 | `/tmp/hbr_direct_opt_64_500000_w128_r128_1782963249837067382` |
| DOCA direct opt+flush | 1024 | 16 | 4152210.57 | 4054.89 | `/tmp/hbr_direct_opt_1024_500000_w16_r16_1782963252618502318` |
| DOCA direct opt+flush | 1024 | 128 | 4427432.44 | 4323.66 | `/tmp/hbr_direct_opt_1024_500000_w128_r128_1782963255388817792` |
| DOCA direct opt+flush | 1048576 | 128 | 14948.64 | 14948.64 | `/tmp/hbr_direct_opt_1048576_2000_w128_r128_1782963258209376761` |
| DMA + ibverbs opt+flush | 64 | 16 | 1948190.93 | 118.91 | `/tmp/hbi_dma_ibverbs_opt_64_500000_w16_r16_1782963204465017402` |
| DMA + ibverbs opt+flush | 64 | 128 | 4028206.53 | 245.86 | `/tmp/hbi_dma_ibverbs_opt_64_500000_w128_r128_1782963207667425268` |
| DMA + ibverbs opt+flush | 1024 | 16 | 2003128.99 | 1956.18 | `/tmp/hbi_dma_ibverbs_opt_1024_500000_w16_r16_1782963211158302004` |
| DMA + ibverbs opt+flush | 1024 | 128 | 4265773.20 | 4165.79 | `/tmp/hbi_dma_ibverbs_opt_1024_500000_w128_r128_1782963214108504204` |
| DMA + ibverbs opt+flush | 1048576 | 128 | 16745.30 | 16745.30 | `/tmp/hbi_dma_ibverbs_opt_1048576_2000_w128_r128_1782963216917840381` |

The all-DOCA staged mode was then rerun with the same opt+flush controls to
close the missing comparison. For 1 MiB/window 128, the benefit did not require
a large report interval: `report_interval=2` was already much faster than the
non-optimized run.

| Path | Bytes | Window | Report interval | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| DOCA staged no opt | 1048576 | 128 | 1 | 13333.64 | 13333.64 | `/tmp/hbr_stage_1048576_2000_w128_1782967192896072629` |
| DOCA staged opt+flush | 1048576 | 128 | 2 | 15628.39 | 15628.39 | `/tmp/hbr_stage-opt-r2_1048576_2000_w128_1782967196323163480` |
| DOCA staged opt+flush | 1048576 | 128 | 4 | 15544.66 | 15544.66 | `/tmp/hbr_stage-opt-r4_1048576_2000_w128_1782967199484186871` |
| DOCA staged opt+flush | 1048576 | 128 | 8 | 15556.47 | 15556.47 | `/tmp/hbr_stage-opt-r8_1048576_2000_w128_1782967202665783379` |
| DOCA staged opt+flush | 1048576 | 128 | 16 | 15227.27 | 15227.27 | `/tmp/hbr_stage-opt-r16_1048576_2000_w128_1782967205842481296` |
| DOCA staged opt+flush | 1048576 | 128 | 32 | 15597.11 | 15597.11 | `/tmp/hbr_stage-opt-r32_1048576_2000_w128_1782967209041865682` |
| DOCA staged opt+flush | 1048576 | 128 | 64 | 15494.79 | 15494.79 | `/tmp/hbr_stage-opt-r64_1048576_2000_w128_1782967212235967598` |
| DOCA staged opt+flush | 1048576 | 128 | 128 | 15970.12 | 15970.12 | `/tmp/hbr_stage-opt-r128_1048576_2000_w128_1782967215414027857` |

Small-record staged checks stayed far below direct DOCA RDMA even with
opt+flush:

| Path | Bytes | Window | Report interval | Ops/s | MiB/s | Artifact |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| DOCA staged no opt | 64 | 128 | 1 | 2363298.51 | 144.24 | `/tmp/hbr_stage_64_500000_w128_1782967255108199352` |
| DOCA staged opt+flush | 64 | 128 | 2 | 2494733.42 | 152.27 | `/tmp/hbr_stage-opt-r2_64_500000_w128_1782967259149789914` |
| DOCA staged opt+flush | 64 | 128 | 128 | 2635927.92 | 160.88 | `/tmp/hbr_stage-opt-r128_64_500000_w128_1782967262180686335` |
| DOCA staged no opt | 1024 | 128 | 1 | 2659816.82 | 2597.48 | `/tmp/hbr_stage_1024_500000_w128_1782967265209319660` |
| DOCA staged opt+flush | 1024 | 128 | 2 | 2688398.16 | 2625.39 | `/tmp/hbr_stage-opt-r2_1024_500000_w128_1782967268258552531` |
| DOCA staged opt+flush | 1024 | 128 | 128 | 2723726.10 | 2659.89 | `/tmp/hbr_stage-opt-r128_1024_500000_w128_1782967271270919888` |

For direct DOCA RDMA, opt+flush did not materially improve this harness shape.
For the hybrid path, opt+flush hurt 64 B, was mixed for 1 KiB, and improved the
1 MiB throughput point from about `15152` to `16745` MiB/s. For all-DOCA
staged, opt+flush improved the large-transfer point from about `13334` to
`15628-15970` MiB/s depending on report interval. The model is therefore:
completion-report coalescing is useful for throughput batches where per-record
latency is not the semantic publication point, but it is not a free win for
small dependent commands. It is also not a replacement for application-level
range coalescing: if adjacent bytes share one lifetime and one publication
boundary, prefer one larger DMA/RDMA task over many small optimized tasks plus a
sentinel.

Implementation policy for DPU-backed Homer:

- Use direct DOCA RDMA from PCI-imported host memory for small command and
  completion records. This is the pgbench-like path where each session often has
  `K=1` dependent work and an extra DMA staging operation is not justified.
- Use DOCA DMA into DPU-local staging memory plus ibverbs RDMA write for large
  bulk payloads, starting with basebackup. Use opt+flush for the DOCA DMA side
  only when the DPU actually has multiple DMA tasks before a publication
  boundary. If the bytes are contiguous and share one boundary, make them one
  larger task first.
- Keep all-DOCA staged DMA-then-DOCA-RDMA as a fallback/comparison path, not the
  chosen basebackup data path.

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
