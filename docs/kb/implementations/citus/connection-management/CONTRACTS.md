# Connection Management — Contracts And Invariants

<!-- kb-summary: Current compatibility-intent, exact-operation, and data-plane/session-close contracts for Homer connection management. -->

> Use this index for connection/session semantics, then verify the cited frontend code and consult ancestor indexes for cross-component identity rules.

## Symbol entries

### `RemoteExecutionSessionIntentSpec` versus `RemoteExecutionOperationSpec` — compatibility domain versus exact operation

- **MEANS:** `RemoteExecutionSessionIntentSpec` describes the reusable compatibility domain: node, database/user, transaction, freshness, ownership, lane, metadata, placement, and operation class. `RemoteExecutionOperationSpec` carries exact operation and tuple-flow bootstrap details.
- **DOES NOT MEAN:** Session intent owns exact tuple-stream identity, queue descriptors, or per-operation rendezvous fields.
- **CONTRACT / INVARIANT:** Per-stream identity and descriptor information remain in the operation spec so compatibility/reuse is not accidentally narrowed by one exact stream. Code deciding whether a session can be reused consumes intent; exact data-plane setup consumes operation details.
- **ENFORCES / EVERY SITE THAT MUST OBEY IT:** type definitions in `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_frontend.h:160`; validation and session-open handling in `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:328` through `:448`.

## Control-flow and lifecycle contracts

### Exact data-plane close precedes compatibility-session close

- **ORDER:** `CloseRemoteExecutionSessionDataPlane()` closes or flushes the exact tuple stream while preserving the transaction-scoped session and command plane; `CloseRemoteExecutionSession()` later tears down the remaining compatibility session when `closeCompatibilitySessionOnClose` permits it (`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend.c:485` -> `:539`).
- **WHAT BREAKS IF REORDERED:** Treating statement-end data completion as whole-session completion destroys command/session state that later completion or transaction finalization still needs.
- **OWNERS:** The data-plane close owns the exact tuple-stream lifetime; the full session close owns compatibility, command-plane, and final session teardown.
- **TEMPTING WRONG MOVE:** Do not replace the two phases with a single generic “close sink/session” operation based on migrated terminology.

## Related

- [cross_node_tuple_sink_peer_control_checkpoint.md](cross_node_tuple_sink_peer_control_checkpoint.md)
- [local_service_owned_tuple_sink_control_checkpoint.md](local_service_owned_tuple_sink_control_checkpoint.md)
- [../CONTRACTS.md](../CONTRACTS.md)
