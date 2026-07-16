# PostgreSQL Replication — Contracts And Invariants

<!-- kb-summary: Current graceful/abort close and exact-record sizing contracts for the Homer basebackup sink. -->

> These contracts preserve PostgreSQL basebackup lifecycle semantics while the Homer sink changes archive-object delivery.

## Control-flow and lifecycle contracts

### Successful end-backup closes gracefully; error, cancel, and FATAL abort

- **ORDER:** Construct and register the `before_shmem_exit` backstop before the backup cleanup window; arm it only after the Homer stream opens; only successful `end_backup` publishes `OBJECT_END` and drains/closes gracefully; disarm it during graceful end or ordinary cleanup (`src/backend/backup/basebackup_homer.c:102` -> `:556`).
- **WHAT BREAKS IF REORDERED:** Treating every cleanup as graceful can publish false success after ERROR/cancel, while relying only on `PG_FINALLY` misses FATAL process exit and can leave the receiver waiting indefinitely.
- **OWNERS:** The `bbsink` owns normal end/cleanup; the exit callback is the FATAL backstop; `stream_open` prevents double close.
- **TEMPTING WRONG MOVE:** Do not emit `OBJECT_END` merely because an open stream is being cleaned up.

## Symbol entries

### Exact Homer record length versus generic `bbsink` buffer capacity

- **MEANS:** Each published Homer record advertises the callback's actual payload length. The compatibility path currently receives bytes in a private scratch buffer, reserves an exact-sized Homer record, and copies only that length.
- **DOES NOT MEAN:** `bbs_buffer_length`, `payloadCapacityBytes`, or the reserved slot envelope is the transmitted record length; the current generic callback path is not zero-copy.
- **CONTRACT / INVARIANT:** Reserve with the exact payload length, copy exactly that many bytes, submit the same length, then restore the scratch buffer as `bbs_buffer` for the next producer callback.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** exact-record reservation in `bbsink_homer_reserve_record()` at `src/backend/backup/basebackup_homer.c:263`; compatibility copy in `bbsink_homer_write_record()` at `:325`; scratch-buffer installation in `bbsink_homer_begin_backup()` at `:380`; archive and manifest callback use at `:402` and `:438`.

## Related

- [homer_base_backup_target_checkpoint.md](homer_base_backup_target_checkpoint.md)
- [../CONTRACTS.md](../CONTRACTS.md)
