# DPA

## Purpose

This area captures how DPA is programmed in this repository context, with emphasis on memory handling. It separates static memory taxonomy from operational access paths so debugging and design decisions can be made quickly.

## Subdirectories

<!-- kb-subdirs:start -->
- `access-mechanisms/`: Concrete read/write/copy paths (host calls, async ops, window LD/ST, NIC DMA).
- `compute-resources/`: DPA compute substrate: cores, EUs, logical threads, affinity, and partitioning.
- `memory-classes/`: Memory taxonomy: DPA process memory vs external registered memory.
- `programming-model/`: Mental model for host/device responsibilities, launch styles, and handle passing.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- Imported source KB: `/home/jasonhu/BenchBF3/docs/kb/dpa/`
- Converted BenchBF3 references: `/home/jasonhu/BenchBF3/DOCA-DPA.md`, `/home/jasonhu/BenchBF3/DPA-Development.md`, `/home/jasonhu/BenchBF3/docs/bluefield3-dpa-onboarding.md`
- FlexIO wrappers used by benchmarks: `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp`, `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio_device.c`
- PostgreSQL serializer offload direction: `../future-directions/postgres/serialization/dpa_printtup_serializer_offload.md`
