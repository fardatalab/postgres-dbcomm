# Window And External LD/ST

## Scope

- **What this doc explains**: Direct DPA load/store to external registered memory via window/mmap handles.
- **What this doc does NOT cover**: NIC queue programming details.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:263` - `doca_dpa_dev_mmap_get_external_ptr(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:190` - `doca_dpa_dev_buf_get_external_ptr(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:177` - External pointer access has 64B alignment restriction.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:181` - Requires `__dpa_thread_window_writeback()` after stores.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:125` - FlexIO exposes two per-thread window entities (`ENTITY_0`, `ENTITY_1`).
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:392` - `flexio_dev_multi_window_config(...)` binds window ID/mkey to an entity.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:37` - `flexio_dev_window_config(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:40` - `flexio_dev_window_ptr_acquire(...)`.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:103` - Kernel obtains external pointer and accesses host memory.

## Data flow

1. Host registers memory and creates mapping/window object.
2. Host passes mapping metadata (`mmap handle` or `window id + mkey`) to kernel.
3. Kernel resolves external pointer.
4. Kernel reads/writes directly.
5. Kernel executes required cache-control primitives for visibility.

## Gotchas

- Access is not coherent by default; stale reads/writes are a real failure mode if writeback/invalidate is omitted.
- 64B alignment restriction applies to pointers from external pointer helper APIs.

## Related

- Memory taxonomy: `../memory-classes/external-registered-memory.md`
- Fence details: `coherency-and-fencing.md`
- Entity-slot behavior and lifecycle: `flexio-window-entities.md`
