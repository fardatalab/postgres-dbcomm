# External Registered Memory

## Scope

- **What this doc explains**: How external memory is registered/mapped and accessed from DPA, and how this differs from heap semantics.
- **What this doc does NOT cover**: Detailed async ops submission mechanics.
- **Primary directory**: `docs/kb/dpa/memory-classes`

## Why this exists (context)

External memory (BlueField DRAM, host DRAM, GPU memory, etc.) is frequently conflated with heap. It is a separate class requiring explicit registration/mapping and has non-coherent access behavior from DPA window paths.

## Key code pointers

- `/home/jasonhu/BenchBF3/DPA-Development.md:383` - External registered memory definition.
- `/home/jasonhu/BenchBF3/DPA-Development.md:421` - Window maps external memory into DPA process address space.
- `/home/jasonhu/BenchBF3/DPA-Development.md:431` - External access is not coherent.
- `/opt/mellanox/doca/include/doca_mmap.h:427` - `doca_mmap_set_memrange(...)`.
- `/opt/mellanox/doca/include/doca_mmap.h:669` - `doca_mmap_set_permissions(...)`.
- `/opt/mellanox/doca/include/doca_mmap.h:350` - `doca_mmap_dev_get_dpa_handle(...)` (extract handle for DPA use).
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:263` - `doca_dpa_dev_mmap_get_external_ptr(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:109` - Creates FlexIO window object.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:120` - Registers memory with `ibv_reg_mr(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:37` - Device configures window/mkey mapping.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:40` - Device acquires DPA pointer to host address.

## Architecture / data flow

- **Entry points**:
  - DOCA mmap registration APIs.
  - FlexIO window APIs for hostful programs.
- **Control flow**:
  1. Host defines memory range and registers it to device context.
  2. Host creates/starts mapping object and obtains DPA handle.
  3. Host passes handle + address metadata into kernel.
  4. Kernel acquires external pointer or uses async copy APIs with mmap handles.
- **Data structures**:
  - `doca_dpa_dev_mmap_t` handles for device-side use.
  - Window ID + mkey pairs in FlexIO codepaths.
- **State / lifecycle**:
  - Mapping objects must be started before extracting DPA handles.
  - Handle validity follows object lifecycle on host.

## Behavior details

- **Happy path**:
  - Register memory, map it, pass handle to kernel, use for direct LD/ST or async copy.
- **Error paths / edge cases**:
  - `doca_mmap` set operations are not allowed while started (`doca_mmap.h:408`).
  - Converted docs and headers show version-dependent constraints around DPA memrange setup (`doca_mmap.h:470` note).
- **Concurrency / ordering constraints**:
  - Writes to external memory require explicit window writeback for visibility (`/home/jasonhu/BenchBF3/DPA-Development.md:989`).
  - Read invalidation can be required for polling patterns (`/home/jasonhu/BenchBF3/DPA-Development.md:967`).

## Invariants and assumptions

- External-pointer APIs have a 64B alignment restriction (`doca_dpa_dev_buf.h:177`, `doca_dpa_dev_buf.h:249`).
- Unaligned accesses are unsupported generally on DPA (`/home/jasonhu/BenchBF3/DPA-Development.md:641`).

## Configuration / flags

- Access rights come from `doca_mmap_set_permissions(...)`.
- In FlexIO codepaths, mkey permissions come from `ibv_reg_mr(...)` and mkey create attributes.

## Interactions and cross-links

- **Overlapping KB areas**:
  - `heap-memory.md`
  - `../access-mechanisms/window-and-external-ldst.md`
  - `../access-mechanisms/flexio-window-entities.md`
  - `../access-mechanisms/dpa-async-ops.md`
- **Shared code**:
  - Benchmark host memory preparation and window use:
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:66`
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:100`
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:103`

## Gotchas

- "External" does not mean remote machine only; local BlueField DRAM may also be treated as external if accessed through registration/mmap/window paths.

## Open questions / TODO

- Document precise supported/unsupported combinations for `doca_mmap_set_dpa_memrange` plus `doca_mmap_add_dev` under the exact DOCA version deployed on this machine.
