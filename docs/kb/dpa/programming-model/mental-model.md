# DPA Programming Mental Model

## Scope

- **What this doc explains**: The host/device split, how kernels are launched, and how host-created handles become usable in DPA kernel code.
- **What this doc does NOT cover**: Detailed memory fence recipes per data path (see access mechanisms docs).
- **Primary directory**: `docs/kb/dpa/programming-model`

## Why this exists (context)

DPA APIs expose both host-facing control objects and device-facing execution APIs, and similar names can hide that split. The most common confusion is whether host APIs are callable on DPA and how DPA receives host-created resource handles.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:459` - `doca_dpa_kernel_launch_update_set(...)` (host-side launch with varargs).
- `/opt/mellanox/doca/include/doca_dpa.h:524` - `doca_dpa_rpc(...)` (host-side blocking RPC launch of `__dpa_rpc__` function).
- `/opt/mellanox/doca/include/doca_dpa.h:926` - `doca_dpa_thread_set_func_arg(...)` (host passes a `uint64_t` arg to DPA thread kernel).
- `/opt/mellanox/doca/include/doca_dpa.h:959` - `doca_dpa_thread_set_local_storage(...)` (host sets per-thread DPA pointer).
- `/opt/mellanox/doca/include/doca_dpa_dev.h:198` - `doca_dpa_dev_thread_get_local_storage()` (device retrieves TLS pointer).
- `/opt/mellanox/doca/include/doca_dpa.h:1442` - `doca_dpa_async_ops_get_dpa_handle(...)` (host obtains opaque device handle).
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:235` - `doca_dpa_dev_post_memcpy(...)` (device consumes async-ops handle).

## Architecture / data flow

- **Entry points**:
  - Host creates context/resources and launches kernels (`doca_dpa.h` host APIs).
  - Device executes kernel body and calls `doca_dpa_dev_*` APIs (`doca_dpa_dev*.h`).
- **Control flow**:
  1. Host creates resource object (`async ops`, `mmap`, `buf array`, `completion`, etc.).
  2. Host starts/attaches where needed.
  3. Host extracts DPA handle (`*_get_dpa_handle`).
  4. Host passes handle into DPA kernel (function arg, pointer to config struct, or TLS).
  5. Device kernel calls device-side API with that handle.
- **Data structures**:
  - Host object pointers: `struct doca_dpa_async_ops *`, `struct doca_mmap *`, `struct doca_buf_arr *`.
  - Device handle typedefs: `doca_dpa_dev_async_ops_t` (`doca_dpa_dev.h:82`), `doca_dpa_dev_mmap_t` (`doca_dpa_dev_buf.h:35`), `doca_dpa_dev_buf_arr_t` (`doca_buf_array.h:53`).
- **State / lifecycle**:
  - Handle retrieval usually requires object started/valid.
  - Device-side usage fails semantically if host object lifecycle is not established first.

## Behavior details

- **Happy path**:
  - Host `create -> attach -> start -> get_dpa_handle`.
  - Kernel receives handle via arg/TLS and posts device operation.
- **Error paths / edge cases**:
  - Calling `get_dpa_handle` before `start` can return bad state.
  - Passing stale handles after host object destroy is invalid.
  - `doca_dpa_dev_post_*` async memcpy APIs are documented for DPA-thread style kernels and not host kernel-launch style (`doca_dpa_dev_buf.h:199`, `doca_dpa_dev_buf.h:222`).
- **Concurrency / ordering constraints**:
  - DOCA DPA objects are documented as non-thread-safe in converted docs (`/home/jasonhu/BenchBF3/DOCA-DPA.md:383`).

## Invariants and assumptions

- `doca_dpa_*` functions in `doca_dpa.h` are host APIs.
- `doca_dpa_dev_*` functions in `doca_dpa_dev*.h` are device APIs.
- "Handle" means opaque ID in DPA address space, not raw host pointer.

## Configuration / flags

- Launch path choice:
  - Blocking RPC path: `doca_dpa_rpc(...)`.
  - Thread/kernel launch path: `doca_dpa_thread_set_func_arg(...)`, `doca_dpa_kernel_launch_update_set(...)`.

## Interactions and cross-links

- **Overlapping KB areas**:
  - `execution-models.md`
  - `../memory-classes/heap-memory.md`
  - `../access-mechanisms/dpa-async-ops.md`
  - `flexio-cmdq-vs-rpc.md`
  - `device-selection-and-netdev-mapping.md`
- **Shared code**:
  - Host-side resource passing patterns mirror how FlexIO wrapper passes config pointers to device benchmarks:
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:109`
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:97`

## Gotchas

- `get_dpa_handle` is not "cross-process export" in the same sense as `doca_mmap_export_*`; it is host-to-device handle extraction for use inside the same app flow.

## Open questions / TODO

- Confirm exact SDK-version behavior differences for device-side async APIs under kernel-launch path vs thread-run path if future code needs mixed modes.
