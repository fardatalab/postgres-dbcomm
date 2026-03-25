#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/*
 * This is a standalone verifier for the binary dump emitted from printtup.c.
 * It intentionally duplicates a narrow subset of PostgreSQL's binary send
 * functions for the lineitem schema so we can replay the dump out of band and
 * byte-compare the results against the captured ground truth.
 */

#define NUMERIC_SIGN_MASK 0xC000
#define NUMERIC_POS 0x0000
#define NUMERIC_NEG 0x4000
#define NUMERIC_SHORT 0x8000
#define NUMERIC_SPECIAL 0xC000
#define NUMERIC_EXT_SIGN_MASK 0xF000
#define NUMERIC_SHORT_SIGN_MASK 0x2000
#define NUMERIC_SHORT_DSCALE_MASK 0x1F80
#define NUMERIC_SHORT_DSCALE_SHIFT 7
#define NUMERIC_SHORT_WEIGHT_SIGN_MASK 0x0040
#define NUMERIC_SHORT_WEIGHT_MASK 0x003F
#define NUMERIC_DSCALE_MASK 0x3FFF

typedef struct
{
	bool		valid;
	bool		words_bigendian;
	uint8_t		datum_size;
	uint8_t		pointer_size;
	uint32_t	pg_version_num;
} HeaderRecord;

typedef struct
{
	uint32_t	attnum;
	uint32_t	atttypid;
	int32_t		atttypmod;
	int16_t		attlen;
	bool		attbyval;
	bool		attisdropped;
	char		attalign;
	char		attstorage;
	uint16_t	fmt;
	uint32_t	typsend;
	uint32_t	typreceive;
	uint32_t	typioparam;
	char	   *attname;
	char	   *typename;
	char	   *sendname;
	char	   *recvname;
} AttrMeta;

typedef struct
{
	uint64_t	schema_id;
	uint32_t	natts;
	AttrMeta    *attrs;
} SchemaRecord;

typedef struct
{
	uint8_t    *data;
	size_t		len;
	size_t		capacity;
} ByteBuf;

typedef struct
{
	bool		is_null;
	const uint8_t *normalized;
	size_t		normalized_len;
	const uint8_t *serialized;
	size_t		serialized_len;
} FieldView;

typedef struct
{
	bool		valid;
	size_t		total_size;
	size_t		header_size;
	const uint8_t *payload;
	size_t		payload_len;
} VarlenaView;

typedef struct
{
	HeaderRecord header;
	SchemaRecord *schemas;
	size_t		schema_count;
	size_t		schema_capacity;
	uint32_t	head_count;
	uint32_t	schema_record_count;
	uint32_t	row_record_count;
	uint64_t	rows_checked;
	uint64_t	fields_checked;
	uint64_t	mismatches;
	uint64_t	max_mismatches;
	uint64_t	type_serialize_ns;
	uint64_t	serialize_proxy_ns;
	uint64_t	serialize_rows;
	uint64_t	serialize_fields;
	uint64_t	type_serialize_bytes;
	uint64_t	serialize_proxy_bytes;
} VerifierState;

static void
die_errno(const char *message)
{
	fprintf(stderr, "%s: %s\n", message, strerror(errno));
	exit(1);
}

static uint64_t
monotonic_now_ns(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
		die_errno("clock_gettime failed");

	return (uint64_t) ts.tv_sec * 1000000000ULL + (uint64_t) ts.tv_nsec;
}

static void
die_parse(const char *message)
{
	fprintf(stderr, "%s\n", message);
	exit(1);
}

static void *
xmalloc(size_t size)
{
	void	   *ptr = malloc(size == 0 ? 1 : size);

	if (ptr == NULL)
		die_errno("malloc failed");
	return ptr;
}

static void *
xrealloc(void *ptr, size_t size)
{
	void	   *newptr = realloc(ptr, size == 0 ? 1 : size);

	if (newptr == NULL)
		die_errno("realloc failed");
	return newptr;
}

static char *
xstrdup_len(const uint8_t *data, size_t len)
{
	char	   *copy = xmalloc(len + 1);

	memcpy(copy, data, len);
	copy[len] = '\0';
	return copy;
}

static void
bytebuf_init(ByteBuf *buf)
{
	memset(buf, 0, sizeof(*buf));
}

static void
bytebuf_reserve(ByteBuf *buf, size_t extra)
{
	size_t		needed = buf->len + extra;
	size_t		newcap = buf->capacity ? buf->capacity : 64;

	if (needed <= buf->capacity)
		return;

	while (newcap < needed)
		newcap *= 2;

	buf->data = xrealloc(buf->data, newcap);
	buf->capacity = newcap;
}

static void
bytebuf_append(ByteBuf *buf, const void *data, size_t len)
{
	bytebuf_reserve(buf, len);
	memcpy(buf->data + buf->len, data, len);
	buf->len += len;
}

static void
bytebuf_append_u16be(ByteBuf *buf, uint16_t value)
{
	uint8_t		bytes[2];

	bytes[0] = (uint8_t) ((value >> 8) & 0xFF);
	bytes[1] = (uint8_t) (value & 0xFF);
	bytebuf_append(buf, bytes, sizeof(bytes));
}

static void
bytebuf_append_u32be(ByteBuf *buf, uint32_t value)
{
	uint8_t		bytes[4];

	bytes[0] = (uint8_t) ((value >> 24) & 0xFF);
	bytes[1] = (uint8_t) ((value >> 16) & 0xFF);
	bytes[2] = (uint8_t) ((value >> 8) & 0xFF);
	bytes[3] = (uint8_t) (value & 0xFF);
	bytebuf_append(buf, bytes, sizeof(bytes));
}

static void
bytebuf_free(ByteBuf *buf)
{
	free(buf->data);
	memset(buf, 0, sizeof(*buf));
}

static uint16_t
read_be_u16(const uint8_t *data)
{
	return ((uint16_t) data[0] << 8) | (uint16_t) data[1];
}

static uint32_t
read_be_u32(const uint8_t *data)
{
	return ((uint32_t) data[0] << 24) |
		((uint32_t) data[1] << 16) |
		((uint32_t) data[2] << 8) |
		(uint32_t) data[3];
}

static uint64_t
read_be_u64(const uint8_t *data)
{
	return ((uint64_t) data[0] << 56) |
		((uint64_t) data[1] << 48) |
		((uint64_t) data[2] << 40) |
		((uint64_t) data[3] << 32) |
		((uint64_t) data[4] << 24) |
		((uint64_t) data[5] << 16) |
		((uint64_t) data[6] << 8) |
		(uint64_t) data[7];
}

static int32_t
read_be_i32(const uint8_t *data)
{
	return (int32_t) read_be_u32(data);
}

static uint16_t
read_dump_u16(const HeaderRecord *header, const uint8_t *data)
{
	if (header->words_bigendian)
		return ((uint16_t) data[0] << 8) | (uint16_t) data[1];
	return ((uint16_t) data[1] << 8) | (uint16_t) data[0];
}

static int16_t
read_dump_i16(const HeaderRecord *header, const uint8_t *data)
{
	return (int16_t) read_dump_u16(header, data);
}

static uint32_t
read_dump_u32(const HeaderRecord *header, const uint8_t *data)
{
	if (header->words_bigendian)
		return ((uint32_t) data[0] << 24) |
			((uint32_t) data[1] << 16) |
			((uint32_t) data[2] << 8) |
			(uint32_t) data[3];
	return ((uint32_t) data[3] << 24) |
		((uint32_t) data[2] << 16) |
		((uint32_t) data[1] << 8) |
		(uint32_t) data[0];
}

static int32_t
read_dump_i32(const HeaderRecord *header, const uint8_t *data)
{
	return (int32_t) read_dump_u32(header, data);
}

static void
print_hex_snippet(const uint8_t *data, size_t len)
{
	size_t		limit = len < 64 ? len : 64;

	for (size_t i = 0; i < limit; i++)
		fprintf(stderr, "%02x", data[i]);
	if (len > limit)
		fprintf(stderr, "...");
}

static char *
read_len_string(const uint8_t *payload, size_t payload_len, size_t *pos)
{
	int32_t		string_len;

	if (*pos + 4 > payload_len)
		die_parse("truncated length-prefixed string");

	string_len = read_be_i32(payload + *pos);
	*pos += 4;

	if (string_len < 0)
		return NULL;

	if (*pos + (size_t) string_len > payload_len)
		die_parse("truncated string payload");

	char *value = xstrdup_len(payload + *pos, (size_t) string_len);
	*pos += (size_t) string_len;
	return value;
}

static SchemaRecord *
find_schema(VerifierState *state, uint64_t schema_id)
{
	for (size_t i = 0; i < state->schema_count; i++)
	{
		if (state->schemas[i].schema_id == schema_id)
			return &state->schemas[i];
	}
	return NULL;
}

/*
 * parse_schema_record stores the binary serializer metadata prepared in
 * printtup_prepare_info() so later row records can dispatch by send function.
 */
static void
parse_schema_record(VerifierState *state, const uint8_t *payload, size_t payload_len)
{
	size_t		pos = 0;
	uint64_t	schema_id;
	uint32_t	natts;
	SchemaRecord *schema;

	if (payload_len < 12)
		die_parse("schema record is truncated");

	schema_id = read_be_u64(payload + pos);
	pos += 8;
	natts = read_be_u32(payload + pos);
	pos += 4;

	if (find_schema(state, schema_id) != NULL)
		die_parse("duplicate schema id in dump");

	if (state->schema_count == state->schema_capacity)
	{
		size_t newcap = state->schema_capacity ? state->schema_capacity * 2 : 4;

		state->schemas = xrealloc(state->schemas, newcap * sizeof(SchemaRecord));
		memset(state->schemas + state->schema_capacity, 0,
			   (newcap - state->schema_capacity) * sizeof(SchemaRecord));
		state->schema_capacity = newcap;
	}

	schema = &state->schemas[state->schema_count++];
	memset(schema, 0, sizeof(*schema));
	schema->schema_id = schema_id;
	schema->natts = natts;
	schema->attrs = xmalloc((size_t) natts * sizeof(AttrMeta));
	memset(schema->attrs, 0, (size_t) natts * sizeof(AttrMeta));

	for (uint32_t i = 0; i < natts; i++)
	{
		AttrMeta   *attr = &schema->attrs[i];
		uint16_t	attlen_raw;

		if (pos + 30 > payload_len)
			die_parse("schema attribute record is truncated");

		attr->attnum = read_be_u32(payload + pos);
		pos += 4;
		attr->atttypid = read_be_u32(payload + pos);
		pos += 4;
		attr->atttypmod = read_be_i32(payload + pos);
		pos += 4;
		attlen_raw = read_be_u16(payload + pos);
		pos += 2;
		attr->attlen = (attlen_raw < 0x8000) ? (int16_t) attlen_raw : (int16_t) (attlen_raw - 0x10000);
		attr->attbyval = payload[pos++] == 1;
		attr->attisdropped = payload[pos++] == 1;
		attr->attalign = (char) payload[pos++];
		attr->attstorage = (char) payload[pos++];
		attr->fmt = read_be_u16(payload + pos);
		pos += 2;
		attr->typsend = read_be_u32(payload + pos);
		pos += 4;
		attr->typreceive = read_be_u32(payload + pos);
		pos += 4;
		attr->typioparam = read_be_u32(payload + pos);
		pos += 4;
		attr->attname = read_len_string(payload, payload_len, &pos);
		attr->typename = read_len_string(payload, payload_len, &pos);
		attr->sendname = read_len_string(payload, payload_len, &pos);
		attr->recvname = read_len_string(payload, payload_len, &pos);
	}

	if (pos != payload_len)
		die_parse("schema record has trailing bytes");
}

/*
 * decode_varlena mirrors the varatt.h flag tests for in-memory varlena headers.
 * The normalized dump keeps PostgreSQL's native header representation, so a
 * naive length read would be wrong on little-endian hosts.
 */
static VarlenaView
decode_varlena(const HeaderRecord *header, const uint8_t *blob, size_t blob_len)
{
	VarlenaView view;
	uint8_t		first;

	memset(&view, 0, sizeof(view));

	if (blob_len == 0)
		return view;

	first = blob[0];
	if (header->words_bigendian)
	{
		bool is_4b = (first & 0x80) == 0x00;
		bool is_4b_u = (first & 0xC0) == 0x00;
		bool is_4b_c = (first & 0xC0) == 0x40;
		bool is_1b = (first & 0x80) == 0x80;
		bool is_1b_e = first == 0x80;

		if (is_4b)
		{
			uint32_t raw = read_dump_u32(header, blob);

			view.total_size = raw & 0x3FFFFFFF;
			view.header_size = 4;
			view.valid = is_4b_u && !is_4b_c && view.total_size == blob_len;
		}
		else if (is_1b && !is_1b_e)
		{
			view.total_size = first & 0x7F;
			view.header_size = 1;
			view.valid = view.total_size == blob_len;
		}
	}
	else
	{
		bool is_4b = (first & 0x01) == 0x00;
		bool is_4b_u = (first & 0x03) == 0x00;
		bool is_4b_c = (first & 0x03) == 0x02;
		bool is_1b = (first & 0x01) == 0x01;
		bool is_1b_e = first == 0x01;

		if (is_4b)
		{
			uint32_t raw = read_dump_u32(header, blob);

			view.total_size = (raw >> 2) & 0x3FFFFFFF;
			view.header_size = 4;
			view.valid = is_4b_u && !is_4b_c && view.total_size == blob_len;
		}
		else if (is_1b && !is_1b_e)
		{
			view.total_size = (first >> 1) & 0x7F;
			view.header_size = 1;
			view.valid = view.total_size == blob_len;
		}
	}

	if (view.valid)
	{
		view.payload = blob + view.header_size;
		view.payload_len = view.total_size - view.header_size;
	}

	return view;
}

/*
 * The helpers below intentionally follow the same binary layout decisions as
 * PostgreSQL's int4send/date_send/textsend/numeric_send code paths.
 */
static bool
reserialize_int4send(const VerifierState *state, const uint8_t *blob, size_t blob_len,
					 ByteBuf *out)
{
	int32_t value;

	if (blob_len != 4)
		return false;

	value = read_dump_i32(&state->header, blob);
	bytebuf_append_u32be(out, (uint32_t) value);
	return true;
}

static bool
reserialize_date_send(const VerifierState *state, const uint8_t *blob, size_t blob_len,
					  ByteBuf *out)
{
	int32_t value;

	if (blob_len != 4)
		return false;

	value = read_dump_i32(&state->header, blob);
	bytebuf_append_u32be(out, (uint32_t) value);
	return true;
}

static bool
reserialize_textsend(const VerifierState *state, const uint8_t *blob, size_t blob_len,
					 ByteBuf *out)
{
	VarlenaView view = decode_varlena(&state->header, blob, blob_len);

	if (!view.valid)
		return false;

	bytebuf_append(out, view.payload, view.payload_len);
	return true;
}

static bool
reserialize_numeric_send(const VerifierState *state, const uint8_t *blob, size_t blob_len,
						 ByteBuf *out)
{
	VarlenaView view = decode_varlena(&state->header, blob, blob_len);
	uint16_t	header_word;
	uint16_t	flagbits;
	uint16_t	sign;
	uint16_t	dscale;
	int16_t		weight;
	size_t		numeric_header_size;
	size_t		digits_off;
	size_t		ndigits;

	if (!view.valid || blob_len < 6)
		return false;

	header_word = read_dump_u16(&state->header, blob + 4);
	flagbits = header_word & NUMERIC_SIGN_MASK;

	if (flagbits == NUMERIC_SHORT)
	{
		uint16_t short_weight;

		sign = (header_word & NUMERIC_SHORT_SIGN_MASK) ? NUMERIC_NEG : NUMERIC_POS;
		dscale = (header_word & NUMERIC_SHORT_DSCALE_MASK) >> NUMERIC_SHORT_DSCALE_SHIFT;
		short_weight = header_word & (NUMERIC_SHORT_WEIGHT_SIGN_MASK | NUMERIC_SHORT_WEIGHT_MASK);
		weight = (short_weight & NUMERIC_SHORT_WEIGHT_SIGN_MASK) ?
			(int16_t) (short_weight - 0x80) :
			(int16_t) short_weight;
		numeric_header_size = 6;
		digits_off = 6;
	}
	else
	{
		if (blob_len < 8)
			return false;

		sign = (flagbits == NUMERIC_SPECIAL) ? (header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
		dscale = header_word & NUMERIC_DSCALE_MASK;
		weight = read_dump_i16(&state->header, blob + 6);
		numeric_header_size = 8;
		digits_off = 8;
	}

	if (view.total_size < numeric_header_size ||
		((view.total_size - numeric_header_size) % 2) != 0)
		return false;

	ndigits = (view.total_size - numeric_header_size) / 2;
	if (digits_off + ndigits * 2 != blob_len)
		return false;

	bytebuf_append_u16be(out, (uint16_t) ndigits);
	bytebuf_append_u16be(out, (uint16_t) weight);
	bytebuf_append_u16be(out, sign);
	bytebuf_append_u16be(out, dscale);

	for (size_t i = 0; i < ndigits; i++)
	{
		uint16_t digit = read_dump_u16(&state->header, blob + digits_off + i * 2);

		bytebuf_append_u16be(out, digit);
	}

	return true;
}

static bool
reserialize_field(const VerifierState *state, const AttrMeta *attr,
				  const FieldView *field, ByteBuf *out)
{
	if (field->is_null)
		return true;

	if (attr->sendname == NULL)
		return false;

	if (strcmp(attr->sendname, "pg_catalog.int4send(integer)") == 0)
		return reserialize_int4send(state, field->normalized, field->normalized_len, out);
	if (strcmp(attr->sendname, "pg_catalog.date_send(pg_catalog.date)") == 0)
		return reserialize_date_send(state, field->normalized, field->normalized_len, out);
	if (strcmp(attr->sendname, "pg_catalog.numeric_send(numeric)") == 0)
		return reserialize_numeric_send(state, field->normalized, field->normalized_len, out);
	if (strcmp(attr->sendname, "pg_catalog.bpcharsend(character)") == 0 ||
		strcmp(attr->sendname, "pg_catalog.varcharsend(character varying)") == 0 ||
		strcmp(attr->sendname, "pg_catalog.textsend(text)") == 0)
		return reserialize_textsend(state, field->normalized, field->normalized_len, out);

	return false;
}

static void
report_field_mismatch(const AttrMeta *attr, uint64_t row_id,
					  const FieldView *field, const ByteBuf *replay)
{
	fprintf(stderr,
			"field mismatch row_id=%" PRIu64 " attnum=%u name=%s send=%s\n",
			row_id,
			attr->attnum,
			attr->attname ? attr->attname : "(null)",
			attr->sendname ? attr->sendname : "(null)");
	fprintf(stderr, "  normalized=");
	if (field->is_null)
		fprintf(stderr, "null");
	else
		print_hex_snippet(field->normalized, field->normalized_len);
	fprintf(stderr, "\n  expected=");
	if (field->is_null)
		fprintf(stderr, "null");
	else
		print_hex_snippet(field->serialized, field->serialized_len);
	fprintf(stderr, "\n  replayed=");
	print_hex_snippet(replay->data, replay->len);
	fprintf(stderr, "\n");
}

static void
report_row_mismatch(uint64_t row_id, uint64_t schema_id,
					const uint8_t *expected, size_t expected_len,
					const ByteBuf *replay)
{
	fprintf(stderr,
			"row payload mismatch row_id=%" PRIu64 " schema_id=%" PRIu64 "\n",
			row_id, schema_id);
	fprintf(stderr, "  expected_row=");
	print_hex_snippet(expected, expected_len);
	fprintf(stderr, "\n  replayed_row=");
	print_hex_snippet(replay->data, replay->len);
	fprintf(stderr, "\n");
}

/*
 * verify_row_record reconstructs both the individual field payloads and the
 * DataRow payload layout from the dump, matching the printtup() path.
 */
static void
verify_row_record(VerifierState *state, const uint8_t *payload, size_t payload_len)
{
	size_t		pos = 0;
	uint64_t	schema_id;
	uint64_t	row_id;
	uint32_t	natts;
	uint32_t	row_payload_len;
	const uint8_t *row_payload;
	SchemaRecord *schema;
	FieldView   *fields;
	ByteBuf		rowbuf;
	uint64_t	row_proxy_start_ns;
	uint64_t	row_proxy_end_ns;

	if (payload_len < 24)
		die_parse("row record is truncated");

	schema_id = read_be_u64(payload + pos);
	pos += 8;
	row_id = read_be_u64(payload + pos);
	pos += 8;
	natts = read_be_u32(payload + pos);
	pos += 4;
	row_payload_len = read_be_u32(payload + pos);
	pos += 4;

	if (pos + row_payload_len > payload_len)
		die_parse("row payload exceeds ROWD record size");

	row_payload = payload + pos;
	pos += row_payload_len;

	schema = find_schema(state, schema_id);
	if (schema == NULL)
		die_parse("row references unknown schema id");
	if (schema->natts != natts)
		die_parse("row natts does not match schema natts");

	fields = xmalloc((size_t) natts * sizeof(FieldView));
	memset(fields, 0, (size_t) natts * sizeof(FieldView));

	for (uint32_t i = 0; i < natts; i++)
	{
		int32_t norm_len;
		int32_t ser_len;

		if (pos + 4 > payload_len)
			die_parse("row normalized length is truncated");
		norm_len = read_be_i32(payload + pos);
		pos += 4;
		fields[i].is_null = norm_len < 0;
		if (norm_len >= 0)
		{
			if (pos + (size_t) norm_len > payload_len)
				die_parse("row normalized payload is truncated");
			fields[i].normalized = payload + pos;
			fields[i].normalized_len = (size_t) norm_len;
			pos += (size_t) norm_len;
		}

		if (pos + 4 > payload_len)
			die_parse("row serialized length is truncated");
		ser_len = read_be_i32(payload + pos);
		pos += 4;
		if (ser_len >= 0)
		{
			if (pos + (size_t) ser_len > payload_len)
				die_parse("row serialized payload is truncated");
			fields[i].serialized = payload + pos;
			fields[i].serialized_len = (size_t) ser_len;
			pos += (size_t) ser_len;
		}

		if ((norm_len < 0) != (ser_len < 0))
			die_parse("row null markers disagree between normalized and serialized fields");
	}

	if (pos != payload_len)
		die_parse("row record has trailing bytes");

	state->rows_checked++;
	state->serialize_rows++;
	bytebuf_init(&rowbuf);
	row_proxy_start_ns = monotonic_now_ns();
	bytebuf_append_u16be(&rowbuf, (uint16_t) natts);

	for (uint32_t i = 0; i < natts; i++)
	{
		AttrMeta   *attr = &schema->attrs[i];
		ByteBuf		replay;
		uint64_t	type_start_ns = 0;
		uint64_t	type_end_ns = 0;

		state->fields_checked++;
		state->serialize_fields++;
		bytebuf_init(&replay);

		if (!fields[i].is_null)
		{
			type_start_ns = monotonic_now_ns();
			if (!reserialize_field(state, attr, &fields[i], &replay))
				die_parse("failed to replay field serializer");
			type_end_ns = monotonic_now_ns();
			state->type_serialize_ns += (type_end_ns - type_start_ns);
			state->type_serialize_bytes += replay.len;
		}

		if (fields[i].is_null)
		{
			bytebuf_append_u32be(&rowbuf, 0xFFFFFFFFU);
		}
		else
		{
			bytebuf_append_u32be(&rowbuf, (uint32_t) replay.len);
			bytebuf_append(&rowbuf, replay.data, replay.len);
		}

		if ((fields[i].is_null && replay.len != 0) ||
			(!fields[i].is_null &&
			 (replay.len != fields[i].serialized_len ||
			  memcmp(replay.data, fields[i].serialized, replay.len) != 0)))
		{
			state->mismatches++;
			report_field_mismatch(attr, row_id, &fields[i], &replay);
			bytebuf_free(&replay);
			free(fields);
			bytebuf_free(&rowbuf);
			if (state->mismatches >= state->max_mismatches)
				return;
			continue;
		}

		bytebuf_free(&replay);
	}
	row_proxy_end_ns = monotonic_now_ns();
	state->serialize_proxy_ns += (row_proxy_end_ns - row_proxy_start_ns);
	state->serialize_proxy_bytes += rowbuf.len;

	if (rowbuf.len != row_payload_len ||
		memcmp(rowbuf.data, row_payload, rowbuf.len) != 0)
	{
		state->mismatches++;
		report_row_mismatch(row_id, schema_id, row_payload, row_payload_len, &rowbuf);
	}

	free(fields);
	bytebuf_free(&rowbuf);
}

static uint8_t *
read_file(const char *path, size_t *len_out)
{
	FILE	   *file;
	long		file_len;
	uint8_t    *data;

	file = fopen(path, "rb");
	if (file == NULL)
		die_errno("failed to open dump file");

	if (fseek(file, 0L, SEEK_END) != 0)
		die_errno("failed to seek dump file");

	file_len = ftell(file);
	if (file_len < 0)
		die_errno("failed to stat dump file");

	if (fseek(file, 0L, SEEK_SET) != 0)
		die_errno("failed to rewind dump file");

	data = xmalloc((size_t) file_len);
	if (fread(data, 1, (size_t) file_len, file) != (size_t) file_len)
		die_errno("failed to read dump file");

	fclose(file);
	*len_out = (size_t) file_len;
	return data;
}

static void
parse_head_record(VerifierState *state, const uint8_t *payload, size_t payload_len)
{
	size_t		pos = 0;
	char	   *magic;
	uint32_t	version;

	magic = read_len_string(payload, payload_len, &pos);
	if (magic == NULL)
		die_parse("HEAD record has no magic");
	if (pos + 7 > payload_len)
		die_parse("HEAD record is truncated");

	version = read_be_u32(payload + pos);
	pos += 4;

	state->header.valid = true;
	state->header.words_bigendian = payload[pos++] == 1;
	state->header.datum_size = payload[pos++];
	state->header.pointer_size = payload[pos++];
	state->header.pg_version_num = read_be_u32(payload + pos);
	pos += 4;

	if (strcmp(magic, "PTBDMP1") != 0)
		die_parse("unexpected dump magic");
	if (version != 1)
		die_parse("unexpected dump version");
	if (pos != payload_len)
		die_parse("HEAD record has trailing bytes");

	free(magic);
}

static void
parse_and_verify_dump(VerifierState *state, const uint8_t *data, size_t data_len)
{
	size_t		off = 0;

	while (off + 8 <= data_len)
	{
		const uint8_t *record = data + off;
		uint32_t	payload_len = read_be_u32(record + 4);
		const uint8_t *payload = record + 8;

		if (off + 8 + payload_len > data_len)
			die_parse("record length exceeds dump size");

		if (memcmp(record, "HEAD", 4) == 0)
		{
			state->head_count++;
			parse_head_record(state, payload, payload_len);
		}
		else if (memcmp(record, "SCHM", 4) == 0)
		{
			state->schema_record_count++;
			parse_schema_record(state, payload, payload_len);
		}
		else if (memcmp(record, "ROWD", 4) == 0)
		{
			if (!state->header.valid)
				die_parse("ROWD record appeared before HEAD");
			state->row_record_count++;
			verify_row_record(state, payload, payload_len);
			if (state->mismatches >= state->max_mismatches)
				break;
		}
		else
			die_parse("unknown record tag in dump");

		off += 8 + payload_len;
	}

	if (off != data_len && state->mismatches < state->max_mismatches)
		die_parse("trailing bytes detected in dump");
}

static void
free_state(VerifierState *state)
{
	for (size_t i = 0; i < state->schema_count; i++)
	{
		SchemaRecord *schema = &state->schemas[i];

		for (uint32_t j = 0; j < schema->natts; j++)
		{
			AttrMeta *attr = &schema->attrs[j];

			free(attr->attname);
			free(attr->typename);
			free(attr->sendname);
			free(attr->recvname);
		}
		free(schema->attrs);
	}
	free(state->schemas);
}

int
main(int argc, char **argv)
{
	const char *path;
	uint8_t    *data;
	size_t		data_len = 0;
	VerifierState state;

	if (argc < 2 || argc > 3)
	{
		fprintf(stderr, "usage: %s <dump_path> [max_mismatches]\n", argv[0]);
		return 1;
	}

	memset(&state, 0, sizeof(state));
	state.max_mismatches = (argc == 3) ? strtoull(argv[2], NULL, 10) : 5;
	if (state.max_mismatches == 0)
		state.max_mismatches = 1;

	path = argv[1];
	data = read_file(path, &data_len);
	parse_and_verify_dump(&state, data, data_len);

	printf("head_records=%u schema_records=%u row_records=%u\n",
		   state.head_count, state.schema_record_count, state.row_record_count);
	printf("rows_checked=%" PRIu64 " fields_checked=%" PRIu64 " mismatches=%" PRIu64 "\n",
		   state.rows_checked, state.fields_checked, state.mismatches);
	printf("serialize_proxy_ns=%" PRIu64 " type_serialize_ns=%" PRIu64 "\n",
		   state.serialize_proxy_ns, state.type_serialize_ns);
	printf("serialize_proxy_bytes=%" PRIu64 " type_serialize_bytes=%" PRIu64 "\n",
		   state.serialize_proxy_bytes, state.type_serialize_bytes);
	if (state.serialize_rows > 0)
	{
		printf("serialize_proxy_ns_per_row=%.3f type_serialize_ns_per_row=%.3f\n",
			   (double) state.serialize_proxy_ns / (double) state.serialize_rows,
			   (double) state.type_serialize_ns / (double) state.serialize_rows);
	}
	if (state.serialize_fields > 0)
	{
		printf("serialize_proxy_ns_per_field=%.3f type_serialize_ns_per_field=%.3f\n",
			   (double) state.serialize_proxy_ns / (double) state.serialize_fields,
			   (double) state.type_serialize_ns / (double) state.serialize_fields);
	}
	if (state.serialize_proxy_ns > 0)
	{
		double proxy_mb_per_s =
			((double) state.serialize_proxy_bytes / 1000000.0) /
			((double) state.serialize_proxy_ns / 1000000000.0);

		printf("serialize_proxy_throughput_mb_s=%.3f\n", proxy_mb_per_s);
	}
	if (state.type_serialize_ns > 0)
	{
		double type_mb_per_s =
			((double) state.type_serialize_bytes / 1000000.0) /
			((double) state.type_serialize_ns / 1000000000.0);

		printf("type_serialize_throughput_mb_s=%.3f\n", type_mb_per_s);
	}

	free_state(&state);
	free(data);
	return state.mismatches == 0 ? 0 : 1;
}
