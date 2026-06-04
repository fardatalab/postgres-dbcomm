# DPA Round-Trip Workflows (Host To DPA Compute To Host Result)

## Scope

- **What this doc explains**: For each major execution workflow, what the end-to-end control/data flow looks like for the simple case "host provides input, DPA computes, host consumes result".
- **What this doc does NOT cover**: Detailed coherency/fencing rules and all error paths.
- **Primary directory**: `docs/kb/dpa/programming-model`

## Why this exists

The recurring confusion is mixing execution workflow with memory mechanism. The same heap APIs are usable across multiple workflows, but TLS, async copy, direct external load/store, and activation semantics are not.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:459` - `doca_dpa_kernel_launch_update_set(...)` launches an SPMD-style kernel with inline args.
- `/opt/mellanox/doca/include/doca_dpa.h:524` - `doca_dpa_rpc(...)` runs a blocking `__dpa_rpc__` function.
- `/opt/mellanox/doca/include/doca_dpa.h:888` - `doca_dpa_thread_create(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:926` - `doca_dpa_thread_set_func_arg(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:959` - `doca_dpa_thread_set_local_storage(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:1060` - `doca_dpa_thread_run(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:563` - `doca_dpa_mem_alloc(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:601` - `doca_dpa_h2d_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:644` - `doca_dpa_d2h_memcpy(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:198` - `doca_dpa_dev_thread_get_local_storage()` is thread-workflow only.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:211` - `doca_dpa_dev_yield()` is kernel-launch only.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:224` - `doca_dpa_dev_thread_reschedule()` is `doca_dpa_thread` only.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1110` - `flexio_process_call(...)` direct synchronous RPC.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1848` - `flexio_cmdq_create(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1867` - `flexio_cmdq_task_add(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1036` - `flexio_event_handler_create(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1063` - `flexio_event_handler_run(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:785` - `flexio_host2dev_memcpy(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:840` - `flexio_copy_from_host(...)`.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:496` - `flexio_dev_window_copy_from_host(...)`.
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:530` - `flexio_dev_window_copy_to_host(...)`.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:125` - Host allocates/copies argument struct to DPA memory and passes it to FlexIO work.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:138` - CmdQ benchmark starts queued work.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:150` - Same benchmark uses direct `flexio_process_call(...)` for result collection.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:96` - `bench_func(...)` receives the DPA pointer argument and computes over it.

## DOCA workflow A: kernel launch

### Typical round-trip

1. Host creates/starts a DPA context.
2. Host allocates DPA heap with `doca_dpa_mem_alloc(...)` and copies input with `doca_dpa_h2d_memcpy(...)`.
3. Host launches a kernel with `doca_dpa_kernel_launch_update_set(...)`, passing one or more heap pointers as inline kernel arguments.
4. Kernel computes directly over the heap pointer argument.
5. Host waits for completion by return from the blocking control path or by a sync event, then copies result back with `doca_dpa_d2h_memcpy(...)`.

### Important constraints

- There is no TLS facility for this workflow; `doca_dpa_dev_thread_get_local_storage()` is explicitly not relevant for host kernel launch APIs.
- If the kernel needs persistent state, the host must pass a heap pointer explicitly as an argument.
- `doca_dpa_dev_yield()` is the device-side continuation primitive for this workflow, not `doca_dpa_dev_thread_reschedule()`.
- Device-posted async copy APIs are not available here; see `doca_dpa_dev_post_memcpy(...)` note in `../access-mechanisms/dpa-async-ops.md`.

## DOCA workflow B: blocking RPC

### Typical round-trip

1. Host creates/starts a DPA context.
2. Host allocates DPA heap and copies input, or passes small scalar arguments inline.
3. Host calls `doca_dpa_rpc(...)` on a `__dpa_rpc__` function.
4. Device function computes and returns a scalar result directly, or writes a larger result into heap memory.
5. Host optionally copies larger output from heap with `doca_dpa_d2h_memcpy(...)`.

### Important constraints

- This is still an ephemeral call-style workflow from the memory point of view.
- There is no thread-local storage API attached to `doca_dpa_rpc(...)`.
- For large input/output, use heap pointers rather than trying to squeeze data through the scalar return value.

## DOCA workflow C: persistent `doca_dpa_thread`

### Typical round-trip

1. Host creates/starts the DPA context, creates a DPA thread, sets function/arg/TLS/affinity as needed, and calls `doca_dpa_thread_run(...)`.
2. Host allocates DPA heap for input, output, and optionally a thread-state/TLS struct.
3. Host populates input with `doca_dpa_h2d_memcpy(...)`.
4. Thread is activated by completion/notification infrastructure, computes over heap/TLS/external memory, and may post async work.
5. Thread either finishes, or persists state in heap/TLS and calls `doca_dpa_dev_thread_reschedule()`.
6. Host copies results back with `doca_dpa_d2h_memcpy(...)`, or the thread writes directly to external registered memory that the host later reads.

### Important constraints

- This is the only DOCA workflow in which TLS and `doca_dpa_dev_thread_reschedule()` are available.
- This is also the workflow where DOCA async ops (`doca_dpa_dev_post_memcpy(...)`) are documented to work.
- The logical thread persists, but stack-local state should not be treated as persistent across activations; use heap/TLS-backed state instead.

## FlexIO workflow A: direct synchronous RPC

### Typical round-trip

1. Host creates a FlexIO process with `flexio_process_create(...)`.
2. Host allocates/copies DPA heap input using `flexio_copy_from_host(...)` or `flexio_host2dev_memcpy(...)`.
3. Host invokes `flexio_process_call(...)`, passing either inline scalars or a DPA pointer to an argument/config struct.
4. RPC code computes over the heap pointer.
5. Host gets a scalar return value directly, or consumes output through heap memory or external window memory.

### Important constraints

- This is the closest FlexIO equivalent to DOCA blocking RPC.
- TLS is available through `flexio_rpc_attr.rpc_tls_daddr`, but most code in this repository uses explicit argument structs.

## FlexIO workflow B: CmdQ async RPC

### Typical round-trip

1. Host creates a FlexIO process and a command queue with `flexio_cmdq_create(...)`.
2. Host allocates/copies input/config structs into DPA heap with `flexio_copy_from_host(...)`.
3. Host enqueues tasks with `flexio_cmdq_task_add(...)`; each task carries a single `uint64_t` argument, usually a DPA pointer to a config struct.
4. Worker threads execute the RPC body on DPA.
5. Host waits for queue drain with `flexio_cmdq_is_empty(...)`.
6. Host retrieves results via direct RPC (`flexio_process_call(...)`), external memory, or host-visible result buffers.

### Important constraints

- Task arguments are single-word; multi-field inputs should be packed into a heap struct.
- Worker TLS can be configured through `flexio_cmdq_attr.workers_tls_daddr`, but rank-like semantics are not guaranteed by public headers.

## FlexIO workflow C: event-handler thread

### Typical round-trip

1. Host creates a FlexIO process and event handler with `flexio_event_handler_create(...)`.
2. Host allocates heap/input/config structures and passes one DPA pointer as the event handler argument.
3. Host wires a CQ/completion source to that event handler thread and makes it runnable with `flexio_event_handler_run(...)`.
4. Event handler executes on DPA when the bound event source triggers, computes over heap/external/window memory, and may re-arm for future activations.
5. Host reads results from DPA heap or external memory, depending on how the handler produced them.

### Important constraints

- This is the natural FlexIO analogue of the completion-driven `doca_dpa_thread` model.
- Stack-local state should not be treated as persistent between activations; use heap/TLS-like state passed via the event handler arg or thread-local storage address.

## Decision summary

- If the data is naturally staged in DPA heap and the host orchestrates every step, start with DOCA kernel launch, DOCA blocking RPC, or FlexIO direct RPC.
- If the DPA side must continue after asynchronous completions, use `doca_dpa_thread` or a FlexIO event-handler style workflow.
- If the host needs to enqueue many independent compute tasks, use FlexIO CmdQ.
- If the DPA side must itself initiate copies, DOCA `doca_dpa_thread` and FlexIO window-copy paths are the relevant workflows; plain DOCA kernel launch does not expose the same device-side async-copy APIs.

## Related

- `execution-models.md`
- `flexio-cmdq-vs-rpc.md`
- `../access-mechanisms/workflow-memory-matrix.md`
- `../access-mechanisms/overview.md`
