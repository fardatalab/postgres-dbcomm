# Serialization And Send Path

## Purpose

Capture how executor tuples become FE/BE protocol bytes, how `printtup` uses memory contexts and buffers, and what design space exists for reducing temporary buffers and copies.

## Subdirectories

<!-- kb-subdirs:start -->
- (none)
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- [datarow_vs_copy_and_citus_transport.md](datarow_vs_copy_and_citus_transport.md) - Distinguishes `printtup`/`DataRow`, PostgreSQL `COPY`, and Citus COPY-based transport/file workflows.
- [printtup_row_serialization_and_transport.md](printtup_row_serialization_and_transport.md) - Grounded `printtup` path from `TupleTableSlot`/`Datum` to `StringInfo`, `PqSendBuffer`, and socket write.
- [server_side_protocol_receive_and_deserialize.md](server_side_protocol_receive_and_deserialize.md) - Backend receive path from `PqRecvBuffer` to `input_message` to typed objects such as bound parameters.
<!-- kb-docs:end -->

## Related

- `../memory-contexts/`: Allocation and context-lifetime groundwork that this path depends on.
- `../libpq-io/socket_wait_and_io.md`: Lower-level socket flush and wait behavior once bytes reach libpq backend transport code.
- `../../dpa/`: BlueField DPA programming and memory-access notes relevant to serializer offload.
- `../../future-directions/postgres/serialization/`: Forward-looking design notes for serializer/send-path changes.
