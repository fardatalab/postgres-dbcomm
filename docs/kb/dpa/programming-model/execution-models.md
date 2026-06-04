# DPA Execution Models (DOCA Kernel Launch Vs FlexIO RPC/CmdQ)

## Scope

- **What this doc explains**: Two scheduler-level models (DOCA kernel/thread vs FlexIO RPC worker-pool) and, within FlexIO, two invocation paths (`flexio_process_call` sync vs `flexio_cmdq_*` async queued).
- **What this doc does NOT cover**: Detailed memory fencing rules and cache semantics (see `../access-mechanisms/coherency-and-fencing.md`).
- **Primary directory**: `docs/kb/dpa/programming-model`

## Why this exists

The biggest recurring confusion is treating `num_threads` in DOCA kernel launch as equivalent to `workers`/`batch_size` in FlexIO CmdQ, and whether `flexio_process_call(...)` is a third independent model. They are related only at a high level (both execute on DPA threads), but they use different host APIs, scheduling contracts, and semantics.

## Taxonomy used in this KB

- **Execution scheduler model #1**: DOCA kernel/thread launch model (`doca_dpa_kernel_launch_*`, `doca_dpa_thread_*`, thread groups).
- **Execution scheduler model #2**: FlexIO RPC worker-pool model.
  - **Submission path A (sync)**: `flexio_process_call(...)`.
  - **Submission path B (async queued)**: `flexio_cmdq_*`.

So, `flexio_process_call(...)` is a different host-side invocation path from CmdQ, but both belong to the same FlexIO RPC execution family.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:459` - `doca_dpa_kernel_launch_update_set(...)` launches one kernel with explicit `num_threads`.
- `/opt/mellanox/doca/include/doca_dpa.h:492` - `doca_dpa_kernel_launch_update_add(...)` launch variant with additive completion behavior.
- `/opt/mellanox/doca/include/doca_dpa.h:524` - `doca_dpa_rpc(...)` blocking RPC path (`__dpa_rpc__` function).
- `/opt/mellanox/doca/include/doca_dpa.h:1094` - `doca_dpa_thread_group_create(...)` explicit thread-group size.
- `/opt/mellanox/doca/include/doca_dpa.h:1123` - `doca_dpa_thread_group_get_num_threads(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:381` - `doca_dpa_get_kernel_max_run_time(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:572` - `flexio_cmdq_attr` (`workers`, `batch_size`, optional worker TLS array).
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1848` - `flexio_cmdq_create(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1867` - `flexio_cmdq_task_add(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1883` - `flexio_cmdq_state_running(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1897` - `flexio_cmdq_is_empty(...)`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1110` - `flexio_process_call(...)` synchronous direct RPC (non-CmdQ).
- `/opt/mellanox/flexio/include/libflexio-dev/flexio_dev.h:547` - `flexio_dev_get_thread_id(void *)` gets current executing device thread ID.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:222` - `FLEX::CommandQ` constructor sets `workers`/`batch_size`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:243` - `FLEX::CommandQ::add_task(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:248` - `FLEX::CommandQ::run(...)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:254` - `FLEX::CommandQ::wait_run(...)` polls until queue drain.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:100` - repo benchmark creates one CmdQ with `ThreadsHost + ThreadsPrivate` workers.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:126` - host enqueues `bench_func` task for each host-memory stream.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:134` - host enqueues `bench_func` task for each private-memory stream.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:96` - `bench_func(...)` device task body executed by CmdQ workers.

## Model A: DOCA kernel/thread execution model

1. Host creates and starts a DPA context, then launches one kernel function with an explicit thread count (`doca_dpa_kernel_launch_update_set/add`).
2. `num_threads` is part of the launch call and is bounded by `doca_dpa_get_max_threads_per_kernel(...)`.
3. If strict rank semantics are required, DOCA offers explicit thread-group APIs (`doca_dpa_thread_group_create`, set/get thread by rank).
4. Runtime bound is explicitly queryable via `doca_dpa_get_kernel_max_run_time(...)`.

This model is the closest to “SPMD launch a kernel across N threads.”

## Model B: FlexIO CmdQ async RPC model

1. Host creates one async RPC queue with `flexio_cmdq_create(...)` and attributes `{workers, batch_size}`.
2. Host enqueues `(func, arg)` tasks with `flexio_cmdq_task_add(...)`; each task is an invocation of a `__dpa_rpc__` function.
3. Host transitions queue to running (`flexio_cmdq_state_running`) and waits/polls (`flexio_cmdq_is_empty`).
4. Queue-drain semantics are explicit (“all jobs up to this point were performed”), but strict execution order/rank mapping is not documented in public headers.

This model is “task-queue execution on a worker pool,” not “single SPMD launch with fixed logical ranks.”

## Model C (same family as B): FlexIO direct synchronous RPC

1. Host calls `flexio_process_call(...)` with one function + arguments.
2. API is direct/synchronous from host perspective (no explicit queue object, no worker-count/batch-size knobs).
3. Best used for control-path calls (for example result collection), not high-rate batched data-path task submission.

In this repository, `flexio_process_call(...)` appears in result collection while hot work submission uses CmdQ.

## Worker management and rank semantics

- There is no public host API to “create CmdQ worker threads one by one” before creating the queue; workers are configured by `flexio_cmdq_attr.workers` during queue creation (`flexio.h:572`, `flexio.h:1848`).
- There is no public host API that returns a stable CmdQ worker rank.
- Device code can query the executing thread ID (`flexio_dev_get_thread_id`) and thread-local storage (`flexio_dev_get_thread_local_storage`), but this is runtime metadata, not a documented stable worker-rank contract.
- If you need deterministic logical rank semantics with CmdQ, pass rank explicitly in each task argument (same pattern as `stream_id` in `mt_memory.cpp:121` and `mt_memory_device.c:98`).

## What `batch_size` means in practice

- `batch_size` is per-worker “up to this many tasks in a single invocation” (`flexio.h:573-578`).
- It is not bytes-per-copy, not elements-per-copy, and not global queue depth.
- `batch_size > 1` allows a worker to execute more than one queued task back-to-back before yielding control to runtime scheduling.
- The header does not define strict fairness, ordering, or a public dequeue-loop contract.

## Side-by-side summary

- **DOCA kernel launch**: one launch call + `num_threads`, SPMD-like, explicit kernel runtime-limit API.
- **FlexIO direct RPC (`flexio_process_call`)**: synchronous single-call submission path.
- **FlexIO CmdQ**: queue-based asynchronous submission path for many RPC tasks, explicit queue-drain completion check.
- **DOCA thread-group APIs**: explicit rankable thread objects/groups.
- **FlexIO RPC tasks**: natural fit for many independent jobs when submitted via CmdQ; same function can also be invoked directly via `flexio_process_call`.

## Repository mapping (on `jason-bench`)

- The active benchmark implementation in this branch uses the FlexIO CmdQ model for work submission:
  - CmdQ creation: `flexio/bench/mt_memory/mt_memory.cpp:100`
  - task enqueue loops: `flexio/bench/mt_memory/mt_memory.cpp:120`, `flexio/bench/mt_memory/mt_memory.cpp:128`
  - queue run/wait: `flexio/bench/mt_memory/mt_memory.cpp:138`, `flexio/bench/mt_memory/mt_memory.cpp:139`
- The same benchmark uses direct synchronous RPC for result aggregation:
  - `flexio/bench/mt_memory/mt_memory.cpp:150`, `flexio/bench/mt_memory/mt_memory.cpp:162`

## Related

- `mental-model.md`
- `flexio-cmdq-vs-rpc.md`
- `../access-mechanisms/dpa-async-ops.md`
