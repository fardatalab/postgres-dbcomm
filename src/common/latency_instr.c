#include "postgres.h"

#include <string.h>

#include "latency_instr.h"
#include "lib/stringinfo.h"
#include "time_instr.h"

#define LATENCY_TRACE_MAX_ENTRIES 1024
#define LATENCY_REMOTE_COMMAND_COMMENT_PREFIX "/*cituscmd:"

typedef struct LatencyTraceEntry
{
	LatencyTraceStage stage;
	uint64 remoteSessionId;
	uint64 remoteCommandSequence;
	uint64 startNs;
	uint64 endNs;
} LatencyTraceEntry;

static const char *LatencyTraceStageNames[LATENCY_STAGE_COUNT] = {
	"backend_spawn",
	"client_session_establish",
	"client_command_wait",
	"client_command_receive",
	"backend_parse_plan",
	"client_bind_complete_send",
	"client_describe_response_send",
	"client_result_send",
	"worker_session_acquire",
	"placement_bind",
	"remote_tx_attach",
	"remote_command_dispatch",
	"remote_command_flush",
	"remote_command_wait",
	"remote_result_drain",
	"remote_tx_commit",
	"remote_tx_abort",
	"remote_tx_prepare",
	"worker_session_release",
	"client_command_complete",
	"client_ready_for_query",
	"backend_command_turnaround",
	"client_session_teardown",
};

static struct
{
	LatencyTraceEntry entries[LATENCY_TRACE_MAX_ENTRIES];
	uint32 entryCount;
	uint32 droppedEntryCount;
	LatencyTraceHandle backendSpawnHandle;
	LatencyTraceHandle clientSessionHandle;
} latency_state = {0};

uint64
latency_trace_now_ns(void)
{
	struct timespec currentTime;

	clock_gettime(CLOCK_MONOTONIC_RAW, &currentTime);
	return ((uint64) currentTime.tv_sec * UINT64CONST(1000000000)) +
		   (uint64) currentTime.tv_nsec;
}

static inline bool
LatencyTraceHandleValid(LatencyTraceHandle handle)
{
	return handle != LATENCY_TRACE_INVALID_HANDLE &&
		   handle <= latency_state.entryCount;
}

static inline bool
LatencyTraceShouldDeferFlush(void)
{
	return logger_distributed_xact_active() || logger_identity_persist_enabled();
}

LatencyTraceHandle
latency_trace_begin_at(LatencyTraceStage stage, uint64 remoteSessionId,
						  uint64 remoteCommandSequence, uint64 startNs)
{
	LatencyTraceEntry *entry = NULL;

	if (stage < 0 || stage >= LATENCY_STAGE_COUNT)
	{
		return LATENCY_TRACE_INVALID_HANDLE;
	}

	if (latency_state.entryCount >= LATENCY_TRACE_MAX_ENTRIES)
	{
		latency_state.droppedEntryCount++;
		return LATENCY_TRACE_INVALID_HANDLE;
	}

	entry = &latency_state.entries[latency_state.entryCount];
	memset(entry, 0, sizeof(LatencyTraceEntry));
	entry->stage = stage;
	entry->remoteSessionId = remoteSessionId;
	entry->remoteCommandSequence = remoteCommandSequence;
	entry->startNs = startNs;

	latency_state.entryCount++;
	return latency_state.entryCount;
}

LatencyTraceHandle
latency_trace_begin(LatencyTraceStage stage, uint64 remoteSessionId,
					   uint64 remoteCommandSequence)
{
	return latency_trace_begin_at(stage, remoteSessionId, remoteCommandSequence,
								  latency_trace_now_ns());
}

void
latency_trace_end(LatencyTraceHandle handle)
{
	LatencyTraceEntry *entry = NULL;

	if (!LatencyTraceHandleValid(handle))
	{
		return;
	}

	entry = &latency_state.entries[handle - 1];
	if (entry->endNs != 0)
	{
		return;
	}

	entry->endNs = latency_trace_now_ns();
}

void
latency_trace_end_at(LatencyTraceHandle handle, uint64 endNs)
{
	LatencyTraceEntry *entry = NULL;

	if (!LatencyTraceHandleValid(handle))
	{
		return;
	}

	entry = &latency_state.entries[handle - 1];
	if (entry->endNs != 0)
	{
		return;
	}

	if (endNs < entry->startNs)
	{
		endNs = entry->startNs;
	}

	entry->endNs = endNs;
}

void
latency_trace_reset(void)
{
	memset(latency_state.entries, 0, sizeof(latency_state.entries));
	latency_state.entryCount = 0;
	latency_state.droppedEntryCount = 0;
	latency_state.backendSpawnHandle = LATENCY_TRACE_INVALID_HANDLE;
	latency_state.clientSessionHandle = LATENCY_TRACE_INVALID_HANDLE;
}

void
latency_trace_backend_spawn_start(uint64 inheritedStartNs)
{
	if (latency_state.backendSpawnHandle != LATENCY_TRACE_INVALID_HANDLE)
	{
		return;
	}

	if (inheritedStartNs == 0)
	{
		inheritedStartNs = latency_trace_now_ns();
	}

	latency_state.backendSpawnHandle =
		latency_trace_begin_at(LATENCY_STAGE_BACKEND_SPAWN, 0, 0,
								  inheritedStartNs);
}

void
latency_trace_backend_spawn_end(void)
{
	if (latency_state.backendSpawnHandle == LATENCY_TRACE_INVALID_HANDLE)
	{
		return;
	}

	latency_trace_end(latency_state.backendSpawnHandle);
	latency_state.backendSpawnHandle = LATENCY_TRACE_INVALID_HANDLE;
}

void
latency_trace_client_session_start(void)
{
	if (latency_state.clientSessionHandle != LATENCY_TRACE_INVALID_HANDLE)
	{
		return;
	}

	latency_state.clientSessionHandle =
		latency_trace_begin(LATENCY_STAGE_CLIENT_SESSION_ESTABLISH, 0, 0);
}

void
latency_trace_client_session_end(void)
{
	if (latency_state.clientSessionHandle == LATENCY_TRACE_INVALID_HANDLE)
	{
		return;
	}

	latency_trace_end(latency_state.clientSessionHandle);
	latency_state.clientSessionHandle = LATENCY_TRACE_INVALID_HANDLE;
}

static void
LatencyTracePrint(void)
{
	StringInfoData buffer;
	const char *identity = logger_get_identity();
	const char *commandTag = logger_get_command_tag();
	uint64 baseStartNs = UINT64_MAX;

	if (latency_state.entryCount == 0)
	{
		return;
	}

	for (uint32 entryIndex = 0; entryIndex < latency_state.entryCount; entryIndex++)
	{
		LatencyTraceEntry *entry = &latency_state.entries[entryIndex];
		if (entry->startNs != 0 && entry->startNs < baseStartNs)
		{
			baseStartNs = entry->startNs;
		}
	}

	if (baseStartNs == UINT64_MAX)
	{
		return;
	}

	initStringInfo(&buffer);
	appendStringInfo(&buffer, "LatencyTraceIdentity: %s\n",
					 identity != NULL ? identity : "");
	appendStringInfo(&buffer, "LatencyTraceCommandTag: %s\n",
					 commandTag != NULL ? commandTag : "");
	appendStringInfo(&buffer, "LatencyTraceDroppedEntries: %u\n",
					 latency_state.droppedEntryCount);
	appendStringInfoString(&buffer,
						   "\n--- Ordered Latency Trace (Nanoseconds) ---\n");
	appendStringInfo(&buffer,
					 "%-4s | %-28s | %-14s | %-18s | %-18s | %-18s | %-18s\n",
					 "Idx", "Stage", "RemoteSession", "RemoteCommand",
					 "Start Offset", "End Offset", "Duration");
	appendStringInfoString(&buffer,
						   "---------------------------------------------------------------------------------------------------------------\n");

	for (uint32 entryIndex = 0; entryIndex < latency_state.entryCount; entryIndex++)
	{
		LatencyTraceEntry *entry = &latency_state.entries[entryIndex];
		uint64 startOffsetNs = 0;
		uint64 endOffsetNs = 0;
		uint64 durationNs = 0;
		char remoteSessionBuffer[32] = "";
		char remoteCommandBuffer[64] = "";
		char startOffsetBuffer[32] = "";
		char endOffsetBuffer[32] = "";
		char durationBuffer[32] = "";

		if (entry->startNs == 0)
		{
			continue;
		}

		startOffsetNs = entry->startNs - baseStartNs;
		if (entry->endNs != 0)
		{
			endOffsetNs = entry->endNs - baseStartNs;
			durationNs = entry->endNs - entry->startNs;
		}

		snprintf(remoteSessionBuffer, sizeof(remoteSessionBuffer),
				 UINT64_FORMAT, entry->remoteSessionId);
		snprintf(startOffsetBuffer, sizeof(startOffsetBuffer),
				 UINT64_FORMAT, startOffsetNs);
		snprintf(endOffsetBuffer, sizeof(endOffsetBuffer),
				 UINT64_FORMAT, endOffsetNs);
		snprintf(durationBuffer, sizeof(durationBuffer),
				 UINT64_FORMAT, durationNs);

		if (entry->remoteCommandSequence != 0)
		{
			snprintf(remoteCommandBuffer, sizeof(remoteCommandBuffer),
					 UINT64_FORMAT "." UINT64_FORMAT,
					 entry->remoteSessionId,
					 entry->remoteCommandSequence);
		}

		appendStringInfo(&buffer,
						 "%-4u | %-28s | %-14s | %-18s | %-18s | %-18s | %-18s\n",
						 entryIndex,
						 LatencyTraceStageNames[entry->stage],
						 remoteSessionBuffer,
						 remoteCommandBuffer,
						 startOffsetBuffer,
						 endOffsetBuffer,
						 durationBuffer);
	}

	appendStringInfoString(&buffer,
						   "---------------------------------------------------------------------------------------------------------------\n");

	elog(LOG_SERVER_ONLY, "%s", buffer.data);
	pfree(buffer.data);
}

void
latency_trace_finish_query_cycle(bool skipPrint)
{
	if (LatencyTraceShouldDeferFlush())
	{
		return;
	}

	if (skipPrint)
	{
		latency_trace_reset();
		return;
	}

	LatencyTracePrint();
	latency_trace_reset();
}

/*
 * latency_trace_force_flush prints or drops the current ordered trace row
 * regardless of the normal defer rules.
 *
 * This is used on backend-exit paths such as frontend disconnect handling,
 * where proc_exit() bypasses the usual ReadyForQuery-driven flush boundary.
 */
void
latency_trace_force_flush(bool skipPrint)
{
	if (skipPrint)
	{
		latency_trace_reset();
		return;
	}

	LatencyTracePrint();
	latency_trace_reset();
}

char *
latency_prefix_remote_command(const char *command, uint64 remoteSessionId,
								 uint64 remoteCommandSequence,
								 const char *commandKind)
{
	if (command == NULL)
	{
		return NULL;
	}

	return psprintf("%srsid=" UINT64_FORMAT " rcid=" UINT64_FORMAT "." UINT64_FORMAT
					" kind=%s*/ %s",
					LATENCY_REMOTE_COMMAND_COMMENT_PREFIX,
					remoteSessionId,
					remoteSessionId,
					remoteCommandSequence,
					commandKind != NULL ? commandKind : "unknown",
					command);
}

bool
latency_extract_remote_command_tag(const char *queryString, char *commandTagBuffer,
									 size_t commandTagBufferSize)
{
	const char *scan = queryString;
	const char *prefixStart = NULL;
	const char *commentEnd = NULL;
	size_t tagLength = 0;

	if (commandTagBuffer == NULL || commandTagBufferSize == 0)
	{
		return false;
	}

	commandTagBuffer[0] = '\0';
	if (queryString == NULL)
	{
		return false;
	}

	while (*scan != '\0' && isspace((unsigned char) *scan))
	{
		scan++;
	}

	if (strncmp(scan, LATENCY_REMOTE_COMMAND_COMMENT_PREFIX,
				strlen(LATENCY_REMOTE_COMMAND_COMMENT_PREFIX)) != 0)
	{
		return false;
	}

	prefixStart = scan + strlen(LATENCY_REMOTE_COMMAND_COMMENT_PREFIX);
	commentEnd = strstr(prefixStart, "*/");
	if (commentEnd == NULL || commentEnd <= prefixStart)
	{
		return false;
	}

	tagLength = (size_t) (commentEnd - prefixStart);
	if (tagLength >= commandTagBufferSize)
	{
		tagLength = commandTagBufferSize - 1;
	}

	memcpy(commandTagBuffer, prefixStart, tagLength);
	commandTagBuffer[tagLength] = '\0';
	return true;
}
