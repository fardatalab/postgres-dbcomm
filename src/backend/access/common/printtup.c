/*-------------------------------------------------------------------------
 *
 * printtup.c
 *	  Routines to print out tuples to the destination (both frontend
 *	  clients and standalone backends are supported here).
 *
 *
 * Portions Copyright (c) 1996-2024, PostgreSQL Global Development Group
 * Portions Copyright (c) 1994, Regents of the University of California
 *
 * IDENTIFICATION
 *	  src/backend/access/common/printtup.c
 *
 *-------------------------------------------------------------------------
 */
#include "postgres.h"

#include <errno.h>

#include "access/printtup.h"
#include "access/tupmacs.h"
#include "libpq/pqformat.h"
#include "libpq/protocol.h"
#include "storage/fd.h"
#include "tcop/pquery.h"
#include "timing_spots.h"
#include "utils/builtins.h"
#include "utils/lsyscache.h"
#include "utils/memdebug.h"
#include "utils/memutils.h"
#include "utils/regproc.h"

#include "time_instr.h"

typedef struct DR_printtup DR_printtup;

static void printtup_startup(DestReceiver *self, int operation,
							 TupleDesc typeinfo);
static bool printtup(TupleTableSlot *slot, DestReceiver *self);
static void printtup_shutdown(DestReceiver *self);
static void printtup_destroy(DestReceiver *self);

static void PrinttupBinaryDumpInit(DR_printtup *myState);
static void PrinttupBinaryDumpWriteSchema(DR_printtup *myState,
										  TupleDesc typeinfo,
										  int numAttrs);
static void PrinttupBinaryDumpWriteRow(DR_printtup *myState,
									   TupleDesc typeinfo,
									   TupleTableSlot *slot,
									   const int32 *serializedLengths,
									   char **serializedValues);
static void PrinttupBinaryDumpShutdown(DR_printtup *myState);

/* ----------------------------------------------------------------
 *		printtup / debugtup support
 * ----------------------------------------------------------------
 */

/* ----------------
 *		Private state for a printtup destination object
 *
 * NOTE: finfo is the lookup info for either typoutput or typsend, whichever
 * we are using for this column.
 * ----------------
 */
typedef struct
{								/* Per-attribute information */
	Oid			typoutput;		/* Oid for the type's text output fn */
	Oid			typsend;		/* Oid for the type's binary output fn */
	Oid			typreceive;		/* Oid for the type's binary input fn */
	Oid			typioparam;		/* I/O parameter for binary input */
	bool		typisvarlena;	/* is it varlena (ie possibly toastable)? */
	bool		attbyval;		/* whether the logical datum is by-value */
	bool		attisdropped;	/* keep dump metadata self-describing */
	int16		attlen;			/* length of the logical datum */
	char		attalign;		/* alignment requirement of the datum */
	char		attstorage;		/* storage strategy for the datum */
	int16		format;			/* format code for this column */
	char	   *attname;		/* cached attribute name for dump metadata */
	char	   *typename;		/* cached type name for dump metadata */
	char	   *typsendname;	/* cached binary output function name */
	char	   *typreceivename;	/* cached binary input function name */
	FmgrInfo	finfo;			/* Precomputed call info for output fn */
} PrinttupAttrInfo;

typedef struct DR_printtup
{
	DestReceiver pub;			/* publicly-known function pointers */
	Portal		portal;			/* the Portal we are printing from */
	bool		sendDescrip;	/* send RowDescription at startup? */
	TupleDesc	attrinfo;		/* The attr info we are set up for */
	int			nattrs;
	PrinttupAttrInfo *myinfo;	/* Cached info about each attr */
	StringInfoData buf;			/* output buffer (*not* in tmpcontext) */
	MemoryContext tmpcontext;	/* Memory context for per-row workspace */
	FILE	   *dumpFile;		/* optional binary dump sidecar */
	uint64		dumpSchemaId;	/* schema record id for current TupleDesc */
	uint64		dumpRowId;		/* monotonically increasing dumped row id */
	bool		dumpEnabled;	/* dump feature requested and file open */
	bool		dumpBinarySchema;	/* current TupleDesc is fully binary */
	bool		dumpWriteFailed;	/* stop dumping after first write failure */
} DR_printtup;

#define PRINTTUP_BINARY_DUMP_MAGIC "PTBDMP1"
#define PRINTTUP_BINARY_DUMP_VERSION 1
#define PRINTTUP_BINARY_DUMP_DEFAULT_FILE "printtup_binary_dump.bin"

/*
 * PrinttupBinaryDumpRequested controls whether the prototype dump path is
 * active. Keeping this behind an env var avoids adding overhead to the normal
 * hot path when the dump is not explicitly requested.
 */
static bool
PrinttupBinaryDumpRequested(void)
{
	static int		dumpRequested = -1;
	const char	   *envValue = NULL;

	if (dumpRequested >= 0)
		return dumpRequested == 1;

	envValue = getenv("PG_PRINTTUP_BINARY_DUMP");
	dumpRequested = (envValue != NULL &&
					 envValue[0] != '\0' &&
					 strcmp(envValue, "0") != 0) ? 1 : 0;

	return dumpRequested == 1;
}

/*
 * PrinttupBinaryDumpPath returns the relative or absolute file path used for
 * the binary dump. By default we use a relative path so the sidecar lands in
 * the backend's working directory as requested.
 */
static const char *
PrinttupBinaryDumpPath(void)
{
	const char *envValue = getenv("PG_PRINTTUP_BINARY_DUMP_FILE");

	if (envValue == NULL || envValue[0] == '\0')
		return PRINTTUP_BINARY_DUMP_DEFAULT_FILE;

	return envValue;
}

/*
 * PrinttupBinaryDumpDisable tears down the optional dump path after a write or
 * open failure. We deliberately log and disable instead of failing the user
 * query, because the dump is debugging instrumentation rather than the primary
 * query path.
 */
static void
PrinttupBinaryDumpDisable(DR_printtup *myState, const char *reason)
{
	if (myState->dumpFile != NULL)
	{
		FreeFile(myState->dumpFile);
		myState->dumpFile = NULL;
	}

	if (!myState->dumpWriteFailed)
	{
		elog(LOG, "disabling printtup binary dump: %s", reason);
		myState->dumpWriteFailed = true;
	}

	myState->dumpEnabled = false;
	myState->dumpBinarySchema = false;
}

/*
 * PrinttupBinaryDumpWriteExact writes the given byte span to the dump file.
 */
static bool
PrinttupBinaryDumpWriteExact(DR_printtup *myState, const void *data, size_t len)
{
	if (len == 0)
		return true;

	if (fwrite(data, 1, len, myState->dumpFile) != len)
	{
		char		errorBuffer[256];

		snprintf(errorBuffer, sizeof(errorBuffer),
				 "failed to write \"%s\": %m",
				 PrinttupBinaryDumpPath());
		PrinttupBinaryDumpDisable(myState, errorBuffer);
		return false;
	}

	return true;
}

/*
 * PrinttupBinaryDumpAppendString appends a length-prefixed string to a record.
 */
static void
PrinttupBinaryDumpAppendString(StringInfo record, const char *value)
{
	int32		stringLength = value ? (int32) strlen(value) : -1;

	pq_sendint32(record, stringLength);
	if (stringLength > 0)
		pq_sendbytes(record, value, stringLength);
}

/*
 * PrinttupBinaryDumpWriteRecord writes one tagged dump record.
 */
static bool
PrinttupBinaryDumpWriteRecord(DR_printtup *myState, const char *recordTag,
							  StringInfo record)
{
	uint32		recordLength = pg_hton32((uint32) record->len);

	Assert(strlen(recordTag) == 4);

	if (!PrinttupBinaryDumpWriteExact(myState, recordTag, 4))
		return false;
	if (!PrinttupBinaryDumpWriteExact(myState, &recordLength, sizeof(recordLength)))
		return false;
	if (!PrinttupBinaryDumpWriteExact(myState, record->data, record->len))
		return false;

	return true;
}

/*
 * PrinttupBinaryDumpNormalizeDatumBytes produces a portable serializer input
 * blob for a single attribute. We intentionally flatten varlena values so the
 * dump does not depend on backend-private pointers or external TOAST storage.
 */
static char *
PrinttupBinaryDumpNormalizeDatumBytes(Form_pg_attribute attr, Datum datum,
									  Size *valueLength)
{
	if (attr->attbyval)
	{
		char	   *copyBytes = palloc(attr->attlen);

		store_att_byval(copyBytes, datum, attr->attlen);
		*valueLength = attr->attlen;
		return copyBytes;
	}

	if (attr->attlen == -1)
	{
		struct varlena *flatValue = PG_DETOAST_DATUM(datum);

		*valueLength = VARSIZE(flatValue);
		return (char *) flatValue;
	}

	if (attr->attlen == -2)
	{
		char	   *cstringValue = DatumGetCString(datum);

		*valueLength = strlen(cstringValue) + 1;
		return cstringValue;
	}

	*valueLength = attr->attlen;
	return DatumGetPointer(datum);
}

/* ----------------
 *		Initialize: create a DestReceiver for printtup
 * ----------------
 */
DestReceiver *
printtup_create_DR(CommandDest dest)
{
	DR_printtup *self = (DR_printtup *) palloc0(sizeof(DR_printtup));

	self->pub.receiveSlot = printtup;	/* might get changed later */
	self->pub.rStartup = printtup_startup;
	self->pub.rShutdown = printtup_shutdown;
	self->pub.rDestroy = printtup_destroy;
	self->pub.mydest = dest;

	/*
	 * Send T message automatically if DestRemote, but not if
	 * DestRemoteExecute
	 */
	self->sendDescrip = (dest == DestRemote);

	self->attrinfo = NULL;
	self->nattrs = 0;
	self->myinfo = NULL;
	self->buf.data = NULL;
	self->tmpcontext = NULL;

	return (DestReceiver *) self;
}

/*
 * Set parameters for a DestRemote (or DestRemoteExecute) receiver
 */
void
SetRemoteDestReceiverParams(DestReceiver *self, Portal portal)
{
	DR_printtup *myState = (DR_printtup *) self;

	Assert(myState->pub.mydest == DestRemote ||
		   myState->pub.mydest == DestRemoteExecute);

	myState->portal = portal;
}

/*
 * PrinttupBinaryDumpInit opens the optional dump sidecar and emits a small
 * header once for new files.
 */
static void
PrinttupBinaryDumpInit(DR_printtup *myState)
{
	StringInfoData headerRecord;
	long		fileOffset = 0;

	if (!PrinttupBinaryDumpRequested())
		return;

	myState->dumpFile = AllocateFile(PrinttupBinaryDumpPath(), "ab+");
	if (myState->dumpFile == NULL)
	{
		char		errorBuffer[256];

		snprintf(errorBuffer, sizeof(errorBuffer),
				 "failed to open \"%s\": %m",
				 PrinttupBinaryDumpPath());
		PrinttupBinaryDumpDisable(myState, errorBuffer);
		return;
	}

	if (fseek(myState->dumpFile, 0L, SEEK_END) != 0)
	{
		PrinttupBinaryDumpDisable(myState, "failed to seek dump file");
		return;
	}

	fileOffset = ftell(myState->dumpFile);
	if (fileOffset < 0)
	{
		PrinttupBinaryDumpDisable(myState, "failed to inspect dump file size");
		return;
	}

	myState->dumpEnabled = true;
	myState->dumpBinarySchema = false;

	if (fileOffset != 0)
		return;

	initStringInfo(&headerRecord);
	PrinttupBinaryDumpAppendString(&headerRecord, PRINTTUP_BINARY_DUMP_MAGIC);
	pq_sendint32(&headerRecord, PRINTTUP_BINARY_DUMP_VERSION);
#ifdef WORDS_BIGENDIAN
	pq_sendbyte(&headerRecord, 1);
#else
	pq_sendbyte(&headerRecord, 0);
#endif
	pq_sendbyte(&headerRecord, sizeof(Datum));
	pq_sendbyte(&headerRecord, sizeof(void *));
	pq_sendint32(&headerRecord, PG_VERSION_NUM);

	if (!PrinttupBinaryDumpWriteRecord(myState, "HEAD", &headerRecord))
	{
		if (headerRecord.data != NULL)
			pfree(headerRecord.data);
		return;
	}

	if (fflush(myState->dumpFile) != 0)
		PrinttupBinaryDumpDisable(myState, "failed to flush dump header");

	if (headerRecord.data != NULL)
		pfree(headerRecord.data);
}

/*
 * PrinttupBinaryDumpWriteSchema records the serializer lookup metadata for the
 * current TupleDesc. We only dump descriptors that are fully binary, because
 * the current prototype intentionally excludes text mode.
 */
static void
PrinttupBinaryDumpWriteSchema(DR_printtup *myState, TupleDesc typeinfo,
							  int numAttrs)
{
	StringInfoData schemaRecord;

	if (!myState->dumpEnabled)
		return;

	myState->dumpSchemaId++;
	myState->dumpBinarySchema = true;

	for (int i = 0; i < numAttrs; i++)
	{
		PrinttupAttrInfo *thisState = myState->myinfo + i;

		if (thisState->format != 1)
		{
			myState->dumpBinarySchema = false;
			break;
		}
	}

	if (!myState->dumpBinarySchema)
	{
		elog(LOG, "printtup binary dump skipped a non-binary descriptor");
		return;
	}

	initStringInfo(&schemaRecord);
	pq_sendint64(&schemaRecord, myState->dumpSchemaId);
	pq_sendint32(&schemaRecord, numAttrs);

	for (int i = 0; i < numAttrs; i++)
	{
		PrinttupAttrInfo *thisState = myState->myinfo + i;
		Form_pg_attribute attr = TupleDescAttr(typeinfo, i);

		pq_sendint32(&schemaRecord, i + 1);
		pq_sendint32(&schemaRecord, attr->atttypid);
		pq_sendint32(&schemaRecord, attr->atttypmod);
		pq_sendint16(&schemaRecord, attr->attlen);
		pq_sendbyte(&schemaRecord, attr->attbyval ? 1 : 0);
		pq_sendbyte(&schemaRecord, attr->attisdropped ? 1 : 0);
		pq_sendbyte(&schemaRecord, (uint8) attr->attalign);
		pq_sendbyte(&schemaRecord, (uint8) attr->attstorage);
		pq_sendint16(&schemaRecord, thisState->format);
		pq_sendint32(&schemaRecord, thisState->typsend);
		pq_sendint32(&schemaRecord, thisState->typreceive);
		pq_sendint32(&schemaRecord, thisState->typioparam);
		PrinttupBinaryDumpAppendString(&schemaRecord, thisState->attname);
		PrinttupBinaryDumpAppendString(&schemaRecord, thisState->typename);
		PrinttupBinaryDumpAppendString(&schemaRecord, thisState->typsendname);
		PrinttupBinaryDumpAppendString(&schemaRecord, thisState->typreceivename);
	}

	if (!PrinttupBinaryDumpWriteRecord(myState, "SCHM", &schemaRecord))
	{
		if (schemaRecord.data != NULL)
			pfree(schemaRecord.data);
		return;
	}

	if (fflush(myState->dumpFile) != 0)
		PrinttupBinaryDumpDisable(myState, "failed to flush schema dump");

	if (schemaRecord.data != NULL)
		pfree(schemaRecord.data);
}

/*
 * PrinttupBinaryDumpWriteRow records both the normalized serializer inputs and
 * the resulting serialized binary field payloads for one row.
 */
static void
PrinttupBinaryDumpWriteRow(DR_printtup *myState, TupleDesc typeinfo,
						   TupleTableSlot *slot, const int32 *serializedLengths,
						   char **serializedValues)
{
	StringInfoData rowRecord;
	int			natts = typeinfo->natts;

	if (!myState->dumpEnabled || !myState->dumpBinarySchema)
		return;

	initStringInfo(&rowRecord);
	pq_sendint64(&rowRecord, myState->dumpSchemaId);
	pq_sendint64(&rowRecord, ++myState->dumpRowId);
	pq_sendint32(&rowRecord, natts);
	pq_sendint32(&rowRecord, myState->buf.len);
	pq_sendbytes(&rowRecord, myState->buf.data, myState->buf.len);

	for (int i = 0; i < natts; i++)
	{
		Form_pg_attribute attr = TupleDescAttr(typeinfo, i);

		if (slot->tts_isnull[i])
		{
			pq_sendint32(&rowRecord, (uint32) -1);
			pq_sendint32(&rowRecord, (uint32) -1);
			continue;
		}
		else
		{
			Size		normalizedLength = 0;
			char	   *normalizedBytes = NULL;

			normalizedBytes =
				PrinttupBinaryDumpNormalizeDatumBytes(attr,
													  slot->tts_values[i],
													  &normalizedLength);
			pq_sendint32(&rowRecord, normalizedLength);
			pq_sendbytes(&rowRecord, normalizedBytes, normalizedLength);
			pq_sendint32(&rowRecord, serializedLengths[i]);
			pq_sendbytes(&rowRecord, serializedValues[i], serializedLengths[i]);
		}
	}

	if (!PrinttupBinaryDumpWriteRecord(myState, "ROWD", &rowRecord))
	{
		if (rowRecord.data != NULL)
			pfree(rowRecord.data);
		return;
	}

	if (fflush(myState->dumpFile) != 0)
		PrinttupBinaryDumpDisable(myState, "failed to flush row dump");

	if (rowRecord.data != NULL)
		pfree(rowRecord.data);
}

/*
 * PrinttupBinaryDumpShutdown closes the optional dump file.
 */
static void
PrinttupBinaryDumpShutdown(DR_printtup *myState)
{
	if (myState->dumpFile != NULL)
	{
		if (fflush(myState->dumpFile) != 0)
			elog(LOG, "failed to flush printtup binary dump during shutdown");
		FreeFile(myState->dumpFile);
		myState->dumpFile = NULL;
	}

	myState->dumpEnabled = false;
	myState->dumpBinarySchema = false;
}

static void
printtup_startup(DestReceiver *self, int operation, TupleDesc typeinfo)
{
	DR_printtup *myState = (DR_printtup *) self;
	Portal		portal = myState->portal;

	/*
	 * Create I/O buffer to be used for all messages.  This cannot be inside
	 * tmpcontext, since we want to re-use it across rows.
	 */
	initStringInfo(&myState->buf);
	PrinttupBinaryDumpInit(myState);

	/*
	 * Create a temporary memory context that we can reset once per row to
	 * recover palloc'd memory.  This avoids any problems with leaks inside
	 * datatype output routines, and should be faster than retail pfree's
	 * anyway.
	 */
	myState->tmpcontext = AllocSetContextCreate(CurrentMemoryContext,
												"printtup",
												ALLOCSET_DEFAULT_SIZES);

	/*
	 * If we are supposed to emit row descriptions, then send the tuple
	 * descriptor of the tuples.
	 */
	if (myState->sendDescrip)
		SendRowDescriptionMessage(&myState->buf,
								  typeinfo,
								  FetchPortalTargetList(portal),
								  portal->formats);

	/* ----------------
	 * We could set up the derived attr info at this time, but we postpone it
	 * until the first call of printtup, for 2 reasons:
	 * 1. We don't waste time (compared to the old way) if there are no
	 *	  tuples at all to output.
	 * 2. Checking in printtup allows us to handle the case that the tuples
	 *	  change type midway through (although this probably can't happen in
	 *	  the current executor).
	 * ----------------
	 */
}

/*
 * SendRowDescriptionMessage --- send a RowDescription message to the frontend
 *
 * Notes: the TupleDesc has typically been manufactured by ExecTypeFromTL()
 * or some similar function; it does not contain a full set of fields.
 * The targetlist will be NIL when executing a utility function that does
 * not have a plan.  If the targetlist isn't NIL then it is a Query node's
 * targetlist; it is up to us to ignore resjunk columns in it.  The formats[]
 * array pointer might be NULL (if we are doing Describe on a prepared stmt);
 * send zeroes for the format codes in that case.
 */
void
SendRowDescriptionMessage(StringInfo buf, TupleDesc typeinfo,
						  List *targetlist, int16 *formats)
{
	int			natts = typeinfo->natts;
	int			i;
	ListCell   *tlist_item = list_head(targetlist);

	/* tuple descriptor message type */
	pq_beginmessage_reuse(buf, PqMsg_RowDescription);
	/* # of attrs in tuples */
	pq_sendint16(buf, natts);

	/*
	 * Preallocate memory for the entire message to be sent. That allows to
	 * use the significantly faster inline pqformat.h functions and to avoid
	 * reallocations.
	 *
	 * Have to overestimate the size of the column-names, to account for
	 * character set overhead.
	 */
	enlargeStringInfo(buf, (NAMEDATALEN * MAX_CONVERSION_GROWTH /* attname */
							+ sizeof(Oid)	/* resorigtbl */
							+ sizeof(AttrNumber)	/* resorigcol */
							+ sizeof(Oid)	/* atttypid */
							+ sizeof(int16) /* attlen */
							+ sizeof(int32) /* attypmod */
							+ sizeof(int16) /* format */
							) * natts);

	for (i = 0; i < natts; ++i)
	{
		Form_pg_attribute att = TupleDescAttr(typeinfo, i);
		Oid			atttypid = att->atttypid;
		int32		atttypmod = att->atttypmod;
		Oid			resorigtbl;
		AttrNumber	resorigcol;
		int16		format;

		/*
		 * If column is a domain, send the base type and typmod instead.
		 * Lookup before sending any ints, for efficiency.
		 */
		atttypid = getBaseTypeAndTypmod(atttypid, &atttypmod);

		/* Do we have a non-resjunk tlist item? */
		while (tlist_item &&
			   ((TargetEntry *) lfirst(tlist_item))->resjunk)
			tlist_item = lnext(targetlist, tlist_item);
		if (tlist_item)
		{
			TargetEntry *tle = (TargetEntry *) lfirst(tlist_item);

			resorigtbl = tle->resorigtbl;
			resorigcol = tle->resorigcol;
			tlist_item = lnext(targetlist, tlist_item);
		}
		else
		{
			/* No info available, so send zeroes */
			resorigtbl = 0;
			resorigcol = 0;
		}

		if (formats)
			format = formats[i];
		else
			format = 0;

		pq_writestring(buf, NameStr(att->attname));
		pq_writeint32(buf, resorigtbl);
		pq_writeint16(buf, resorigcol);
		pq_writeint32(buf, atttypid);
		pq_writeint16(buf, att->attlen);
		pq_writeint32(buf, atttypmod);
		pq_writeint16(buf, format);
	}

	pq_endmessage_reuse(buf);
}

/*
 * Get the lookup info that printtup() needs
 */
static void
printtup_prepare_info(DR_printtup *myState, TupleDesc typeinfo, int numAttrs)
{
	int16	   *formats = myState->portal->formats;
	int			i;

	/* get rid of any old data */
	if (myState->myinfo)
		pfree(myState->myinfo);
	myState->myinfo = NULL;

	myState->attrinfo = typeinfo;
	myState->nattrs = numAttrs;
	if (numAttrs <= 0)
		return;

	myState->myinfo = (PrinttupAttrInfo *)
		palloc0(numAttrs * sizeof(PrinttupAttrInfo));

	for (i = 0; i < numAttrs; i++)
	{
		PrinttupAttrInfo *thisState = myState->myinfo + i;
		int16		format = (formats ? formats[i] : 0);
		Form_pg_attribute attr = TupleDescAttr(typeinfo, i);

		thisState->format = format;
		thisState->attbyval = attr->attbyval;
		thisState->attisdropped = attr->attisdropped;
		thisState->attlen = attr->attlen;
		thisState->attalign = attr->attalign;
		thisState->attstorage = attr->attstorage;
		thisState->attname = pstrdup(NameStr(attr->attname));
		thisState->typename = format_type_be(attr->atttypid);
		if (format == 0)
		{
			getTypeOutputInfo(attr->atttypid,
							  &thisState->typoutput,
							  &thisState->typisvarlena);
			fmgr_info(thisState->typoutput, &thisState->finfo);
		}
		else if (format == 1)
		{
			getTypeBinaryOutputInfo(attr->atttypid,
									&thisState->typsend,
									&thisState->typisvarlena);
			getTypeBinaryInputInfo(attr->atttypid,
								   &thisState->typreceive,
								   &thisState->typioparam);
			fmgr_info(thisState->typsend, &thisState->finfo);
			thisState->typsendname =
				format_procedure_qualified(thisState->typsend);
			thisState->typreceivename =
				format_procedure_qualified(thisState->typreceive);
		}
		else
			ereport(ERROR,
					(errcode(ERRCODE_INVALID_PARAMETER_VALUE),
					 errmsg("unsupported format code: %d", format)));
	}

	PrinttupBinaryDumpWriteSchema(myState, typeinfo, numAttrs);
}

/* ----------------
 *		printtup --- send a tuple to the client
 *
 * Note: if you change this function, see also serializeAnalyzeReceive
 * in explain.c, which is meant to replicate the computations done here.
 * ----------------
 */
static bool
printtup(TupleTableSlot *slot, DestReceiver *self)
{
	TupleDesc	typeinfo = slot->tts_tupleDescriptor;
	DR_printtup *myState = (DR_printtup *) self;
	MemoryContext oldcontext;
	StringInfo	buf = &myState->buf;
	int			natts = typeinfo->natts;
	int			i;
	int32	   *serializedLengths = NULL;
	char	  **serializedValues = NULL;

    // jason: timing the printtup (send row) process for the adaptive executor (send half of ReceiveResults())
    timing_add_stat(Printtup, STAT_TOTAL_ROWS_SENT, 1);
    timing_start(Printtup);

    /* Set or update my derived attribute info, if needed */
	if (myState->attrinfo != typeinfo || myState->nattrs != natts)
		printtup_prepare_info(myState, typeinfo, natts);

	/* Make sure the tuple is fully deconstructed */
	slot_getallattrs(slot);

	/* Switch into per-row context so we can recover memory below */
	oldcontext = MemoryContextSwitchTo(myState->tmpcontext);

	if (myState->dumpEnabled && myState->dumpBinarySchema)
	{
		serializedLengths = palloc0(natts * sizeof(int32));
		serializedValues = palloc0(natts * sizeof(char *));
	}

	/*
	 * Prepare a DataRow message (note buffer is in per-query context)
	 */
	pq_beginmessage_reuse(buf, PqMsg_DataRow);

	pq_sendint16(buf, natts);

	/*
	 * send the attributes of this tuple
	 */
	for (i = 0; i < natts; ++i)
	{
		PrinttupAttrInfo *thisState = myState->myinfo + i;
		Datum		attr = slot->tts_values[i];

		if (slot->tts_isnull[i])
		{
			pq_sendint32(buf, -1);
			continue;
		}

		/*
		 * Here we catch undefined bytes in datums that are returned to the
		 * client without hitting disk; see comments at the related check in
		 * PageAddItem().  This test is most useful for uncompressed,
		 * non-external datums, but we're quite likely to see such here when
		 * testing new C functions.
		 */
		if (thisState->typisvarlena)
			VALGRIND_CHECK_MEM_IS_DEFINED(DatumGetPointer(attr),
										  VARSIZE_ANY(attr));

		if (thisState->format == 0)
		{
			/* Text output */
			char	   *outputstr;

			outputstr = OutputFunctionCall(&thisState->finfo, attr);
			pq_sendcountedtext(buf, outputstr, strlen(outputstr));
		}
		else
		{
			/* Binary output */
			bytea	   *outputbytes;
			int32		outputLength;

			outputbytes = SendFunctionCall(&thisState->finfo, attr);
			outputLength = VARSIZE(outputbytes) - VARHDRSZ;
			if (serializedLengths != NULL)
			{
				serializedLengths[i] = outputLength;
				serializedValues[i] = VARDATA(outputbytes);
			}
			pq_sendint32(buf, outputLength);
			pq_sendbytes(buf, VARDATA(outputbytes), outputLength);
		}
	}

	if (serializedLengths != NULL)
		PrinttupBinaryDumpWriteRow(myState, typeinfo, slot,
								   serializedLengths, serializedValues);

	    // jason: timing the network send part
	    timing_start(Printtup_Net);

    timing_add_stat(Printtup, STAT_TOTAL_BYTES_SENT, buf->len);

    pq_endmessage_reuse(buf);

    timing_end(Printtup_Net);

    /* Return to caller's context, and flush row's temporary memory */
	MemoryContextSwitchTo(oldcontext);
	MemoryContextReset(myState->tmpcontext);

    // jason: end timing
    timing_end(Printtup);

    return true;
}

/* ----------------
 *		printtup_shutdown
 * ----------------
 */
static void
printtup_shutdown(DestReceiver *self)
{
	DR_printtup *myState = (DR_printtup *) self;

	if (myState->myinfo)
		pfree(myState->myinfo);
	myState->myinfo = NULL;

	myState->attrinfo = NULL;

	if (myState->buf.data)
		pfree(myState->buf.data);
	myState->buf.data = NULL;

	PrinttupBinaryDumpShutdown(myState);

	if (myState->tmpcontext)
		MemoryContextDelete(myState->tmpcontext);
	myState->tmpcontext = NULL;
}

/* ----------------
 *		printtup_destroy
 * ----------------
 */
static void
printtup_destroy(DestReceiver *self)
{
	pfree(self);
}

/* ----------------
 *		printatt
 * ----------------
 */
static void
printatt(unsigned attributeId,
		 Form_pg_attribute attributeP,
		 char *value)
{
	printf("\t%2d: %s%s%s%s\t(typeid = %u, len = %d, typmod = %d, byval = %c)\n",
		   attributeId,
		   NameStr(attributeP->attname),
		   value != NULL ? " = \"" : "",
		   value != NULL ? value : "",
		   value != NULL ? "\"" : "",
		   (unsigned int) (attributeP->atttypid),
		   attributeP->attlen,
		   attributeP->atttypmod,
		   attributeP->attbyval ? 't' : 'f');
}

/* ----------------
 *		debugStartup - prepare to print tuples for an interactive backend
 * ----------------
 */
void
debugStartup(DestReceiver *self, int operation, TupleDesc typeinfo)
{
	int			natts = typeinfo->natts;
	int			i;

	/*
	 * show the return type of the tuples
	 */
	for (i = 0; i < natts; ++i)
		printatt((unsigned) i + 1, TupleDescAttr(typeinfo, i), NULL);
	printf("\t----\n");
}

/* ----------------
 *		debugtup - print one tuple for an interactive backend
 * ----------------
 */
bool
debugtup(TupleTableSlot *slot, DestReceiver *self)
{
	TupleDesc	typeinfo = slot->tts_tupleDescriptor;
	int			natts = typeinfo->natts;
	int			i;
	Datum		attr;
	char	   *value;
	bool		isnull;
	Oid			typoutput;
	bool		typisvarlena;

	for (i = 0; i < natts; ++i)
	{
		attr = slot_getattr(slot, i + 1, &isnull);
		if (isnull)
			continue;
		getTypeOutputInfo(TupleDescAttr(typeinfo, i)->atttypid,
						  &typoutput, &typisvarlena);

		value = OidOutputFunctionCall(typoutput, attr);

		printatt((unsigned) i + 1, TupleDescAttr(typeinfo, i), value);
	}
	printf("\t----\n");

	return true;
}
