# DPA Workflow Memory Access Matrix

## Scope

- **What this doc explains**: For each execution workflow, which of the four high-level access patterns are available and which API families implement them.
- **What this doc does NOT cover**: Exhaustive low-level fence rules or NIC queue details.
- **Primary directory**: `docs/kb/dpa/access-mechanisms`

## Why this exists

The same question keeps recurring in slightly different forms: who initiates the transfer, in which direction, and under which execution workflow is that API actually valid. This doc is the matrix answer.

## Matrix dimensions used here

- **Initiator**:
  - host-initiated
  - DPA-initiated
- **Direction**:
  - host/external -> DPA
  - DPA -> host/external

This yields four high-level patterns for each workflow.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:563` - `doca_dpa_mem_alloc(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:601` - `doca_dpa_h2d_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:644` - `doca_dpa_d2h_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:190` - `doca_dpa_dev_buf_get_external_ptr(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:235` - `doca_dpa_dev_post_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev_buf.h:263` - `doca_dpa_dev_mmap_get_external_ptr(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:198` - `doca_dpa_dev_thread_get_local_storage()` is thread-workflow only.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:785` - `flexio_host2dev_memcpy(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:805` - `flexio_memcpy(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:840` - `flexio_copy_from_host(...)`.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:489` - `flexio_dev_window_copy_from_host(...)`.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:523` - `flexio_dev_window_copy_to_host(...)`.

## DOCA kernel launch / blocking RPC

### Host-initiated host -> DPA

- **Available**.
- Main API options:
  - `doca_dpa_mem_alloc(...)` + `doca_dpa_h2d_memcpy(...)`
  - `doca_dpa_mem_alloc(...)` + `doca_dpa_h2d_buf_memcpy(...)`
- Typical use: host stages input in DPA heap, then passes heap pointer as kernel/RPC argument.

### Host-initiated DPA -> host

- **Available**.
- Main API options:
  - `doca_dpa_d2h_memcpy(...)`
  - `doca_dpa_d2h_buf_memcpy(...)`
- Typical use: kernel writes result into heap; host copies it back after completion.

### DPA-initiated host/external -> DPA

- **Partially available**.
- Main API options:
  - Direct external-memory load/read via `doca_dpa_dev_buf_get_external_ptr(...)` or `doca_dpa_dev_mmap_get_external_ptr(...)`, if the host pre-registered/passed external mappings.
- **Not available**:
  - `doca_dpa_dev_post_memcpy(...)` / `doca_dpa_dev_post_buf_memcpy(...)` are explicitly not relevant for host kernel launch APIs.
- Practical meaning: kernel-launch code can read external memory directly if the host sets up the mapping, but it does not get the DOCA thread-style async-copy engine.

### DPA-initiated DPA -> host/external

- **Partially available**.
- Main API options:
  - Direct external-memory stores through external pointers.
- **Not available**:
  - thread-style async posted memcpy APIs.
- Practical meaning: kernel-launch code can write external memory directly, but host visibility/coherency rules still apply; see `coherency-and-fencing.md`.

## DOCA `doca_dpa_thread`

### Host-initiated host -> DPA

- **Available**.
- Main API options:
  - `doca_dpa_mem_alloc(...)` + `doca_dpa_h2d_memcpy(...)`
  - `doca_dpa_mem_alloc(...)` + `doca_dpa_h2d_buf_memcpy(...)`
  - bind heap pointer as thread arg or TLS

### Host-initiated DPA -> host

- **Available**.
- Main API options:
  - `doca_dpa_d2h_memcpy(...)`
  - `doca_dpa_d2h_buf_memcpy(...)`

### DPA-initiated host/external -> DPA

- **Available**.
- Main API options:
  - direct external-memory reads through `doca_dpa_dev_*_get_external_ptr(...)`
  - async device-posted copy through `doca_dpa_dev_post_memcpy(...)`
  - async device-posted copy through `doca_dpa_dev_post_buf_memcpy(...)`
- This is the workflow where DOCA async ops are intended to be used.

### DPA-initiated DPA -> host/external

- **Available**.
- Main API options:
  - direct external-memory stores through `doca_dpa_dev_*_get_external_ptr(...)`
  - async device-posted memcpy from heap/mmap/buf-backed region into external memory
- This is also the workflow where TLS can hold the thread’s long-lived state/config pointer.

## FlexIO direct RPC / CmdQ / event handler

These three workflows differ in activation/submission, but their high-level memory access options come from the same FlexIO memory family.

### Host-initiated host -> DPA

- **Available**.
- Main API options:
  - `flexio_copy_from_host(...)` allocates and copies in one step
  - `flexio_buf_dev_alloc(...)` + `flexio_host2dev_memcpy(...)`
  - `flexio_memcpy(...)` with host/device memory descriptors

### Host-initiated DPA -> host

- **Available from host**, but not through a single dedicated `flexio_d2h_memcpy(...)` convenience API in the headers we inspected.
- Main API options:
  - `flexio_memcpy(...)` with device->host memory descriptors
  - use host-visible/external/window-backed memory instead of copying from heap late
- Repository practice today tends to return scalars via RPC or write into host-visible memory rather than perform a symmetric heap copy-back helper.

### DPA-initiated host/external -> DPA

- **Available**.
- Main API options:
  - `flexio_dev_window_copy_from_host(...)`
  - direct window-backed external pointer load/store paths in device code

### DPA-initiated DPA -> host/external

- **Available**.
- Main API options:
  - `flexio_dev_window_copy_to_host(...)`
  - direct window-backed external pointer stores

## Practical summary

- If you need the simplest host-managed staging model, use host blocking heap copies.
- If you need DPA-initiated copies under DOCA, you are effectively in `doca_dpa_thread` territory.
- If you need DPA-initiated copies under FlexIO, window APIs are the canonical device-side path.
- Kernel launch without external mappings is basically “host stages heap, kernel computes, host collects heap result”.

## Related

- `overview.md`
- `dpa-async-ops.md`
- `window-and-external-ldst.md`
- `host-blocking-copies.md`
- `../programming-model/roundtrip-workflows.md`
