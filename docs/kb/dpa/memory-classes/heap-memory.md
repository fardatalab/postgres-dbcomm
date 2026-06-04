# DPA Heap Memory

## Scope

- **What this doc explains**: How DPA heap is allocated/managed and how host/NIC interact with heap-resident data.
- **What this doc does NOT cover**: External memory window mapping details.
- **Primary directory**: `docs/kb/dpa/memory-classes`

## Why this exists (context)

Heap is often misunderstood as "DPA-only private memory". In practice, heap is process memory owned by DPA runtime, but host can copy to/from it and NIC can consume it when queue/mkey setup points at DPA addresses.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:563` - `doca_dpa_mem_alloc(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:581` - `doca_dpa_mem_free(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:601` - `doca_dpa_h2d_memcpy(...)` (blocking host->heap copy).
- `/opt/mellanox/doca/include/doca_dpa.h:644` - `doca_dpa_d2h_memcpy(...)` (blocking heap->host copy).
- `/opt/mellanox/flexio/include/libflexio/flexio.h:752` - `flexio_buf_dev_memalign(...)` (FlexIO heap alloc).
- `/opt/mellanox/flexio/include/libflexio/flexio.h:766` - `flexio_buf_dev_free(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:387` - SQ path allocates DPA-backed data area.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:429` - Creates DPA mkey for RQ data area.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1013` - NIC visibility requires memory writeback in relevant paths.

## Architecture / data flow

- **Entry points**:
  - Host alloc/copy APIs (`doca_dpa_mem_alloc`, `doca_dpa_h2d_memcpy`, `doca_dpa_d2h_memcpy`).
  - DPA kernel direct access by DPA pointer.
- **Control flow**:
  1. Host allocates DPA heap memory.
  2. Optional host initialization copy to heap.
  3. Kernel reads/writes heap buffer.
  4. Optional copy back to host.
  5. Host frees heap memory after synchronization.
- **Data structures**:
  - DPA pointer type: `doca_dpa_dev_uintptr_t` (`doca_dpa_dev.h:62`).
- **State / lifecycle**:
  - Heap lifetime is bound to DPA process/context.
  - Caller must prevent kernel use-after-free (`doca_dpa.h:568`).

## Behavior details

- **Happy path**:
  - Host allocates heap, initializes via blocking copy, kernel processes, host retrieves output.
- **Error paths / edge cases**:
  - `doca_dpa_mem_alloc` returns `0x0` on allocation failure (`doca_dpa.h:550`).
  - Copy APIs are synchronous but can still take long under traffic contention (documented for FlexIO copy calls in `/home/jasonhu/BenchBF3/DPA-Development.md:5086`).
- **Concurrency / ordering constraints**:
  - Heap is still in weakly ordered memory model; ordering/visibility to NIC requires writeback/fence in producer-consumer paths.

## Invariants and assumptions

- Heap allocation alignment is suitable for standard language datatypes (`doca_dpa.h:548`).
- DPA does not support unaligned accesses in general (`/home/jasonhu/BenchBF3/DPA-Development.md:641`).

## Configuration / flags

- No special heap object config beyond DPA context state.

## Interactions and cross-links

- **Overlapping KB areas**:
  - `external-registered-memory.md` for memory that needs host direct mapping.
  - `../access-mechanisms/nic-dma-paths.md` for NIC usage of heap memory via mkeys.
- **Shared code**:
  - FlexIO benchmark private memory init:
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:54`
    - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:109`

## Gotchas

- "Heap is internal" does not mean "NIC cannot access it". NIC access is possible when queue descriptors and mkeys target heap-backed DPA addresses.

## Open questions / TODO

- Validate practical upper limits for heap footprint under this repo's benchmark mix and BF3 firmware configuration.
