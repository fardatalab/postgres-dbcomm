# Homer frontend/service separation plan

## Scope

This note is the concrete refactor plan for separating the PostgreSQL/Citus-facing Homer frontend from the Homer service/backend before the DPU migration.

The desired end state is:

- PostgreSQL and Citus backend processes include one public frontend API header, `src/include/distributed/homer/homer_frontend.h`
- those backend processes link a separately identifiable Homer frontend component/library
- the Homer service compiles and runs on its own, today as the standalone host process and later on the DPU
- both sides include only shared fixed-width ABI headers for control messages, tuple-stream records, queue attachments, completion mailboxes, and object protocols
- the current POSIX shared-memory channel is isolated behind frontend SHM files so a later DMA channel can replace it without editing ordinary Citus call sites

This plan intentionally does **not** introduce a runtime transport vtable in the first pass. The first pass should be mechanical: move functions into better files, update includes, and update the build boundary. A `HomerFrontendTransportOps`-style vtable can be introduced later only if SHM and DMA need to coexist in one binary or be selected at runtime.

## Clarification: what is the new API?

The new API is **not** a new semantic API in the first pass.

The first-pass public API is the current `remote_execution_session.h` surface moved to a new source-of-truth header named:

```text
src/include/distributed/homer/homer_frontend.h
```

That means the first pass may keep existing type/function names such as:

```c
RemoteExecutionSession
RemoteSendBatch
RemoteRecvBatch
RemoteTupleView
RemoteExecutionSessionIntentSpec
RemoteExecutionOperationSpec
RemoteExecutionCommandSpec
OpenRemoteExecutionSession
CloseRemoteExecutionSession
StartRemoteExecutionCommand
AppendTupleViewToRemoteSendStream
PollRemoteRecvBatch
BorrowNextRemoteTupleView
ReleaseRemoteRecvBatch
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

The first-pass API change is the **header and compilation boundary**, not a broad rename. A later cleanup may rename these to `HomerSession`, `HomerOpenSession`, etc., but doing that during the boundary refactor would make the diff larger without improving DPU readiness.

There should be no temporary `remote_execution_session.h` compatibility shim in this plan. The old header should be moved/deleted and all call sites should be updated to include `homer_frontend.h` directly. Any missed include should fail to compile, which is useful during this refactor.

Recommended mechanical first step:

```bash
git mv src/include/distributed/homer/remote_execution_session.h \
       src/include/distributed/homer/homer_frontend.h
```

Then update the include guard and comment block inside the moved header.

## Current host-backend path, summarized

Today, PostgreSQL/Citus backend code calls the public-ish session API from `remote_execution_session.h`. The implementation in `remote_execution_session.c` does all of the following directly:

1. constructs Citus/Homer session intent and tuple-sink operation specs
2. maps the well-known local control shared-memory object
3. claims a control slot
4. writes open/close/start/poll/report control requests into that slot
5. spins until the standalone service publishes a response
6. maps the service-owned peer completion ring
7. opens a local tuple queue endpoint from a queue descriptor returned by the service
8. encodes `TupleTableSlot` values into tuple-view records
9. publishes byte-ring tails for send batches
10. polls receive records, decodes tuple views, and provides borrow/copy-out APIs to the worker
11. owns Citus transaction-finalization callbacks for session-owned worker transactions

The refactor should split these concerns without changing behavior.

## Target high-level layout

```text
PostgreSQL/Citus call sites
  multi_copy.c
  worker_tuple_sink_insert.c
  transaction_management.c
  other ordinary backend callers
        |
        v
Public frontend API
  src/include/distributed/homer/homer_frontend.h
        |
        v
Host frontend implementation/library
  homer_frontend.c
  homer_frontend_control.c
  homer_frontend_shm.c
  homer_tuple_queue_frontend.c
  homer_tuple_view_codec.c
  homer_citus_policy.c
  homer_citus_xact.c
        |
        v
Shared ABI headers
  homer_abi_version.h
  homer_tuple_abi.h
  homer_queue_abi.h
  homer_control_abi.h
  homer_completion_abi.h
  homer_shm_channel_abi.h
  homer_basebackup_abi.h
        |
        v
Homer service/backend
  tuple_sink_service_process.c first, later split into service modules
  remote_execution_peer_transport_rdma.c service-side transport
  service-owned session/sink tables
  service scheduler
  service-side tuple decode/rebuild
```

## New and renamed files

### 1. Public frontend API

#### `src/include/distributed/homer/homer_frontend.h`

This is the only public API header ordinary PostgreSQL/Citus backend callers should include.

First-pass contents are the current contents of `remote_execution_session.h`, with these edits:

- update the header comment to describe the host-side Homer frontend API
- update the include guard to `HOMER_FRONTEND_H`
- include only host-backend dependencies needed by the API, currently `postgres.h`, `executor/tuptable.h`, `utils/rel.h`, and shared Homer ABI headers
- keep existing public type/function names for the first mechanical pass
- add no raw SHM types, no fd/mmap fields, no RDMA types, and no service-internal table structs

Current public declarations to keep here initially:

```text
RemoteExecOpKind
RemoteExecAccessKind
RemoteExecTxPolicy
RemoteExecFreshnessPolicy
RemoteExecOwnershipPolicy
RemoteExecExecutionLane
RemoteExecMetadataPolicy
RemoteExecPlacementScope
RemoteExecTupleSinkDirection
RemoteExecTupleSinkOperationSpec
RemoteExecutionOperationSpec
RemoteExecutionSessionIntentSpec
RemoteExecutionSession
RemoteSendBatch
RemoteRecvBatch
RemoteTupleView
RemoteExecutionSendReserveStatus
RemoteExecutionSendAppendStatus
RemoteExecCommandKind
RemoteExecCommandState
RemoteExecCopyIngestCommandSpec
RemoteExecTxBeginAttachCommandSpec
RemoteExecTxPrepareCommandSpec
RemoteExecSqlResultMode
RemoteExecSqlExecuteCommandSpec
RemoteExecutionCommandSpec
RemoteExecPostCommandState
RemoteExecutionCommandCompletion
```

Current public functions to keep here initially:

```text
InitRemoteTupleSinkSendSessionIntent
InitRemoteTupleSinkReceiveSessionIntent
InitRemoteSqlCommandSessionIntent
InitRemoteClientSqlSessionIntent
InitRemoteTupleSinkSendOperationSpec
InitRemoteTupleSinkReceiveOperationSpec
InitRemoteCopyIngestFromTupleSinkCommandSpec
InitRemoteTransactionBeginAttachCommandSpec
InitRemoteClientSqlTransactionBeginCommandSpec
InitRemoteTransactionPrepareCommandSpec
InitRemoteTransactionCommitCommandSpec
InitRemoteTransactionAbortCommandSpec
InitRemoteSqlExecuteCommandSpec
RemoteExecutionSessionOpenSpecToString
RemoteExecutionSessionPrototypeEnabled
RemoteExecutionSessionLoopbackValidationEnabled
OpenRemoteExecutionSession
CloseRemoteExecutionSessionDataPlane
CloseRemoteExecutionSession
StartRemoteExecutionCommand
PollRemoteExecutionCommandCompletion
WaitForRemoteExecutionCommandCompletion
ReportRemoteExecutionSessionPostCommandState
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
TryReserveRemoteSendBatch
ReserveRemoteSendBatch
TryAppendTupleViewToRemoteSendBatch
AppendTupleViewToRemoteSendBatch
SubmitRemoteSendBatch
DiscardRemoteSendBatch
AppendTupleViewToRemoteSendStream
FlushRemoteSendStream
PollRemoteRecvBatch
RemoteExecutionSessionReceivePeerClosed
RemoteRecvBatchTupleCount
BorrowNextRemoteTupleView
ReleaseBorrowedRemoteTupleView
CopyOutNextRemoteTupleView
ReleaseRemoteRecvBatch
```

Move the frontend-visible tuple-sink GUC declarations here as well, so ordinary call sites do not include the queue substrate header just to read a frontend toggle:

```text
EnableExperimentalTupleSinkRouting
EnableExperimentalTupleSinkLoopbackValidation
ExperimentalTupleSinkSlotCapacityBytes
ExperimentalTupleSinkBatchTupleTarget
ExperimentalTupleSinkUseExplicitBatchApi
```

Longer-term rename is optional and should be a separate patch after the boundary is clean.

### 2. Frontend implementation coordinator

#### `src/backend/distributed/utils/homer/homer_frontend.c`

Recommended mechanical step:

```bash
git mv src/backend/distributed/utils/homer/remote_execution_session.c \
       src/backend/distributed/utils/homer/homer_frontend.c
```

After the move, gradually delete moved-out static helpers as later phases extract them.

This file should end up containing the frontend session coordinator only:

```text
OpenRemoteExecutionSession
CloseRemoteExecutionSessionDataPlane
CloseRemoteExecutionSession
StartRemoteExecutionCommand
PollRemoteExecutionCommandCompletion
WaitForRemoteExecutionCommandCompletion
ReportRemoteExecutionSessionPostCommandState
TryReserveRemoteSendBatch
ReserveRemoteSendBatch
TryAppendTupleViewToRemoteSendBatch
AppendTupleViewToRemoteSendBatch
SubmitRemoteSendBatch
DiscardRemoteSendBatch
AppendTupleViewToRemoteSendStream
FlushRemoteSendStream
PollRemoteRecvBatch
RemoteExecutionSessionReceivePeerClosed
RemoteRecvBatchTupleCount
BorrowNextRemoteTupleView
ReleaseBorrowedRemoteTupleView
CopyOutNextRemoteTupleView
ReleaseRemoteRecvBatch
RemoteExecutionRaiseTupleSinkErrorRecord
RemoteExecutionSessionCpuRelax
RemoteSendBatchTupleCount
RemoteExecutionSessionCurrentCopyCommandFailed
WaitForRemoteSendBatchReserve
FlushRemoteSendStreamInternal
AppendTupleViewToRemoteSendStreamInternal
RemoteExecutionCommandCompletedSuccessfully
WaitForRemoteExecutionCommandSuccess
```

It should **not** contain, after extraction:

```text
shm_open
mmap
munmap
ftruncate
close of SHM fd fields
CitusRemoteExecControlRegion slot-scanning logic
CitusRemoteExecControlSlot claim/publish/wait logic
peer completion ring mmap logic
TupleDesc-to-contract builder internals
tuple row encode/decode internals
Citus transaction-finalization list internals
RDMA headers or RDMA symbols
```

#### `src/backend/distributed/utils/homer/homer_frontend_internal.h`

Private header included only by frontend implementation files.

Move private structs here:

```text
RemoteExecutionSessionData
RemoteExecutionBatchData
```

But adjust fields as extraction progresses:

- replace raw peer completion ring fd/mapping fields with a private `HomerPeerCompletionConsumer` struct from `homer_frontend_shm.h`
- replace direct `CitusTupleSinkHandle` naming with the queue-frontend handle type once `tuple_sink_service.c` is renamed
- keep service ids and high-level session state here because they are frontend session state, not public API

### 3. Frontend SHM channel

#### `src/backend/distributed/utils/homer/homer_frontend_shm.h`

Private frontend header for the current POSIX shared-memory host-backend-to-service channel.

First-pass structs:

```c
typedef struct HomerShmControlClient
{
    int shmFileDescriptor;
    Size mappingBytes;
    void *mappingAddress;
    CitusRemoteExecControlRegion *controlRegion;
} HomerShmControlClient;

typedef struct HomerPeerCompletionConsumer
{
    int fileDescriptor;
    Size mappingBytes;
    void *mappingAddress;
    CitusRemoteExecPeerCommandCompletionRing *ring;
    char shmName[CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES];
} HomerPeerCompletionConsumer;
```

First-pass function surface:

```text
HomerShmControlClientOpen
HomerShmControlClientClose
HomerShmControlReserveSlot
HomerShmControlPublishRequest
HomerShmControlWaitForResponse
HomerShmControlCheckResponseHeader
HomerShmControlRoundTrip
HomerShmPeerCompletionRingDescriptorValid
HomerShmPeerCompletionConsumerMap
HomerShmPeerCompletionConsumerUnmap
HomerShmTryConsumePeerCompletion
```

For the first patch, the function bodies can be direct moves/renames of the existing helpers from `remote_execution_session.c`.

#### `src/backend/distributed/utils/homer/homer_frontend_shm.c`

Move these existing helpers from `remote_execution_session.c`:

```text
RemoteExecutionControlState
RemoteExecutionControlAtomicLoadU32
RemoteExecutionControlAtomicLoadU64
RemoteExecutionControlAtomicStoreU64
RemoteExecutionControlAtomicFetchOrU64
RemoteExecutionControlAtomicStoreU32
RemoteExecutionControlCompareExchangeU32
MapRemoteExecutionControlRegion
UnmapRemoteExecutionControlRegion
ReserveRemoteExecutionControlSlot
PublishRemoteExecutionControlRequest
WaitForRemoteExecutionControlResponse
RemoteExecutionControlCheckResponseHeader
RemoteExecutionPeerCommandCompletionRingDescriptorValid
MapRemoteExecutionPeerCommandCompletionRing
UnmapRemoteExecutionPeerCommandCompletionRing
TryConsumeRemoteExecutionPeerCommandCompletion
```

Acceptable first-pass approach:

- keep old static names during the first move if that reduces risk
- rename to `HomerShm*` in a follow-up cleanup
- do not add a transport vtable yet

This file is the exact place a later `homer_frontend_dma.c` should replace. The high-level frontend should call a small internal channel surface, not call POSIX SHM functions directly.

### 4. Frontend control message builders

#### `src/backend/distributed/utils/homer/homer_frontend_control.h`

Private header for building semantic control requests and converting control responses into frontend API structs.

Expected functions:

```text
BuildTupleSinkKeyFromOperation
BuildPlacementAccessDescriptorFromOperation
BuildControlSessionKeyFromIntent
BuildPeerEndpointFromIntent
RemoteExecutionControlDirectionCode
RemoteExecutionControlCommandKindCode
RemoteExecutionCommandKindFromControl
RemoteExecutionCommandStateFromControl
RemoteExecutionPostCommandStateFromControl
RemoteExecutionFillCommandCompletion
RemoteExecutionControlPostCommandStateCode
OpenTupleSinkSessionThroughLocalService
OpenCommandSessionThroughLocalService
CloseTupleSinkSessionThroughLocalService
CloseRemoteExecutionCompatibilitySessionThroughLocalService
ReportTupleSinkSessionPostCommandStateThroughLocalService
StartRemoteExecutionCommandThroughLocalService
PollRemoteExecutionCommandCompletionThroughLocalService
```

#### `src/backend/distributed/utils/homer/homer_frontend_control.c`

Move those helpers from `remote_execution_session.c`.

This file should call `homer_frontend_shm.c` functions in the first pass. It should not know about DMA and should not expose a runtime vtable.

Boundary rule:

- `homer_frontend_control.c` knows the shared control ABI structs
- `homer_frontend_shm.c` knows how those structs are transported through the current SHM control region
- `homer_frontend.c` knows session semantics and calls control helpers

### 5. Citus/Postgres policy helpers

#### `src/backend/distributed/utils/homer/homer_citus_policy.h`

Private or semi-private header for constructing frontend API specs from Citus/Postgres backend state.

#### `src/backend/distributed/utils/homer/homer_citus_policy.c`

Move from `remote_execution_session.c`:

```text
ValidateTupleSinkSessionOpenSpec
ValidateSqlCommandSessionOpenSpec
InitRemoteExecutionSessionIntentDefaults
InitRemoteTupleSinkSendSessionIntent
InitRemoteTupleSinkReceiveSessionIntent
InitRemoteSqlCommandSessionIntent
InitRemoteClientSqlSessionIntent
InitRemoteTupleSinkSendOperationSpec
InitRemoteTupleSinkReceiveOperationSpec
InitRemoteCopyIngestFromTupleSinkCommandSpec
InitRemoteTransactionBeginAttachCommandSpec
InitRemoteClientSqlTransactionBeginCommandSpec
InitRemoteTransactionPrepareCommandSpec
InitRemoteTransactionCommitCommandSpec
InitRemoteTransactionAbortCommandSpec
InitRemoteSqlExecuteCommandSpec
RemoteExecutionSessionOpenSpecToString
RemoteExecutionSessionPrototypeEnabled
RemoteExecutionSessionLoopbackValidationEnabled
RemoteExecutionSessionDirectionName
RemoteExecutionOpKindName
RemoteExecutionExecutionLaneName
RemoteExecutionMetadataPolicyName
RemoteExecutionCommandKindName
```

This file may include Citus/Postgres headers such as transaction management, metadata cache, node lookup, user/database identity, and GUCs. It is host-frontend code, not shared ABI and not service code.

### 6. Citus transaction-finalization integration

#### `src/backend/distributed/utils/homer/homer_citus_xact.h`

Declares:

```text
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

These functions are also declared in `homer_frontend.h` for ordinary callers during the first pass, but the implementation should live in the xact file.

#### `src/backend/distributed/utils/homer/homer_citus_xact.c`

Move from `remote_execution_session.c`:

```text
RemoteExecutionTxFinalizationEntry
RemoteExecutionControlRequestSequence if it remains only tx-related, otherwise keep it in SHM/control
RemoteExecutionTxFinalizationConnectionOrdinal
RemoteExecutionTxFinalizationList
DisarmRemoteExecutionTxFinalizationEntry
EnsureRemoteExecutionSessionReadyForFinalization
StartAndWaitRemoteExecutionTransactionCommand
RegisterRemoteExecutionSessionForTransactionFinalization
RemoteExecutionSessionsPrepareAtPreCommit
RemoteExecutionSessionsCommitAtCommit
RemoteExecutionSessionsAbortAtAbort
```

If `RemoteExecutionControlRequestSequence` is still used by the SHM control slot code, move it to `homer_frontend_shm.c` instead of this file.

### 7. Frontend tuple queue attachment

The current `tuple_sink_service.h/.c` name is misleading for the frontend library. It is not the service process; it is the host-side tuple queue/batch substrate used by the frontend after the service returns a queue descriptor.

Recommended mechanical moves:

```bash
git mv src/include/distributed/homer/tuple_sink_service.h \
       src/include/distributed/homer/homer_tuple_queue_frontend.h

git mv src/backend/distributed/utils/homer/tuple_sink_service.c \
       src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c
```

#### `src/include/distributed/homer/homer_tuple_queue_frontend.h`

First-pass contents are the current queue/batch substrate API, with the header comment changed to make clear this is host frontend queue attachment code.

Keep current names initially to reduce risk, or rename only after the file move compiles.

Current declarations to keep/move:

```text
CitusTupleSinkDirection
CitusTupleSinkReserveStatus
CitusTupleSinkAppendStatus
CitusTupleSinkRecordStatus
CitusTupleSinkHandle
CitusTupleSinkBatchHandle
CitusTupleSinkEnabled
InitExplicitCitusTupleSinkKey
OpenCitusTupleSink
ResetCitusTupleSinkSendHandle
CloseCitusTupleSink
TryReserveCitusTupleSinkBatch
ReserveCitusTupleSinkBatch
TryAppendTupleViewToCitusTupleSinkBatch
AppendTupleViewToCitusTupleSinkBatch
SubmitCitusTupleSinkBatch
DiscardCitusTupleSinkBatch
PublishCitusTupleSinkErrorRecord
FinalizeCitusTupleSinkGeneration
MarkCitusTupleSinkPeerClosed
PollCitusTupleSinkRecord
PollCitusTupleSinkBatch
CitusTupleSinkRecordError
CitusTupleSinkReceivePeerClosed
CitusTupleSinkReceivePeerFailed
CitusTupleSinkReceiveTerminalState
CitusTupleSinkBatchTupleCount
BorrowNextTupleViewFromCitusTupleSinkBatch
ReleaseBorrowedTupleViewFromCitusTupleSinkBatch
ReleaseCitusTupleSinkBatch
CopyOutNextTupleViewFromCitusTupleSinkBatch
```

Move the frontend-visible GUC declarations out to `homer_frontend.h` and define them in `homer_citus_policy.c` or `homer_tuple_queue_frontend.c`. Ordinary callers should not include the queue substrate header just to read GUCs.

Suggested later rename map, after the mechanical move is stable:

```text
CitusTupleSinkHandle                    -> HomerTupleQueueEndpoint
CitusTupleSinkBatchHandle               -> HomerTupleBatch
OpenCitusTupleSink                      -> HomerTupleQueueOpen
CloseCitusTupleSink                     -> HomerTupleQueueClose
TryReserveCitusTupleSinkBatch           -> HomerTupleQueueTryReserveSendBatch
TryAppendTupleViewToCitusTupleSinkBatch -> HomerTupleQueueTryAppendTuple
SubmitCitusTupleSinkBatch               -> HomerTupleQueueSubmitBatch
PollCitusTupleSinkRecord                -> HomerTupleQueuePollRecord
BorrowNextTupleViewFromCitusTupleSinkBatch -> HomerTupleBatchBorrowNext
ReleaseCitusTupleSinkBatch              -> HomerTupleBatchRelease
```

#### `src/backend/distributed/utils/homer/homer_tuple_queue_frontend.c`

First-pass contents are the current `tuple_sink_service.c` body, minus helpers later extracted into `homer_tuple_view_codec.c`.

This file owns:

```text
host-side mapping of queue attachment descriptors
byte-ring control pointer setup
send reservation against consumedHead/publishedTail
send batch submit/publication into the local queue
receive polling from the local queue
receive batch release and consumedHead advancement
terminal state reads from the local queue control block
borrow/release bookkeeping at the batch-handle level
```

This file should not own:

```text
local control-slot request/response mechanics
peer RDMA transport
service-owned session/sink tables
service scheduler
ordinary Citus transaction finalization
```

### 8. Tuple-view codec

#### `src/include/distributed/homer/homer_tuple_view_codec.h`

Private frontend/service-support header for tuple contract construction and local tuple-view row encode/decode.

The first pass can keep helper names private. Do not expose these to ordinary Citus call sites.

#### `src/backend/distributed/utils/homer/homer_tuple_view_codec.c`

Move from `remote_execution_session.c`:

```text
BuildTupleViewContractFromTupleDesc
```

Move from current `tuple_sink_service.c` / future `homer_tuple_queue_frontend.c`:

```text
TupleSinkTupleDescriptorsCompatible
TupleSinkAttributeUsesLengthPrefix
NormalizeTupleSinkAttribute
TupleSinkTupleBytes
SetTupleSinkNonNullBit
DecodeTupleViewFromTupleSinkRow
CopyOutTupleViewFromTupleSinkRow if present as a separate helper
```

This file owns:

```text
TupleDesc -> CitusTupleViewContract
TupleTableSlot -> sequential tuple-view row
sequential tuple-view row -> Datum/isnull arrays
borrow-by-reference vs datumCopy policy
null bitmap handling
dropped/generated attribute handling
varlena detoast/cstring length handling
```

This file should not know about:

```text
control SHM
service sessions
peer endpoints
RDMA
queue file descriptors
mmap addresses except through caller-provided buffers
```

## Shared ABI header split

The shared ABI headers must compile in both the host frontend library and the Homer service. They should stay fixed-width and should avoid PostgreSQL-only types.

Do **not** include in shared ABI headers:

```text
postgres.h
executor/tuptable.h
utils/rel.h
Datum
TupleDesc
TupleTableSlot
MemoryContext
elog/ereport
libpq
ibverbs/rdmacm
```

Use standard C headers only where possible:

```c
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
```

First-pass policy: keep the existing `Citus*` struct and macro names to avoid a protocol rename diff. Move them into clearer headers first; rename later only if needed.

### `src/include/distributed/homer/homer_abi_version.h`

Move shared version/capacity constants here:

```text
CITUS_TUPLE_SINK_PROTOCOL_VERSION
CITUS_TUPLE_SINK_DEFAULT_SLOT_CAPACITY_BYTES
CITUS_TUPLE_SINK_DEFAULT_BATCH_TUPLE_TARGET
CITUS_TUPLE_SINK_DEFAULT_SLOT_COUNT
CITUS_TUPLE_SINK_MAX_SHARED_ROUTES
CITUS_TUPLE_SINK_MAX_CONTRACT_ATTRIBUTES
CITUS_TUPLE_SINK_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_CONTROL_PROTOCOL_VERSION
CITUS_REMOTE_EXEC_CONTROL_ERROR_BYTES
CITUS_REMOTE_EXEC_CONTROL_SLOT_COUNT
CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SESSIONS
CITUS_REMOTE_EXEC_CONTROL_MAX_LOCAL_SINKS
CITUS_REMOTE_EXEC_CONTROL_HOST_BYTES
CITUS_REMOTE_EXEC_CONTROL_NAME_BYTES
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_CONTROL_INVALID_SESSION_INDEX
CITUS_REMOTE_EXEC_TX_BEGIN_ATTACH_REPLAY_BYTES
CITUS_REMOTE_EXEC_SQL_COMMAND_BYTES
```

Leave SHM object names/prefixes in `homer_shm_channel_abi.h`, not here.

### `src/include/distributed/homer/homer_tuple_abi.h`

Move tuple/data-plane semantic ABI from `tuple_sink_protocol.h`:

```text
CITUS_TUPLE_SINK_DIRECTION_SEND_CODE
CITUS_TUPLE_SINK_DIRECTION_RECEIVE_CODE
CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_CLOSED
CITUS_TUPLE_SINK_QUEUE_FLAG_PEER_FAILED
CITUS_TUPLE_SINK_TRANSPORT_FLAG_EOS
CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_FIRST
CITUS_TUPLE_SINK_TRANSPORT_FLAG_FRAGMENT_LAST
CITUS_TUPLE_SINK_RECORD_KIND_DATA
CITUS_TUPLE_SINK_RECORD_KIND_EOS
CITUS_TUPLE_SINK_RECORD_KIND_ERROR
CITUS_TUPLE_SINK_RECORD_KIND_PADDING
CITUS_TUPLE_SINK_TRANSPORT_FLAG_WRAP
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_NULL
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_DROPPED
CITUS_TUPLE_SINK_ATTRIBUTE_FLAG_GENERATED_OMITTED
CitusTupleSinkTransactionId
CitusTupleSinkKey
CitusTupleContractAttribute
CitusTupleViewContract
CitusTupleSinkTerminalState
CitusTupleSinkFailureCode
CitusTupleSinkTerminalStatus
CitusTupleSinkBatchHeader
CitusTupleSinkErrorRecord
CitusTupleSinkTransportHeader
```

The full `CitusTupleViewContract` remains shared because the frontend builds it and the service validates/installs it.

### `src/include/distributed/homer/homer_queue_abi.h`

Move queue and byte-ring attachment/control ABI:

```text
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_RECEIVER_OWNED
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_LOGICAL_DESCRIPTOR_ONLY
CITUS_TUPLE_SINK_PAYLOAD_RING_FLAG_RDMA_REGISTERED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_RECEIVER_OWNED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_RDMA_REGISTERED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PRODUCER_OWNED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_CLOSED
CITUS_HOMER_PAYLOAD_BYTE_RING_FLAG_PEER_FAILED
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_SENDER_OWNED
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_LOGICAL_DESCRIPTOR_ONLY
CITUS_TUPLE_SINK_PAYLOAD_HEAD_MIRROR_FLAG_RDMA_REGISTERED
CITUS_TUPLE_SINK_QUEUE_DESCRIPTOR_FLAG_BYTE_RING
CitusTupleSinkQueueControl
CitusTupleSinkPayloadRingControl
CitusTupleSinkQueueDescriptor
CitusTupleSinkPayloadRingDescriptor
CitusHomerPayloadByteRingControl
CitusHomerPayloadByteRingDescriptor
CitusTupleSinkPayloadHeadMirrorDescriptor
CitusTupleSinkRegistryHeader
CitusTupleSinkRegistryEntry
CitusTupleSinkQueueAttachment
CitusResultStreamBinding
```

The current `CitusTupleSinkQueueDescriptor` contains `queueShmName`, so it is not the final DMA attachment shape. Keep it for the current SHM model. Later, introduce a separate `HomerQueueAttachmentDescriptor` with an attachment kind or add a DMA-specific descriptor in a new ABI version.

### `src/include/distributed/homer/homer_control_abi.h`

Move semantic control-plane request/response ABI from `remote_execution_control_protocol.h`:

```text
CitusRemoteExecControlRequestKind
CitusRemoteExecControlStatusCode
CitusRemoteExecControlOpKind
CITUS_REMOTE_EXEC_OP_*
CITUS_REMOTE_EXEC_ACCESS_*
CITUS_REMOTE_EXEC_TX_*
CITUS_REMOTE_EXEC_FRESHNESS_*
CITUS_REMOTE_EXEC_OWNERSHIP_*
CITUS_REMOTE_EXEC_LANE_*
CITUS_REMOTE_EXEC_METADATA_POLICY_*
CITUS_REMOTE_EXEC_SCOPE_*
CITUS_REMOTE_EXEC_POST_COMMAND_STATE_*
CITUS_REMOTE_EXEC_COMMAND_*
CITUS_REMOTE_EXEC_SQL_RESULT_*
CITUS_REMOTE_EXEC_COMMAND_STATE_*
CITUS_REMOTE_EXEC_COMMAND_FLAG_WAIT_FOR_TERMINAL_COMPLETION
CITUS_REMOTE_EXEC_COMMAND_FLAG_RESULT_BINDING_PREARMED
CitusRemoteExecSessionKey
CitusRemoteExecPlacementAccessDescriptor
CitusRemoteExecPeerEndpoint
CitusRemoteExecCopyIngestCommandSpec
CitusRemoteExecTxBeginAttachCommandSpec
CitusRemoteExecTxPrepareCommandSpec
CitusRemoteExecSqlExecuteCommandSpec
CitusRemoteExecControlRequestHeader
CitusRemoteExecControlResponseHeader
CitusRemoteExecOpenSessionRequest
CitusRemoteExecOpenSessionResponse
CitusRemoteExecCloseSessionRequest
CitusRemoteExecCloseSessionResponse
CitusRemoteExecReportPostCommandStateRequest
CitusRemoteExecReportPostCommandStateResponse
CitusRemoteExecCommandCompletion
CitusRemoteExecStartCommandRequest
CitusRemoteExecStartCommandResponse
CitusRemoteExecPollCommandCompletionRequest
CitusRemoteExecPollCommandCompletionResponse
CitusRemoteExecControlRequestUnion
CitusRemoteExecControlResponseUnion
```

This header should describe the semantic messages. It should not contain the local SHM control-region slot array.

### `src/include/distributed/homer/homer_completion_abi.h`

Move completion mailbox/ring and result descriptor ABI:

```text
CITUS_REMOTE_EXEC_RESULT_FLAG_NONE
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_READY
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_EOS
CITUS_REMOTE_EXEC_RESULT_FLAG_TUPLE_SINK_FAILED
HOMER_COMPLETION_INLINE_DETAIL_BYTES
HOMER_RESULT_DESCRIPTOR_SLOTS
CitusRemoteExecCommandCompletionHot
CitusRemoteExecClientCompletionSeal
CitusRemoteExecClientCompletionSlot
HomerResultDescriptorSlot
HomerResultDescriptorTable
HomerResultDescriptorReadStatus
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_MAILBOX_SLOTS
CitusRemoteExecClientCompletionMailbox
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_RING_SLOTS
CitusRemoteExecPeerCommandCompletionEvent
CitusRemoteExecPeerCommandCompletionRing
```

Move pure inline helpers here:

```text
CitusRemoteExecFillClientCompletionSeal
CitusRemoteExecClientCompletionSealMatches
CitusRemoteExecResultDescriptorSlotIndex
CitusRemoteExecCompletionDetailBytes
CitusRemoteExecFillQueueAttachmentFromDescriptor
CitusRemoteExecFillStreamBindingFromDescriptor
CitusRemoteExecFillQueueDescriptorFromAttachment
CitusRemoteExecFillHotCompletionFromLegacy
CitusRemoteExecPublishResultDescriptor
CitusRemoteExecReadResultDescriptorStable
```

### `src/include/distributed/homer/homer_shm_channel_abi.h`

Move current local POSIX-SHM channel ABI:

```text
CITUS_REMOTE_EXEC_CONTROL_SHM_NAME
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_CLIENT_COMPLETION_SHM_PREFIX
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_NAME_BYTES
CITUS_REMOTE_EXEC_PEER_COMMAND_COMPLETION_SHM_PREFIX
CitusRemoteExecControlSlotState
CitusRemoteExecControlSlot
CitusRemoteExecReadyBitmapLine
CitusRemoteExecControlRegion
```

This header is shared between the host frontend SHM implementation and the host service implementation in the current model. It is not the final DPU/DMA semantic ABI.

### `src/include/distributed/homer/homer_basebackup_abi.h`

Move basebackup object protocol from `tuple_sink_protocol.h`:

```text
CITUS_REMOTE_BASEBACKUP_PROTOCOL_VERSION
CITUS_REMOTE_BASEBACKUP_NAME_BYTES
CITUS_REMOTE_BASEBACKUP_OBJECT_*
CITUS_REMOTE_BASEBACKUP_HEADER_FIXED_BYTES
CITUS_REMOTE_BASEBACKUP_HEADER_MAX_BYTES
CitusRemoteBaseBackupMessageHeader
CitusRemoteBaseBackupObjectKindValid
CitusRemoteBaseBackupObjectKindCarriesName
CitusRemoteBaseBackupHeaderBytesForName
```

This keeps tuple-stream ABI and basebackup object ABI separate.

## Include updates at call sites

Because this plan does not keep a `remote_execution_session.h` shim, update call sites directly.

Use a mechanical search:

```bash
grep -R "distributed/homer/remote_execution_session.h" -n src
```

Known call sites to update:

### `src/backend/distributed/commands/multi_copy.c`

Current includes include both:

```c
#include "distributed/homer/remote_execution_session.h"
#include "distributed/homer/tuple_sink_service.h"
```

Target ordinary backend caller include:

```c
#include "distributed/homer/homer_frontend.h"
```

Remove the direct queue-substrate include once `ExperimentalTupleSinkUseExplicitBatchApi` and related frontend GUC declarations are available through `homer_frontend.h`.

### `src/backend/distributed/worker/homer/worker_tuple_sink_insert.c`

Current include:

```c
#include "distributed/homer/remote_execution_session.h"
```

Target:

```c
#include "distributed/homer/homer_frontend.h"
```

The worker should remain an ordinary consumer of the frontend API: open receive session, poll receive batches, borrow tuple views, release tuple views/batches.

### `src/backend/distributed/transaction/transaction_management.c`

Current include:

```c
#include "distributed/homer/remote_execution_session.h"
```

Target:

```c
#include "distributed/homer/homer_frontend.h"
```

This file calls the transaction-finalization hooks declared by the frontend API.

### `src/backend/distributed/utils/homer/remote_execution_backend_bridge.c`

This file is not an ordinary public frontend API caller. It is PostgreSQL-side service/backend bridge code for socketless backend spawn and command execution.

Current include:

```c
#include "distributed/homer/tuple_sink_service.h"
```

Target after queue rename:

```c
#include "distributed/homer/homer_tuple_queue_frontend.h"
#include "distributed/homer/homer_control_abi.h"
#include "distributed/homer/homer_completion_abi.h"
#include "distributed/homer/homer_queue_abi.h"
```

Do not force this file through `homer_frontend.h` unless it truly calls the session frontend API. Its role is closer to host-side service integration and local backend command execution.

### `src/bin/homer_client.c` and `src/include/distributed/homer/remote_execution_client.h`

These are client-facing, not ordinary PostgreSQL backend frontend API. They should be updated to include the split shared ABI headers instead of the old monolithic protocol headers.

Do not merge this API into `homer_frontend.h` in the first pass. Keep the external client API separate.

## Build plan

### First build milestone: file separation with current extension build

The current Citus build gathers `*.c` files under `SUBDIRS` and filters out `tuple_sink_service_process.o`. Keep that mechanism initially.

After file moves, update `src/backend/distributed/Makefile` only as needed for filenames:

```make
# Still included in citus.so in the first build milestone:
utils/homer/homer_frontend.o
utils/homer/homer_frontend_control.o
utils/homer/homer_frontend_shm.o
utils/homer/homer_tuple_queue_frontend.o
utils/homer/homer_tuple_view_codec.o
utils/homer/homer_citus_policy.o
utils/homer/homer_citus_xact.o
```

Keep excluding the standalone service process object from the extension:

```make
OBJS = $(filter-out utils/homer/tuple_sink_service_process.o,$(DIST_EXTENSION_OBJS))
```

If `remote_execution_peer_transport_rdma.o` is not referenced by frontend objects after the refactor, exclude it from `citus.so` too:

```make
HOMER_SERVICE_OBJS = \
    utils/homer/tuple_sink_service_process.o \
    utils/homer/remote_execution_peer_transport_rdma.o

OBJS = $(filter-out $(HOMER_SERVICE_OBJS),$(DIST_EXTENSION_OBJS))
```

Then remove RDMA libraries from `SHLIB_LINK` for `citus.so` once no extension object needs them:

```make
# Remove from citus.so once service-only:
# -Wl,--no-as-needed -lrdmacm -libverbs -Wl,--as-needed
```

### Second build milestone: named frontend component/library

After the file split compiles, define the frontend object group explicitly:

```make
HOMER_FRONTEND_OBJS = \
    utils/homer/homer_frontend.o \
    utils/homer/homer_frontend_control.o \
    utils/homer/homer_frontend_shm.o \
    utils/homer/homer_tuple_queue_frontend.o \
    utils/homer/homer_tuple_view_codec.o \
    utils/homer/homer_citus_policy.o \
    utils/homer/homer_citus_xact.o
```

Two acceptable implementation choices:

1. keep `HOMER_FRONTEND_OBJS` linked directly into `citus.so`, but grouped and documented as the frontend component
2. build a static archive, `libhomer_frontend.a`, and link `citus.so` against that archive

Preferred target if the build system supports the archive cleanly:

```make
HOMER_FRONTEND_LIB = utils/homer/libhomer_frontend.a

$(HOMER_FRONTEND_LIB): $(HOMER_FRONTEND_OBJS)
	$(AR) rcs $@ $^

OBJS = $(filter-out $(HOMER_SERVICE_OBJS) $(HOMER_FRONTEND_OBJS),$(DIST_EXTENSION_OBJS))
SHLIB_LINK += $(HOMER_FRONTEND_LIB)
```

If PostgreSQL/Citus extension linking resists `.a` archives in `SHLIB_LINK`, keep the explicit object group as the intermediate state and document it as the frontend component. The separation still matters because service-only and frontend-only objects are no longer mixed.

### Service build milestone

The service build should include:

```text
tuple_sink_service_process.o
remote_execution_peer_transport_rdma.o
service-side helpers split later
shared ABI headers
```

The service build should link RDMA libraries:

```text
-lrdmacm
-libverbs
```

The extension/frontend build should not link RDMA libraries after `remote_execution_peer_transport_rdma.o` leaves `citus.so`.

## Mechanical migration order

### Phase 0: branch hygiene

Work on:

```text
postgres-dbcomm:homer-dpu-migration
citus-dbcomm:homer-dpu-migration
```

Keep KB edits in `postgres-dbcomm`. Code changes belong in `citus-dbcomm`.

### Phase 1: create the public frontend header

1. `git mv remote_execution_session.h homer_frontend.h`
2. update include guard and file comment
3. update known call-site includes to `homer_frontend.h`
4. run `grep -R "remote_execution_session.h" -n src` and remove all hits
5. do not add a compatibility shim

Expected outcome: ordinary backend callers now depend on the new header name, but behavior and function names are unchanged.

### Phase 2: move/rename frontend implementation coordinator

1. `git mv remote_execution_session.c homer_frontend.c`
2. update the include inside the moved C file from `remote_execution_session.h` to `homer_frontend.h`
3. leave helper functions in place for this phase
4. compile

Expected outcome: source filenames now match the intended component boundary, but behavior remains unchanged.

### Phase 3: split shared ABI headers

1. create `homer_abi_version.h`
2. create `homer_tuple_abi.h`
3. create `homer_queue_abi.h`
4. create `homer_control_abi.h`
5. create `homer_completion_abi.h`
6. create `homer_shm_channel_abi.h`
7. create `homer_basebackup_abi.h`
8. update includes in frontend, service, bridge, client, and transport files
9. delete or empty old monolithic protocol headers only after all includes are updated

Expected outcome: both frontend and service include shared ABI headers, but no runtime behavior changes.

### Phase 4: extract SHM control channel

1. create `homer_frontend_shm.h/.c`
2. move SHM control-state, atomic, map/unmap, control-slot, request-publish, wait-response, response-check, and completion-ring mapping helpers from `homer_frontend.c`
3. update `homer_frontend.c` to call these helpers
4. compile

Expected outcome: `homer_frontend.c` has no direct `shm_open`, `mmap`, `munmap`, or `ftruncate` calls.

### Phase 5: extract control request builders

1. create `homer_frontend_control.h/.c`
2. move session-key, sink-key, placement-descriptor, peer-endpoint, enum-conversion, command-completion conversion, and `*ThroughLocalService` helpers out of `homer_frontend.c`
3. have these helpers call the SHM channel functions directly
4. compile

Expected outcome: `homer_frontend.c` owns high-level session semantics; `homer_frontend_control.c` owns message construction; `homer_frontend_shm.c` owns the current channel.

### Phase 6: move Citus policy and transaction glue

1. create `homer_citus_policy.h/.c`
2. move init/spec/string/feature-gate helpers there
3. create `homer_citus_xact.h/.c`
4. move transaction-finalization list and callbacks there
5. compile

Expected outcome: PostgreSQL/Citus policy and transaction lifecycle glue are clearly host-side frontend files.

### Phase 7: rename tuple queue frontend substrate

1. `git mv tuple_sink_service.h homer_tuple_queue_frontend.h`
2. `git mv tuple_sink_service.c homer_tuple_queue_frontend.c`
3. update includes in `homer_frontend.c`, `remote_execution_backend_bridge.c`, and any other queue-substrate users
4. move frontend-visible GUC declarations to `homer_frontend.h`
5. compile

Expected outcome: the host-side queue attachment code is no longer named as if it were the service process.

### Phase 8: extract tuple-view codec

1. create `homer_tuple_view_codec.h/.c`
2. move tuple contract construction and tuple row encode/decode helpers there
3. keep queue endpoint code focused on byte-ring reservation/publication and batch-handle lifetime
4. compile

Expected outcome: tuple shape/Datum encoding is separated from queue frontier mechanics.

### Phase 9: update build boundary

1. define `HOMER_FRONTEND_OBJS`
2. define `HOMER_SERVICE_OBJS`
3. exclude service-only objects from `citus.so`
4. remove RDMA libraries from `citus.so` link once possible
5. optionally build `libhomer_frontend.a`
6. ensure service binary links service-only/RDMA objects

Expected outcome: frontend and service are identifiable build components; ordinary PostgreSQL backend linkage is not polluted by service/RDMA objects.

### Phase 10: service-file split in a later pass

Do not split `tuple_sink_service_process.c` during the first frontend separation unless necessary for compile hygiene. It is too large and service-internal. Once the frontend boundary is clean, split it into service modules in a separate design pass.

Candidate later service split:

```text
homer_service_main.c
homer_service_control.c
homer_service_session.c
homer_service_sink.c
homer_service_peer_control.c
homer_service_transport_rdma.c
homer_service_scheduler.c
homer_service_command_dispatch.c
homer_service_tuple_decode.c
```

## Acceptance checks

### Header boundary

```bash
grep -R "remote_execution_session.h" -n src
# expected: no results

grep -R "tuple_sink_service.h" -n src
# expected after queue rename: no results
```

Ordinary backend call sites should include:

```c
#include "distributed/homer/homer_frontend.h"
```

Service code should not include `homer_frontend.h`.

### Frontend implementation boundary

```bash
grep -n "shm_open\|mmap\|munmap\|ftruncate" src/backend/distributed/utils/homer/homer_frontend.c
# expected after SHM extraction: no results

grep -n "rdma_\|ibv_" src/backend/distributed/utils/homer/homer_frontend*.c
# expected: no results
```

### ABI boundary

Shared ABI headers should compile without PostgreSQL backend headers:

```bash
grep -R "postgres.h\|executor/tuptable.h\|utils/rel.h\|Datum\|TupleDesc\|TupleTableSlot" \
    src/include/distributed/homer/homer_*_abi.h
# expected: no PostgreSQL-only API dependencies
```

### Build boundary

```text
citus.so includes frontend objects or libhomer_frontend.a
citus.so excludes tuple_sink_service_process.o
citus.so excludes remote_execution_peer_transport_rdma.o once frontend no longer references it
citus.so does not need -lrdmacm/-libverbs after RDMA object exclusion
Homer service binary links service/RDMA objects and RDMA libraries
```

### Behavior regression checks

Run existing tuple-sink/Homer validation after each phase that moves code:

```text
experimental COPY tuple-sink worker insert path
loopback validation path
explicit reserve/append/submit path
worker borrow/use/release insert path
transaction PREPARE/COMMIT/ABORT finalization hooks
peer completion ring command-completion path
semantic ERROR+EOS record propagation
receive terminal state / peer-closed handling
```

## Later DMA transition

After the mechanical separation is complete, add:

```text
homer_frontend_dma.h
homer_frontend_dma.c
```

Only then decide whether to introduce a runtime abstraction such as:

```c
typedef struct HomerFrontendTransportOps HomerFrontendTransportOps;
```

A vtable is justified only if:

- SHM and DMA must coexist in one binary
- runtime selection is required
- unit tests need to swap the channel implementation without recompiling

If the DPU migration is a build-time or deployment-time replacement, a concrete `homer_frontend_dma.c` implementing the same internal channel functions as `homer_frontend_shm.c` is sufficient and less invasive.
