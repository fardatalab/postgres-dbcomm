# FlexIO CmdQ Vs RPC

## Scope

- **What this doc explains**: Whether FlexIO `cmdq` is an RPC mechanism, and how it differs from direct `flexio_process_call(...)`.
- **What this doc does NOT cover**: DOCA DPA thread APIs (`doca_dpa_thread_*`).
- **Primary directory**: `docs/kb/dpa/programming-model`

## Key code pointers

- `/opt/mellanox/flexio/include/libflexio/flexio.h:1837` - `flexio_cmdq_create(...)` described as asynchronous RPC command queue infrastructure.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1867` - `flexio_cmdq_task_add(...)` enqueues `(host_func, arg)` task for background execution on DPA.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1883` - `flexio_cmdq_state_running(...)` starts queue processing.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1897` - `flexio_cmdq_is_empty(...)` checks completion.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1110` - `flexio_process_call(...)` direct blocking RPC path.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:222` - `FLEX::CommandQ` wraps `flexio_cmdq_create(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:243` - `FLEX::CommandQ::add_task(...)` wraps `flexio_cmdq_task_add(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:248` - `FLEX::CommandQ::run(...)` transitions queue to running.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:254` - `FLEX::CommandQ::wait_run(...)` polls `flexio_cmdq_is_empty(...)`.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:138` - benchmark submits batched RPC work through cmdq.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:150` - benchmark also uses direct `flexio_process_call(...)` for result collection.

## Answer to the confusion

Yes, CmdQ is an RPC-style invocation path, but specifically the **asynchronous/batched** one:

1. Host enqueues many DPA function calls (`flexio_cmdq_task_add`).
2. Queue is switched to running (`flexio_cmdq_state_running`).
3. Host can wait or poll for drain (`flexio_cmdq_is_empty`).

In contrast, `flexio_process_call(...)` is a direct synchronous RPC submission (one call at a time, blocking semantics for that call).

Therefore, if we classify at host-invocation level, FlexIO has two paths: synchronous direct RPC (`flexio_process_call`) and asynchronous queued RPC (`flexio_cmdq_*`).
If we classify at execution-runtime family level, both are FlexIO RPC mechanisms (not the DOCA `kernel_launch` SPMD model).

## Practical implication for `flexio-window-copy`

- In `flexio-window-copy`, the "CmdQ task" is the **RPC function invocation** of `mt_copy_chunk` / `mt_copy_to_host`.
- Inside that device function body, `flexio_dev_window_copy_from_host(...)` is just one instruction path used by that task.
- Therefore `flexio_dev_window_copy_from_host` does not take a task handle; it executes as part of the currently running DPA RPC task context.

## Related

- Canonical execution-model comparison: `execution-models.md`
- Host/device split and handle passing: `mental-model.md`
- Window entity semantics: `../access-mechanisms/flexio-window-entities.md`
