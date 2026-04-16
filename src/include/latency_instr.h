#pragma once
#ifndef LATENCY_INSTR_H
#define LATENCY_INSTR_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef uint32 LatencyTraceHandle;

#define LATENCY_TRACE_INVALID_HANDLE 0
#define LATENCY_REMOTE_COMMAND_TAG_MAXLEN 128

/*
 * Ordered latency stages for per-transaction timeline tracing. These stages
 * intentionally focus on communication/control-path work rather than detailed
 * executor sub-phases.
 */
typedef enum LatencyTraceStage
{
	LATENCY_STAGE_BACKEND_SPAWN = 0,
	LATENCY_STAGE_CLIENT_SESSION_ESTABLISH,
	LATENCY_STAGE_CLIENT_COMMAND_WAIT,
	LATENCY_STAGE_CLIENT_COMMAND_RECEIVE,
	LATENCY_STAGE_BACKEND_PARSE_PLAN,
	LATENCY_STAGE_CLIENT_BIND_COMPLETE_SEND,
	LATENCY_STAGE_CLIENT_DESCRIBE_RESPONSE_SEND,
	LATENCY_STAGE_CLIENT_RESULT_SEND,
	LATENCY_STAGE_WORKER_SESSION_ACQUIRE,
	LATENCY_STAGE_PLACEMENT_BIND,
	LATENCY_STAGE_REMOTE_TX_ATTACH,
	LATENCY_STAGE_REMOTE_COMMAND_DISPATCH,
	LATENCY_STAGE_REMOTE_COMMAND_FLUSH,
	LATENCY_STAGE_REMOTE_COMMAND_WAIT,
	LATENCY_STAGE_REMOTE_RESULT_DRAIN,
	LATENCY_STAGE_REMOTE_TX_COMMIT,
	LATENCY_STAGE_REMOTE_TX_ABORT,
	LATENCY_STAGE_REMOTE_TX_PREPARE,
	LATENCY_STAGE_WORKER_SESSION_RELEASE,
	LATENCY_STAGE_CLIENT_COMMAND_COMPLETE,
	LATENCY_STAGE_CLIENT_READY_FOR_QUERY,
	LATENCY_STAGE_BACKEND_COMMAND_TURNAROUND,
	LATENCY_STAGE_BACKEND_LOGGING,
	LATENCY_STAGE_BACKEND_REPORTING,
	LATENCY_STAGE_CLIENT_SESSION_TEARDOWN,
	LATENCY_STAGE_COUNT
} LatencyTraceStage;

extern uint64 latency_trace_now_ns(void);
extern LatencyTraceHandle latency_trace_begin(LatencyTraceStage stage,
												 uint64 remoteSessionId,
												 uint64 remoteCommandSequence);
extern LatencyTraceHandle latency_trace_begin_at(LatencyTraceStage stage,
												   uint64 remoteSessionId,
												   uint64 remoteCommandSequence,
												   uint64 startNs);
extern void latency_trace_end(LatencyTraceHandle handle);
extern void latency_trace_end_at(LatencyTraceHandle handle, uint64 endNs);
extern void latency_trace_reset(void);
extern void latency_trace_finish_query_cycle(bool skipPrint);
extern void latency_trace_force_flush(bool skipPrint);

/*
 * Client session establishment begins before the normal query loop. The first
 * query on a short-lived session should still include this startup/auth cost,
 * so it is recorded in the same ordered trace buffer and flushed later.
 */
extern void latency_trace_backend_spawn_start(uint64 inheritedStartNs);
extern void latency_trace_backend_spawn_end(void);
extern void latency_trace_client_session_start(void);
extern void latency_trace_client_session_end(void);

extern char * latency_prefix_remote_command(const char *command,
											   uint64 remoteSessionId,
											   uint64 remoteCommandSequence,
											   const char *commandKind);
extern bool latency_extract_remote_command_tag(const char *queryString,
												 char *commandTagBuffer,
												 size_t commandTagBufferSize);

#endif /* LATENCY_INSTR_H */
