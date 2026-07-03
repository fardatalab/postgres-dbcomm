#include <arpa/inet.h>
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <infiniband/verbs.h>

#define HVR_MAGIC 0x485652315752444dULL
#define HVR_DEFAULT_BYTES 4096U
#define HVR_DEFAULT_ITERATIONS 1ULL
#define HVR_DEFAULT_WINDOW 1U
#define HVR_DEFAULT_TIMEOUT_SEC 30U
#define HVR_MAX_WINDOW 256U
#define HVR_MAX_ITERATIONS 10485760ULL
#define HVR_PORT 1U

typedef enum HvrRole
{
	HVR_ROLE_NONE = 0,
	HVR_ROLE_HOST,
	HVR_ROLE_DPU,
} HvrRole;

typedef struct HvrConfig
{
	HvrRole role;
	const char *ibdev;
	const char *local_desc_path;
	const char *remote_desc_path;
	const char *done_path;
	const char *ready_path;
	uint32_t gid_index;
	uint32_t bytes;
	uint32_t window;
	uint32_t timeout_sec;
	uint64_t iterations;
	bool has_gid_index;
	bool single_buffer;
} HvrConfig;

typedef struct HvrRecordHeader
{
	uint64_t magic;
	uint64_t sequence;
	uint64_t payload_len;
	uint64_t checksum;
} HvrRecordHeader;

typedef struct HvrPeerDesc
{
	uint32_t qpn;
	uint32_t psn;
	uint32_t rkey;
	uint32_t gid_index;
	uint64_t vaddr;
	uint8_t gid[16];
} HvrPeerDesc;

typedef struct HvrContext
{
	const HvrConfig *config;
	struct ibv_context *ctx;
	struct ibv_pd *pd;
	struct ibv_cq *cq;
	struct ibv_qp *qp;
	struct ibv_mr *mr;
	void *region;
	size_t region_len;
	HvrPeerDesc local;
	HvrPeerDesc remote;
	struct ibv_sge *sges;
	struct ibv_send_wr *wrs;
	uint64_t submitted;
	uint64_t completed;
	struct timespec start_ts;
	struct timespec end_ts;
} HvrContext;

static uint64_t
hvr_checksum_bytes(const uint8_t *bytes, size_t len)
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
hvr_fill_record(void *region, size_t len, uint64_t sequence)
{
	HvrRecordHeader *header = (HvrRecordHeader *)region;
	uint8_t *payload = (uint8_t *)region + sizeof(*header);
	size_t payload_len = len - sizeof(*header);

	header->magic = HVR_MAGIC;
	header->sequence = sequence;
	header->payload_len = payload_len;
	for (size_t i = 0; i < payload_len; i++)
		payload[i] = (uint8_t)((i * 131U + sequence * 17U) & 0xffU);
	header->checksum = hvr_checksum_bytes(payload, payload_len);
}

static bool
hvr_validate_record(const void *region, size_t len, uint64_t sequence, char *error_buf, size_t error_buf_len)
{
	const HvrRecordHeader *header = (const HvrRecordHeader *)region;
	const uint8_t *payload = (const uint8_t *)region + sizeof(*header);
	uint64_t checksum;

	if (len < sizeof(*header))
	{
		snprintf(error_buf, error_buf_len, "record too small: %zu", len);
		return false;
	}
	if (header->magic != HVR_MAGIC)
	{
		snprintf(error_buf, error_buf_len, "magic mismatch: got 0x%016" PRIx64, header->magic);
		return false;
	}
	if (header->sequence != sequence)
	{
		snprintf(error_buf, error_buf_len, "sequence mismatch: got %" PRIu64 " expected %" PRIu64, header->sequence,
				 sequence);
		return false;
	}
	if (header->payload_len != len - sizeof(*header))
	{
		snprintf(error_buf, error_buf_len, "payload length mismatch: got %" PRIu64, header->payload_len);
		return false;
	}
	checksum = hvr_checksum_bytes(payload, len - sizeof(*header));
	if (checksum != header->checksum)
	{
		snprintf(error_buf, error_buf_len, "checksum mismatch: got 0x%016" PRIx64 " expected 0x%016" PRIx64,
				 header->checksum, checksum);
		return false;
	}
	return true;
}

static bool
hvr_parse_u32(const char *value, uint32_t *out)
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
hvr_parse_u64(const char *value, uint64_t *out)
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
hvr_usage(const char *prog)
{
	fprintf(stderr,
			"usage: %s --role=host|dpu --ibdev=NAME --local-desc=PATH --remote-desc=PATH [options]\n"
			"options:\n"
			"  --gid-index=N       RoCE GID index\n"
			"  --bytes=N           transfer size, default %u\n"
			"  --iterations=N      operations, default %" PRIu64 "\n"
			"  --window=N          max in-flight WRs, default %u\n"
			"  --single-buffer     post all WRs against lane 0, matching perftest-like buffer reuse\n"
			"  --ready=PATH        optional host-ready file; host writes after RTS, DPU waits before timing\n"
			"  --done=PATH         optional DPU completion file for host validation\n"
			"  --timeout-sec=N     descriptor/progress timeout, default %u\n",
			prog, HVR_DEFAULT_BYTES, (uint64_t)HVR_DEFAULT_ITERATIONS, HVR_DEFAULT_WINDOW,
			HVR_DEFAULT_TIMEOUT_SEC);
}

static bool
hvr_parse_args(int argc, char **argv, HvrConfig *config)
{
	memset(config, 0, sizeof(*config));
	config->bytes = HVR_DEFAULT_BYTES;
	config->iterations = HVR_DEFAULT_ITERATIONS;
	config->window = HVR_DEFAULT_WINDOW;
	config->timeout_sec = HVR_DEFAULT_TIMEOUT_SEC;

	for (int i = 1; i < argc; i++)
	{
		const char *arg = argv[i];
		const char *value = strchr(arg, '=');
		size_t name_len;

		if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0)
		{
			hvr_usage(argv[0]);
			return false;
		}
		if (strcmp(arg, "--single-buffer") == 0)
		{
			config->single_buffer = true;
			continue;
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
				config->role = HVR_ROLE_HOST;
			else if (strcmp(value, "dpu") == 0)
				config->role = HVR_ROLE_DPU;
			else
			{
				fprintf(stderr, "invalid role: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--ibdev") && strncmp(arg, "--ibdev", name_len) == 0)
			config->ibdev = value;
		else if (name_len == strlen("--local-desc") && strncmp(arg, "--local-desc", name_len) == 0)
			config->local_desc_path = value;
		else if (name_len == strlen("--remote-desc") && strncmp(arg, "--remote-desc", name_len) == 0)
			config->remote_desc_path = value;
		else if (name_len == strlen("--done") && strncmp(arg, "--done", name_len) == 0)
			config->done_path = value;
		else if (name_len == strlen("--ready") && strncmp(arg, "--ready", name_len) == 0)
			config->ready_path = value;
		else if (name_len == strlen("--gid-index") && strncmp(arg, "--gid-index", name_len) == 0)
		{
			if (!hvr_parse_u32(value, &config->gid_index))
			{
				fprintf(stderr, "invalid gid index: %s\n", value);
				return false;
			}
			config->has_gid_index = true;
		}
		else if (name_len == strlen("--bytes") && strncmp(arg, "--bytes", name_len) == 0)
		{
			if (!hvr_parse_u32(value, &config->bytes))
			{
				fprintf(stderr, "invalid bytes: %s\n", value);
				return false;
			}
		}
		else if (name_len == strlen("--iterations") && strncmp(arg, "--iterations", name_len) == 0)
		{
			if (!hvr_parse_u64(value, &config->iterations) || config->iterations == 0 ||
				config->iterations > HVR_MAX_ITERATIONS)
			{
				fprintf(stderr, "invalid iterations: %s, expected 1..%" PRIu64 "\n", value,
						(uint64_t)HVR_MAX_ITERATIONS);
				return false;
			}
		}
		else if (name_len == strlen("--window") && strncmp(arg, "--window", name_len) == 0)
		{
			if (!hvr_parse_u32(value, &config->window) || config->window == 0 || config->window > HVR_MAX_WINDOW)
			{
				fprintf(stderr, "invalid window: %s, expected 1..%u\n", value, HVR_MAX_WINDOW);
				return false;
			}
		}
		else if (name_len == strlen("--timeout-sec") && strncmp(arg, "--timeout-sec", name_len) == 0)
		{
			if (!hvr_parse_u32(value, &config->timeout_sec))
			{
				fprintf(stderr, "invalid timeout: %s\n", value);
				return false;
			}
		}
		else
		{
			fprintf(stderr, "unknown argument: %.*s\n", (int)name_len, arg);
			return false;
		}
	}

	if (config->role == HVR_ROLE_NONE || config->ibdev == NULL || config->local_desc_path == NULL ||
		config->remote_desc_path == NULL || !config->has_gid_index)
	{
		hvr_usage(argv[0]);
		return false;
	}
	if (config->bytes < sizeof(HvrRecordHeader))
	{
		fprintf(stderr, "--bytes must be at least %zu\n", sizeof(HvrRecordHeader));
		return false;
	}
	if (config->window > config->iterations)
		config->window = (uint32_t)config->iterations;
	return true;
}

static int
hvr_write_file_exact(const char *path, const void *data, size_t len)
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
hvr_read_file_exact(const char *path, void *data, size_t len)
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
hvr_wait_read_desc(const char *path, uint32_t timeout_sec, HvrPeerDesc *desc)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (hvr_read_file_exact(path, desc, sizeof(*desc)) == 0)
			return 0;
		usleep(1000);
	}
	fprintf(stderr, "timed out waiting for %s\n", path);
	return -1;
}

static int
hvr_wait_file_exists(const char *path, uint32_t timeout_sec)
{
	const uint64_t max_attempts = (uint64_t)timeout_sec * 1000ULL;

	for (uint64_t attempt = 0; attempt < max_attempts; attempt++)
	{
		if (access(path, R_OK) == 0)
			return 0;
		usleep(1000);
	}
	return -1;
}

static double
hvr_timespec_diff_sec(const struct timespec *start, const struct timespec *end)
{
	return (double)(end->tv_sec - start->tv_sec) + (double)(end->tv_nsec - start->tv_nsec) / 1000000000.0;
}

static uint32_t
hvr_random_psn(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)((ts.tv_nsec ^ ts.tv_sec ^ getpid()) & 0xffffffU);
}

static int
hvr_open_device(HvrContext *ctx)
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
hvr_init_verbs(HvrContext *ctx)
{
	struct ibv_qp_init_attr qp_init;
	union ibv_gid gid;
	size_t region_len = (size_t)ctx->config->bytes * (size_t)ctx->config->window;
	int access_flags = IBV_ACCESS_LOCAL_WRITE;

	/*
	 * Match perftest's default "PCIe relax order: ON" shape. Without this
	 * per-MR flag, large RDMA writes on the host-DPU path measured around
	 * 1 GiB/s even though ib_write_bw on the same devices reached line-rate
	 * ballpark numbers.
	 */
	access_flags |= IBV_ACCESS_RELAXED_ORDERING;
	if (ctx->config->role == HVR_ROLE_HOST)
		access_flags |= IBV_ACCESS_REMOTE_WRITE;
	ctx->region_len = region_len;
	if (posix_memalign(&ctx->region, 4096, region_len) != 0)
	{
		fprintf(stderr, "failed to allocate %zu bytes\n", region_len);
		return -1;
	}
	if (ctx->config->role == HVR_ROLE_DPU)
	{
		for (uint32_t i = 0; i < ctx->config->window; i++)
			hvr_fill_record((uint8_t *)ctx->region + (size_t)i * ctx->config->bytes, ctx->config->bytes,
							(uint64_t)i + 1U);
	}
	else
		memset(ctx->region, 0, region_len);

	if (hvr_open_device(ctx) != 0)
		return -1;
	if (ibv_query_gid(ctx->ctx, HVR_PORT, ctx->config->gid_index, &gid) != 0)
	{
		fprintf(stderr, "ibv_query_gid failed for %s gid_index=%u: %s\n", ctx->config->ibdev, ctx->config->gid_index,
				strerror(errno));
		return -1;
	}
	ctx->pd = ibv_alloc_pd(ctx->ctx);
	if (ctx->pd == NULL)
	{
		fprintf(stderr, "ibv_alloc_pd failed\n");
		return -1;
	}
	ctx->cq = ibv_create_cq(ctx->ctx, (int)(ctx->config->window * 2U + 16U), NULL, NULL, 0);
	if (ctx->cq == NULL)
	{
		fprintf(stderr, "ibv_create_cq failed\n");
		return -1;
	}
	memset(&qp_init, 0, sizeof(qp_init));
	qp_init.send_cq = ctx->cq;
	qp_init.recv_cq = ctx->cq;
	qp_init.qp_type = IBV_QPT_RC;
	qp_init.cap.max_send_wr = ctx->config->window + 16U;
	qp_init.cap.max_recv_wr = 1;
	qp_init.cap.max_send_sge = 1;
	qp_init.cap.max_recv_sge = 1;
	ctx->qp = ibv_create_qp(ctx->pd, &qp_init);
	if (ctx->qp == NULL)
	{
		fprintf(stderr, "ibv_create_qp failed\n");
		return -1;
	}
	ctx->mr = ibv_reg_mr(ctx->pd, ctx->region, region_len, access_flags);
	if (ctx->mr == NULL)
	{
		fprintf(stderr, "ibv_reg_mr failed for %zu bytes: %s\n", region_len, strerror(errno));
		return -1;
	}
	ctx->local.qpn = ctx->qp->qp_num;
	ctx->local.psn = hvr_random_psn();
	ctx->local.rkey = ctx->mr->rkey;
	ctx->local.vaddr = (uint64_t)(uintptr_t)ctx->region;
	ctx->local.gid_index = ctx->config->gid_index;
	memcpy(ctx->local.gid, &gid, sizeof(ctx->local.gid));
	return 0;
}

static int
hvr_connect_qp(HvrContext *ctx)
{
	struct ibv_qp_attr attr;
	union ibv_gid remote_gid;

	memset(&attr, 0, sizeof(attr));
	attr.qp_state = IBV_QPS_INIT;
	attr.port_num = HVR_PORT;
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
	attr.ah_attr.port_num = HVR_PORT;
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
hvr_exchange_and_connect(HvrContext *ctx)
{
	if (hvr_write_file_exact(ctx->config->local_desc_path, &ctx->local, sizeof(ctx->local)) != 0)
		return -1;
	if (hvr_wait_read_desc(ctx->config->remote_desc_path, ctx->config->timeout_sec, &ctx->remote) != 0)
		return -1;
	if (hvr_connect_qp(ctx) != 0)
		return -1;
	if (ctx->config->ready_path != NULL)
	{
		const char ready_byte = '1';

		if (ctx->config->role == HVR_ROLE_HOST)
		{
			if (hvr_write_file_exact(ctx->config->ready_path, &ready_byte, sizeof(ready_byte)) != 0)
				return -1;
		}
		else if (hvr_wait_file_exists(ctx->config->ready_path, ctx->config->timeout_sec) != 0)
		{
			fprintf(stderr, "timed out waiting for ready file %s\n", ctx->config->ready_path);
			return -1;
		}
	}
	return 0;
}

static void
hvr_prepare_write(HvrContext *ctx, uint32_t lane_index, uint64_t op_index)
{
	uint32_t buffer_index = ctx->config->single_buffer ? 0 : lane_index;
	uint8_t *src = (uint8_t *)ctx->region + (size_t)buffer_index * ctx->config->bytes;
	uint64_t remote_addr = ctx->remote.vaddr + (uint64_t)buffer_index * ctx->config->bytes;
	struct ibv_sge *sge = &ctx->sges[lane_index];
	struct ibv_send_wr *wr = &ctx->wrs[lane_index];

	sge->addr = (uintptr_t)src;
	sge->length = ctx->config->bytes;
	sge->lkey = ctx->mr->lkey;
	memset(wr, 0, sizeof(*wr));
	wr->wr_id = op_index;
	wr->sg_list = sge;
	wr->num_sge = 1;
	wr->opcode = IBV_WR_RDMA_WRITE;
	wr->send_flags = IBV_SEND_SIGNALED;
	wr->wr.rdma.remote_addr = remote_addr;
	wr->wr.rdma.rkey = ctx->remote.rkey;
}

static int
hvr_post_write_batch(HvrContext *ctx, const uint32_t *lane_indexes, const uint64_t *op_indexes, uint32_t count)
{
	struct ibv_send_wr *bad_wr = NULL;
	struct ibv_send_wr *first = NULL;
	struct ibv_send_wr *prev = NULL;

	for (uint32_t i = 0; i < count; i++)
	{
		struct ibv_send_wr *wr;

		hvr_prepare_write(ctx, lane_indexes[i], op_indexes[i]);
		wr = &ctx->wrs[lane_indexes[i]];
		if (prev != NULL)
			prev->next = wr;
		else
			first = wr;
		prev = wr;
	}
	if (prev != NULL)
		prev->next = NULL;
	if (first == NULL)
		return 0;
	if (ibv_post_send(ctx->qp, first, &bad_wr) != 0)
	{
		uint64_t bad_op = bad_wr != NULL ? bad_wr->wr_id : UINT64_MAX;

		fprintf(stderr, "ibv_post_send batch failed count=%u bad_op=%" PRIu64 ": %s\n", count, bad_op,
				strerror(errno));
		return -1;
	}
	return 0;
}

static int
hvr_run_dpu(HvrContext *ctx)
{
	struct ibv_wc wc[64];
	uint32_t initial_lanes[HVR_MAX_WINDOW];
	uint64_t initial_ops[HVR_MAX_WINDOW];
	double seconds;
	double ops_per_sec;
	double mib_per_sec;
	uint64_t total_bytes;

	ctx->sges = calloc(ctx->config->window, sizeof(*ctx->sges));
	ctx->wrs = calloc(ctx->config->window, sizeof(*ctx->wrs));
	if (ctx->sges == NULL || ctx->wrs == NULL)
	{
		fprintf(stderr, "failed to allocate WR lane arrays\n");
		return 1;
	}

	clock_gettime(CLOCK_MONOTONIC, &ctx->start_ts);
	for (uint32_t i = 0; i < ctx->config->window && ctx->submitted < ctx->config->iterations; i++)
	{
		initial_lanes[i] = i;
		initial_ops[i] = ctx->submitted;
		ctx->submitted++;
	}
	if (hvr_post_write_batch(ctx, initial_lanes, initial_ops, (uint32_t)ctx->submitted) != 0)
		return 1;

	while (ctx->completed < ctx->config->iterations)
	{
		int n = ibv_poll_cq(ctx->cq, 64, wc);
		uint32_t repost_lanes[64];
		uint64_t repost_ops[64];
		uint32_t repost_count = 0;

		if (n < 0)
		{
			fprintf(stderr, "ibv_poll_cq failed\n");
			return 1;
		}
		for (int i = 0; i < n; i++)
		{
			uint64_t op_index = wc[i].wr_id;
			uint32_t lane_index = (uint32_t)(op_index % ctx->config->window);

			if (wc[i].status != IBV_WC_SUCCESS)
			{
				fprintf(stderr, "send completion failed lane=%u op=%" PRIu64 " status=%s vendor=0x%x\n", lane_index,
						op_index, ibv_wc_status_str(wc[i].status), wc[i].vendor_err);
				return 1;
			}
			ctx->completed++;
			if (ctx->submitted < ctx->config->iterations)
			{
				uint64_t next_op = ctx->submitted++;

				repost_lanes[repost_count] = lane_index;
				repost_ops[repost_count] = next_op;
				repost_count++;
			}
		}
		if (hvr_post_write_batch(ctx, repost_lanes, repost_ops, repost_count) != 0)
			return 1;
	}
	clock_gettime(CLOCK_MONOTONIC, &ctx->end_ts);

	seconds = hvr_timespec_diff_sec(&ctx->start_ts, &ctx->end_ts);
	if (seconds <= 0.0)
		seconds = 1e-9;
	total_bytes = ctx->completed * (uint64_t)ctx->config->bytes;
	ops_per_sec = (double)ctx->completed / seconds;
	mib_per_sec = ((double)total_bytes / (1024.0 * 1024.0)) / seconds;
	printf("HVR_DPU_RESULT bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
		   " seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
		   ctx->config->bytes, ctx->config->iterations, ctx->config->window, ctx->completed, seconds, ops_per_sec,
		   mib_per_sec);
	if (ctx->config->done_path != NULL)
	{
		char done_buf[256];
		int done_len = snprintf(done_buf, sizeof(done_buf),
								"bytes=%u iterations=%" PRIu64 " window=%u completed=%" PRIu64
								" seconds=%.9f ops_per_sec=%.2f mib_per_sec=%.2f\n",
								ctx->config->bytes, ctx->config->iterations, ctx->config->window, ctx->completed,
								seconds, ops_per_sec, mib_per_sec);

		if (done_len > 0)
			(void)hvr_write_file_exact(ctx->config->done_path, done_buf, (size_t)done_len);
	}
	printf("HVR_DPU_DONE bytes=%u iterations=%" PRIu64 " window=%u\n", ctx->config->bytes, ctx->config->iterations,
		   ctx->config->window);
	return 0;
}

static int
hvr_run_host(HvrContext *ctx)
{
	char validation_error[128] = "";

	if (ctx->config->done_path != NULL)
	{
		if (hvr_wait_file_exists(ctx->config->done_path, ctx->config->timeout_sec) != 0)
		{
			fprintf(stderr, "timed out waiting for done file %s\n", ctx->config->done_path);
			return 1;
		}
	}
	else
		sleep(ctx->config->timeout_sec);

	for (uint32_t i = 0; i < ctx->config->window; i++)
	{
		void *target = (uint8_t *)ctx->region + (size_t)i * ctx->config->bytes;

		if (ctx->config->single_buffer && i > 0)
			break;
		if (!hvr_validate_record(target, ctx->config->bytes, (uint64_t)i + 1U, validation_error,
								 sizeof(validation_error)))
		{
			fprintf(stderr, "host validation failed for lane %u: %s\n", i, validation_error);
			return 1;
		}
	}
	printf("HVR_HOST_VALIDATED bytes=%u iterations=%" PRIu64 " window=%u\n", ctx->config->bytes,
		   ctx->config->iterations, ctx->config->window);
	printf("HVR_HOST_DONE\n");
	return 0;
}

int
main(int argc, char **argv)
{
	HvrConfig config;
	HvrContext ctx;

	setvbuf(stdout, NULL, _IOLBF, 0);
	setvbuf(stderr, NULL, _IOLBF, 0);
	if (!hvr_parse_args(argc, argv, &config))
		return 1;
	memset(&ctx, 0, sizeof(ctx));
	ctx.config = &config;
	if (hvr_init_verbs(&ctx) != 0)
		return 1;
	if (hvr_exchange_and_connect(&ctx) != 0)
		return 1;
	printf("HVR_%s_READY ibdev=%s bytes=%u iterations=%" PRIu64 " window=%u qpn=%u\n",
		   config.role == HVR_ROLE_HOST ? "HOST" : "DPU", config.ibdev, config.bytes, config.iterations,
		   config.window, ctx.local.qpn);
	if (config.role == HVR_ROLE_HOST)
		return hvr_run_host(&ctx);
	return hvr_run_dpu(&ctx);
}
