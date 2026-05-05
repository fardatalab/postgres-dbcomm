# Citus overview (conceptual)

## Scope

This is a conceptual overview (from earlier writeups) intended to anchor the Citus-oriented timing spots in `src/include/timing_spots.h:84`–`src/include/timing_spots.h:119`.

Code pointer note:

- Citus source code is located at `/data/dbcomm/citus-dbcomm`. This doc includes representative Citus entry points that correspond to the timing spots you instrumented.

## Architecture summary

- Shared-nothing cluster.
- One **coordinator** (client-facing) and multiple **workers**.
- Coordinator orchestrates workers via distributed planning and execution.
- Coordinator keeps relevant table metadata / transaction records.
- Tables are sharded across workers; optional multi-tenancy/co-location.

## Recursive planning / subplans

In the described model, coordinator planning can produce “recursive subplans”:

- For a subquery/CTE that can’t be pushed down cleanly, the coordinator replaces it with a placeholder.
- The placeholder executes separately to produce intermediate results.
- The main query is rewritten to read the intermediate results.

Intended timing spots (conceptual mapping):

- Planning-level spots: `QueryPlanning` (`src/include/timing_spots.h:67`) and `QueryAnalyzeAndRewrite` (`src/include/timing_spots.h:65`) are likely intended to be used on the coordinator side to break down end-to-end timing beyond `ExecSimpleQuery` (`src/include/timing_spots.h:63`). These are not wired in the Postgres tree snapshot yet (see `docs/kb/instrumentation/timing-spots/timing_spots.md`).
- Intermediate results (push/pull) spots are listed under `FetchIntermediate_*`, `SendViaCopy_*`, `ExecutePlanInto*`, `ReceiveAndWriteCopyData_*` (`src/include/timing_spots.h:93`–`src/include/timing_spots.h:105`).

Concrete Citus entry points for intermediate results + repartitioning:

- `ExecutePlanIntoColocatedIntermediateResults(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:340`) uses:
  - `ExecutePlanIntoColocatedIntermediateResults_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:347`)
  - `ExecutePlanIntoDestReceiver_` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/insert_select_executor.c:374`)
- Pull-based colocation is implemented via SQL calls to `fetch_intermediate_results()`:
  - `ColocateFragmentsWithRelation(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/distributed_intermediate_results.c:472`)
  - `fetch_intermediate_results(PG_FUNCTION_ARGS)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/intermediate_results.c:922`)

Concrete Citus entry point for portal/result streaming:

- Adaptive executor receive loop on coordinator: `ReceiveResults(...)` (`/data/dbcomm/citus-dbcomm/src/backend/distributed/executor/adaptive_executor.c:3995`) timed by `ReceiveResults_*` spots (see `docs/kb/citus/executor/receive_results_streaming.md`).
