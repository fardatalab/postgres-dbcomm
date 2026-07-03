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
#include <doca_rdma.h>
#include <doca_types.h>

#include "common.h"

#define HBR_MAGIC 0x48425231444d4155ULL
#define HBR_DEFAULT_BYTES 4096U
#define HBR_DEFAULT_TIMEOUT_SEC 30U
#define HBR_BUF_INVENTORY_SIZE 8U
#define HBR_DEFAULT_ITERATIONS 1ULL
#define HBR_DEFAULT_WINDOW 1U
#define HBR_MAX_WINDOW 256U
#define HBR_MAX_ITERATIONS 10485760ULL
#define HBR_LOG(role, fmt, ...)                                                                                       \
	do                                                                                                                 \
	{                                                                                                                  \
		fprintf(stderr, "%s: " fmt "\n", role, ##__VA_ARGS__);                                                         \
		fflush(stderr);                                                                                                \
	} while (0)

typedef enum HbrRole
{
	HBR_ROLE_NONE = 0,
	HBR_ROLE_HOST,
	HBR_ROLE_DPU,
} HbrRole;

typedef enum HbrMode
{
	HBR_MODE_DIRECT_PCI_RDMA = 0,
	HBR_MODE_DMA_STAGE_RDMA,
} HbrMode;

typedef struct HbrConfig
{
	HbrRole role;
	HbrMode mode;
	const char *pci_addr;
	const char *rdma_pci_addr;
	const char *rdma_ibdev;
	const char *source_pci_desc_path;
	const char *source_info_path;
	const char *target_rdma_desc_path;
	const char *target_conn_desc_path;
	const char *dpu_conn_desc_path;
	const char *dpu_done_path;
	const char *ready_path;
	uint32_t bytes;
	uint32_t window;
	uint32_t timeout_sec;
	uint32_t gid_index;
	uint64_t iterations;
	bool relaxed_ordering;
	bool has_gid_index;
	bool optimize_reports;
	bool flush_report_sentinel;
	uint32_t report_interval;
} HbrConfig;

typedef struct HbrBufferInfo
{
	uint64_t addr;
	uint64_t len;
} HbrBufferInfo;

typedef struct HbrRecordHeader
{
	uint64_t magic;
	uint64_t sequence;
	uint64_t payload_len;
	uint64_t checksum;
} HbrRecordHeader;

typedef struct HbrRdmaState
{
	struct doca_dev *dev;
	struct doca_pe *pe;
	struct doca_rdma *rdma;
	struct doca_ctx *ctx;
	struct doca_rdma_connection *connection;
	const void *local_conn_desc;
	size_t local_conn_desc_len;
	enum doca_ctx_states ctx_state;
	bool run_pe;
	bool connected;
	bool connect_attempted;
	bool task_submitted;
	bool task_done;
	doca_error_t first_error;
} HbrRdmaState;

typedef struct HbrHostState
{
	const HbrConfig *config;
	HbrRdmaState rdma;
	void *source_region;
	void *target_region;
	struct doca_dev *pci_dev;
	struct doca_mmap *source_mmap;
	struct doca_mmap *target_mmap;
	const void *source_pci_desc;
	size_t source_pci_desc_len;
	const void *target_rdma_desc;
	size_t target_rdma_desc_len;
	bool validation_done;
	bool stop_requested;
} HbrHostState;

typedef struct HbrBenchLane
{
	struct doca_buf *direct_src_buf;
	struct doca_buf *stage_dma_dst_buf;
	struct doca_buf *stage_rdma_src_buf;
	struct doca_buf *dst_buf;
	struct doca_dma_task_memcpy *dma_task;
	struct doca_rdma_task_write *rdma_task;
	uint64_t dma_done_op_plus_one;
	bool ready_for_next;
	bool dma_done;
} HbrBenchLane;

typedef struct HbrDpuState
{
	const HbrConfig *config;
	HbrRdmaState rdma;
	struct doca_dma *dma;
	struct doca_pe *dma_pe;
	struct doca_ctx *dma_ctx;
	struct doca_mmap *source_pci_mmap;
	struct doca_mmap *target_rdma_mmap;
	struct doca_mmap *stage_mmap;
	struct doca_buf_inventory *buf_inv;
	struct doca_buf *src_buf;
	struct doca_buf *dst_buf;
	struct doca_rdma_task_write *write_task;
	HbrBenchLane *lanes;
	struct doca_rdma_task_write **rdma_tasks;
	struct doca_dma_task_memcpy **dma_tasks;
	void *stage_region;
	void *source_addr;
	size_t source_len;
	void *target_addr;
	size_t target_len;
	uint64_t submitted;
	uint64_t completed;
	struct timespec start_ts;
	struct timespec end_ts;
	bool target_ready;
	bool benchmark_running;
	bool benchmark_done;
} HbrDpuState;

static uint64_t
hbr_checksum_bytes(const uint8_t *bytes, size_t len)
{
	uint64_t checksum = 1469598103934665603ULL;

	for (size_t i = 0; i < len; i++)
	{
		checksum ^= bytes[i];
		checksum *= 1099511628211ULL;
	}
	return checksum;
}

static void
hbr_fill_record(void *region, size_t len)
{
	HbrRecordHeader *header = (HbrRecordHeader *)region;
	uint8_t *payload = (uint8_t *)region + sizeof(*header);
	size_t payload_len = len - sizeof(*header);

	header->magic = HBR_MAGIC;
	header->sequence = 1;
	header->payload_len = payload_len;
	for (size_t i = 0; i < payload_len; i++)
		payload[i] = (uint8_t)((i * 131U + 17U) & 0xffU);
	header->checksum = hbr_checksum_bytes(payload, payload_len);
}

static bool
hbr_validate_record(const void *region, size_t len, char *error_buf, size_t error_buf_len)
{
	const HbrRecordHeader *header = (const HbrRecordHeader *)region;
	const uint8_t *payload = (const uint8_t *)region + sizeof(*header);
	uint64_t checksum;

	if (len < sizeof(*header))
	{
		snprintf(error_buf, error_buf_len, "record too small: %zu", len);
		return false;
	}
	if (header->magic != HBR_MAGIC)
	{
		snprintf(error_buf, error_buf_len, "magic mismatch: got 0x%016" PRIx64, header->magic);
		return false;
	}
	if (header->sequence != 1)
	{
		snprintf(error_buf, error_buf_len, "sequence mismatch: got %" PRIu64, header->sequence);
		return false;
	}
	if (header->payload_len != len - sizeof(*header))
	{
		snprintf(error_buf, error_buf_len, "payload length mismatch: got %" PRIu64, header->payload_len);
		return false;
	}
	checksum = hbr_checksum_bytes(payload, len - sizeof(*header));
	if (checksum != header->checksum)
	{
		snprintf(error_buf, error_buf_len, "checksum mismatch: got 0x%016" PRIx64 " expected 0x%016" PRIx64,
				 header->checksum, checksum);
		return false;
	}
	return true;
}

static void
hbr_log_record_prefix(const char *role, const char *label, const void *region, size_t len)
{
	const uint8_t *bytes = (const uint8_t *)region;
	size_t prefix_len = len < 32 ? len : 32;

	fprintf(stderr, "%s: %s first_%zu_bytes=", role, label, prefix_len);
	for (size_t i = 0; i < prefix_len; i++)
		fprintf(stderr, "%02x", bytes[i]);
	fprintf(stderr, "\n");
	fflush(stderr);
}

static bool
hbr_parse_u32(const char *value, uint32_t *out)
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

static bool
hbr_parse_u64(const char *value, uint64_t *out)
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

static const char *
hbr_mode_name(HbrMode mode)
{
	switch (mode)
	{
		case HBR_MODE_DIRECT_PCI_RDMA:
			return "direct-pci-rdma";
		case HBR_MODE_DMA_STAGE_RDMA:
			return "dma-stage-rdma";
	}
	return "unknown";
}

static void
hbr_usage(const char *prog)
{
	fprintf(stderr,
			"usage: %s --role=host|dpu --source-pci-desc=PATH --source-info=PATH --target-rdma-desc=PATH "
			"--target-conn-desc=PATH --dpu-conn-desc=PATH [options]\n"
			"options:\n"
			"  --pci-addr=ADDR          host PCI export device or DPU PCI import/RDMA device\n"
			"  --rdma-pci-addr=ADDR     optional separate RDMA DOCA device PCI address\n"
			"  --rdma-ibdev=NAME        optional RDMA DOCA device IB name, for host-side convenience\n"
			"  --gid-index=N            optional RoCE GID index for DOCA RDMA\n"
			"  --mode=direct-pci-rdma|dma-stage-rdma\n"
			"  --bytes=N                transfer size, default %u\n"
			"  --iterations=N           benchmark operations, default %" PRIu64 "\n"
			"  --window=N               max in-flight operations, default %u\n"
			"  --ready=PATH             optional host-ready file; host writes after connect, DPU waits before timing\n"
			"  --dpu-done=PATH          optional DPU completion file for benchmark host wait\n"
			"  --timeout-sec=N          descriptor/progress timeout, default %u\n"
			"  --relaxed-ordering       add PCI relaxed-ordering to the host source mmap\n"
			"  --optimize-reports       submit non-sentinel DOCA tasks with OPTIMIZE_REPORTS\n"
			"  --flush-report-sentinel  submit report-boundary tasks with FLUSH\n"
			"  --report-interval=N      non-optimized report sentinel interval, default 1\n",
			prog, HBR_DEFAULT_BYTES, (uint64_t)HBR_DEFAULT_ITERATIONS, HBR_DEFAULT_WINDOW, HBR_DEFAULT_TIMEOUT_SEC);
}

static bool
hbr_parse_args(int argc, char **argv, HbrConfig *config)
{
	memset(config, 0, sizeof(*config));
	config->mode = HBR_MODE_DIRECT_PCI_RDMA;
	config->bytes = HBR_DEFAULT_BYTES;
	config->iterations = HBR_DEFAULT_ITERATIONS;
	config->window = HBR_DEFAULT_WINDOW;
	config->timeout_sec = HBR_DEFAULT_TIMEOUT_SEC;
	config->report_interval = 1;

	for (int i = 1; i < argc; i++)
	{
		const char *arg = argv[i];
		const char *value = strchr(arg, '=');
		size_t name_len;

		if (strcmp(arg, "--relaxed-ordering") == 0)
		{
			config->relaxed_ordering = true;
			continue;
		}
		if (strcmp(arg, "--optimize-reports") == 0)
		{
			config->optimize_reports = true;
			continue;
		}
		if (strcmp(arg, "--flush-report-sentinel") == 0)
		{
			config->flush_report_sentinel = true;
			continue;
		}
		if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0)
		{
			hbr_usage(argv[0]);
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
				config->role = HBR_ROLE_HOST;
			else if (strcmp(value, "dpu") == 0)
				config->role = HBR_ROLE_DPU;
			else
			{
				fprintf(stderr, "invalid role: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--pci-addr") && strncmp(arg, "--pci-addr", name_len) == 0)
			config->pci_addr = value;
		else if (name_len == strlen("--rdma-pci-addr") && strncmp(arg, "--rdma-pci-addr", name_len) == 0)
			config->rdma_pci_addr = value;
		else if (name_len == strlen("--rdma-ibdev") && strncmp(arg, "--rdma-ibdev", name_len) == 0)
			config->rdma_ibdev = value;
		else if (name_len == strlen("--gid-index") && strncmp(arg, "--gid-index", name_len) == 0)
		{
			if (!hbr_parse_u32(value, &config->gid_index))
			{
				fprintf(stderr, "invalid gid index: %s\n", value);
				return false;
			}
			config->has_gid_index = true;
		}
		else if (name_len == strlen("--mode") && strncmp(arg, "--mode", name_len) == 0)
		{
			if (strcmp(value, "direct-pci-rdma") == 0)
				config->mode = HBR_MODE_DIRECT_PCI_RDMA;
			else if (strcmp(value, "dma-stage-rdma") == 0)
				config->mode = HBR_MODE_DMA_STAGE_RDMA;
			else
			{
				fprintf(stderr, "invalid mode: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--source-pci-desc") && strncmp(arg, "--source-pci-desc", name_len) == 0)
			config->source_pci_desc_path = value;
		else if (name_len == strlen("--source-info") && strncmp(arg, "--source-info", name_len) == 0)
			config->source_info_path = value;
		else if (name_len == strlen("--target-rdma-desc") && strncmp(arg, "--target-rdma-desc", name_len) == 0)
			config->target_rdma_desc_path = value;
		else if (name_len == strlen("--target-conn-desc") && strncmp(arg, "--target-conn-desc", name_len) == 0)
			config->target_conn_desc_path = value;
		else if (name_len == strlen("--dpu-conn-desc") && strncmp(arg, "--dpu-conn-desc", name_len) == 0)
			config->dpu_conn_desc_path = value;
		else if (name_len == strlen("--dpu-done") && strncmp(arg, "--dpu-done", name_len) == 0)
			config->dpu_done_path = value;
		else if (name_len == strlen("--ready") && strncmp(arg, "--ready", name_len) == 0)
			config->ready_path = value;
		else if (name_len == strlen("--bytes") && strncmp(arg, "--bytes", name_len) == 0)
		{
			if (!hbr_parse_u32(value, &config->bytes))
			{
				fprintf(stderr, "invalid bytes: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--iterations") && strncmp(arg, "--iterations", name_len) == 0)
		{
			if (!hbr_parse_u64(value, &config->iterations) || config->iterations == 0 ||
				config->iterations > HBR_MAX_ITERATIONS)
			{
				fprintf(stderr, "invalid iterations: %s, expected 1..%" PRIu64 "\n", value,
						(uint64_t)HBR_MAX_ITERATIONS);
				return false;
			}
		}
		else if (name_len == strlen("--window") && strncmp(arg, "--window", name_len) == 0)
		{
			if (!hbr_parse_u32(value, &config->window) || config->window == 0 || config->window > HBR_MAX_WINDOW)
			{
				fprintf(stderr, "invalid window: %s, expected 1..%u\n", value, HBR_MAX_WINDOW);
				return false;
			}
		}
		else if (name_len == strlen("--timeout-sec") && strncmp(arg, "--timeout-sec", name_len) == 0)
		{
			if (!hbr_parse_u32(value, &config->timeout_sec))
			{
				fprintf(stderr, "invalid timeout: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--report-interval") && strncmp(arg, "--report-interval", name_len) == 0)
		{
			if (!hbr_parse_u32(value, &config->report_interval) || config->report_interval == 0)
			{
				fprintf(stderr, "invalid report interval: %s\n", value);
				return false;
			}
		}
		else
		{
			fprintf(stderr, "unknown argument: %.*s\n", (int)name_len, arg);
			return false;
		}
	}

	if (config->role == HBR_ROLE_NONE || config->source_pci_desc_path == NULL || config->source_info_path == NULL ||
		config->target_rdma_desc_path == NULL || config->target_conn_desc_path == NULL ||
		config->dpu_conn_desc_path == NULL)
	{
		hbr_usage(argv[0]);
		return false;
	}
	if (config->bytes < sizeof(HbrRecordHeader))
	{
		fprintf(stderr, "--bytes must be at least %zu\n", sizeof(HbrRecordHeader));
		return false;
	}
	if (config->window > config->iterations)
		config->window = (uint32_t)config->iterations;
	if (config->optimize_reports && config->report_interval > config->window && config->iterations > config->window)
	{
		fprintf(stderr, "--report-interval must be <= --window when optimized callbacks are used\n");
		return false;
	}
	return true;
}

static int
hbr_write_file_exact(const char *path, const void *data, size_t len)
{
	FILE *file = fopen(path, "wb");

	if (file == NULL)
	{
		fprintf(stderr, "open for write failed for %s: %s\n", path, strerror(errno));
		return -1;
	}
	if (fwrite(data, 1, len, file) != len)
	{
		fprintf(stderr, "write failed for %s: %s\n", path, strerror(errno));
		fclose(file);
		return -1;
	}
	if (fclose(file) != 0)
	{
		fprintf(stderr, "close failed for %s: %s\n", path, strerror(errno));
		return -1;
	}
	return 0;
}

static int
hbr_read_file_alloc(const char *path, void **data, size_t *len)
{
	FILE *file = fopen(path, "rb");
	long file_len;
	void *buffer;

	if (file == NULL)
		return -1;
	if (fseek(file, 0, SEEK_END) != 0)
	{
		fclose(file);
		return -1;
	}
	file_len = ftell(file);
	if (file_len <= 0)
	{
		fclose(file);
		return -1;
	}
	rewind(file);

	buffer = malloc((size_t)file_len);
	if (buffer == NULL)
	{
		fclose(file);
		return -1;
	}
	if (fread(buffer, 1, (size_t)file_len, file) != (size_t)file_len)
	{
		free(buffer);
		fclose(file);
		return -1;
	}
	fclose(file);
	*data = buffer;
	*len = (size_t)file_len;
	return 0;
}

static int
hbr_wait_read_file_alloc(const char *path, uint32_t timeout_sec, void **data, size_t *len)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (hbr_read_file_alloc(path, data, len) == 0)
			return 0;
		usleep(1000);
	}
	fprintf(stderr, "timed out waiting for %s\n", path);
	return -1;
}

static int
hbr_write_buffer_info(const char *path, void *addr, size_t len)
{
	HbrBufferInfo info;

	info.addr = (uint64_t)(uintptr_t)addr;
	info.len = (uint64_t)len;
	return hbr_write_file_exact(path, &info, sizeof(info));
}

static int
hbr_read_buffer_info(const char *path, uint32_t timeout_sec, void **addr, size_t *len)
{
	HbrBufferInfo info;
	void *data = NULL;
	size_t data_len = 0;

	if (hbr_wait_read_file_alloc(path, timeout_sec, &data, &data_len) != 0)
		return -1;
	if (data_len != sizeof(info))
	{
		fprintf(stderr, "unexpected buffer info size in %s: %zu\n", path, data_len);
		free(data);
		return -1;
	}
	memcpy(&info, data, sizeof(info));
	free(data);
	*addr = (void *)(uintptr_t)info.addr;
	*len = (size_t)info.len;
	return 0;
}

static doca_error_t
hbr_rdma_write_supported(struct doca_devinfo *devinfo)
{
	return doca_rdma_cap_task_write_is_supported(devinfo);
}

static doca_error_t
hbr_open_rdma_dev(const HbrConfig *config, struct doca_dev **dev)
{
	if (config->rdma_pci_addr != NULL)
		return open_doca_device_with_pci(config->rdma_pci_addr, hbr_rdma_write_supported, dev);
	if (config->rdma_ibdev != NULL)
		return open_doca_device_with_ibdev_name((const uint8_t *)config->rdma_ibdev, strlen(config->rdma_ibdev),
												hbr_rdma_write_supported, dev);
	if (config->pci_addr != NULL)
		return open_doca_device_with_pci(config->pci_addr, hbr_rdma_write_supported, dev);
	return open_doca_device_with_capabilities(hbr_rdma_write_supported, dev);
}

static doca_error_t
hbr_create_local_mmap(struct doca_mmap **mmap, struct doca_dev *dev, void *addr, size_t len, uint32_t permissions)
{
	doca_error_t result;

	result = doca_mmap_create(mmap);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_set_permissions(*mmap, permissions);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_set_memrange(*mmap, addr, len);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_mmap_add_dev(*mmap, dev);
	if (result != DOCA_SUCCESS)
		return result;
	return doca_mmap_start(*mmap);
}

static double
hbr_timespec_diff_sec(const struct timespec *start, const struct timespec *end)
{
	return (double)(end->tv_sec - start->tv_sec) + (double)(end->tv_nsec - start->tv_nsec) / 1000000000.0;
}

static void hbr_dpu_finish_benchmark(HbrDpuState *state);
static doca_error_t hbr_submit_direct_rdma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index);
static doca_error_t hbr_submit_stage_dma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index);
static doca_error_t hbr_submit_stage_rdma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index);

static bool
hbr_is_report_sentinel(const HbrConfig *config, uint64_t op_index)
{
	return ((op_index + 1U) % config->report_interval) == 0 || op_index + 1U == config->iterations;
}

static uint32_t
hbr_submit_flags(const HbrConfig *config, uint64_t op_index)
{
	uint32_t flags = 0;

	if (!hbr_is_report_sentinel(config, op_index))
	{
		if (config->optimize_reports)
			flags |= DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS;
	}
	else if (config->flush_report_sentinel)
		flags |= DOCA_TASK_SUBMIT_FLAG_FLUSH;
	return flags;
}

static doca_error_t
hbr_submit_doca_task(const HbrConfig *config, struct doca_task *task, uint64_t op_index)
{
	uint32_t flags = hbr_submit_flags(config, op_index);

	if (flags != 0)
		return doca_task_submit_ex(task, flags);
	return doca_task_submit(task);
}

static void
hbr_record_submit_failure(HbrDpuState *state, const char *label, uint32_t lane_index, uint64_t op_index,
						  doca_error_t result)
{
	if (state->rdma.first_error == DOCA_SUCCESS)
		state->rdma.first_error = result;
	fprintf(stderr, "%s submit failed on lane %u op %" PRIu64 ": %s\n", label, lane_index, op_index,
			doca_error_get_descr(result));
	hbr_dpu_finish_benchmark(state);
}

static void
hbr_rdma_write_complete_cb(struct doca_rdma_task_write *task, union doca_data task_user_data,
						   union doca_data ctx_user_data)
{
	HbrDpuState *state = (HbrDpuState *)ctx_user_data.ptr;
	uint64_t op_index = task_user_data.u64;
	uint32_t lane_index = (uint32_t)(op_index % state->config->window);

	if (state->benchmark_running)
	{
		doca_error_t result;

		state->completed++;
		if (state->completed == state->config->iterations)
		{
			hbr_dpu_finish_benchmark(state);
			return;
		}
		/*
		 * The DOCA RDMA header explicitly permits resubmitting from the
		 * completion callback. This benchmark uses that shape to measure the
		 * reusable task/buffer fast path without a scheduler scan in the loop.
		 */
		if (state->submitted < state->config->iterations)
		{
			uint64_t next_op = state->submitted++;

			if (state->config->mode == HBR_MODE_DIRECT_PCI_RDMA)
				result = hbr_submit_direct_rdma(state, lane_index, next_op);
			else
				result = hbr_submit_stage_dma(state, lane_index, next_op);
			if (result != DOCA_SUCCESS)
				hbr_record_submit_failure(state,
										  state->config->mode == HBR_MODE_DIRECT_PCI_RDMA ? "direct RDMA" :
																							 "DMA stage",
										  lane_index, next_op, result);
		}
		return;
	}
	state->rdma.task_done = true;
	state->rdma.first_error = DOCA_SUCCESS;
	doca_task_free(doca_rdma_task_write_as_task(task));
	state->write_task = NULL;
	(void)doca_ctx_stop(state->rdma.ctx);
}

static void
hbr_rdma_write_error_cb(struct doca_rdma_task_write *task, union doca_data task_user_data, union doca_data ctx_user_data)
{
	HbrDpuState *state = (HbrDpuState *)ctx_user_data.ptr;

	(void)task_user_data;
	if (state->benchmark_running)
	{
		state->rdma.first_error = doca_task_get_status(doca_rdma_task_write_as_task(task));
		fprintf(stderr, "RDMA write task failed: %s\n", doca_error_get_descr(state->rdma.first_error));
		hbr_dpu_finish_benchmark(state);
		return;
	}
	state->rdma.task_done = true;
	state->rdma.first_error = doca_task_get_status(doca_rdma_task_write_as_task(task));
	fprintf(stderr, "RDMA write task failed: %s\n", doca_error_get_descr(state->rdma.first_error));
	doca_task_free(doca_rdma_task_write_as_task(task));
	state->write_task = NULL;
	(void)doca_ctx_stop(state->rdma.ctx);
}

static void
hbr_dma_memcpy_complete_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data,
						   union doca_data ctx_user_data)
{
	HbrDpuState *state = (HbrDpuState *)ctx_user_data.ptr;
	uint64_t op_index = task_user_data.u64;
	uint32_t lane_index = (uint32_t)(op_index % state->config->window);

	(void)task;
	state->lanes[lane_index].dma_done_op_plus_one = op_index + 1U;
	if (state->benchmark_running)
	{
		doca_error_t result = hbr_submit_stage_rdma(state, lane_index, op_index);

		if (result != DOCA_SUCCESS)
			hbr_record_submit_failure(state, "staged RDMA", lane_index, op_index, result);
	}
}

static void
hbr_dma_memcpy_error_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data, union doca_data ctx_user_data)
{
	HbrDpuState *state = (HbrDpuState *)ctx_user_data.ptr;

	(void)task_user_data;
	state->rdma.first_error = doca_task_get_status(doca_dma_task_memcpy_as_task(task));
	fprintf(stderr, "DMA stage task failed: %s\n", doca_error_get_descr(state->rdma.first_error));
	hbr_dpu_finish_benchmark(state);
}

static void
hbr_dpu_finish_benchmark(HbrDpuState *state)
{
	double seconds;
	double mib_per_sec;
	double ops_per_sec;
	uint64_t total_bytes;

	if (state->benchmark_done)
		return;

	clock_gettime(CLOCK_MONOTONIC, &state->end_ts);
	state->benchmark_running = false;
	state->benchmark_done = true;
	state->rdma.task_done = true;

	seconds = hbr_timespec_diff_sec(&state->start_ts, &state->end_ts);
	if (seconds <= 0.0)
		seconds = 1e-9;
	total_bytes = state->completed * (uint64_t)state->config->bytes;
	mib_per_sec = ((double)total_bytes / (1024.0 * 1024.0)) / seconds;
	ops_per_sec = (double)state->completed / seconds;

	printf("HBR_DPU_RESULT mode=%s bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
		   " seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
		   hbr_mode_name(state->config->mode), state->config->bytes, state->config->iterations, state->config->window,
		   state->completed, seconds, ops_per_sec, mib_per_sec);
	if (state->config->dpu_done_path != NULL)
	{
		char done_buf[256];
		int done_len = snprintf(done_buf, sizeof(done_buf),
								"mode=%s bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
								" seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
								hbr_mode_name(state->config->mode), state->config->bytes, state->config->iterations,
								state->config->window, state->completed, seconds, ops_per_sec, mib_per_sec);

		if (done_len > 0)
			(void)hbr_write_file_exact(state->config->dpu_done_path, done_buf, (size_t)done_len);
	}
	(void)doca_ctx_stop(state->rdma.ctx);
	if (state->dma_ctx != NULL)
		(void)doca_ctx_stop(state->dma_ctx);
}

static doca_error_t
hbr_submit_direct_rdma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index)
{
	union doca_data task_user_data = {0};
	const HbrConfig *config = state->config;
	HbrBenchLane *lane = &state->lanes[lane_index];
	uint8_t *target_ptr = (uint8_t *)state->target_addr + ((size_t)lane_index * (size_t)config->bytes);
	struct doca_task *task;
	doca_error_t result;

	task_user_data.u64 = op_index;
	result = doca_buf_inventory_buf_reuse_by_data(lane->direct_src_buf, state->source_addr, state->source_len);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_buf_reuse_by_addr(lane->dst_buf, target_ptr, config->bytes);
	if (result != DOCA_SUCCESS)
		return result;
	doca_rdma_task_write_set_src_buf(lane->rdma_task, lane->direct_src_buf);
	doca_rdma_task_write_set_dst_buf(lane->rdma_task, lane->dst_buf);
	doca_rdma_task_write_set_rdma_connection(lane->rdma_task, state->rdma.connection);
	task = doca_rdma_task_write_as_task(lane->rdma_task);
	doca_task_set_user_data(task, task_user_data);
	result = hbr_submit_doca_task(config, task, op_index);
	if (result != DOCA_SUCCESS)
		fprintf(stderr, "direct RDMA submit failed on lane %u op %" PRIu64 ": %s\n", lane_index, op_index,
				doca_error_get_descr(result));
	return result;
}

static doca_error_t
hbr_submit_stage_dma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index)
{
	union doca_data task_user_data = {0};
	const HbrConfig *config = state->config;
	HbrBenchLane *lane = &state->lanes[lane_index];
	uint8_t *stage_ptr = (uint8_t *)state->stage_region + ((size_t)lane_index * (size_t)config->bytes);
	struct doca_task *task;
	doca_error_t result;

	task_user_data.u64 = op_index;
	result = doca_buf_inventory_buf_reuse_by_data(lane->direct_src_buf, state->source_addr, state->source_len);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_buf_reuse_by_addr(lane->stage_dma_dst_buf, stage_ptr, config->bytes);
	if (result != DOCA_SUCCESS)
		return result;
	doca_dma_task_memcpy_set_src(lane->dma_task, lane->direct_src_buf);
	doca_dma_task_memcpy_set_dst(lane->dma_task, lane->stage_dma_dst_buf);
	task = doca_dma_task_memcpy_as_task(lane->dma_task);
	doca_task_set_user_data(task, task_user_data);
	result = hbr_submit_doca_task(config, task, op_index);
	if (result != DOCA_SUCCESS)
		fprintf(stderr, "DMA stage submit failed on lane %u op %" PRIu64 ": %s\n", lane_index, op_index,
				doca_error_get_descr(result));
	return result;
}

static doca_error_t
hbr_submit_stage_rdma(HbrDpuState *state, uint32_t lane_index, uint64_t op_index)
{
	union doca_data task_user_data = {0};
	const HbrConfig *config = state->config;
	HbrBenchLane *lane = &state->lanes[lane_index];
	uint8_t *stage_ptr = (uint8_t *)state->stage_region + ((size_t)lane_index * (size_t)config->bytes);
	uint8_t *target_ptr = (uint8_t *)state->target_addr + ((size_t)lane_index * (size_t)config->bytes);
	struct doca_task *task = doca_rdma_task_write_as_task(lane->rdma_task);
	doca_error_t result;

	task_user_data.u64 = op_index;
	result = doca_buf_inventory_buf_reuse_by_data(lane->stage_rdma_src_buf, stage_ptr, config->bytes);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_buf_reuse_by_addr(lane->dst_buf, target_ptr, config->bytes);
	if (result != DOCA_SUCCESS)
		return result;
	doca_rdma_task_write_set_src_buf(lane->rdma_task, lane->stage_rdma_src_buf);
	doca_rdma_task_write_set_dst_buf(lane->rdma_task, lane->dst_buf);
	doca_rdma_task_write_set_rdma_connection(lane->rdma_task, state->rdma.connection);
	doca_task_set_user_data(task, task_user_data);
	result = hbr_submit_doca_task(config, task, op_index);
	if (result != DOCA_SUCCESS)
		fprintf(stderr, "staged RDMA submit failed on lane %u: %s\n", lane_index, doca_error_get_descr(result));
	return result;
}

static doca_error_t
hbr_drive_benchmark_submissions(HbrDpuState *state)
{
	(void)state;
	return DOCA_SUCCESS;
}

static doca_error_t
hbr_start_dma(HbrDpuState *state)
{
	union doca_data user_data = {0};
	doca_error_t result;

	result = doca_pe_create(&state->dma_pe);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_dma_create(state->rdma.dev, &state->dma);
	if (result != DOCA_SUCCESS)
		return result;
	state->dma_ctx = doca_dma_as_ctx(state->dma);
	if (state->dma_ctx == NULL)
		return DOCA_ERROR_UNEXPECTED;
	result = doca_dma_task_memcpy_set_conf(state->dma, hbr_dma_memcpy_complete_cb, hbr_dma_memcpy_error_cb,
										   state->config->window);
	if (result != DOCA_SUCCESS)
		return result;
	user_data.ptr = state;
	result = doca_ctx_set_user_data(state->dma_ctx, user_data);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_pe_connect_ctx(state->dma_pe, state->dma_ctx);
	if (result != DOCA_SUCCESS)
		return result;
	return doca_ctx_start(state->dma_ctx);
}

static doca_error_t
hbr_prepare_benchmark_resources(HbrDpuState *state)
{
	const HbrConfig *config = state->config;
	size_t stage_bytes = (size_t)config->window * (size_t)config->bytes;
	doca_error_t result;

	if (state->target_len < stage_bytes)
	{
		fprintf(stderr, "target RDMA mmap too small: got %zu expected at least %zu\n", state->target_len, stage_bytes);
		return DOCA_ERROR_INVALID_VALUE;
	}

	state->lanes = calloc(config->window, sizeof(*state->lanes));
	if (state->lanes == NULL)
		return DOCA_ERROR_NO_MEMORY;
	state->rdma_tasks = calloc((size_t)config->window, sizeof(*state->rdma_tasks));
	if (state->rdma_tasks == NULL)
		return DOCA_ERROR_NO_MEMORY;
	if (config->mode == HBR_MODE_DMA_STAGE_RDMA)
	{
		state->dma_tasks = calloc((size_t)config->window, sizeof(*state->dma_tasks));
		if (state->dma_tasks == NULL)
			return DOCA_ERROR_NO_MEMORY;
	}

	if (config->mode == HBR_MODE_DMA_STAGE_RDMA)
	{
		if (posix_memalign(&state->stage_region, 4096, stage_bytes) != 0)
			return DOCA_ERROR_NO_MEMORY;
		memset(state->stage_region, 0, stage_bytes);
		result = hbr_create_local_mmap(&state->stage_mmap, state->rdma.dev, state->stage_region, stage_bytes,
									   DOCA_ACCESS_FLAG_LOCAL_READ_WRITE);
		if (result != DOCA_SUCCESS)
			return result;
		result = hbr_start_dma(state);
		if (result != DOCA_SUCCESS)
			return result;
	}

	for (uint32_t lane_index = 0; lane_index < config->window; lane_index++)
	{
		HbrBenchLane *lane = &state->lanes[lane_index];
		uint8_t *target_ptr = (uint8_t *)state->target_addr + ((size_t)lane_index * (size_t)config->bytes);
		struct doca_buf *direct_src_buf = NULL;
		struct doca_buf *dst_buf = NULL;
		union doca_data task_user_data = {0};

		task_user_data.u64 = lane_index;
		result = doca_buf_inventory_buf_get_by_data(state->buf_inv, state->source_pci_mmap, state->source_addr,
													state->source_len, &direct_src_buf);
		if (result != DOCA_SUCCESS)
			return result;
		result = doca_buf_inventory_buf_get_by_addr(state->buf_inv, state->target_rdma_mmap, target_ptr, config->bytes,
													&dst_buf);
		if (result != DOCA_SUCCESS)
			return result;
		if (config->mode == HBR_MODE_DMA_STAGE_RDMA)
		{
			uint8_t *stage_ptr = (uint8_t *)state->stage_region + ((size_t)lane_index * (size_t)config->bytes);
			struct doca_buf *stage_dma_dst_buf = NULL;
			struct doca_buf *stage_rdma_src_buf = NULL;

			result = doca_buf_inventory_buf_get_by_addr(state->buf_inv, state->stage_mmap, stage_ptr, config->bytes,
														&stage_dma_dst_buf);
			if (result != DOCA_SUCCESS)
				return result;
			result = doca_buf_inventory_buf_get_by_data(state->buf_inv, state->stage_mmap, stage_ptr, config->bytes,
														&stage_rdma_src_buf);
			if (result != DOCA_SUCCESS)
				return result;
			result = doca_dma_task_memcpy_alloc_init(state->dma, direct_src_buf, stage_dma_dst_buf, task_user_data,
													 &lane->dma_task);
			if (result != DOCA_SUCCESS)
				return result;
			result = doca_rdma_task_write_allocate_init(state->rdma.rdma, state->rdma.connection,
														stage_rdma_src_buf, dst_buf, task_user_data, &lane->rdma_task);
			lane->stage_dma_dst_buf = stage_dma_dst_buf;
			lane->stage_rdma_src_buf = stage_rdma_src_buf;
			state->dma_tasks[lane_index] = lane->dma_task;
		}
		else
			result = doca_rdma_task_write_allocate_init(state->rdma.rdma, state->rdma.connection, direct_src_buf,
														dst_buf, task_user_data, &lane->rdma_task);
		if (result != DOCA_SUCCESS)
			return result;
		lane->direct_src_buf = direct_src_buf;
		lane->dst_buf = dst_buf;
		state->rdma_tasks[lane_index] = lane->rdma_task;
	}
	return DOCA_SUCCESS;
}

static doca_error_t
hbr_run_benchmark(HbrDpuState *state)
{
	uint32_t initial = state->config->window;
	doca_error_t result;

	if (initial > state->config->iterations)
		initial = (uint32_t)state->config->iterations;
	clock_gettime(CLOCK_MONOTONIC, &state->start_ts);
	state->benchmark_running = true;
	for (uint32_t i = 0; i < initial; i++)
	{
		uint64_t op_index = state->submitted++;

		if (state->config->mode == HBR_MODE_DIRECT_PCI_RDMA)
			result = hbr_submit_direct_rdma(state, i, op_index);
		else
			result = hbr_submit_stage_dma(state, i, op_index);
		if (result != DOCA_SUCCESS)
			return result;
	}
	return DOCA_SUCCESS;
}

static doca_error_t
hbr_start_rdma(HbrRdmaState *rdma, const HbrConfig *config, void *owner,
			   doca_ctx_state_changed_callback_t state_cb, bool configure_write_task, uint32_t rdma_permissions)
{
	union doca_data user_data = {0};
	doca_error_t result;

	if (rdma->dev == NULL)
	{
		result = hbr_open_rdma_dev(config, &rdma->dev);
		if (result != DOCA_SUCCESS)
			return result;
	}
	result = doca_pe_create(&rdma->pe);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_rdma_create(rdma->dev, &rdma->rdma);
	if (result != DOCA_SUCCESS)
		return result;
	rdma->ctx = doca_rdma_as_ctx(rdma->rdma);
	if (rdma->ctx == NULL)
		return DOCA_ERROR_UNEXPECTED;
	result = doca_rdma_set_permissions(rdma->rdma, rdma_permissions);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_rdma_set_max_num_connections(rdma->rdma, 1);
	if (result != DOCA_SUCCESS)
		return result;
	if (config->has_gid_index)
	{
		result = doca_rdma_set_gid_index(rdma->rdma, config->gid_index);
		if (result != DOCA_SUCCESS)
			return result;
		result = doca_rdma_set_grh_enabled(rdma->rdma, 1);
		if (result != DOCA_SUCCESS)
			return result;
	}
	result = doca_pe_connect_ctx(rdma->pe, rdma->ctx);
	if (result != DOCA_SUCCESS)
		return result;
	if (configure_write_task)
	{
		result = doca_rdma_task_write_set_conf(rdma->rdma, hbr_rdma_write_complete_cb, hbr_rdma_write_error_cb,
											   config->window);
		if (result != DOCA_SUCCESS)
			return result;
	}
	result = doca_ctx_set_state_changed_cb(rdma->ctx, state_cb);
	if (result != DOCA_SUCCESS)
		return result;
	user_data.ptr = owner;
	result = doca_ctx_set_user_data(rdma->ctx, user_data);
	if (result != DOCA_SUCCESS)
		return result;
	rdma->run_pe = true;
	rdma->first_error = DOCA_SUCCESS;
	return doca_ctx_start(rdma->ctx);
}

static void
hbr_host_state_cb(const union doca_data user_data, struct doca_ctx *ctx, enum doca_ctx_states prev_state,
				  enum doca_ctx_states next_state)
{
	HbrHostState *state = (HbrHostState *)user_data.ptr;
	void *dpu_conn_desc = NULL;
	size_t dpu_conn_desc_len = 0;
	doca_error_t result;

	(void)ctx;
	(void)prev_state;
	state->rdma.ctx_state = next_state;
	if (next_state == DOCA_CTX_STATE_RUNNING && !state->rdma.connect_attempted)
	{
		state->rdma.connect_attempted = true;
		HBR_LOG("host", "RDMA context running; exporting responder connection descriptor");
		result = doca_rdma_export(state->rdma.rdma, &state->rdma.local_conn_desc, &state->rdma.local_conn_desc_len,
								  &state->rdma.connection);
		if (result != DOCA_SUCCESS)
		{
			HBR_LOG("host", "doca_rdma_export failed: %s", doca_error_get_descr(result));
			state->rdma.first_error = result;
			(void)doca_ctx_stop(state->rdma.ctx);
			return;
		}
		HBR_LOG("host", "waiting for DPU connection descriptor");
		if (hbr_write_file_exact(state->config->target_conn_desc_path, state->rdma.local_conn_desc,
								 state->rdma.local_conn_desc_len) != 0 ||
			hbr_wait_read_file_alloc(state->config->dpu_conn_desc_path, state->config->timeout_sec, &dpu_conn_desc,
									 &dpu_conn_desc_len) != 0)
		{
			state->rdma.first_error = DOCA_ERROR_OPERATING_SYSTEM;
			(void)doca_ctx_stop(state->rdma.ctx);
			return;
		}
		HBR_LOG("host", "connecting responder RDMA endpoint");
		result = doca_rdma_connect(state->rdma.rdma, dpu_conn_desc, dpu_conn_desc_len, state->rdma.connection);
		free(dpu_conn_desc);
		if (result != DOCA_SUCCESS)
		{
			HBR_LOG("host", "doca_rdma_connect failed: %s", doca_error_get_descr(result));
			state->rdma.first_error = result;
			(void)doca_ctx_stop(state->rdma.ctx);
			return;
		}
		state->rdma.connected = true;
		if (state->config->ready_path != NULL)
		{
			const char ready_byte = '1';

			if (hbr_write_file_exact(state->config->ready_path, &ready_byte, sizeof(ready_byte)) != 0)
			{
				state->rdma.first_error = DOCA_ERROR_OPERATING_SYSTEM;
				(void)doca_ctx_stop(state->rdma.ctx);
				return;
			}
		}
		HBR_LOG("host", "RDMA endpoint connected; polling target buffer for written record");
	}
	else if (next_state == DOCA_CTX_STATE_IDLE)
		state->rdma.run_pe = false;
}

static void
hbr_dpu_state_cb(const union doca_data user_data, struct doca_ctx *ctx, enum doca_ctx_states prev_state,
				 enum doca_ctx_states next_state)
{
	HbrDpuState *state = (HbrDpuState *)user_data.ptr;
	void *host_conn_desc = NULL;
	size_t host_conn_desc_len = 0;
	void *target_desc = NULL;
	size_t target_desc_len = 0;
	union doca_data task_user_data = {0};
	doca_error_t result;

	(void)task_user_data;
	(void)ctx;
	(void)prev_state;
	state->rdma.ctx_state = next_state;
	if (next_state == DOCA_CTX_STATE_RUNNING && !state->rdma.task_submitted)
	{
		state->rdma.task_submitted = true;
		if (state->source_pci_mmap == NULL || state->buf_inv == NULL)
		{
			HBR_LOG("dpu", "source PCI mmap or buffer inventory is not ready before RDMA callback");
			result = DOCA_ERROR_BAD_STATE;
			goto fail;
		}
		HBR_LOG("dpu", "RDMA context running; exporting requester connection descriptor");
		result = doca_rdma_export(state->rdma.rdma, &state->rdma.local_conn_desc, &state->rdma.local_conn_desc_len,
								  &state->rdma.connection);
		if (result != DOCA_SUCCESS)
			goto fail;
		HBR_LOG("dpu", "waiting for host connection and target RDMA mmap descriptors");
		if (hbr_write_file_exact(state->config->dpu_conn_desc_path, state->rdma.local_conn_desc,
								 state->rdma.local_conn_desc_len) != 0 ||
			hbr_wait_read_file_alloc(state->config->target_conn_desc_path, state->config->timeout_sec, &host_conn_desc,
									 &host_conn_desc_len) != 0 ||
			hbr_wait_read_file_alloc(state->config->target_rdma_desc_path, state->config->timeout_sec, &target_desc,
									 &target_desc_len) != 0)
		{
			result = DOCA_ERROR_OPERATING_SYSTEM;
			goto fail;
		}
		HBR_LOG("dpu", "connecting requester RDMA endpoint");
		result = doca_rdma_connect(state->rdma.rdma, host_conn_desc, host_conn_desc_len, state->rdma.connection);
		if (result != DOCA_SUCCESS)
			goto fail;
		state->rdma.connected = true;

		HBR_LOG("dpu", "importing remote target RDMA mmap");
		result = doca_mmap_create_from_export(NULL, target_desc, target_desc_len, state->rdma.dev,
											  &state->target_rdma_mmap);
		if (result != DOCA_SUCCESS)
			goto fail;
		result = doca_mmap_get_memrange(state->target_rdma_mmap, &state->target_addr, &state->target_len);
		if (result != DOCA_SUCCESS)
			goto fail;
		state->target_ready = true;

		free(host_conn_desc);
		free(target_desc);
		return;
	}
	else if (next_state == DOCA_CTX_STATE_IDLE)
		state->rdma.run_pe = false;
	return;

fail:
	HBR_LOG("dpu", "RDMA bridge setup/submit failed: %s", doca_error_get_descr(result));
	free(host_conn_desc);
	free(target_desc);
	state->rdma.first_error = result;
	(void)doca_ctx_stop(state->rdma.ctx);
}

static int
hbr_run_host(const HbrConfig *config)
{
	HbrHostState state;
	uint32_t pci_permissions = DOCA_ACCESS_FLAG_PCI_READ_ONLY;
	uint32_t target_permissions = DOCA_ACCESS_FLAG_LOCAL_READ_WRITE | DOCA_ACCESS_FLAG_RDMA_WRITE;
	size_t target_bytes = (size_t)config->bytes * (size_t)config->window;
	char validation_error[128];
	doca_error_t result;
	const uint64_t max_attempts = (uint64_t)config->timeout_sec * 1000ULL;

	memset(&state, 0, sizeof(state));
	state.config = config;
	if (config->relaxed_ordering)
		pci_permissions |= DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING;

	if (posix_memalign(&state.source_region, 4096, config->bytes) != 0 ||
		posix_memalign(&state.target_region, 4096, target_bytes) != 0)
	{
		fprintf(stderr, "failed to allocate host buffers\n");
		return 1;
	}
	hbr_fill_record(state.source_region, config->bytes);
	memset(state.target_region, 0, target_bytes);

	result = open_doca_device_with_pci(config->pci_addr, NULL, &state.pci_dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to open host PCI device %s: %s\n", config->pci_addr, doca_error_get_descr(result));
		return 1;
	}
	result = hbr_create_local_mmap(&state.source_mmap, state.pci_dev, state.source_region, config->bytes,
								   pci_permissions);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to create source PCI mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = doca_mmap_export_pci(state.source_mmap, state.pci_dev, &state.source_pci_desc,
								  &state.source_pci_desc_len);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to export source PCI mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	if (hbr_write_file_exact(config->source_pci_desc_path, state.source_pci_desc, state.source_pci_desc_len) != 0 ||
		hbr_write_buffer_info(config->source_info_path, state.source_region, config->bytes) != 0)
		return 1;

	result = hbr_open_rdma_dev(config, &state.rdma.dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to open host RDMA device: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = hbr_create_local_mmap(&state.target_mmap, state.rdma.dev, state.target_region, target_bytes,
								   target_permissions);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to create target RDMA mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = doca_mmap_export_rdma(state.target_mmap, state.rdma.dev, &state.target_rdma_desc,
								   &state.target_rdma_desc_len);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to export target RDMA mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	if (hbr_write_file_exact(config->target_rdma_desc_path, state.target_rdma_desc, state.target_rdma_desc_len) != 0)
		return 1;

	printf("HBR_HOST_READY source_pci_desc=%s source=%p target=%p bytes=%u\n", config->source_pci_desc_path,
		   state.source_region, state.target_region, config->bytes);
	fflush(stdout);

	result = hbr_start_rdma(&state.rdma, config, &state, hbr_host_state_cb, false, DOCA_ACCESS_FLAG_RDMA_WRITE);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to start host RDMA context: %s\n", doca_error_get_descr(result));
		return 1;
	}

	for (uint64_t attempt = 0; attempt < max_attempts && state.rdma.run_pe; attempt++)
	{
		(void)doca_pe_progress(state.rdma.pe);
		if (config->dpu_done_path != NULL)
		{
			void *done_data = NULL;
			size_t done_len = 0;

			if (hbr_read_file_alloc(config->dpu_done_path, &done_data, &done_len) == 0)
			{
				bool all_valid = true;

				free(done_data);
				for (uint32_t i = 0; i < config->window; i++)
				{
					uint8_t *target_ptr = (uint8_t *)state.target_region + ((size_t)i * (size_t)config->bytes);

					if (!hbr_validate_record(target_ptr, config->bytes, validation_error, sizeof(validation_error)))
					{
						all_valid = false;
						break;
					}
				}
				if (all_valid)
				{
					state.validation_done = true;
					printf("HBR_HOST_VALIDATED mode=%s bytes=%u iterations=%" PRIu64 " window=%u\n",
						   hbr_mode_name(config->mode), config->bytes, config->iterations, config->window);
					(void)doca_ctx_stop(state.rdma.ctx);
				}
			}
		}
		else if (!state.validation_done && hbr_validate_record(state.target_region, config->bytes, validation_error,
															   sizeof(validation_error)))
		{
			state.validation_done = true;
			printf("HBR_HOST_VALIDATED direct_pci_source_rdma_write bytes=%u\n", config->bytes);
			(void)doca_ctx_stop(state.rdma.ctx);
		}
		usleep(1000);
	}
	if (!state.validation_done)
	{
		hbr_log_record_prefix("host", "target buffer after timeout", state.target_region, config->bytes);
		if (state.rdma.first_error != DOCA_SUCCESS)
			fprintf(stderr, "host RDMA error: %s\n", doca_error_get_descr(state.rdma.first_error));
		else
			fprintf(stderr, "host validation timed out or failed: %s\n", validation_error);
		return 1;
	}
	printf("HBR_HOST_DONE\n");
	return 0;
}

static int
hbr_run_dpu(const HbrConfig *config)
{
	HbrDpuState state;
	void *source_desc = NULL;
	size_t source_desc_len = 0;
	doca_error_t result;
	const uint64_t max_attempts = (uint64_t)config->timeout_sec * 1000ULL;

	memset(&state, 0, sizeof(state));
	state.config = config;

	/*
	 * Open the RDMA-capable DOCA device before starting the RDMA context so
	 * the DPU can import the host PCI mmap first. The RUNNING callback submits
	 * the RDMA write, so the source mmap and buffer inventory must already be
	 * valid when that callback fires.
	 */
	result = hbr_open_rdma_dev(config, &state.rdma.dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to open DPU RDMA device: %s\n", doca_error_get_descr(result));
		return 1;
	}
	HBR_LOG("dpu", "waiting for host PCI source descriptor");
	if (hbr_wait_read_file_alloc(config->source_pci_desc_path, config->timeout_sec, &source_desc, &source_desc_len) !=
			0 ||
		hbr_read_buffer_info(config->source_info_path, config->timeout_sec, &state.source_addr, &state.source_len) != 0)
		return 1;
	HBR_LOG("dpu", "importing host PCI source mmap");
	result = doca_mmap_create_from_export(NULL, source_desc, source_desc_len, state.rdma.dev, &state.source_pci_mmap);
	free(source_desc);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to import source PCI mmap on DPU: %s\n", doca_error_get_descr(result));
		return 1;
	}
	if (state.source_len != config->bytes)
	{
		fprintf(stderr, "source length mismatch: got %zu expected %u\n", state.source_len, config->bytes);
		return 1;
	}
	HBR_LOG("dpu", "starting buffer inventory");
	result = doca_buf_inventory_create(HBR_BUF_INVENTORY_SIZE +
										   config->window * (config->mode == HBR_MODE_DMA_STAGE_RDMA ? 4U : 2U),
									   &state.buf_inv);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to create buf inventory: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = doca_buf_inventory_start(state.buf_inv);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to start buf inventory: %s\n", doca_error_get_descr(result));
		return 1;
	}

	result = hbr_start_rdma(&state.rdma, config, &state, hbr_dpu_state_cb, true, DOCA_ACCESS_FLAG_LOCAL_READ_WRITE);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to start DPU RDMA context: %s\n", doca_error_get_descr(result));
		return 1;
	}

	for (uint64_t attempt = 0; attempt < max_attempts && state.rdma.run_pe && !state.target_ready; attempt++)
	{
		if (doca_pe_progress(state.rdma.pe) == 0)
			usleep(1000);
	}
	if (!state.target_ready)
	{
		(void)doca_ctx_stop(state.rdma.ctx);
		fprintf(stderr, "DPU validation timed out while waiting for RDMA target import\n");
		return 1;
	}
	if (config->ready_path != NULL)
	{
		void *ready_data = NULL;
		size_t ready_len = 0;

		HBR_LOG("dpu", "waiting for host RDMA ready file");
		if (hbr_wait_read_file_alloc(config->ready_path, config->timeout_sec, &ready_data, &ready_len) != 0)
		{
			(void)doca_ctx_stop(state.rdma.ctx);
			fprintf(stderr, "DPU validation timed out while waiting for host ready file\n");
			return 1;
		}
		free(ready_data);
	}

	HBR_LOG("dpu", "preparing benchmark resources mode=%s bytes=%u iterations=%" PRIu64 " window=%u",
			hbr_mode_name(config->mode), config->bytes, config->iterations, config->window);
	result = hbr_prepare_benchmark_resources(&state);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to prepare benchmark resources: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = hbr_run_benchmark(&state);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to start benchmark: %s\n", doca_error_get_descr(result));
		return 1;
	}

	for (uint64_t attempt = 0; attempt < max_attempts * 1000ULL && !state.benchmark_done; attempt++)
	{
		int progress = doca_pe_progress(state.rdma.pe);
		doca_error_t drive_result;

		if (state.dma_pe != NULL)
			progress += doca_pe_progress(state.dma_pe);
		drive_result = hbr_drive_benchmark_submissions(&state);
		if (drive_result != DOCA_SUCCESS)
		{
			state.rdma.first_error = drive_result;
			hbr_dpu_finish_benchmark(&state);
			break;
		}
		(void)progress;
	}
	if (!state.benchmark_done)
	{
		(void)doca_ctx_stop(state.rdma.ctx);
		if (state.dma_ctx != NULL)
			(void)doca_ctx_stop(state.dma_ctx);
		fprintf(stderr, "DPU benchmark timed out after completed=%" PRIu64 " submitted=%" PRIu64 "\n", state.completed,
				state.submitted);
		return 1;
	}

	if (state.rdma.first_error != DOCA_SUCCESS)
	{
		fprintf(stderr, "DPU validation failed: %s\n", doca_error_get_descr(state.rdma.first_error));
		return 1;
	}
	if (!state.rdma.task_done)
	{
		fprintf(stderr, "DPU validation ended without RDMA write completion\n");
		return 1;
	}
	printf("HBR_DPU_DONE mode=%s bytes=%u iterations=%" PRIu64 " window=%u\n", hbr_mode_name(config->mode),
		   config->bytes, config->iterations, config->window);
	return 0;
}

int
main(int argc, char **argv)
{
	HbrConfig config;

	setvbuf(stdout, NULL, _IOLBF, 0);
	setvbuf(stderr, NULL, _IOLBF, 0);

	if (!hbr_parse_args(argc, argv, &config))
		return 1;
	if (config.role == HBR_ROLE_HOST)
		return hbr_run_host(&config);
	return hbr_run_dpu(&config);
}
