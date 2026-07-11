# Homer tuple DEFORM and the two-ring result relay

This note follows one SQL result from a socketless PostgreSQL backend to a Homer client. The short
version is: the backend writes PostgreSQL-aware **tuple-view** batches, the services move those bytes
between hosts, the receiving DPU deforms each packed row into an O(1)-addressable decoded image, and
the client drains that image from its own role-7 ring. No result row takes PostgreSQL's normal libpq
`DataRow` serialization path.

## 1. The problem: result tuples without libpq serialization

PostgreSQL normally sends a result through [printtup](/data/dbcomm/postgres-citus/src/backend/access/common/printtup.c:39),
whose row path formats each column for the frontend protocol. A Homer socketless backend instead installs
[RemoteExecSqlDestReceiveSlot](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1569)
as its executor destination and appends each `TupleTableSlot` with
[TryAppendTupleViewToCitusTupleSinkBatch](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1587).

The resulting payload is not PostgreSQL's native frontend/backend wire format. It is a Homer transport
record containing a [CitusTupleSinkBatchHeader](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:184)
and packed, aligned tuple-view rows. The contract itself explicitly distinguishes this intermediate
layout from both a wire-format contract and a final PostgreSQL tuple representation in
[CitusTupleViewContract](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:121).
That intermediate form preserves enough PostgreSQL layout information to reconstruct values, while
avoiding per-column text/binary output conversion and libpq framing on the producer.

## 2. The tuple-view contract

[CitusTupleViewContract](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:121) is the
schema/layout contract shared by producer, relay, and consumer. It carries a protocol version, attribute
count, relation OID, null-bitmap width, and one
[CitusTupleContractAttribute](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:94)
per column: type OID, typmod, collation, fixed/variable length, by-value status, alignment, dropped status,
and generated-column marker.

The executor supplies the authoritative `TupleDesc`. During
[RemoteExecSqlDestStartup](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1333),
the backend calls
[RemoteExecBuildTupleViewContractFromTupleDesc](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:566),
which copies those `Form_pg_attribute` fields into the fixed-width contract. This happens at destination
startup—not command submission—because only then is the real result descriptor available.

The contract travels with result metadata rather than with every row. The backend copies it into each
result-bearing [CitusRemoteExecCommandCompletion](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_completion_abi.h:35)
when it sets `CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_READY` in
[PublishCompletionToMailbox](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1851).
The client preserves it while copying/peeking completions in
[HomerClientCopyCommandCompletion](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:1178), and pgbench later passes
that per-command contract to the role-7 drain.

## 3. Tuple DEFORM

“Deform” is the receiving-DPU conversion from the packed tuple-view row to Homer's decoded delivery
image. [HomerTupleDeformBatchToDecoded](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_tuple_deform.c:294)
walks the packed null bitmap and aligned attributes using the tuple-view contract. Its output uses
[HomerDecodedTupleHeader](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_decoded_tuple_abi.h:104):
one null byte and one `Datum`-width value per attribute, with by-reference data copied into an aligned
tail and represented by offsets rather than process-local pointers. The per-tuple byte length makes the
next tuple O(1)-skippable; values are O(1)-addressable rather than requiring another packed attribute walk.

The relay wraps that primitive in
[HomerServiceTupleSourceRingProduceRecord](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:32929).
It preserves the transport envelope and record ordinal, rewrites `payloadBytes` for the decoded body,
passes `ERROR` records through verbatim, and keeps `DATA`/`EOS` as decoded batches. On the client,
[HomerDecodedTupleCursorNext](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_decoded_tuple_abi.h:175)
performs the remaining “finalize” step: inline by-value Datums are used directly and by-reference offsets
become pointers into the locally landed record.

For the smallest executable reference, build/run the
[`tuple-deform-smoke` target](/data/dbcomm/citus-dbcomm/Makefile:70). Its
[DeformAndFinalize](/data/dbcomm/citus-dbcomm/src/bin/homer_tuple_deform_smoke.c:158) exercises the same
packed-batch → deform → client-finalize round trip without the network or DMA machinery.

## 4. The two-ring relay

On the producer side,
[HomerServicePumpOutgoingDpuMirrorByteRingPayload](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:30414)
takes complete published ranges from the backend/service role-5 byte-ring mirror and RDMA-writes them,
position-preserving, into the peer's receive byte ring. It observes the peer's mirrored consumed head as
remote credit and publishes a tail only after the preceding RDMA writes, so the receiver never observes
torn payload.

On the receiving DPU,
[HomerServicePumpIncomingTupleViewDpuTwoRingRelay](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33044)
separates two different ownership and lifetime domains:

1. The **landing/source-input ring** is the receiver service's peer-written byte ring. The remote sender
   owns its produced tail; the receiving service owns `consumedHead`. Whole packed records land here.
2. The **tuple-source/output ring** is DPU-service memory returned by
   [HomerDpuDmaGetTupleSourceRingMemory](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:4470).
   The deform loop produces decoded records into it; the DPU DMA engine consumes those bytes and writes
   them into the client's host-exported role-7 destination ring.

The split is necessary because packed input and decoded output have different record sizes and different
back-pressure owners. The deform loop advances the landing `consumedHead` only after a whole packed
record has been copied/deformed; [the Loop-1 credit publication](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33352)
then lets the far sender reuse those landing bytes. Independently, the tuple-source `producedTail` may
advance only while space remains beyond the role-7 DMA `writtenTail`; a full tuple-source ring therefore
stops deform and naturally withholds upstream credit. Finally,
[HomerDpuDmaSubmitByteRingWrite](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33326)
copies decoded ranges into role 7, where the engine enforces host-consumer credit.

EOS remains in-band throughout: the backend flushes an EOS result batch in
[RemoteExecSqlDestShutdown](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1617),
the deform records it while preserving the envelope in
[HomerServicePumpIncomingTupleViewDpuTwoRingRelay](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33334),
and only after every decoded byte has been DMA-written does the relay publish the out-of-band CLOSED
terminal at [HomerDpuDmaSubmitByteRingWriteTerminal](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:33449).

The consuming client drives
[HomerClientPollSqlResultDpuReceive](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4850). It acquire-loads
the role-7 produced frontier, delivers only whole records through
[HomerClientSqlResultDeliverDecoded](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4763), advances
`consumedHead`, and publishes that head back to the DPU as host-side credit. Pgbench's
[HomerDrainPendingDpuResultRelay](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3761) supplies the
completion's contract and waits for in-band EOS; assigning the decoded first column to
[homer_last_abalance](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3805) is the end-to-end proof
that the deform result—not merely a completion—reached the application.

## 5. Stream identity and routing

The client's role-7 export is bound to one session by a nonzero `sessionUID`. During
[HomerClientOpenSqlResultReceiveStreamSelectedDpu](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4298),
the client stamps that UID into the role-7 bridge descriptor at
[descriptor[1].sessionUID](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4565) and into the receive-open
request at [request->sessionUID](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4623).

The routing intent starts as `clientSqlResultDpuRelay` (the wire field corresponding to the service's
`dpuRelayResultStream`). Protocol
[v17](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:33)
added that flag to command-session and result opens; protocol
[v18](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:34)
added `sessionUID` to both. The command-session open records both on the remote session in
[TupleSinkServiceHandleOpenCommandSession](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:35136),
and the later result peer-open copies them into
[CitusRemoteExecPeerOpenRequest](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:107)
at [TupleSinkServiceBuildPeerOpenRequestForAsyncOp](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:37232).
The receiver creates a relay stream with
[HomerServiceCreatePayloadStreamEntry](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:24799),
which persists the flag as `dpuRelayResultStream` and the UID as `dpuRelayResultSessionUID`.

At delivery time,
[HomerServiceResolveRelayResultSessionUID](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:32490)
returns that stream UID, and
[HomerDpuDmaFindDpuToHostByteRingRef](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_service_dpu_dma.c:6715)
requires an exact descriptor `sessionUID` match. There is deliberately no role-only or `sessionKey`
fallback: several clients can export role-7 rings concurrently, while the UID identifies exactly one.

## 6. Where to start reading

1. [homer_tuple_abi.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_tuple_abi.h:86) — packed tuple-view contract and record ABI.
2. [remote_execution_backend_bridge.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/remote_execution_backend_bridge.c:1333) — executor contract derivation and row production.
3. [remote_execution_peer_control_protocol.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/remote_execution_peer_control_protocol.h:107) — cross-node result-open routing fields.
4. [tuple_sink_service_process.c](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/tuple_sink_service_process.c:32919) — sender mirror and receiving two-ring deform pumps.
5. [homer_decoded_tuple_abi.h](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_decoded_tuple_abi.h:46) — decoded image and client finalize contract.
6. [homer_client.c](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4298) and [pgbench.c](/data/dbcomm/postgres-citus/src/bin/pgbench/pgbench.c:3761) — role-7 setup, drain, and visible decode proof.

### Ring-role glossary

The canonical role numbers are [HomerDpuBridgeDescriptorRole](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:99).

- **Role 1 — frontend control slot:** client-owned control request/response slot used to open and manage a selected-DPU session; exported at [descriptor 0](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:3120).
- **Role 2 — backend command mailbox:** frontend-arena command slots consumed by socketless backends; exported by [HomerFrontendAgentBuildDescriptors](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_agent.c:885).
- **Role 3 — backend completion mailbox:** backend-to-DPU completion publication for arena commands; exported beside role 2 at [completion->descriptorRole](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_agent.c:916).
- **Role 5 — SQL result byte ring:** backend/service-produced packed tuple-view source ring; exported per arena slot at [result->descriptorRole](/data/dbcomm/citus-dbcomm/src/backend/distributed/utils/homer/homer_frontend_agent.c:931).
- **Role 6 — frontend completion event:** service/DPU-to-client command-completion event line; the client exports it at [descriptor[1].descriptorRole](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:3155).
- **Role 7 — DPU-to-host payload byte ring:** client-owned host storage, DPU-produced and client-consumed; its direction and produced-tail credit-line contract are defined at [role 7](/data/dbcomm/citus-dbcomm/src/include/distributed/homer/homer_dpu_bridge_abi.h:117), and the SQL client exports it at [descriptor[1].descriptorRole](/data/dbcomm/citus-dbcomm/src/bin/homer_client.c:4543).
