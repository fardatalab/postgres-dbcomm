# Peer Service Push Completion Implementation Plan

## Summary

The existing no-poll command-completion plan is directionally correct, but it is
not detailed enough to implement the service-to-service path without making new
ad hoc choices. This note narrows the next prerequisite milestone: remove the
normal peer `POLL_COMMAND_COMPLETION` round trip for backend-to-backend command
completion, with Citus tuple COPY as the first acceptance workload.

This is separate from local frontend client-SQL pushed completion. That path
already has a caller-visible completion mailbox. The missing path is generic
service-to-service command completion for commands started through a peer service,
including tuple COPY's `COPY_INGEST_FROM_TUPLE_SINK`.

## Current Code Facts

- The high-level target is already described in
  [homer_transport_scheduler_and_payload_streams.md](homer_transport_scheduler_and_payload_streams.md),
  especially the concrete no-poll plan around `POLL_COMMAND_COMPLETION`.
- The peer protocol still exposes
  `CITUS_REMOTE_EXEC_PEER_REQUEST_POLL_COMMAND_COMPLETION` in
  `/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:40`.
- The backend-facing wait API still issues a local service
  `POLL_COMMAND_COMPLETION` request in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2301`,
  and `WaitForRemoteExecutionCommandCompletion()` loops on that poll at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:2722`.
- Local-control async peer polling is still built in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:18205`.
- The worker-side service consumes backend completion records in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8450`.
  For non-client-SQL terminal peer commands it still sets
  `terminalCompletionPendingPeerPoll` at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:8595`.
- The responder-side peer poll handler still serializes completion into a peer
  response in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:19545`.
- Tuple COPY starts and later waits for the worker-side ingest command in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2650`
  and
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2817`.

## Design Decisions To Preserve

- `START_COMMAND` remains command submission. It may carry an immediate
  `STARTED`, `COMPLETED`, or `FAILED` response, but delayed terminal completion
  is delivered as a separate event.
- The peer completion ring is a command/control event queue, not a tuple/result
  payload sink.
- A pushed completion means the responder writes completion state to a destination
  chosen at session/open time. It does not require interrupt-style notification.
  The requester may still poll a local epoch/ring as part of the service loop.
- Keep `POLL_COMMAND_COMPLETION` as a debug fallback until tuple COPY, pgbench,
  and basebackup validation pass through the new path.

## Chosen Implementation Shape

Use a requester-service-owned, backend-visible peer command completion ring. This
matches the local frontend pushed-completion precedent: the service owns
creation, lifetime, and RDMA registration, while the command caller maps the ring
directly and waits on its epoch instead of submitting another local control
request.

The first implementation should not add a service-mediated intermediate wait
surface. That would remove the remote peer poll but keep a local control request
in the normal completion path, which is not the long-term shape we want. Add the
direct ring state to the backend-facing `RemoteExecutionSessionData` from the
start.

Use the same choices as local frontend pushed completion:

- fixed 8-slot SPSC ring,
- one command in flight per session,
- producer fills the completion event slot before publishing `publishedEpoch`,
- consumer advances `consumedEpoch` after copying the event,
- a full ring must not overwrite unread state.

## Implementation Steps

### 1. Define The Peer Completion Ring

Add a small fixed-size SPSC ring to the peer control protocol header near the
existing peer request/response types:

- `CitusRemoteExecPeerCommandCompletionEvent`
  - `localServiceSessionId`
  - `peerServiceSessionId`
  - `commandSequence`
  - fixed-width `CitusRemoteExecCommandCompletion`
- `CitusRemoteExecPeerCommandCompletionRing`
  - `protocolVersion`
  - `publishedEpoch`
  - `consumedEpoch`
  - eight fixed power-of-two event slots

The ring should carry command completion events only. Tuple/result sink
descriptors remain inside `CitusRemoteExecCommandCompletion` when the command
state/result flags require them.

### 2. Allocate And Register The Requester-Side Ring

Add requester-service state for a peer command completion ring. The ring should
be local to the requester service and registered for RDMA writes before the peer
command session or payload-bound command endpoint is opened.

The requester service creates and owns the shared-memory object, following the
same lifetime pattern as the local frontend completion mailbox. The backend
caller maps the same shared-memory ring into `RemoteExecutionSessionData` and
waits on it directly.

For tuple COPY, the command endpoint is currently obtained as a side effect of
the tuple/payload stream open, not through the command-only open path. Therefore
the ring binding must cover the payload-bound command endpoint too, not only
`OPEN_COMMAND_SESSION`.

Implementation choices:

- extend `CitusRemoteExecPeerOpenRequest` with a requester completion-ring
  descriptor for payload-bound command sessions; and
- extend `CitusRemoteExecPeerOpenCommandSessionRequest` with the same descriptor
  for command-only sessions.

The responder should store the descriptor in its `TupleSinkServiceSessionState`
alongside existing peer command endpoint state.

Add corresponding cleanup paths:

- requester service unmaps/unlinks the local ring when the service session is
  reset,
- backend `RemoteExecutionSession` unmaps/closes its mapping when the session is
  closed,
- responder deregisters any scratch/source memory it uses for publishing events.

### 3. Publish Completion From Backend-Mailbox Consumption

When the responder service consumes a backend completion in
`TupleSinkServiceConsumeCompletionMailbox()`, it should publish push-visible
non-client-SQL peer completions to the bound peer completion ring instead of
setting `terminalCompletionPendingPeerPoll`.

The publish order should be:

1. fill a fixed-width completion event in a registered scratch buffer,
2. RDMA-write the event slot,
3. publish `publishedEpoch` after the slot write,
4. update local delivery bookkeeping only after the RDMA publishes succeed.

This mirrors the local/client-SQL pushed completion pattern but targets the peer
completion ring rather than `CitusRemoteExecClientCompletionMailbox`.

### 4. Add Requester-Side Consumption

Add a requester-side service helper that consumes events from the local peer
completion ring and records scheduler/progress facts if the service needs them.
The backend-facing wait path should not depend on this helper for normal command
completion.

Add direct ring mapping into `RemoteExecutionSessionData` in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:44`.
`WaitForRemoteExecutionCommandCompletion()` should observe the local peer
completion ring directly, copy matching events in order, and advance
`consumedEpoch`. It should validate `(localServiceSessionId, peerServiceSessionId,
commandSequence)` before treating an event as the requested completion.

### 5. Route Tuple COPY Waits Through Push Completion

Change `PollRemoteExecutionCommandCompletionThroughLocalService()` so remote peer
sessions prefer the directly mapped peer-completion ring path. It should submit
a peer `POLL_COMMAND_COMPLETION` only when the push ring is unavailable or an
explicit debug fallback is enabled.

This keeps callers such as `FinishExperimentalTupleSinkWorkerInsert()` unchanged
while the lower command-completion implementation changes.

### 6. Replace Terminal-Pending Retirement State

Replace `terminalCompletionPendingPeerPoll` with delivery-oriented state, for
example:

- completion not yet consumed from backend mailbox,
- completion consumed and peer-push delivery pending,
- completion delivered to requester ring.

Session retirement should wait for delivery to the bound destination, not for a
future peer poll request.

### 7. Demote Poll Completion

After validation, remove `POLL_COMMAND_COMPLETION` from normal backend-to-backend
paths:

- local-control async code should not build
  `CITUS_REMOTE_EXEC_PEER_REQUEST_POLL_COMMAND_COMPLETION` for ordinary remote
  command waits,
- responder-side peer poll handling remains only as a debug/compatibility path,
- comments mentioning mandatory follow-up poll should be rewritten.

## Closed Choices

- **Local backend wait surface:** direct backend-visible ring mapping from the
  first implementation. Do not add a local service-mediated completion wait as
  the normal path.
- **Publication notification:** use metadata/event write followed by
  `publishedEpoch` update as the correctness rule. The command caller busy-polls
  the local epoch, matching the existing local frontend completion-ring design.
  Do not require a new interrupt-style notification for this milestone.
- **Ring overflow behavior:** use an 8-slot SPSC ring and never overwrite unread
  state. Since the current command plane keeps one command in flight per session,
  full-ring pressure is unexpected; the producer should spin or report a
  diagnostic failure rather than corrupting completion order.

## Verification

Validate in this order:

1. backend-to-backend Citus tuple COPY correctness,
2. backend-to-backend Citus tuple COPY warmed performance against the current
   baseline,
3. pgbench Homer c1/c4 smoke to catch client-SQL command/completion regressions,
4. local and remote Homer basebackup smoke to catch shared peer/control regressions,
5. grep service logs for unexpected fallback peer `POLL_COMMAND_COMPLETION`.

Do not claim this prerequisite complete until the normal tuple COPY path reaches
terminal command completion without a peer `POLL_COMMAND_COMPLETION` request.
