#pragma once

#include <stdint.h>

/*
 * Shared ABI between the Arm-side validation harness and the DPA executable.
 * Keep this header C-only and fixed-width: dpacc compiles the device side with
 * a freestanding RISC-V toolchain, while the host harness is C++.
 */

#define PRINTTUP_DPA_MAX_ATTRS 64
#define PRINTTUP_DPA_SLOT_MAGIC 0x5054445041534c54ULL /* "PTDPASLT" */
#define PRINTTUP_DPA_OUTPUT_SLOT 0
#define PRINTTUP_DPA_OUTPUT_RING 1
#define PRINTTUP_DPA_RING_HOST_WINDOW 0
#define PRINTTUP_DPA_RING_DPA_HEAP 1
#define PRINTTUP_DPA_INPUT_DPA_HEAP 0
#define PRINTTUP_DPA_INPUT_HOST_WINDOW 1
#define PRINTTUP_DPA_PLAN_GENERIC 0
#define PRINTTUP_DPA_PLAN_TPCH_LINEITEM 1
#define PRINTTUP_DPA_SCHEDULE_CONTIGUOUS 0
#define PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED 1
#define PRINTTUP_DPA_RING_PARTITION_SHARED 0
#define PRINTTUP_DPA_RING_PARTITION_PER_WORKER 1
#define PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO 0
#define PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET 1
#define PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD 2

enum PrinttupDpaSerializer
{
	PRINTTUP_DPA_SERIALIZER_UNSUPPORTED = 0,
	PRINTTUP_DPA_SERIALIZER_INT4 = 1,
	PRINTTUP_DPA_SERIALIZER_DATE = 2,
	PRINTTUP_DPA_SERIALIZER_TEXTLIKE = 3,
	PRINTTUP_DPA_SERIALIZER_NUMERIC = 4
};

enum PrinttupDpaStatus
{
	PRINTTUP_DPA_STATUS_EMPTY = 0,
	PRINTTUP_DPA_STATUS_OK = 1,
	PRINTTUP_DPA_STATUS_ERROR = 2
};

enum PrinttupDpaError
{
	PRINTTUP_DPA_ERR_NONE = 0,
	PRINTTUP_DPA_ERR_TOO_MANY_ATTRS = 1,
	PRINTTUP_DPA_ERR_OUTPUT_OVERFLOW = 2,
	PRINTTUP_DPA_ERR_BAD_INPUT = 3,
	PRINTTUP_DPA_ERR_UNSUPPORTED_SERIALIZER = 4,
	PRINTTUP_DPA_ERR_WINDOW = 5
};

typedef struct PrinttupDpaField
{
	uint32_t	serializer;
	uint32_t	is_null;
	uint64_t	normalized_daddr;
	uint32_t	normalized_len;
	/*
	 * Expected wire-format payload length for this field, excluding the DataRow
	 * per-field length word. Normal correctness builds do not need this, but the
	 * DPA-side benchmark toggles use it to advance writer length while skipping
	 * selected serializer work at compile time.
	 */
	uint32_t	serialized_len;
} PrinttupDpaField;

typedef struct PrinttupDpaTask
{
	uint64_t	row_id;
	uint32_t	row_index;
	uint32_t	natts;
	uint32_t	words_bigendian;
	uint32_t	output_capacity;
	uint32_t	iterations;
	/*
	 * Optional schema-specialized serializer plan. Generic remains the fallback;
	 * specialized plans are selected by the ARM harness only when the captured
	 * schema layout exactly matches the expected serializer sequence.
	 */
	uint32_t	plan_id;
	/*
	 * Complete DataRow body length for this prepared row, including natts,
	 * per-field length words, and field payload bytes. Normal correctness builds
	 * recompute this as they serialize; benchmark-only DPA builds use it to skip
	 * the whole field loop while preserving byte-count accounting.
	 */
	uint32_t	row_payload_len;
	/*
	 * Benchmark-only compact lineitem split-output metadata. This is deliberately
	 * row-local, not schema-local: the tuple shape stays fixed by plan_id, while
	 * numeric/text payload lengths can still vary per row. The DPA split-output
	 * fast path can write its per-row metadata from these fields instead of
	 * reloading selected PrinttupDpaField descriptors after serializing payloads.
	 */
	uint32_t	split_payload_len;
	uint16_t	split_extendedprice_len;
	uint16_t	split_discount_len;
	uint16_t	split_tax_len;
	uint16_t	split_comment_len;
	uint16_t	split_reserved;
	uint32_t	window_id;
	uint32_t	mkey;
	uint64_t	output_haddr;
	uint64_t	fields_daddr;
} PrinttupDpaTask;

typedef struct PrinttupDpaBatchConfig
{
	uint64_t	row_task_array_daddr;
	uint64_t	output_base_haddr;
	uint64_t	output_slot_size;
	uint64_t	ring_base_offset;
	uint64_t	ring_base_daddr;
	uint64_t	ring_entry_size;
	uint64_t	ring_slots;
	uint64_t	input_base_offset;
	uint32_t	window_id;
	uint32_t	mkey;
	uint32_t	prepared_rows;
	uint32_t	window_writeback;
	uint32_t	output_mode;
	uint32_t	ring_location;
	uint32_t	output_slot_rows;
	uint32_t	input_location;
	uint32_t	ring_partition_mode;
	/*
	 * Benchmark-only prepared-row mapping. LOGICAL_MODULO preserves the original
	 * logical_index % prepared_rows behavior. WORKER_OFFSET is static-scheduler
	 * only: each worker walks prepared rows by local row ordinal plus a hashed
	 * worker offset, reducing simultaneous convergence on the same tiny input set.
	 * WORKER_SHARD is also static-scheduler only: each worker owns a contiguous
	 * prepared-row shard and cycles inside that shard, avoiding cross-worker input
	 * descriptor/payload sharing while keeping total prepared rows unchanged.
	 */
	uint32_t	input_map_mode;
	/*
	 * Static chunked scheduling is benchmark-only control metadata. The host
	 * enqueues one DPA RPC per logical worker; each RPC walks chunks of this
	 * many logical rows, then skips ahead by worker_count * static_chunk_rows.
	 * This gives us a no-atomic approximation of per-worker queues while still
	 * using the existing FlexIO CmdQ launch path.
	 */
	uint32_t	static_chunk_rows;
	uint32_t	static_logical_rows;
} PrinttupDpaBatchConfig;

typedef struct PrinttupDpaResultHeader
{
	uint64_t	magic;
	uint64_t	row_id;
	uint32_t	row_index;
	uint32_t	status;
	uint32_t	error;
	uint32_t	output_len;
	uint64_t	cycles;
	uint64_t	bytes_written;
	uint64_t	reserved0;
	uint64_t	reserved1;
} PrinttupDpaResultHeader;
