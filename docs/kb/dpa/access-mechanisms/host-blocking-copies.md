# Host Blocking Copy Paths

## Scope

- **What this doc explains**: Host-initiated synchronous copy paths between host memory and DPA heap memory.
- **What this doc does NOT cover**: Device-posted async copy path.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:586` - Header states `doca_dpa_h2d_memcpy(...)` is blocking.
- `/opt/mellanox/doca/include/doca_dpa.h:629` - Header states `doca_dpa_d2h_memcpy(...)` is blocking.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:785` - `flexio_host2dev_memcpy(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:840` - `flexio_copy_from_host(...)`.
- `/home/jasonhu/BenchBF3/DPA-Development.md:5086` - FlexIO warning about host->DPA copy calls being blocked under traffic.

## Behavior details

- These APIs are synchronous from the host perspective.
- Blocking does not imply fixed latency; under contention, call duration can become large.
- In this repo, host initialization of DPA buffers uses this path:
  - `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.hpp:95`
  - `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:54`

## Gotchas

- Converted docs contain text saying memory APIs are asynchronous in a device context (`/home/jasonhu/BenchBF3/DOCA-DPA.md:2175`), but the host copy APIs above are explicitly blocking in the authoritative header declarations.

## Related

- Device async path: `dpa-async-ops.md`

