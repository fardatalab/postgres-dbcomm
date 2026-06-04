# DPA Printtup Serializer Offload

## Purpose

Capture the first DPA hardware test shape for PostgreSQL binary `printtup` serialization. This is a future-direction note, not current behavior. The goal is not PostgreSQL integration yet; it is to move the existing standalone tuple reserializer onto the BlueField DPA so the device code can be validated against dump files before touching the live backend send path.

## Current PostgreSQL Anchors

- `printtup()` in `src/backend/access/common/printtup.c:746` is the current per-row binary/text output loop.
- `printtup()` prepares the FE/BE `DataRow` message with `pq_beginmessage_reuse()` at `src/backend/access/common/printtup.c:780` and sends the attribute count with `pq_sendint16()` at `src/backend/access/common/printtup.c:782`.
- For non-null binary attributes, `printtup()` calls `SendFunctionCall()` at `src/backend/access/common/printtup.c:823`, records the binary payload length at `src/backend/access/common/printtup.c:824`, and appends the protocol field length and bytes with `pq_sendint32()` and `pq_sendbytes()` at `src/backend/access/common/printtup.c:830`.
- `PrinttupBinaryDumpNormalizeDatumBytes()` in `src/backend/access/common/printtup.c:233` creates portable serializer inputs by copying by-value datums and flattening varlena values before dumping them.
- `PrinttupBinaryDumpWriteRow()` in `src/backend/access/common/printtup.c:460` dumps one row's normalized inputs together with the already serialized binary field payloads.
- The standalone replay path mirrors selected PostgreSQL binary send functions in `reserialize_int4send()` at `fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c:498`, `reserialize_date_send()` at `fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c:513`, `reserialize_textsend()` at `fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c:527`, and `reserialize_numeric_send()` at `fdl_utils/printtup_dump/printtup_binary_dump_reserialize.c:540`.

## DPA Execution Model To Start With

The safest first offload should use host-managed batching plus DPA heap staging, then move toward direct host/window memory only after standalone DPA serializer correctness is stable.

The preferred initial execution model is FlexIO CmdQ:

- `FLEX::CommandQ::CommandQ()` in `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:222` creates an asynchronous worker pool with a configured worker count and batch size.
- `FLEX::CommandQ::add_task()` in `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:250` enqueues one `uint64_t` task argument; for serializer work this should be a DPA pointer to a task/config descriptor, not an overloaded scalar.
- Device-side host-window access is wrapped by `get_host_buffer()` in `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c:31`, which configures a FlexIO window entity and acquires a DPA-side pointer for a registered host address.
- The multithreaded memory benchmark's `bench_func()` in `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/device/mt_memory_device.c:189` is the closest existing shape: host code submits independent tasks, and device workers operate on DPA heap or registered host memory based on a copied argument block.

DOCA kernel launch is a reasonable alternative for a pure SPMD batch where the host knows a fixed `num_threads` for one row batch. DOCA persistent threads are a later option if we need device-side queues, completions, or device-posted async copy operations.

## Memory Plan

Start with explicit DPA heap staging for the standalone test harness:

1. Host preallocates reusable input/output slots for a batch of rows.
2. Host copies compact row descriptors and normalized attribute payloads to DPA heap.
3. Each DPA task serializes one row or a small row chunk into a complete `DataRow` body output slot.
4. Host copies back output lengths/status and complete `DataRow` body bytes.
5. Host compares the returned bytes against the `row_payload` dumped from the current `printtup()` path.

This intentionally adds copies for the first correctness phase, but it avoids the hardest coherency and registration problems while the DPA executable version is still being proven. Direct host/window memory can remove copies later, but BenchBF3 notes show it requires aligned host buffers, registered memory, mkeys/window IDs, and explicit coherency actions such as `__dpa_thread_window_writeback()` before the CPU or NIC consumes DPA-written window memory. Those requirements are summarized in `../../../dpa/access-mechanisms/coherency-and-fencing.md` and `../../../dpa/access-mechanisms/workflow-memory-matrix.md`.

## Serializer Semantics

The first offloaded serializer intentionally reuses the same assumptions as the current standalone C reserializer:

- `int4send` and `date_send` are good first targets because both emit one 32-bit value in network byte order.
- Text-like varlena payloads assume client encoding is identical to server encoding, matching the current standalone test scope.
- Numeric serialization assumes finite values; special values such as NaN and infinities remain intentionally outside the first DPA hardware test scope.
- The dumped normalized inputs are suitable DPA inputs because varlena values are flattened by `PrinttupBinaryDumpNormalizeDatumBytes()`. They are not the same thing as arbitrary raw backend `Datum` values with possible TOAST indirection or backend-private pointers.

## Performance Direction

Avoid per-row dynamic allocation in the standalone DPA harness and in any later hot path. Preallocate host staging slots, DPA heap slots, and output buffers as a ring or slab, then reuse them across batches. Submit multiple rows per task or per CmdQ run so FlexIO/DOCA launch and copy overhead is amortized.

The chosen first output shape is a complete `DataRow` body:

- DPA writes the attribute count, per-field lengths, null markers, and field payloads in the same byte layout that `printtup()` puts into the `DataRow` message body.
- The host-side harness compares that complete body against the dumped `row_payload`.
- This is stricter than comparing individual field payloads because it also verifies row assembly, length placement, and null marker layout.

## Open Questions

- The cleanest standalone harness shape is still to be chosen: it can run as a host process that drives DPA work from the host side, or as a DPU-side process if the DPU deployment/toolchain path is simpler for early experiments.
- Direct host/window output should wait until the heap-staged serializer has exact byte-for-byte correctness.
- Device/root-PF discovery needs a fresh privileged pass. The imported BenchBF3 notes record an observed BF3 setup with roughly 190 usable EUs, but `dpaeumgmt` from this repo context did not verify the current root device state.

## Related

- `../../../dpa/programming-model/roundtrip-workflows.md`
- `../../../dpa/programming-model/execution-models.md`
- `../../../dpa/access-mechanisms/workflow-memory-matrix.md`
- `../../../dpa/access-mechanisms/coherency-and-fencing.md`
- `../../../postgres/serialization/printtup_row_serialization_and_transport.md`
- `printtup_send_path_future_directions.md`
