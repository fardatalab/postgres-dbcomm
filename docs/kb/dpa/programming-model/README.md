# Programming Model

## Purpose

This directory explains the high-level execution model for DPA and where host APIs end versus device APIs begin. Use it first before diving into memory path details.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [device-selection-and-netdev-mapping.md](device-selection-and-netdev-mapping.md) - Maps `mlx5_X` to netdevs for this setup and records which devices actually create DPA processes.
- [doca-thread-activation.md](doca-thread-activation.md) - Explains `doca_dpa_thread` runnable state, explicit notify-based activation, and completion-driven reactivation.
- [doca-thread-benchmark-bringup.md](doca-thread-benchmark-bringup.md) - Records what broke, what was fixed, and what still fails in the DOCA `doca_dpa_thread` memory benchmark bring-up.
- [execution-models.md](execution-models.md) - Side-by-side model of DOCA kernel launch/thread semantics vs FlexIO CmdQ async-RPC worker/task semantics.
- [flexio-cmdq-vs-rpc.md](flexio-cmdq-vs-rpc.md) - Clarifies that CmdQ is asynchronous/batched RPC submission, while `flexio_process_call` is direct synchronous RPC.
- [mental-model.md](mental-model.md) - End-to-end model for compilation, launch styles, and DPA handle passing.
- [roundtrip-workflows.md](roundtrip-workflows.md) - End-to-end host->DPA->host workflows for DOCA and FlexIO, separated by execution model.
<!-- kb-docs:end -->

## Related

- Compute substrate: `../compute-resources/README.md`
- Memory taxonomy: `../memory-classes/README.md`
- Access path details: `../access-mechanisms/README.md`
