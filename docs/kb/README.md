# Knowledge Base

## Purpose

This is the living knowledge base (KB) for this repo. The directory tree is the index; leaf directories contain the detailed topic docs. The top-level split is now:

- factual notes about the existing codebase
- implementation-progress notes about prototypes and in-flight changes
- future-direction notes about target designs and open research questions

## Subdirectories

<!-- kb-subdirs:start -->
- `citus/`: Citus design/workflow notes and how they map to timing spots (Citus source: `/data/dbcomm/citus-dbcomm`).
- `future-directions/`: Forward-looking design notes, open questions, and research directions cross-linked to the grounded KB.
- `implementations/`: Implementation-progress notes for landed and in-flight prototypes, including current behavior, validation, caveats, and milestone history.
- `instrumentation/`: Timing/custom-stats instrumentation spots, call sites, and log aggregation tooling.
- `postgres/`: PostgreSQL-side workflows and hot paths (query loop, libpq IO, COPY, etc.) with code pointers.
<!-- kb-subdirs:end -->

## Documents

<!-- kb-docs:start -->
- (none)
<!-- kb-docs:end -->

## Related

- `README.md`: repo-level entry point (outside KB).
