# Cross-Node Peer-Session Control Path For `RemoteExecutionSession`

## Scope

- **What this doc explains**: the corrected next concrete control-plane step after the landed local `session -> sink` checkpoint: how two standalone homer services on different nodes should establish a point-to-point peer session, bind exact tuple sinks under that peer session, and drive the cross-node control path before the RDMA data path is resumed.
- **What this doc does NOT cover**: provider-specific RDMA verbs/QP setup, the final wire layout of tuple batches, or the final zero-copy receive fast path.
- **Primary directory**: `docs/kb/future-directions/citus/connection-management/`
- **Doc type**: `future-direction`

## Why this exists

The current prototype now has a clean **local** split:

- backend-visible `RemoteExecutionSession`
- service-owned compatibility session
- service-owned exact tuple sink
- local tuple-view queue/batch substrate

That landed shape is documented in [local_service_owned_tuple_sink_control_checkpoint.md](../../../implementations/citus/connection-management/local_service_owned_tuple_sink_control_checkpoint.md).

This note originally described the missing **cross-node** half of the control plane. That cross-node slice has now landed for the current tuple-sink prototype, so this note now serves mainly as:

- the rationale behind the chosen peer-session shape
- the longer-term direction beyond the landed tuple-sink checkpoint
- the place where future generalizations, such as multi-slot control rings and richer peer-session state, are still recorded

What used to be missing was the actual **cross-node** half of the control plane:

- the local service still only resolves compatibility and sink state inside one process
- the local service still does not own real remote SQL/RDMA state
- the local data movement path is still local queue mirroring in [`TupleSinkServicePumpSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1879)

So the current control plane stops at:

- local backend -> local service open in [`OpenTupleSinkSessionThroughLocalService()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:944)
- local service session matching in [`TupleSinkServiceFindReusableSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:570)
- local service exact sink matching in [`TupleSinkServiceFindSinkBySessionAndKey()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:666)

That next step has now landed for the current tuple-sink prototype. The current remaining work is narrower and is discussed below in “Remaining gaps after the landed checkpoint”.

Transport refinement from the current discussion:

- the cross-node control path should converge on the same **peer RDMA implementation substrate** that the future data plane will use
- but control messages and data payload must still use **separate logical channels/rings**
- for the current tuple-sink prototype, that means at least:
  - one control ring/channel for peer/session/sink lifecycle messages
  - one tuple-payload data ring/channel for serialized tuple batches
- if future operation classes need materially different scheduling or buffering policy, the service may add additional data-plane rings without changing the backend-visible session abstraction

## Implementation progress

The first code checkpoint for this design has now landed in
[cross_node_tuple_sink_peer_control_checkpoint.md](../../../implementations/citus/connection-management/cross_node_tuple_sink_peer_control_checkpoint.md).

What is implemented now:

- backend-side peer endpoint resolution from Citus metadata in [`BuildPeerEndpointFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:404)
- full tuple-view contract construction in [`BuildTupleViewContractFromTupleDesc()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:583)
- sender-initiated peer open before local send open succeeds in [`TupleSinkServicePeerOpenSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1008) and [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1528)
- eager peer-side receive-sink provisioning in [`TupleSinkServiceHandlePeerOpenRequest()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1150)
- peer-bound sink lifetime tracking in [`TupleSinkServiceMaybeReclaimSinkAndSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:884)
- peer close on sender-side teardown in [`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1084)
- full-contract carriage on both local open and peer open through [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:276), [`CitusRemoteExecPeerOpenRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:75), and service-side validation in [`TupleSinkServiceEnsureTupleViewContract()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2538)

Important implemented shortcut:

- the current implementation now has a persistent per-peer RDMA transport owner through [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2536), [`TupleSinkServiceSendPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3152), and [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:3365)
- after a one-time bootstrap exchange, control messages already ride over a persistent mailbox/channel whose responder-visible publication is signaled by `WRITE_WITH_IMM` in [`TupleSinkServicePublishControlMessage()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2096) and consumed in [`TupleSinkServiceTryConsumeLocalMailbox()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2048)
- the remaining shortcut is narrower: the mailbox is still one-message-at-a-time and the logical control protocol is still fixed-width request/response, not yet the richer multi-slot control ring described below

## Grounding in current code

The current local control/session layer already gives us the right semantics to extend rather than replace:

- session compatibility key construction in [`BuildControlSessionKeyFromIntent()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:272)
- exact tuple-sink key construction in [`BuildTupleSinkKeyFromOperation()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:228)
- local backend->service open request in [`CitusRemoteExecOpenSessionRequest`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:169)
- service-visible compatibility lookup key in [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:111)
- exact tuple-sink identity in [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:54)
- compatibility session table in [`TupleSinkServiceSessionState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:45)
- exact sink table in [`TupleSinkServiceSinkState`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:54)
- local open handling in [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1962)
- local close handling in [`TupleSinkServiceHandleCloseSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1089)

Important correction:

- the current fixed-width session key is already the right **semantic** lookup object for compatibility
- the exact tuple-sink key is already the right **semantic** lookup object for one concrete tuple sink under that compatibility session
- what is missing is not a third lookup meaning, but the missing **cross-node peer-session ownership and binding path**

## Corrections to the earlier draft

### 1. The cross-node control path must be point-to-point, not broadcast

The previous draft used “sink announcement” wording that was easy to read as a broadcast/global-advertisement design. That is not the intended model.

The correct target is:

- one point-to-point peer-control channel per relevant destination node
- one peer-session bind between those two services
- one or more directional exact sinks under that peer session

No cluster-wide broadcast, global sink directory, or consensus-style agreement is needed.

### 2. The cross-node session should be modeled as two-ended and bidirectional

Current Citus already behaves this way at the connection/session layer:

- one [`MultiConnection`](/data/dbcomm/citus-dbcomm/src/include/distributed/connection_management.h:162) owns both the remote connection state and the remote transaction state
- commands are sent over that same object by [`SendRemoteCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:565)
- results are received over that same object by [`GetRemoteCommandResult()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/remote_commands.c:683)
- reuse is based on compatibility and caching in [`StartNodeUserDatabaseConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:277) plus [`FindAvailableConnection()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/connection/connection_management.c:466)

So the corrected external-service model should mirror that semantic shape:

- a cross-node peer session is a two-ended object jointly represented on both services
- exact tuple sinks are directional children under that peer session
- local queue descriptors remain purely local resource attachments and never become cross-node identity objects

Important refinement:

- this semantic split does **not** require separate application-level round trips for "peer hello", "peer session bind", and "exact sink bind"
- the wire protocol can still carry session-open and initial-operation-open in one optimistic request/response pair

### 3. Backend-visible APIs should stay unchanged

The backend-visible API should remain [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:941) plus the existing session-scoped batch operations.

The cross-node control path must stay service-owned. The backend should not gain peer-session, peer-sink, or transport-management responsibilities.

### 4. Sender-initiated open is the grounded default, even if receiver-attach remains possible

Current grounded paths are sender-driven:

- original Citus opens the remote COPY sink from the sender/coordinator side in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4294)
- the current experimental sender path opens the send-side session in [`MaybeOpenExperimentalRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2559)

The reason a receive-side open exists conceptually in the current prototype is narrower:

- earlier bring-up temporarily used a worker-side SQL bootstrap seam for validation
- that seam has now been removed from the current tree, but the conceptual distinction still matters because a receive sink may already exist before a local backend starts consuming from it

So the final control-path design should optimize:

- **sender-initiated** peer open as the normal Citus-aligned case

but it is still useful to keep distinct conceptually:

- **later local backend consumption** of an already-created receive sink, because the remote service may already have created the peer-owned receive-side resources before a local backend starts polling them

### 5. Host/port still do not belong in the backend-facing session key

The session key should stay at the Citus semantic level:

- destination node id
- user/database
- op/access/tx/freshness/ownership/scope compatibility

Peer endpoint resolution belongs in the service-owned peer table, not in the backend-visible compatibility key.

## Approaches considered

### Approach A: sender-driven remote create/open

Idea:

- sender-side service opens a local send sink
- sender-side service asks the remote service to create/open the matching remote receive sink on demand

Pros:

- simple sender-centric story
- fewer peer-side pending states in the first version

Cons:

- the remote service would be opening/managing a sink that really belongs to the remote local backend
- ownership becomes ambiguous between “locally opened sink” and “peer-created sink”
- this does not match the local service-owned session->sink model we just established

### Approach B: point-to-point sink bind requests under purely local sessions

Idea:

- each service only creates/manages local sessions and local sinks
- when a local sink opens, the service sends a point-to-point bind request to the peer service for that exact sink
- the peer service either matches it against an already-open local complementary sink or stores the request until that sink exists

Pros:

- no broadcast and no global consistency layer
- preserves local ownership of local resources
- easy to layer on top of the current exact sink table

Cons:

- still frames the cross-node object primarily as “a sink bind”
- leaves the higher-level peer session implicit instead of first-class
- gives us a weaker place to hang future remote SQL/RDMA state

### Approach C: point-to-point bidirectional peer session with directional sink children

Idea:

- first establish or reuse a **peer session** between the two services using the compatibility session key
- then open/bind one or more exact directional tuple sinks under that peer session
- each side owns only its local resources, while the peer session itself is mirrored/jointly represented on both ends

Pros:

- matches the semantic shape of current Citus `MultiConnection` usage better
- avoids the “announce to everyone” reading completely
- gives one clear owner for future reusable remote state: the peer session
- keeps exact sink identity below the peer session where it belongs

Cons:

- requires one additional explicit peer-session state machine
- slightly more structure up front than a sink-only bind path

## Chosen direction

Choose **Approach C**.

That is the right next step because it preserves the correct layering:

- service-owned compatibility session remains the local semantic anchor
- cross-node peer session becomes the first real two-ended remote execution object
- exact tuple sinks remain narrower directional objects under that peer session

This avoids both wrong extremes:

- not sender-owned remote sink creation
- not cluster-wide sink advertisement/global agreement

One more refinement:

- keep the **semantic** layering as `peer session -> exact sink`
- but make the first wire message optimistic and fused so the initial peer-session creation plus initial exact-sink open can complete in one RTT when successful

## Proposed service-owned object model

### 1. Peer service state

Add one service-owned peer table keyed by logical destination node id:

- `PeerServiceState`
  - destination node id
  - resolved peer endpoint / transport hint
  - control-channel state
  - capability/version handshake state
  - heartbeat / liveness data
  - send/receive request sequence numbers

This is the service-owned endpoint/transport anchor. It is below the compatibility session layer.

### 2. Peer session state

Add one cross-node peer-session layer above exact sinks:

- `PeerRemoteExecutionSessionState`
  - local `serviceSessionId`
  - peer node id
  - peer-side `serviceSessionId`
  - [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:100)
  - peer-session state
  - refcount of active exact sinks under that peer session
  - place to hang future reusable remote SQL/RDMA state

Important distinction:

- the **local** compatibility session remains the local service’s open-time reuse object
- the **peer** session is the cross-node two-ended object created from that local session plus one peer node

### 3. Peer sink binding state

Add one exact directional sink-binding object under a peer session:

- `PeerTupleSinkBindingState`
  - local `serviceSinkId`
  - peer-side `serviceSinkId`
  - local `serviceSessionId`
  - peer-session reference
  - exact [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:54)
  - local direction and peer direction
  - full tuple-view contract
  - binding state

This is the owner of “this local exact sink is bound to that remote exact sink under this peer session”.

### 4. Full tuple-view contract

The services cannot rely on backend-local `TupleDesc` pointers. The active direction is a fixed-width full tuple-view contract carried on the peer-control path:

- `CitusTupleViewContract`
  - attribute count
  - per-attribute type/layout metadata
  - dropped/generated-omitted state
  - enough schema information for service-side semantic receive decode

This is required so the peer service can reject incompatible sender/receiver sink opens before the data plane starts and so the receive-side service can rebuild tuple-view batches without depending on backend-local `TupleDesc` state.

## Proposed cross-node peer-control protocol

Create a new fixed-width header, for example:

- `remote_execution_peer_control_protocol.h`

The protocol should stay transport-neutral even though the target is a dedicated control transport between services.

Transport clarification:

- “dedicated control transport” here means a dedicated **control channel** under the peer transport substrate, not a requirement for a wholly separate non-RDMA implementation stack
- the same peer RDMA substrate may and should later carry both control and data channels, provided the channels remain separate

### 1. Optional `PEER_HELLO` / `PEER_HELLO_ACK`

Purpose:

- protocol version check
- node identity exchange
- capability exchange
- initial liveness establishment

Important correction:

- this should **not** be a mandatory hot-path round trip
- protocol version/capability fields can be carried directly on the first open request
- keep an explicit hello only if the transport layer needs it for reconnect, capability refresh, or debugging

### 2. `PEER_OPEN_REQUEST` / `PEER_OPEN_RESPONSE`

Purpose:

- establish or reuse one point-to-point peer session for one compatibility domain on one peer node pair
- open or bind the **initial** operation under that peer session in the same optimistic request/response exchange
- let the remote service allocate the remote peer-owned state needed for that session and initial operation before acknowledging success

Payload should include:

- local node id
- local `serviceSessionId`
- [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:100)
- operation kind
- exact operation identity for the current prototype: [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:54)
- local direction
- tuple contract digest
- any transport bootstrap/capability fields needed by the eventual RDMA control path

Response should include:

- peer node id
- peer-side `serviceSessionId`
- peer-side `serviceSinkId` for the initial exact sink when the current tuple-sink prototype is used
- bind status / error code

Important semantic point:

- this is semantically doing **two** things:
  - peer-session establishment/reuse
  - initial exact-operation open/bind
- but it still preserves the internal split because the remote service should store peer-session state separately from exact sink state after open

Readiness rule:

- `PEER_OPEN_RESPONSE` must report success **only after** the remote service has allocated and bound the peer-owned resources required for the declared operation
- the initiator must not publish any data until that success response is received
- if the remote side cannot allocate the needed resources, open must fail or time out; it must not succeed in a half-ready state
- once success is reported, no further resource attachment is allowed to be required for that operation to be ready; any later local backend action is only consumer/producer use of already-created local service-owned resources

### 3. Optional `PEER_OPEN_CHILD_SINK` / `PEER_OPEN_CHILD_SINK_RESPONSE`

Purpose:

- open an additional exact sink under an already-bound peer session if and when one peer session needs more than one concurrent exact sink

Payload should include:

- local `serviceSessionId`
- peer `serviceSessionId`
- local `serviceSinkId`
- exact [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:54)
- local direction
- tuple contract digest
- queue geometry summary if needed for validation/debug

Response should include:

- peer-side `serviceSinkId`
- bind status / error code

Important correction:

- this is a point-to-point peer request to one destination service, not a broadcast
- this is **not** the local shm queue descriptor
- queue shm names are local implementation details and must never cross nodes

Pragmatic refinement:

- the current tuple-view workflow usually opens one exact sink per logical stream
- so the first implementation does **not** need this message on the critical path if `PEER_OPEN_REQUEST` already opens the initial exact sink
- keep this as an extension point rather than a mandatory first-step message

### 4. `PEER_CLOSE_SINK`

Purpose:

- notify the peer service that one exact sink is closing
- let the peer service tear down the binding cleanly and stop expecting data

### 5. `PEER_CLOSE_SESSION`

Purpose:

- optionally tear down the point-to-point peer session once no exact sinks remain under it
- can be delayed or omitted in the first implementation if idle peer sessions are retained for reuse

### 6. `PEER_ERROR`

Purpose:

- report bind failure, tuple contract mismatch, incompatible direction, or peer-session invalidation

### 7. Optional `PEER_KEEPALIVE`

Purpose:

- keep the peer control channel alive
- detect peer service death independently of batch traffic

Important refinement:

- the first implementation does **not** need a dedicated keepalive if transport errors and ordinary control/data traffic already surface peer death well enough
- add a keepalive only if idle peer-session liveness becomes a practical problem

## Matching semantics

### Peer-session match

Two cross-node session-open requests refer to the same peer session only if all of the following hold:

1. same peer node pair
2. same [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:100)

That means:

- the session key still answers only: “do I already have a compatible service session for this request?”
- the peer session is the cross-node two-ended realization of that compatibility domain

### Exact sink match under a peer session

Two exact tuple sinks match only if all of the following hold:

1. same bound peer session
2. same exact [`CitusTupleSinkKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:54)
3. complementary tuple-sink directions
4. equal tuple contract digest

That means:

- session compatibility remains broader than one exact sink
- exact sink binding remains narrower than the peer session
- one peer session may eventually own more than one exact sink even if the current tuple-view workflow usually opens only one sink per logical stream

## State machines

### Peer control channel states

- `PEER_DISCONNECTED`
- `PEER_CONNECTING`
- `PEER_HELLO_PENDING`
- `PEER_READY`
- `PEER_ERROR`

### Peer-session states

- `PEER_SESSION_LOCAL_ONLY`
- `PEER_SESSION_BINDING`
- `PEER_SESSION_READY`
- `PEER_SESSION_IDLE`
- `PEER_SESSION_ERROR`

### Exact sink-binding states

- `SINK_LOCAL_ONLY`
- `SINK_BIND_PENDING`
- `SINK_READY`
- `SINK_CLOSING`
- `SINK_CLOSED`
- `SINK_ERROR`

## End-to-end workflow

### Sender opens first: grounded Citus-aligned path

1. coordinator backend opens the send-side session locally
2. coordinator local service creates or reuses the local compatibility session and local exact send sink
3. coordinator local service sends one optimistic `PEER_OPEN_REQUEST`
4. worker peer service creates or reuses the cross-node peer session and allocates the peer-owned exact receive sink resources required for the declared operation
5. worker peer service completes `PEER_OPEN_RESPONSE` only after those peer-owned resources are ready
6. after that success response, both services may treat the initial sink as `SINK_READY` for data movement
7. a local worker backend may attach later to that already-created receive sink for consumption, but this later local attach is not part of the sender-side readiness contract

### Later local backend consumption is orthogonal to peer readiness

1. sender local service sends `PEER_OPEN_REQUEST`
2. receiver peer service creates or reuses the peer session and allocates the peer-owned exact receive sink resources needed for the declared operation
3. `PEER_OPEN_RESPONSE` succeeds only after those peer-owned resources are ready
4. a receiver backend may later open a local receive session through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:941) and begin polling the already-created local receive sink

Important correction:

- step `4` is **not** part of peer open readiness
- step `4` must not allocate missing peer-owned resources for the declared operation
- it is only later use of an already-ready local service-owned receive sink

### Receiver opens first: keep only as a supported capability

This order can still be supported:

1. worker backend opens a receive-side session locally through [`OpenRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:941)
2. worker local service creates or reuses the local compatibility session and local exact receive sink through [`TupleSinkServiceHandleOpenSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1962)
3. worker local service may either wait locally for the sender or proactively issue `PEER_OPEN_REQUEST` depending on the final workflow choice
4. later, sender-side open completes the exact sink binding under the same peer session

Important pragmatic plan:

- the first implementation should optimize the sender-initiated path because that matches original Citus sender-side remote sink open in [`StartPlacementStateCopyCommand()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:4294)
- but the protocol should still tolerate a later local backend starting to consume from an already-created receive sink so we do not unnecessarily couple cross-node readiness to local consumer scheduling

### Close

1. local backend closes through [`CloseRemoteExecutionSession()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:1026)
2. local service emits `PEER_CLOSE_SINK`
3. peer service marks the exact sink binding closed and rejects new data on that sink
4. local service reclaims local sink resources only after local handles and peer binding have both drained
5. if no exact sinks remain, either side may later emit `PEER_CLOSE_SESSION` or keep the peer session idle for reuse

## RTT expectations

### Hot peer-control channel with peer session already bound

If the peer control channel is established and the peer session already exists:

- `PEER_OPEN_REQUEST -> PEER_OPEN_RESPONSE` for the initial exact sink should be about **1 RTT**

### Hot peer-control channel without a peer session yet

If the peer control channel is established but the peer session is not:

- the application-level control path should still aim for **1 RTT** by letting `PEER_OPEN_REQUEST` create/reuse the peer session and open/bind the initial exact sink in one optimistic exchange
- any extra delay here should come only from the underlying transport establishment or from waiting for the complementary local sink, not from an unnecessary extra control RTT

So the first bind under a new peer session should still be **1 application-level RTT** in the optimistic success path.

### Cold peer-control channel

If the peer control channel is not established yet:

- the control protocol should still aim for **1 application-level RTT** by putting version/capability/session/initial-operation information into `PEER_OPEN_REQUEST`
- any extra startup delay here is transport bring-up cost, not a mandatory second or third control RTT

So cold open should be described as:

- **1 application-level RTT** for optimistic peer open
- plus any underlying transport-setup latency if no peer transport exists yet

That is acceptable because:

- this is control-path work, not hot per-batch work
- once `PEER_OPEN_RESPONSE` succeeds, the peer session and initial exact sink are already ready enough for data movement
- the protocol no longer bakes in extra round trips that the common success path does not need

## Control/data channel split beneath the peer session

The peer-session design should not be read as “one peer session equals one ring”.

The intended layering is:

- `RemoteExecutionSession` and its peer-session realization define the **semantic** compatibility domain
- exact tuple sinks define the narrower **operation** domain under that session
- one peer transport owner then exposes separate **logical channels** for control and data

For the current tuple-sink prototype, the recommended minimum is:

- one control ring/channel per direction for `PEER_OPEN`, `PEER_CLOSE`, `PEER_ERROR`, and related lifecycle/control messages
- one tuple-payload data ring/channel per direction for serialized tuple batches

If future operation kinds require different traffic classes, the service may add more data channels, for example command/result rings, without changing the session/sink semantics above them.

Important correction:

- the current fixed-width request/response semantics are no longer using a short-lived transport seam
- they already ride over the persistent one-sided control mailbox/channel described above
- a richer multi-slot control ring can still be added later on the same peer transport substrate, but that is now a future refinement rather than a prerequisite for completing the current tuple-sink control plane
- control and data should still remain separate channels so the service can prioritize lifecycle/teardown/error traffic independently from bulk tuple movement

## Recommended phased implementation plan

### Phase 1: define the peer-control protocol and peer table

Add:

- fixed-width peer-control protocol header
- `PeerServiceState` table
- peer transport/bootstrap hook in the standalone service

Do **not** change tuple batch transport yet.

### Phase 2: add fused peer open for session plus initial operation

Add:

- `PeerRemoteExecutionSessionState`
- `PEER_OPEN_REQUEST` / `PEER_OPEN_RESPONSE`
- peer-session lookup keyed by peer node + [`CitusRemoteExecSessionKey`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_control_protocol.h:100)
- initial exact-sink open/bind carried inside the same optimistic open request
- eager peer-side allocation of the resources required by the initial operation before acknowledging success

This is the first place that starts owning real cross-node remote execution state.

Implementation refinement:

- shape the peer transport state so it can later host both a persistent control ring and one or more data rings, instead of treating the current request/response control transport as the final form

### Phase 3: support pending local attach and optional additional sinks

Add:

- `PeerTupleSinkBindingState`
- tuple contract digest validation
- later local attach to an already-created peer-owned exact sink
- optional `PEER_OPEN_CHILD_SINK` path if one peer session later needs more than one concurrent exact sink

Still keep local tuple queues and local queue descriptors unchanged.

### Phase 4: switch the data path from local slot mirroring to the real cross-node transport

Replace:

- local memcpy loop in [`TupleSinkServicePumpSink()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1879)

with:

- sender service reading the local send queue
- sender service publishing serialized payload to the peer transport
- receiver service materializing into the local receive queue

At that point, the peer transport should converge on:

- a dedicated control ring/channel
- a dedicated tuple-payload ring/channel
- optional future operation-specific data rings if later operation kinds justify them

### Phase 5: optimize and simplify

After end-to-end correctness is in place:

- rename remaining `tuple_sink_*` substrate names to `tuple_sink_*`
- reduce repeated control payload by exchanging peer session ids after initial bind instead of resending the full session key every time
- move more real remote SQL/RDMA ownership under peer-session state
- add optional keepalive only if idle peer-session liveness becomes a practical problem

## Remaining gaps after the landed checkpoint

The cross-node peer-control path now exists for the tuple-sink prototype, so the earlier "missing prerequisites" list is no longer accurate. What still remains after the landed checkpoint is narrower:

1. **Persistent peer-service transport now exists, and the prototype control channel now exists too**
   - the current code creates the shared transport owner in [`TupleSinkServiceCreatePeerTransportState()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:1861)
   - it reuses persistent outgoing connections in [`TupleSinkServiceSendPeerRequestRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2562)
   - it pumps accepted incoming connections in [`TupleSinkServicePumpPeerRequestsRdma()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_peer_transport_rdma.c:2762)
   - the current control channel is a one-slot persistent mailbox, which is enough for the sender-driven tuple-sink prototype
   - what is still future work is the richer multi-slot control ring and any fuller `PeerServiceState` metadata beyond the current fixed-size transport table

2. **Peer close is still sender-driven and best-effort**
   - sender-side local close emits peer close through [`TupleSinkServicePeerCloseSinkBestEffort()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:1817)
   - receive-side local close currently does not actively tear the peer binding down

3. **Control-plane coverage is still tuple-sink-only**
   - the wrapper still rejects non tuple-sink prototype operations in [`ValidateTupleSinkSessionOpenSpec()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_session.c:156)
   - the peer-control protocol only carries tuple-sink open/close semantics in [`remote_execution_peer_control_protocol.h`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:1)

4. **The cross-node payload checkpoint and first end-to-end vertical slice now exist**
   - the service now moves payload across nodes through [`TupleSinkServicePumpOutgoingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2256) and [`TupleSinkServicePumpIncomingTuplePayload()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:2414)
   - that first payload mover intentionally transports one [`CitusTupleSinkTransportHeader`](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/tuple_sink_protocol.h:282) plus exact local send-slot bytes, and republishes them into the local receive queue, rather than introducing a second custom serializer yet
   - the real worker-side receive path now exists in [`worker_tuple_sink_insert()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/worker/homer/worker_tuple_sink_insert.c:160), and the experimental sender-side switch-over is now coupled to it in [`CitusSendTupleToPlacements()`](/data/dbcomm/citus-dbcomm/src/backend/distributed/commands/multi_copy.c:2321)
   - the remaining follow-up work now lives in [../data-movement/tuple_sink_data_plane_completion_plan.md](../data-movement/tuple_sink_data_plane_completion_plan.md), especially receive-side transport-error propagation, borrowed tuple-view access, and later batching/generalization

So the control plane is now effectively complete for the current tuple-sink prototype shape. What remains is:

- broader operation coverage
- richer peer-session/control-channel generalization
- and the follow-up robustness/performance work above the now-landed first end-to-end tuple-sink path

## Why this is the right next step before resuming the data plane

Without the cross-node peer-session path:

- the current service session layer still does not own real remote state
- the sender and receiver services cannot bind their local sinks end-to-end
- the data plane would still be forced to grow on top of local-only scaffolding

With this plan:

- the control plane becomes end-to-end first
- the cross-node object model now matches the two-ended/bidirectional semantics we actually want
- exact sink identity remains below the peer session where it belongs
- the later RDMA data path has a real control owner and exact binding state to sit on

## Related

- [remote_execution_session_control_plane.md](remote_execution_session_control_plane.md): the higher-level backend-visible session abstraction this cross-node path must serve.
- [local_service_owned_tuple_sink_control_checkpoint.md](../../../implementations/citus/connection-management/local_service_owned_tuple_sink_control_checkpoint.md): current landed local-only checkpoint that this plan extends.
- [rdma_transport_control_plane_abstraction.md](../transport/rdma_transport_control_plane_abstraction.md): lower-layer transport choices beneath this control path.
- [off_path_local_table_rdma_tuple_service_first_prototype.md](../data-movement/off_path_local_table_rdma_tuple_service_first_prototype.md): broader first-prototype data-plane direction that depends on this control path.
