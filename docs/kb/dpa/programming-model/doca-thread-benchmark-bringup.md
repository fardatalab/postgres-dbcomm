# DOCA Thread Benchmark Bring-Up Notes

## Scope

- **What this doc explains**: What broke while bringing up the `doca_dpa_thread` benchmark, what was fixed, and what still does not work reliably.
- **What this doc does NOT cover**: Final benchmark conclusions. This is a bring-up/debugging note.
- **Primary directory**: `docs/kb/dpa/programming-model`

## Benchmark entry points

- Host control path lives in [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:866).
- Host thread/object setup lives in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:566).
- Device-side worker entry point is [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:181).
- The host-side wakeup RPC is [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:284), invoked from [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:900).

## Issues that were identified and fixed

### 1. Wrong DPACC build flow for DOCA device code

- The first version used the local FlexIO-oriented builder. That was the wrong toolchain path for DOCA DPA device code.
- The fix was to switch [`build_doca_thread_memory.sh`](/home/jasonhu/BenchBF3/scripts/build_doca_thread_memory.sh:17) to the official DOCA sample DPACC flow instead of the local helper.
- This resolved the device-link/build failures against DOCA DPA symbols.

### 2. Notification RPC ran, but notified DPA threads did not activate

- The device RPC [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:284) calls [`doca_dpa_dev_thread_notify()`](/opt/mellanox/doca/include/doca_dpa_dev.h:253) for each notification handle at [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:291).
- In practice, that RPC returned, but the target thread entry point [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:181) did not run.
- The fix was enabling `Generating_Empty_CQE: true` in [`thread_memory_attributes.yaml`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_attributes.yaml:7).
- This matches the official DOCA sample manifest pattern, for example [`dpa_nvqual_attributes.yaml`](/opt/mellanox/doca/samples/doca_dpa/dpa_nvqual/device/dpa_nvqual_attributes.yaml:25).

### 3. TLS payload and notification handle were visible too late

- The benchmark now copies the finalized TLS block with [`doca_dpa_h2d_memcpy()`](/opt/mellanox/doca/include/doca_dpa.h:601) before calling [`doca_dpa_thread_run()`](/opt/mellanox/doca/include/doca_dpa.h:1060) in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:703) and [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:719).
- The original helper-based path is intentionally left commented out at [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:648).
- The reason for the change is stated in the comment at [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:698): the thread must not become runnable before the benchmark-specific state is copied into DPA heap.

### 4. Host/window mmaps needed RDMA export permissions

- The benchmark reuses the shared DOCA sample helper path in [`doca_mmap_obj_init()`](/opt/mellanox/doca/samples/doca_dpa/dpa_common.c:1197).
- That helper calls [`doca_mmap_export_rdma()`](/opt/mellanox/doca/samples/doca_dpa/dpa_common.c:1267), so local-only mmap permissions were not sufficient.
- The host memory setup in [`thread_memory_prepare_memory`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:428) therefore had to follow the sample helper’s expectations rather than using a purely local mmap model.

### 5. Host result polling initially observed stale result slots

- Device code writes benchmark results into DPA heap in [`thread_memory_finalize`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:142).
- The host polls those slots with [`doca_dpa_d2h_memcpy()`](/opt/mellanox/doca/include/doca_dpa.h:644) inside [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:828).
- The fix was to explicitly call `__dpa_thread_memory_writeback()` in [`thread_memory_finalize`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:160) before signaling completion.

### 6. Host polling buffer alignment needed cleanup

- The host result buffer is now allocated with [`thread_memory_aligned_zalloc`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:343) and used from [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:579).
- This was a bring-up cleanup to avoid unnecessary alignment-related warnings/noise while polling the result array back from the DPA.

## Issues that are still open

### 1. CPU-side sync-event completion detection is still not working reliably

- The benchmark creates a DPA-published / CPU-subscribed completion event using [`create_doca_dpa_completion_sync_event()`](/opt/mellanox/doca/samples/doca_dpa/dpa_common.c:430) from [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:885).
- Device code increments that event with [`doca_dpa_dev_sync_event_update_add()`](/opt/mellanox/doca/include/doca_dpa_dev_sync_event.h:54) in [`thread_memory_finalize`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:170).
- However, the current benchmark no longer trusts sync-event completion as the primary completion detector. Instead it polls the DPA result array in [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:828).
- This means the sync-event object is still created and updated, but benchmark completion is currently declared only after result-slot polling succeeds.
- This needs a future return pass: verify whether the sync-event itself is misconfigured, whether publication is racing teardown, or whether the host wait path is wrong for this runtime.

### 2. Async dispatch can report host-side RPC polling failure even when device work finishes

- The wakeup RPC is issued with [`doca_dpa_rpc()`](/opt/mellanox/doca/include/doca_dpa.h:524) in [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:900).
- For async-copy modes, the host can receive a failure in the branch at [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:905), even though device logs show that [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:181) ran and [`thread_memory_finalize`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:142) published results.
- Current workaround: for async ops only, keep the warning loud, but continue into result polling from [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:925).

### 3. All 4 async-copy modes currently fail validation

- Validation runs in [`thread_memory_validate`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:726).
- Async posting happens in [`thread_memory_post_async_copy`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:89).
- Completion handling happens in the `THREAD_MEMORY_STAGE_WAIT_COMPLETION` branch of [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:236), using [`doca_dpa_dev_get_completion()`](/opt/mellanox/doca/include/doca_dpa_dev.h:380) and [`doca_dpa_dev_completion_ack()`](/opt/mellanox/doca/include/doca_dpa_dev.h:538).
- Even after completions are observed, the copied contents do not currently validate for:
  - `async_mmap_h2d`
  - `async_mmap_d2h`
  - `async_buf_h2d`
  - `async_buf_d2h`
- This currently looks more like an addressing / mapping / runtime-behavior problem than a simple completion-visibility problem.

### 4. Multi-thread external direct-read hangs beyond 2 benchmark threads

- The benchmark’s `--threads` parameter controls the number of DPA threads created in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:566), not host-side concurrency.
- The host creates and starts those thread objects sequentially in the loop at [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:606), [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:652), and [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:721).
- So the known “host DOCA objects are not generally safe for concurrent host use” rule does not explain the current hang: the host is not concurrently calling into the same context from multiple CPU threads here.
- The observed issue is runtime-side: for `ext_read`, `1` and `2` threads completed, but beyond that the host poll loop in [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:828) timed out because not all DPA result slots reached the expected `completed_ops`.

#### Current narrowed-down evidence

- The benchmark now writes a coarse per-thread progress marker into [`thread_memory_result.progress_marker`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/common/thread_memory_common.h:48), and the host timeout snapshot dumps it in [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:1042).
- Direct external read publishes those markers from [`thread_memory_run_ext_read`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:32):
  - `1` = entered external-read path
  - `2` = completed first scan
  - `3` = completed all scans, not yet finalized
  - `4` = finalized
- Adding `__dpa_thread_window_read_inv()` in [`thread_memory_run_ext_read`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:57) improved the `2`-thread repeated-scan case (`region_bytes=1 MiB`, `total_bytes=64 MiB`) enough to produce at least one successful run on `mel-18-dpu`, but it is still not reliable.
- Default placement is flaky for the `2`-thread repeated-scan case. Enabling fixed affinity in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:719) with `THREAD_MEMORY_FORCE_AFFINITY=1` produced one successful run by pinning the two benchmark threads to separated EUs via [`thread_memory_pick_eu_id`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:386), but three immediate follow-up reruns still timed out at [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:1042).
- However, that did **not** fix `3+` threads. With the same repeated-scan case, the stuck threads still time out at `progress=2`, meaning they finish the first full scan and then stall before later passes.
- Splitting the host side into per-thread mmaps alone did not fix the issue. The benchmark now gives each thread its own host mmap handle in [`thread_memory_prepare_memory`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:538) and passes it through [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:770), but the `4`-thread `ext_read` case still fails.
- Splitting the host side further into fully separate per-thread host allocations also did not fix the issue. The direct path now uses [`thread_memory_host_ptr`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:377) and allocates each thread’s host memory separately in [`thread_memory_prepare_memory`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:497), yet the `4`-thread `ext_read` case still times out.
- Large single-scan reads can also fail. With `region_bytes=64 MiB` and `total_bytes=64 MiB`, the `2`-thread and `4`-thread `ext_read` cases both timed out, so this is not only a repeated-scan-loop bug.

#### Current working / failing matrix on `mel-18-dpu`

- **Works**:
  - one observed run of `ext_read`, `threads=2`, `region_bytes=1 MiB`, `total_bytes=64 MiB`, with `THREAD_MEMORY_FORCE_AFFINITY=1`
- **Fails**:
  - `ext_read`, `threads=2`, `region_bytes=1 MiB`, `total_bytes=64 MiB`, with default placement (flaky timeout at `progress=2` on one thread)
  - `ext_read`, `threads=2`, `region_bytes=1 MiB`, `total_bytes=64 MiB`, with `THREAD_MEMORY_FORCE_AFFINITY=1`, on repeated reruns after the single observed success
  - `ext_read`, `threads>=3`, `region_bytes=1 MiB`, `total_bytes=64 MiB`
  - `ext_read`, `threads>=2`, `region_bytes=64 MiB`, `total_bytes=64 MiB`

#### Current conclusion

- The remaining issue is not explained by:
  - shared host mmap handle ownership,
  - shared host backing allocation,
  - host-side concurrent DOCA API use,
  - or PF-context / 64B-alignment violations.
- The strongest current conclusion is that concurrent direct external reads through [`doca_dpa_dev_mmap_get_external_ptr()`](/opt/mellanox/doca/include/doca_dpa_dev_buf.h:263) are unstable on this runtime, and the instability depends on both thread count and read region size.

### 5. Device selection is still environment-sensitive

- The benchmark currently reuses shared DOCA sample resource code, and that path expects a PF DPA device plus a distinct RDMA-side device when extendable contexts are used.
- Local device-name adjustment is done in [`thread_memory_fixup_device_names`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:218), but that helper currently only auto-swaps `mlx5_0` and `mlx5_1`.
- On `mel-18-dpu`, the empirically working pair was different, so this remains an environment-specific bring-up caveat rather than a solved problem.

## Current completion model in the benchmark

- **Intended signal path**:
  1. Host creates DPA threads in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:566).
  2. Host wakes them through RPC in [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:900).
  3. Each device thread signals the host completion event in [`thread_memory_finalize`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:170).
  4. Host should detect completion from the sync event.
- **Current actual path**:
  1. Steps 1-3 still happen.
  2. Host declares completion only after result-slot polling succeeds in [`thread_memory_wait_for_results`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:828).

## Related

- [execution-models.md](execution-models.md)
- [roundtrip-workflows.md](roundtrip-workflows.md)
- [../access-mechanisms/dpa-async-ops.md](../access-mechanisms/dpa-async-ops.md)
- [../access-mechanisms/coherency-and-fencing.md](../access-mechanisms/coherency-and-fencing.md)
