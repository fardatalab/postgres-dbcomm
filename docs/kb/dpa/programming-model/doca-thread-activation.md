# DOCA DPA Thread Activation And Notification

## Scope

- **What this doc explains**: What “runnable”, “activate”, and “notify” mean for `doca_dpa_thread`, and how notification-completion activation differs from completion-driven reactivation.
- **What this doc does NOT cover**: FlexIO worker semantics or kernel-launch-only control flow.
- **Primary directory**: `docs/kb/dpa/programming-model`

## Why this exists

One recurring confusion is treating `doca_dpa_dev_thread_notify()` as an async-ops-only feature. That is incorrect. Notification-based activation is a general `doca_dpa_thread` lifecycle mechanism. Async ops are only one source of later re-activation.

## Key code pointers

- [`doca_dpa_thread_create()`](/opt/mellanox/doca/include/doca_dpa.h:888) - Header explicitly says a DPA thread is activated in two ways: notification completion or attached completion context.
- [`doca_dpa_thread_set_func_arg()`](/opt/mellanox/doca/include/doca_dpa.h:926) - Sets the thread entry function and argument before start.
- [`doca_dpa_thread_start()`](/opt/mellanox/doca/include/doca_dpa.h:1023) - Starts the thread object.
- [`doca_dpa_thread_run()`](/opt/mellanox/doca/include/doca_dpa.h:1060) - Marks the thread runnable so it can run when its completion source is triggered.
- [`doca_dpa_completion_set_thread()`](/opt/mellanox/doca/include/doca_dpa.h:1234) - Attaches a normal completion context to a DPA thread.
- [`doca_dpa_completion_start()`](/opt/mellanox/doca/include/doca_dpa.h:1265) - Starts the attached completion context.
- [`doca_dpa_notification_completion_create()`](/opt/mellanox/doca/include/doca_dpa.h:1463) - Creates the notification-completion context used for explicit thread notification.
- [`doca_dpa_notification_completion_start()`](/opt/mellanox/doca/include/doca_dpa.h:1510) - Starts that notification-completion context.
- [`doca_dpa_notification_completion_get_dpa_handle()`](/opt/mellanox/doca/include/doca_dpa.h:1534) - Produces the device handle later passed to DPA code.
- [`doca_dpa_dev_thread_notify()`](/opt/mellanox/doca/include/doca_dpa_dev.h:253) - Device-side API that notifies a notification-completion handle and triggers the attached DPA thread.
- [`doca_dpa_dev_thread_reschedule()`](/opt/mellanox/doca/include/doca_dpa_dev.h:224) - Releases the EU and allows the same logical thread to be activated again by a future completion.
- [`doca_dpa_dev_thread_finish()`](/opt/mellanox/doca/include/doca_dpa_dev.h:237) - Stops future reactivation.
- [`doca_dpa_dev_get_completion()`](/opt/mellanox/doca/include/doca_dpa_dev.h:380) - Reads completion entries when activation came from a normal completion context.
- [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:566) - Current repo host setup for `doca_dpa_thread`.
- [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:284) - Current repo RPC that explicitly notifies benchmark threads.
- [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:181) - Current repo DPA thread entry point.
- [`dpa_notification_completion_obj_init()`](/opt/mellanox/doca/samples/doca_dpa/dpa_common.c:1000) - Official helper for notification-completion setup.
- [`dpa_nvqual_entry_point()`](/opt/mellanox/doca/samples/doca_dpa/dpa_nvqual/device/dpa_nvqual_kernels_dev.c:175) - Official sample entry-point RPC that notifies multiple DPA threads.

## Terminology

- **Started**: The host has called [`doca_dpa_thread_start()`](/opt/mellanox/doca/include/doca_dpa.h:1023). The thread object exists in the runtime, but that alone does not mean the entry function is running.
- **Runnable**: The host has called [`doca_dpa_thread_run()`](/opt/mellanox/doca/include/doca_dpa.h:1060). The header states this makes the thread runnable such that it will run **when the attached completion source receives a message**.
- **Activated / triggered**: The runtime actually schedules the thread entry function on a DPA EU.
- **Notified**: Some device code called [`doca_dpa_dev_thread_notify()`](/opt/mellanox/doca/include/doca_dpa_dev.h:253) on a notification-completion handle attached to that thread.

The important distinction is: **`start` and `run` prepare a thread to run; `notify` or a real completion event actually activate it.**

## The two activation mechanisms

### 1. Notification-completion activation

- Host side:
  1. Create thread with [`doca_dpa_thread_create()`](/opt/mellanox/doca/include/doca_dpa.h:888).
  2. Set function/arg/TLS as needed with [`doca_dpa_thread_set_func_arg()`](/opt/mellanox/doca/include/doca_dpa.h:926) and optionally [`doca_dpa_thread_set_local_storage()`](/opt/mellanox/doca/include/doca_dpa.h:959).
  3. Start the thread with [`doca_dpa_thread_start()`](/opt/mellanox/doca/include/doca_dpa.h:1023).
  4. Create a notification-completion context with [`doca_dpa_notification_completion_create()`](/opt/mellanox/doca/include/doca_dpa.h:1463), start it with [`doca_dpa_notification_completion_start()`](/opt/mellanox/doca/include/doca_dpa.h:1510), and get its device handle with [`doca_dpa_notification_completion_get_dpa_handle()`](/opt/mellanox/doca/include/doca_dpa.h:1534).
  5. Mark the thread runnable with [`doca_dpa_thread_run()`](/opt/mellanox/doca/include/doca_dpa.h:1060).
- Device side:
  1. Some other DPA context, usually an RPC or another thread, calls [`doca_dpa_dev_thread_notify()`](/opt/mellanox/doca/include/doca_dpa_dev.h:253) on that handle.
  2. The runtime activates the attached thread.

This is the pattern used in the repo benchmark:
- Host builds notification handles in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:692).
- Host copies those handles into DPA memory in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:711).
- A device RPC wakes the threads in [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:284) via the call at [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:291).

### 2. Completion-driven activation / reactivation

- Host side:
  1. Create a normal completion context with [`doca_dpa_completion_create()`](/opt/mellanox/doca/include/doca_dpa.h:1201).
  2. Attach it to the thread with [`doca_dpa_completion_set_thread()`](/opt/mellanox/doca/include/doca_dpa.h:1234).
  3. Start the completion context with [`doca_dpa_completion_start()`](/opt/mellanox/doca/include/doca_dpa.h:1265).
  4. Mark the thread runnable with [`doca_dpa_thread_run()`](/opt/mellanox/doca/include/doca_dpa.h:1060).
- Device side:
  1. A real completion arrives on that completion context.
  2. The runtime activates the attached thread.
  3. The thread consumes entries with [`doca_dpa_dev_get_completion()`](/opt/mellanox/doca/include/doca_dpa_dev.h:380) and acknowledges them with [`doca_dpa_dev_completion_ack()`](/opt/mellanox/doca/include/doca_dpa_dev.h:538).

This is the mechanism async ops rely on after the first wakeup. In the benchmark:
- Host attaches per-thread completion contexts in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:662).
- Device posts async work in [`thread_memory_post_async_copy`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:89).
- Device asks for future notification with [`doca_dpa_dev_completion_request_notification()`](/opt/mellanox/doca/include/doca_dpa_dev.h:550) at [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:230).
- Device then releases the EU with [`doca_dpa_dev_thread_reschedule()`](/opt/mellanox/doca/include/doca_dpa_dev.h:224) at [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:234), and later re-enters the `THREAD_MEMORY_STAGE_WAIT_COMPLETION` path at [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:236).

## Important correction: this is not async-ops-only

- Notification-based activation is a **general** `doca_dpa_thread` wakeup mechanism. The header for [`doca_dpa_thread_create()`](/opt/mellanox/doca/include/doca_dpa.h:888) explicitly lists two activation methods, and async ops are not the only reason to use notification completion.
- Async ops matter because they usually need:
  - an initial activation, often by notification completion or a similar starter path, and then
  - later reactivation on the async completion queue.
- So the right model is:
  - **notification completion** = explicit wakeup trigger,
  - **normal completion context** = event source that can reactivate the thread when real completions arrive,
  - **async ops** = one producer of those real completions.

## What `doca_dpa_dev_thread_notify()` does not do

- The header for [`doca_dpa_dev_thread_notify()`](/opt/mellanox/doca/include/doca_dpa_dev.h:253) is explicit: notification-triggered activation happens **without receiving a completion** on the attached completion context.
- Therefore, after a pure notify-triggered activation, the thread should **not** expect [`doca_dpa_dev_get_completion()`](/opt/mellanox/doca/include/doca_dpa_dev.h:380) to return a meaningful completion for that wakeup.
- Any payload or control message for a pure notify path must be carried separately, for example in a DPA heap struct shared with the thread.

## Practical lifecycle in this repo

1. Host allocates per-thread TLS/config in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:606).
2. Host creates the DPA thread in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:652), starts it at [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:661), and marks it runnable at [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:719).
3. Host prepares notification-completion handles in [`thread_memory_prepare_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:692).
4. Host invokes the dispatch RPC in [`thread_memory_run`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/host/thread_memory_host.c:900).
5. Device RPC [`thread_memory_dispatch_threads`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:284) notifies each thread.
6. The thread first enters [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:181) through notification-based activation.
7. For async modes, the thread posts the first copy, requests completion notification, and reschedules in [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:229).
8. Later completions reactivate the same logical thread, which resumes in the `WAIT_COMPLETION` branch at [`thread_memory_thread_main`](/home/jasonhu/BenchBF3/doca/bench/thread_memory/device/thread_memory_dev.c:236).

## Related

- [execution-models.md](execution-models.md)
- [roundtrip-workflows.md](roundtrip-workflows.md)
- [doca-thread-benchmark-bringup.md](doca-thread-benchmark-bringup.md)
- [../access-mechanisms/dpa-async-ops.md](../access-mechanisms/dpa-async-ops.md)
