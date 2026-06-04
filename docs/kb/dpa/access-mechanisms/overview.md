# DPA Memory Access Mechanisms Overview

## Scope

- **What this doc explains**: The canonical access mechanism taxonomy and API mapping.
- **What this doc does NOT cover**: Full API signatures and all edge-case return codes.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Why this exists (context)

Memory behavior questions are easier to resolve if mechanism and memory class are separated. This doc is the canonical mapping used by all DPA memory notes in this KB.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:601` - Host blocking heap copy (`h2d`).
- `/opt/mellanox/doca/include/doca_dpa.h:644` - Host blocking heap copy (`d2h`).
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:235` - Device-posted async memcpy by mmap handles.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:209` - Device-posted async memcpy by doca-buf handles.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:263` - Device obtains external pointer by mmap handle.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:40` - FlexIO window pointer acquire in device code.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:429` - DPA memory key setup for NIC-facing RQ path.

## Architecture / data flow

- **Mechanism A - Host blocking copy APIs**:
  - Initiator: host.
  - Typical memory: host buffer <-> DPA heap.
  - Core APIs: `doca_dpa_h2d_memcpy`, `doca_dpa_d2h_memcpy`.
- **Mechanism B - Device async ops**:
  - Initiator: DPA kernel.
  - Typical memory: mmap/buf-backed regions (heap or external), copied asynchronously.
  - Core APIs: `doca_dpa_dev_post_memcpy`, `doca_dpa_dev_post_buf_memcpy`.
  - Continuation: completion-context driven (poll in-kernel or reschedule/reactivate pattern).
- **Mechanism C - Device direct LD/ST to external mappings**:
  - Initiator: DPA kernel.
  - Typical memory: external registered memory via window/mmap.
  - Core APIs: `doca_dpa_dev_mmap_get_external_ptr`, FlexIO `window_ptr_acquire`.
- **Mechanism D - NIC DMA path**:
  - Initiator: NIC engines.
  - Typical memory: queue/data buffers in host or DPA memory as configured.
  - Core APIs/objects: queue memtype, mkeys, doorbells.

## Invariants and assumptions

- `doca_buf`/`doca_buf_arr` are abstractions over memory mappings, not a separate memory class.
- Access semantics and required fencing depend on mechanism, not only on memory class.

## Interactions and cross-links

- `host-blocking-copies.md`
- `dpa-async-ops.md`
- `window-and-external-ldst.md`
- `nic-dma-paths.md`
- `coherency-and-fencing.md`
