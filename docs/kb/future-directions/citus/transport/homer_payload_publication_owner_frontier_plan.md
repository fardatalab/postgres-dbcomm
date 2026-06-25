# Homer Payload Publication Owner And Frontier Plan

## Scope

- **What this doc explains**: the design correction around Homer payload
  publication grants, local send-CQ owner tracking, byte-ring tail publication,
  and the invariant that every posted payload WR must already have durable
  service-owned retirement state.
- **What this doc does NOT cover**: the full Slice 6F6
  `payloadTransportBroken` deletion plan, SQL semantic `ERROR+EOS`, basebackup
  local failure handling, or the eventual command/control/mailbox unification
  except where those topics constrain payload publication.
- **Primary directory**: `docs/kb/future-directions/citus/transport/`
- **Doc type**: `future-direction`

This note is a focused companion to
[`homer_completion_publication_v27_recovery_plan.md`](homer_completion_publication_v27_recovery_plan.md).
It records the June 25, 2026 discussion that clarified why the current
`payloadCompletion*` queue exists, why its naming is misleading, and how the
target design should make owner reservation and byte-ring tail derivation
structural instead of relying on post-hoc failure handling.

## Current Code Facts

### Payload substrates are still split

The current service has two physical payload publication shapes:

- **Fixed-slot payload path**:
  `HomerServicePumpOutgoingPayloadStream()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26350`
  builds a descriptor array for one selected payload grant. Adjacent local and
  remote ranges can be coalesced at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26816`.
  `TupleSinkServicePostPeerRegisteredPayloadBatchWithImmediateResultRdma()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8143`
  posts one verbs WR per payload descriptor and changes the final payload WR to
  `IBV_WR_RDMA_WRITE_WITH_IMM` at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8300`.

- **Byte-ring payload path**:
  `HomerServicePumpOutgoingByteRingPayload()` in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25537`
  publishes tuple-view and basebackup byte-ring records. It builds
  `TupleSinkServicePeerPayloadWriteDescriptor` entries from in-band transport
  records and coalesces only byte-contiguous local and remote ranges at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:26120`.
  `TupleSinkServicePostPeerRegisteredPayloadBatchWithTailImmediateResultRdma()`
  in
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8351`
  posts the payload WRs and then appends a separate 8-byte
  `IBV_WR_RDMA_WRITE_WITH_IMM` tail publication at
  `/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:8526`.

This split is real today. The byte-ring is the desired common payload RDMA
substrate, but fixed-slot payload support and command/completion/control
mailbox paths have not all been rewritten on top of one byte-ring abstraction.
Multi-slot command/completion mailboxes are ring-like, but they are still
separate typed mailbox structures rather than the same payload byte-ring
protocol.

### Descriptor does not mean SGE

`TupleSinkServicePeerPayloadWriteDescriptor` in
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.h:146`
is a service-level payload write descriptor. Today each descriptor normally
becomes one verbs WR. When `prependGeneratedTransportHeader` is true, that WR
uses two SGEs: a generated transport header and the payload bytes. The QP
advertises `CITUS_REMOTE_EXEC_PEER_MAX_SEND_SGE` as `2` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:91`,
so arbitrary non-contiguous descriptor ranges cannot be collapsed into one WR
without either copying or changing QP/SGE assumptions.

The helper `HomerServiceAppendOutgoingBaseBackupFragmentWrite()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:14726`
uses this two-SGE WR shape for diagnostic basebackup fragmentation. The
generated fragment header comes from a lane-owned registered header pool, and
the semantic payload bytes remain in the producer byte-ring.

### The current `payloadCompletion*` queue is local send-owner state

The fields named `payloadCompletionTokens[]`, `payloadCompletionByteTails[]`,
`payloadCompletionSemanticTails[]`, `payloadCompletionWrCounts[]`, and
`payloadCompletionOwnerStates[]` live in `HomerPayloadStreamState` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1420`.
They do **not** track the remote byte-ring tail itself. They track local
send-CQ ownership for signaled payload publication grants.

One entry represents one signaled publication grant:

- `payloadCompletionTokens[]`: the expected local send-CQE token.
- `payloadCompletionByteTails[]`: source byte frontier that can be
  abort-released or success-released after local NIC ownership is done.
- `payloadCompletionSemanticTails[]`: semantic object frontier completed by
  that grant.
- `payloadCompletionWrCounts[]`: number of WQEs represented by the one
  signaled CQE.
- `payloadCompletionOwnerStates[]`: one-way owner state such as `POSTED`,
  `RETIRED`, or `ABORTED`.

`HomerServiceTrackPayloadCompletion()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13693`
currently records this owner after the post call. `HomerServiceCompleteTrackedPayload()`
at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13762`
treats a completed token as a cumulative RC-QP completion frontier and retires
every owner up to that token. Reset cleanup uses
`HomerServiceAbortTrackedPayloadSendOwners()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:23502`
to transition remaining `POSTED` owners to `ABORTED` without advancing
successful delivery frontiers.

The current names are misleading. These fields should be thought of as
payload-send-owner state, not payload-completion semantic state.

## Decisions

### Keep payload token in immediate data

Payload WIMM immediate data is already the receiver-issued payload doorbell
token. Slice 2E defines the immediate token as a stream-index plus token
generation capability. The recv-CQ dispatcher uses that token for O(1) stream
lookup and stale-generation rejection.

Do not repurpose immediate data to carry a byte-ring tail:

- the tail is 64-bit while immediate data is 32-bit;
- the immediate token is the transport binding and lifetime proof;
- O(1) dispatch should continue to depend on the immediate token.

### Future byte-ring publication should derive the tail from in-band records

The current byte-ring path uses a conservative extra tail WIMM. The target
design is to eliminate that extra 8-byte tail WR for the normal byte-ring
payload path by making the final payload WR itself `WRITE_WITH_IMM`, as the
fixed-slot path already does.

That requires a reliable receiver rule:

```text
payload WIMM token identifies the stream;
the receiver starts from its local arrived/drain cursor;
the receiver parses complete in-band transport records written by the grant;
the receiver derives the new arrived tail from the last complete record;
the receiver does not need the immediate value to carry the tail.
```

The existing transport header already carries enough per-record shape for the
normal case: header bytes, payload bytes, record kind, record ordinal, flags,
and stream generation. The future implementation should tighten this into an
explicit byte-ring publication contract.

### Known caveats are protocol-design work, not blockers

The following caveats should be addressed by the byte-ring protocol, not by
retaining the separate tail WIMM forever:

- **Wrap gaps**: current sender can leave trailing remote bytes unused when a
  complete byte-ring record cannot fit before wrap. The receiver must have an
  explicit rule for recognizing and skipping the gap while deriving the next
  tail.
- **Fragmented basebackup records**: generated fragment headers can be carried
  as a second SGE in the same WR. The receiver should still treat each fragment
  as an in-band byte-ring transport record and derive the frontier from record
  parsing.
- **Empty publication**: there is no current normal need for a
  frontier-advance-without-payload publication on this path. If one appears
  later, it should be modeled explicitly rather than weakening the normal
  payload WIMM contract.
- **O(1) dispatch**: immediate data remains the payload token. Tail derivation
  must not require scanning stream tables.

### Reserve local send-owner state before posting

The service must not permit this state:

```text
ibv_post_send accepted payload WRs
but the service failed to enqueue local retirement ownership
```

The current code tries to avoid that with capacity checks before posting, for
example at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:25732`.
That is not strong enough as an ownership boundary. The owner record should
exist before the NIC can later produce a CQE for it.

Target invariant:

```text
No payload WR may be posted unless its local send-owner slot has already been
reserved and populated with the token/frontiers/WR count needed for retirement
or abort cleanup.
```

Target post sequence:

```text
reserve payload send-owner slot
build WR chain using the reserved token
post WR chain

post fully succeeds:
    RESERVED -> POSTED
    advance posted frontiers

zero WR accepted, retryable pressure:
    RESERVED -> FREE
    mark blocked/rearm

zero WR accepted, hard connection/QP error:
    RESERVED -> FREE
    request exact reset

partial WR accepted:
    RESERVED -> POSTED/ABORT_OWNED
    request exact reset

send CQE:
    POSTED -> RETIRED

connection reset:
    POSTED -> ABORTED
```

This is primarily a correctness and debugging invariant. It also prepares the
path for future multi-threading or DPU offload, where "single service loop"
reasoning will not be enough.

### Bounded owner capacity is still useful

RC QP ordering means a later signaled grant completion retires all earlier WRs
on that QP. The code already relies on this cumulative frontier behavior in
`HomerServiceCompleteTrackedPayload()` at
`/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:13809`.

The owner queue still has value because multiple signaled grants may be
outstanding at once:

- `HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS` currently caps the number of
  outstanding signaled publication owners, despite its object-oriented name.
- `HOMER_SERVICE_PAYLOAD_MAX_OUTSTANDING_WRS` caps total WQEs represented by
  those owners.
- `CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES` caps how many payload write
  descriptors one grant may build.

The target design should rename or document these concepts as grant-owner and
WQE bounds. The scheduler grant controls the amount of payload work attempted in
one pass; the owner bounds control how much signaled publication work can remain
unretired across passes.

### Normal object and grant WR counts are different

A normal byte-ring semantic object can eventually be one payload WIMM WR when
its source and remote byte ranges are contiguous and the receiver can derive the
tail from the in-band record. That is not the same as saying every scheduler
grant is one WR.

A scheduler grant may intentionally cover multiple records. Today its maximum
physical write descriptor count is bounded by
`CITUS_REMOTE_EXEC_PEER_PAYLOAD_BATCH_MAX_WRITES`; the byte-ring path then adds
one separate tail WIMM in the current implementation. In the target
tail-derived design, the last payload descriptor in the grant should become the
WIMM publication WR, so the common grant should no longer need the extra 8-byte
tail WR. The owner entry still records the grant-level WQE count because one
signaled CQE retires the whole posted grant.

## Implementation Direction

1. Rename or clearly document `payloadCompletion*` fields as payload send-owner
   state. This can be staged separately from behavior changes.
2. Add a reserve/commit/rollback owner API:
   - reserve returns a token and durable owner slot;
   - commit marks the owner `POSTED` after a full post succeeds;
   - rollback is legal only when no WR was accepted;
   - partial post transfers the owner to reset cleanup, never to retry.
3. Move outgoing byte-ring and fixed-slot post paths to reserve-before-post.
4. Once owner reservation is structural, remove any "post succeeded but owner
   tracking failed" fatal branch.
5. Design and implement byte-ring tail derivation from in-band records:
   - keep immediate token for dispatch;
   - make final payload WR the WIMM publication WR;
   - eliminate the separate 8-byte tail WIMM from the normal byte-ring path;
   - keep a deliberately separate path only for any future empty-publication
     semantic if one is actually needed.
6. Revisit unification after payload byte-ring correctness is stable:
   command/completion/control rings can be compared against the same record-ring
   model, but they do not need to be forced into this change.

## Open Questions

- Should the byte-ring receiver derive the tail by parsing until a record is not
  complete, or should the final payload record carry an explicit grant-end
  frontier in its header?
- Should wrap gaps be represented as an explicit in-band padding record, or
  should the receiver skip to zero when the next complete header cannot fit
  before ring end?
- Is there any real close/control case that needs payload empty publication, or
  can all such events stay in control/close state machines?
- Should `HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_OBJECTS` be renamed to
  `HOMER_SERVICE_PAYLOAD_MAX_INFLIGHT_GRANTS` once code churn is acceptable?

## Related

- [`rdma_publication_visibility_and_doorbells.md`](rdma_publication_visibility_and_doorbells.md):
  publication-doorbell visibility rules behind the WIMM direction.
- [`homer_completion_publication_v27_recovery_plan.md`](homer_completion_publication_v27_recovery_plan.md):
  broader recovery plan that currently owns Slice 6F6 cleanup.
- [`homer_payload_control_unification_plan.md`](homer_payload_control_unification_plan.md):
  larger byte-ring/control/payload lifecycle unification direction.
