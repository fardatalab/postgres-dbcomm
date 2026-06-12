#include "doca_homer_validation_common.h"

#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <doca_buf.h>
#include <doca_buf_inventory.h>
#include <doca_ctx.h>
#include <doca_dev.h>
#include <doca_dma.h>
#include <doca_error.h>
#include <doca_mmap.h>
#include <doca_pe.h>
#include <doca_sync_event.h>

#include "common.h"

#define HDV_DEFAULT_SLOT_COUNT 1024U
#define HDV_DEFAULT_SLOT_BYTES 4096U
#define HDV_DEFAULT_ITERATIONS 100000ULL
#define HDV_DEFAULT_BATCH_SIZE 1U
#define HDV_DEFAULT_ASYNC_WINDOW 1U
#define HDV_DEFAULT_TIMEOUT_SEC 60U
#define HDV_LOCAL_EXTRA_BYTES 4096U
#define HDV_EXPORT_DESC_MAX 4096U

typedef enum HdvRole
{
	HDV_ROLE_NONE = 0,
	HDV_ROLE_HOST,
	HDV_ROLE_DPU,
} HdvRole;

typedef enum HdvFlushPolicy
{
	HDV_FLUSH_NONE = 0,
	HDV_FLUSH_PUBLISH,
	HDV_FLUSH_ALL,
} HdvFlushPolicy;

typedef struct HdvConfig
{
	HdvRole role;
	enum HdvMode mode;
	const char *pci_addr;
	const char *descriptor_path;
	const char *buffer_info_path;
	const char *sync_event_path;
	uint32_t slot_count;
	uint32_t slot_bytes;
	uint32_t batch_size;
	uint32_t async_window;
	uint64_t iterations;
	uint64_t seed;
	uint32_t timeout_sec;
	uint32_t early_publish_delay_us;
	uint32_t ready_delay_ms;
	bool relaxed_ordering;
	bool ordered_completions;
	bool optimize_reports;
	HdvFlushPolicy flush_policy;
	enum HdvPayloadMode payload_mode;
} HdvConfig;

typedef struct HdvTaskState
{
	bool done;
	doca_error_t result;
} HdvTaskState;

typedef struct HdvAsyncDmaTask
{
	bool done;
	doca_error_t result;
	bool in_flight;
	uint64_t epoch;
	void *local_slot;
	struct doca_buf *src;
	struct doca_buf *dst;
	struct doca_dma_task_memcpy *task;
} HdvAsyncDmaTask;

typedef struct HdvDmaContext
{
	struct doca_dev *dev;
	struct doca_dma *dma;
	struct doca_ctx *ctx;
	struct doca_pe *pe;
	struct doca_buf_inventory *buf_inv;
	struct doca_mmap *local_mmap;
	struct doca_mmap *remote_mmap;
	uint64_t pe_progress_calls;
} HdvDmaContext;

static doca_error_t hdv_dma_task_is_supported(struct doca_devinfo *devinfo)
{
	return doca_dma_cap_task_memcpy_is_supported(devinfo);
}

static void hdv_dma_complete_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data,
								union doca_data ctx_user_data)
{
	HdvTaskState *wait = (HdvTaskState *)task_user_data.ptr;

	(void)task;
	(void)ctx_user_data;

	wait->result = DOCA_SUCCESS;
	wait->done = true;
}

static void hdv_dma_error_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data,
							 union doca_data ctx_user_data)
{
	HdvTaskState *wait = (HdvTaskState *)task_user_data.ptr;

	(void)ctx_user_data;

	wait->result = doca_task_get_status(doca_dma_task_memcpy_as_task(task));
	wait->done = true;
}

static void usage(const char *prog)
{
	fprintf(stderr,
			"usage: %s --role=host|dpu --descriptor-path=PATH --buffer-info-path=PATH [options]\n"
			"options:\n"
			"  --pci-addr=ADDR              DOCA PCI address; omitted means first DMA-capable device\n"
			"  "
			"--mode=write-publish|early-publish|host-write-dpu-read|sync-event-publish|sync-event-early-publish|sync-"
			"event-async-pull\n"
			"  --slot-count=N              power-of-two ring slots, default %u\n"
			"  --slot-bytes=N              bytes per slot, default %u\n"
			"  --batch-size=N              publish after N slot writes, default %u\n"
			"  --async-window=N            in-flight DMA tasks for async DPU-pull, default %u\n"
			"  --iterations=N              epochs to produce/validate, default %" PRIu64 "\n"
			"  --payload-mode=full|header  full payload scan or header-only validation\n"
			"  --timeout-sec=N             host wait timeout, default %u\n"
			"  --ready-delay-ms=N          host delay after descriptor export, default 0\n"
			"  --sync-event-path=PATH      sync-event export descriptor for sync-event modes\n"
			"  --early-publish-delay-us=N  DPU delay after early publish, default 1000\n"
			"  --flush=none|publish|all    doca_task_submit_ex flush policy\n"
			"  --optimize-reports          allow deferred data-DMA completion callbacks\n"
			"  --ordered-completions       request ordered DMA completions on the DPU\n"
			"  --relaxed-ordering          add PCI relaxed-ordering mmap permission on host\n",
			prog, HDV_DEFAULT_SLOT_COUNT, HDV_DEFAULT_SLOT_BYTES, HDV_DEFAULT_BATCH_SIZE, HDV_DEFAULT_ASYNC_WINDOW,
			(uint64_t)HDV_DEFAULT_ITERATIONS, HDV_DEFAULT_TIMEOUT_SEC);
}

static bool parse_u32(const char *value, uint32_t *out)
{
	char *end = NULL;
	unsigned long parsed;

	errno = 0;
	parsed = strtoul(value, &end, 0);
	if (errno != 0 || end == value || *end != '\0' || parsed > UINT32_MAX)
		return false;
	*out = (uint32_t)parsed;
	return true;
}

static bool parse_u64(const char *value, uint64_t *out)
{
	char *end = NULL;
	unsigned long long parsed;

	errno = 0;
	parsed = strtoull(value, &end, 0);
	if (errno != 0 || end == value || *end != '\0')
		return false;
	*out = (uint64_t)parsed;
	return true;
}

static bool is_power_of_two(uint32_t value) { return value != 0 && (value & (value - 1U)) == 0; }

static bool parse_args(int argc, char **argv, HdvConfig *config)
{
	memset(config, 0, sizeof(*config));
	config->mode = HDV_MODE_WRITE_PUBLISH;
	config->slot_count = HDV_DEFAULT_SLOT_COUNT;
	config->slot_bytes = HDV_DEFAULT_SLOT_BYTES;
	config->batch_size = HDV_DEFAULT_BATCH_SIZE;
	config->async_window = HDV_DEFAULT_ASYNC_WINDOW;
	config->iterations = HDV_DEFAULT_ITERATIONS;
	config->seed = HDV_DEFAULT_SEED;
	config->timeout_sec = HDV_DEFAULT_TIMEOUT_SEC;
	config->early_publish_delay_us = 1000;
	config->flush_policy = HDV_FLUSH_PUBLISH;
	config->payload_mode = HDV_PAYLOAD_FULL;

	for (int i = 1; i < argc; i++)
	{
		const char *arg = argv[i];
		const char *value = strchr(arg, '=');
		size_t name_len;

		if (strcmp(arg, "--ordered-completions") == 0)
		{
			config->ordered_completions = true;
			continue;
		}
		if (strcmp(arg, "--optimize-reports") == 0)
		{
			config->optimize_reports = true;
			continue;
		}
		if (strcmp(arg, "--relaxed-ordering") == 0)
		{
			config->relaxed_ordering = true;
			continue;
		}
		if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0)
		{
			usage(argv[0]);
			return false;
		}
		if (value == NULL || strncmp(arg, "--", 2) != 0)
		{
			fprintf(stderr, "invalid argument: %s\n", arg);
			return false;
		}

		name_len = (size_t)(value - arg);
		value++;

		if (name_len == strlen("--role") && strncmp(arg, "--role", name_len) == 0)
		{
			if (strcmp(value, "host") == 0)
				config->role = HDV_ROLE_HOST;
			else if (strcmp(value, "dpu") == 0)
				config->role = HDV_ROLE_DPU;
			else
			{
				fprintf(stderr, "invalid role: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--mode") && strncmp(arg, "--mode", name_len) == 0)
		{
			if (strcmp(value, "write-publish") == 0)
				config->mode = HDV_MODE_WRITE_PUBLISH;
			else if (strcmp(value, "early-publish") == 0)
				config->mode = HDV_MODE_EARLY_PUBLISH;
			else if (strcmp(value, "host-write-dpu-read") == 0)
				config->mode = HDV_MODE_HOST_WRITE_DPU_READ;
			else if (strcmp(value, "sync-event-publish") == 0)
				config->mode = HDV_MODE_SYNC_EVENT_PUBLISH;
			else if (strcmp(value, "sync-event-early-publish") == 0)
				config->mode = HDV_MODE_SYNC_EVENT_EARLY_PUBLISH;
			else if (strcmp(value, "sync-event-async-pull") == 0)
				config->mode = HDV_MODE_SYNC_EVENT_ASYNC_PULL;
			else
			{
				fprintf(stderr, "invalid mode: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--pci-addr") && strncmp(arg, "--pci-addr", name_len) == 0)
		{
			config->pci_addr = value;
		}
		else if (name_len == strlen("--descriptor-path") && strncmp(arg, "--descriptor-path", name_len) == 0)
		{
			config->descriptor_path = value;
		}
		else if (name_len == strlen("--buffer-info-path") && strncmp(arg, "--buffer-info-path", name_len) == 0)
		{
			config->buffer_info_path = value;
		}
		else if (name_len == strlen("--sync-event-path") && strncmp(arg, "--sync-event-path", name_len) == 0)
		{
			config->sync_event_path = value;
		}
		else if (name_len == strlen("--slot-count") && strncmp(arg, "--slot-count", name_len) == 0)
		{
			if (!parse_u32(value, &config->slot_count))
				return false;
		}
		else if (name_len == strlen("--slot-bytes") && strncmp(arg, "--slot-bytes", name_len) == 0)
		{
			if (!parse_u32(value, &config->slot_bytes))
				return false;
		}
		else if (name_len == strlen("--batch-size") && strncmp(arg, "--batch-size", name_len) == 0)
		{
			if (!parse_u32(value, &config->batch_size))
				return false;
		}
		else if (name_len == strlen("--async-window") && strncmp(arg, "--async-window", name_len) == 0)
		{
			if (!parse_u32(value, &config->async_window))
				return false;
		}
		else if (name_len == strlen("--iterations") && strncmp(arg, "--iterations", name_len) == 0)
		{
			if (!parse_u64(value, &config->iterations))
				return false;
		}
		else if (name_len == strlen("--seed") && strncmp(arg, "--seed", name_len) == 0)
		{
			if (!parse_u64(value, &config->seed))
				return false;
		}
		else if (name_len == strlen("--timeout-sec") && strncmp(arg, "--timeout-sec", name_len) == 0)
		{
			if (!parse_u32(value, &config->timeout_sec))
				return false;
		}
		else if (name_len == strlen("--early-publish-delay-us") &&
				 strncmp(arg, "--early-publish-delay-us", name_len) == 0)
		{
			if (!parse_u32(value, &config->early_publish_delay_us))
				return false;
		}
		else if (name_len == strlen("--ready-delay-ms") && strncmp(arg, "--ready-delay-ms", name_len) == 0)
		{
			if (!parse_u32(value, &config->ready_delay_ms))
				return false;
		}
		else if (name_len == strlen("--flush") && strncmp(arg, "--flush", name_len) == 0)
		{
			if (strcmp(value, "none") == 0)
				config->flush_policy = HDV_FLUSH_NONE;
			else if (strcmp(value, "publish") == 0)
				config->flush_policy = HDV_FLUSH_PUBLISH;
			else if (strcmp(value, "all") == 0)
				config->flush_policy = HDV_FLUSH_ALL;
			else
				return false;
		}
		else if (name_len == strlen("--payload-mode") && strncmp(arg, "--payload-mode", name_len) == 0)
		{
			if (strcmp(value, "full") == 0)
				config->payload_mode = HDV_PAYLOAD_FULL;
			else if (strcmp(value, "header") == 0)
				config->payload_mode = HDV_PAYLOAD_HEADER;
			else
				return false;
		}
		else
		{
			fprintf(stderr, "unknown argument: %s\n", arg);
			return false;
		}
	}

	if (config->role == HDV_ROLE_NONE || config->descriptor_path == NULL || config->buffer_info_path == NULL)
	{
		usage(argv[0]);
		return false;
	}
	if ((config->mode == HDV_MODE_SYNC_EVENT_PUBLISH || config->mode == HDV_MODE_SYNC_EVENT_EARLY_PUBLISH ||
		 config->mode == HDV_MODE_SYNC_EVENT_ASYNC_PULL) &&
		config->sync_event_path == NULL)
	{
		fprintf(stderr, "--sync-event-path is required for sync-event modes\n");
		return false;
	}
	if (!is_power_of_two(config->slot_count))
	{
		fprintf(stderr, "--slot-count must be a power of two\n");
		return false;
	}
	if (config->slot_bytes < sizeof(HdvSlotHeader) + 1U)
	{
		fprintf(stderr, "--slot-bytes must be at least %zu\n", sizeof(HdvSlotHeader) + 1U);
		return false;
	}
	if (config->batch_size == 0 || config->batch_size > config->slot_count)
	{
		fprintf(stderr, "--batch-size must be in [1, slot-count]\n");
		return false;
	}
	if (config->async_window == 0 || config->async_window > config->slot_count)
	{
		fprintf(stderr, "--async-window must be in [1, slot-count]\n");
		return false;
	}
	if (config->iterations == 0)
	{
		fprintf(stderr, "--iterations must be nonzero\n");
		return false;
	}

	return true;
}

static doca_error_t open_dma_device(const char *pci_addr, struct doca_dev **dev)
{
	if (pci_addr != NULL && pci_addr[0] != '\0')
		return open_doca_device_with_pci(pci_addr, hdv_dma_task_is_supported, dev);

	return open_doca_device_with_capabilities(hdv_dma_task_is_supported, dev);
}

static int write_file_exact(const char *path, const void *data, size_t len)
{
	FILE *file = fopen(path, "wb");

	if (file == NULL)
	{
		perror(path);
		return -1;
	}
	if (fwrite(data, 1, len, file) != len)
	{
		perror("fwrite");
		fclose(file);
		return -1;
	}
	fclose(file);
	return 0;
}

static int write_buffer_info(const char *path, void *addr, size_t len)
{
	FILE *file = fopen(path, "w");

	if (file == NULL)
	{
		perror(path);
		return -1;
	}
	fprintf(file, "%" PRIu64 "\n%" PRIu64 "\n", (uint64_t)(uintptr_t)addr, (uint64_t)len);
	fclose(file);
	return 0;
}

static int read_file_alloc(const char *path, void **data, size_t *len)
{
	FILE *file = fopen(path, "rb");
	long size;
	void *buf;

	if (file == NULL)
	{
		perror(path);
		return -1;
	}
	if (fseek(file, 0, SEEK_END) != 0)
	{
		perror("fseek");
		fclose(file);
		return -1;
	}
	size = ftell(file);
	if (size < 0 || size > HDV_EXPORT_DESC_MAX)
	{
		fprintf(stderr, "invalid descriptor size: %ld\n", size);
		fclose(file);
		return -1;
	}
	if (fseek(file, 0, SEEK_SET) != 0)
	{
		perror("fseek");
		fclose(file);
		return -1;
	}
	buf = calloc(1, (size_t)size);
	if (buf == NULL)
	{
		perror("calloc");
		fclose(file);
		return -1;
	}
	if (fread(buf, 1, (size_t)size, file) != (size_t)size)
	{
		perror("fread");
		free(buf);
		fclose(file);
		return -1;
	}
	fclose(file);
	*data = buf;
	*len = (size_t)size;
	return 0;
}

static int read_buffer_info(const char *path, void **addr, size_t *len)
{
	FILE *file = fopen(path, "r");
	uint64_t addr64;
	uint64_t len64;

	if (file == NULL)
	{
		perror(path);
		return -1;
	}
	if (fscanf(file, "%" SCNu64 "\n%" SCNu64, &addr64, &len64) != 2)
	{
		fprintf(stderr, "failed to parse %s\n", path);
		fclose(file);
		return -1;
	}
	fclose(file);
	*addr = (void *)(uintptr_t)addr64;
	*len = (size_t)len64;
	return 0;
}

static uint64_t monotonic_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
	return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void fill_slot_for_mode(const HdvConfig *config, void *slot, uint64_t epoch)
{
	if (config->payload_mode == HDV_PAYLOAD_HEADER)
		hdv_fill_slot_header_only(slot, config->slot_bytes, config->seed, epoch, config->slot_count);
	else
		hdv_fill_slot(slot, config->slot_bytes, config->seed, epoch, config->slot_count);
}

static int validate_slot(const HdvConfig *config, void *slot, uint64_t epoch, char *error, size_t error_len)
{
	const HdvSlotHeader *header = (const HdvSlotHeader *)slot;
	const uint8_t *payload = (const uint8_t *)slot + hdv_payload_offset();
	size_t payload_len = hdv_payload_bytes(config->slot_bytes);
	uint64_t checksum;

	if (header->seq_begin != epoch)
	{
		snprintf(error, error_len, "seq_begin=%" PRIu64 " expected=%" PRIu64, header->seq_begin, epoch);
		return -1;
	}
	if (header->seq_end != epoch)
	{
		snprintf(error, error_len, "seq_end=%" PRIu64 " expected=%" PRIu64, header->seq_end, epoch);
		return -1;
	}
	if (header->generation != (epoch - 1) / config->slot_count)
	{
		snprintf(error, error_len, "generation=%" PRIu64 " expected=%" PRIu64, header->generation,
				 (epoch - 1) / config->slot_count);
		return -1;
	}
	if (header->payload_bytes != payload_len)
	{
		snprintf(error, error_len, "payload_bytes=%u expected=%zu", header->payload_bytes, payload_len);
		return -1;
	}
	if (header->poison_before != HDV_POISON_BEFORE || header->poison_after != HDV_POISON_AFTER)
	{
		snprintf(error, error_len, "poison mismatch before=0x%" PRIx64 " after=0x%" PRIx64, header->poison_before,
				 header->poison_after);
		return -1;
	}
	if (config->payload_mode == HDV_PAYLOAD_HEADER)
	{
		checksum = config->seed ^ epoch ^ ((uint64_t)config->slot_bytes << 32);
		if (header->checksum != checksum)
		{
			snprintf(error, error_len, "header_checksum=0x%" PRIx64 " expected=0x%" PRIx64, header->checksum, checksum);
			return -1;
		}
		return 0;
	}
	for (size_t i = 0; i < payload_len; i++)
	{
		uint8_t expected = hdv_expected_payload_byte(config->seed, epoch, i);

		if (payload[i] != expected)
		{
			snprintf(error, error_len, "payload[%zu]=0x%02x expected=0x%02x", i, payload[i], expected);
			return -1;
		}
	}
	checksum = hdv_checksum_bytes(payload, payload_len);
	if (header->checksum != checksum)
	{
		snprintf(error, error_len, "checksum=0x%" PRIx64 " expected=0x%" PRIx64, header->checksum, checksum);
		return -1;
	}
	return 0;
}

static int run_host(const HdvConfig *config)
{
	struct doca_dev *dev = NULL;
	struct doca_mmap *mmap = NULL;
	struct doca_sync_event *sync_event = NULL;
	const void *export_desc = NULL;
	size_t export_desc_len = 0;
	const uint8_t *sync_event_desc = NULL;
	size_t sync_event_desc_len = 0;
	void *region = NULL;
	size_t region_size = hdv_region_size(config->slot_count, config->slot_bytes);
	size_t page_size = (size_t)sysconf(_SC_PAGESIZE);
	HdvControlBlock *control;
	uint64_t next_epoch = 1;
	uint64_t start_ns;
	uint64_t first_publish_ns = 0;
	uint64_t last_report_ns;
	doca_error_t result;
	int rc = 1;

	if (posix_memalign(&region, page_size, region_size) != 0)
	{
		perror("posix_memalign");
		return 1;
	}
	memset(region, 0xa5, region_size);

	control = (HdvControlBlock *)region;
	memset(control, 0, sizeof(*control));
	control->magic = HDV_MAGIC;
	control->version = HDV_VERSION;
	control->mode = (uint32_t)config->mode;
	control->slot_count = config->slot_count;
	control->slot_bytes = config->slot_bytes;
	control->batch_size = config->batch_size;
	control->payload_mode = (uint32_t)config->payload_mode;
	control->iterations = config->iterations;
	control->payload_seed = config->seed;
	__atomic_store_n(&control->published_epoch, 0, __ATOMIC_RELEASE);
	__atomic_store_n(&control->consumed_epoch, 0, __ATOMIC_RELEASE);
	__atomic_store_n(&control->start_epoch, 1, __ATOMIC_RELEASE);

	result = open_dma_device(config->pci_addr, &dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "open DOCA DMA device failed: %s\n", doca_error_get_descr(result));
		goto out_free_region;
	}
	result = doca_mmap_create(&mmap);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_create failed: %s\n", doca_error_get_descr(result));
		goto out_close_dev;
	}
	result = doca_mmap_add_dev(mmap, dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_add_dev failed: %s\n", doca_error_get_descr(result));
		goto out_stop_sync_event;
	}
	result = doca_mmap_set_permissions(
		mmap, DOCA_ACCESS_FLAG_PCI_READ_WRITE | (config->relaxed_ordering ? DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING : 0));
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_set_permissions failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_mmap;
	}
	result = doca_mmap_set_memrange(mmap, region, region_size);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_set_memrange failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_mmap;
	}
	result = doca_mmap_start(mmap);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_start failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_mmap;
	}
	result = doca_mmap_export_pci(mmap, dev, &export_desc, &export_desc_len);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_mmap_export_pci failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_mmap;
	}
	if (write_file_exact(config->descriptor_path, export_desc, export_desc_len) != 0 ||
		write_buffer_info(config->buffer_info_path, region, region_size) != 0)
		goto out_destroy_mmap;

	if (config->mode == HDV_MODE_SYNC_EVENT_PUBLISH || config->mode == HDV_MODE_SYNC_EVENT_EARLY_PUBLISH ||
		config->mode == HDV_MODE_SYNC_EVENT_ASYNC_PULL)
	{
		result = doca_sync_event_create(&sync_event);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "doca_sync_event_create failed: %s\n", doca_error_get_descr(result));
			goto out_destroy_mmap;
		}
		result = doca_sync_event_add_publisher_location_cpu(sync_event, dev);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "sync_event publisher CPU failed: %s\n", doca_error_get_descr(result));
			goto out_destroy_sync_event;
		}
		result = doca_sync_event_add_subscriber_location_remote_pci(sync_event);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "sync_event remote PCI subscriber failed: %s\n", doca_error_get_descr(result));
			goto out_destroy_sync_event;
		}
		result = doca_sync_event_start(sync_event);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "doca_sync_event_start failed: %s\n", doca_error_get_descr(result));
			goto out_destroy_sync_event;
		}
		result = doca_sync_event_export_to_remote_pci(sync_event, dev, &sync_event_desc, &sync_event_desc_len);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "doca_sync_event_export_to_remote_pci failed: %s\n", doca_error_get_descr(result));
			goto out_stop_sync_event;
		}
		if (write_file_exact(config->sync_event_path, sync_event_desc, sync_event_desc_len) != 0)
			goto out_stop_sync_event;
	}

	printf("HDV_HOST_READY descriptor=%s buffer=%s region=%p bytes=%zu iterations=%" PRIu64 "\n",
		   config->descriptor_path, config->buffer_info_path, region, region_size, config->iterations);
	fflush(stdout);
	if (config->ready_delay_ms != 0)
		usleep(config->ready_delay_ms * 1000U);

	if (config->mode == HDV_MODE_HOST_WRITE_DPU_READ || config->mode == HDV_MODE_SYNC_EVENT_PUBLISH ||
		config->mode == HDV_MODE_SYNC_EVENT_EARLY_PUBLISH || config->mode == HDV_MODE_SYNC_EVENT_ASYNC_PULL)
	{
		uint64_t epoch = 1;
		uint64_t consumed = 0;
		uint64_t producer_start_ns = monotonic_ns();

		while (epoch <= config->iterations)
		{
			uint64_t batch_end = epoch + config->batch_size - 1;

			if (batch_end > config->iterations)
				batch_end = config->iterations;

			while (batch_end - consumed > config->slot_count)
			{
				consumed = __atomic_load_n(&control->consumed_epoch, __ATOMIC_ACQUIRE);
				if ((monotonic_ns() - producer_start_ns) / 1000000000ULL > config->timeout_sec)
				{
					fprintf(stderr, "HDV_HOST_TIMEOUT producing epoch=%" PRIu64 " consumed=%" PRIu64 "\n", epoch,
							consumed);
					goto out_stop_sync_event;
				}
			}

			if (config->mode == HDV_MODE_SYNC_EVENT_EARLY_PUBLISH)
			{
				result = doca_sync_event_update_set(sync_event, batch_end);
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "early sync_event update failed: %s\n", doca_error_get_descr(result));
					goto out_stop_sync_event;
				}
				if (config->early_publish_delay_us != 0)
					usleep(config->early_publish_delay_us);
			}

			while (epoch <= batch_end)
			{
				void *slot = (uint8_t *)region + hdv_slot_offset(epoch, config->slot_count, config->slot_bytes);

				fill_slot_for_mode(config, slot, epoch);
				epoch++;
			}
			if (config->mode == HDV_MODE_HOST_WRITE_DPU_READ)
			{
				__atomic_store_n(&control->published_epoch, batch_end, __ATOMIC_RELEASE);
			}
			else if (config->mode == HDV_MODE_SYNC_EVENT_PUBLISH || config->mode == HDV_MODE_SYNC_EVENT_ASYNC_PULL)
			{
				/*
				 * This is the exact candidate publish operation under test:
				 * host CPU writes the slots first, then updates the
				 * remote-PCI sync-event with the published frontier.
				 */
				__atomic_thread_fence(__ATOMIC_RELEASE);
				result = doca_sync_event_update_set(sync_event, batch_end);
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "sync_event update failed: %s\n", doca_error_get_descr(result));
					goto out_stop_sync_event;
				}
			}
		}

		while (__atomic_load_n(&control->done_epoch, __ATOMIC_ACQUIRE) != config->iterations)
		{
			if (__atomic_load_n(&control->error_epoch, __ATOMIC_ACQUIRE) != 0)
			{
				fprintf(stderr, "HDV_HOST_REMOTE_ERROR epoch=%" PRIu64 " code=%" PRIu64 "\n", control->error_epoch,
						control->error_code);
				goto out_stop_sync_event;
			}
			if ((monotonic_ns() - producer_start_ns) / 1000000000ULL > config->timeout_sec)
			{
				fprintf(stderr, "HDV_HOST_TIMEOUT waiting_done consumed=%" PRIu64 " published=%" PRIu64 "\n",
						__atomic_load_n(&control->consumed_epoch, __ATOMIC_ACQUIRE),
						__atomic_load_n(&control->published_epoch, __ATOMIC_ACQUIRE));
				goto out_stop_sync_event;
			}
		}

		{
			uint64_t elapsed_ns = monotonic_ns() - producer_start_ns;
			double elapsed_sec = (double)elapsed_ns / 1000000000.0;
			double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

			printf("HDV_HOST_PRODUCER_PASS iterations=%" PRIu64 " dpu_data_dma=%" PRIu64 " dpu_publish_dma=%" PRIu64
				   " dpu_pe_progress=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
				   config->iterations, control->dpu_data_dma_count, control->dpu_publish_dma_count,
				   control->dpu_pe_progress_calls, elapsed_sec, elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
		}
		rc = 0;
		goto out_stop_sync_event;
	}

	start_ns = monotonic_ns();
	last_report_ns = start_ns;
	while (next_epoch <= config->iterations)
	{
		uint64_t published = __atomic_load_n(&control->published_epoch, __ATOMIC_ACQUIRE);

		control->host_poll_iterations++;
		if (published != 0 && first_publish_ns == 0)
			first_publish_ns = monotonic_ns();
		while (next_epoch <= published && next_epoch <= config->iterations)
		{
			char error[256];
			void *slot = (uint8_t *)region + hdv_slot_offset(next_epoch, config->slot_count, config->slot_bytes);

			if (validate_slot(config, slot, next_epoch, error, sizeof(error)) != 0)
			{
				control->error_epoch = next_epoch;
				control->error_code = 1;
				fprintf(stderr, "HDV_HOST_VALIDATION_FAIL epoch=%" PRIu64 " %s\n", next_epoch, error);
				goto out_destroy_mmap;
			}
			__atomic_store_n(&control->consumed_epoch, next_epoch, __ATOMIC_RELEASE);
			control->host_validated_epochs = next_epoch;
			next_epoch++;
		}
		if (__atomic_load_n(&control->error_epoch, __ATOMIC_ACQUIRE) != 0)
		{
			fprintf(stderr, "HDV_HOST_REMOTE_ERROR epoch=%" PRIu64 " code=%" PRIu64 "\n", control->error_epoch,
					control->error_code);
			goto out_destroy_mmap;
		}
		if ((monotonic_ns() - start_ns) / 1000000000ULL > config->timeout_sec)
		{
			fprintf(stderr, "HDV_HOST_TIMEOUT next_epoch=%" PRIu64 " published=%" PRIu64 "\n", next_epoch, published);
			goto out_destroy_mmap;
		}
		if (monotonic_ns() - last_report_ns > 5000000000ULL)
		{
			fprintf(stderr, "HDV_HOST_PROGRESS validated=%" PRIu64 " published=%" PRIu64 "\n", next_epoch - 1,
					published);
			last_report_ns = monotonic_ns();
		}
	}

	control->done_epoch = config->iterations;
	{
		uint64_t elapsed_ns = first_publish_ns == 0 ? 0 : monotonic_ns() - first_publish_ns;
		double elapsed_sec = elapsed_ns == 0 ? 0.0 : (double)elapsed_ns / 1000000000.0;
		double mbps = elapsed_sec == 0.0
						  ? 0.0
						  : ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0) / elapsed_sec;

		printf("HDV_HOST_PASS iterations=%" PRIu64 " polls=%" PRIu64 " data_dma=%" PRIu64 " publish_dma=%" PRIu64
			   " consumed_dma_reads=%" PRIu64 " dpu_pe_progress=%" PRIu64
			   " host_validate_sec=%.6f host_validate_mib_s=%.2f\n",
			   config->iterations, control->host_poll_iterations, control->dpu_data_dma_count,
			   control->dpu_publish_dma_count, control->dpu_consumed_dma_read_count, control->dpu_pe_progress_calls,
			   elapsed_sec, mbps);
	}
	rc = 0;

out_stop_sync_event:
	if (sync_event != NULL)
	{
		result = doca_sync_event_stop(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "doca_sync_event_stop failed: %s\n", doca_error_get_descr(result));
	}
out_destroy_sync_event:
	if (sync_event != NULL)
	{
		result = doca_sync_event_destroy(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "doca_sync_event_destroy failed: %s\n", doca_error_get_descr(result));
	}
out_destroy_mmap:
	if (mmap != NULL)
	{
		result = doca_mmap_destroy(mmap);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "doca_mmap_destroy failed: %s\n", doca_error_get_descr(result));
	}
out_close_dev:
	if (dev != NULL)
	{
		result = doca_dev_close(dev);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "doca_dev_close failed: %s\n", doca_error_get_descr(result));
	}
out_free_region:
	free(region);
	return rc;
}

static doca_error_t hdv_dpu_init(const HdvConfig *config, void *local_region, size_t local_region_size,
								 const void *export_desc, size_t export_desc_len, HdvDmaContext *ctx)
{
	doca_error_t result;
	union doca_data ctx_user_data = {0};

	memset(ctx, 0, sizeof(*ctx));
	result = open_dma_device(config->pci_addr, &ctx->dev);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_create(&ctx->local_mmap);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_add_dev(ctx->local_mmap, ctx->dev);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_set_memrange(ctx->local_mmap, local_region, local_region_size);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_start(ctx->local_mmap);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_create_from_export(NULL, export_desc, export_desc_len, ctx->dev, &ctx->remote_mmap);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_create((size_t)config->async_window * 2U + 16U, &ctx->buf_inv);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_start(ctx->buf_inv);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_pe_create(&ctx->pe);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_dma_create(ctx->dev, &ctx->dma);
	if (result != DOCA_SUCCESS)
		return result;
	ctx->ctx = doca_dma_as_ctx(ctx->dma);
	result = doca_dma_task_memcpy_set_conf(ctx->dma, hdv_dma_complete_cb, hdv_dma_error_cb, config->async_window + 8U);
	if (result != DOCA_SUCCESS)
		return result;
	ctx_user_data.ptr = ctx;
	(void)doca_ctx_set_user_data(ctx->ctx, ctx_user_data);
	result = doca_pe_connect_ctx(ctx->pe, ctx->ctx);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_ctx_start(ctx->ctx);
	if (result != DOCA_SUCCESS)
		return result;
	if (config->ordered_completions)
	{
		result = doca_dma_set_ordered_completions(ctx->dma, 1);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "ordered completions request failed: %s\n", doca_error_get_descr(result));
	}
	return DOCA_SUCCESS;
}

static void hdv_dpu_destroy(HdvDmaContext *ctx)
{
	if (ctx->ctx != NULL)
		(void)doca_ctx_stop(ctx->ctx);
	if (ctx->pe != NULL)
	{
		for (int i = 0; i < 1000; i++)
		{
			if (doca_pe_progress(ctx->pe) == 0)
				break;
		}
	}
	if (ctx->dma != NULL)
		(void)doca_dma_destroy(ctx->dma);
	if (ctx->pe != NULL)
		(void)doca_pe_destroy(ctx->pe);
	if (ctx->buf_inv != NULL)
		(void)doca_buf_inventory_destroy(ctx->buf_inv);
	if (ctx->remote_mmap != NULL)
		(void)doca_mmap_destroy(ctx->remote_mmap);
	if (ctx->local_mmap != NULL)
		(void)doca_mmap_destroy(ctx->local_mmap);
	if (ctx->dev != NULL)
		(void)doca_dev_close(ctx->dev);
}

static doca_error_t hdv_dma_copy(HdvDmaContext *ctx, struct doca_mmap *src_mmap, void *src_addr, bool src_has_data,
								 struct doca_mmap *dst_mmap, void *dst_addr, size_t len, uint32_t submit_flags)
{
	struct doca_buf *src = NULL;
	struct doca_buf *dst = NULL;
	struct doca_dma_task_memcpy *dma_task = NULL;
	struct doca_task *task;
	HdvTaskState wait = {0};
	union doca_data task_data = {0};
	doca_error_t result;

	if (src_has_data)
		result = doca_buf_inventory_buf_get_by_data(ctx->buf_inv, src_mmap, src_addr, len, &src);
	else
		result = doca_buf_inventory_buf_get_by_addr(ctx->buf_inv, src_mmap, src_addr, len, &src);
	if (result != DOCA_SUCCESS)
		goto out;
	result = doca_buf_inventory_buf_get_by_addr(ctx->buf_inv, dst_mmap, dst_addr, len, &dst);
	if (result != DOCA_SUCCESS)
		goto out;

	task_data.ptr = &wait;
	result = doca_dma_task_memcpy_alloc_init(ctx->dma, src, dst, task_data, &dma_task);
	if (result != DOCA_SUCCESS)
		goto out;
	task = doca_dma_task_memcpy_as_task(dma_task);
	if (submit_flags != 0)
		result = doca_task_submit_ex(task, submit_flags);
	else
		result = doca_task_submit(task);
	if (result != DOCA_SUCCESS)
		goto out_free_task;

	while (!wait.done)
	{
		ctx->pe_progress_calls++;
		(void)doca_pe_progress(ctx->pe);
	}
	result = wait.result;

out_free_task:
	if (dma_task != NULL)
		doca_task_free(doca_dma_task_memcpy_as_task(dma_task));
out:
	if (dst != NULL)
		(void)doca_buf_dec_refcount(dst, NULL);
	if (src != NULL)
		(void)doca_buf_dec_refcount(src, NULL);
	return result;
}

static uint32_t submit_flags_for(const HdvConfig *config, bool publish)
{
	if (config->flush_policy == HDV_FLUSH_ALL)
		return DOCA_TASK_SUBMIT_FLAG_FLUSH;
	if (config->flush_policy == HDV_FLUSH_PUBLISH && publish)
		return DOCA_TASK_SUBMIT_FLAG_FLUSH;
	return 0;
}

static doca_error_t read_remote_u64(HdvDmaContext *dma, void *remote_base, size_t offset, uint64_t *local_value)
{
	return hdv_dma_copy(dma, dma->remote_mmap, (uint8_t *)remote_base + offset, true, dma->local_mmap, local_value,
						sizeof(*local_value), 0);
}

static doca_error_t write_remote_u64(HdvDmaContext *dma, void *remote_base, size_t offset, uint64_t *local_value,
									 uint32_t submit_flags)
{
	return hdv_dma_copy(dma, dma->local_mmap, local_value, true, dma->remote_mmap, (uint8_t *)remote_base + offset,
						sizeof(*local_value), submit_flags);
}

static uint32_t async_data_submit_flags(const HdvConfig *config, bool force_report)
{
	uint32_t flags = 0;

	if (config->optimize_reports && !force_report)
		flags |= DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS;
	if (config->flush_policy == HDV_FLUSH_ALL)
		flags |= DOCA_TASK_SUBMIT_FLAG_FLUSH;
	return flags;
}

static void hdv_async_pool_destroy(HdvAsyncDmaTask *tasks, uint32_t count)
{
	if (tasks == NULL)
		return;
	for (uint32_t i = 0; i < count; i++)
	{
		if (tasks[i].task != NULL)
			doca_task_free(doca_dma_task_memcpy_as_task(tasks[i].task));
		if (tasks[i].dst != NULL)
			(void)doca_buf_dec_refcount(tasks[i].dst, NULL);
		if (tasks[i].src != NULL)
			(void)doca_buf_dec_refcount(tasks[i].src, NULL);
	}
	free(tasks);
}

static doca_error_t hdv_async_pool_create(HdvDmaContext *dma, const HdvConfig *config, void *remote_base,
										  void *local_slots, HdvAsyncDmaTask **out_tasks)
{
	HdvAsyncDmaTask *tasks = calloc(config->async_window, sizeof(*tasks));
	doca_error_t result;

	if (tasks == NULL)
		return DOCA_ERROR_NO_MEMORY;

	for (uint32_t i = 0; i < config->async_window; i++)
	{
		union doca_data task_data = {0};
		void *remote_slot = (uint8_t *)remote_base + hdv_slot_offset(1, config->slot_count, config->slot_bytes);
		void *local_slot = (uint8_t *)local_slots + (size_t)i * config->slot_bytes;

		tasks[i].local_slot = local_slot;
		tasks[i].result = DOCA_SUCCESS;
		task_data.ptr = &tasks[i];

		/*
		 * The async DPU-pull path keeps one reusable task and two reusable
		 * doca_buf objects per in-flight lane. The buffers are retargeted
		 * after each completion; the task object itself is not reallocated.
		 */
		result = doca_buf_inventory_buf_get_by_data(dma->buf_inv, dma->remote_mmap, remote_slot, config->slot_bytes,
													&tasks[i].src);
		if (result != DOCA_SUCCESS)
			goto fail;
		result = doca_buf_inventory_buf_get_by_addr(dma->buf_inv, dma->local_mmap, local_slot, config->slot_bytes,
													&tasks[i].dst);
		if (result != DOCA_SUCCESS)
			goto fail;
		result = doca_dma_task_memcpy_alloc_init(dma->dma, tasks[i].src, tasks[i].dst, task_data, &tasks[i].task);
		if (result != DOCA_SUCCESS)
			goto fail;
	}

	*out_tasks = tasks;
	return DOCA_SUCCESS;

fail:
	hdv_async_pool_destroy(tasks, config->async_window);
	return result;
}

static doca_error_t hdv_async_submit_host_read(HdvAsyncDmaTask *task, const HdvConfig *config, void *remote_slot,
											   uint64_t epoch, bool force_report)
{
	struct doca_task *doca_task = doca_dma_task_memcpy_as_task(task->task);
	doca_error_t result;

	task->done = false;
	task->result = DOCA_SUCCESS;
	task->in_flight = true;
	task->epoch = epoch;

	result = doca_buf_inventory_buf_reuse_by_data(task->src, remote_slot, config->slot_bytes);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_buf_reuse_by_addr(task->dst, task->local_slot, config->slot_bytes);
	if (result != DOCA_SUCCESS)
		return result;
	doca_dma_task_memcpy_set_src(task->task, task->src);
	doca_dma_task_memcpy_set_dst(task->task, task->dst);
	doca_task_set_user_data(doca_task, (union doca_data){.ptr = task});
	return doca_task_submit_ex(doca_task, async_data_submit_flags(config, force_report));
}

static int run_dpu_host_read_async(const HdvConfig *config, HdvDmaContext *dma, void *remote_base, void *local_slots,
								   uint64_t *scratch_value)
{
	HdvAsyncDmaTask *tasks = NULL;
	uint64_t *completed_epochs = NULL;
	uint64_t published = 0;
	uint64_t next_submit = 1;
	uint64_t completed = 0;
	uint64_t consumed_contig = 0;
	uint64_t last_consumed_write = 0;
	uint64_t read_dma_count = 0;
	uint64_t frontier_read_count = 0;
	uint64_t consumed_write_count = 0;
	uint64_t in_flight = 0;
	uint64_t start = monotonic_ns();
	uint64_t last_progress_report = start;
	doca_error_t result;
	int rc = 1;

	result = hdv_async_pool_create(dma, config, remote_base, local_slots, &tasks);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "async pool create failed: %s\n", doca_error_get_descr(result));
		return 1;
	}
	completed_epochs = calloc(config->slot_count, sizeof(*completed_epochs));
	if (completed_epochs == NULL)
	{
		perror("calloc completed_epochs");
		goto out;
	}

	while (completed < config->iterations)
	{
		bool submitted_any = false;

		if (published < next_submit && next_submit <= config->iterations)
		{
			result = read_remote_u64(dma, remote_base, offsetof(HdvControlBlock, published_epoch), scratch_value);
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async read published_epoch failed: %s\n", doca_error_get_descr(result));
				goto out;
			}
			frontier_read_count++;
			published = *scratch_value;
		}

		while (next_submit <= published && next_submit <= config->iterations && in_flight < config->async_window &&
			   next_submit - consumed_contig <= config->slot_count)
		{
			uint32_t task_index = (uint32_t)((next_submit - 1) % config->async_window);
			HdvAsyncDmaTask *task = &tasks[task_index];
			void *remote_slot =
				(uint8_t *)remote_base + hdv_slot_offset(next_submit, config->slot_count, config->slot_bytes);
			bool force_report = (in_flight + 1 == config->async_window) || (next_submit == published) ||
								(next_submit == config->iterations);

			if (task->in_flight)
			{
				fprintf(stderr, "async task reuse while in flight index=%u epoch=%" PRIu64 "\n", task_index,
						task->epoch);
				goto out;
			}
			result = hdv_async_submit_host_read(task, config, remote_slot, next_submit, force_report);
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async slot read submit failed epoch=%" PRIu64 ": %s\n", next_submit,
						doca_error_get_descr(result));
				goto out;
			}
			next_submit++;
			read_dma_count++;
			in_flight++;
			submitted_any = true;
		}
		if (submitted_any && config->flush_policy != HDV_FLUSH_ALL)
			doca_ctx_flush_tasks(dma->ctx);

		dma->pe_progress_calls++;
		(void)doca_pe_progress(dma->pe);

		for (uint32_t i = 0; i < config->async_window; i++)
		{
			HdvAsyncDmaTask *task = &tasks[i];

			if (!task->in_flight || !task->done)
				continue;
			if (task->result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async slot read failed epoch=%" PRIu64 ": %s\n", task->epoch,
						doca_error_get_descr(task->result));
				goto out;
			}
			{
				char error[256];

				if (validate_slot(config, task->local_slot, task->epoch, error, sizeof(error)) != 0)
				{
					*scratch_value = task->epoch;
					(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_epoch), scratch_value,
										   submit_flags_for(config, true));
					*scratch_value = 3;
					(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_code), scratch_value,
										   submit_flags_for(config, true));
					fprintf(stderr, "HDV_DPU_ASYNC_VALIDATION_FAIL epoch=%" PRIu64 " %s\n", task->epoch, error);
					goto out;
				}
			}
			completed_epochs[(task->epoch - 1) & (uint64_t)(config->slot_count - 1)] = task->epoch;
			task->in_flight = false;
			task->done = false;
			completed++;
			in_flight--;
		}

		while (consumed_contig < config->iterations &&
			   completed_epochs[consumed_contig & (uint64_t)(config->slot_count - 1)] == consumed_contig + 1)
		{
			completed_epochs[consumed_contig & (uint64_t)(config->slot_count - 1)] = 0;
			consumed_contig++;
		}

		if ((consumed_contig == config->iterations && last_consumed_write != consumed_contig) ||
			consumed_contig - last_consumed_write >= config->batch_size)
		{
			*scratch_value = consumed_contig;
			result = write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, consumed_epoch), scratch_value,
									  submit_flags_for(config, true));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async write consumed_epoch failed: %s\n", doca_error_get_descr(result));
				goto out;
			}
			last_consumed_write = consumed_contig;
			consumed_write_count++;
		}

		if ((monotonic_ns() - start) / 1000000000ULL > config->timeout_sec)
		{
			fprintf(stderr,
					"HDV_DPU_ASYNC_TIMEOUT completed=%" PRIu64 " submitted=%" PRIu64 " published=%" PRIu64
					" consumed=%" PRIu64 "\n",
					completed, next_submit - 1, published, consumed_contig);
			goto out;
		}
		if (monotonic_ns() - last_progress_report > 5000000000ULL)
		{
			fprintf(stderr,
					"HDV_DPU_ASYNC_PROGRESS completed=%" PRIu64 " submitted=%" PRIu64 " published=%" PRIu64
					" consumed=%" PRIu64 "\n",
					completed, next_submit - 1, published, consumed_contig);
			last_progress_report = monotonic_ns();
		}
	}

	{
		uint64_t elapsed = monotonic_ns() - start;
		double elapsed_sec = (double)elapsed / 1000000000.0;
		double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

		*scratch_value = read_dma_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_data_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = consumed_write_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_publish_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = frontier_read_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_consumed_dma_read_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = dma->pe_progress_calls;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_pe_progress_calls), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = config->iterations;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, done_epoch), scratch_value,
							   submit_flags_for(config, true));

		printf("HDV_DPU_READ_ASYNC_DONE iterations=%" PRIu64 " read_dma=%" PRIu64 " frontier_reads=%" PRIu64
			   " consumed_writes=%" PRIu64 " window=%u pe_progress=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
			   config->iterations, read_dma_count, frontier_read_count, consumed_write_count, config->async_window,
			   dma->pe_progress_calls, elapsed_sec, elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
	}
	rc = 0;

out:
	free(completed_epochs);
	hdv_async_pool_destroy(tasks, config->async_window);
	return rc;
}

static int run_dpu_sync_event_read(const HdvConfig *config, HdvDmaContext *dma, void *remote_base, void *slot_buffer,
								   uint64_t *scratch_value)
{
	void *sync_desc = NULL;
	size_t sync_desc_len = 0;
	struct doca_sync_event *sync_event = NULL;
	uint64_t published = 0;
	uint64_t expected = 1;
	uint64_t read_dma_count = 0;
	uint64_t consumed_write_count = 0;
	uint64_t wait_count = 0;
	uint64_t start = monotonic_ns();
	doca_error_t result;
	int rc = 1;

	if (read_file_alloc(config->sync_event_path, &sync_desc, &sync_desc_len) != 0)
		return 1;

	result = doca_sync_event_create_from_export(dma->dev, (const uint8_t *)sync_desc, sync_desc_len, &sync_event);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_sync_event_create_from_export failed: %s\n", doca_error_get_descr(result));
		goto out_free_desc;
	}
	result = doca_sync_event_start(sync_event);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "doca_sync_event_start importer failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_sync_event;
	}

	while (expected <= config->iterations)
	{
		result = doca_sync_event_wait_gt(sync_event, published, UINT64_MAX);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "sync_event wait_gt failed: %s\n", doca_error_get_descr(result));
			goto out_stop_sync_event;
		}
		wait_count++;
		result = doca_sync_event_get(sync_event, &published);
		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "sync_event get failed: %s\n", doca_error_get_descr(result));
			goto out_stop_sync_event;
		}
		if (published > config->iterations)
			published = config->iterations;

		while (expected <= published)
		{
			char error[256];

			result =
				hdv_dma_copy(dma, dma->remote_mmap,
							 (uint8_t *)remote_base + hdv_slot_offset(expected, config->slot_count, config->slot_bytes),
							 true, dma->local_mmap, slot_buffer, config->slot_bytes, submit_flags_for(config, false));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "sync_event slot read failed epoch=%" PRIu64 ": %s\n", expected,
						doca_error_get_descr(result));
				goto out_stop_sync_event;
			}
			read_dma_count++;

			if (validate_slot(config, slot_buffer, expected, error, sizeof(error)) != 0)
			{
				*scratch_value = expected;
				(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_epoch), scratch_value,
									   submit_flags_for(config, true));
				*scratch_value = 4;
				(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_code), scratch_value,
									   submit_flags_for(config, true));
				fprintf(stderr, "HDV_DPU_SYNC_EVENT_VALIDATION_FAIL epoch=%" PRIu64 " published=%" PRIu64 " %s\n",
						expected, published, error);
				goto out_stop_sync_event;
			}

			if (expected % config->batch_size == 0 || expected == config->iterations)
			{
				*scratch_value = expected;
				result = write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, consumed_epoch), scratch_value,
										  submit_flags_for(config, true));
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "sync_event consumed write failed: %s\n", doca_error_get_descr(result));
					goto out_stop_sync_event;
				}
				consumed_write_count++;
			}
			expected++;
		}

		if ((monotonic_ns() - start) / 1000000000ULL > config->timeout_sec)
		{
			fprintf(stderr, "HDV_DPU_SYNC_EVENT_TIMEOUT expected=%" PRIu64 " published=%" PRIu64 "\n", expected,
					published);
			goto out_stop_sync_event;
		}
	}

	{
		uint64_t elapsed = monotonic_ns() - start;
		double elapsed_sec = (double)elapsed / 1000000000.0;
		double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

		*scratch_value = read_dma_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_data_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = consumed_write_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_publish_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = wait_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_consumed_dma_read_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = dma->pe_progress_calls;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_pe_progress_calls), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = config->iterations;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, done_epoch), scratch_value,
							   submit_flags_for(config, true));

		printf("HDV_DPU_SYNC_EVENT_DONE iterations=%" PRIu64 " read_dma=%" PRIu64 " waits=%" PRIu64
			   " consumed_writes=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
			   config->iterations, read_dma_count, wait_count, consumed_write_count, elapsed_sec,
			   elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
	}
	rc = 0;

out_stop_sync_event:
	if (sync_event != NULL)
	{
		result = doca_sync_event_stop(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "sync_event stop importer failed: %s\n", doca_error_get_descr(result));
	}
out_destroy_sync_event:
	if (sync_event != NULL)
	{
		result = doca_sync_event_destroy(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "sync_event destroy importer failed: %s\n", doca_error_get_descr(result));
	}
out_free_desc:
	free(sync_desc);
	return rc;
}

static int run_dpu_sync_event_read_async(const HdvConfig *config, HdvDmaContext *dma, void *remote_base,
										 void *local_slots, uint64_t *scratch_value)
{
	void *sync_desc = NULL;
	size_t sync_desc_len = 0;
	struct doca_sync_event *sync_event = NULL;
	HdvAsyncDmaTask *tasks = NULL;
	uint64_t *completed_epochs = NULL;
	uint64_t published = 0;
	uint64_t next_submit = 1;
	uint64_t completed = 0;
	uint64_t consumed_contig = 0;
	uint64_t last_consumed_write = 0;
	uint64_t read_dma_count = 0;
	uint64_t consumed_write_count = 0;
	uint64_t wait_count = 0;
	uint64_t in_flight = 0;
	uint64_t start = monotonic_ns();
	uint64_t last_progress_report = start;
	doca_error_t result;
	int rc = 1;

	if (read_file_alloc(config->sync_event_path, &sync_desc, &sync_desc_len) != 0)
		return 1;
	result = doca_sync_event_create_from_export(dma->dev, (const uint8_t *)sync_desc, sync_desc_len, &sync_event);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "async sync_event create_from_export failed: %s\n", doca_error_get_descr(result));
		goto out_free_desc;
	}
	result = doca_sync_event_start(sync_event);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "async sync_event start importer failed: %s\n", doca_error_get_descr(result));
		goto out_destroy_sync_event;
	}
	result = hdv_async_pool_create(dma, config, remote_base, local_slots, &tasks);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "sync-event async pool create failed: %s\n", doca_error_get_descr(result));
		goto out_stop_sync_event;
	}
	completed_epochs = calloc(config->slot_count, sizeof(*completed_epochs));
	if (completed_epochs == NULL)
	{
		perror("calloc completed_epochs");
		goto out_stop_sync_event;
	}

	while (completed < config->iterations)
	{
		bool submitted_any = false;

		if (next_submit > published && next_submit <= config->iterations && in_flight == 0)
		{
			result = doca_sync_event_wait_gt(sync_event, published, UINT64_MAX);
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async sync_event wait_gt failed: %s\n", doca_error_get_descr(result));
				goto out_stop_sync_event;
			}
			wait_count++;
			result = doca_sync_event_get(sync_event, &published);
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "async sync_event get failed: %s\n", doca_error_get_descr(result));
				goto out_stop_sync_event;
			}
			if (published > config->iterations)
				published = config->iterations;
		}

		while (next_submit <= published && next_submit <= config->iterations && in_flight < config->async_window &&
			   next_submit - consumed_contig <= config->slot_count)
		{
			uint32_t task_index = (uint32_t)((next_submit - 1) % config->async_window);
			HdvAsyncDmaTask *task = &tasks[task_index];
			void *remote_slot =
				(uint8_t *)remote_base + hdv_slot_offset(next_submit, config->slot_count, config->slot_bytes);
			bool force_report = (in_flight + 1 == config->async_window) || (next_submit == published) ||
								(next_submit == config->iterations);

			if (task->in_flight)
			{
				fprintf(stderr, "sync-event async task reuse while in flight index=%u epoch=%" PRIu64 "\n", task_index,
						task->epoch);
				goto out_stop_sync_event;
			}
			result = hdv_async_submit_host_read(task, config, remote_slot, next_submit, force_report);
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "sync-event async slot read submit failed epoch=%" PRIu64 ": %s\n", next_submit,
						doca_error_get_descr(result));
				goto out_stop_sync_event;
			}
			next_submit++;
			read_dma_count++;
			in_flight++;
			submitted_any = true;
		}
		if (submitted_any && config->flush_policy != HDV_FLUSH_ALL)
			doca_ctx_flush_tasks(dma->ctx);

		dma->pe_progress_calls++;
		(void)doca_pe_progress(dma->pe);

		for (uint32_t i = 0; i < config->async_window; i++)
		{
			HdvAsyncDmaTask *task = &tasks[i];

			if (!task->in_flight || !task->done)
				continue;
			if (task->result != DOCA_SUCCESS)
			{
				fprintf(stderr, "sync-event async slot read failed epoch=%" PRIu64 ": %s\n", task->epoch,
						doca_error_get_descr(task->result));
				goto out_stop_sync_event;
			}
			{
				char error[256];

				if (validate_slot(config, task->local_slot, task->epoch, error, sizeof(error)) != 0)
				{
					*scratch_value = task->epoch;
					(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_epoch), scratch_value,
										   submit_flags_for(config, true));
					*scratch_value = 5;
					(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, error_code), scratch_value,
										   submit_flags_for(config, true));
					fprintf(stderr,
							"HDV_DPU_SYNC_EVENT_ASYNC_VALIDATION_FAIL epoch=%" PRIu64 " published=%" PRIu64 " %s\n",
							task->epoch, published, error);
					goto out_stop_sync_event;
				}
			}
			completed_epochs[(task->epoch - 1) & (uint64_t)(config->slot_count - 1)] = task->epoch;
			task->in_flight = false;
			task->done = false;
			completed++;
			in_flight--;
		}

		while (consumed_contig < config->iterations &&
			   completed_epochs[consumed_contig & (uint64_t)(config->slot_count - 1)] == consumed_contig + 1)
		{
			completed_epochs[consumed_contig & (uint64_t)(config->slot_count - 1)] = 0;
			consumed_contig++;
		}

		if ((consumed_contig == config->iterations && last_consumed_write != consumed_contig) ||
			consumed_contig - last_consumed_write >= config->batch_size)
		{
			*scratch_value = consumed_contig;
			result = write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, consumed_epoch), scratch_value,
									  submit_flags_for(config, true));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "sync-event async consumed write failed: %s\n", doca_error_get_descr(result));
				goto out_stop_sync_event;
			}
			last_consumed_write = consumed_contig;
			consumed_write_count++;
		}

		if ((monotonic_ns() - start) / 1000000000ULL > config->timeout_sec)
		{
			fprintf(stderr,
					"HDV_DPU_SYNC_EVENT_ASYNC_TIMEOUT completed=%" PRIu64 " submitted=%" PRIu64 " published=%" PRIu64
					" consumed=%" PRIu64 "\n",
					completed, next_submit - 1, published, consumed_contig);
			goto out_stop_sync_event;
		}
		if (monotonic_ns() - last_progress_report > 5000000000ULL)
		{
			fprintf(stderr,
					"HDV_DPU_SYNC_EVENT_ASYNC_PROGRESS completed=%" PRIu64 " submitted=%" PRIu64 " published=%" PRIu64
					" consumed=%" PRIu64 "\n",
					completed, next_submit - 1, published, consumed_contig);
			last_progress_report = monotonic_ns();
		}
	}

	{
		uint64_t elapsed = monotonic_ns() - start;
		double elapsed_sec = (double)elapsed / 1000000000.0;
		double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

		*scratch_value = read_dma_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_data_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = consumed_write_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_publish_dma_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = wait_count;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_consumed_dma_read_count), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = dma->pe_progress_calls;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, dpu_pe_progress_calls), scratch_value,
							   submit_flags_for(config, true));
		*scratch_value = config->iterations;
		(void)write_remote_u64(dma, remote_base, offsetof(HdvControlBlock, done_epoch), scratch_value,
							   submit_flags_for(config, true));

		printf("HDV_DPU_SYNC_EVENT_ASYNC_DONE iterations=%" PRIu64 " read_dma=%" PRIu64 " waits=%" PRIu64
			   " consumed_writes=%" PRIu64 " window=%u pe_progress=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
			   config->iterations, read_dma_count, wait_count, consumed_write_count, config->async_window,
			   dma->pe_progress_calls, elapsed_sec, elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
	}
	rc = 0;

out_stop_sync_event:
	free(completed_epochs);
	hdv_async_pool_destroy(tasks, config->async_window);
	if (sync_event != NULL)
	{
		result = doca_sync_event_stop(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "async sync_event stop importer failed: %s\n", doca_error_get_descr(result));
	}
out_destroy_sync_event:
	if (sync_event != NULL)
	{
		result = doca_sync_event_destroy(sync_event);
		if (result != DOCA_SUCCESS)
			fprintf(stderr, "async sync_event destroy importer failed: %s\n", doca_error_get_descr(result));
	}
out_free_desc:
	free(sync_desc);
	return rc;
}

static int run_dpu(const HdvConfig *config)
{
	void *export_desc = NULL;
	size_t export_desc_len = 0;
	void *remote_base = NULL;
	size_t remote_len = 0;
	void *local_region = NULL;
	size_t local_region_size = (size_t)config->slot_bytes * config->async_window + HDV_LOCAL_EXTRA_BYTES;
	size_t page_size = (size_t)sysconf(_SC_PAGESIZE);
	void *slot_buffer;
	uint64_t *publish_value;
	uint64_t *consumed_snapshot;
	HdvDmaContext dma;
	uint64_t consumed = 0;
	uint64_t epoch = 1;
	uint64_t data_dma_count = 0;
	uint64_t publish_dma_count = 0;
	uint64_t consumed_read_count = 0;
	uint64_t start_ns;
	uint64_t elapsed_ns;
	doca_error_t result;
	int rc = 1;

	if (read_file_alloc(config->descriptor_path, &export_desc, &export_desc_len) != 0 ||
		read_buffer_info(config->buffer_info_path, &remote_base, &remote_len) != 0)
		goto out_free_desc;
	if (remote_len < hdv_region_size(config->slot_count, config->slot_bytes))
	{
		fprintf(stderr, "remote region too small: %zu\n", remote_len);
		goto out_free_desc;
	}
	if (posix_memalign(&local_region, page_size, local_region_size) != 0)
	{
		perror("posix_memalign");
		goto out_free_desc;
	}
	memset(local_region, 0, local_region_size);
	slot_buffer = local_region;
	publish_value = (uint64_t *)((uint8_t *)local_region + (size_t)config->slot_bytes * config->async_window);
	consumed_snapshot = (uint64_t *)((uint8_t *)local_region + (size_t)config->slot_bytes * config->async_window + 64U);

	result = hdv_dpu_init(config, local_region, local_region_size, export_desc, export_desc_len, &dma);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "DPU DOCA init failed: %s\n", doca_error_get_descr(result));
		goto out_free_local;
	}

	printf("HDV_DPU_START remote=%p bytes=%zu iterations=%" PRIu64 " mode=%u batch=%u\n", remote_base, remote_len,
		   config->iterations, (uint32_t)config->mode, config->batch_size);
	fflush(stdout);

	if (config->mode == HDV_MODE_HOST_WRITE_DPU_READ)
	{
		if (config->async_window > 1)
		{
			rc = run_dpu_host_read_async(config, &dma, remote_base, slot_buffer, publish_value);
			goto out_destroy_dma;
		}

		uint64_t expected = 1;
		uint64_t published = 0;
		uint64_t read_dma_count = 0;
		uint64_t frontier_read_count = 0;
		uint64_t consumed_write_count = 0;
		uint64_t start = monotonic_ns();
		uint64_t elapsed;

		while (expected <= config->iterations)
		{
			char error[256];

			while (published < expected)
			{
				result =
					read_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, published_epoch), consumed_snapshot);
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "read published_epoch failed: %s\n", doca_error_get_descr(result));
					goto out_destroy_dma;
				}
				frontier_read_count++;
				published = *consumed_snapshot;
			}

			result =
				hdv_dma_copy(&dma, dma.remote_mmap,
							 (uint8_t *)remote_base + hdv_slot_offset(expected, config->slot_count, config->slot_bytes),
							 true, dma.local_mmap, slot_buffer, config->slot_bytes, submit_flags_for(config, false));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "slot read DMA failed epoch=%" PRIu64 ": %s\n", expected, doca_error_get_descr(result));
				goto out_destroy_dma;
			}
			read_dma_count++;

			if (validate_slot(config, slot_buffer, expected, error, sizeof(error)) != 0)
			{
				*publish_value = expected;
				(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, error_epoch), publish_value,
									   submit_flags_for(config, true));
				*publish_value = 2;
				(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, error_code), publish_value,
									   submit_flags_for(config, true));
				fprintf(stderr, "HDV_DPU_VALIDATION_FAIL epoch=%" PRIu64 " %s\n", expected, error);
				goto out_destroy_dma;
			}

			*publish_value = expected;
			result = write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, consumed_epoch), publish_value,
									  submit_flags_for(config, true));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "write consumed_epoch failed: %s\n", doca_error_get_descr(result));
				goto out_destroy_dma;
			}
			consumed_write_count++;
			expected++;
		}

		elapsed = monotonic_ns() - start;
		*publish_value = read_dma_count;
		(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_data_dma_count), publish_value,
							   submit_flags_for(config, true));
		*publish_value = consumed_write_count;
		(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_publish_dma_count), publish_value,
							   submit_flags_for(config, true));
		*publish_value = frontier_read_count;
		(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_consumed_dma_read_count), publish_value,
							   submit_flags_for(config, true));
		*publish_value = dma.pe_progress_calls;
		(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_pe_progress_calls), publish_value,
							   submit_flags_for(config, true));
		*publish_value = config->iterations;
		(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, done_epoch), publish_value,
							   submit_flags_for(config, true));

		{
			double elapsed_sec = (double)elapsed / 1000000000.0;
			double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

			printf("HDV_DPU_READ_DONE iterations=%" PRIu64 " read_dma=%" PRIu64 " frontier_reads=%" PRIu64
				   " consumed_writes=%" PRIu64 " pe_progress=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
				   config->iterations, read_dma_count, frontier_read_count, consumed_write_count, dma.pe_progress_calls,
				   elapsed_sec, elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
		}
		rc = 0;
		goto out_destroy_dma;
	}

	if (config->mode == HDV_MODE_SYNC_EVENT_PUBLISH || config->mode == HDV_MODE_SYNC_EVENT_EARLY_PUBLISH)
	{
		rc = run_dpu_sync_event_read(config, &dma, remote_base, slot_buffer, publish_value);
		goto out_destroy_dma;
	}

	if (config->mode == HDV_MODE_SYNC_EVENT_ASYNC_PULL)
	{
		rc = run_dpu_sync_event_read_async(config, &dma, remote_base, slot_buffer, publish_value);
		goto out_destroy_dma;
	}

	start_ns = monotonic_ns();
	while (epoch <= config->iterations)
	{
		uint64_t batch_end = epoch + config->batch_size - 1;

		if (batch_end > config->iterations)
			batch_end = config->iterations;

		while (epoch <= batch_end)
		{
			while (epoch - consumed > config->slot_count)
			{
				result =
					read_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, consumed_epoch), consumed_snapshot);
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "read consumed_epoch failed: %s\n", doca_error_get_descr(result));
					goto out_destroy_dma;
				}
				consumed_read_count++;
				consumed = *consumed_snapshot;
			}

			fill_slot_for_mode(config, slot_buffer, epoch);

			if (config->mode == HDV_MODE_EARLY_PUBLISH)
			{
				*publish_value = epoch;
				result = write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, published_epoch), publish_value,
										  submit_flags_for(config, true));
				if (result != DOCA_SUCCESS)
				{
					fprintf(stderr, "early publish DMA failed: %s\n", doca_error_get_descr(result));
					goto out_destroy_dma;
				}
				publish_dma_count++;
				if (config->early_publish_delay_us != 0)
					usleep(config->early_publish_delay_us);
			}

			result =
				hdv_dma_copy(&dma, dma.local_mmap, slot_buffer, true, dma.remote_mmap,
							 (uint8_t *)remote_base + hdv_slot_offset(epoch, config->slot_count, config->slot_bytes),
							 config->slot_bytes, submit_flags_for(config, false));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "slot DMA failed epoch=%" PRIu64 ": %s\n", epoch, doca_error_get_descr(result));
				goto out_destroy_dma;
			}
			data_dma_count++;
			epoch++;
		}

		if (config->mode == HDV_MODE_WRITE_PUBLISH)
		{
			*publish_value = batch_end;
			result = write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, published_epoch), publish_value,
									  submit_flags_for(config, true));
			if (result != DOCA_SUCCESS)
			{
				fprintf(stderr, "publish DMA failed epoch=%" PRIu64 ": %s\n", batch_end, doca_error_get_descr(result));
				goto out_destroy_dma;
			}
			publish_dma_count++;
		}
	}

	elapsed_ns = monotonic_ns() - start_ns;

	*publish_value = data_dma_count;
	(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_data_dma_count), publish_value,
						   submit_flags_for(config, true));
	*publish_value = publish_dma_count;
	(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_publish_dma_count), publish_value,
						   submit_flags_for(config, true));
	*publish_value = consumed_read_count;
	(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_consumed_dma_read_count), publish_value,
						   submit_flags_for(config, true));
	*publish_value = dma.pe_progress_calls;
	(void)write_remote_u64(&dma, remote_base, offsetof(HdvControlBlock, dpu_pe_progress_calls), publish_value,
						   submit_flags_for(config, true));

	{
		double elapsed_sec = (double)elapsed_ns / 1000000000.0;
		double mib = ((double)config->iterations * (double)config->slot_bytes) / (1024.0 * 1024.0);

		printf("HDV_DPU_DONE iterations=%" PRIu64 " data_dma=%" PRIu64 " publish_dma=%" PRIu64
			   " consumed_dma_reads=%" PRIu64 " pe_progress=%" PRIu64 " elapsed_sec=%.6f mib_s=%.2f\n",
			   config->iterations, data_dma_count, publish_dma_count, consumed_read_count, dma.pe_progress_calls,
			   elapsed_sec, elapsed_sec == 0.0 ? 0.0 : mib / elapsed_sec);
	}
	rc = 0;

out_destroy_dma:
	hdv_dpu_destroy(&dma);
out_free_local:
	free(local_region);
out_free_desc:
	free(export_desc);
	return rc;
}

int main(int argc, char **argv)
{
	HdvConfig config;

	if (!parse_args(argc, argv, &config))
		return 2;

	if (config.role == HDV_ROLE_HOST)
		return run_host(&config);
	return run_dpu(&config);
}
