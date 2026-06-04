# Access Mechanisms

## Purpose

This directory captures operational memory access paths independently of memory class. Use it to answer "who initiates the access" and "which API family applies".

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [coherency-and-fencing.md](coherency-and-fencing.md) - Practical ordering and visibility rules by path.
- [dpa-async-ops.md](dpa-async-ops.md) - Device-posted non-blocking operations and handle-passing pattern.
- [flexio-window-entities.md](flexio-window-entities.md) - Distinguishes host window objects from per-thread entity slots and explains lifecycle/usage.
- [host-blocking-copies.md](host-blocking-copies.md) - Host-side synchronous copy paths into/out of DPA heap.
- [nic-dma-paths.md](nic-dma-paths.md) - NIC engine access via queue memory and mkeys.
- [overview.md](overview.md) - Canonical map of access mechanisms and when to choose each one.
- [window-and-external-ldst.md](window-and-external-ldst.md) - Direct DPA load/store into external mapped memory.
- [workflow-memory-matrix.md](workflow-memory-matrix.md) - Availability matrix of host-initiated and DPA-initiated copy directions under each workflow.
<!-- kb-docs:end -->

## Related

- Memory classes: `../memory-classes/README.md`
- Compute substrate: `../compute-resources/README.md`
- Host/device API split: `../programming-model/mental-model.md`
