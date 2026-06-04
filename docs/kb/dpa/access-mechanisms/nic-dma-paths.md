# NIC DMA Paths

## Scope

- **What this doc explains**: How NIC engines interact with buffers in DPA memory or host/external memory depending on queue/mkey configuration.
- **What this doc does NOT cover**: Full RDMA API surface beyond queue-memory implications.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/opt/mellanox/flexio/include/libflexio/flexio.h:219` - `flexio_memtype` (`FLEXIO_MEMTYPE_DPA` vs `FLEXIO_MEMTYPE_HOST`).
- `/opt/mellanox/flexio/include/libflexio/flexio.h:227` - `struct flexio_qmem` queue memory descriptor.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:336` - SQ data buffer backed by DPA memory and DPA mkey.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:429` - RQ path creates DPA mkey for DPA-backed data buffers.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:403` - Host-backed SQ buffer registration path (`ibv_reg_mr`).
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:487` - Host-backed RQ buffer registration path (`ibv_reg_mr`).
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:513` - RQ descriptors point NIC to chosen buffer addresses + mkey.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.h:183` - Memory writeback before ringing doorbell.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.h:203` - Window writeback in host-window send path.

## Behavior details

- NIC access target is not fixed to one memory class; it follows queue/mkey setup.
- DPA heap-backed buffers can be NIC-accessed when mkeys and descriptors reference them.
- Host/external buffers can likewise be used via host registration + mapping.

## Gotchas

- Data visibility to NIC depends on required writeback/fence points, not only descriptor correctness.
- Debugging NIC data corruption must inspect both address/mkey programming and ordering primitives.

## Related

- Coherency rules: `coherency-and-fencing.md`
- Memory classes: `../memory-classes/heap-memory.md`, `../memory-classes/external-registered-memory.md`

