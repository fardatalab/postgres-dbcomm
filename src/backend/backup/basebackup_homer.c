/*-------------------------------------------------------------------------
 *
 * basebackup_homer.c
 *	  Service-backed base backup target for the Homer/RDMA prototype.
 *
 * This sink is deliberately scoped to the first research path: tar-format
 * BASE_BACKUP, no compression, no throttling, no WAL special handling, and a
 * remote service receiver that can initially blackhole/count received bytes.
 * The important invariant is that PostgreSQL writes archive/manifest bytes
 * directly into the Homer queue payload slot exposed as bbs_buffer.
 *
 * IDENTIFICATION
 *	  src/backend/backup/basebackup_homer.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include "backup/basebackup_sink.h"
#include "common/string.h"
#include "distributed/homer/remote_execution_client.h"
#include "distributed/homer/remote_execution_peer_control_protocol.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "utils/builtins.h"

typedef struct bbsink_homer
{
	bbsink		base;
	char	   *target_detail;
	HomerClientControl control;
	HomerClientBaseBackupStream stream;
	bool		control_open;
	bool		stream_open;
	bool		slot_reserved;
	uint32		payload_capacity;
	uint32		archive_index;
	uint64		archive_offset;
	uint64		total_bytes;
	char	   *current_payload;
	char		current_archive_name[CITUS_REMOTE_BASEBACKUP_NAME_BYTES];
} bbsink_homer;

static void bbsink_homer_begin_backup(bbsink *sink);
static void bbsink_homer_begin_archive(bbsink *sink,
									   const char *archive_name);
static void bbsink_homer_archive_contents(bbsink *sink, size_t len);
static void bbsink_homer_end_archive(bbsink *sink);
static void bbsink_homer_begin_manifest(bbsink *sink);
static void bbsink_homer_manifest_contents(bbsink *sink, size_t len);
static void bbsink_homer_end_manifest(bbsink *sink);
static void bbsink_homer_end_backup(bbsink *sink, XLogRecPtr endptr,
									TimeLineID endtli);
static void bbsink_homer_cleanup(bbsink *sink);

static const bbsink_ops bbsink_homer_ops = {
	.begin_backup = bbsink_homer_begin_backup,
	.begin_archive = bbsink_homer_begin_archive,
	.archive_contents = bbsink_homer_archive_contents,
	.end_archive = bbsink_homer_end_archive,
	.begin_manifest = bbsink_homer_begin_manifest,
	.manifest_contents = bbsink_homer_manifest_contents,
	.end_manifest = bbsink_homer_end_manifest,
	.end_backup = bbsink_homer_end_backup,
	.cleanup = bbsink_homer_cleanup
};

static void
bbsink_homer_error(const char *operation, const char *detail)
{
	ereport(ERROR,
			(errcode(ERRCODE_CONNECTION_FAILURE),
			 errmsg("Homer base backup target failed during %s", operation),
			 errdetail("%s", detail != NULL ? detail : "<no detail>")));
}

static void
bbsink_homer_apply_detail(HomerClientBaseBackupStreamOptions *options,
						  const char *target_detail)
{
	char	   *detail_copy = NULL;
	char	   *token = NULL;
	char	   *saveptr = NULL;

	if (target_detail == NULL || target_detail[0] == '\0')
		return;

	detail_copy = pstrdup(target_detail);
	for (token = strtok_r(detail_copy, ",", &saveptr);
		 token != NULL;
		 token = strtok_r(NULL, ",", &saveptr))
	{
		char	   *equals = strchr(token, '=');
		char	   *key = token;
		char	   *value = NULL;

		if (equals == NULL)
			ereport(ERROR,
					(errcode(ERRCODE_SYNTAX_ERROR),
					 errmsg("invalid Homer target detail token \"%s\"", token),
					 errhint("Use comma-separated key=value fields such as host=127.0.0.1,port=9717,node=1.")));
		*equals = '\0';
		value = equals + 1;

		if (strcmp(key, "host") == 0)
			strlcpy(options->peerHost, value, sizeof(options->peerHost));
		else if (strcmp(key, "port") == 0)
			options->peerControlPort = pg_strtoint32(value);
		else if (strcmp(key, "node") == 0)
			options->destinationNodeId = pg_strtoint32(value);
		else if (strcmp(key, "slots") == 0)
			options->slotCount = pg_strtoint32(value);
		else if (strcmp(key, "bytes") == 0)
			options->payloadCapacityBytes = pg_strtoint32(value);
		else if (strcmp(key, "dboid") == 0)
			options->databaseOid = pg_strtoint32(value);
		else if (strcmp(key, "useroid") == 0)
			options->userOid = pg_strtoint32(value);
		else
			ereport(ERROR,
					(errcode(ERRCODE_SYNTAX_ERROR),
					 errmsg("unrecognized Homer target detail key \"%s\"", key)));
	}

	if ((options->payloadCapacityBytes % BLCKSZ) != 0)
		ereport(ERROR,
				(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
				 errmsg("Homer base backup payload bytes must be a multiple of BLCKSZ"),
				 errdetail("payload_bytes=%u BLCKSZ=%u",
						   options->payloadCapacityBytes, BLCKSZ)));

	pfree(detail_copy);
}

static void
bbsink_homer_reserve_slot(bbsink_homer *mysink, const char *operation)
{
	void	   *payload = NULL;
	uint32		payload_capacity = 0;
	uint64		stream_sequence = 0;
	char		error[HOMER_CLIENT_ERROR_BYTES];

	if (!HomerClientReserveBaseBackupSlot(&mysink->stream,
										  &payload,
										  &payload_capacity,
										  &stream_sequence,
										  error,
										  sizeof(error)))
		bbsink_homer_error(operation, error);

	mysink->current_payload = payload;
	mysink->payload_capacity = payload_capacity;
	mysink->slot_reserved = true;
	mysink->base.bbs_buffer = payload;
	mysink->base.bbs_buffer_length = payload_capacity;
}

static void
bbsink_homer_submit_reserved_slot(bbsink_homer *mysink, uint32 object_kind,
								  uint32 archive_index, const char *name,
								  size_t payload_len, uint64 stream_offset,
								  const char *operation)
{
	char		error[HOMER_CLIENT_ERROR_BYTES];

	if (!mysink->slot_reserved)
		bbsink_homer_reserve_slot(mysink, operation);

	if (payload_len > UINT32_MAX)
		ereport(ERROR,
				(errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
				 errmsg("Homer base backup payload length exceeded uint32")));

	if (!HomerClientSubmitBaseBackupSlot(&mysink->stream,
										 object_kind,
										 archive_index,
										 name,
										 (uint32) payload_len,
										 stream_offset,
										 mysink->total_bytes,
										 0,
										 error,
										 sizeof(error)))
		bbsink_homer_error(operation, error);

	mysink->slot_reserved = false;
	mysink->current_payload = NULL;
}

static void
bbsink_homer_begin_backup(bbsink *sink)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;
	HomerClientBaseBackupStreamOptions options;
	char		error[HOMER_CLIENT_ERROR_BYTES];

	/*
	 * Keep the ordinary replication-command result-set lifecycle alive for
	 * pg_basebackup and other callers, but replace only the archive/manifest
	 * payload data path below.
	 */
	bbsink_begin_backup(sink->bbs_next, sink->bbs_state, sink->bbs_buffer_length);

	HomerClientDefaultBaseBackupStreamOptions(&options);
	options.databaseOid = MyDatabaseId;
	options.userOid = GetUserId();
	bbsink_homer_apply_detail(&options, mysink->target_detail);

	if (!HomerClientOpenControl(&mysink->control, error, sizeof(error)))
		bbsink_homer_error("open control", error);
	mysink->control_open = true;

	if (!HomerClientOpenBaseBackupStream(&mysink->control,
										 &options,
										 &mysink->stream,
										 error,
										 sizeof(error)))
		bbsink_homer_error("open stream", error);
	mysink->stream_open = true;

	bbsink_homer_reserve_slot(mysink, "reserve begin slot");
	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_BEGIN,
									  0,
									  "basebackup",
									  0,
									  0,
									  "submit begin");
	bbsink_homer_reserve_slot(mysink, "reserve first payload slot");
}

static void
bbsink_homer_begin_archive(bbsink *sink, const char *archive_name)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;

	mysink->archive_offset = 0;
	strlcpy(mysink->current_archive_name,
			archive_name,
			sizeof(mysink->current_archive_name));
	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_ARCHIVE_BEGIN,
									  mysink->archive_index,
									  archive_name,
									  0,
									  0,
									  "submit archive begin");
	bbsink_homer_reserve_slot(mysink, "reserve archive payload slot");
	bbsink_begin_archive(sink->bbs_next, archive_name);
}

static void
bbsink_homer_archive_contents(bbsink *sink, size_t len)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;
	uint64		chunk_offset = mysink->archive_offset;

	mysink->base.bbs_state->bytes_done += len;
	mysink->archive_offset += len;
	mysink->total_bytes += len;
	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_ARCHIVE_CHUNK,
									  mysink->archive_index,
									  mysink->current_archive_name,
									  len,
									  chunk_offset,
									  "submit archive chunk");
	bbsink_homer_reserve_slot(mysink, "reserve next archive payload slot");
}

static void
bbsink_homer_end_archive(bbsink *sink)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;

	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_ARCHIVE_END,
									  mysink->archive_index,
									  mysink->current_archive_name,
									  0,
									  mysink->archive_offset,
									  "submit archive end");
	mysink->base.bbs_state->tablespace_num++;
	mysink->archive_index++;
	bbsink_homer_reserve_slot(mysink, "reserve post-archive slot");
	bbsink_end_archive(sink->bbs_next);
}

static void
bbsink_homer_begin_manifest(bbsink *sink)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;

	mysink->archive_offset = 0;
	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_MANIFEST_BEGIN,
									  mysink->archive_index,
									  "backup_manifest",
									  0,
									  0,
									  "submit manifest begin");
	bbsink_homer_reserve_slot(mysink, "reserve manifest payload slot");
	bbsink_begin_manifest(sink->bbs_next);
}

static void
bbsink_homer_manifest_contents(bbsink *sink, size_t len)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;
	uint64		chunk_offset = mysink->archive_offset;

	mysink->archive_offset += len;
	mysink->total_bytes += len;
	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_MANIFEST_CHUNK,
									  mysink->archive_index,
									  "backup_manifest",
									  len,
									  chunk_offset,
									  "submit manifest chunk");
	bbsink_homer_reserve_slot(mysink, "reserve next manifest payload slot");
}

static void
bbsink_homer_end_manifest(bbsink *sink)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;

	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_MANIFEST_END,
									  mysink->archive_index,
									  "backup_manifest",
									  0,
									  mysink->archive_offset,
									  "submit manifest end");
	bbsink_homer_reserve_slot(mysink, "reserve end slot");
	bbsink_end_manifest(sink->bbs_next);
}

static void
bbsink_homer_end_backup(bbsink *sink, XLogRecPtr endptr, TimeLineID endtli)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;
	char		error[HOMER_CLIENT_ERROR_BYTES];

	bbsink_homer_submit_reserved_slot(mysink,
									  CITUS_REMOTE_BASEBACKUP_OBJECT_END,
									  mysink->archive_index,
									  "basebackup",
									  0,
									  0,
									  "submit end");
	if (!HomerClientCloseBaseBackupStream(&mysink->stream,
										  error,
										  sizeof(error)))
		bbsink_homer_error("close stream", error);
	mysink->stream_open = false;
	HomerClientCloseControl(&mysink->control);
	mysink->control_open = false;
	bbsink_end_backup(sink->bbs_next, endptr, endtli);
}

static void
bbsink_homer_cleanup(bbsink *sink)
{
	bbsink_homer *mysink = (bbsink_homer *) sink;

	if (mysink->stream_open)
	{
		char		error[HOMER_CLIENT_ERROR_BYTES];

		(void) HomerClientCloseBaseBackupStream(&mysink->stream,
												error,
												sizeof(error));
		mysink->stream_open = false;
	}
	if (mysink->control_open)
	{
		HomerClientCloseControl(&mysink->control);
		mysink->control_open = false;
	}
	bbsink_cleanup(sink->bbs_next);
}

/*
 * bbsink_homer_new constructs the service-backed target sink. The target detail
 * string is parsed later in begin_backup so default database/user OIDs can be
 * filled from the executing replication backend first.
 */
bbsink *
bbsink_homer_new(bbsink *next, char *target_detail)
{
	bbsink_homer *sink = palloc0(sizeof(bbsink_homer));

	*((const bbsink_ops **) &sink->base.bbs_ops) = &bbsink_homer_ops;
	sink->base.bbs_next = next;
	sink->target_detail = target_detail;

	return &sink->base;
}
