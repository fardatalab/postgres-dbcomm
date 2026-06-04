# DPA Async Ops

## Scope

- **What this doc explains**: What async ops are, host/device responsibilities, and exactly how DPA kernels obtain the async-ops handle used by `doca_dpa_dev_post_memcpy(...)`.
- **What this doc does NOT cover**: End-to-end performance tuning for batching policy.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:1333` - `doca_dpa_async_ops_create(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1397` - `doca_dpa_async_ops_attach(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1414` - `doca_dpa_async_ops_start(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1442` - `doca_dpa_async_ops_get_dpa_handle(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1201` - `doca_dpa_completion_create(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1251` - `doca_dpa_completion_set_thread(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1281` - `doca_dpa_completion_start(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:82` - `doca_dpa_dev_async_ops_t` handle type.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:380` - `doca_dpa_dev_get_completion(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:538` - `doca_dpa_dev_completion_ack(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:550` - `doca_dpa_dev_completion_request_notification(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:224` - `doca_dpa_dev_thread_reschedule(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:237` - `doca_dpa_dev_thread_finish(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:235` - `doca_dpa_dev_post_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:926` - `doca_dpa_thread_set_func_arg(...)` for passing args into thread kernel.
- `/opt/mellanox/doca/include/doca_dpa.h:959` - `doca_dpa_thread_set_local_storage(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:198` - `doca_dpa_dev_thread_get_local_storage(...)`.

## Host/device split

- Host side:
  - Owns lifecycle of async ops context object.
  - Retrieves DPA handle.
  - Passes handle to kernel (arg/TLS/config struct in DPA heap).
- Device side:
  - Receives opaque handle value.
  - Calls `doca_dpa_dev_post_*` APIs with that handle.

## Canonical handle-passing patterns

- **Pattern 1: kernel arg**
  - Host packs async handle and mmap handles in a config struct in DPA heap.
  - Host passes config pointer as `uint64_t arg` via `doca_dpa_thread_set_func_arg`.
  - Kernel casts arg to config pointer and posts async operations.
- **Pattern 2: thread local storage**
  - Host writes config pointer via `doca_dpa_thread_set_local_storage`.
  - Kernel reads pointer with `doca_dpa_dev_thread_get_local_storage`.

## Completion and continuation model

- **Setup on host (required for completion-driven continuation)**:
  1. Create completion context with queue size: `doca_dpa_completion_create(...)`.
  2. Attach DPA thread to completion context: `doca_dpa_completion_set_thread(...)`.
  3. Start completion context: `doca_dpa_completion_start(...)`.
  4. Attach async ops to this completion context: `doca_dpa_async_ops_attach(...)`.
  5. Start async ops and pass async handle to kernel.
- **Continuation pattern A - poll in same activation**:
  - Kernel posts async op.
  - Kernel polls with `doca_dpa_dev_get_completion(...)` until completion arrives.
  - Kernel consumes metadata (type/user-data/etc), then calls `doca_dpa_dev_completion_ack(...)`.
  - Kernel continues immediately.
- **Continuation pattern B - event-driven reactivation**:
  - Kernel posts async op.
  - Kernel requests future notification with `doca_dpa_dev_completion_request_notification(...)`.
  - Kernel yields/releases EU via `doca_dpa_dev_thread_reschedule(...)`.
  - Runtime re-triggers the attached thread when completion arrives.
  - Re-entered kernel drains completion(s), `ack`s, and advances state machine.
- **State persistence requirement**:
  - Cross-activation state should live in heap/TLS-backed structures, not stack.
  - Stack persistence is not guaranteed between handler executions (`/home/jasonhu/BenchBF3/DPA-Development.md:375`).

## Invariants and assumptions

- There is no separate "mmap-style export descriptor" API for async ops.
- `get_dpa_handle` is host->device handle extraction within the same app flow, not cross-process export.

## Gotchas

- `doca_dpa_dev_post_memcpy` / `doca_dpa_dev_post_buf_memcpy` notes state they are relevant for DPA-thread style kernels (`doca_dpa_dev_buf.h:199`, `doca_dpa_dev_buf.h:222`).
- If `doca_dpa_dev_completion_request_notification(...)` is skipped, new completions are not notified/populated on that completion context (`doca_dpa_dev.h:544`).
- Completion queue entries must be acked (`doca_dpa_dev_completion_ack(...)`) to release resources and allow more completions.

## Related

- Mechanism map: `overview.md`
- External memory mapping handles: `window-and-external-ldst.md`
- Fence/ordering notes: `coherency-and-fencing.md`
- Thread activation lifecycle: `../programming-model/doca-thread-activation.md`
