# Portal result streaming: worker -> coordinator -> SQL client (no intermediate files)

## Scope

This doc covers the “portal execution / streaming results” characterization from the writeup:

- A worker executes a task query under PostgreSQL’s normal executor/portal model.
- The worker streams result tuples back to the coordinator as *query results* (libpq `PGresult`), not as intermediate-result files.
- The coordinator consumes the stream and forwards/aggregates as needed, then eventually returns to the SQL client.

This is distinct from intermediate results (push/pull), where results are persisted as intermediate files via COPY “format result”.

Primary timers (overview):

- Coordinator receive loop: `ReceiveResults_`, `ReceiveResults_Net`, `ReceiveResults_Deserialize`, `ReceiveResults_BuildTuples`, `ReceiveResults_HeapFormTuple`
- Backend tuple send (worker or coordinator): `Printtup` / `Printtup_Net` (when sending DataRow messages)

## Coordinator-side workflow (adaptive executor receive loop)

Coordinator consumes worker results in:

- `ReceiveResults(WorkerSession *session, bool storeRows)` defined at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3996`

High-level structure:

- Start `ReceiveResults_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4009`
- Loop while `!PQisBusy(connection->pgConn)` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4019`
- For each `PGresult *result = PQgetResult(...)`:
  - `ReceiveResults_Net` wraps the `PQgetResult` call (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4025` … `:4027`)
  - `ReceiveResults_Deserialize` wraps status checks + `PQgetvalue`/`PQgetlength` extraction (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4037` … multiple end sites up to `:4203`)
  - `ReceiveResults_BuildTuples` wraps heap tuple construction + tuple store writes (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4206` … `:4235`)
    - `ReceiveResults_HeapFormTuple` times `heap_form_tuple(...)` inside tuple builders:
      - `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/executor_util_tuples.c:120` (binary path)
      - `/data/dbcomm/postgres-citus/src/backend/executor/execTuples.c:2266` (text path)
- End `ReceiveResults_` at `/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:4247`

More detailed breakdown:

- `docs/kb/citus/executor/receive_results_streaming.md`

## Worker-side behavior (portal execution + tuple send)

On the worker, query execution uses standard Postgres executor/portal machinery and sends results over the protocol as DataRow/CommandComplete messages.

The instrumentation that captures the per-row “build and send” cost at the sender side is:

- `Printtup` (`/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:317`)
  - `Printtup_Net` (`/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:382`)

## Accuracy of the earlier characterization (portal execution)

Accurate:

- Portal execution streams result tuples to a DestReceiver.
- This path does **not** create intermediate result files; it is a direct result stream (though Postgres/Citus may still buffer or spill in tuple stores depending on plan/executor behavior).

Refinement:

- When focusing on network timing, distinguish:
  - Sender-side per-row send (`Printtup_Net`)
  - Coordinator-side per-row receive/deserialize/build (`ReceiveResults_*`)

## Related

- `docs/kb/instrumentation/timing-spots/timing_spots.md`
- `docs/kb/instrumentation/timing-spots/spot_hierarchy.md`

