# Printtup DPA Standalone Reserializer

## Purpose

Record the first standalone DPA executable version of the `printtup` binary tuple reserializer. This is not wired into PostgreSQL. It is a FlexIO/DPA validation harness that reads an existing printtup binary dump on the DPU Arm cores, sends compact row tasks to DPA workers, and compares the complete `DataRow` body bytes after DPA execution.

## Current Files

- `fdl_utils/printtup_dump/dpa/printtup_dpa_common.h`: Shared host/device ABI for row tasks, field descriptors, and output/result slots.
- `fdl_utils/printtup_dump/dpa/printtup_dpa_device.c`: DPA-side serializer entry point `printtup_dpa_reserialize_row()`.
- `fdl_utils/printtup_dump/dpa/printtup_dpa_host.cpp`: DPU Arm-side parser, FlexIO process/CmdQ driver, output comparison, and CSV writer.
- `fdl_utils/printtup_dump/dpa/build_printtup_dpa.sh`: DPU build script using `/opt/mellanox/doca/tools/dpacc` for the device archive and `g++` for the Arm host harness.
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_sample_200.csv`: First benchmark CSV from `dumps/printtup_binary_dump_sample_200.bin`.

## Execution Shape

The host harness parses `HEAD`, `SCHM`, and `ROWD` records on the DPU Arm side. Schema parsing maps `sendname` to a compact serializer enum for the same narrow scope as the regular C reserializer: `int4send`, `date_send`, text-like varlena send functions, and finite `numeric_send`.

For each row, the host copies normalized field payloads and a `PrinttupDpaTask` into DPA heap with `flexio_copy_from_host()`. The task points each field at DPA heap input and points output at a registered Arm buffer exposed through a FlexIO window.

`printtup_dpa_reserialize_row()` builds a complete `DataRow` body in the output slot: attribute count, per-field length words, null markers, and field payload bytes. The host then compares that output body against the dumped `row_payload`.

## Timing

The harness records separate payload and serializer-work throughput columns:

- `host_payload_throughput_mb_s`: end-to-end host-observed throughput for final `DataRow` body bytes. This is the closer number for one-pass output production.
- `host_serializer_work_throughput_mb_s`: end-to-end host-observed throughput for repeated serializer work, counted as `payload_bytes * iterations`. This is useful for isolating serializer execution rate, but it is not the actual number of distinct output bytes emitted to the ARM-visible output buffer when `--iterations > 1`.
- `dpa_throughput_mb_s`: DPA-cycle-derived estimate using aggregate task cycles divided by worker count, assuming the 1.8 GHz DPA cycle rate used in the BenchBF3 DPA benchmark notes.

The `--iterations` option repeats serialization inside each DPA row task while writing only the final `DataRow` body. This means repeated-iteration runs do perform repeated serializer work, but they do not produce `iterations` distinct output buffers. The repeated work is counted in `serializer_work_bytes`, while `payload_bytes` stays tied to final output body bytes.

The `--rows-per-task` option controls how many logical rows one CmdQ work item serializes on a DPA worker. The default is `1`, preserving the original one-row-per-CmdQ-item shape. Larger values reduce Arm-side enqueue overhead by making the DPA worker loop over a row range locally. `--rows-per-task all` submits at most one row-range task per worker for each drained chunk, so it is the current closest standalone harness setting to "each DPA worker processes all rows assigned to it."

The first 64-worker smoke sweep on `100000` logical rows and `--iterations 1` did not show monotonic improvement from larger row ranges. `--rows-per-task 4` improved host-observed throughput, but larger ranges regressed sharply. This suggests there is a real tradeoff: fewer CmdQ items reduce Arm enqueue overhead, while larger DPA row loops reduce scheduling granularity and can lose latency hiding around host-window output writes. The comparison CSV is stored at `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_rows_per_task_compare.csv`.

Follow-up optimization removed host-window control calls from the per-row DPA loop in batched mode. The DPA batch entry point now caches the configured/acquired registered output base per DPA thread, serializes each row by offsetting into the contiguous output-slot array, and performs one window writeback after the whole row range. The Arm drain loop also defaults to busy polling; `--poll-us N` restores sleeping if the experiment should trade latency for lower Arm CPU burn.

After that hot-path cleanup, the same 64-worker, `100000` logical row, `--iterations 1` sweep still peaks at small row batches: `--rows-per-task 4` reached about `101 MB/s` host-observed throughput, while `64`, `256`, and `all` remained slower despite fewer CmdQ work items. The updated CSV is stored at `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_rows_per_task_hotpath_compare.csv`. The likely remaining bottleneck is not Arm enqueue count alone; large row ranges make each DPA work item responsible for a larger host-window dirty region and reduce CmdQ scheduling granularity.

`--no-window-writeback` disables the DPA window writeback at the end of each CmdQ work item. This is a timing-only mode: the ARM side skips output-byte verification and cannot compute DPA-cycle-derived throughput from the host result headers because those headers are intentionally not made coherent. Use a matching writeback-enabled run first for correctness, then use no-writeback runs to isolate serialization/submission timing without the explicit DPA-to-host publish cost.

The CSV includes host-side CmdQ timing breakdown columns: `enqueue_elapsed_us` sums time spent adding CmdQ tasks, `start_elapsed_us` sums `flexio_cmdq_state_running()` calls, and `drain_wait_us` is the wait from after start until the queue is empty. `drain_wait_us` includes DPA execution and any polling behavior, so it is not pure ARM overhead.

The first paired writeback-on/off run used 64 workers, `100000` logical rows, `--iterations 1`, and `--rows-per-task 4`. Disabling writeback improved host-observed throughput only slightly, from about `101.0 MB/s` to `102.0 MB/s`, so explicit window writeback is not the dominant overhead at that point. The no-writeback row-batch sweep still peaked at `--rows-per-task 4`; larger row ranges continued to regress. Result CSVs:

- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_writeback_on_timing.csv`
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_writeback_off_timing.csv`
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_rows_per_task_no_writeback_compare.csv`

For longer hot-path proxy runs, keep writeback enabled for now and use enough `--iterations` to run for at least a few seconds. A 64-worker, `100000` logical row, `--rows-per-task 4`, writeback-enabled sweep showed `host_serializer_work_throughput_mb_s` stabilizing near `250 MB/s` by `--iterations 50..200`; `--iterations 200` ran for about `3.95s`. This is a proxy for saturated serializer work, not distinct emitted payload bytes. The CSV is stored at `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_iteration_sweep_writeback_on.csv`.

`--ring-output-bytes N[k|m|g]` switches the output body writes from overwriting each row's stats/output slot to a moving ring region inside the same registered output MR. The per-row slot header remains as the status/cycle accounting record; body-byte verification is skipped in ring mode because the final body for a row is no longer stored at `header + 1`. The ring deliberately wraps without throttling CmdQ chunks, so small ring sizes measure repeated writes to a small moving region rather than adding extra host chunking/backpressure.

The first moving-ring sweep used 64 workers, `100000` logical rows, `--rows-per-task 4`, `--iterations 200`, writeback enabled, and ring sizes from `4 KiB` to `64 MiB`. Throughput was essentially flat at about `245 MB/s` host-observed serializer-work throughput across all tested ring sizes; no cache-size cliff appeared in this range. This may mean the current bottleneck is serializer/control overhead rather than output-region locality, or that DPA host-window stores do not expose the same L1/L2 locality behavior that a DPA-local ring would. Result CSV: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_ring_output_size_sweep.csv`.

`--ring-output-location host|dpa` controls where that moving ring body region lives. `host` keeps the ring inside the registered ARM host-window MR. `dpa` allocates only the moving ring body from FlexIO DPA heap with `flexio_buf_dev_alloc()`, while the per-row status/cycle headers remain in host-window memory. A first side-by-side sweep (`4 KiB`, `64 KiB`, `1 MiB`, `16 MiB`, `64 MiB`) again showed mostly flat throughput. The only visible outlier was a slower `4 KiB` DPA-heap ring (`~234 MB/s` vs `~245 MB/s`), while larger DPA-heap rings were slightly above the host-window ring (`~245.3..245.7 MB/s`). Result CSV: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_ring_output_location_sweep.csv`.

`--materialize-input-working-set` changes the input preparation model. The default still copies one DPA-resident normalized field payload per parsed dump row and points repeated prepared rows back to those shared input copies. The materialized mode duplicates non-null normalized field payloads for each prepared input row. `--working-set-rows` now controls the output/status slot reuse pool and CmdQ chunking, while `--input-working-set-rows` controls the prepared input task table. This lets host-observed throughput stay comparable while input locality changes.

When those counts differ, the DPA batch entry point maps `logical_index % prepared_rows` to choose the prepared input task and `logical_index % output_slot_rows` to choose the output/status slot. The result header keeps cumulative `cycles` and `bytes_written` across output-slot reuse but updates row metadata/output bytes for the latest row in that slot. This keeps DPA-cycle accounting meaningful after decoupling.

`--input-location dpa|host` controls where normalized field payload bytes live. `dpa` uses `flexio_copy_from_host()`/DPA heap as before. `host` appends the normalized payload working set to the same registered ARM host MR used for output/status slots and stores offsets in `PrinttupDpaField::normalized_daddr`; the DPA maps the existing output window once, then reads host input at `output_base + input_base_offset + normalized_daddr`. The host input region is padded by a 4 KiB guard tail because ending the registered MR exactly at the final varlena byte produced `BAD_INPUT` on the final prepared rows, consistent with DPA/window-side cache-line or window-granularity fetching near the MR boundary.

The cleaner input working-set comparison used 64 workers, `5000000` logical rows, `--working-set-rows 32768`, `--rows-per-task 4`, `--iterations 1`, and writeback enabled. `--iterations 1` is deliberate: repeating one row inside a DPA task makes that row's input hot and hides cross-row locality. The longer logical-row stream gives a multi-second host-observed run without row-local repetition. Because `--working-set-rows` is fixed, every row has the same `cmdq_chunks=153`, so `host_serializer_work_throughput_mb_s` is now comparable across input working-set sizes:

- Shared input mode keeps normalized field payloads at `6853` bytes and isolates the prepared task-table footprint. Host-observed throughput declines from about `120 MB/s` at 200 input rows to about `107 MB/s` once the prepared task table reaches `32768+` rows. DPA-cycle throughput declines from about `917 MB/s` to about `821 MB/s`.
- Materialized input mode scales normalized field payloads from `6853` bytes to about `8.98 MiB`. Host-observed throughput declines from about `120 MB/s` to about `95 MB/s`; DPA-cycle throughput declines from about `917 MB/s` to about `588 MB/s`. Most of the additional normalized-input locality penalty appears by roughly `1.12 MiB` to `2.25 MiB`, then plateaus.

Result CSVs:

- Coupled/older sweep: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_input_working_set_compare.csv`
- Decoupled/current sweep: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_input_working_set_decoupled_compare.csv`

The first input-location comparison used the decoupled shape with 64 workers, `5000000` logical rows, `--working-set-rows 32768`, `--rows-per-task 4`, `--iterations 1`, and writeback enabled. With shared input at `--input-working-set-rows 32768`, host-observed throughput was effectively identical for DPA heap vs host-window input (`~107.7 MB/s`) because the actual normalized payload footprint was only `6853` bytes. With materialized input, host-window input was faster in this setup: at `32768` input rows (`~1.12 MiB` normalized input), DPA heap reached `~95.8 MB/s` while host-window input reached `~107.4 MB/s`; at `262144` input rows (`~8.98 MiB`), DPA heap reached `~95.0 MB/s` while host-window input reached `~106.7 MB/s`. This is an empirical result for the current tiny-row sample and should be retested with larger tuple payloads before treating host input as generally better.

Input-location comparison CSV: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_input_location_compare.csv`.

Task descriptor layout was then tightened. The earlier DPA hot loop loaded a task pointer from a DPA-side pointer table and each prepared task carried a fixed inline `PrinttupDpaField fields[64]` array. The current layout copies one contiguous compact task-header array and one contiguous field array containing only each row's actual `natts` entries; `PrinttupDpaTask::fields_daddr` points to that row's slice. This removes the task pointer-table load and greatly reduces task-descriptor footprint for narrow rows.

The compact descriptor layout helped, but only modestly for the tiny sample. With materialized DPA-heap input, host-observed throughput improved from about `95.8` to `97.5 MB/s` at `32768` input rows and from about `95.0` to `96.7 MB/s` at `262144` input rows. With materialized host-window input, it improved from about `107.4` to `109.1 MB/s` at `32768` input rows and from about `106.7` to `108.6 MB/s` at `262144` input rows. The result suggests task descriptor loads were real overhead but not the dominant remaining limiter.

Compact descriptor comparison CSV: `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_compact_task_input_location_compare.csv`.

The DPA byte writer was then tightened for the lineitem workload. `dpa_writer_append_u16be()` and `dpa_writer_append_u32be()` now reserve destination bytes and write network-order bytes directly, instead of building tiny stack buffers and routing through the generic append loop. `dpa_writer_append()` now copies raw payloads with fixed-size `__builtin_memcpy` 8/4/2-byte chunks plus a byte tail. This matters because text-like payloads commonly begin after a 1-byte short-varlena header and DataRow bodies have mixed 2-byte and 4-byte prefixes; `__builtin_memcpy` keeps the C expression valid for unaligned byte addresses without raw typed pointer casts, while letting the DPA compiler choose the final lowering. On the 5M-row, 100K-working-set, materialized host-input lineitem run with `--rows-per-task 4`, the change improved the 64-worker host-observed payload result from `298.589 MB/s` to `317.889 MB/s`. The 256-worker result was effectively flat (`390.690` to `393.142 MB/s`), and the 512-worker result was within noise (`408.021` to `406.766 MB/s`). This suggests the byte-copy/fixed-append work is a real low-to-mid worker-count cost, but not the dominant limiter once the CmdQ/output path is already saturated. A later alignment-gated raw typed-copy experiment passed correctness but was not a clear win (`269.003`, `392.268`, `413.126 MB/s` at 64/256/512 workers), so the active code uses the builtin-copy chunk path.

Chunked byte-writer comparison CSVs:

- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_chunkcopy_smoke.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_chunkcopy_t64_256_512.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_alignedcopy_t64_256_512.csv`

Network-byte-order conversion was also tested with a compile-time-only macro,
`PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER`, passed through the build script as
`PRINTTUP_DPA_DEVICECC_OPTIONS=-DPRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER=1`. The
macro changes the fixed-width emit helpers to write DPA-native integer bytes
instead of PostgreSQL/libpq network byte order. This intentionally breaks
wire-byte correctness for both protocol fields and values, so the native-order
run reports body mismatches; timing still stops before ARM-side verification.
On the 5M-row, 100K-working-set, materialized host-input lineitem run with
`--rows-per-task 4`, skipping byte-order output was not materially faster:
64 workers changed from `318.737` to `318.480 MB/s`, 256 workers from
`392.803` to `395.976 MB/s`, and 512 workers from `407.873` to
`410.519 MB/s`. DPA-cycle-derived throughput was also effectively unchanged.
This suggests network-byte-order byte shuffling is not a major remaining
bottleneck for the current lineitem serializer.

Network-byte-order toggle CSVs:

- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_nbo_toggle_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_nbo_macro_t64_256_512.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_native_order_macro_t64_256_512.csv`

Follow-up bottleneck isolation added compile-time-only macros for three
non-wire-compatible cuts: skip text payload copy after varlena decode, skip
numeric parsing/output using the captured serialized field length, and skip all
per-type field serializers while preserving row/field framing length accounting.
The descriptor ABI now carries `PrinttupDpaField::serialized_len` for these
benchmark builds. With the old `--rows-per-task 4` shape, those cuts improved
DPA-cycle-derived work but did not improve host-observed throughput. At 512
workers, the full serializer measured about `33228` DPA cycles per row; fast
numeric measured about `27941`, skip text payload copy about `27403`, and fast
fields about `17940`. Host-observed throughput, however, stayed flat or
regressed because the run still used `1,250,000` CmdQ tasks and was dominated by
submission/drain granularity rather than the inner serializer body.

The decisive check was retuning row batching at 512 workers. With correct output
and the real serializer, `--rows-per-task 4` reached `408.542 MB/s`,
`8` reached `703.489 MB/s`, `16` reached `957.032 MB/s`, and `32` fell back to
`713.974 MB/s`. The same pattern appeared in the fast-fields build: `1` and `4`
were dominated by millions of CmdQ items, `16` peaked, and very large row ranges
regressed from coarse scheduling/load-balance. Disabling DPA window writeback did
not materially change the 512-worker normal run (`408.542` to `407.596 MB/s`) or
the fast-fields `rows_per_task=16` run (`910.551` to `911.895 MB/s`), so the
current bottleneck is CmdQ item scheduling/drain plus row-range granularity, not
network byte order and not explicit window writeback.

A deeper cut then skipped the whole DataRow field loop using captured
`PrinttupDpaTask::row_payload_len`. At 512 workers and `--rows-per-task 16`,
this measured only about `88` DPA cycles per row, compared with about `20407`
cycles per row for the `fast_fields` build at the same batch shape. That means
the fast-fields residual is overwhelmingly generic field-loop/framing work:
descriptor walk, per-field length word handling, writer reserve/checks, and
loop/branch overhead, not row/result accounting. The first concrete optimization
from that finding was to stop reserving zero and patching each non-null field
length. The standalone harness now writes the captured field payload length from
`PrinttupDpaField::serialized_len` up front and verifies that the serializer
advanced by exactly that length. This preserves byte correctness for the
standalone dump workload while removing the per-field patch stores. With correct
output, 512 workers and `--rows-per-task 16` improved from `957.032 MB/s` to
`1044.840 MB/s`; 256 workers at `--rows-per-task 16` improved from
`616.596 MB/s` to `684.917 MB/s`; and 512 workers at `--rows-per-task 4`
improved from `408.542 MB/s` to `423.832 MB/s`. A real integrated path can use
this shape only if it precomputes each attribute's serialized payload length;
otherwise it needs the older reserve/patch approach.

A schema-specialized lineitem plan was then tested. The ARM harness marks tasks
with `PRINTTUP_DPA_PLAN_TPCH_LINEITEM` when the captured serializer sequence
matches int4 x4, numeric x4, text-like x2, date x3, text-like x3. The DPA path
uses an unrolled 16-field serializer and bypasses generic serializer dispatch.
This was not a large win: the first version was effectively flat in
host-observed throughput and worse in DPA cycles, and a reduced-check version
reached `1058.280 MB/s` at 512 workers, only slightly above the `1044.840 MB/s`
length-upfront generic path. This suggests simple branch removal can be offset
by code size or remaining function-call costs on the DPA cores. A more robust
small win came from trusting captured row lengths for capacity: if
`PrinttupDpaTask::row_payload_len <= output_capacity`, the writer skips repeated
per-append capacity checks and relies on the final row-length assertion. With
the reduced-check lineitem plan plus trusted writer, the 512-worker,
`--rows-per-task 16` lineitem run reached `1071.740 MB/s` with correct output
and about `30328.5` DPA cycles per row.

The next attempt bypassed `DpaByteWriter` inside the lineitem-specialized path:
after one row-level capacity precheck, direct pointer/offset helpers write the
16 field length words and field payloads. This reduced DPA cycles to about
`29123.1` cycles per row at 512 workers and `--rows-per-task 16`, but the
host-observed result was `1067.500 MB/s`, effectively tied with the trusted
writer run and slightly lower than its best measured host throughput. The useful
takeaway is that writer reserve/check abstraction is a measurable DPA-cycle cost
but does not explain the whole generic bucket. Remaining cost is likely spread
across varlena decode, numeric header/digit handling, many small fixed-width
writes, instruction footprint, and DPA scheduling/load-balance effects. Retesting
batching with the direct plan kept the prior sweet spot: at 512 workers,
`rows_per_task=8/16/32/64` measured `749.384/1067.500/832.493/480.178 MB/s`.

The current best correct path is the compact inferred-layout lineitem plan. It
does not enlarge `PrinttupDpaField`; instead, the DPA infers text payload offset
and numeric digit count/offset from the existing `normalized_len` and
`serialized_len` fields. This skips repeated varlena decode and most numeric
layout rediscovery while still generating complete DataRow bodies from normalized
tuple bytes. At 512 workers and `--rows-per-task 16`, it reached
`1074.840 MB/s` and `28646.5` DPA cycles per row with `mismatches=0`. A larger
field-descriptor attempt was rejected: ABI-only descriptor growth measured
`1035.510 MB/s` and `31286.5` cycles/row, while actually using the predecoded
metadata measured `1042.100 MB/s` and `31102.9` cycles/row. The descriptor
footprint/load cost outweighed the decode saving.

The latest isolations split the remaining cycle bucket more cleanly. With the
compact inferred layout, skipping text payload copies or all numeric work each
landed near `23690` cycles/row; skipping both reached `20878.7` cycles/row. A
lineitem framing-only build that writes the 16 per-field length words and merely
advances payload lengths still measured `17715.8` cycles/row to the host output
window. Moving that framing-only output body to a DPA ring reduced it to
`9083.1` cycles/row, showing that many tiny host-window stores are expensive.
The full correct serializer did not benefit from DPA-ring output, and DPA-heap
input was worse (`39289.6` cycles/row), so host input plus host output remains
the best tested correct setup. A DPA-local scratch row followed by a final copy
was also rejected (`51837.8` cycles/row).

Because output shape is flexible for future RDMA-oriented experiments, one
payload-only build omitted the DataRow `natts` word and all 16 per-field length
words while still running the scalar/numeric/text payload serializers. It
intentionally mismatched normal DataRow verification, but measured `21846.3`
cycles/row and a `1185.590 MB/s` host-observed byte-accounting proxy. This
suggests exact PostgreSQL DataRow framing stores cost roughly `6.8K` cycles per
lineitem row; carrying lengths out of band or using a less interleaved output
format could be materially faster than exact DataRow bodies.

The next output-shape experiment made that less synthetic: the lineitem
specialized path can write a compact 32-byte metadata prefix plus a contiguous
payload stream into a DPA-private ring. The per-row metadata is intentionally
small for fixed-schema lineitem: `payload_len`, the three variable numeric send
lengths, and `l_comment` length. Fixed integer/date widths, fixed numeric
lengths, and fixed text lengths are implied by schema. In a 190/256/512 worker
sweep with `--rows-per-task 16`, exact DataRow output measured
`603.987/714.055/1078.530 MB/s`. Split DPA-ring output with per-row host result
headers still enabled measured `701.543/811.997/1130.530 MB/s` and reduced the
512-worker DPA cycles from `28609.7` to `23279.4` cycles/row. A timing-only
variant that suppresses per-row host result headers and disables window
writeback measured `721.698/832.205/1158.330 MB/s`; it is closer to the intended
DPA-private-ring hot path, but has no DPA-cycle counters because the ARM side
does not read row headers. Retesting `rows_per_task=8/16/32/64` at 512 workers
kept `16` best: `798.891/1158.240/1019.240/611.578 MB/s`.

The next scheduler experiment added a static chunked per-worker mode. It is not
a full persistent DOCA thread queue yet: the host still uses FlexIO CmdQ, but it
enqueues one RPC per logical worker instead of one RPC per row range. Each DPA
RPC processes `rows_per_task` adjacent rows, then jumps by
`worker_count * rows_per_task`, giving deterministic per-worker work ownership
without atomics. This dramatically reduced host-observed overhead. For split
DPA-ring output with no per-row host headers and no window writeback,
190/256/512 workers measured `2462.810/1846.490/2233.310 MB/s`; the byte-verified
exact DataRow path under the same scheduler measured
`1917.690/1453.270/1817.470 MB/s`. The 190-worker point is now best from the ARM
host's perspective, which fits the observed DPA EU availability better than the
older many-CmdQ-task harness where 512 logical workers hid scheduler overhead.
At 190 workers, static chunk sizes `1/4/16/64/256` all measured about
`2.43-2.47 GB/s`, so this experiment points at CmdQ enqueue/drain overhead as
the major removed cost rather than a narrow row-chunk-size sweet spot.

The static memory-path follow-up added explicit ring partitioning and reran the
key input/output memory choices under the static scheduler. `--ring-partition
per-worker` divides the moving ring slots by logical worker in
`printtup_dpa_reserialize_static_worker()`; this is an ownership cleanup rather
than a throughput win so far. Shared versus per-worker DPA rings measured
`2449.58/1848.82/2226.43 MB/s` and `2453.68/1855.48/2227.91 MB/s` at
190/256/512 workers. DPA-heap ring output is modestly better than host-window
ring output at the 190-worker point (`2454.19` versus `2358.27 MB/s`). Tuple
input location is more important: host-window input with a DPA-heap output ring
measured `2453.56 MB/s`, while DPA-heap input with the same output path measured
`1637.12 MB/s`. Setup-time caveat: the current DPA-heap input preparation path
does many small `flexio_copy_from_host()` calls before timing begins, but the hot
path result still argues against assuming DPA-heap tuple input is automatically
best.

That last statement was narrowed by a follow-up fix. The old DPA-heap input
preparation called `flexio_copy_from_host()` once per normalized field, creating
roughly 1.6M tiny DPA heap objects for the 100K-row lineitem materialized input.
The host harness now stages all normalized input bytes contiguously, copies the
blob to DPA heap once, and patches `PrinttupDpaField::normalized_daddr` offsets
into absolute DPA addresses before copying descriptors. With this packed DPA
input layout, the 190-worker DPA-input run improved from `1637.12 MB/s` to
`2398.61 MB/s`, close to the host-window input baseline of `2455.53 MB/s`.
Across 190/256/512 workers, packed DPA input measured
`2398.61/1708.40/2289.30 MB/s`; host-window input measured
`2455.53/1852.53/2226.72 MB/s`. The corrected conclusion is that scattered DPA
heap allocation layout was the main bug/artifact; DPA-heap input is competitive
when packed contiguously, though host-window input remains slightly better at
190/256 and packed DPA input is slightly better at 512 in this run.

The packed-input cache working-set sweep then tested whether smaller per-worker
memory footprints help. It used `printtup_dpa_reserialize_static_worker()` with
190 workers, 25M logical rows, `--rows-per-task 16`, materialized DPA-heap input,
per-worker DPA-heap output rings, and no window writeback. Varying the output
ring from `64 KiB` to `256 MiB` did not expose a cache cliff: throughput stayed
between `2320.58` and `2371.73 MB/s`, with `64 KiB`, `256 KiB`, `1 MiB`,
`4 MiB`, `16 MiB`, `64 MiB`, and `256 MiB` measuring
`2371.73/2353.62/2320.58/2336.58/2354.80/2328.20/2333.91 MB/s`. The output
ring is selected in `dpa_reserialize_one_row_mapped()` using `logical_index %
ring_slots`, and per-worker partitioning only constrains each worker's slot
range; it did not make small output regions materially faster.

Varying packed tuple input was more surprising. With a fixed `64 MiB` DPA output
ring, input footprints from `1024` to `1000000` prepared rows (`0.15 MiB` to
`146.81 MiB`) measured
`2020.34/1964.34/2169.76/2297.55/2328.60/2355.76/2452.90/2446.58 MB/s` for
`1024/4096/16384/65536/100000/262144/524288/1000000` rows. Small packed input
was worse, not better. The likely reason is that the static chunk loop walks
`worker_count * static_chunk_rows` strides, while
`dpa_reserialize_logical_row()` maps every logical row to
`logical_index % prepared_rows`; if `prepared_rows` is very small, many DPA
workers repeatedly converge on the same descriptor and payload addresses. On
this hardware, spreading reads over a larger packed DPA blob appears to preserve
more memory-system parallelism than trying to force the tuple input into a very
small cache footprint.

Two follow-up input-side optimizations refined that conclusion. The
`--input-map worker-offset` experiment added a static-worker-only mapping where
each worker selects prepared rows by local row ordinal plus a hashed worker
offset, instead of `logical_index % prepared_rows`. The DPA implementation lives
in `fdl_utils/printtup_dump/dpa/printtup_dpa_device.c` inside
`dpa_worker_input_offset()` and the `PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET` branch
of `printtup_dpa_reserialize_static_worker()`. The Arm harness mirrors the
mapping in `prepared_index_for_logical()` so correctness checks and byte
accounting follow the actual row selected by DPA. A 100K-row exact run at 4096
prepared input rows passed with `mismatches=0`, but the timing sweep was negative:
worker-offset changed throughput by `-4.31%`, `-1.01%`, `-1.33%`, `-3.27%`, and
`-5.27%` at `1024`, `4096`, `16384`, `100000`, and `524288` prepared input rows.
This means simple phase-shifting does not solve the small-input slowdown.

The `PRINTTUP_DPA_BENCH_CONTIG_LINEITEM_INPUT=1` experiment added a
benchmark-only lineitem split-output path that assumes materialized packed input
and walks a single normalized-input cursor through the 16 fixed-schema fields.
That avoids loading each field's `normalized_daddr`, but it must reparse varlena
headers to recover text/numeric lengths that the normal descriptor path already
precomputes. The result was mixed: `1951.73 -> 2202.86 MB/s` (`+12.87%`) at
4096 prepared input rows, but `2328.53 -> 2278.34 MB/s` (`-2.16%`) at 100K rows
and `2438.97 -> 2409.55 MB/s` (`-1.21%`) at 524288 rows. The current conclusion
is that descriptor-pointer traffic matters in the tiny-input stress case, but the
precomputed `PrinttupDpaField` metadata remains better for representative larger
input footprints.

The next two experiments tested the stronger remaining leads and both were
positive. `--input-map worker-shard` partitions the prepared-row index space by
static DPA worker. Unlike `worker-offset`, it does not merely phase-shift a
shared global walk; worker `i` cycles only inside a contiguous shard computed by
`worker_shard_bounds()` in
`fdl_utils/printtup_dump/dpa/printtup_dpa_host.cpp` and
`dpa_worker_shard_bounds()` in
`fdl_utils/printtup_dump/dpa/printtup_dpa_device.c`. The host-side
`prepared_index_for_logical()` mirror keeps byte accounting and exact-output
verification aligned with the DPA-selected row. A 100K-row exact smoke at 4096
prepared input rows passed with `mismatches=0`. The 190-worker timing sweep
improved logical mapping by `+24.67%`, `+26.99%`, `+19.88%`, `+4.34%`, and
`+0.40%` at `1024`, `4096`, `16384`, `100000`, and `524288` prepared input
rows. This supports the refined conclusion that per-worker ownership, not simple
phase shifting, is what fixes the small-input slowdown.

`PRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT=1` is the generated/static
metadata path that still uses precomputed `PrinttupDpaField` lengths and
addresses. It avoids generic serializer dispatch and the fallback varlena decode
branches in the normal split serializer, while also avoiding the
contiguous-input experiment's need to reparse varlena headers for every field.
On logical mapping it improved throughput by `+2.22%`, `+3.22%`, `+2.29%`, and
`+2.07%` at `1024`, `4096`, `100000`, and `524288` prepared input rows. Combined
with `worker-shard`, it added another `+2.10%`, `+2.21%`, `+1.88%`, and
`+1.89%` over the worker-shard baseline. The best observed point in this sweep
was `2571.18 MB/s` at 1024 prepared input rows with both optimizations enabled.

The `1/3/4/5` follow-up sweep narrowed the remaining hot-path leads. The host
harness now reports one-time Arm/control setup separately from the timed DPA
hot path: setup covers normalized input preparation, the packed DPA-input copy,
task/field descriptor copies, and batch-config installation, while
`host_elapsed_us` remains the elapsed time from CmdQ start/submission to drain.
This keeps the main metric aligned with the eventual steady-state integration,
but also exposes an honest `end_to_end_serializer_work_throughput_mb_s` when the
prep work is not amortized. On the 25M-row runs, setup was about `145 ms` for
1024 materialized input rows and about `765-769 ms` for 100K materialized input
rows.

The best result from this sweep was the compiled-plan shape, not the larger
per-row task descriptor. `PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN=1` treats the
fixed lineitem tuple shape as already selected by the benchmark build and enters
the generated lineitem serializer directly, skipping the per-row `plan_id`
branch and schema check. Combined with field-metadata row lengths and a 16-byte
split prefix, it measured `2692.92 MB/s` at 190 workers / 1024 input rows and
`2592.04 MB/s` at 190 workers / 100K input rows. The same field-metadata,
16-byte-prefix path without the forced plan measured `2571.26 MB/s` and
`2488.60 MB/s`, so the compiled-plan assumption is worth about `4-5%` in this
generated lineitem benchmark.

The row-local split metadata fields in `PrinttupDpaTask` let the DPA
precomputed lineitem path write `payload_len` and selected variable lengths
without reading those values from `PrinttupDpaField`, but the variant using those
task fields was slower than the old field-metadata path. With a 16-byte split
prefix and no forced plan, the task-metadata variant measured `2553.59 MB/s` and
`2463.28 MB/s`. The practical conclusion is that moving tiny row-local lengths
into a bigger task descriptor is not worthwhile on this DPA path; the descriptor
footprint and task-field load dependencies matter more than the four
field-length loads saved at the end of the row.

Shrinking split row metadata from 32 bytes to 16 bytes was a small positive
change when paired with the field-metadata path. It keeps the same live fields
(`payload_len`, `extendedprice_len`, `discount_len`, `tax_len`, `comment_len`,
and flags) and removes only padding. The gain was small but consistent at
190 workers: `2567.66 -> 2571.26 MB/s` for 1024 input rows and
`2480.21 -> 2488.60 MB/s` for 100K input rows.

The worker-local ring cursor did not pay off. The static worker can carry a
per-worker cursor and pass an explicit ring output pointer to the row serializer,
avoiding the old per-row `logical_index * iterations % ring_slots` selection.
In practice it was slightly slower at 190 workers and only helped the tiny-input
256-worker case, while 256 workers remained much slower than 190 workers. That
means the ring-slot arithmetic is not the dominant bottleneck in the current
shape, or the extra cursor branch/pointer dependency cancels the saved modulo.
Raw CSVs are summarized in
`fdl_utils/printtup_dump/dpa/results/lineitem_100k_optimizations_1_3_4_5_t190_256_summary.csv`.

The current best timing-only build is:

```bash
PRINTTUP_DPA_DEVICECC_OPTIONS=-DPRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT=1,-DPRINTTUP_DPA_BENCH_NO_ROW_HEADERS=1,-DPRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT=1,-DPRINTTUP_DPA_BENCH_TASK_SPLIT_META=0,-DPRINTTUP_DPA_BENCH_SPLIT_META_SIZE=16,-DPRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN=1 \
  ./build_printtup_dpa.sh build_opt_precomp_fieldmeta16_forcedplan
```

The current best timing-only run is:

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

This is a hot-path timing command, not a byte-verifying command, because it uses
`PRINTTUP_DPA_BENCH_NO_ROW_HEADERS=1` and `--no-window-writeback`. The same
build with `--input-working-set-rows 100000` is the more conservative
large-input-footprint comparison and measured `2592.04 MB/s` at 190 workers.

Cycle-accounting caveat: the `worker-shard + precomputed` sweep was a
timing-only hot-path run built with `PRINTTUP_DPA_BENCH_NO_ROW_HEADERS=1` and run
with `--no-window-writeback`, so those CSV rows intentionally report
`dpa_cycles_sum=0`. The current cycle-percentage estimate therefore still comes
from the latest header/writeback-enabled isolation runs. In the best
cycle-accounted exact DataRow-body path, `inferred_layout` measured `28646.5`
cycles/row and the framing-only `fast_lineitem_fields` isolation measured
`17715.8` cycles/row, so generic non-payload-serializer work was about
`61.8%` of device cycles. With the output body moved to a DPA ring, the
framing-only isolation fell to `9083.1` cycles/row. Compared with the
cycle-accounted split-output DPA-ring path at `23279.4` cycles/row, that is about
`39.0%`; compared with the exact DataRow DPA-ring path at `29241.9` cycles/row,
it is about `31.1%`. Treat those as approximations because the newest best
timing-only path does not currently publish per-row cycle headers.

## Current Takeaways And Next Targets

Current takeaways from the static chunked results:

- Host-observed throughput is the primary metric for this stage. DPA-cycle
  estimates remain useful for isolating serializer body work, but the integration
  proxy is the elapsed time from Arm-side DPA submission to CmdQ drain.
- The old contiguous scheduler was dominated by control-path shape. In
  `fdl_utils/printtup_dump/dpa/printtup_dpa_host.cpp:1093`, contiguous mode still
  loops over row chunks, repeatedly calls `flexio_cmdq_task_add()`,
  `flexio_cmdq_state_running()`, and drains the queue. The static mode at
  `fdl_utils/printtup_dump/dpa/printtup_dpa_host.cpp:1055` submits only one CmdQ
  task per logical worker, so `rows_per_task` no longer reduces Arm enqueue/drain
  count. It only changes the DPA-local chunk size consumed by
  `printtup_dpa_reserialize_static_worker()` in
  `fdl_utils/printtup_dump/dpa/printtup_dpa_device.c:1271`.
- Once repeated CmdQ submission/drain is removed, `rows_per_task` is mostly flat.
  The 190-worker sweep from `1` to `256` rows per static chunk stayed around
  `2.43-2.47 GB/s`. That means the remaining hot cost is not the DPA outer
  chunk-loop overhead; it is the per-row serializer/output/input work plus the
  single long CmdQ execution.
- The best host-observed worker count moved from 512 in the old many-CmdQ-task
  harness to 190 in static mode. This is important: 512 logical workers was
  hiding or compensating for the control-path/scheduling shape, not proving that
  oversubscribing the DPA EUs is intrinsically better for this serializer. The
  190-worker result fits the observed available-EU count better.
- Split metadata/payload output is still worth keeping. It is not a huge win by
  itself under the old scheduler, but under static mode it raises the 190-worker
  timing-only path from the byte-verified exact DataRow result of
  `1917.690 MB/s` to `2462.810 MB/s`. The caveat remains that the split path is
  intentionally not PostgreSQL DataRow compatible.
- DPA atomics are still deliberately avoided. The static chunked mode is a
  deterministic ownership scheme: worker `i` starts at `i * rows_per_task` and
  advances by `worker_count * rows_per_task`. This approximates per-worker queues
  without relying on untested DPA synchronization primitives.
- The packed-input working-set sweep argues against treating "fits in cache" as
  automatically good for DPA. Very small output rings were not faster, and very
  small tuple-input footprints were slower. The likely issue is address
  convergence: `dpa_reserialize_logical_row()` maps logical rows through
  `logical_index % prepared_rows`, so small `prepared_rows` makes many static
  workers revisit the same descriptor/payload addresses and may reduce memory
  system parallelism.
- Follow-up experiments narrowed the input-side lead. Worker-offset row mapping
  was correct but slower, so simple phase shifting is not enough. A contiguous
  input cursor helped only at the 4096-row stress point and regressed at 100K+
  rows, so reparsing varlena headers is more expensive than using precomputed
  field metadata once the input footprint is representative.
- Per-worker prepared-row ownership is now the strongest positive result after
  static scheduling. `worker-shard` makes small input footprints competitive
  without increasing total prepared input rows, and the generated/precomputed
  lineitem path gives a smaller but consistent additional gain.

Next optimization targets:

- Keep FlexIO static workers as the baseline rather than switching APIs by
  default. The current static mode already removes the repeated CmdQ control-path
  cost, and FlexIO exposes the same high-level EU/concurrency constraints as the
  DOCA thread model. A DOCA `doca_dpa_thread` port is only clearly motivated if an
  experiment needs DOCA-thread-only features, especially device-posted async
  memory copy (`doca_dpa_dev_post_memcpy()` / `doca_dpa_dev_post_buf_memcpy()`).
  Otherwise the next scheduler work should stay in the FlexIO harness and improve
  ownership, output partitioning, and memory access patterns.
- Keep the per-worker DPA-private output ring, but do not spend the next effort
  on tuning only its capacity. The implemented per-worker partitioning removes
  accidental cross-worker write collisions in timing-only long runs, but the
  packed-input cache sweep showed no meaningful throughput cliff from `64 KiB`
  through `256 MiB` total ring size. This makes output-region cache fitting a
  weaker lead than input access and per-row metadata work.
- Add a host-side verifier for the split metadata/payload shape when output is in
  a host-visible ring. Today the split path uses the same payload serializer
  helpers as the byte-verified exact path, but DPA-private-ring timing runs skip
  byte verification. A host-ring split verifier would catch layout bugs before
  trusting DPA-private timings.
- Treat `worker-shard` plus the generated/precomputed lineitem split path as the
  current best timing-only hot path. Remaining input-layout work should focus on
  whether this per-worker ownership model can be represented naturally in a real
  integration, for example by assigning each DPA worker a stable input/RDMA ring
  region. Further generated-code work should keep precomputed varlena lengths;
  the cursor-only variant showed that reparsing lengths is the wrong tradeoff.
- Evaluate memory access/copy mechanisms explicitly. The current code uses FlexIO
  host-blocking copies for setup (`flexio_copy_from_host()`), DPA direct
  load/store for serialization, optional FlexIO window direct loads for host-side
  input, and direct stores into either host-window slots or DPA-heap rings. The
  next useful experiments are: compare DPA-heap input vs host-window input under
  the static scheduler; compare DPA-heap ring vs host-window ring again under the
  static scheduler; test FlexIO device-side `flexio_dev_window_copy_to_host()` for
  coarse row/ring-copy publication; and only then consider DOCA async-copy APIs if
  we need a device-initiated copy engine rather than DPA-core load/store loops.
- Reduce per-row descriptor and field metadata loads further. The compact
  descriptor layout helped only modestly, and descriptor growth was rejected
  because it hurt DPA cycles. Any next attempt should prefer schema-constant
  metadata in code or a small shared plan structure, not larger per-row field
  descriptors.
- Continue optimizing for simple DPA-core execution: predictable loops, few
  branches, compact descriptors, mostly sequential memory access, and fewer tiny
  host-window writes. Earlier tests showed byte-order conversion was not a major
  cost, explicit window writeback was not the dominant cost, and `__builtin_memcpy`
  chunk copying helped only modestly. The remaining wins are more likely from
  control-path removal, output layout, work ownership, and reducing per-row
  metadata/framing traffic than from micro-optimizing endian stores.

Bottleneck isolation CSVs:

- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_bottleneck_isolation_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_rows_per_task_t512_followup_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_rpt16_thread_followup_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_fast_row_and_len_upfront_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_schema_plan_and_trusted_writer_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_direct_lineitem_rows_per_task_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_inferred_layout_and_isolation_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_split_output_worker_scaling_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_split_output_rows_per_task_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_static_chunked_schedule_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_static_chunked_chunk_size_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_static_mempath_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_static_packed_input_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_cache_working_set_sweep_t190_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_next_optimizations_t190_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_first_two_optimizations_t190_summary.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_iso_normal_nowb_t512.csv`
- `fdl_utils/printtup_dump/dpa/results/lineitem_100k_iso_fast_fields_rpt16_nowb_t512.csv`

## First Result

The first validated run used `dumps/printtup_binary_dump_sample_200.bin` with `--iterations 10000` and worker counts `1,2,4,8,16,32,64`. All rows matched for every worker count.

The CSV result is stored at `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_sample_200.csv`.

## Batch-Size Observation

After fixing small-batch correctness by reusing one CmdQ and submitting/draining row chunks, `--batch-size 1` was not slower than the default one-chunk run for the 200-row sample. It was close at low worker counts and faster at higher worker counts in the host-observed metric. The DPA-cycle-derived serializer throughput stayed essentially unchanged.

Current conjecture: `batch_size=1` gives the FlexIO runtime finer-grained work distribution. The serializer tasks are not perfectly uniform because rows have different payload sizes and numeric/text layouts, so large per-worker batches can create load imbalance even though they reduce host chunking. With `batch_size=1`, workers consume smaller units, which can improve tail behavior and therefore improve the host-observed drain time. This is a scheduling/load-balance effect, not evidence that the serializer itself got faster.

This observation is currently based on the small repeated sample, so it should be retested with larger logical row counts before becoming a default tuning rule.

## Larger Logical Row Sweeps

The larger-row tests used the same 200 parsed dump rows as a repeated logical workload:

- `100000` logical rows with `--iterations 200`
- `1000000` logical rows with `--iterations 20`
- `5000000` logical rows with `--iterations 1`

All tested worker counts (`1,2,4,8,16,32,64`) completed with zero mismatches. Result CSVs:

- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_logical_100k.csv`
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_logical_1m.csv`
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_logical_5m.csv`
- `fdl_utils/printtup_dump/dpa/results/printtup_dpa_results_logical_rowcount_compare.csv`

The 5M-row run shows a clear split between DPA serializer throughput and host-observed throughput: DPA-cycle-derived throughput continues scaling with workers, while host-observed throughput is much lower because the run has many CmdQ chunks and only one serializer iteration per task. In other words, the numerator is only one serialization pass per row, while the denominator includes per-row host enqueue work, repeated `flexio_cmdq_state_running()`/drain cycles, and the current 1 ms drain polling cadence for every chunk.

## Caveats

- CmdQ `batch_size` controls how many tasks each worker can process in one CmdQ invocation. A too-small batch size produced unprocessed output slots during bringup, which looked like mismatches but was not a serializer bug. The harness now treats `batch_size` as chunk capacity: one CmdQ is created per benchmark run, then the host submits at most `workers * batch_size` row tasks per chunk and waits for the queue to drain before submitting the next chunk. Small batch sizes such as `--batch-size 1` therefore loop over multiple chunks without repeatedly creating the CmdQ.
- Larger logical row-count sweeps can use `--logical-rows` without materializing multi-GB dump files. The harness repeats the parsed dump rows as logical work and uses a bounded `--working-set-rows` pool of output/status slots. By default that bound is `min(logical_rows, 32768)`, unless `--working-set-rows` is provided.
- `--input-working-set-rows` controls the prepared input task table size. If omitted, it defaults to `--working-set-rows` for historical behavior.
- `--materialize-input-working-set` makes `--input-working-set-rows` scale the normalized tuple-input footprint. Without it, repeated prepared rows share the same DPA-resident normalized field payloads from the parsed dump rows, so input payload locality is almost constant even if the prepared input task table grows.
- `--input-location host` keeps task descriptors in DPA heap but places normalized field payload bytes in the registered ARM host window. It intentionally uses the same MR/window as output/status slots to avoid hot-path FlexIO window reconfiguration.
- FlexIO CmdQ creation failed when `workers * batch_size` reached `32768` tasks (`Requested QP SQ depth 2^16 is larger than device limit (2^15)`). This is a per-CmdQ-submission capacity issue, not a total-work limit. The harness now caps default batch sizing and clamps oversized explicit settings so each chunk stays at `workers * batch_size <= 16384`, then loops over as many chunks as needed.
- The current memory model intentionally uses DPA heap input plus a registered host/window output buffer. Batched mode assumes the output slots are contiguous inside one registered output MR and uses one window pointer acquisition for the base address per CmdQ work item. This is still a standalone correctness/throughput harness, not the final zero-copy integration design.
- The serializer intentionally keeps the current standalone assumptions: same client/server encoding for text-like values, finite numeric values, and normalized varlena input from the dump rather than arbitrary raw backend `Datum` pointers.

## Related

- `../../../future-directions/postgres/serialization/dpa_printtup_serializer_offload.md`
- `../../../postgres/serialization/printtup_row_serialization_and_transport.md`
- `../../../dpa/programming-model/flexio-cmdq-vs-rpc.md`
