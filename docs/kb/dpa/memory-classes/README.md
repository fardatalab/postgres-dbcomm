# Memory Classes

## Purpose

This directory separates DPA memory by ownership and semantics. Use it to decide whether a buffer should live in DPA heap or in external registered memory, and which APIs/fences follow from that choice.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [external-registered-memory.md](external-registered-memory.md) - Registration and mapping flow for memory accessible through window/mmap handles.
- [heap-memory.md](heap-memory.md) - DPA heap allocation, host interactions, and NIC visibility considerations.
<!-- kb-docs:end -->

## Related

- Programming model: `../programming-model/mental-model.md`
- Access paths: `../access-mechanisms/overview.md`
- Fencing details: `../access-mechanisms/coherency-and-fencing.md`

