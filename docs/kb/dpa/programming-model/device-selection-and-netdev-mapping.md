# Device Selection And Netdev Mapping

## Scope

- **What this doc explains**: Which `mlx5_X` argument should be passed to FlexIO/benchmark host apps, how it maps to Linux netdev names on this system, and why representor naming can be misleading.
- **What this doc does NOT cover**: NIC queue steering/topology design.
- **Primary directory**: `docs/kb/dpa/programming-model`

## Key code pointers

- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:183` - `--device_name` CLI flag consumed by benchmark host app.
- `/home/jasonhu/BenchBF3/flexio/bench/mt_memory/mt_memory.cpp:83` - Host creates `FLEX::Context(DeviceName)`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:29` - Device is selected by matching IBV device name from `ibv_get_device_list`.
- `/home/jasonhu/BenchBF3/wrapper_flexio/wrapper_flexio.cpp:113` - `flexio_process_create(...)` is the decisive step that must succeed for DPA execution.
- `/opt/mellanox/flexio/include/libflexio/flexio.h:1130` - `flexio_process_create(struct ibv_context *ibv_ctx, ...)` takes an IB device context, not a Linux interface name.
- `/opt/mellanox/flexio/samples/flexio_rpc/README:24` - Sample usage expects `<mlx5 device>` (IB device with DPA).
- `/opt/mellanox/flexio/samples/packet_processor/README:35` - Same requirement phrased as “IB device with DPA”.

## Observed mapping on this machine

- `mlx5_0` -> `pf0hpf` (also linked to `en3f0pf0sf0`)
- `mlx5_1` -> `pf1hpf` (also linked to `en3f1pf1sf0`)
- `mlx5_2` -> `enp3s0f0s0`
- `mlx5_3` -> `enp3s0f1s0`

The map above comes from `ibdev2netdev`, `rdma link show`, and `/sys/class/infiniband/*/device/net`.

## Practical guidance (current setup)

- For this repo’s FlexIO DPA process creation path, `mlx5_0` and `mlx5_1` work; `mlx5_2` and `mlx5_3` fail at `flexio_process_create(...)`.
- Therefore, pass `--device_name=mlx5_0` (or `mlx5_1`) for now, even if traffic/IP is configured on representor-style interfaces like `enp3s0f1s0`.
- Do not infer DPA process eligibility from “interface has IP” or “representor is up”; eligibility is determined by the IBV device context used for `flexio_process_create`.

## Related

- Execution/handle model: `mental-model.md`
- External-memory path that uses window IDs/mkeys: `../access-mechanisms/window-and-external-ldst.md`
- FlexIO window entities: `../access-mechanisms/flexio-window-entities.md`
