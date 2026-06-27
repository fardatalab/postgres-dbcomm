#ifndef DOCA_HOMER_VALIDATION_COMMON_H
#define DOCA_HOMER_VALIDATION_COMMON_H

#include <stddef.h>
#include <stdint.h>

#define HDV_MAGIC 0x48445631444d4155ULL
#define HDV_VERSION 1U
#define HDV_CACHELINE 64U
#define HDV_POISON_BEFORE 0x1111222233334444ULL
#define HDV_POISON_AFTER 0xaaaabbbbccccddddULL
#define HDV_DEFAULT_SEED 0x9e3779b97f4a7c15ULL

enum HdvMode
{
	HDV_MODE_WRITE_PUBLISH = 1,
	HDV_MODE_EARLY_PUBLISH = 2,
	HDV_MODE_HOST_WRITE_DPU_READ = 3,
	HDV_MODE_SYNC_EVENT_PUBLISH = 4,
	HDV_MODE_SYNC_EVENT_EARLY_PUBLISH = 5,
	HDV_MODE_SYNC_EVENT_ASYNC_PULL = 6,
	HDV_MODE_DPU_SYNC_EVENT_PUBLISH = 7,
	HDV_MODE_DPU_SYNC_EVENT_EARLY_PUBLISH = 8,
	HDV_MODE_WRITE_PUBLISH_ASYNC = 9,
	HDV_MODE_WRITE_PUBLISH_SPLIT_NOWAIT = 10,
	HDV_MODE_WRITE_PUBLISH_SPLIT_COMPLETION = 11,
};

enum HdvPayloadMode
{
	HDV_PAYLOAD_FULL = 1,
	HDV_PAYLOAD_HEADER = 2,
};

enum HdvSizePattern
{
	HDV_SIZE_PATTERN_FIXED = 1,
	HDV_SIZE_PATTERN_MIXED_64_4K = 2,
	HDV_SIZE_PATTERN_MIXED_64_1K = 3,
};

enum
{
	HDV_MIXED_SMALL_BYTES = 64U,
	HDV_MIXED_MEDIUM_BYTES = 1024U,
	HDV_MIXED_LARGE_BYTES = 4096U,
};

typedef struct HdvControlBlock
{
	uint64_t magic;
	uint32_t version;
	uint32_t mode;
	uint32_t slot_count;
	uint32_t slot_bytes;
	uint32_t batch_size;
	uint32_t payload_mode;
	uint32_t size_pattern;
	uint32_t reserved32;
	uint64_t iterations;
	uint64_t payload_seed;

	uint64_t published_epoch;
	uint64_t consumed_epoch;
	uint64_t start_epoch;
	uint64_t done_epoch;
	uint64_t error_epoch;
	uint64_t error_code;

	uint64_t dpu_data_dma_count;
	uint64_t dpu_publish_dma_count;
	uint64_t dpu_consumed_dma_read_count;
	uint64_t dpu_pe_progress_calls;
	uint64_t host_poll_iterations;
	uint64_t host_validated_epochs;
	uint64_t reserved[6];
} __attribute__((aligned(HDV_CACHELINE))) HdvControlBlock;

typedef struct HdvSlotHeader
{
	uint64_t seq_begin;
	uint64_t seq_end;
	uint64_t generation;
	uint32_t payload_bytes;
	uint32_t flags;
	uint64_t checksum;
	uint64_t poison_before;
	uint64_t poison_after;
} HdvSlotHeader;

static inline size_t hdv_payload_offset(void) { return sizeof(HdvSlotHeader); }

static inline size_t hdv_payload_bytes(uint32_t slot_bytes)
{
	if (slot_bytes <= sizeof(HdvSlotHeader))
		return 0;
	return slot_bytes - sizeof(HdvSlotHeader);
}

static inline size_t hdv_record_bytes(uint32_t slot_bytes, uint64_t epoch, enum HdvSizePattern size_pattern)
{
	if (size_pattern == HDV_SIZE_PATTERN_MIXED_64_4K)
		return (epoch & 1ULL) != 0 ? HDV_MIXED_SMALL_BYTES : HDV_MIXED_LARGE_BYTES;
	if (size_pattern == HDV_SIZE_PATTERN_MIXED_64_1K)
		return (epoch & 1ULL) != 0 ? HDV_MIXED_SMALL_BYTES : HDV_MIXED_MEDIUM_BYTES;
	return slot_bytes;
}

static inline size_t hdv_record_payload_bytes(uint32_t slot_bytes, uint64_t epoch, enum HdvSizePattern size_pattern)
{
	size_t record_bytes = hdv_record_bytes(slot_bytes, epoch, size_pattern);

	if (record_bytes <= sizeof(HdvSlotHeader))
		return 0;
	return record_bytes - sizeof(HdvSlotHeader);
}

static inline uint64_t hdv_total_record_bytes(uint32_t slot_bytes, uint64_t iterations,
											  enum HdvSizePattern size_pattern)
{
	if (size_pattern == HDV_SIZE_PATTERN_MIXED_64_4K)
	{
		uint64_t small_count = (iterations + 1) / 2;
		uint64_t large_count = iterations / 2;

		(void)slot_bytes;
		return small_count * HDV_MIXED_SMALL_BYTES + large_count * HDV_MIXED_LARGE_BYTES;
	}
	if (size_pattern == HDV_SIZE_PATTERN_MIXED_64_1K)
	{
		uint64_t small_count = (iterations + 1) / 2;
		uint64_t medium_count = iterations / 2;

		(void)slot_bytes;
		return small_count * HDV_MIXED_SMALL_BYTES + medium_count * HDV_MIXED_MEDIUM_BYTES;
	}
	return iterations * (uint64_t)slot_bytes;
}

static inline size_t hdv_slots_offset(void)
{
	size_t control_size = sizeof(HdvControlBlock);
	return (control_size + HDV_CACHELINE - 1) & ~(size_t)(HDV_CACHELINE - 1);
}

static inline size_t hdv_slot_offset(uint64_t epoch, uint32_t slot_count, uint32_t slot_bytes)
{
	uint64_t slot_index = (epoch - 1) & (uint64_t)(slot_count - 1);
	return hdv_slots_offset() + (size_t)slot_index * (size_t)slot_bytes;
}

static inline size_t hdv_region_size(uint32_t slot_count, uint32_t slot_bytes)
{
	return hdv_slots_offset() + (size_t)slot_count * (size_t)slot_bytes;
}

static inline uint8_t hdv_expected_payload_byte(uint64_t seed, uint64_t epoch, size_t offset)
{
	uint64_t x = seed ^ (epoch * 0x9e3779b97f4a7c15ULL);
	x ^= (uint64_t)offset * 0xbf58476d1ce4e5b9ULL;
	x ^= x >> 33;
	x *= 0xff51afd7ed558ccdULL;
	x ^= x >> 33;
	return (uint8_t)(x & 0xffU);
}

static inline uint64_t hdv_checksum_bytes(const uint8_t *payload, size_t len)
{
	uint64_t checksum = 1469598103934665603ULL;

	for (size_t i = 0; i < len; i++)
	{
		checksum ^= payload[i];
		checksum *= 1099511628211ULL;
	}

	return checksum;
}

static inline void hdv_fill_slot(void *slot, uint32_t slot_bytes, uint64_t seed, uint64_t epoch, uint32_t slot_count,
								 enum HdvSizePattern size_pattern)
{
	HdvSlotHeader *header = (HdvSlotHeader *)slot;
	uint8_t *payload = (uint8_t *)slot + hdv_payload_offset();
	size_t payload_len = hdv_record_payload_bytes(slot_bytes, epoch, size_pattern);

	header->seq_begin = epoch;
	header->seq_end = 0;
	header->generation = (epoch - 1) / slot_count;
	header->payload_bytes = (uint32_t)payload_len;
	header->flags = 0;
	header->poison_before = HDV_POISON_BEFORE;
	header->poison_after = HDV_POISON_AFTER;

	for (size_t i = 0; i < payload_len; i++)
		payload[i] = hdv_expected_payload_byte(seed, epoch, i);

	header->checksum = hdv_checksum_bytes(payload, payload_len);
	header->seq_end = epoch;
}

static inline void hdv_fill_slot_header_only(void *slot, uint32_t slot_bytes, uint64_t seed, uint64_t epoch,
											 uint32_t slot_count, enum HdvSizePattern size_pattern)
{
	HdvSlotHeader *header = (HdvSlotHeader *)slot;
	size_t record_bytes = hdv_record_bytes(slot_bytes, epoch, size_pattern);
	size_t payload_len = hdv_record_payload_bytes(slot_bytes, epoch, size_pattern);

	header->seq_begin = epoch;
	header->seq_end = 0;
	header->generation = (epoch - 1) / slot_count;
	header->payload_bytes = (uint32_t)payload_len;
	header->flags = 0;
	header->poison_before = HDV_POISON_BEFORE;
	header->poison_after = HDV_POISON_AFTER;
	header->checksum = seed ^ epoch ^ ((uint64_t)record_bytes << 32);
	header->seq_end = epoch;
}

#endif
