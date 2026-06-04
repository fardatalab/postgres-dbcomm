#include "printtup_dpa_common.h"

#include <libflexio-dev/flexio_dev_ver.h>
#ifndef FLEXIO_DEV_VER_USED
#define FLEXIO_DEV_VER_USED FLEXIO_DEV_VER(25, 10, 0)
#endif
#include <libflexio-dev/flexio_dev.h>
#include <dpaintrin.h>

#define NUMERIC_SIGN_MASK 0xC000U
#define NUMERIC_POS 0x0000U
#define NUMERIC_NEG 0x4000U
#define NUMERIC_SHORT 0x8000U
#define NUMERIC_SPECIAL 0xC000U
#define NUMERIC_EXT_SIGN_MASK 0xF000U
#define NUMERIC_SHORT_SIGN_MASK 0x2000U
#define NUMERIC_SHORT_DSCALE_MASK 0x1F80U
#define NUMERIC_SHORT_DSCALE_SHIFT 7
#define NUMERIC_SHORT_WEIGHT_SIGN_MASK 0x0040U
#define NUMERIC_SHORT_WEIGHT_MASK 0x003FU
#define NUMERIC_DSCALE_MASK 0x3FFFU
#define PRINTTUP_DPA_WINDOW_CACHE_SLOTS 1024U

/*
 * Compile-time benchmark switch: when enabled, integer emit helpers write DPA
 * native-order bytes instead of PostgreSQL/libpq network byte order. This makes
 * the produced body intentionally non-wire-compatible, but it isolates the cost
 * of byte-order shuffling without adding a runtime branch to the hot path.
 */
#ifndef PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER
#define PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER 0
#endif
#ifndef PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
#define PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY 0
#endif
#ifndef PRINTTUP_DPA_BENCH_FAST_NUMERIC
#define PRINTTUP_DPA_BENCH_FAST_NUMERIC 0
#endif
#ifndef PRINTTUP_DPA_BENCH_FAST_FIELDS
#define PRINTTUP_DPA_BENCH_FAST_FIELDS 0
#endif
#ifndef PRINTTUP_DPA_BENCH_FAST_ROW
#define PRINTTUP_DPA_BENCH_FAST_ROW 0
#endif
#ifndef PRINTTUP_DPA_BENCH_FAST_LINEITEM_FIELDS
#define PRINTTUP_DPA_BENCH_FAST_LINEITEM_FIELDS 0
#endif
#ifndef PRINTTUP_DPA_LINEITEM_SCRATCH_OUTPUT
#define PRINTTUP_DPA_LINEITEM_SCRATCH_OUTPUT 0
#endif
#ifndef PRINTTUP_DPA_BENCH_PAYLOAD_ONLY_OUTPUT
#define PRINTTUP_DPA_BENCH_PAYLOAD_ONLY_OUTPUT 0
#endif
#ifndef PRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT
#define PRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT 0
#endif
#ifndef PRINTTUP_DPA_BENCH_NO_ROW_HEADERS
#define PRINTTUP_DPA_BENCH_NO_ROW_HEADERS 0
#endif
#ifndef PRINTTUP_DPA_BENCH_CONTIG_LINEITEM_INPUT
#define PRINTTUP_DPA_BENCH_CONTIG_LINEITEM_INPUT 0
#endif
#ifndef PRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT
#define PRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT 0
#endif
#ifndef PRINTTUP_DPA_BENCH_TASK_SPLIT_META
#define PRINTTUP_DPA_BENCH_TASK_SPLIT_META 1
#endif
#ifndef PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN
#define PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN 0
#endif
#ifndef PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA
#define PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN
#endif
#ifndef PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR
#define PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR 0
#endif
#ifndef PRINTTUP_DPA_BENCH_SPLIT_META_SIZE
#define PRINTTUP_DPA_BENCH_SPLIT_META_SIZE 32U
#endif
#define PRINTTUP_DPA_LINEITEM_SCRATCH_CAPACITY 512U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE PRINTTUP_DPA_BENCH_SPLIT_META_SIZE
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_PAYLOAD_LEN 0U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_EXTENDEDPRICE_LEN 4U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_DISCOUNT_LEN 6U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_TAX_LEN 8U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_COMMENT_LEN 10U
#define PRINTTUP_DPA_LINEITEM_SPLIT_META_FLAGS 12U

typedef struct DpaByteWriter
{
	uint8_t	   *data;
	uint32_t	len;
	uint32_t	capacity;
	uint32_t	error;
	uint32_t	trust_capacity;
} DpaByteWriter;

typedef struct DpaVarlenaView
{
	uint32_t	valid;
	uint32_t	total_size;
	uint32_t	header_size;
	const uint8_t *payload;
	uint32_t	payload_len;
} DpaVarlenaView;

typedef struct DpaOutputWindowCache
{
	uint64_t	output_base_haddr;
	flexio_uintptr_t output_base_daddr;
	uint32_t	window_id;
	uint32_t	mkey;
	uint32_t	valid;
} DpaOutputWindowCache;

static PrinttupDpaBatchConfig *g_batch_config = 0;
static DpaOutputWindowCache g_output_window_cache[PRINTTUP_DPA_WINDOW_CACHE_SLOTS];

/*
 * Window configuration and host-pointer acquisition are control-path operations.
 * Cache the registered output-base mapping per DPA thread so the row hot loop
 * only sees plain pointer arithmetic into the fixed output-slot array. If the
 * FlexIO thread id is outside the local cache, fall back to a correct uncached
 * acquire path rather than risking an out-of-bounds cache access.
 */
static uint32_t
dpa_get_output_base(PrinttupDpaBatchConfig *config, flexio_uintptr_t *output_base_daddr)
{
	uint32_t thread_id = flexio_dev_get_thread_id((void *) 0);
	DpaOutputWindowCache *cache = 0;

	if (thread_id < PRINTTUP_DPA_WINDOW_CACHE_SLOTS)
	{
		cache = &g_output_window_cache[thread_id];
		if (cache->valid &&
			cache->output_base_haddr == config->output_base_haddr &&
			cache->window_id == config->window_id &&
			cache->mkey == config->mkey)
		{
			*output_base_daddr = cache->output_base_daddr;
			return PRINTTUP_DPA_ERR_NONE;
		}
	}

	if (flexio_dev_window_config(FLEXIO_DEV_WINDOW_ENTITY_0,
								 (uint16_t) config->window_id,
								 config->mkey) != FLEXIO_DEV_STATUS_SUCCESS)
		return PRINTTUP_DPA_ERR_WINDOW;
	if (flexio_dev_window_ptr_acquire(FLEXIO_DEV_WINDOW_ENTITY_0,
									  config->output_base_haddr,
									  output_base_daddr) != FLEXIO_DEV_STATUS_SUCCESS ||
		*output_base_daddr == 0)
		return PRINTTUP_DPA_ERR_WINDOW;

	if (cache != 0)
	{
		cache->output_base_haddr = config->output_base_haddr;
		cache->output_base_daddr = *output_base_daddr;
		cache->window_id = config->window_id;
		cache->mkey = config->mkey;
		cache->valid = 1;
	}
	return PRINTTUP_DPA_ERR_NONE;
}

static uint16_t
dpa_read_dump_u16(uint32_t words_bigendian, const uint8_t *data)
{
	if (words_bigendian)
		return ((uint16_t) data[0] << 8) | (uint16_t) data[1];
	return ((uint16_t) data[1] << 8) | (uint16_t) data[0];
}

static int16_t
dpa_read_dump_i16(uint32_t words_bigendian, const uint8_t *data)
{
	return (int16_t) dpa_read_dump_u16(words_bigendian, data);
}

static uint32_t
dpa_read_dump_u32(uint32_t words_bigendian, const uint8_t *data)
{
	if (words_bigendian)
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
dpa_read_dump_i32(uint32_t words_bigendian, const uint8_t *data)
{
	return (int32_t) dpa_read_dump_u32(words_bigendian, data);
}

/*
 * Reserve output bytes in the current DataRow body writer. Keeping capacity
 * handling in one helper lets the fixed-width appenders write directly to the
 * destination instead of building tiny stack buffers and then re-entering the
 * generic copy loop.
 */
static uint8_t *
dpa_writer_reserve(DpaByteWriter *writer, uint32_t len)
{
	uint8_t *dst;

	if (writer->error != PRINTTUP_DPA_ERR_NONE)
		return 0;
	if (!writer->trust_capacity &&
		(writer->len > writer->capacity || len > writer->capacity - writer->len))
	{
		writer->error = PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW;
		return 0;
	}

	dst = writer->data + writer->len;
	writer->len += len;
	return dst;
}

/*
 * Benchmark-only writer advance. This preserves DataRow length accounting while
 * deliberately leaving the corresponding body bytes unwritten. Use only in
 * compile-time isolation builds where ARM-side byte mismatches are expected.
 */
static void __attribute__((unused))
dpa_writer_skip_bytes(DpaByteWriter *writer, uint32_t len)
{
	(void) dpa_writer_reserve(writer, len);
}

/*
 * Append raw payload bytes with a chunked, source-level unaligned-safe copy.
 * Text-like fields commonly start after a 1-byte short-varlena header, and
 * DataRow bodies contain mixed 2-byte and 4-byte prefixes. Avoid raw typed
 * pointer casts here; fixed-size __builtin_memcpy keeps the C expression valid
 * for unaligned byte addresses while still giving the DPA compiler a chance to
 * lower the copy into wider moves when the target supports them.
 */
static void
dpa_writer_append(DpaByteWriter *writer, const void *src, uint32_t len)
{
	const uint8_t *bytes = (const uint8_t *) src;
	uint8_t	   *dst = dpa_writer_reserve(writer, len);
	uint32_t	off = 0;

	if (dst == 0 || len == 0)
		return;

	for (; off + 8U <= len; off += 8U)
	{
		uint64_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
	}
	if (off + 4U <= len)
	{
		uint32_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
		off += 4U;
	}
	if (off + 2U <= len)
	{
		uint16_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
		off += 2U;
	}
	if (off < len)
		dst[off] = bytes[off];
}

/*
 * Append fixed-width PostgreSQL send-protocol integers directly in network byte
 * order. These helpers are hit for DataRow attribute counts, per-field lengths,
 * int4/date values, and numeric metadata/digits, so avoiding the generic copy
 * loop removes a small but very frequent hot-path cost.
 */
static void
dpa_writer_append_u16be(DpaByteWriter *writer, uint16_t value)
{
	uint8_t *dst = dpa_writer_reserve(writer, 2);

	if (dst == 0)
		return;
#if PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER
	__builtin_memcpy(dst, &value, sizeof(value));
#else
	dst[0] = (uint8_t) ((value >> 8) & 0xFFU);
	dst[1] = (uint8_t) (value & 0xFFU);
#endif
}

static void
dpa_writer_append_u32be(DpaByteWriter *writer, uint32_t value)
{
	uint8_t *dst = dpa_writer_reserve(writer, 4);

	if (dst == 0)
		return;
#if PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER
	__builtin_memcpy(dst, &value, sizeof(value));
#else
	dst[0] = (uint8_t) ((value >> 24) & 0xFFU);
	dst[1] = (uint8_t) ((value >> 16) & 0xFFU);
	dst[2] = (uint8_t) ((value >> 8) & 0xFFU);
	dst[3] = (uint8_t) (value & 0xFFU);
#endif
}

/*
 * Mirror PostgreSQL varlena header decoding for the already-normalized dump
 * input. The dump still carries native PostgreSQL in-memory varlena headers,
 * so the byte-order flag from HEAD remains part of the DPA task ABI.
 */
static DpaVarlenaView
dpa_decode_varlena(uint32_t words_bigendian, const uint8_t *blob, uint32_t blob_len)
{
	DpaVarlenaView view;
	uint8_t first;

	view.valid = 0;
	view.total_size = 0;
	view.header_size = 0;
	view.payload = 0;
	view.payload_len = 0;

	if (blob_len == 0)
		return view;

	first = blob[0];
	if (words_bigendian)
	{
		uint32_t is_4b = (first & 0x80U) == 0x00U;
		uint32_t is_4b_u = (first & 0xC0U) == 0x00U;
		uint32_t is_4b_c = (first & 0xC0U) == 0x40U;
		uint32_t is_1b = (first & 0x80U) == 0x80U;
		uint32_t is_1b_e = first == 0x80U;

		if (is_4b)
		{
			uint32_t raw = dpa_read_dump_u32(words_bigendian, blob);

			view.total_size = raw & 0x3FFFFFFFU;
			view.header_size = 4;
			view.valid = is_4b_u && !is_4b_c && view.total_size == blob_len;
		}
		else if (is_1b && !is_1b_e)
		{
			view.total_size = first & 0x7FU;
			view.header_size = 1;
			view.valid = view.total_size == blob_len;
		}
	}
	else
	{
		uint32_t is_4b = (first & 0x01U) == 0x00U;
		uint32_t is_4b_u = (first & 0x03U) == 0x00U;
		uint32_t is_4b_c = (first & 0x03U) == 0x02U;
		uint32_t is_1b = (first & 0x01U) == 0x01U;
		uint32_t is_1b_e = first == 0x01U;

		if (is_4b)
		{
			uint32_t raw = dpa_read_dump_u32(words_bigendian, blob);

			view.total_size = (raw >> 2) & 0x3FFFFFFFU;
			view.header_size = 4;
			view.valid = is_4b_u && !is_4b_c && view.total_size == blob_len;
		}
		else if (is_1b && !is_1b_e)
		{
			view.total_size = (first >> 1) & 0x7FU;
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

static DpaVarlenaView
dpa_decode_varlena_header_only(uint32_t words_bigendian, const uint8_t *blob)
{
	DpaVarlenaView view;
	uint8_t first;

	view.valid = 0;
	view.total_size = 0;
	view.header_size = 0;
	view.payload = 0;
	view.payload_len = 0;

	first = blob[0];
	if (words_bigendian)
	{
		if ((first & 0x80U) == 0x00U)
		{
			uint32_t raw = dpa_read_dump_u32(words_bigendian, blob);

			if ((first & 0xC0U) == 0x00U)
			{
				view.total_size = raw & 0x3FFFFFFFU;
				view.header_size = 4;
				view.valid = view.total_size >= view.header_size;
			}
		}
		else if (first != 0x80U)
		{
			view.total_size = first & 0x7FU;
			view.header_size = 1;
			view.valid = view.total_size >= view.header_size;
		}
	}
	else
	{
		if ((first & 0x01U) == 0x00U)
		{
			uint32_t raw = dpa_read_dump_u32(words_bigendian, blob);

			if ((first & 0x03U) == 0x00U)
			{
				view.total_size = (raw >> 2) & 0x3FFFFFFFU;
				view.header_size = 4;
				view.valid = view.total_size >= view.header_size;
			}
		}
		else if (first != 0x01U)
		{
			view.total_size = (first >> 1) & 0x7FU;
			view.header_size = 1;
			view.valid = view.total_size >= view.header_size;
		}
	}

	if (view.valid)
	{
		view.payload = blob + view.header_size;
		view.payload_len = view.total_size - view.header_size;
	}
	return view;
}

static uint32_t
dpa_reserialize_scalar32(const PrinttupDpaTask *task, const PrinttupDpaField *field,
						 const uint8_t *normalized, DpaByteWriter *writer)
{
	int32_t value;

	if (field->normalized_len != 4)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	value = dpa_read_dump_i32(task->words_bigendian, normalized);
	dpa_writer_append_u32be(writer, (uint32_t) value);
	return writer->error;
}

static uint32_t
dpa_reserialize_textlike(const PrinttupDpaTask *task, const PrinttupDpaField *field,
						 const uint8_t *blob, DpaByteWriter *writer)
{
	DpaVarlenaView view = dpa_decode_varlena(task->words_bigendian, blob,
											 field->normalized_len);

	if (!view.valid)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

#if PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
	/*
	 * Isolation build: keep varlena-header decode/checking, but remove the raw
	 * text payload load/store work. The resulting body intentionally mismatches.
	 */
	dpa_writer_skip_bytes(writer, view.payload_len);
#else
	dpa_writer_append(writer, view.payload, view.payload_len);
#endif
	return writer->error;
}

static uint32_t
dpa_reserialize_numeric(const PrinttupDpaTask *task, const PrinttupDpaField *field,
						const uint8_t *blob, DpaByteWriter *writer)
{
#if PRINTTUP_DPA_BENCH_FAST_NUMERIC
	/*
	 * Isolation build: skip numeric varlena/header/digit parsing and skip the
	 * corresponding output bytes using the captured serialized length.
	 */
	(void) task;
	(void) blob;
	dpa_writer_skip_bytes(writer, field->serialized_len);
	return writer->error;
#else
	DpaVarlenaView view = dpa_decode_varlena(task->words_bigendian, blob,
											 field->normalized_len);
	uint16_t header_word;
	uint16_t flagbits;
	uint16_t sign;
	uint16_t dscale;
	int16_t weight;
	uint32_t numeric_header_size;
	uint32_t digits_off;
	uint32_t ndigits;

	if (!view.valid || field->normalized_len < 6)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	header_word = dpa_read_dump_u16(task->words_bigendian, blob + 4);
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
		if (field->normalized_len < 8)
			return PRINTTUP_DPA_ERR_BAD_INPUT;

		sign = (flagbits == NUMERIC_SPECIAL) ? (header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
		dscale = header_word & NUMERIC_DSCALE_MASK;
		weight = dpa_read_dump_i16(task->words_bigendian, blob + 6);
		numeric_header_size = 8;
		digits_off = 8;
	}

	if (view.total_size < numeric_header_size ||
		((view.total_size - numeric_header_size) % 2U) != 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	ndigits = (view.total_size - numeric_header_size) / 2U;
	if (digits_off + ndigits * 2U != field->normalized_len)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	dpa_writer_append_u16be(writer, (uint16_t) ndigits);
	dpa_writer_append_u16be(writer, (uint16_t) weight);
	dpa_writer_append_u16be(writer, sign);
	dpa_writer_append_u16be(writer, dscale);

	for (uint32_t i = 0; i < ndigits; i++)
	{
		uint16_t digit = dpa_read_dump_u16(task->words_bigendian,
										   blob + digits_off + i * 2U);

		dpa_writer_append_u16be(writer, digit);
	}

	return writer->error;
#endif
}

static uint32_t
dpa_reserialize_field(const PrinttupDpaTask *task, const PrinttupDpaField *field,
					  uint8_t *input_base, DpaByteWriter *writer)
{
	const uint8_t *normalized;

	if (input_base != 0)
		normalized = input_base + field->normalized_daddr;
	else
		normalized = (const uint8_t *) field->normalized_daddr;

#if PRINTTUP_DPA_BENCH_FAST_FIELDS
	/*
	 * Isolation build: keep the row/field loop and DataRow length patching, but
	 * skip all per-type input parsing and payload writes. This bounds the cost of
	 * CmdQ scheduling, descriptor walking, and per-field framing.
	 */
	dpa_writer_skip_bytes(writer, field->serialized_len);
	return writer->error;
#endif

	if (field->serializer == PRINTTUP_DPA_SERIALIZER_INT4 ||
		field->serializer == PRINTTUP_DPA_SERIALIZER_DATE)
		return dpa_reserialize_scalar32(task, field, normalized, writer);
	if (field->serializer == PRINTTUP_DPA_SERIALIZER_TEXTLIKE)
		return dpa_reserialize_textlike(task, field, normalized, writer);
	if (field->serializer == PRINTTUP_DPA_SERIALIZER_NUMERIC)
		return dpa_reserialize_numeric(task, field, normalized, writer);
	return PRINTTUP_DPA_ERR_UNSUPPORTED_SERIALIZER;
}

/*
 * Direct append helpers for schema-specialized plans. They intentionally bypass
 * DpaByteWriter's per-append reserve/error plumbing after the row-level capacity
 * precheck has proved that the complete row body fits in the destination slot.
 * This isolates whether the remaining "generic" cost is writer abstraction and
 * branch bookkeeping rather than PostgreSQL send-format work.
 */
static void
dpa_direct_append_u16be(uint8_t *out, uint32_t *pos, uint16_t value)
{
#if PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER
	__builtin_memcpy(out + *pos, &value, sizeof(value));
#else
	out[*pos + 0] = (uint8_t) ((value >> 8) & 0xFFU);
	out[*pos + 1] = (uint8_t) (value & 0xFFU);
#endif
	*pos += 2U;
}

static void
dpa_direct_append_u32be(uint8_t *out, uint32_t *pos, uint32_t value)
{
#if PRINTTUP_DPA_SKIP_NETWORK_BYTE_ORDER
	__builtin_memcpy(out + *pos, &value, sizeof(value));
#else
	out[*pos + 0] = (uint8_t) ((value >> 24) & 0xFFU);
	out[*pos + 1] = (uint8_t) ((value >> 16) & 0xFFU);
	out[*pos + 2] = (uint8_t) ((value >> 8) & 0xFFU);
	out[*pos + 3] = (uint8_t) (value & 0xFFU);
#endif
	*pos += 4U;
}

/*
 * Native-endian aligned metadata stores for benchmark-only non-DataRow output
 * shapes. This metadata is consumed by our DPA/RDMA prototype, not libpq, so it
 * does not pay network-byte-order conversion or PostgreSQL DataRow framing.
 */
static void
dpa_direct_store_u16(uint8_t *out, uint32_t off, uint16_t value)
{
	__builtin_memcpy(out + off, &value, sizeof(value));
}

static void
dpa_direct_store_u32(uint8_t *out, uint32_t off, uint32_t value)
{
	__builtin_memcpy(out + off, &value, sizeof(value));
}

static void
dpa_direct_append_bytes(uint8_t *out, uint32_t *pos, const void *src, uint32_t len)
{
	const uint8_t *bytes = (const uint8_t *) src;
	uint32_t off = 0;
	uint8_t *dst = out + *pos;

	for (; off + 8U <= len; off += 8U)
	{
		uint64_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
	}
	if (off + 4U <= len)
	{
		uint32_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
		off += 4U;
	}
	if (off + 2U <= len)
	{
		uint16_t word;

		__builtin_memcpy(&word, bytes + off, sizeof(word));
		__builtin_memcpy(dst + off, &word, sizeof(word));
		off += 2U;
	}
	if (off < len)
		dst[off] = bytes[off];
	*pos += len;
}

static uint32_t
dpa_direct_scalar32(const PrinttupDpaTask *task, const PrinttupDpaField *field,
					const uint8_t *normalized, uint8_t *out, uint32_t *pos)
{
	if (field->normalized_len != 4)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	dpa_direct_append_u32be(out, pos,
							(uint32_t) dpa_read_dump_i32(task->words_bigendian,
														 normalized));
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t
dpa_direct_textlike(const PrinttupDpaTask *task, const PrinttupDpaField *field,
					const uint8_t *blob, uint8_t *out, uint32_t *pos)
{
	if (field->serialized_len <= field->normalized_len)
	{
		/*
		 * In the dump workload, textsend/bpcharsend/varcharsend output is exactly
		 * the varlena payload. Use the captured send length to infer the payload
		 * offset without re-decoding the varlena header on DPA.
		 */
		uint32_t payload_offset = field->normalized_len - field->serialized_len;

#if PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
		*pos += field->serialized_len;
#else
		dpa_direct_append_bytes(out, pos, blob + payload_offset,
								field->serialized_len);
#endif
		return PRINTTUP_DPA_ERR_NONE;
	}

	DpaVarlenaView view = dpa_decode_varlena(task->words_bigendian, blob,
											 field->normalized_len);

	if (!view.valid)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

#if PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
	*pos += view.payload_len;
#else
	dpa_direct_append_bytes(out, pos, view.payload, view.payload_len);
#endif
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t
dpa_direct_numeric(const PrinttupDpaTask *task, const PrinttupDpaField *field,
				   const uint8_t *blob, uint8_t *out, uint32_t *pos)
{
#if PRINTTUP_DPA_BENCH_FAST_NUMERIC
	(void) task;
	(void) blob;
	(void) out;
	*pos += field->serialized_len;
	return PRINTTUP_DPA_ERR_NONE;
#else
	uint32_t inferred_ndigits;
	uint32_t inferred_digit_bytes;
	uint32_t inferred_header_size;

	if (field->serialized_len >= 8U &&
		((field->serialized_len - 8U) % 2U) == 0)
	{
		/*
		 * numeric_send output is ndigits/weight/sign/dscale plus the base-10000
		 * digit array. The captured send length gives ndigits, and the normalized
		 * datum tail is the digit array; parse only the small Numeric header needed
		 * for weight/sign/dscale instead of running full varlena validation.
		 */
		inferred_ndigits = (field->serialized_len - 8U) / 2U;
		inferred_digit_bytes = inferred_ndigits * 2U;
		if (inferred_digit_bytes <= field->normalized_len)
		{
			uint16_t header_word;
			uint16_t flagbits;
			uint16_t sign;
			uint16_t dscale;
			int16_t weight;
			uint32_t metadata_ok = 0;

			inferred_header_size = field->normalized_len - inferred_digit_bytes;
			if (inferred_header_size == 6U || inferred_header_size == 8U)
			{
				header_word = dpa_read_dump_u16(task->words_bigendian, blob + 4);
				flagbits = header_word & NUMERIC_SIGN_MASK;

				if (flagbits == NUMERIC_SHORT && inferred_header_size == 6U)
				{
					uint16_t short_weight;

					sign = (header_word & NUMERIC_SHORT_SIGN_MASK) ? NUMERIC_NEG : NUMERIC_POS;
					dscale = (header_word & NUMERIC_SHORT_DSCALE_MASK) >> NUMERIC_SHORT_DSCALE_SHIFT;
					short_weight = header_word & (NUMERIC_SHORT_WEIGHT_SIGN_MASK |
												  NUMERIC_SHORT_WEIGHT_MASK);
					weight = (short_weight & NUMERIC_SHORT_WEIGHT_SIGN_MASK) ?
						(int16_t) (short_weight - 0x80) :
						(int16_t) short_weight;
					metadata_ok = 1;
				}
				else if (inferred_header_size == 8U)
				{
					sign = (flagbits == NUMERIC_SPECIAL) ?
						(header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
					dscale = header_word & NUMERIC_DSCALE_MASK;
					weight = dpa_read_dump_i16(task->words_bigendian, blob + 6);
					metadata_ok = 1;
				}
			}

			if (metadata_ok)
			{
				dpa_direct_append_u16be(out, pos, (uint16_t) inferred_ndigits);
				dpa_direct_append_u16be(out, pos, (uint16_t) weight);
				dpa_direct_append_u16be(out, pos, sign);
				dpa_direct_append_u16be(out, pos, dscale);

				for (uint32_t i = 0; i < inferred_ndigits; i++)
				{
					uint16_t digit = dpa_read_dump_u16(task->words_bigendian,
													   blob + inferred_header_size + i * 2U);

					dpa_direct_append_u16be(out, pos, digit);
				}
				return PRINTTUP_DPA_ERR_NONE;
			}
		}
	}

	DpaVarlenaView view = dpa_decode_varlena(task->words_bigendian, blob,
											 field->normalized_len);
	uint16_t header_word;
	uint16_t flagbits;
	uint16_t sign;
	uint16_t dscale;
	int16_t weight;
	uint32_t numeric_header_size;
	uint32_t digits_off;
	uint32_t ndigits;

	if (!view.valid || field->normalized_len < 6)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	header_word = dpa_read_dump_u16(task->words_bigendian, blob + 4);
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
		if (field->normalized_len < 8)
			return PRINTTUP_DPA_ERR_BAD_INPUT;

		sign = (flagbits == NUMERIC_SPECIAL) ? (header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
		dscale = header_word & NUMERIC_DSCALE_MASK;
		weight = dpa_read_dump_i16(task->words_bigendian, blob + 6);
		numeric_header_size = 8;
		digits_off = 8;
	}

	if (view.total_size < numeric_header_size ||
		((view.total_size - numeric_header_size) % 2U) != 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	ndigits = (view.total_size - numeric_header_size) / 2U;
	if (digits_off + ndigits * 2U != field->normalized_len)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	dpa_direct_append_u16be(out, pos, (uint16_t) ndigits);
	dpa_direct_append_u16be(out, pos, (uint16_t) weight);
	dpa_direct_append_u16be(out, pos, sign);
	dpa_direct_append_u16be(out, pos, dscale);

	for (uint32_t i = 0; i < ndigits; i++)
	{
		uint16_t digit = dpa_read_dump_u16(task->words_bigendian,
										   blob + digits_off + i * 2U);

		dpa_direct_append_u16be(out, pos, digit);
	}

	return PRINTTUP_DPA_ERR_NONE;
#endif
}

/*
 * Specialized TPC-H lineitem row body serializer. The ARM harness assigns this
 * plan only when the schema's send-function sequence exactly matches lineitem:
 * int4 x4, numeric x4, text-like x2, date x3, text-like x3. This removes the
 * generic per-field serializer dispatch and per-field patch/check path while
 * preserving correct DataRow bytes for the standalone dump. The final row-length
 * check catches unexpected nulls, malformed descriptors, or a stale plan.
 */
static uint32_t __attribute__((unused))
dpa_reserialize_lineitem_row(const PrinttupDpaTask *task, PrinttupDpaField *fields,
							 uint8_t *input_base, DpaByteWriter *writer)
{
#define DPA_LINEITEM_FIELD(index, serializer_fn) \
	do { \
		PrinttupDpaField *field = &fields[(index)]; \
		if (!PRINTTUP_DPA_BENCH_PAYLOAD_ONLY_OUTPUT) \
			dpa_direct_append_u32be(out, &pos, field->serialized_len); \
		if (PRINTTUP_DPA_BENCH_FAST_LINEITEM_FIELDS) { \
			pos += field->serialized_len; \
		} else { \
			const uint8_t *normalized = input_is_window ? \
				input_base + field->normalized_daddr : \
				(const uint8_t *) field->normalized_daddr; \
			error = serializer_fn(task, field, normalized, out, &pos); \
			if (error != PRINTTUP_DPA_ERR_NONE) \
				return error; \
		} \
	} while (0)

	uint32_t input_is_window = input_base != 0;
	uint8_t scratch[PRINTTUP_DPA_LINEITEM_SCRATCH_CAPACITY];
	uint8_t *dst = writer->data;
	uint8_t *out = dst;
	uint32_t pos = 0;
	uint32_t error;
	uint32_t use_scratch = 0;

	if (!PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA && task->natts != 16)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	if (task->row_payload_len > writer->capacity)
		return PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW;
#if PRINTTUP_DPA_LINEITEM_SCRATCH_OUTPUT && \
	!PRINTTUP_DPA_BENCH_FAST_LINEITEM_FIELDS && \
	!PRINTTUP_DPA_BENCH_FAST_NUMERIC && \
	!PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
	if (task->row_payload_len <= PRINTTUP_DPA_LINEITEM_SCRATCH_CAPACITY)
	{
		use_scratch = 1;
		out = scratch;
	}
#endif

	if (!PRINTTUP_DPA_BENCH_PAYLOAD_ONLY_OUTPUT)
		dpa_direct_append_u16be(out, &pos, 16);
	DPA_LINEITEM_FIELD(0, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(1, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(2, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(3, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(4, dpa_direct_numeric);
	DPA_LINEITEM_FIELD(5, dpa_direct_numeric);
	DPA_LINEITEM_FIELD(6, dpa_direct_numeric);
	DPA_LINEITEM_FIELD(7, dpa_direct_numeric);
	DPA_LINEITEM_FIELD(8, dpa_direct_textlike);
	DPA_LINEITEM_FIELD(9, dpa_direct_textlike);
	DPA_LINEITEM_FIELD(10, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(11, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(12, dpa_direct_scalar32);
	DPA_LINEITEM_FIELD(13, dpa_direct_textlike);
	DPA_LINEITEM_FIELD(14, dpa_direct_textlike);
	DPA_LINEITEM_FIELD(15, dpa_direct_textlike);

	writer->len = pos;
	if (!PRINTTUP_DPA_BENCH_PAYLOAD_ONLY_OUTPUT && pos != task->row_payload_len)
	{
		writer->error = PRINTTUP_DPA_ERR_BAD_INPUT;
		return writer->error;
	}
	if (use_scratch)
	{
		uint32_t copy_pos = 0;

		dpa_direct_append_bytes(dst, &copy_pos, scratch, pos);
	}
	return writer->error;

#undef DPA_LINEITEM_FIELD
}

/*
 * Benchmark-only fixed-schema lineitem output for the RDMA-oriented direction.
 * The output is intentionally not a PostgreSQL DataRow body. It writes a compact
 * 32-byte aligned metadata prefix followed by a contiguous stream of field
 * payloads in schema order. Fixed-width/fixed-length lineitem attributes are
 * implied by the schema; only varying numeric/text lengths are stored per row.
 *
 * Layout:
 *   u32 payload_len
 *   u16 l_extendedprice_len
 *   u16 l_discount_len
 *   u16 l_tax_len
 *   u16 l_comment_len
 *   u32 flags/reserved
 *   padding to 32 bytes
 *   payload bytes for all 16 fields, without DataRow natts/field length words
 */
static uint32_t __attribute__((unused))
dpa_split_lineitem_append_scalar32_contig(const PrinttupDpaTask *task,
										  const uint8_t **cursor,
										  uint8_t *out, uint32_t *payload_pos)
{
	dpa_direct_append_u32be(out, payload_pos,
							(uint32_t) dpa_read_dump_i32(task->words_bigendian,
														 *cursor));
	*cursor += 4;
	return 4;
}

static uint32_t __attribute__((unused))
dpa_split_lineitem_append_textlike_contig(const PrinttupDpaTask *task,
										  const uint8_t **cursor,
										  uint8_t *out, uint32_t *payload_pos,
										  uint32_t *serialized_len)
{
	DpaVarlenaView view =
		dpa_decode_varlena_header_only(task->words_bigendian, *cursor);

	if (!view.valid)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
#if PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
	*payload_pos += view.payload_len;
#else
	dpa_direct_append_bytes(out, payload_pos, view.payload, view.payload_len);
#endif
	*cursor += view.total_size;
	*serialized_len = view.payload_len;
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_split_lineitem_append_numeric_contig(const PrinttupDpaTask *task,
										 const uint8_t **cursor,
										 uint8_t *out, uint32_t *payload_pos,
										 uint32_t *serialized_len)
{
	DpaVarlenaView view =
		dpa_decode_varlena_header_only(task->words_bigendian, *cursor);
	uint16_t header_word;
	uint16_t flagbits;
	uint16_t sign;
	uint16_t dscale;
	int16_t weight;
	uint32_t numeric_header_size;
	uint32_t ndigits;

	if (!view.valid || view.header_size != 4U || view.total_size < 6U)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	header_word = dpa_read_dump_u16(task->words_bigendian, *cursor + 4);
	flagbits = header_word & NUMERIC_SIGN_MASK;
	if (flagbits == NUMERIC_SHORT)
	{
		uint16_t short_weight;

		numeric_header_size = 6U;
		sign = (header_word & NUMERIC_SHORT_SIGN_MASK) ? NUMERIC_NEG : NUMERIC_POS;
		dscale = (header_word & NUMERIC_SHORT_DSCALE_MASK) >> NUMERIC_SHORT_DSCALE_SHIFT;
		short_weight = header_word & (NUMERIC_SHORT_WEIGHT_SIGN_MASK |
									  NUMERIC_SHORT_WEIGHT_MASK);
		weight = (short_weight & NUMERIC_SHORT_WEIGHT_SIGN_MASK) ?
			(int16_t) (short_weight - 0x80) :
			(int16_t) short_weight;
	}
	else
	{
		if (view.total_size < 8U)
			return PRINTTUP_DPA_ERR_BAD_INPUT;
		numeric_header_size = 8U;
		sign = (flagbits == NUMERIC_SPECIAL) ?
			(header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
		dscale = header_word & NUMERIC_DSCALE_MASK;
		weight = dpa_read_dump_i16(task->words_bigendian, *cursor + 6);
	}
	if (view.total_size < numeric_header_size ||
		((view.total_size - numeric_header_size) % 2U) != 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	ndigits = (view.total_size - numeric_header_size) / 2U;
	dpa_direct_append_u16be(out, payload_pos, (uint16_t) ndigits);
	dpa_direct_append_u16be(out, payload_pos, (uint16_t) weight);
	dpa_direct_append_u16be(out, payload_pos, sign);
	dpa_direct_append_u16be(out, payload_pos, dscale);
	for (uint32_t i = 0; i < ndigits; i++)
	{
		uint16_t digit = dpa_read_dump_u16(task->words_bigendian,
										   *cursor + numeric_header_size + i * 2U);

		dpa_direct_append_u16be(out, payload_pos, digit);
	}
	*cursor += view.total_size;
	*serialized_len = 8U + ndigits * 2U;
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_reserialize_lineitem_split_row_contig_input(const PrinttupDpaTask *task,
												PrinttupDpaField *fields,
												uint8_t *input_base,
												DpaByteWriter *writer)
{
	uint32_t input_is_window = input_base != 0;
	const uint8_t *cursor = input_is_window ?
		input_base + fields[0].normalized_daddr :
		(const uint8_t *) fields[0].normalized_daddr;
	uint8_t *out = writer->data;
	uint32_t payload_pos = PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE;
	uint32_t extendedprice_len = 0;
	uint32_t discount_len = 0;
	uint32_t tax_len = 0;
	uint32_t comment_len = 0;
	uint32_t scratch_len = 0;
	uint32_t error;

	if (!PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA && task->natts != 16)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	if (PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE <
		PRINTTUP_DPA_LINEITEM_SPLIT_META_FLAGS + 4U)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	if (PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE + task->row_payload_len > writer->capacity)
		return PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW;

	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	error = dpa_split_lineitem_append_numeric_contig(task, &cursor, out,
													&payload_pos, &scratch_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_numeric_contig(task, &cursor, out,
													&payload_pos, &extendedprice_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_numeric_contig(task, &cursor, out,
													&payload_pos, &discount_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_numeric_contig(task, &cursor, out,
													&payload_pos, &tax_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_textlike_contig(task, &cursor, out,
													 &payload_pos, &scratch_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_textlike_contig(task, &cursor, out,
													 &payload_pos, &scratch_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	dpa_split_lineitem_append_scalar32_contig(task, &cursor, out, &payload_pos);
	error = dpa_split_lineitem_append_textlike_contig(task, &cursor, out,
													 &payload_pos, &scratch_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_textlike_contig(task, &cursor, out,
													 &payload_pos, &scratch_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;
	error = dpa_split_lineitem_append_textlike_contig(task, &cursor, out,
													 &payload_pos, &comment_len);
	if (error != PRINTTUP_DPA_ERR_NONE)
		return error;

	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_PAYLOAD_LEN,
						 payload_pos - PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_EXTENDEDPRICE_LEN,
						 (uint16_t) extendedprice_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_DISCOUNT_LEN,
						 (uint16_t) discount_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_TAX_LEN,
						 (uint16_t) tax_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_COMMENT_LEN,
						 (uint16_t) comment_len);
	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_FLAGS, 0);

	writer->len = payload_pos;
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_split_lineitem_append_scalar32_precomputed(const PrinttupDpaTask *task,
											   const PrinttupDpaField *field,
											   uint8_t *input_base,
											   uint8_t *out, uint32_t *payload_pos)
{
	const uint8_t *normalized = input_base != 0 ?
		input_base + field->normalized_daddr :
		(const uint8_t *) field->normalized_daddr;

	dpa_direct_append_u32be(out, payload_pos,
							(uint32_t) dpa_read_dump_i32(task->words_bigendian,
														 normalized));
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_split_lineitem_append_textlike_precomputed(const PrinttupDpaField *field,
											   uint8_t *input_base,
											   uint8_t *out, uint32_t *payload_pos)
{
	const uint8_t *normalized = input_base != 0 ?
		input_base + field->normalized_daddr :
		(const uint8_t *) field->normalized_daddr;
	uint32_t payload_offset;

	if (field->serialized_len > field->normalized_len)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	payload_offset = field->normalized_len - field->serialized_len;
#if PRINTTUP_DPA_BENCH_SKIP_TEXT_PAYLOAD_COPY
	*payload_pos += field->serialized_len;
#else
	dpa_direct_append_bytes(out, payload_pos, normalized + payload_offset,
							field->serialized_len);
#endif
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_split_lineitem_append_numeric_precomputed(const PrinttupDpaTask *task,
											  const PrinttupDpaField *field,
											  uint8_t *input_base,
											  uint8_t *out, uint32_t *payload_pos)
{
	const uint8_t *normalized = input_base != 0 ?
		input_base + field->normalized_daddr :
		(const uint8_t *) field->normalized_daddr;
	uint32_t ndigits;
	uint32_t digit_bytes;
	uint32_t header_size;
	uint16_t header_word;
	uint16_t flagbits;
	uint16_t sign;
	uint16_t dscale;
	int16_t weight;

	if (field->serialized_len < 8U || ((field->serialized_len - 8U) % 2U) != 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	ndigits = (field->serialized_len - 8U) / 2U;
	digit_bytes = ndigits * 2U;
	if (digit_bytes > field->normalized_len)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	header_size = field->normalized_len - digit_bytes;
	if (header_size != 6U && header_size != 8U)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	header_word = dpa_read_dump_u16(task->words_bigendian, normalized + 4);
	flagbits = header_word & NUMERIC_SIGN_MASK;
	if (flagbits == NUMERIC_SHORT && header_size == 6U)
	{
		uint16_t short_weight;

		sign = (header_word & NUMERIC_SHORT_SIGN_MASK) ? NUMERIC_NEG : NUMERIC_POS;
		dscale = (header_word & NUMERIC_SHORT_DSCALE_MASK) >> NUMERIC_SHORT_DSCALE_SHIFT;
		short_weight = header_word & (NUMERIC_SHORT_WEIGHT_SIGN_MASK |
									  NUMERIC_SHORT_WEIGHT_MASK);
		weight = (short_weight & NUMERIC_SHORT_WEIGHT_SIGN_MASK) ?
			(int16_t) (short_weight - 0x80) :
			(int16_t) short_weight;
	}
	else if (header_size == 8U)
	{
		sign = (flagbits == NUMERIC_SPECIAL) ?
			(header_word & NUMERIC_EXT_SIGN_MASK) : flagbits;
		dscale = header_word & NUMERIC_DSCALE_MASK;
		weight = dpa_read_dump_i16(task->words_bigendian, normalized + 6);
	}
	else
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	dpa_direct_append_u16be(out, payload_pos, (uint16_t) ndigits);
	dpa_direct_append_u16be(out, payload_pos, (uint16_t) weight);
	dpa_direct_append_u16be(out, payload_pos, sign);
	dpa_direct_append_u16be(out, payload_pos, dscale);
	for (uint32_t i = 0; i < ndigits; i++)
	{
		uint16_t digit = dpa_read_dump_u16(task->words_bigendian,
										   normalized + header_size + i * 2U);

		dpa_direct_append_u16be(out, payload_pos, digit);
	}
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t __attribute__((unused))
dpa_reserialize_lineitem_split_row_precomputed_input(const PrinttupDpaTask *task,
													 PrinttupDpaField *fields,
													 uint8_t *input_base,
													 DpaByteWriter *writer)
{
#define DPA_PRECOMP_FIELD(index, append_fn) \
	do { \
		error = append_fn(task, &fields[(index)], input_base, out, &payload_pos); \
		if (error != PRINTTUP_DPA_ERR_NONE) \
			return error; \
	} while (0)
#define DPA_PRECOMP_TEXT_FIELD(index) \
	do { \
		error = dpa_split_lineitem_append_textlike_precomputed(&fields[(index)], \
															  input_base, out, &payload_pos); \
		if (error != PRINTTUP_DPA_ERR_NONE) \
			return error; \
	} while (0)

	uint8_t *out = writer->data;
	uint32_t payload_pos = PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE;
	uint32_t use_task_split_meta =
		PRINTTUP_DPA_BENCH_TASK_SPLIT_META && task->split_payload_len != 0;
	uint32_t error;

	if (!PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA && task->natts != 16)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	if (PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE + task->row_payload_len > writer->capacity)
		return PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW;

	DPA_PRECOMP_FIELD(0, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(1, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(2, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(3, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(4, dpa_split_lineitem_append_numeric_precomputed);
	DPA_PRECOMP_FIELD(5, dpa_split_lineitem_append_numeric_precomputed);
	DPA_PRECOMP_FIELD(6, dpa_split_lineitem_append_numeric_precomputed);
	DPA_PRECOMP_FIELD(7, dpa_split_lineitem_append_numeric_precomputed);
	DPA_PRECOMP_TEXT_FIELD(8);
	DPA_PRECOMP_TEXT_FIELD(9);
	DPA_PRECOMP_FIELD(10, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(11, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_FIELD(12, dpa_split_lineitem_append_scalar32_precomputed);
	DPA_PRECOMP_TEXT_FIELD(13);
	DPA_PRECOMP_TEXT_FIELD(14);
	DPA_PRECOMP_TEXT_FIELD(15);

	/*
	 * The ARM harness has already computed the compact split-output row metadata
	 * from the captured send-function payload lengths. Keep a defensive length
	 * check here: if the DPA serializer emits a different payload length, the
	 * descriptor and hot-path work are no longer describing the same row.
	 */
	if (use_task_split_meta &&
		payload_pos != PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE + task->split_payload_len)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_PAYLOAD_LEN,
						 use_task_split_meta ?
						 task->split_payload_len :
						 payload_pos - PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_EXTENDEDPRICE_LEN,
						 use_task_split_meta ?
						 task->split_extendedprice_len :
						 (uint16_t) fields[5].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_DISCOUNT_LEN,
						 use_task_split_meta ?
						 task->split_discount_len :
						 (uint16_t) fields[6].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_TAX_LEN,
						 use_task_split_meta ?
						 task->split_tax_len :
						 (uint16_t) fields[7].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_COMMENT_LEN,
						 use_task_split_meta ?
						 task->split_comment_len :
						 (uint16_t) fields[15].serialized_len);
	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_FLAGS, 0);

	writer->len = payload_pos;
	return PRINTTUP_DPA_ERR_NONE;

#undef DPA_PRECOMP_TEXT_FIELD
#undef DPA_PRECOMP_FIELD
}

static uint32_t __attribute__((unused))
dpa_reserialize_lineitem_split_row(const PrinttupDpaTask *task, PrinttupDpaField *fields,
								   uint8_t *input_base, DpaByteWriter *writer)
{
#if PRINTTUP_DPA_BENCH_PRECOMPUTED_LINEITEM_INPUT
	/*
	 * Benchmark-only generated lineitem path: still uses precomputed field
	 * lengths/addresses, but removes generic serializer dispatch and the fallback
	 * varlena decode branches that are unnecessary for the captured lineitem dump.
	 */
	return dpa_reserialize_lineitem_split_row_precomputed_input(task, fields,
															   input_base, writer);
#elif PRINTTUP_DPA_BENCH_CONTIG_LINEITEM_INPUT
	/*
	 * Benchmark-only fast path for materialized packed input: prepare_rows()
	 * appends a row's normalized field payloads in schema order, so lineitem can
	 * walk one input cursor instead of loading a per-field payload pointer.
	 */
	return dpa_reserialize_lineitem_split_row_contig_input(task, fields,
														   input_base, writer);
#else
#define DPA_SPLIT_LINEITEM_FIELD(index, serializer_fn) \
	do { \
		PrinttupDpaField *field = &fields[(index)]; \
		const uint8_t *normalized = input_is_window ? \
			input_base + field->normalized_daddr : \
			(const uint8_t *) field->normalized_daddr; \
		error = serializer_fn(task, field, normalized, out, &payload_pos); \
		if (error != PRINTTUP_DPA_ERR_NONE) \
			return error; \
	} while (0)

	uint32_t input_is_window = input_base != 0;
	uint8_t *out = writer->data;
	uint32_t payload_pos = PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE;
	uint32_t error;

	if (!PRINTTUP_DPA_BENCH_TRUST_LINEITEM_SCHEMA && task->natts != 16)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	/*
	 * row_payload_len is the larger exact-DataRow body length, so this is a
	 * conservative slot-size check for the compact split format.
	 */
	if (PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE + task->row_payload_len > writer->capacity)
		return PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW;

	DPA_SPLIT_LINEITEM_FIELD(0, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(1, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(2, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(3, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(4, dpa_direct_numeric);
	DPA_SPLIT_LINEITEM_FIELD(5, dpa_direct_numeric);
	DPA_SPLIT_LINEITEM_FIELD(6, dpa_direct_numeric);
	DPA_SPLIT_LINEITEM_FIELD(7, dpa_direct_numeric);
	DPA_SPLIT_LINEITEM_FIELD(8, dpa_direct_textlike);
	DPA_SPLIT_LINEITEM_FIELD(9, dpa_direct_textlike);
	DPA_SPLIT_LINEITEM_FIELD(10, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(11, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(12, dpa_direct_scalar32);
	DPA_SPLIT_LINEITEM_FIELD(13, dpa_direct_textlike);
	DPA_SPLIT_LINEITEM_FIELD(14, dpa_direct_textlike);
	DPA_SPLIT_LINEITEM_FIELD(15, dpa_direct_textlike);

	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_PAYLOAD_LEN,
						 payload_pos - PRINTTUP_DPA_LINEITEM_SPLIT_META_SIZE);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_EXTENDEDPRICE_LEN,
						 (uint16_t) fields[5].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_DISCOUNT_LEN,
						 (uint16_t) fields[6].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_TAX_LEN,
						 (uint16_t) fields[7].serialized_len);
	dpa_direct_store_u16(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_COMMENT_LEN,
						 (uint16_t) fields[15].serialized_len);
	dpa_direct_store_u32(out, PRINTTUP_DPA_LINEITEM_SPLIT_META_FLAGS, 0);

	writer->len = payload_pos;
	return PRINTTUP_DPA_ERR_NONE;

#undef DPA_SPLIT_LINEITEM_FIELD
#endif
}

static uint32_t
dpa_reserialize_one_row_mapped(PrinttupDpaTask *task, PrinttupDpaResultHeader *header,
							   uint32_t logical_index, uint8_t *ring_base,
							   uint64_t ring_entry_size, uint64_t ring_slot_base,
							   uint64_t ring_slots,
							   uint8_t *input_base, uint8_t *explicit_ring_output)
{
	DpaByteWriter writer;
	uint64_t start_cycles;
	uint64_t end_cycles;
	uint64_t bytes_written = 0;
	uint32_t error = PRINTTUP_DPA_ERR_NONE;
	uint32_t iterations = task->iterations == 0 ? 1U : task->iterations;
	uint32_t use_ring = ring_base != 0 && ring_slots != 0;
	uint32_t use_explicit_ring_output =
		explicit_ring_output != 0 && use_ring && iterations == 1U;
	PrinttupDpaField *fields = (PrinttupDpaField *) task->fields_daddr;

	if (!PRINTTUP_DPA_BENCH_NO_ROW_HEADERS &&
		header->magic != PRINTTUP_DPA_SLOT_MAGIC)
	{
		header->magic = PRINTTUP_DPA_SLOT_MAGIC;
		header->status = PRINTTUP_DPA_STATUS_ERROR;
		header->error = PRINTTUP_DPA_ERR_NONE;
		header->output_len = 0;
		header->cycles = 0;
		header->bytes_written = 0;
	}
	/*
	 * Output slots may now be reused for different prepared input rows when the
	 * output/status working set is intentionally decoupled from the input working
	 * set. Preserve cumulative cycle/byte counters across that reuse, but keep the
	 * latest row metadata and output body for ARM-side correctness checks.
	 */
	if (!PRINTTUP_DPA_BENCH_NO_ROW_HEADERS)
	{
		header->row_id = task->row_id;
		header->row_index = task->row_index;
	}

	if (task->natts > PRINTTUP_DPA_MAX_ATTRS)
		error = PRINTTUP_DPA_ERR_TOO_MANY_ATTRS;

	writer.len = 0;
	writer.capacity = task->output_capacity;
	writer.error = error;
	/*
	 * Standalone dump tasks carry the complete expected DataRow body length.
	 * When it fits in the output slot, skip repeated per-append capacity checks
	 * and rely on the final row-length assertion below. This is a hot-path
	 * optimization for trusted benchmark input; malformed descriptors could write
	 * past the slot before the final assertion catches the inconsistency.
	 */
	writer.trust_capacity =
		(task->row_payload_len <= task->output_capacity) ? 1U : 0U;

	start_cycles = __dpa_thread_cycles();
	for (uint32_t iter = 0; iter < iterations && writer.error == PRINTTUP_DPA_ERR_NONE; iter++)
	{
			if (use_explicit_ring_output)
				writer.data = explicit_ring_output;
			else if (use_ring)
			{
				uint64_t ring_slot =
					ring_slot_base +
					((((uint64_t) logical_index * (uint64_t) iterations) +
					  (uint64_t) iter) % ring_slots);
				writer.data = ring_base + ring_slot * ring_entry_size;
			}
		else
			writer.data = (uint8_t *) (header + 1);
		writer.len = 0;
#if PRINTTUP_DPA_BENCH_FAST_ROW
		/*
		 * Isolation build: skip the entire DataRow body construction while keeping
		 * row scheduling, output-slot selection, result accounting, and byte-count
		 * accounting intact. This bounds non-field-loop overhead.
		 */
		dpa_writer_skip_bytes(&writer, task->row_payload_len);
#else
#if !PRINTTUP_DPA_BENCH_FAST_FIELDS
#if PRINTTUP_DPA_BENCH_FORCE_LINEITEM_PLAN
			/*
			 * Benchmark-only compiled-plan path: the host run has already selected
			 * the fixed lineitem schema for every prepared row, so skip the per-row
			 * plan_id load/branch and enter the generated serializer directly.
			 */
#if PRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT
			error = dpa_reserialize_lineitem_split_row(task, fields, input_base, &writer);
#else
			error = dpa_reserialize_lineitem_row(task, fields, input_base, &writer);
#endif
			if (error != PRINTTUP_DPA_ERR_NONE)
			{
				writer.error = error;
				break;
			}
			goto row_done;
#else
			if (task->plan_id == PRINTTUP_DPA_PLAN_TPCH_LINEITEM)
			{
#if PRINTTUP_DPA_BENCH_SPLIT_LINEITEM_OUTPUT
				error = dpa_reserialize_lineitem_split_row(task, fields, input_base, &writer);
#else
				error = dpa_reserialize_lineitem_row(task, fields, input_base, &writer);
#endif
				if (error != PRINTTUP_DPA_ERR_NONE)
				{
					writer.error = error;
					break;
				}
				goto row_done;
			}
#endif
#endif

			dpa_writer_append_u16be(&writer, (uint16_t) task->natts);

		for (uint32_t i = 0; i < task->natts && writer.error == PRINTTUP_DPA_ERR_NONE; i++)
		{
			PrinttupDpaField *field = &fields[i];
			uint32_t before_len = writer.len;

			if (field->is_null)
			{
				dpa_writer_append_u32be(&writer, 0xFFFFFFFFU);
				continue;
			}

			/*
			 * The standalone dump already carries the captured send-function
			 * payload length. Write it up front instead of reserving zero and
			 * patching later, then defensively verify that the DPA serializer
			 * produced exactly that many bytes. A future integrated path can keep
			 * this shape if it precomputes the payload length per attribute; if
			 * not, it will need the older reserve/patch pattern.
			 */
			dpa_writer_append_u32be(&writer, field->serialized_len);
			error = dpa_reserialize_field(task, field, input_base, &writer);
			if (error != PRINTTUP_DPA_ERR_NONE)
			{
				writer.error = error;
				break;
			}
			if (writer.len != before_len + 4U + field->serialized_len)
			{
				writer.error = PRINTTUP_DPA_ERR_BAD_INPUT;
				break;
			}
		}
row_done:
#endif
		if (writer.error == PRINTTUP_DPA_ERR_NONE)
			bytes_written += writer.len;
	}
	end_cycles = __dpa_thread_cycles();

	if (!PRINTTUP_DPA_BENCH_NO_ROW_HEADERS)
	{
		header->output_len = writer.len;
		header->cycles += end_cycles - start_cycles;
		header->bytes_written += bytes_written;
		header->error = writer.error;
		header->status = (writer.error == PRINTTUP_DPA_ERR_NONE) ?
			PRINTTUP_DPA_STATUS_OK : PRINTTUP_DPA_STATUS_ERROR;
	}

	return writer.error;
}

__dpa_rpc__ uint64_t
printtup_dpa_set_batch_config(uint64_t config_daddr)
{
	g_batch_config = (PrinttupDpaBatchConfig *) config_daddr;
	return 0;
}

__dpa_rpc__ uint64_t
printtup_dpa_reserialize_row(uint64_t task_daddr)
{
	PrinttupDpaTask *task = (PrinttupDpaTask *) task_daddr;
	flexio_uintptr_t result_daddr = 0;
	uint32_t error;

	if (flexio_dev_window_config(FLEXIO_DEV_WINDOW_ENTITY_0,
								 (uint16_t) task->window_id,
								 task->mkey) != FLEXIO_DEV_STATUS_SUCCESS)
		return PRINTTUP_DPA_ERR_WINDOW;

	if (flexio_dev_window_ptr_acquire(FLEXIO_DEV_WINDOW_ENTITY_0,
									  task->output_haddr,
									  &result_daddr) != FLEXIO_DEV_STATUS_SUCCESS ||
		result_daddr == 0)
		return PRINTTUP_DPA_ERR_WINDOW;

	error = dpa_reserialize_one_row_mapped(task,
										  (PrinttupDpaResultHeader *) result_daddr,
										  task->row_index, 0, 0, 0, 0, 0, 0);

	/* Publish host/window writes before the Arm-side harness compares bytes. */
	__dpa_thread_window_writeback();
	return error;
}

typedef struct DpaBatchRuntime
{
	PrinttupDpaTask *row_task_array;
	uint8_t    *output_base;
	uint8_t    *ring_base;
	uint8_t    *input_base;
	uint32_t	prepared_rows;
	uint32_t	output_slot_rows;
} DpaBatchRuntime;

/*
 * Resolve the shared batch configuration into DPA-local pointers for a CmdQ RPC.
 * The window acquisition and host/DPA ring/input routing is shared by contiguous
 * row ranges and the static chunked per-worker queue experiment.
 */
static uint32_t
dpa_batch_runtime_init(DpaBatchRuntime *runtime)
{
	flexio_uintptr_t output_base_daddr = 0;
	uint32_t window_error;

	if (runtime == 0 || g_batch_config == 0 || g_batch_config->prepared_rows == 0 ||
		g_batch_config->output_slot_rows == 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	window_error = dpa_get_output_base(g_batch_config, &output_base_daddr);
	if (window_error != PRINTTUP_DPA_ERR_NONE)
		return window_error;

	runtime->output_base = (uint8_t *) output_base_daddr;
	runtime->ring_base = 0;
	runtime->input_base = 0;
	if (g_batch_config->output_mode == PRINTTUP_DPA_OUTPUT_RING)
	{
		if (g_batch_config->ring_location == PRINTTUP_DPA_RING_DPA_HEAP)
			runtime->ring_base = (uint8_t *) g_batch_config->ring_base_daddr;
		else
			runtime->ring_base =
				runtime->output_base + g_batch_config->ring_base_offset;
	}
	if (g_batch_config->input_location == PRINTTUP_DPA_INPUT_HOST_WINDOW)
		runtime->input_base = runtime->output_base + g_batch_config->input_base_offset;
	runtime->row_task_array =
		(PrinttupDpaTask *) g_batch_config->row_task_array_daddr;
	runtime->prepared_rows = g_batch_config->prepared_rows;
	runtime->output_slot_rows = g_batch_config->output_slot_rows;
	return PRINTTUP_DPA_ERR_NONE;
}

static uint32_t
dpa_worker_input_offset(uint32_t worker_id, uint32_t prepared_rows)
{
	if (prepared_rows == 0)
		return 0;
	/*
	 * Match the Arm-side helper. This benchmark-only mapping spreads static
	 * workers over the prepared-row array without needing a DPA-side table.
	 */
	return (uint32_t) (((uint64_t) worker_id * 2654435761ULL) %
					   (uint64_t) prepared_rows);
}

static uint32_t
dpa_worker_shard_bounds(uint32_t worker_id, uint32_t worker_count,
						uint32_t prepared_rows, uint32_t *start,
						uint32_t *count)
{
	uint32_t base;
	uint32_t extra;

	if (worker_count == 0 || worker_id >= worker_count ||
		prepared_rows < worker_count)
		return PRINTTUP_DPA_ERR_BAD_INPUT;
	base = prepared_rows / worker_count;
	extra = prepared_rows % worker_count;
	*start = worker_id * base + (worker_id < extra ? worker_id : extra);
	*count = base + (worker_id < extra ? 1U : 0U);
	return *count == 0 ? PRINTTUP_DPA_ERR_BAD_INPUT : PRINTTUP_DPA_ERR_NONE;
}

/*
 * Serialize one logical row using the current reusable input/output working-set
 * mappings. Keeping this as a helper makes scheduling experiments change only
 * the row-order loop, not the serializer body.
 */
static uint32_t
dpa_reserialize_logical_row(DpaBatchRuntime *runtime, uint32_t logical_index,
							uint32_t prepared_index,
							uint64_t ring_slot_base, uint64_t ring_slot_count,
							uint8_t *explicit_ring_output)
{
	PrinttupDpaTask *task =
		&runtime->row_task_array[prepared_index];
	uint32_t output_slot_index = logical_index % runtime->output_slot_rows;
	PrinttupDpaResultHeader *header =
		(PrinttupDpaResultHeader *)
		(runtime->output_base + ((uint64_t) output_slot_index *
								 g_batch_config->output_slot_size));

	return dpa_reserialize_one_row_mapped(task, header, logical_index,
										  runtime->ring_base,
										  g_batch_config->ring_entry_size,
										  ring_slot_base,
										  ring_slot_count,
										  runtime->input_base,
										  explicit_ring_output);
}

__dpa_rpc__ uint64_t
printtup_dpa_reserialize_batch(uint64_t packed_range)
{
	uint32_t start_index = (uint32_t) (packed_range & 0xFFFFFFFFULL);
	uint32_t row_count = (uint32_t) (packed_range >> 32);
	DpaBatchRuntime runtime;
	uint32_t first_error = PRINTTUP_DPA_ERR_NONE;
	uint32_t init_error;

	init_error = dpa_batch_runtime_init(&runtime);
	if (init_error != PRINTTUP_DPA_ERR_NONE)
		return init_error;

	/*
	 * One CmdQ item now represents a configurable row range. Tasks live in one
	 * contiguous DPA array, so the hot loop avoids the older row_task_table
	 * pointer load and scattered per-task allocations.
	 */
	for (uint32_t row_offset = 0; row_offset < row_count; row_offset++)
	{
		uint32_t logical_index = start_index + row_offset;
		uint32_t prepared_index = logical_index % runtime.prepared_rows;
		uint32_t error = dpa_reserialize_logical_row(&runtime, logical_index,
													 prepared_index,
													 0, g_batch_config->ring_slots,
													 0);

		if (first_error == PRINTTUP_DPA_ERR_NONE &&
			error != PRINTTUP_DPA_ERR_NONE)
			first_error = error;
	}

	/*
	 * Publish rows for ARM-side byte verification. Benchmark-only runs can skip
	 * this to measure serialization plus host submission/drain without forcing
	 * DPA window cache writeback on every CmdQ work item.
	 */
	if (g_batch_config->window_writeback)
		__dpa_thread_window_writeback();
	return first_error;
}

/*
 * Static chunked per-worker queue experiment. The host enqueues exactly one CmdQ
 * task per logical worker. Each task processes chunk_rows consecutive logical
 * rows, then jumps ahead by worker_count * chunk_rows. This is intentionally
 * atomics-free: every worker's row set is predetermined by worker_id.
 */
__dpa_rpc__ uint64_t
printtup_dpa_reserialize_static_worker(uint64_t packed_worker)
{
	uint32_t worker_id = (uint32_t) (packed_worker & 0xFFFFFFFFULL);
	uint32_t worker_count = (uint32_t) (packed_worker >> 32);
	DpaBatchRuntime runtime;
	uint32_t first_error = PRINTTUP_DPA_ERR_NONE;
	uint32_t init_error;
	uint32_t chunk_rows;
	uint32_t logical_rows;
	uint64_t ring_slot_base = 0;
	uint64_t ring_slot_count;
	uint64_t stride_rows;
	uint32_t input_offset = 0;
	uint32_t shard_start = 0;
	uint32_t shard_count = 0;
	uint64_t worker_row_ordinal = 0;
#if PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR
	uint64_t ring_cursor = 0;
	uint64_t ring_cursor_end = 0;
#endif

	init_error = dpa_batch_runtime_init(&runtime);
	if (init_error != PRINTTUP_DPA_ERR_NONE)
		return init_error;
	if (worker_count == 0 || worker_id >= worker_count)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	chunk_rows = g_batch_config->static_chunk_rows;
	logical_rows = g_batch_config->static_logical_rows;
	if (chunk_rows == 0 || logical_rows == 0)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	ring_slot_count = g_batch_config->ring_slots;
	if (runtime.ring_base != 0 &&
		g_batch_config->ring_partition_mode == PRINTTUP_DPA_RING_PARTITION_PER_WORKER)
	{
		uint64_t slots_per_worker = g_batch_config->ring_slots / worker_count;
		uint64_t extra_slots = g_batch_config->ring_slots % worker_count;

		if (slots_per_worker == 0)
			return PRINTTUP_DPA_ERR_BAD_INPUT;
		ring_slot_base = (uint64_t) worker_id * slots_per_worker;
		ring_slot_count = slots_per_worker;
		if (worker_id == worker_count - 1)
			ring_slot_count += extra_slots;
	}
#if PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR
	ring_cursor = ring_slot_base;
	ring_cursor_end = ring_slot_base + ring_slot_count;
#endif
	if (g_batch_config->input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET)
		input_offset = dpa_worker_input_offset(worker_id, runtime.prepared_rows);
	else if (g_batch_config->input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD)
	{
		uint32_t shard_error =
			dpa_worker_shard_bounds(worker_id, worker_count, runtime.prepared_rows,
									&shard_start, &shard_count);

		if (shard_error != PRINTTUP_DPA_ERR_NONE)
			return shard_error;
	}
	else if (g_batch_config->input_map_mode != PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO)
		return PRINTTUP_DPA_ERR_BAD_INPUT;

	stride_rows = (uint64_t) worker_count * (uint64_t) chunk_rows;
	for (uint64_t chunk_start = (uint64_t) worker_id * (uint64_t) chunk_rows;
		 chunk_start < (uint64_t) logical_rows;
		 chunk_start += stride_rows)
	{
		uint32_t rows_this_chunk = chunk_rows;

		if (chunk_start + rows_this_chunk > (uint64_t) logical_rows)
			rows_this_chunk = (uint32_t) ((uint64_t) logical_rows - chunk_start);
		for (uint32_t row_offset = 0; row_offset < rows_this_chunk; row_offset++)
		{
			uint32_t logical_index = (uint32_t) chunk_start + row_offset;
			uint32_t prepared_index;
			uint8_t *explicit_ring_output = 0;

			if (g_batch_config->input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET)
				prepared_index =
					(uint32_t) ((worker_row_ordinal + (uint64_t) input_offset) %
								(uint64_t) runtime.prepared_rows);
			else if (g_batch_config->input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD)
				prepared_index =
					shard_start +
					(uint32_t) (worker_row_ordinal % (uint64_t) shard_count);
			else
				prepared_index = logical_index % runtime.prepared_rows;
#if PRINTTUP_DPA_BENCH_WORKER_RING_CURSOR
			/*
			 * In static-worker mode every worker owns a deterministic row stream.
			 * Keep an output-ring cursor in that worker instead of recomputing
			 * logical_index * iterations modulo ring_slots for every row. This is
			 * only used for the common timing shape where iterations == 1; the
			 * row helper falls back to the older formula for repeated-row runs.
			 */
			if (runtime.ring_base != 0 && ring_slot_count != 0)
			{
				explicit_ring_output =
					runtime.ring_base + ring_cursor * g_batch_config->ring_entry_size;
				ring_cursor++;
				if (ring_cursor >= ring_cursor_end)
					ring_cursor = ring_slot_base;
			}
#endif
			uint32_t error = dpa_reserialize_logical_row(&runtime, logical_index,
														 prepared_index,
														 ring_slot_base,
														 ring_slot_count,
														 explicit_ring_output);

			if (first_error == PRINTTUP_DPA_ERR_NONE &&
				error != PRINTTUP_DPA_ERR_NONE)
				first_error = error;
			worker_row_ordinal++;
		}
	}

	if (g_batch_config->window_writeback)
		__dpa_thread_window_writeback();
	return first_error;
}
