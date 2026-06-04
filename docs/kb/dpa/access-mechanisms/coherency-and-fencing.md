# Coherency And Fencing

## Scope

- **What this doc explains**: Practical fence/writeback requirements by access mechanism.
- **What this doc does NOT cover**: Formal memory-model proof details.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/home/jasonhu/BenchBF3/DPA-Development.md:626` - DPA memory model is coherent but weakly ordered.
- `/home/jasonhu/BenchBF3/DPA-Development.md:849` - Fence API section.
- `/home/jasonhu/BenchBF3/DPA-Development.md:989` - `__dpa_thread_window_writeback()`.
- `/home/jasonhu/BenchBF3/DPA-Development.md:967` - `__dpa_thread_window_read_inv()`.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1010` - `__dpa_thread_memory_writeback()`.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1032` - SDKs may already apply required fences in many paths.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.h:183` - Repository uses memory writeback before SQ doorbell.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.h:203` - Repository uses window writeback when host-window memory path is used.

## Practical decision table

- **DPA writes external/window memory and host/NIC/peer must observe it**:
  - Require `__dpa_thread_window_writeback()`.
- **DPA polls external/window location updated elsewhere**:
  - Use `__dpa_thread_window_read_inv()` where needed to avoid stale cache reads.
- **DPA writes internal memory that NIC will read (e.g., WQE/data before doorbell)**:
  - Use memory writeback/fence sequence as required by path.
- **Host blocking copy APIs (`doca_dpa_h2d_memcpy`, `doca_dpa_d2h_memcpy`)**:
  - Host call semantics are synchronous; avoid adding redundant device-side fences unless path includes additional device/NIC interactions.

## Invariants and assumptions

- Fence requirements come from producer/observer pair and mechanism, not only from memory class labels.
- External mapped memory is the highest-risk path for stale visibility bugs.

## Gotchas

- A frequent error pattern is correct pointer/mkey setup but missing writeback before doorbell or before external observer reads.
- Converted docs include broad statements; use API headers + path-specific behavior to resolve contradictions.

## Related

- `overview.md`
- `window-and-external-ldst.md`
- `nic-dma-paths.md`

