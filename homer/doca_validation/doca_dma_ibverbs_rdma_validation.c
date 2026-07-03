#include <arpa/inet.h>
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
#include <doca_types.h>
#include <infiniband/verbs.h>

#include "common.h"

#define HBI_MAGIC 0x484249314452444dULL
#define HBI_DEFAULT_BYTES 4096U
#define HBI_DEFAULT_ITERATIONS 1ULL
#define HBI_DEFAULT_WINDOW 1U
#define HBI_DEFAULT_TIMEOUT_SEC 30U
#define HBI_MAX_WINDOW 256U
#define HBI_MAX_ITERATIONS 10485760ULL
#define HBI_PORT 1U
#define HBI_BUF_INVENTORY_BASE 8U

typedef enum HbiRole
{
	HBI_ROLE_NONE = 0,
	HBI_ROLE_HOST,
	HBI_ROLE_DPU,
} HbiRole;

typedef struct HbiConfig
{
	HbiRole role;
	const char *pci_addr;
	const char *ibdev;
	const char *source_pci_desc_path;
	const char *source_info_path;
	const char *local_desc_path;
	const char *remote_desc_path;
	const char *ready_path;
	const char *done_path;
	uint32_t gid_index;
	uint32_t bytes;
	uint32_t window;
	uint32_t timeout_sec;
	uint64_t iterations;
	bool has_gid_index;
	bool relaxed_ordering;
	bool optimize_reports;
	bool flush_report_sentinel;
	uint32_t report_interval;
} HbiConfig;

typedef struct HbiBufferInfo
{
	uint64_t addr;
	uint64_t len;
} HbiBufferInfo;

typedef struct HbiRecordHeader
{
	uint64_t magic;
	uint64_t sequence;
	uint64_t payload_len;
	uint64_t checksum;
} HbiRecordHeader;

typedef struct HbiPeerDesc
{
	uint32_t qpn;
	uint32_t psn;
	uint32_t rkey;
	uint32_t gid_index;
	uint64_t vaddr;
	uint8_t gid[16];
} HbiPeerDesc;

typedef struct HbiVerbsContext
{
	const HbiConfig *config;
	struct ibv_context *ctx;
	struct ibv_pd *pd;
	struct ibv_cq *cq;
	struct ibv_qp *qp;
	struct ibv_mr *mr;
	void *region;
	size_t region_len;
	HbiPeerDesc local;
	HbiPeerDesc remote;
} HbiVerbsContext;

typedef struct HbiLane
{
	struct doca_buf *src_buf;
	struct doca_buf *stage_dma_dst_buf;
	struct doca_dma_task_memcpy *dma_task;
	struct ibv_sge sge;
	struct ibv_send_wr wr;
	uint64_t dma_done_op_plus_one;
} HbiLane;

typedef struct HbiDpuState
{
	const HbiConfig *config;
	HbiVerbsContext verbs;
	struct doca_dev *dma_dev;
	struct doca_dma *dma;
	struct doca_ctx *dma_ctx;
	struct doca_pe *dma_pe;
	struct doca_mmap *source_pci_mmap;
	struct doca_mmap *stage_mmap;
	struct doca_buf_inventory *buf_inv;
	HbiLane *lanes;
	void *source_addr;
	size_t source_len;
	void *stage_region;
	size_t stage_len;
	uint64_t submitted;
	uint64_t completed;
	doca_error_t first_error;
	bool benchmark_done;
	struct timespec start_ts;
	struct timespec end_ts;
} HbiDpuState;

static uint64_t
hbi_checksum_bytes(const uint8_t *bytes, size_t len)
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
hbi_fill_record(void *region, size_t len)
{
	HbiRecordHeader *header = (HbiRecordHeader *)region;
	uint8_t *payload = (uint8_t *)region + sizeof(*header);
	size_t payload_len = len - sizeof(*header);

	header->magic = HBI_MAGIC;
	header->sequence = 1;
	header->payload_len = payload_len;
	for (size_t i = 0; i < payload_len; i++)
		payload[i] = (uint8_t)((i * 131U + 17U) & 0xffU);
	header->checksum = hbi_checksum_bytes(payload, payload_len);
}

static bool
hbi_validate_record(const void *region, size_t len, char *error_buf, size_t error_buf_len)
{
	const HbiRecordHeader *header = (const HbiRecordHeader *)region;
	const uint8_t *payload = (const uint8_t *)region + sizeof(*header);
	uint64_t checksum;

	if (len < sizeof(*header))
	{
		snprintf(error_buf, error_buf_len, "record too small: %zu", len);
		return false;
	}
	if (header->magic != HBI_MAGIC)
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
	checksum = hbi_checksum_bytes(payload, len - sizeof(*header));
	if (checksum != header->checksum)
	{
		snprintf(error_buf, error_buf_len, "checksum mismatch: got 0x%016" PRIx64 " expected 0x%016" PRIx64,
				 header->checksum, checksum);
		return false;
	}
	return true;
}

static bool
hbi_parse_u32(const char *value, uint32_t *out)
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
hbi_parse_u64(const char *value, uint64_t *out)
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

static void
hbi_usage(const char *prog)
{
	fprintf(stderr,
			"usage: %s --role=host|dpu --pci-addr=ADDR --ibdev=NAME --source-pci-desc=PATH "
			"--source-info=PATH --local-desc=PATH --remote-desc=PATH --gid-index=N [options]\n"
			"options:\n"
			"  --bytes=N             transfer size, default %u\n"
			"  --iterations=N        operations, default %" PRIu64 "\n"
			"  --window=N            max in-flight DMA/RDMA lanes, default %u\n"
			"  --ready=PATH          host writes after QP RTS; DPU waits before timing\n"
			"  --done=PATH           DPU completion file for host validation\n"
			"  --timeout-sec=N       descriptor/progress timeout, default %u\n"
			"  --relaxed-ordering    add PCI relaxed ordering to host source mmap\n"
			"  --optimize-reports    submit non-sentinel DMA tasks with OPTIMIZE_REPORTS\n"
			"  --flush-report-sentinel\n"
			"                        submit report-boundary DMA tasks with FLUSH\n"
			"  --report-interval=N   non-optimized DMA sentinel interval, default 1\n",
			prog, HBI_DEFAULT_BYTES, (uint64_t)HBI_DEFAULT_ITERATIONS, HBI_DEFAULT_WINDOW,
			HBI_DEFAULT_TIMEOUT_SEC);
}

static bool
hbi_parse_args(int argc, char **argv, HbiConfig *config)
{
	memset(config, 0, sizeof(*config));
	config->bytes = HBI_DEFAULT_BYTES;
	config->iterations = HBI_DEFAULT_ITERATIONS;
	config->window = HBI_DEFAULT_WINDOW;
	config->timeout_sec = HBI_DEFAULT_TIMEOUT_SEC;
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
			hbi_usage(argv[0]);
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
				config->role = HBI_ROLE_HOST;
			else if (strcmp(value, "dpu") == 0)
				config->role = HBI_ROLE_DPU;
			else
			{
				fprintf(stderr, "invalid role: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--pci-addr") && strncmp(arg, "--pci-addr", name_len) == 0)
			config->pci_addr = value;
		else if (name_len == strlen("--ibdev") && strncmp(arg, "--ibdev", name_len) == 0)
			config->ibdev = value;
		else if (name_len == strlen("--source-pci-desc") && strncmp(arg, "--source-pci-desc", name_len) == 0)
			config->source_pci_desc_path = value;
		else if (name_len == strlen("--source-info") && strncmp(arg, "--source-info", name_len) == 0)
			config->source_info_path = value;
		else if (name_len == strlen("--local-desc") && strncmp(arg, "--local-desc", name_len) == 0)
			config->local_desc_path = value;
		else if (name_len == strlen("--remote-desc") && strncmp(arg, "--remote-desc", name_len) == 0)
			config->remote_desc_path = value;
		else if (name_len == strlen("--ready") && strncmp(arg, "--ready", name_len) == 0)
			config->ready_path = value;
		else if (name_len == strlen("--done") && strncmp(arg, "--done", name_len) == 0)
			config->done_path = value;
		else if (name_len == strlen("--gid-index") && strncmp(arg, "--gid-index", name_len) == 0)
		{
			if (!hbi_parse_u32(value, &config->gid_index))
			{
				fprintf(stderr, "invalid gid index: %s\n", value);
				return false;
			}
			config->has_gid_index = true;
		}
		else if (name_len == strlen("--bytes") && strncmp(arg, "--bytes", name_len) == 0)
		{
			if (!hbi_parse_u32(value, &config->bytes))
			{
				fprintf(stderr, "invalid bytes: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--iterations") && strncmp(arg, "--iterations", name_len) == 0)
		{
			if (!hbi_parse_u64(value, &config->iterations) || config->iterations == 0 ||
				config->iterations > HBI_MAX_ITERATIONS)
			{
				fprintf(stderr, "invalid iterations: %s, expected 1..%" PRIu64 "\n", value,
						(uint64_t)HBI_MAX_ITERATIONS);
				return false;
			}
		}
		else if (name_len == strlen("--window") && strncmp(arg, "--window", name_len) == 0)
		{
			if (!hbi_parse_u32(value, &config->window) || config->window == 0 || config->window > HBI_MAX_WINDOW)
			{
				fprintf(stderr, "invalid window: %s, expected 1..%u\n", value, HBI_MAX_WINDOW);
				return false;
			}
		}
		else if (name_len == strlen("--timeout-sec") && strncmp(arg, "--timeout-sec", name_len) == 0)
		{
			if (!hbi_parse_u32(value, &config->timeout_sec))
			{
				fprintf(stderr, "invalid timeout: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--report-interval") && strncmp(arg, "--report-interval", name_len) == 0)
		{
			if (!hbi_parse_u32(value, &config->report_interval) || config->report_interval == 0)
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

	if (config->role == HBI_ROLE_NONE || config->pci_addr == NULL || config->ibdev == NULL ||
		config->source_pci_desc_path == NULL || config->source_info_path == NULL ||
		config->local_desc_path == NULL || config->remote_desc_path == NULL || !config->has_gid_index)
	{
		hbi_usage(argv[0]);
		return false;
	}
	if (config->bytes < sizeof(HbiRecordHeader))
	{
		fprintf(stderr, "--bytes must be at least %zu\n", sizeof(HbiRecordHeader));
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
hbi_write_file_exact(const char *path, const void *data, size_t len)
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
hbi_read_file_exact(const char *path, void *data, size_t len)
{
	FILE *file = fopen(path, "rb");

	if (file == NULL)
		return -1;
	if (fread(data, 1, len, file) != len)
	{
		fclose(file);
		return -1;
	}
	fclose(file);
	return 0;
}

static int
hbi_read_file_alloc(const char *path, void **data, size_t *len)
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
hbi_wait_read_file_alloc(const char *path, uint32_t timeout_sec, void **data, size_t *len)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (hbi_read_file_alloc(path, data, len) == 0)
			return 0;
		usleep(1000);
	}
	fprintf(stderr, "timed out waiting for %s\n", path);
	return -1;
}

static int
hbi_wait_read_desc(const char *path, uint32_t timeout_sec, HbiPeerDesc *desc)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (hbi_read_file_exact(path, desc, sizeof(*desc)) == 0)
			return 0;
		usleep(1000);
	}
	fprintf(stderr, "timed out waiting for %s\n", path);
	return -1;
}

static int
hbi_wait_file_exists(const char *path, uint32_t timeout_sec)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (access(path, R_OK) == 0)
			return 0;
		usleep(1000);
	}
	fprintf(stderr, "timed out waiting for %s\n", path);
	return -1;
}

static int
hbi_write_buffer_info(const char *path, void *addr, size_t len)
{
	HbiBufferInfo info;

	info.addr = (uint64_t)(uintptr_t)addr;
	info.len = (uint64_t)len;
	return hbi_write_file_exact(path, &info, sizeof(info));
}

static int
hbi_read_buffer_info(const char *path, uint32_t timeout_sec, void **addr, size_t *len)
{
	HbiBufferInfo info;
	void *data = NULL;
	size_t data_len = 0;

	if (hbi_wait_read_file_alloc(path, timeout_sec, &data, &data_len) != 0)
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

static double
hbi_timespec_diff_sec(const struct timespec *start, const struct timespec *end)
{
	return (double)(end->tv_sec - start->tv_sec) + (double)(end->tv_nsec - start->tv_nsec) / 1000000000.0;
}

static uint32_t
hbi_random_psn(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)((ts.tv_nsec ^ ts.tv_sec ^ getpid()) & 0xffffffU);
}

static uint64_t
hbi_pack_completion_id(uint32_t lane_index, uint64_t op_index)
{
	return (op_index << 32U) | (uint64_t)lane_index;
}

static uint32_t
hbi_completion_lane(uint64_t completion_id)
{
	return (uint32_t)(completion_id & 0xffffffffU);
}

static uint64_t
hbi_completion_op(uint64_t completion_id)
{
	return completion_id >> 32U;
}

static doca_error_t
hbi_create_local_mmap(struct doca_mmap **mmap, struct doca_dev *dev, void *addr, size_t len, uint32_t permissions)
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

static int
hbi_open_ib_device(HbiVerbsContext *ctx)
{
	struct ibv_device **devices;
	int num_devices = 0;
	int rc = -1;

	devices = ibv_get_device_list(&num_devices);
	if (devices == NULL)
	{
		fprintf(stderr, "ibv_get_device_list failed\n");
		return -1;
	}
	for (int i = 0; i < num_devices; i++)
	{
		if (strcmp(ibv_get_device_name(devices[i]), ctx->config->ibdev) == 0)
		{
			ctx->ctx = ibv_open_device(devices[i]);
			if (ctx->ctx == NULL)
				fprintf(stderr, "ibv_open_device(%s) failed\n", ctx->config->ibdev);
			else
				rc = 0;
			break;
		}
	}
	ibv_free_device_list(devices);
	if (ctx->ctx == NULL && rc != 0)
		fprintf(stderr, "IB device %s not found\n", ctx->config->ibdev);
	return rc;
}

static int
hbi_init_verbs(HbiVerbsContext *ctx, const HbiConfig *config, void *region, size_t region_len)
{
	struct ibv_qp_init_attr qp_init;
	union ibv_gid gid;
	int access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_RELAXED_ORDERING;

	memset(ctx, 0, sizeof(*ctx));
	ctx->config = config;
	ctx->region = region;
	ctx->region_len = region_len;
	if (config->role == HBI_ROLE_HOST)
		access_flags |= IBV_ACCESS_REMOTE_WRITE;

	if (hbi_open_ib_device(ctx) != 0)
		return -1;
	if (ibv_query_gid(ctx->ctx, HBI_PORT, config->gid_index, &gid) != 0)
	{
		fprintf(stderr, "ibv_query_gid failed for %s gid_index=%u: %s\n", config->ibdev, config->gid_index,
				strerror(errno));
		return -1;
	}
	ctx->pd = ibv_alloc_pd(ctx->ctx);
	if (ctx->pd == NULL)
	{
		fprintf(stderr, "ibv_alloc_pd failed\n");
		return -1;
	}
	ctx->cq = ibv_create_cq(ctx->ctx, (int)(config->window * 2U + 64U), NULL, NULL, 0);
	if (ctx->cq == NULL)
	{
		fprintf(stderr, "ibv_create_cq failed\n");
		return -1;
	}
	memset(&qp_init, 0, sizeof(qp_init));
	qp_init.send_cq = ctx->cq;
	qp_init.recv_cq = ctx->cq;
	qp_init.qp_type = IBV_QPT_RC;
	qp_init.cap.max_send_wr = config->window + 64U;
	qp_init.cap.max_recv_wr = 1;
	qp_init.cap.max_send_sge = 1;
	qp_init.cap.max_recv_sge = 1;
	ctx->qp = ibv_create_qp(ctx->pd, &qp_init);
	if (ctx->qp == NULL)
	{
		fprintf(stderr, "ibv_create_qp failed\n");
		return -1;
	}
	ctx->mr = ibv_reg_mr(ctx->pd, region, region_len, access_flags);
	if (ctx->mr == NULL)
	{
		fprintf(stderr, "ibv_reg_mr failed for %zu bytes: %s\n", region_len, strerror(errno));
		return -1;
	}
	ctx->local.qpn = ctx->qp->qp_num;
	ctx->local.psn = hbi_random_psn();
	ctx->local.rkey = ctx->mr->rkey;
	ctx->local.vaddr = (uint64_t)(uintptr_t)region;
	ctx->local.gid_index = config->gid_index;
	memcpy(ctx->local.gid, &gid, sizeof(ctx->local.gid));
	return 0;
}

static int
hbi_connect_qp(HbiVerbsContext *ctx)
{
	struct ibv_qp_attr attr;
	union ibv_gid remote_gid;

	memset(&attr, 0, sizeof(attr));
	attr.qp_state = IBV_QPS_INIT;
	attr.port_num = HBI_PORT;
	attr.pkey_index = 0;
	attr.qp_access_flags = IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_LOCAL_WRITE;
	if (ibv_modify_qp(ctx->qp, &attr,
					  IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS) != 0)
	{
		fprintf(stderr, "modify QP INIT failed: %s\n", strerror(errno));
		return -1;
	}

	memcpy(&remote_gid, ctx->remote.gid, sizeof(remote_gid));
	memset(&attr, 0, sizeof(attr));
	attr.qp_state = IBV_QPS_RTR;
	attr.path_mtu = IBV_MTU_1024;
	attr.dest_qp_num = ctx->remote.qpn;
	attr.rq_psn = ctx->remote.psn;
	attr.max_dest_rd_atomic = 1;
	attr.min_rnr_timer = 12;
	attr.ah_attr.is_global = 1;
	attr.ah_attr.grh.dgid = remote_gid;
	attr.ah_attr.grh.sgid_index = ctx->config->gid_index;
	attr.ah_attr.grh.hop_limit = 64;
	attr.ah_attr.dlid = 0;
	attr.ah_attr.sl = 0;
	attr.ah_attr.src_path_bits = 0;
	attr.ah_attr.port_num = HBI_PORT;
	if (ibv_modify_qp(ctx->qp, &attr,
					  IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
						  IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER) != 0)
	{
		fprintf(stderr, "modify QP RTR failed: %s\n", strerror(errno));
		return -1;
	}

	memset(&attr, 0, sizeof(attr));
	attr.qp_state = IBV_QPS_RTS;
	attr.timeout = 14;
	attr.retry_cnt = 7;
	attr.rnr_retry = 7;
	attr.sq_psn = ctx->local.psn;
	attr.max_rd_atomic = 1;
	if (ibv_modify_qp(ctx->qp, &attr,
					  IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN |
						  IBV_QP_MAX_QP_RD_ATOMIC) != 0)
	{
		fprintf(stderr, "modify QP RTS failed: %s\n", strerror(errno));
		return -1;
	}
	return 0;
}

static int
hbi_exchange_and_connect(HbiVerbsContext *ctx)
{
	if (hbi_write_file_exact(ctx->config->local_desc_path, &ctx->local, sizeof(ctx->local)) != 0)
		return -1;
	if (hbi_wait_read_desc(ctx->config->remote_desc_path, ctx->config->timeout_sec, &ctx->remote) != 0)
		return -1;
	if (hbi_connect_qp(ctx) != 0)
		return -1;
	if (ctx->config->ready_path != NULL)
	{
		const char ready_byte = '1';

		if (ctx->config->role == HBI_ROLE_HOST)
		{
			if (hbi_write_file_exact(ctx->config->ready_path, &ready_byte, sizeof(ready_byte)) != 0)
				return -1;
		}
		else if (hbi_wait_file_exists(ctx->config->ready_path, ctx->config->timeout_sec) != 0)
			return -1;
	}
	return 0;
}

static void hbi_finish_benchmark(HbiDpuState *state);
static doca_error_t hbi_submit_dma(HbiDpuState *state, uint32_t lane_index, uint64_t op_index);

static bool
hbi_is_report_sentinel(const HbiConfig *config, uint64_t op_index)
{
	return ((op_index + 1U) % config->report_interval) == 0 || op_index + 1U == config->iterations;
}

static uint32_t
hbi_submit_flags(const HbiConfig *config, uint64_t op_index)
{
	uint32_t flags = 0;

	if (!hbi_is_report_sentinel(config, op_index))
	{
		if (config->optimize_reports)
			flags |= DOCA_TASK_SUBMIT_FLAG_OPTIMIZE_REPORTS;
	}
	else if (config->flush_report_sentinel)
		flags |= DOCA_TASK_SUBMIT_FLAG_FLUSH;
	return flags;
}

static doca_error_t
hbi_submit_doca_task(const HbiConfig *config, struct doca_task *task, uint64_t op_index)
{
	uint32_t flags = hbi_submit_flags(config, op_index);

	if (flags != 0)
		return doca_task_submit_ex(task, flags);
	return doca_task_submit(task);
}

static void
hbi_dma_complete_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data, union doca_data ctx_user_data)
{
	HbiDpuState *state = (HbiDpuState *)ctx_user_data.ptr;
	uint64_t op_index = hbi_completion_op(task_user_data.u64);
	uint32_t lane_index = hbi_completion_lane(task_user_data.u64);

	(void)task;
	state->lanes[lane_index].dma_done_op_plus_one = op_index + 1U;
}

static void
hbi_dma_error_cb(struct doca_dma_task_memcpy *task, union doca_data task_user_data, union doca_data ctx_user_data)
{
	HbiDpuState *state = (HbiDpuState *)ctx_user_data.ptr;

	(void)task_user_data;
	state->first_error = doca_task_get_status(doca_dma_task_memcpy_as_task(task));
	fprintf(stderr, "DMA pull task failed: %s\n", doca_error_get_descr(state->first_error));
	hbi_finish_benchmark(state);
}

static doca_error_t
hbi_start_dma(HbiDpuState *state)
{
	union doca_data user_data = {0};
	doca_error_t result;

	result = doca_pe_create(&state->dma_pe);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_dma_create(state->dma_dev, &state->dma);
	if (result != DOCA_SUCCESS)
		return result;
	state->dma_ctx = doca_dma_as_ctx(state->dma);
	if (state->dma_ctx == NULL)
		return DOCA_ERROR_UNEXPECTED;
	result = doca_dma_task_memcpy_set_conf(state->dma, hbi_dma_complete_cb, hbi_dma_error_cb, state->config->window);
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
hbi_prepare_dpu_resources(HbiDpuState *state)
{
	const HbiConfig *config = state->config;
	size_t stage_bytes = (size_t)config->window * (size_t)config->bytes;
	doca_error_t result;

	state->lanes = calloc(config->window, sizeof(*state->lanes));
	if (state->lanes == NULL)
		return DOCA_ERROR_NO_MEMORY;
	if (posix_memalign(&state->stage_region, 4096, stage_bytes) != 0)
		return DOCA_ERROR_NO_MEMORY;
	state->stage_len = stage_bytes;
	memset(state->stage_region, 0, stage_bytes);

	result = hbi_create_local_mmap(&state->stage_mmap, state->dma_dev, state->stage_region, stage_bytes,
								   DOCA_ACCESS_FLAG_LOCAL_READ_WRITE);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_create(HBI_BUF_INVENTORY_BASE + config->window * 2U, &state->buf_inv);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_start(state->buf_inv);
	if (result != DOCA_SUCCESS)
		return result;
	result = hbi_start_dma(state);
	if (result != DOCA_SUCCESS)
		return result;
	if (hbi_init_verbs(&state->verbs, config, state->stage_region, stage_bytes) != 0)
		return DOCA_ERROR_OPERATING_SYSTEM;
	if (hbi_exchange_and_connect(&state->verbs) != 0)
		return DOCA_ERROR_OPERATING_SYSTEM;

	for (uint32_t lane_index = 0; lane_index < config->window; lane_index++)
	{
		HbiLane *lane = &state->lanes[lane_index];
		uint8_t *stage_ptr = (uint8_t *)state->stage_region + (size_t)lane_index * config->bytes;
		union doca_data task_user_data = {0};

		task_user_data.u64 = lane_index;
		result = doca_buf_inventory_buf_get_by_data(state->buf_inv, state->source_pci_mmap, state->source_addr,
													state->source_len, &lane->src_buf);
		if (result != DOCA_SUCCESS)
			return result;
		result = doca_buf_inventory_buf_get_by_addr(state->buf_inv, state->stage_mmap, stage_ptr, config->bytes,
													&lane->stage_dma_dst_buf);
		if (result != DOCA_SUCCESS)
			return result;
		result = doca_dma_task_memcpy_alloc_init(state->dma, lane->src_buf, lane->stage_dma_dst_buf, task_user_data,
												 &lane->dma_task);
		if (result != DOCA_SUCCESS)
			return result;
	}
	return DOCA_SUCCESS;
}

static doca_error_t
hbi_submit_dma(HbiDpuState *state, uint32_t lane_index, uint64_t op_index)
{
	const HbiConfig *config = state->config;
	HbiLane *lane = &state->lanes[lane_index];
	uint8_t *stage_ptr = (uint8_t *)state->stage_region + ((size_t)lane_index * (size_t)config->bytes);
	union doca_data task_user_data = {0};
	struct doca_task *task;
	doca_error_t result;

	task_user_data.u64 = hbi_pack_completion_id(lane_index, op_index);
	result = doca_buf_inventory_buf_reuse_by_data(lane->src_buf, state->source_addr, state->source_len);
	if (result != DOCA_SUCCESS)
		return result;
	result = doca_buf_inventory_buf_reuse_by_addr(lane->stage_dma_dst_buf, stage_ptr, config->bytes);
	if (result != DOCA_SUCCESS)
		return result;
	doca_dma_task_memcpy_set_src(lane->dma_task, lane->src_buf);
	doca_dma_task_memcpy_set_dst(lane->dma_task, lane->stage_dma_dst_buf);
	task = doca_dma_task_memcpy_as_task(lane->dma_task);
	doca_task_set_user_data(task, task_user_data);
	return hbi_submit_doca_task(config, task, op_index);
}

static void
hbi_prepare_rdma_write(HbiDpuState *state, uint32_t lane_index, uint64_t op_index)
{
	const HbiConfig *config = state->config;
	HbiLane *lane = &state->lanes[lane_index];
	uint8_t *stage_ptr = (uint8_t *)state->stage_region + (size_t)lane_index * config->bytes;
	uint64_t remote_addr = state->verbs.remote.vaddr + (uint64_t)lane_index * config->bytes;

	lane->sge.addr = (uintptr_t)stage_ptr;
	lane->sge.length = config->bytes;
	lane->sge.lkey = state->verbs.mr->lkey;
	memset(&lane->wr, 0, sizeof(lane->wr));
	lane->wr.wr_id = hbi_pack_completion_id(lane_index, op_index);
	lane->wr.sg_list = &lane->sge;
	lane->wr.num_sge = 1;
	lane->wr.opcode = IBV_WR_RDMA_WRITE;
	lane->wr.send_flags = IBV_SEND_SIGNALED;
	lane->wr.wr.rdma.remote_addr = remote_addr;
	lane->wr.wr.rdma.rkey = state->verbs.remote.rkey;
}

static int
hbi_post_rdma_batch(HbiDpuState *state, const uint32_t *lane_indexes, const uint64_t *op_indexes, uint32_t count)
{
	struct ibv_send_wr *bad_wr = NULL;
	struct ibv_send_wr *first = NULL;
	struct ibv_send_wr *prev = NULL;

	for (uint32_t i = 0; i < count; i++)
	{
		HbiLane *lane = &state->lanes[lane_indexes[i]];

		hbi_prepare_rdma_write(state, lane_indexes[i], op_indexes[i]);
		if (prev != NULL)
			prev->next = &lane->wr;
		else
			first = &lane->wr;
		prev = &lane->wr;
	}
	if (prev != NULL)
		prev->next = NULL;
	if (first == NULL)
		return 0;
	if (ibv_post_send(state->verbs.qp, first, &bad_wr) != 0)
	{
		uint64_t bad_op = bad_wr != NULL ? bad_wr->wr_id : UINT64_MAX;

		fprintf(stderr, "ibv_post_send batch failed count=%u bad_op=%" PRIu64 ": %s\n", count, bad_op,
				strerror(errno));
		return -1;
	}
	return 0;
}

static void
hbi_finish_benchmark(HbiDpuState *state)
{
	double seconds;
	double mib_per_sec;
	double ops_per_sec;
	uint64_t total_bytes;

	if (state->benchmark_done)
		return;
	clock_gettime(CLOCK_MONOTONIC, &state->end_ts);
	state->benchmark_done = true;
	seconds = hbi_timespec_diff_sec(&state->start_ts, &state->end_ts);
	if (seconds <= 0.0)
		seconds = 1e-9;
	total_bytes = state->completed * (uint64_t)state->config->bytes;
	mib_per_sec = ((double)total_bytes / (1024.0 * 1024.0)) / seconds;
	ops_per_sec = (double)state->completed / seconds;
	printf("HBI_DPU_RESULT mode=dma-ibverbs-rdma bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
		   " seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
		   state->config->bytes, state->config->iterations, state->config->window, state->completed, seconds,
		   ops_per_sec, mib_per_sec);
	if (state->config->done_path != NULL)
	{
		char done_buf[256];
		int done_len = snprintf(done_buf, sizeof(done_buf),
								"mode=dma-ibverbs-rdma bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
								" seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
								state->config->bytes, state->config->iterations, state->config->window,
								state->completed, seconds, ops_per_sec, mib_per_sec);

		if (done_len > 0)
			(void)hbi_write_file_exact(state->config->done_path, done_buf, (size_t)done_len);
	}
}

static int
hbi_run_dpu_benchmark(HbiDpuState *state)
{
	struct ibv_wc wc[64];
	uint32_t ready_lanes[64];
	uint64_t ready_ops[64];
	uint32_t initial = state->config->window;

	if (initial > state->config->iterations)
		initial = (uint32_t)state->config->iterations;

	/*
	 * Timed work starts after descriptor exchange, QP RTS, DOCA mmap import,
	 * task allocation, and the host-ready gate. The loop does not issue SSH or
	 * filesystem polling; it only progresses DOCA DMA completions and verbs CQEs.
	 */
	clock_gettime(CLOCK_MONOTONIC, &state->start_ts);
	for (uint32_t i = 0; i < initial; i++)
	{
		doca_error_t result = hbi_submit_dma(state, i, state->submitted++);

		if (result != DOCA_SUCCESS)
		{
			fprintf(stderr, "initial DMA submit failed lane=%u: %s\n", i, doca_error_get_descr(result));
			state->first_error = result;
			hbi_finish_benchmark(state);
			return 1;
		}
	}

	while (state->completed < state->config->iterations && !state->benchmark_done)
	{
		uint32_t ready_count = 0;

		while (doca_pe_progress(state->dma_pe) > 0)
			;
		for (uint32_t i = 0; i < state->config->window && ready_count < 64U; i++)
		{
			uint64_t op_plus_one = state->lanes[i].dma_done_op_plus_one;

			if (op_plus_one == 0)
				continue;
			state->lanes[i].dma_done_op_plus_one = 0;
			ready_lanes[ready_count] = i;
			ready_ops[ready_count] = op_plus_one - 1U;
			ready_count++;
		}
		if (hbi_post_rdma_batch(state, ready_lanes, ready_ops, ready_count) != 0)
			return 1;

		for (;;)
		{
			int n = ibv_poll_cq(state->verbs.cq, 64, wc);

			if (n < 0)
			{
				fprintf(stderr, "ibv_poll_cq failed\n");
				return 1;
			}
			if (n == 0)
				break;
			for (int i = 0; i < n; i++)
			{
			uint64_t op_index = hbi_completion_op(wc[i].wr_id);
			uint32_t lane_index = hbi_completion_lane(wc[i].wr_id);

				if (wc[i].status != IBV_WC_SUCCESS)
				{
					fprintf(stderr, "RDMA completion failed lane=%u op=%" PRIu64 " status=%s vendor=0x%x\n",
							lane_index, op_index, ibv_wc_status_str(wc[i].status), wc[i].vendor_err);
					return 1;
				}
				state->completed++;
				if (state->completed == state->config->iterations)
				{
					hbi_finish_benchmark(state);
					break;
				}
				if (state->submitted < state->config->iterations)
				{
					doca_error_t result = hbi_submit_dma(state, lane_index, state->submitted++);

					if (result != DOCA_SUCCESS)
					{
						fprintf(stderr, "DMA resubmit failed lane=%u: %s\n", lane_index, doca_error_get_descr(result));
						state->first_error = result;
						hbi_finish_benchmark(state);
						return 1;
					}
				}
			}
			if (state->benchmark_done)
				break;
		}
	}
	return state->first_error == DOCA_SUCCESS ? 0 : 1;
}

static int
hbi_run_host(const HbiConfig *config)
{
	void *source_region = NULL;
	void *target_region = NULL;
	struct doca_dev *pci_dev = NULL;
	struct doca_mmap *source_mmap = NULL;
	const void *source_pci_desc = NULL;
	size_t source_pci_desc_len = 0;
	HbiVerbsContext verbs;
	uint32_t pci_permissions = DOCA_ACCESS_FLAG_PCI_READ_ONLY;
	size_t target_bytes = (size_t)config->bytes * (size_t)config->window;
	char validation_error[128] = "";
	doca_error_t result;

	if (config->relaxed_ordering)
		pci_permissions |= DOCA_ACCESS_FLAG_PCI_RELAXED_ORDERING;
	if (posix_memalign(&source_region, 4096, config->bytes) != 0 ||
		posix_memalign(&target_region, 4096, target_bytes) != 0)
	{
		fprintf(stderr, "failed to allocate host buffers\n");
		return 1;
	}
	hbi_fill_record(source_region, config->bytes);
	memset(target_region, 0, target_bytes);

	result = open_doca_device_with_pci(config->pci_addr, NULL, &pci_dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to open host PCI device %s: %s\n", config->pci_addr, doca_error_get_descr(result));
		return 1;
	}
	result = hbi_create_local_mmap(&source_mmap, pci_dev, source_region, config->bytes, pci_permissions);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to create source PCI mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	result = doca_mmap_export_pci(source_mmap, pci_dev, &source_pci_desc, &source_pci_desc_len);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to export source PCI mmap: %s\n", doca_error_get_descr(result));
		return 1;
	}
	if (hbi_write_file_exact(config->source_pci_desc_path, source_pci_desc, source_pci_desc_len) != 0 ||
		hbi_write_buffer_info(config->source_info_path, source_region, config->bytes) != 0)
		return 1;

	if (hbi_init_verbs(&verbs, config, target_region, target_bytes) != 0)
		return 1;
	if (hbi_exchange_and_connect(&verbs) != 0)
		return 1;
	printf("HBI_HOST_READY source=%p target=%p bytes=%u iterations=%" PRIu64 " window=%u qpn=%u\n",
		   source_region, target_region, config->bytes, config->iterations, config->window, verbs.local.qpn);

	if (config->done_path != NULL)
	{
		if (hbi_wait_file_exists(config->done_path, config->timeout_sec) != 0)
			return 1;
	}
	else
		sleep(config->timeout_sec);

	for (uint32_t i = 0; i < config->window; i++)
	{
		void *target = (uint8_t *)target_region + (size_t)i * config->bytes;

		if (!hbi_validate_record(target, config->bytes, validation_error, sizeof(validation_error)))
		{
			fprintf(stderr, "host validation failed for lane %u: %s\n", i, validation_error);
			return 1;
		}
	}
	printf("HBI_HOST_VALIDATED bytes=%u iterations=%" PRIu64 " window=%u\n", config->bytes, config->iterations,
		   config->window);
	printf("HBI_HOST_DONE\n");
	return 0;
}

static int
hbi_run_dpu(const HbiConfig *config)
{
	HbiDpuState state;
	void *source_desc = NULL;
	size_t source_desc_len = 0;
	doca_error_t result;

	memset(&state, 0, sizeof(state));
	state.config = config;
	state.first_error = DOCA_SUCCESS;

	result = open_doca_device_with_pci(config->pci_addr, NULL, &state.dma_dev);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to open DPU DOCA device %s: %s\n", config->pci_addr, doca_error_get_descr(result));
		return 1;
	}
	if (hbi_wait_read_file_alloc(config->source_pci_desc_path, config->timeout_sec, &source_desc, &source_desc_len) !=
			0 ||
		hbi_read_buffer_info(config->source_info_path, config->timeout_sec, &state.source_addr, &state.source_len) != 0)
		return 1;
	result = doca_mmap_create_from_export(NULL, source_desc, source_desc_len, state.dma_dev, &state.source_pci_mmap);
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
	result = hbi_prepare_dpu_resources(&state);
	if (result != DOCA_SUCCESS)
	{
		fprintf(stderr, "failed to prepare DPU resources: %s\n", doca_error_get_descr(result));
		return 1;
	}
	printf("HBI_DPU_READY bytes=%u iterations=%" PRIu64 " window=%u qpn=%u\n", config->bytes, config->iterations,
		   config->window, state.verbs.local.qpn);

	if (hbi_run_dpu_benchmark(&state) != 0)
		return 1;
	printf("HBI_DPU_DONE bytes=%u iterations=%" PRIu64 " window=%u\n", config->bytes, config->iterations,
		   config->window);
	return 0;
}

int
main(int argc, char **argv)
{
	HbiConfig config;

	setvbuf(stdout, NULL, _IOLBF, 0);
	setvbuf(stderr, NULL, _IOLBF, 0);
	if (!hbi_parse_args(argc, argv, &config))
		return 1;
	if (config.role == HBI_ROLE_HOST)
		return hbi_run_host(&config);
	return hbi_run_dpu(&config);
}
