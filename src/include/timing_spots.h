#pragma once

#include "./timing_custom_stats.h"

#define VERBOSE_TIMING_SPOTS 1 // set to 1 to get more detailed timing spots

/*
 * Helper macros to conditionally emit verbose timing calls. Use
 * verbose_timing_start(<spot>) and verbose_timing_end(<spot>).
 */
#ifndef verbose_timing_start
#if VERBOSE_TIMING_SPOTS
#define verbose_timing_start(spot) timing_start(spot)
#define verbose_timing_end(spot) timing_end(spot)
#else
/* no-op when verbose timing is disabled */
#define verbose_timing_start(spot) ((void)0)
#define verbose_timing_end(spot) ((void)0)
#endif
#endif

// TODO: add func/names for timing spots here
/* Notes:
 * pull pair: FetchIntermediate_ & SendViaCopy_ sending/receiving intermediate results
 *   - receiving tuples via copy: FetchIntermediate_CopyData_ & FetchIntermediate_File_ receiving and writing to file
 * push pair: DestReceiver_All & ExecutePlanIntoDestReceiver_ executing and pushing intermediate results
 *   - target receive via copy: ReceiveViaCopy_ & ReceiveAndWriteCopyData_ receiving and writing to file
 */

// parseInput: examines whatever bytes pqReadData() already placed in conn->inBuffer. For each message it sees, it pulls
// the type/length header, confirms the entire payload is buffered, dispatches to the appropriate handler to update
// PGconn state/results (and notification/error queues), and then loops. If the payload isn’t fully buffered yet, the
// function just returns immediately; it’s the caller’s job to call pqReadData()/PQconsumeInput() (and, if needed,
// pqWait()) to bring in more bytes before invoking pqParseInput3() again.
// NOTE: no socket IO here

// DoCopyFromLocalTableIntoShards: the actual loop that copies data
// CitusSendTupleToPlacements_: serializing and sending out the tuple
// SendViaCopy_: calls FileReadCompat to read from file and just send
//
// RemoteFileDestReceiver_Init may include writing to local file, plus setting up connections.
// ReceiveResults_HeapFormTuple happens during ReceiveResults_BuildTuples; keeping it separate allows
// us to bucket tuple materialization apart from transport/protocol work when desired.
// Printtup_Ser, PQ_getbyte, PQ_getmessage, PQ_getbytes, PQ_putmessage, PQ_sendCommand, PQ_putCopyData,
// PQ_putCopyEnd, PQ_getResult, PQ_getCopyData, PQ_connectStart, PQ_connectPoll, and CheckConnectionReady_
// are intended as lower-overhead communication-stack leaves around row serialization, backend receive-side
// protocol framing, backend receive-buffer draining/copy, backend/frontend protocol message staging,
// buffered result/COPY extraction, connection-setup state-machine work, and post-wakeup libpq processing,
// respectively.

// GetRemoteCommandResult and WaitForConnections are closely related: they both waits for workers to ACK the command
// sent to them by the coordinator (during a xact)

// xaction start its lifecycle with UseCoordinatedTransaction (I think true for all), and ends with
// CoordinatedTransactionCallback (after commit/abort etc.). So timing for the whole xaction should be from there.

// XACT_TS_WaitForConnections: waits/sleeps for socket readiness and pulls data from socket until not busy anymore
// should be followed by a later GetRemoteCommandResult() etc. which deserializes the socket bytes
//
// QUERY_ACTIVE_WALL is the top-level logical query-cycle denominator. It is
// started from PostgresMain() when a query cycle begins and stopped only after
// the matching ReadyForQuery() flush completes. QUERY_WAIT_WALL is the
// companion excluded-wait bucket that is driven centrally by PostgreSQL wait
// events plus FE-libpq PQsocketPoll() waits.
#define TIMING_SPOTS(X)                                                                                                \
    X(PG_WAIT)                                                                                                         \
    X(PG_WAIT_DONT_COUNT)                                                                                              \
    X(PG_FE_WAIT)                                                                                                      \
    X(QUERY_WAIT_WALL)                                                                                                 \
    X(QUERY_ACTIVE_WALL)                                                                                               \
    X(PG_FE_SOCK_READ)                                                                                                 \
    X(PG_FE_SOCK_WRITE)                                                                                                \
    X(PG_BE_SOCK_READ)                                                                                                 \
	X(PG_BE_SOCK_WRITE)                                                                                                \
	X(PG_parseInput)                                                                                                   \
	X(PQ_getbyte)                                                                                                      \
	X(PQ_getmessage)                                                                                                   \
	X(PQ_getbytes)                                                                                                     \
                                                                                                                       \
    X(ExecSimpleQuery)                                                                                                 \
    X(ParseQuery)                                                                                                      \
    X(QueryAnalyzeAndRewrite)                                                                                          \
    X(QueryExecution)                                                                                                  \
    X(QueryPlanning)                                                                                                   \
    X(EndingComms)                                                                                                     \
    X(CopyTo_)                                                                                                         \
    X(CopyTo_GetTuples)                                                                                                \
    X(CopyTo_CopyOneRowTo)                                                                                             \
    X(CopyTo_fwrite)                                                                                                   \
                                                                                                                       \
    X(Receiver_CopyFrom)                                                                                               \
    X(NextCopyFrom_)                                                                                                   \
    X(CopyFrom_CopyGetData)                                                                                            \
    X(CopyFromInsertIntoTable)                                                                                         \
                                                                                                                       \
    X(Printtup_Startup)                                                                                                \
	X(Printtup)                                                                                                        \
	X(Printtup_Ser)                                                                                                    \
	X(Printtup_Net)                                                                                                    \
	X(PQ_putmessage)                                                                                                   \
	X(PQ_sendCommand)                                                                                                  \
	X(PQ_putCopyData)                                                                                                  \
	X(PQ_putCopyEnd)                                                                                                   \
	X(PQ_getResult)                                                                                                    \
	X(PQ_getCopyData)                                                                                                  \
	X(PQ_connectStart)                                                                                                 \
	X(PQ_connectPoll)                                                                                                  \
                                                                                                                       \
    X(CreateCitusTable_)                                                                                               \
    X(CopyFromLocalTableIntoDistTable_)                                                                                \
    X(DoCopyFromLocalTableIntoShards_)                                                                                 \
    X(CitusSendTupleToPlacements_)                                                                                     \
    X(SerializeAndCopyRow_)                                                                                            \
    X(SendCopyDataToPlacement_)                                                                                        \
    X(WriteTupleToLocal_)                                                                                              \
    X(DoLocalCopy_)                                                                                                    \
                                                                                                                       \
    X(FetchIntermediate_)                                                                                              \
    X(FetchIntermediate_CopyAndWrite)                                                                                  \
    X(FetchIntermediate_FileWrite)                                                                                     \
                                                                                                                       \
    X(ReceiveAndWriteCopyData_)                                                                                        \
    X(ReceiveAndWriteCopyData_Deser)                                                                                   \
    X(ReceiveAndWriteCopyData_WriteFile)                                                                               \
    X(SendViaCopy_)                                                                                                    \
    X(SendViaCopy_Send)                                                                                                \
    X(SendViaCopy_FileRead)                                                                                            \
                                                                                                                       \
    X(ExecutePlanIntoColocatedIntermediateResults_)                                                                    \
    X(ExecutePlanIntoDestReceiver_)                                                                                    \
                                                                                                                       \
	X(CheckConnectionReady_)                                                                                           \
	X(ReceiveResults_)                                                                                                 \
	X(ReceiveResults_Net)                                                                                              \
	X(ReceiveResults_Deserialize)                                                                                      \
	X(ReceiveResults_BuildTuples)                                                                                      \
	X(ReceiveResults_HeapFormTuple)                                                                                    \
                                                                                                                       \
    X(RemoteFileDestReceiver_Init)                                                                                     \
    X(RemoteFileDestReceiver_SerAndSend)                                                                               \
    X(RemoteFileDestReceiver_Ser)                                                                                      \
    X(RemoteFileDestReceiver_Send)                                                                                     \
    X(RemoteFileDestReceiver_SerAndSend_WriteLocal)                                                                    \
                                                                                                                       \
    X(ProcessCopyStmt_)                                                                                                \
                                                                                                                       \
    X(XACT_PROCESSING)                                                                                                 \
    X(XACT_TS_SendRemoteCommand)                                                                                       \
    X(XACT_TS_GetRemoteCommandResult)                                                                                  \
    X(XACT_TS_WaitForConnections)                                                                                      \
    X(XACT_TS_coordinated_commit_abort)                                                                                \
    X(XACT_TS_EndCommand)                                                                                              \
    X(XACT_TS_CoordinatorPrepare)                                                                                      \
    X(XACT_WAIT)                                                                                                       \
                                                                                                                       \
    X(SendQuery_func)                                                                                                  \
    X(ExecQueryAndProcessResults_func)                                                                                 \
    X(ExecQueryAndProcessResults_parse_results)                                                                        \
    X(ExecQueryAndProcessResults_read_data)                                                                            \
    X(SomethingElseFunc)

// --- Use the list to generate the enum ---
#define AS_ENUM(name) name,

typedef enum
{
    TIMING_SPOTS(AS_ENUM) _NUM_TIMING_SPOTS // get the count of timing spots
} timing_spot_enums;

// --- Use the same list to generate the names array ---
#define AS_STRING(name) #name,

extern const char *timing_spot_names[_NUM_TIMING_SPOTS];
