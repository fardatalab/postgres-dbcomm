# Printtup DPA Standalone Reserializer

This directory contains the standalone DPA prototype for replaying PostgreSQL
`printtup()` binary `DataRow` body serialization from a captured dump. The host
program parses a dump produced by the `printtup.c` instrumentation, prepares
compact row/field descriptors, submits row ranges to FlexIO CmdQ workers, and
checks the DPA-produced bytes against the dump's captured `ROWD` payloads.

The current lineitem benchmark uses:

- dump: `dumps/printtup_binary_dump_lineitem_100000.bin`
- DPU working directory: `/tmp/postgres-citus-dpa-printtup`
- DPU host binary: `/tmp/postgres-citus-dpa-printtup/build/printtup_dpa_host`
- logical rows per timed run: `5000000`
- prepared input/output working set: `100000` rows
- row batching: `--rows-per-task 4`
- input placement: `--input-location host`
- input layout: `--materialize-input-working-set`
- output correctness path: DPA window writeback enabled

## Files

- `printtup_dpa_common.h`: shared host/device structs and serializer enum.
- `printtup_dpa_device.c`: DPA-side serializer and batched row-range worker.
- `printtup_dpa_host.cpp`: ARM-side parser, preparation, CmdQ submission,
  timing, and verification harness.
- `build_printtup_dpa.sh`: DPU-side build script for the device archive and
  ARM host harness.
- `plot_lineitem_results.py`: pandas/seaborn plotting script for the lineitem
  benchmark CSV summaries.
- `results/lineitem_100k_thread_scaling_1_512.csv`: raw thread-scaling CSV.
- `results/lineitem_100k_thread_scaling_summary.csv`: compact thread-scaling
  summary with speedups.
- `results/lineitem_100k_rows_per_task_compare.csv`: 64-worker row batching
  comparison.
- `results/lineitem_100k_rows_per_task_summary.csv`: compact row batching
  summary with relative throughput and enqueue/drain shares.
- `results/lineitem_100k_working_set_sweep_t256_summary.csv`: 256-worker
  sweep that varies output/status slots and tuple-input rows separately and
  together.
- `figures/lineitem_benchmark_overview.png`: compact visual summary.
- `figures/lineitem_thread_scaling.png`: worker scaling and speedup plot.
- `figures/lineitem_rows_per_task.png`: row batching tradeoff plot.
- `figures/lineitem_working_set_sweep.png`: working-set sweep plot.

## Build On The DPU

From the host machine, copy the prototype and the generated dump to the DPU:

```bash
rsync -az fdl_utils/printtup_dump/dpa/ \
  dumps/printtup_binary_dump_lineitem_100000.bin \
  dpu:/tmp/postgres-citus-dpa-printtup/
```

Then build on the DPU:

```bash
ssh dpu 'cd /tmp/postgres-citus-dpa-printtup && ./build_printtup_dpa.sh build'
```

The build script expects the BlueField DOCA/FlexIO toolchain under
`/opt/mellanox/doca` and `/opt/mellanox/flexio`.

## Correctness Smoke

Run a short 100K-row smoke first. This uses the same serializer path as the
long run and verifies DPA output bytes on the DPU ARM cores.

```bash
ssh dpu 'cd /tmp/postgres-citus-dpa-printtup && \
  ./build/printtup_dpa_host \
    --dump printtup_binary_dump_lineitem_100000.bin \
    --threads 64 \
    --logical-rows 100000 \
    --working-set-rows 32768 \
    --input-working-set-rows 32768 \
    --rows-per-task 4 \
    --materialize-input-working-set \
    --input-location host \
    --iterations 1 \
    --csv lineitem_100k_smoke.csv \
    --timeout 180'
```

Expected result: `mismatches=0` and `status_errors=0`.

## Thread-Scaling Run

Run the main lineitem thread-scaling benchmark:

```bash
ssh dpu 'cd /tmp/postgres-citus-dpa-printtup && \
  ./build/printtup_dpa_host \
    --dump printtup_binary_dump_lineitem_100000.bin \
    --threads 1,2,4,8,16,32,64,128,256,512 \
    --logical-rows 5000000 \
    --working-set-rows 100000 \
    --input-working-set-rows 100000 \
    --rows-per-task 4 \
    --materialize-input-working-set \
    --input-location host \
    --iterations 1 \
    --csv lineitem_100k_thread_scaling_1_512.csv \
    --timeout 240'
```

If you already have the 1-64 and 128-512 raw CSVs separately, concatenate them
with one header:

```bash
cat lineitem_100k_logical_5m_full_ws.csv \
    lineitem_100k_threads_128_256_512.csv |
  awk 'NR==1 || $1 !~ /^threads/' > lineitem_100k_thread_scaling_1_512.csv
```

Copy results back to the repo:

```bash
rsync -az dpu:/tmp/postgres-citus-dpa-printtup/lineitem_100k_*.csv \
  fdl_utils/printtup_dump/dpa/results/
```

## Current Results

The latest `lineitem` thread-scaling run used 5M logical rows and a 100K-row
working set. All rows verified with zero mismatches.

| workers | host payload MB/s |
| ---: | ---: |
| 1 | 16.5114 |
| 2 | 31.7028 |
| 4 | 58.2153 |
| 8 | 94.4573 |
| 16 | 120.479 |
| 32 | 192.354 |
| 64 | 298.589 |
| 128 | 351.608 |
| 256 | 390.690 |
| 512 | 408.021 |

Host-observed throughput is the main number to compare. The harness starts
timing when it first submits work to the DPA CmdQ and stops before ARM-side
verification. DPA-cycle-derived throughput is still useful as a device-side
diagnostic, but it is an approximation because per-row cycle counters are summed
across workers.

The byte-writer optimization in `printtup_dpa_device.c` changed fixed-width
append helpers to write directly and changed raw payload append to use
fixed-size `__builtin_memcpy` 8/4/2-byte chunks plus a byte tail. This keeps the
C expression valid for unaligned PostgreSQL payload addresses while letting the
DPA compiler choose how to lower the copy. A focused rerun with the same 5M-row,
100K-working-set, materialized host-input settings produced:

| workers | before MB/s | builtin chunk append MB/s |
| ---: | ---: | ---: |
| 64 | 298.589 | 317.889 |
| 256 | 390.690 | 393.142 |
| 512 | 408.021 | 406.766 |

The focused CSV is `results/lineitem_100k_chunkcopy_t64_256_512.csv`. A later
experiment with explicit alignment gating around raw typed 8/4/2-byte loads and
stores was not a clear win for this workload; its CSV is
`results/lineitem_100k_alignedcopy_t64_256_512.csv`.

The network-byte-order conversion cost can be isolated at compile time with
`PRINTTUP_DPA_DEVICECC_OPTIONS=-DPRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER=1`. This
makes fixed-width emit helpers write DPA-native integer bytes, so the generated
body intentionally fails PostgreSQL wire-byte comparison. On the same 5M-row
lineitem shape, skipping byte-order output was effectively noise-level:

| workers | normal MB/s | native-order MB/s | delta |
| ---: | ---: | ---: | ---: |
| 64 | 318.737 | 318.480 | -0.081% |
| 256 | 392.803 | 395.976 | +0.808% |
| 512 | 407.873 | 410.519 | +0.649% |

The compact comparison is `results/lineitem_100k_nbo_toggle_summary.csv`; raw
CSVs are `results/lineitem_100k_nbo_macro_t64_256_512.csv` and
`results/lineitem_100k_native_order_macro_t64_256_512.csv`.

## Bottleneck Isolation

Compile-time isolation builds show that network byte order is not where the
time goes. Skipping numeric parsing/output or text payload copying reduces
DPA-cycle work, and skipping all per-field payload serializers cuts average DPA
cycles per row roughly in half at 512 workers. But with the old
`--rows-per-task 4` setting, host-observed throughput does not improve because
the run still submits and drains `1,250,000` CmdQ items.

The key follow-up is row-batch tuning at high worker counts. The earlier
`--rows-per-task 4` choice came from a 64-worker sweep; at 512 workers it is too
fine-grained. With correct output and the real serializer:

| rows per task | CmdQ tasks | host payload MB/s |
| ---: | ---: | ---: |
| 4 | 1,250,000 | 408.542 |
| 8 | 625,000 | 703.489 |
| 16 | 312,500 | 957.032 |
| 32 | 156,250 | 713.974 |

`--rows-per-task 16` is the best tested point for 512 workers so far. Disabling
window writeback did not materially change the 512-worker normal run
(`408.542` to `407.596 MB/s`), so the remaining host-observed bottleneck is
more likely CmdQ item scheduling/drain and row-range granularity than explicit
`__dpa_thread_window_writeback()` cost.

The next isolation cut skipped the entire field loop using the captured row body
length. At 512 workers and `--rows-per-task 16`, this reduced the device work to
about `88` cycles/row, so the earlier `fast_fields` residual is almost entirely
generic field-loop/framing work rather than row/result accounting. Based on that,
the serializer now writes captured field payload lengths up front and verifies
the serializer produced exactly that length, instead of writing zero and patching
the field length after serialization. This is a standalone-harness optimization;
an integrated path needs either the same precomputed length or the older
reserve/patch pattern.

Correct-output improvement from writing field lengths up front:

| workers | rows per task | before MB/s | len-upfront MB/s |
| ---: | ---: | ---: | ---: |
| 256 | 16 | 616.596 | 684.917 |
| 512 | 16 | 957.032 | 1044.840 |
| 512 | 4 | 408.542 | 423.832 |

Follow-up attempts probed the remaining field-loop cost. A schema-specialized
lineitem plan unrolled the known 16-column serializer sequence and bypassed the
generic serializer dispatch. This was only marginal by itself, and the first
version even increased DPA cycles, suggesting that code size/function-call
effects can erase simple branch-removal wins on the DPA cores. A narrower
trusted-writer change then skipped repeated per-append capacity checks when the
captured row payload length fits in the output slot. Finally, the lineitem plan
was changed to write with direct pointer/offset helpers, bypassing
`DpaByteWriter` inside the specialized path after a row-level capacity precheck.
That reduced DPA cycles but did not materially improve the host-observed 512
worker throughput, so the remaining hot-path cost is not just the generic writer
reserve/check branch.

| variant | 512 workers, rows/task=16 |
| --- | ---: |
| len-upfront generic | 1044.840 MB/s |
| lineitem plan, reduced checks | 1058.280 MB/s |
| trusted writer + lineitem plan | 1071.740 MB/s |
| direct pointer lineitem plan | 1067.500 MB/s |
| inferred text/numeric layout | 1074.840 MB/s |

For the direct pointer plan at 512 workers, `rows_per_task=16` remains the best
tested batching point: `8` rows/task reached `749.384 MB/s`, `16` reached
`1067.500 MB/s`, `32` reached `832.493 MB/s`, and `64` reached
`480.178 MB/s`.

The next useful improvement was not adding more descriptor metadata. Enlarging
`PrinttupDpaField` for predecoded text/numeric metadata made the 512-worker run
slower (`1035.510 MB/s` ABI-only, `1042.100 MB/s` with metadata used), so the
descriptor footprint itself is part of the hot-path cost. The retained
optimization instead infers text payload offset and numeric digit layout from
the existing `normalized_len` and `serialized_len` fields, avoiding varlena
decode without growing the descriptor. That is the current best correct
DataRow-body path: `1074.840 MB/s`, `28646.5` DPA cycles/row.

Further isolations show why the old "generic" bucket looked so large. With the
inferred layout, skipping text payload copies or all numeric work each reduces
the 512-worker path to about `23690` cycles/row; skipping both reaches
`20878.7` cycles/row. A lineitem framing-only isolation that writes the 16
per-field length words and only advances payload lengths still costs
`17715.8` cycles/row when writing to the host output window. Moving that
framing-only body to a DPA ring lowers it to `9083.1` cycles/row, so many tiny
host-window stores are expensive. The full correct serializer, however, did not
benefit from DPA-ring output, and DPA-heap input was worse (`39289.6`
cycles/row), so the best current setup is still host input plus host output with
compact inferred layout. A DPA-local scratch row followed by a final row copy was
also worse (`51837.8` cycles/row) and remains off by default.

An output-shape experiment omitted the DataRow `natts` word and all 16 per-field
length words while still running the scalar/numeric/text payload serializers.
That intentionally mismatched normal verification, but it reduced cycles to
`21846.3` per row and raised the host-observed byte-accounting proxy to
`1185.590 MB/s`. This says DataRow framing stores cost roughly `6.8K` cycles per
row on this shape; a future RDMA-oriented output format that carries lengths out
of band could be meaningfully cheaper than exact PostgreSQL DataRow bodies.

The first fixed-schema split-output experiment writes a compact 32-byte metadata
prefix plus a contiguous payload stream into a DPA-private ring. For lineitem,
the metadata stores only `payload_len`, the three variable numeric send lengths,
and `l_comment` length; fixed widths and fixed text lengths are implied by the
schema. With per-row host result headers still enabled for DPA-cycle accounting,
the 512-worker run reached `1130.530 MB/s` and `23279.4` cycles/row, compared
with `1078.530 MB/s` and `28609.7` cycles/row for the exact DataRow path in the
same 190/256/512 sweep. A timing-only variant that also suppresses per-row host
result headers and disables window writeback reached `1158.330 MB/s` at 512
workers. This is closer to the intended DPA-private-ring hot path, but it has no
DPA-cycle counters because the ARM side intentionally does not read per-row
headers. Retesting row batching for this shape kept `rows_per_task=16` as the
best current CmdQ setting (`8/16/32/64` rows per task measured
`798.891/1158.240/1019.240/611.578 MB/s` at 512 workers).

`--schedule static-chunked` is the first no-atomic per-worker queue
approximation. The host enqueues one CmdQ RPC per logical worker; that RPC walks
fixed-size chunks in a strided pattern (`worker_id`, `worker_id + workers`, and
so on). This removes the many-small-CmdQ-task control overhead from the hot
measurement while still keeping the benchmark inside FlexIO CmdQ. With split
DPA-ring output, no per-row result headers, and no window writeback,
190/256/512 workers measured `2462.810/1846.490/2233.310 MB/s`; the exact
DataRow byte-verified path measured `1917.690/1453.270/1817.470 MB/s`.
Unlike the earlier contiguous scheduler, the best host-observed point is now
near the observed available EU count (`190`) instead of at 512 logical workers.
At 190 workers, varying the static chunk size with
`rows_per_task=1/4/16/64/256` was almost flat:
`2426.290/2464.620/2461.670/2467.300/2463.450 MB/s`. The useful result is not a
chunk-size sweet spot; it is that removing repeated CmdQ enqueue/drain cycles is
worth roughly 2-3x for this timing-only hot-path proxy.

The follow-up static memory-path sweep added `--ring-partition
shared|per-worker`. Per-worker ring partitioning is an ownership cleanup, but it
was not a throughput win by itself: shared versus per-worker DPA rings measured
`2449.58/1848.82/2226.43 MB/s` and `2453.68/1855.48/2227.91 MB/s` at
190/256/512 workers. Ring location still mattered modestly: with 190 workers and
per-worker partitioning, host-window ring output measured `2358.27 MB/s`, while
DPA-heap ring output measured `2454.19 MB/s`. Input location mattered much more:
host-window input measured `2453.56 MB/s`, while DPA-heap input measured
`1637.12 MB/s` with the same DPA-heap output ring. The operational caveat is
that the current DPA-heap input setup performs many small `flexio_copy_from_host`
calls before timing begins; the timed result still shows that DPA-heap tuple
input is not automatically the faster hot-path read source in this harness.

That DPA-heap input result was then corrected by changing setup to stage all
normalized tuple bytes in one contiguous host buffer, copy that blob to DPA heap
once, and patch field offsets into absolute DPA addresses. With packed DPA input,
the 190-worker result improved from `1637.12 MB/s` to `2398.61 MB/s`, close to
the host-window input baseline of `2455.53 MB/s`. At 190/256/512 workers, packed
DPA input measured `2398.61/1708.40/2289.30 MB/s`; host-window input measured
`2455.53/1852.53/2226.72 MB/s`. The corrected takeaway is that many tiny DPA
heap allocations were the problem; DPA heap input itself is roughly competitive
when laid out contiguously.

The first packed-DPA-input working-set cache sweep used the static scheduler,
190 workers, 25M logical rows, `--rows-per-task 16`, materialized tuple input,
per-worker DPA-ring output, and no window writeback. The output-ring sweep varied
`--ring-output-bytes` from `64 KiB` to `256 MiB` while holding tuple input at the
100K-row dump footprint (`14.68 MiB`). Throughput stayed mostly flat:
`2371.73`, `2353.62`, `2320.58`, `2336.58`, `2354.80`, `2328.20`, and
`2333.91 MB/s` for `64 KiB`, `256 KiB`, `1 MiB`, `4 MiB`, `16 MiB`, `64 MiB`,
and `256 MiB`. This does not show a useful output-cache-size cliff; even the
smallest ring gives each worker only about `344 B` of ring space, but it is not
faster than larger per-worker regions.

The tuple-input sweep held the DPA output ring at `64 MiB` and varied
`--input-working-set-rows` from `1024` to `1000000`, which corresponds to about
`0.15 MiB` through `146.81 MiB` of packed DPA input bytes. The small-input
hypothesis did not hold here: throughput rose from `2020.34 MB/s` at 1024 rows
and `1964.34 MB/s` at 4096 rows to `2328.60 MB/s` at 100K rows and
`2452.90/2446.58 MB/s` at 524288/1000000 rows. The likely explanation is that
the static worker loop in `printtup_dpa_reserialize_static_worker()` makes many
workers revisit the same tiny set of prepared row descriptors and payload
addresses when the input footprint is very small. That can increase hot-address
contention or reduce memory-system parallelism, so "fits in cache" is not
automatically better on these DPA cores.

Two follow-up input-side optimization experiments tested that hypothesis. First,
`--input-map worker-offset` changed the static-worker prepared-row mapping so a
worker uses its local row ordinal plus a hashed worker offset instead of plain
`logical_index % prepared_rows`. This was byte-verified on a 100K-row exact run
with zero mismatches, but it was not a throughput win: compared with the logical
mapping, it measured `-4.31%`, `-1.01%`, `-1.33%`, `-3.27%`, and `-5.27%` at
`1024`, `4096`, `16384`, `100000`, and `524288` prepared input rows. That
weakens the simple hot-address-convergence explanation.

Second, the benchmark-only `PRINTTUP_DPA_BENCH_CONTIG_LINEITEM_INPUT=1` build
assumes materialized packed lineitem input and walks one normalized-input cursor
through the 16 fields, avoiding per-field payload-pointer loads in the split
output serializer. This helped the tiny 4096-row input footprint
(`1951.73 -> 2202.86 MB/s`, `+12.87%`) but regressed at larger footprints
(`2328.53 -> 2278.34 MB/s`, `-2.16%` at 100K rows, and
`2438.97 -> 2409.55 MB/s`, `-1.21%` at 524288 rows). The useful takeaway is
that descriptor-pointer traffic can matter when the input footprint is tiny, but
the precomputed field metadata path is better for the representative large
working-set case because it avoids reparsing varlena headers just to recover
lengths.

The next two input/metadata experiments were both useful. `--input-map
worker-shard` gives each static DPA worker a disjoint prepared-row shard and
cycles within that shard, keeping total prepared rows unchanged while avoiding
cross-worker descriptor/payload sharing. It passed an exact 100K-row smoke with
zero mismatches. In timing-only split-output runs, it improved logical mapping by
`+24.67%`, `+26.99%`, `+19.88%`, `+4.34%`, and `+0.40%` at `1024`, `4096`,
`16384`, `100000`, and `524288` prepared input rows. This is the first
optimization that makes the tiny input footprints competitive with the large
footprints.

`PRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT=1` keeps the precomputed
`PrinttupDpaField` lengths/addresses but uses a generated lineitem split-output
path with no generic serializer dispatch and no fallback varlena decode path. On
the logical mapping it improved throughput by `+2.22%`, `+3.22%`, `+2.29%`, and
`+2.07%` at `1024`, `4096`, `100000`, and `524288` prepared input rows. Combined
with `worker-shard`, it added another `+2.10%`, `+2.21%`, `+1.88%`, and `+1.89%`
over the worker-shard baseline. The best observed point in this sweep was
`2571.18 MB/s` at 1024 prepared input rows with both optimizations enabled.

The follow-up `1/3/4/5` optimization sweep tested four additional ideas on the
same static-worker, DPA-input, DPA-ring shape. The code now records Arm/control
setup time separately from hot DPA submission-to-drain time, so each CSV row has
both `host_serializer_work_throughput_mb_s` and
`end_to_end_serializer_work_throughput_mb_s`. The setup column includes
preparing normalized input, copying the packed DPA input blob, copying task/field
descriptor arrays, and installing the batch config. For 25M logical rows, setup
was about `145 ms` for 1024 materialized input rows and about `765-769 ms` for
100K materialized input rows.

The best new result was the compiled-plan shape: keep reading split metadata
from `PrinttupDpaField` (`PRINTTUP_DPA_BENCH_TASK_SPLIT_META=0`), shrink the
split row prefix from 32 bytes to 16 bytes
(`PRINTTUP_DPA_BENCH_SPLIT_META_SIZE=16`), and force the already-selected
lineitem plan at compile time (`PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN=1`). That
reached `2692.92 MB/s` at 190 workers / 1024 input rows and `2592.04 MB/s` at
190 workers / 100K input rows. The non-forced 16-byte field-metadata path was
`2571.26 MB/s` and `2488.60 MB/s`, so skipping the per-row `plan_id` branch and
schema check is worth about `4-5%` in this generated lineitem benchmark.

Moving `payload_len` and selected variable lengths into the per-row
`PrinttupDpaTask` descriptor was not a win: with the same 16-byte prefix but
without the forced plan it fell to `2553.59 MB/s` and `2463.28 MB/s`. The likely
reason is that the saved field loads are too small to pay for the larger task
descriptor and extra task-field dependencies.

The worker-local ring cursor (`PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR=1`) also
did not help in this static-worker shape. At 190 workers it was slightly slower
for both 1024-row and 100K-row input footprints; at 256 workers it helped the
tiny input footprint slightly but 256 workers remained much slower overall.
This suggests the old per-row ring-slot multiply/modulo is not currently the
dominant hot-path cost, or the added cursor branch/pointer dependency cancels
the saved arithmetic. CSV summary:
`results/lineitem_100k_optimizations_1_3_4_5_t190_256_summary.csv`.

Current best timing-only build:

```bash
PRINTTUP_DPA_DEVICECC_OPTIONS=-DPRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT=1,-DPRINTTUP_DPA_BENCH_NO_ROW_HEADERS=1,-DPRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT=1,-DPRINTTUP_DPA_BENCH_TASK_SPLIT_META=0,-DPRINTTUP_DPA_BENCH_SPLIT_META_SIZE=16,-DPRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN=1 \
  ./build_printtup_dpa.sh build_opt_precomp_fieldmeta16_forcedplan
```

Current best timing-only run, assuming the 100K lineitem dump is present in the
working directory:

```bash
./build_opt_precomp_fieldmeta16_forcedplan/printtup_dpa_host \
  --dump printtup_binary_dump_lineitem_100000.bin \
  --threads 190 \
  --logical-rows 25000000 \
  --working-set-rows 100000 \
  --input-working-set-rows 1024 \
  --rows-per-task 16 \
  --materialize-input-working-set \
  --input-location dpa \
  --input-map worker-shard \
  --iterations 1 \
  --ring-output-bytes 64m \
  --ring-output-location dpa \
  --ring-partition per-worker \
  --no-window-writeback \
  --schedule static-chunked \
  --csv lineitem_100k_opt_fieldmeta16_forcedplan_input_1024_t190.csv \
  --timeout 240
```

The same build with `--input-working-set-rows 100000` is the more conservative
large-input-footprint comparison and measured `2592.04 MB/s` at 190 workers.

The newest best timing-only runs do not produce DPA cycle percentages because
they disable per-row result headers and window writeback. The latest
cycle-accounted isolation still puts generic non-payload-serializer work at about
`61.8%` for the exact DataRow host-output path:
`17715.8 / 28646.5` cycles/row. With a DPA-ring output body, the framing-only
bucket is smaller: `9083.1` cycles/row, or about `39.0%` of the
cycle-accounted split-output DPA-ring path (`23279.4` cycles/row).

Raw and compact CSVs:

- `results/lineitem_100k_bottleneck_isolation_summary.csv`
- `results/lineitem_100k_rows_per_task_t512_followup_summary.csv`
- `results/lineitem_100k_rpt16_thread_followup_summary.csv`
- `results/lineitem_100k_fast_row_and_len_upfront_summary.csv`
- `results/lineitem_100k_schema_plan_and_trusted_writer_summary.csv`
- `results/lineitem_100k_direct_lineitem_rows_per_task_summary.csv`
- `results/lineitem_100k_inferred_layout_and_isolation_summary.csv`
- `results/lineitem_100k_split_output_worker_scaling_summary.csv`
- `results/lineitem_100k_split_output_rows_per_task_summary.csv`
- `results/lineitem_100k_static_chunked_schedule_summary.csv`
- `results/lineitem_100k_static_chunked_chunk_size_summary.csv`
- `results/lineitem_100k_static_mempath_summary.csv`
- `results/lineitem_100k_static_packed_input_summary.csv`
- `results/lineitem_100k_cache_working_set_sweep_t190_summary.csv`
- `results/lineitem_100k_next_optimizations_t190_summary.csv`
- `results/lineitem_100k_first_two_optimizations_t190_summary.csv`
- `results/lineitem_100k_optimizations_1_3_4_5_t190_256_summary.csv`

## Figures

Regenerate the plots from the CSV summaries:

```bash
python3 fdl_utils/printtup_dump/dpa/plot_lineitem_results.py
```

The generated figures are:

- `figures/lineitem_benchmark_overview.png`: one-page view of thread scaling,
  row batching, and working-set effects.
- `figures/lineitem_thread_scaling.png`: shows host throughput tapering as
  DPA workers increase, plus measured speedup against ideal speedup.
- `figures/lineitem_rows_per_task.png`: shows the `rows_per_task=4` sweet spot
  and how CmdQ task count/enqueue share change with batching.
- `figures/lineitem_working_set_sweep.png`: shows that small output/status
  pools and small tuple-input pools have different performance effects.
- `figures/lineitem_cache_working_set_sweep.png`: shows the packed-DPA-input
  static-worker cache sweep, including flat output-ring behavior and the
  unexpected slowdown at very small tuple-input footprints.

## Row Batching

`--rows-per-task` controls how many logical rows one CmdQ task processes in a
serial loop on a DPA worker. It does not change the configured worker count, but
it changes work granularity. On the latest 64-worker lineitem sweep:

| rows per task | host payload MB/s |
| ---: | ---: |
| 1 | 118.824 |
| 4 | 306.105 |
| 16 | 197.227 |
| 64 | 91.5799 |
| all | 17.7684 |

Very small batches spend too much time enqueuing CmdQ tasks. Very large batches
reduce scheduling granularity and can leave workers waiting for coarse serial
row ranges to finish. For this workload, `--rows-per-task 4` is the best tested
point so far.

Reproduce the row batching sweep:

```bash
ssh dpu 'cd /tmp/postgres-citus-dpa-printtup && \
  for rpt in 1 4 16 64 all; do \
    ./build/printtup_dpa_host \
      --dump printtup_binary_dump_lineitem_100000.bin \
      --threads 64 \
      --logical-rows 5000000 \
      --working-set-rows 100000 \
      --input-working-set-rows 100000 \
      --rows-per-task "$rpt" \
      --materialize-input-working-set \
      --input-location host \
      --iterations 1 \
      --csv "lineitem_100k_rpt_${rpt}.csv" \
      --timeout 180; \
  done'
```

The compact summary is in
`results/lineitem_100k_rows_per_task_summary.csv`.

## Working Sets

The benchmark intentionally separates output/status slot reuse from tuple-input
locality:

- `--working-set-rows` controls the number of reusable output/status slots.
  The ARM host allocates `output_slot_size * working_set_rows`, and the DPA maps
  each logical row to `logical_index % output_slot_rows`. A smaller value means
  more output slot reuse and less host-visible output/status memory; it does not
  reduce the logical number of rows serialized.
- `--input-working-set-rows` controls the number of prepared input row
  descriptors and normalized tuple payloads. The DPA maps each logical row to
  `logical_index % prepared_rows`. With `--materialize-input-working-set`, this
  also duplicates normalized input bytes, so this option changes the tuple-input
  memory footprint seen by DPA loads.

If `--input-working-set-rows` is omitted, it defaults to `--working-set-rows`.
For the lineitem thread-scaling runs both are set to `100000`, so the DPA cycles
through the entire captured lineitem dump before repeating logical rows.

The latest 256-worker working-set sweep used values `1024`, `4096`, `16384`,
`32768`, and `100000`. The compact CSV is in
`results/lineitem_100k_working_set_sweep_t256_summary.csv`.

| scenario | 1024 | 4096 | 16384 | 32768 | 100000 |
| --- | ---: | ---: | ---: | ---: | ---: |
| output only | 278.125 | 294.748 | 292.460 | 389.896 | 391.539 |
| input only | 464.821 | 431.002 | 389.759 | 387.262 | 391.729 |
| both equal | 283.141 | 296.351 | 293.659 | 390.212 | 390.724 |

These numbers are host-observed payload MB/s. In this run, small output working
sets hurt because they force many more CmdQ chunks before output slots can be
reused. Small input working sets can help because the DPA repeatedly reads a
smaller normalized tuple-input footprint, but that is also a less representative
stand-in for a large live tuple stream.
