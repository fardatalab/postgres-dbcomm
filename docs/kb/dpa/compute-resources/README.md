# Compute Resources

## Purpose

This directory captures how the DPA compute substrate is organized: cores, EUs, logical threads, affinity, and partitioning. Use it when questions are about "where can code run" rather than "which API copies data".

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [eus-threads-and-partitions.md](eus-threads-and-partitions.md) - Defines cores vs EUs vs logical threads, records the current DPU partition state, and explains the practical effect of EU partitioning and affinity.
<!-- kb-docs:end -->

## Related

- Programming/activation model: `../programming-model/README.md`
- Memory access implications: `../access-mechanisms/README.md`
