# DOCA Header Snapshot For DPU DMA Homer Plan

## Purpose

This directory is a local snapshot of the DOCA API headers referenced by
[`../dpu_dma_backend_homer_service_plan.md`](../dpu_dma_backend_homer_service_plan.md).
It is intentionally copied beside the design note so reviewers can inspect the
API prototypes and comments without access to `/opt/mellanox/doca/` on the
original development machine.

## Source

Copied from `/opt/mellanox/doca/include/` on `farnet1` on June 27, 2026.

## Headers

<!-- kb-docs:start -->
- [doca_buf.h](doca_buf.h) - DOCA buffer object and data pointer/length APIs used to point DMA tasks at registered host or DPU memory.
- [doca_comch.h](doca_comch.h) - DOCA communication channel APIs for cold-path descriptor exchange and setup messages.
- [doca_ctx.h](doca_ctx.h) - Generic DOCA context lifecycle APIs used by DMA, COMCH, and sync-event task contexts.
- [doca_dev.h](doca_dev.h) - DOCA device discovery/opening and device representation APIs.
- [doca_dma.h](doca_dma.h) - DOCA DMA context and memcpy task APIs.
- [doca_error.h](doca_error.h) - DOCA error codes and string conversion helpers.
- [doca_mmap.h](doca_mmap.h) - DOCA memory registration, export, import, and permission APIs.
- [doca_pe.h](doca_pe.h) - DOCA progress engine, task submit, submit flags, and batch submit APIs.
- [doca_sync_event.h](doca_sync_event.h) - Optional sync-event APIs for future idle/cold wakeup experiments.
- [doca_types.h](doca_types.h) - Common DOCA types and access flags such as PCI read/write and relaxed ordering.
<!-- kb-docs:end -->

## SHA256

```text
dc76521da94a162e4f053734126bef90c7bed6000b9130e58089a2609a62b29d  doca_buf.h
f7959b42a833939bd3740c95630c82a695b060c2ed890f8d28652510d02dcf74  doca_comch.h
fcc94505655fc5d4be67f4ff19f862013e2b6c4e465b54be9f88cc5561c2ab0d  doca_ctx.h
0e11d01408950e2258cf848825192741903b51b61078f50c1fd9deec999febd6  doca_dev.h
092b85bde459fecfb24f9f2c38aa7debc837e9c14da96edc9a040153560361f6  doca_dma.h
93179509cd3d515f3e3148fdcac24ea26e93948440091ec34b3d83935e181a7b  doca_error.h
f511262b4dee692a67509afc70834c3cfacc356bfdc7442b159460e77ed31dc6  doca_mmap.h
86790c6bac32dc140debd67f4f224ecec35af4eea993871889b2ea4beca6b170  doca_pe.h
d5ebc24a5553dd7670ce0bc758288439a6b0ce0eac05f9468a518c1ec88305bd  doca_sync_event.h
feb715c5ba965d0f4c2b19d05108256a8f111baa43a775d02cd9a729498b5e2b  doca_types.h
```
