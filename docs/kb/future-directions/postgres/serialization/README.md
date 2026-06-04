# Serialization Future Directions

## Purpose

Index exploratory serializer/send-path ideas for PostgreSQL. The grounded/current behavior lives under `docs/kb/postgres/serialization/`.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [dpa_printtup_serializer_offload.md](dpa_printtup_serializer_offload.md) - Standalone DPA hardware test shape for the binary `printtup` serializer mirror, including complete `DataRow` body output.
- [printtup_send_path_future_directions.md](printtup_send_path_future_directions.md) - Forward-looking design notes on reducing temporary allocations and copies on the `printtup` send path.
<!-- kb-docs:end -->

## Related

- `../../../postgres/serialization/`: Grounded PostgreSQL serialization/send-path docs.
- `../../../dpa/`: DPA programming model and memory/access-path notes imported from BenchBF3.
