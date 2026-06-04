# FlexIO Window Entities

## Scope

- **What this doc explains**: What `flexio_window_entity` means, how it relates to host-created `flexio_window` objects, lifecycle, and practical usage patterns.
- **What this doc does NOT cover**: DOCA DPA `doca_mmap` object lifecycle details.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:125` - `enum flexio_window_entity` defines `FLEXIO_DEV_WINDOW_ENTITY_0`, `FLEXIO_DEV_WINDOW_ENTITY_1`, and `..._NUM = 2`.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:380` - Window config APIs are documented as updating the **thread window object**.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:392` - `flexio_dev_multi_window_config(...)` binds a window ID + mkey to a selected entity.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:452` - `flexio_dev_multi_window_ptr_acquire(...)` resolves host address into DPA pointer for a selected entity.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1430` - `flexio_window_create(...)` creates host-side window objects (hostful only).
- `/opt/mellanox/flexio/include/libflexio/flexio.h:2164` - `flexio_window_get_id(...)` provides the window ID passed into device code.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:123` - Host creates a window per process in current wrapper path.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:41` - Device currently binds `FLEXIO_DEV_WINDOW_ENTITY_0`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:47` - Device acquires external pointer from entity `0`.

## Mental model

- A **window object** (`struct flexio_window`) is created on the host and has an ID.
- A **window entity** is a per-thread binding slot in DPA execution context.
- In modern signatures, device code explicitly selects the slot (`ENTITY_0` or `ENTITY_1`) instead of passing `dtctx` for these APIs.
- So, `ENTITY_0` is not “window #0 in the process”; it is “binding slot 0 in the current DPA thread context.”

## How many windows exist?

- Host-side window objects: potentially many per process (resource-limited; created by repeated `flexio_window_create(...)`).
- Per DPA thread active bindings: exactly two slots (`FLEXIO_DEV_WINDOW_ENTITY_NUM = 2`) exposed by current device API.
- Practical implication: you may create multiple windows on host, but in one thread’s hot path you can keep at most two simultaneously bound without reconfiguration.

## Lifecycle and where creation happens

1. Host creates FlexIO process (`flexio_process_create`).
2. Host allocates/registers memory (PD + MR/mkey path).
3. Host creates one or more windows (`flexio_window_create`) and obtains IDs (`flexio_window_get_id`).
4. Host passes `window_id`, `mkey`, and host address metadata to DPA kernel/thread function.
5. Device code binds a chosen entity via `flexio_dev_window_config(entity, window_id, mkey)`.
6. Device resolves pointer/copies via `flexio_dev_window_ptr_acquire` / `flexio_dev_window_copy_*`.

Creation is on host (DPU SoC side in hostful mode), not on DPA cores.

## When to use `ENTITY_0` vs `ENTITY_1`

- Use only `ENTITY_0` when one external mapping is enough (current repo behavior).
- Use both entities when a thread needs quick switching between two external mappings (e.g., source/destination in a copy pipeline) and you want to avoid rebinding one slot repeatedly.
- Rebinding is legal but adds control overhead and may complicate correctness if not done consistently before pointer use.

## Gotchas

- Window-based external memory is non-coherent from DPA perspective; pairing with proper writeback/invalidate/fence sequence is still required.
- `window_config_id` in device API is `uint16_t` (`flexio_dev.h:393`), so make sure IDs passed from host fit expected range for your runtime.

## Related

- External memory class and mapping semantics: `../memory-classes/external-registered-memory.md`
- Direct LD/ST and fence checklist: `window-and-external-ldst.md`
- Host/device handle flow: `../programming-model/mental-model.md`
