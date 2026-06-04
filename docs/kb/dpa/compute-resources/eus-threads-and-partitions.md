# DPA Cores, EUs, Threads, Affinity, And Partitions

## Scope

- **What this doc explains**: The practical meaning of DPA cores, execution units, logical threads, thread affinity, and EU partitioning.
- **What this doc does NOT cover**: Detailed memory coherency behavior or per-API return codes.
- **Primary directory**: `docs/kb/dpa/compute-resources`

## Why this exists

These terms are easy to conflate. The most common mistakes are:

- flipping cores and EUs,
- treating logical thread count as concurrent running-thread count,
- assuming EU partitioning changes the kernel API instead of only the set/numbering of eligible EUs.

## Key code pointers

- `/opt/mellanox/doca/include/doca_dpa.h:756` - `doca_dpa_get_core_num(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:771` - `doca_dpa_get_num_eus_per_core(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:786` - `doca_dpa_get_total_num_eus_available(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:804` - `doca_dpa_eu_affinity_create(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:834` - `doca_dpa_eu_affinity_set(...)`.
- `/opt/mellanox/doca/include/doca_dpa.h:991` - `doca_dpa_thread_set_affinity(...)`.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:169` - `doca_dpa_dev_thread_rank()` for kernel/thread-group rank, not hardware thread ID.
- `/opt/mellanox/doca/include/doca_dpa_dev.h:224` - `doca_dpa_dev_thread_reschedule()` releases the EU back to runtime scheduling.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1738` - EUs are described as the equivalent of logical cores.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1774` - EU partition definition and default-partition behavior.
- `/home/jasonhu/BenchBF3/DPA-Development.md:1795` - non-default partitions use virtual EU numbering.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:424` - `struct flexio_affinity`.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:442` - event-handler affinity.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:595` - direct RPC affinity.

## Mental model

- **DPA core**: physical compute cluster on the DPA.
- **EU (execution unit)**: schedulable execution slot exposed to software; treat this as the closest public-facing equivalent to a logical core.
- **Logical DPA thread / worker / handler**: runtime object that can be scheduled onto an EU.

The critical distinction is that the runtime can manage more logical threads than there are EUs, but only a bounded number can run at the same time. Public API wording for `doca_dpa_dev_thread_reschedule()` says it releases the EU and makes it available again, which implies one running thread occupies one EU while scheduled.

## Current observed configuration on this DPU

From `dpaeumgmt info chip --dpa_device mlx5_0` and `mlx5_1`:

- `num_cores = 12`
- `eu_num = 16` per core in the chip dump
- `max_threads = 65536`
- `max_threads_per_process = 16384`
- `thread_stack_size = 8192`

From `dpaeumgmt partition info --dpa_device mlx5_0` and `mlx5_1`:

- `Max number of DPA EUs available to use = 190`

Interpretation:

- The chip-level topology exposed by the management tool looks like 12 cores x 16 EUs/core.
- The practical usable-EU count reported to partition management is 190.
- Therefore, plan around **190 usable concurrent execution slots**, not 192 inferred from raw topology.

## Current partition state on this DPU

From `dpaeumgmt info status` / `partition query`:

- `mlx5_0`: root device, `number_of_partitions = 0`, `number_of_root_groups = 0`
- `mlx5_1`: root device, `number_of_partitions = 0`, `number_of_root_groups = 0`

This means the system is currently using the default/root partition model with no explicit user-created partitions or EU groups.

## `mlx5_X` devices vs vHCAs

- `mlx5_0` / `mlx5_1` are device names accepted by DOCA/FlexIO tooling.
- They are **not** themselves vHCA IDs.
- vHCAs are the IDs printed by `dpaeumgmt info vhca`, such as `0`, `0x5`, `0x6`, etc.

This matters because partitions are assigned to vHCAs/functions, while many application flags refer to `mlx5_X` device names.

## What partitioning changes

Per the docs, an EU partition is just a subset of EUs marked as available for a device/function. The practical effects are:

- It changes which EUs a given function may run on.
- It changes the visible EU numbering for non-default partitions.
- It does **not** introduce a different kernel API or a different memory model.

The virtual-numbering note is the biggest practical trap: if a partition contains real EUs `20-40`, the function in that partition may see them as `0-20` instead. Any fixed EU affinity or YAML resource selection must therefore use the partition-visible numbering, not root physical numbering.

## Affinity implications

### DOCA

- `doca_dpa_eu_affinity_set(...)` targets one EU ID at a time.
- `doca_dpa_thread_set_affinity(...)` fixes a `doca_dpa_thread` to that EU.
- If no affinity is specified, the docs say the default is relaxed scheduling and the thread may run on any available EU when rescheduled.

### FlexIO

- FlexIO affinity is carried in `struct flexio_affinity`.
- The same high-level rule applies: affinity constrains where execution may happen, but it does not change the memory API family available to the code.

## Same-core vs different-core placement

The public docs and headers examined here do not define a richer “same-core locality contract” beyond exposing the core/EU hierarchy and EU-level affinity controls. So:

- It is reasonable to think of EUs on the same core as a hardware grouping.
- It is **not** reasonable to assume a documented user-facing cache-sharing or scheduling guarantee beyond what the APIs explicitly say.

If same-core placement matters for an experiment, choose EU IDs accordingly and document the assumption explicitly.

## Concurrency summary

- More logical threads may exist than EUs.
- Roughly 190 threads can be running concurrently on the current configuration.
- The rest are runtime-managed objects waiting to be scheduled or reactivated.
- Kernel-launch `num_threads` and thread-group size are logical concurrency requests bounded by the available EU/runtime limits.

## Related

- `../programming-model/execution-models.md`
- `../programming-model/roundtrip-workflows.md`
- `../programming-model/device-selection-and-netdev-mapping.md`
