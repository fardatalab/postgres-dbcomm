#include "printtup_dpa_common.h"

#include <libflexio/flexio_ver.h>
#ifndef FLEXIO_VER_USED
#define FLEXIO_VER_USED FLEXIO_VER(25, 10, 0)
#endif
#include <libflexio/flexio.h>

#include <infiniband/verbs.h>

#include <algorithm>
#include <chrono>
#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

extern "C" {
extern flexio_func_t printtup_dpa_reserialize_row;
extern flexio_func_t printtup_dpa_reserialize_batch;
extern flexio_func_t printtup_dpa_reserialize_static_worker;
extern flexio_func_t printtup_dpa_set_batch_config;
extern struct flexio_app *printtup_dpa_device;
}

namespace {

struct Options {
	std::string dump_path;
	std::string csv_path = "printtup_dpa_results.csv";
	std::string device_name = "mlx5_0";
	std::vector<int> thread_counts{1};
	int batch_size = 0;
	size_t rows_per_task = 1;
	bool rows_per_task_all = false;
	bool window_writeback = true;
	bool materialize_input_working_set = false;
	std::string input_location = "dpa";
	uint64_t ring_output_bytes = 0;
	std::string ring_output_location = "host";
	uint32_t ring_partition_mode = PRINTTUP_DPA_RING_PARTITION_SHARED;
	uint32_t input_map_mode = PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO;
	uint32_t poll_us = 0;
	int timeout_seconds = 120;
	size_t max_rows = 0;
	size_t logical_rows = 0;
	size_t working_set_rows = 0;
	size_t input_working_set_rows = 0;
	uint32_t iterations = 1;
	uint32_t schedule_mode = PRINTTUP_DPA_SCHEDULE_CONTIGUOUS;
};

struct HeaderRecord {
	bool valid = false;
	bool words_bigendian = false;
	uint8_t datum_size = 0;
	uint8_t pointer_size = 0;
	uint32_t pg_version_num = 0;
};

struct AttrMeta {
	std::string sendname;
	uint32_t serializer = PRINTTUP_DPA_SERIALIZER_UNSUPPORTED;
};

struct SchemaRecord {
	uint64_t schema_id = 0;
	uint32_t natts = 0;
	std::vector<AttrMeta> attrs;
};

struct FieldRecord {
	bool is_null = false;
	std::vector<uint8_t> normalized;
	std::vector<uint8_t> serialized;
};

struct RowRecord {
	uint64_t schema_id = 0;
	uint64_t row_id = 0;
	uint32_t natts = 0;
	std::vector<uint8_t> row_payload;
	std::vector<FieldRecord> fields;
};

struct DumpData {
	HeaderRecord header;
	std::vector<SchemaRecord> schemas;
	std::vector<RowRecord> rows;
	uint32_t head_records = 0;
	uint32_t schema_records = 0;
	uint32_t row_records = 0;
};

struct DpaPreparedRow {
	uint64_t row_id = 0;
	uint32_t row_index = 0;
	std::vector<uint8_t> expected_row_payload;
};

struct RunResult {
	int threads = 0;
	size_t rows = 0;
	uint64_t payload_bytes = 0;
	uint64_t serializer_work_bytes = 0;
	uint64_t host_elapsed_us = 0;
	uint64_t enqueue_elapsed_us = 0;
	uint64_t start_elapsed_us = 0;
	uint64_t drain_wait_us = 0;
	uint64_t dpa_cycles_sum = 0;
	uint64_t dpa_cycles_max = 0;
	uint64_t cmdq_chunks = 0;
	uint64_t cmdq_tasks = 0;
	uint64_t ring_output_bytes = 0;
	uint64_t ring_slots = 0;
	uint32_t ring_partition_mode = PRINTTUP_DPA_RING_PARTITION_SHARED;
	uint32_t input_map_mode = PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO;
	uint64_t input_working_set_bytes = 0;
	std::string input_location = "dpa";
	size_t input_working_set_rows = 0;
	size_t working_set_rows = 0;
	size_t rows_per_task = 1;
	uint32_t schedule_mode = PRINTTUP_DPA_SCHEDULE_CONTIGUOUS;
	uint64_t mismatches = 0;
	uint64_t status_errors = 0;
	uint64_t verification_skipped = 0;
	double payload_throughput_mb_s = 0.0;
	double throughput_mb_s = 0.0;
	double dpa_throughput_mb_s = 0.0;
};

static uint64_t
now_us()
{
	auto now = std::chrono::steady_clock::now().time_since_epoch();
	return std::chrono::duration_cast<std::chrono::microseconds>(now).count();
}

[[noreturn]] static void
fail(const std::string &message)
{
	throw std::runtime_error(message);
}

static uint32_t
read_be_u32(const uint8_t *data)
{
	return ((uint32_t) data[0] << 24) |
		((uint32_t) data[1] << 16) |
		((uint32_t) data[2] << 8) |
		(uint32_t) data[3];
}

static int32_t
read_be_i32(const uint8_t *data)
{
	return (int32_t) read_be_u32(data);
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

static std::vector<uint8_t>
read_file(const std::string &path)
{
	std::ifstream in(path, std::ios::binary);

	if (!in)
		fail("failed to open dump file: " + path);
	in.seekg(0, std::ios::end);
	std::streamoff len = in.tellg();
	if (len < 0)
		fail("failed to stat dump file: " + path);
	in.seekg(0, std::ios::beg);
	std::vector<uint8_t> data((size_t) len);
	if (!data.empty())
		in.read(reinterpret_cast<char *>(data.data()), (std::streamsize) data.size());
	if (!in && !data.empty())
		fail("failed to read dump file: " + path);
	return data;
}

static std::string
read_len_string(const uint8_t *payload, size_t payload_len, size_t &pos)
{
	if (pos + 4 > payload_len)
		fail("truncated length-prefixed string");
	int32_t string_len = read_be_i32(payload + pos);
	pos += 4;
	if (string_len < 0)
		return std::string();
	if (pos + (size_t) string_len > payload_len)
		fail("truncated string payload");
	std::string result(reinterpret_cast<const char *>(payload + pos), (size_t) string_len);
	pos += (size_t) string_len;
	return result;
}

static uint32_t
serializer_for_sendname(const std::string &sendname)
{
	if (sendname == "pg_catalog.int4send(integer)")
		return PRINTTUP_DPA_SERIALIZER_INT4;
	if (sendname == "pg_catalog.date_send(pg_catalog.date)")
		return PRINTTUP_DPA_SERIALIZER_DATE;
	if (sendname == "pg_catalog.numeric_send(numeric)")
		return PRINTTUP_DPA_SERIALIZER_NUMERIC;
	if (sendname == "pg_catalog.textsend(text)" ||
		sendname == "pg_catalog.bpcharsend(character)" ||
		sendname == "pg_catalog.varcharsend(character varying)")
		return PRINTTUP_DPA_SERIALIZER_TEXTLIKE;
	return PRINTTUP_DPA_SERIALIZER_UNSUPPORTED;
}

static void
parse_head_record(DumpData &dump, const uint8_t *payload, size_t payload_len)
{
	size_t pos = 0;
	std::string magic = read_len_string(payload, payload_len, pos);

	if (pos + 7 > payload_len)
		fail("HEAD record is truncated");
	uint32_t version = read_be_u32(payload + pos);
	pos += 4;

	dump.header.valid = true;
	dump.header.words_bigendian = payload[pos++] == 1;
	dump.header.datum_size = payload[pos++];
	dump.header.pointer_size = payload[pos++];
	dump.header.pg_version_num = read_be_u32(payload + pos);
	pos += 4;

	if (magic != "PTBDMP1")
		fail("unexpected dump magic");
	if (version != 1)
		fail("unexpected dump version");
	if (pos != payload_len)
		fail("HEAD record has trailing bytes");
}

static SchemaRecord *
find_schema(DumpData &dump, uint64_t schema_id)
{
	for (auto &schema : dump.schemas)
	{
		if (schema.schema_id == schema_id)
			return &schema;
	}
	return nullptr;
}

static void
parse_schema_record(DumpData &dump, const uint8_t *payload, size_t payload_len)
{
	size_t pos = 0;

	if (payload_len < 12)
		fail("schema record is truncated");

	SchemaRecord schema;
	schema.schema_id = read_be_u64(payload + pos);
	pos += 8;
	schema.natts = read_be_u32(payload + pos);
	pos += 4;

	if (schema.natts > PRINTTUP_DPA_MAX_ATTRS)
		fail("schema natts exceeds PRINTTUP_DPA_MAX_ATTRS");
	if (find_schema(dump, schema.schema_id) != nullptr)
		fail("duplicate schema id in dump");

	schema.attrs.resize(schema.natts);
	for (uint32_t i = 0; i < schema.natts; i++)
	{
		if (pos + 30 > payload_len)
			fail("schema attribute record is truncated");

		pos += 4; /* attnum */
		pos += 4; /* atttypid */
		pos += 4; /* atttypmod */
		pos += 2; /* attlen */
		pos += 1; /* attbyval */
		pos += 1; /* attisdropped */
		pos += 1; /* attalign */
		pos += 1; /* attstorage */
		pos += 2; /* format */
		pos += 4; /* typsend */
		pos += 4; /* typreceive */
		pos += 4; /* typioparam */
		(void) read_len_string(payload, payload_len, pos); /* attname */
		(void) read_len_string(payload, payload_len, pos); /* typename */
		schema.attrs[i].sendname = read_len_string(payload, payload_len, pos);
		(void) read_len_string(payload, payload_len, pos); /* recvname */
		schema.attrs[i].serializer = serializer_for_sendname(schema.attrs[i].sendname);
		if (schema.attrs[i].serializer == PRINTTUP_DPA_SERIALIZER_UNSUPPORTED)
			fail("unsupported serializer in schema: " + schema.attrs[i].sendname);
	}

	if (pos != payload_len)
		fail("schema record has trailing bytes");
	dump.schemas.push_back(std::move(schema));
}

static void
parse_row_record(DumpData &dump, const uint8_t *payload, size_t payload_len)
{
	size_t pos = 0;
	RowRecord row;

	if (payload_len < 24)
		fail("row record is truncated");

	row.schema_id = read_be_u64(payload + pos);
	pos += 8;
	row.row_id = read_be_u64(payload + pos);
	pos += 8;
	row.natts = read_be_u32(payload + pos);
	pos += 4;
	uint32_t row_payload_len = read_be_u32(payload + pos);
	pos += 4;

	if (row.natts > PRINTTUP_DPA_MAX_ATTRS)
		fail("row natts exceeds PRINTTUP_DPA_MAX_ATTRS");
	if (pos + row_payload_len > payload_len)
		fail("row payload exceeds ROWD record size");

	row.row_payload.assign(payload + pos, payload + pos + row_payload_len);
	pos += row_payload_len;
	row.fields.resize(row.natts);

	for (uint32_t i = 0; i < row.natts; i++)
	{
		if (pos + 4 > payload_len)
			fail("row normalized length is truncated");
		int32_t norm_len = read_be_i32(payload + pos);
		pos += 4;
		row.fields[i].is_null = norm_len < 0;
		if (norm_len >= 0)
		{
			if (pos + (size_t) norm_len > payload_len)
				fail("row normalized payload is truncated");
			row.fields[i].normalized.assign(payload + pos, payload + pos + (size_t) norm_len);
			pos += (size_t) norm_len;
		}

		if (pos + 4 > payload_len)
			fail("row serialized length is truncated");
		int32_t ser_len = read_be_i32(payload + pos);
		pos += 4;
		if (ser_len >= 0)
		{
			if (pos + (size_t) ser_len > payload_len)
				fail("row serialized payload is truncated");
			row.fields[i].serialized.assign(payload + pos, payload + pos + (size_t) ser_len);
			pos += (size_t) ser_len;
		}

		if ((norm_len < 0) != (ser_len < 0))
			fail("row null markers disagree between normalized and serialized fields");
	}

	if (pos != payload_len)
		fail("row record has trailing bytes");

	SchemaRecord *schema = find_schema(dump, row.schema_id);
	if (schema == nullptr)
		fail("row references unknown schema id");
	if (schema->natts != row.natts)
		fail("row natts does not match schema natts");

	dump.rows.push_back(std::move(row));
}

static DumpData
parse_dump(const std::vector<uint8_t> &data, size_t max_rows)
{
	DumpData dump;
	size_t off = 0;

	while (off + 8 <= data.size())
	{
		const uint8_t *record = data.data() + off;
		uint32_t payload_len = read_be_u32(record + 4);
		const uint8_t *payload = record + 8;

		if (off + 8 + payload_len > data.size())
			fail("record length exceeds dump size");

		if (memcmp(record, "HEAD", 4) == 0)
		{
			dump.head_records++;
			parse_head_record(dump, payload, payload_len);
		}
		else if (memcmp(record, "SCHM", 4) == 0)
		{
			dump.schema_records++;
			parse_schema_record(dump, payload, payload_len);
		}
		else if (memcmp(record, "ROWD", 4) == 0)
		{
			if (!dump.header.valid)
				fail("ROWD record appeared before HEAD");
			dump.row_records++;
			if (max_rows == 0 || dump.rows.size() < max_rows)
				parse_row_record(dump, payload, payload_len);
		}
		else
			fail("unknown record tag in dump");

		off += 8 + payload_len;
	}

	if (off != data.size())
		fail("trailing bytes detected in dump");
	if (dump.rows.empty())
		fail("dump has no usable rows");
	return dump;
}

static size_t
align_up(size_t value, size_t alignment)
{
	return (value + alignment - 1) & ~(alignment - 1);
}

static std::vector<int>
parse_thread_counts(const std::string &value)
{
	std::vector<int> counts;
	std::stringstream ss(value);
	std::string item;

	while (std::getline(ss, item, ','))
	{
		if (item.empty())
			continue;
		int count = std::stoi(item);
		if (count <= 0)
			fail("thread counts must be positive");
		counts.push_back(count);
	}
	if (counts.empty())
		fail("--threads must contain at least one value");
	return counts;
}

static uint64_t
parse_size_bytes(const std::string &text)
{
	if (text.empty())
		fail("size value must not be empty");

	char suffix = text.back();
	uint64_t multiplier = 1;
	std::string digits = text;

	if (suffix == 'k' || suffix == 'K')
	{
		multiplier = 1024ULL;
		digits.pop_back();
	}
	else if (suffix == 'm' || suffix == 'M')
	{
		multiplier = 1024ULL * 1024ULL;
		digits.pop_back();
	}
	else if (suffix == 'g' || suffix == 'G')
	{
		multiplier = 1024ULL * 1024ULL * 1024ULL;
		digits.pop_back();
	}
	if (digits.empty())
		fail("size value is missing digits: " + text);
	return (uint64_t) std::stoull(digits) * multiplier;
}

static std::string
schedule_mode_name(uint32_t schedule_mode)
{
	switch (schedule_mode)
	{
		case PRINTTUP_DPA_SCHEDULE_CONTIGUOUS:
			return "contiguous";
		case PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED:
			return "static-chunked";
		default:
			return "unknown";
	}
}

static std::string
ring_partition_name(uint32_t ring_partition_mode)
{
	switch (ring_partition_mode)
	{
		case PRINTTUP_DPA_RING_PARTITION_SHARED:
			return "shared";
		case PRINTTUP_DPA_RING_PARTITION_PER_WORKER:
			return "per-worker";
		default:
			return "unknown";
	}
}

static std::string
input_map_mode_name(uint32_t input_map_mode)
{
	switch (input_map_mode)
	{
		case PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO:
			return "logical";
		case PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET:
			return "worker-offset";
		case PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD:
			return "worker-shard";
		default:
			return "unknown";
	}
}

static void
worker_shard_bounds(uint32_t worker_id, uint32_t worker_count,
					uint32_t prepared_rows, uint32_t *start, uint32_t *count)
{
	uint32_t base;
	uint32_t extra;

	if (worker_count == 0 || worker_id >= worker_count ||
		prepared_rows < worker_count)
		fail("worker-shard input map requires prepared rows >= worker count");
	base = prepared_rows / worker_count;
	extra = prepared_rows % worker_count;
	*start = worker_id * base + std::min(worker_id, extra);
	*count = base + (worker_id < extra ? 1U : 0U);
	if (*count == 0)
		fail("internal error: empty worker input shard");
}

static uint32_t
worker_input_offset(uint32_t worker_id, uint32_t prepared_rows)
{
	if (prepared_rows == 0)
		return 0;
	/*
	 * Multiplicative hashing spreads worker starting points over the prepared-row
	 * array without adding per-worker tables. This is only a benchmark mapping
	 * experiment; correctness still comes from selecting a complete prepared row.
	 */
	return (uint32_t) (((uint64_t) worker_id * 2654435761ULL) %
					   (uint64_t) prepared_rows);
}

static size_t
prepared_index_for_logical(size_t logical_index, size_t prepared_rows,
						   uint32_t schedule_mode, uint32_t input_map_mode,
						   int threads, size_t static_chunk_rows)
{
	if (prepared_rows == 0)
		fail("internal error: no prepared rows");
	if (input_map_mode == PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO ||
		schedule_mode != PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
		return logical_index % prepared_rows;
	if (input_map_mode != PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET &&
		input_map_mode != PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD)
		fail("internal error: unknown input map mode");
	if (threads <= 0 || static_chunk_rows == 0)
		fail("internal error: static input map requires static workers");

	size_t chunk_group = logical_index / static_chunk_rows;
	size_t row_offset = logical_index % static_chunk_rows;
	uint32_t worker_id = (uint32_t) (chunk_group % (size_t) threads);
	size_t chunk_round = chunk_group / (size_t) threads;
	size_t worker_local_ordinal = chunk_round * static_chunk_rows + row_offset;
	if (input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD)
	{
		uint32_t shard_start;
		uint32_t shard_count;

		worker_shard_bounds(worker_id, (uint32_t) threads,
							(uint32_t) prepared_rows,
							&shard_start, &shard_count);
		return shard_start + (worker_local_ordinal % shard_count);
	}
	uint32_t offset = worker_input_offset(worker_id, (uint32_t) prepared_rows);

	return (worker_local_ordinal + (size_t) offset) % prepared_rows;
}

static Options
parse_options(int argc, char **argv)
{
	Options opts;

	for (int i = 1; i < argc; i++)
	{
		std::string arg = argv[i];
		auto require_value = [&](const char *name) -> std::string {
			if (i + 1 >= argc)
				fail(std::string("missing value for ") + name);
			return argv[++i];
		};

		if (arg == "--dump")
			opts.dump_path = require_value("--dump");
		else if (arg == "--csv")
			opts.csv_path = require_value("--csv");
		else if (arg == "--device")
			opts.device_name = require_value("--device");
		else if (arg == "--threads")
			opts.thread_counts = parse_thread_counts(require_value("--threads"));
		else if (arg == "--batch-size")
			opts.batch_size = std::stoi(require_value("--batch-size"));
		else if (arg == "--rows-per-task")
		{
			std::string value = require_value("--rows-per-task");
			if (value == "all")
				opts.rows_per_task_all = true;
			else
				opts.rows_per_task = (size_t) std::stoull(value);
		}
		else if (arg == "--poll-us")
			opts.poll_us = (uint32_t) std::stoul(require_value("--poll-us"));
		else if (arg == "--no-window-writeback")
			opts.window_writeback = false;
		else if (arg == "--materialize-input-working-set")
			opts.materialize_input_working_set = true;
		else if (arg == "--input-location")
			opts.input_location = require_value("--input-location");
		else if (arg == "--ring-output-bytes")
			opts.ring_output_bytes = parse_size_bytes(require_value("--ring-output-bytes"));
		else if (arg == "--ring-output-location")
			opts.ring_output_location = require_value("--ring-output-location");
		else if (arg == "--ring-partition")
		{
			std::string value = require_value("--ring-partition");

			if (value == "shared")
				opts.ring_partition_mode = PRINTTUP_DPA_RING_PARTITION_SHARED;
			else if (value == "per-worker")
				opts.ring_partition_mode = PRINTTUP_DPA_RING_PARTITION_PER_WORKER;
			else
				fail("--ring-partition must be shared or per-worker");
		}
		else if (arg == "--input-map")
		{
			std::string value = require_value("--input-map");

			if (value == "logical")
				opts.input_map_mode = PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO;
			else if (value == "worker-offset")
				opts.input_map_mode = PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET;
			else if (value == "worker-shard")
				opts.input_map_mode = PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD;
			else
				fail("--input-map must be logical, worker-offset, or worker-shard");
		}
		else if (arg == "--iterations")
			opts.iterations = (uint32_t) std::stoul(require_value("--iterations"));
		else if (arg == "--schedule")
		{
			std::string value = require_value("--schedule");

			if (value == "contiguous")
				opts.schedule_mode = PRINTTUP_DPA_SCHEDULE_CONTIGUOUS;
			else if (value == "static-chunked")
				opts.schedule_mode = PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED;
			else
				fail("--schedule must be contiguous or static-chunked");
		}
		else if (arg == "--timeout")
			opts.timeout_seconds = std::stoi(require_value("--timeout"));
		else if (arg == "--max-rows")
			opts.max_rows = (size_t) std::stoull(require_value("--max-rows"));
		else if (arg == "--logical-rows")
			opts.logical_rows = (size_t) std::stoull(require_value("--logical-rows"));
		else if (arg == "--working-set-rows")
			opts.working_set_rows = (size_t) std::stoull(require_value("--working-set-rows"));
		else if (arg == "--input-working-set-rows")
			opts.input_working_set_rows =
				(size_t) std::stoull(require_value("--input-working-set-rows"));
		else if (arg == "--help")
		{
			std::cout
				<< "usage: " << argv[0]
				<< " --dump PATH [--csv PATH] [--device mlx5_0] [--threads 1,2,4]\n"
					<< "       [--batch-size N] [--rows-per-task N|all]\n"
					<< "       [--poll-us N] [--no-window-writeback]\n"
					<< "       [--materialize-input-working-set]\n"
					<< "       [--input-location dpa|host]\n"
					<< "       [--ring-output-bytes N[k|m|g]]\n"
					<< "       [--ring-output-location host|dpa]\n"
					<< "       [--ring-partition shared|per-worker]\n"
					<< "       [--input-map logical|worker-offset|worker-shard]\n"
					<< "       [--schedule contiguous|static-chunked]\n"
					<< "       [--iterations N] [--timeout SECONDS]\n"
					<< "       [--max-rows N] [--logical-rows N]\n"
				<< "       [--working-set-rows N] [--input-working-set-rows N]\n";
			std::exit(0);
		}
		else
			fail("unknown argument: " + arg);
	}

	if (opts.dump_path.empty())
		fail("--dump is required");
	if (opts.batch_size < 0)
		fail("--batch-size must be non-negative");
	if (!opts.rows_per_task_all && opts.rows_per_task == 0)
		fail("--rows-per-task must be positive, or use --rows-per-task all");
	if (opts.timeout_seconds <= 0)
		fail("--timeout must be positive");
	if (opts.iterations == 0)
		fail("--iterations must be positive");
	if (opts.schedule_mode == PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED &&
		opts.rows_per_task_all)
		fail("--schedule static-chunked requires a numeric --rows-per-task chunk size");
	if (opts.input_location != "dpa" && opts.input_location != "host")
		fail("--input-location must be dpa or host");
	if (opts.ring_output_bytes > 0 && opts.ring_output_bytes < 64)
		fail("--ring-output-bytes must be at least 64 bytes");
	if (opts.ring_output_location != "host" && opts.ring_output_location != "dpa")
		fail("--ring-output-location must be host or dpa");
	if (opts.ring_output_bytes == 0 && opts.ring_output_location != "host")
		fail("--ring-output-location dpa requires --ring-output-bytes");
	if (opts.ring_partition_mode == PRINTTUP_DPA_RING_PARTITION_PER_WORKER &&
		opts.schedule_mode != PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
		fail("--ring-partition per-worker requires --schedule static-chunked");
	if (opts.ring_partition_mode == PRINTTUP_DPA_RING_PARTITION_PER_WORKER &&
		opts.ring_output_bytes == 0)
		fail("--ring-partition per-worker requires --ring-output-bytes");
	if ((opts.input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_OFFSET ||
		 opts.input_map_mode == PRINTTUP_DPA_INPUT_MAP_WORKER_SHARD) &&
		opts.schedule_mode != PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
		fail("--input-map worker-offset/worker-shard requires --schedule static-chunked");
	return opts;
}

class FlexioHarness {
public:
	explicit FlexioHarness(const std::string &device_name)
	{
		flexio_status st = flexio_version_set(FLEXIO_VER_USED);
		if (st != FLEXIO_STATUS_SUCCESS)
			fail("flexio_version_set failed");

		struct ibv_device **dev_list = ibv_get_device_list(nullptr);
		if (dev_list == nullptr)
			fail("ibv_get_device_list failed");

		struct ibv_device *selected = nullptr;
		for (int i = 0; dev_list[i] != nullptr; i++)
		{
			if (device_name == ibv_get_device_name(dev_list[i]))
			{
				selected = dev_list[i];
				break;
			}
		}
		if (selected == nullptr)
		{
			std::cerr << "available IB devices:";
			for (int i = 0; dev_list[i] != nullptr; i++)
				std::cerr << " " << ibv_get_device_name(dev_list[i]);
			std::cerr << "\n";
			ibv_free_device_list(dev_list);
			fail("requested IB device not found: " + device_name);
		}

		ibv_ctx_ = ibv_open_device(selected);
		ibv_free_device_list(dev_list);
		if (ibv_ctx_ == nullptr)
			fail("ibv_open_device failed");

		pd_ = ibv_alloc_pd(ibv_ctx_);
		if (pd_ == nullptr)
			fail("ibv_alloc_pd failed");

		flexio_process_attr process_attr{};
		st = flexio_process_create(ibv_ctx_, printtup_dpa_device, &process_attr, &process_);
		if (st != FLEXIO_STATUS_SUCCESS)
			fail("flexio_process_create failed");

		st = flexio_window_create(process_, pd_, &window_);
		if (st != FLEXIO_STATUS_SUCCESS)
			fail("flexio_window_create failed");
	}

	~FlexioHarness()
	{
		if (output_mr_ != nullptr)
			ibv_dereg_mr(output_mr_);
		if (pd_ != nullptr)
			ibv_dealloc_pd(pd_);
		if (ibv_ctx_ != nullptr)
			ibv_close_device(ibv_ctx_);
	}

	flexio_process *process() const { return process_; }
	uint32_t window_id() const { return flexio_window_get_id(window_); }
	uint32_t output_lkey() const { return output_mr_->lkey; }

	void register_output(void *addr, size_t len)
	{
		output_mr_ = ibv_reg_mr(pd_, addr, len, IBV_ACCESS_LOCAL_WRITE);
		if (output_mr_ == nullptr)
			fail("ibv_reg_mr for output buffer failed");
	}

	flexio_uintptr_t copy_to_dpa(const void *data, size_t len)
	{
		flexio_uintptr_t daddr = 0;
		flexio_status st = flexio_copy_from_host(process_, const_cast<void *>(data), len, &daddr);
		if (st != FLEXIO_STATUS_SUCCESS)
			fail("flexio_copy_from_host failed");
		return daddr;
	}

	flexio_uintptr_t allocate_dpa(size_t len)
	{
		flexio_uintptr_t daddr = 0;
		flexio_status st = flexio_buf_dev_alloc(process_, len, &daddr);
		if (st != FLEXIO_STATUS_SUCCESS || daddr == 0)
			fail("flexio_buf_dev_alloc failed");
		return daddr;
	}

	void call_set_batch_config(flexio_uintptr_t config_daddr)
	{
		uint64_t func_ret = 0;
		flexio_status st = flexio_process_call(process_, &printtup_dpa_set_batch_config,
											   &func_ret, config_daddr);
		if (st != FLEXIO_STATUS_SUCCESS || func_ret != 0)
			fail("printtup_dpa_set_batch_config RPC failed");
	}

private:
	ibv_context *ibv_ctx_ = nullptr;
	ibv_pd *pd_ = nullptr;
	flexio_process *process_ = nullptr;
	flexio_window *window_ = nullptr;
	ibv_mr *output_mr_ = nullptr;
};

static uint64_t
compute_input_working_set_bytes(const DumpData &dump, size_t input_working_set_rows,
								bool materialize_input_working_set)
{
	auto row_normalized_bytes = [](const RowRecord &row) -> uint64_t {
		uint64_t bytes = 0;

		for (uint32_t attr_index = 0; attr_index < row.natts; attr_index++)
		{
			if (!row.fields[attr_index].is_null)
				bytes += row.fields[attr_index].normalized.size();
		}
		return bytes;
	};

	if (!materialize_input_working_set)
	{
		uint64_t bytes = 0;

		for (const RowRecord &row : dump.rows)
			bytes += row_normalized_bytes(row);
		return bytes;
	}

	uint64_t base_bytes = 0;
	std::vector<uint64_t> prefix_bytes(dump.rows.size() + 1, 0);
	for (size_t row_index = 0; row_index < dump.rows.size(); row_index++)
	{
		base_bytes += row_normalized_bytes(dump.rows[row_index]);
		prefix_bytes[row_index + 1] = base_bytes;
	}
	return (uint64_t) (input_working_set_rows / dump.rows.size()) * base_bytes +
		prefix_bytes[input_working_set_rows % dump.rows.size()];
}

/*
 * Detect the TPC-H lineitem dump shape used by the DPA serializer benchmark.
 * The DPA-side plan is deliberately keyed by the serializer sequence, not by a
 * relation name that is not present in the compact schema metadata. If another
 * 16-column schema has exactly this send-function layout, the specialized plan
 * would still be semantically valid for this standalone serializer because it
 * only relies on send-function behavior and non-null normalized payloads.
 */
static uint32_t
schema_plan_id(const SchemaRecord &schema)
{
	static constexpr uint32_t lineitem_serializers[] = {
		PRINTTUP_DPA_SERIALIZER_INT4,
		PRINTTUP_DPA_SERIALIZER_INT4,
		PRINTTUP_DPA_SERIALIZER_INT4,
		PRINTTUP_DPA_SERIALIZER_INT4,
		PRINTTUP_DPA_SERIALIZER_NUMERIC,
		PRINTTUP_DPA_SERIALIZER_NUMERIC,
		PRINTTUP_DPA_SERIALIZER_NUMERIC,
		PRINTTUP_DPA_SERIALIZER_NUMERIC,
		PRINTTUP_DPA_SERIALIZER_TEXTLIKE,
		PRINTTUP_DPA_SERIALIZER_TEXTLIKE,
		PRINTTUP_DPA_SERIALIZER_DATE,
		PRINTTUP_DPA_SERIALIZER_DATE,
		PRINTTUP_DPA_SERIALIZER_DATE,
		PRINTTUP_DPA_SERIALIZER_TEXTLIKE,
		PRINTTUP_DPA_SERIALIZER_TEXTLIKE,
		PRINTTUP_DPA_SERIALIZER_TEXTLIKE
	};

	if (schema.natts != sizeof(lineitem_serializers) / sizeof(lineitem_serializers[0]))
		return PRINTTUP_DPA_PLAN_GENERIC;
	for (uint32_t i = 0; i < schema.natts; i++)
	{
		if (schema.attrs[i].serializer != lineitem_serializers[i])
			return PRINTTUP_DPA_PLAN_GENERIC;
	}
	return PRINTTUP_DPA_PLAN_TPCH_LINEITEM;
}

static std::vector<DpaPreparedRow>
prepare_rows(FlexioHarness &harness, const DumpData &dump, uint8_t *output_base,
			 size_t output_slot_size, uint32_t output_capacity, uint32_t iterations,
			 size_t input_working_set_rows, size_t output_slot_rows,
			 bool materialize_input_working_set, bool input_on_host,
			 uint8_t *host_input_base, uint64_t host_input_capacity,
			 std::vector<uint8_t> *dpa_input_staging,
			 std::vector<PrinttupDpaTask> *tasks,
			 std::vector<PrinttupDpaField> *fields,
			 uint64_t *input_working_set_bytes)
{
	std::vector<std::vector<uint64_t>> base_normalized_daddrs;
	std::vector<DpaPreparedRow> prepared;
	uint64_t input_bytes = 0;
	auto store_normalized = [&](const std::vector<uint8_t> &normalized) -> uint64_t {
		uint64_t addr_or_offset;

		if (input_on_host)
		{
			if (host_input_base == nullptr ||
				input_bytes > host_input_capacity ||
				normalized.size() > host_input_capacity - input_bytes)
				fail("host input working-set buffer is too small");
			addr_or_offset = input_bytes;
			memcpy(host_input_base + input_bytes, normalized.data(), normalized.size());
			input_bytes += normalized.size();
			return addr_or_offset;
		}

		/*
		 * DPA-heap input used to call flexio_copy_from_host() once per normalized
		 * field. That produced millions of tiny heap allocations for materialized
		 * lineitem input and made the timed DPA access path chase scattered heap
		 * objects. Stage all input bytes contiguously instead; main() copies this
		 * blob to DPA heap once and patches offsets into absolute DPA addresses.
		 */
		if (dpa_input_staging == nullptr)
			fail("internal error: missing DPA input staging buffer");
		addr_or_offset = input_bytes;
		dpa_input_staging->insert(dpa_input_staging->end(),
								  normalized.begin(), normalized.end());
		input_bytes += normalized.size();
		return addr_or_offset;
	};

	/*
	 * The original benchmark shape copied one DPA-resident normalized payload per
	 * parsed dump row, then pointed repeated prepared rows back to those shared
	 * inputs. Keep that default for historical comparisons. The materialized mode
	 * below intentionally duplicates normalized payloads per prepared input row,
	 * so --input-working-set-rows changes the tuple-input footprint seen by DPA
	 * loads independently of the output/status slot reuse pool.
	 */
	if (!materialize_input_working_set)
	{
		base_normalized_daddrs.resize(dump.rows.size());
		for (size_t row_index = 0; row_index < dump.rows.size(); row_index++)
		{
			const RowRecord &row = dump.rows[row_index];
			base_normalized_daddrs[row_index].resize(row.natts);
			for (uint32_t attr_index = 0; attr_index < row.natts; attr_index++)
			{
				if (!row.fields[attr_index].is_null)
				{
					const std::vector<uint8_t> &normalized =
						row.fields[attr_index].normalized;

					base_normalized_daddrs[row_index][attr_index] =
						store_normalized(normalized);
				}
			}
		}
	}

	if (tasks == nullptr)
		fail("internal error: missing contiguous DPA task staging vector");
	if (fields == nullptr)
		fail("internal error: missing compact DPA field staging vector");
	tasks->clear();
	tasks->reserve(input_working_set_rows);
	fields->clear();
	prepared.reserve(input_working_set_rows);
	for (size_t row_index = 0; row_index < input_working_set_rows; row_index++)
	{
		size_t base_row_index = row_index % dump.rows.size();
		const RowRecord &row = dump.rows[base_row_index];
		SchemaRecord *schema = nullptr;
		for (const auto &candidate : dump.schemas)
		{
			if (candidate.schema_id == row.schema_id)
			{
				schema = const_cast<SchemaRecord *>(&candidate);
				break;
			}
		}
		if (schema == nullptr)
			fail("internal error: missing schema during DPA prep");

		PrinttupDpaTask task{};
		task.row_id = row.row_id;
		task.row_index = (uint32_t) row_index;
		task.natts = row.natts;
		task.words_bigendian = dump.header.words_bigendian ? 1U : 0U;
		task.output_capacity = output_capacity;
		task.iterations = iterations;
		task.plan_id = schema_plan_id(*schema);
		if (row.row_payload.size() > UINT32_MAX)
			fail("row payload is too large for DPA descriptor");
		task.row_payload_len = (uint32_t) row.row_payload.size();
		task.window_id = harness.window_id();
		task.mkey = harness.output_lkey();
		task.fields_daddr = (uint64_t) fields->size() * sizeof(PrinttupDpaField);
		/*
		 * Batched mode now chooses the output slot from logical_index modulo
		 * output_slot_rows. Keep output_haddr valid for the legacy one-row RPC path
		 * by mapping oversized input-task pools back into the output slot pool.
		 */
		task.output_haddr = (uint64_t) (uintptr_t)
			(output_base + (row_index % output_slot_rows) * output_slot_size);

		for (uint32_t i = 0; i < row.natts; i++)
		{
			PrinttupDpaField field{};

			field.serializer = schema->attrs[i].serializer;
			field.is_null = row.fields[i].is_null ? 1U : 0U;
			if (!row.fields[i].is_null)
			{
				const std::vector<uint8_t> &normalized = row.fields[i].normalized;
				const std::vector<uint8_t> &serialized = row.fields[i].serialized;

				field.normalized_len = (uint32_t) normalized.size();
				if (serialized.size() > UINT32_MAX)
					fail("serialized field payload is too large for DPA descriptor");
				/*
				 * The normal DPA serializer recomputes this length as it appends
				 * bytes. Benchmark-only device builds use the dump's captured
				 * serialized length to skip selected serializer bodies while still
				 * preserving DataRow length accounting.
				 */
				field.serialized_len = (uint32_t) serialized.size();
				if (materialize_input_working_set)
				{
					/*
					 * Benchmark-only mode: duplicate each prepared row's tuple
					 * input so locality changes with the configured working-set
					 * size. The input may live in DPA heap or in the registered
					 * ARM host window, but preparation remains outside the timed
					 * DPA hot path.
					 */
					field.normalized_daddr = store_normalized(normalized);
				}
				else
					field.normalized_daddr = base_normalized_daddrs[base_row_index][i];
			}
			fields->push_back(field);
		}
		if (task.plan_id == PRINTTUP_DPA_PLAN_TPCH_LINEITEM && row.natts == 16)
		{
			uint64_t split_payload_len = 0;

			/*
			 * Split-output metadata is now prepared once on the Arm side from the
			 * captured send-function payload lengths. The DPA still serializes and
			 * writes every payload byte, but it does not need to reload selected
			 * field descriptors just to produce row-local metadata.
			 */
			for (uint32_t i = 0; i < row.natts; i++)
			{
				if (row.fields[i].is_null)
					fail("lineitem split-output benchmark does not support null fields");
				split_payload_len += row.fields[i].serialized.size();
			}
			if (split_payload_len > UINT32_MAX)
				fail("lineitem split payload is too large for DPA descriptor");
			if (row.fields[5].serialized.size() > UINT16_MAX ||
				row.fields[6].serialized.size() > UINT16_MAX ||
				row.fields[7].serialized.size() > UINT16_MAX ||
				row.fields[15].serialized.size() > UINT16_MAX)
				fail("lineitem split variable payload length exceeds 16-bit metadata");
			task.split_payload_len = (uint32_t) split_payload_len;
			task.split_extendedprice_len =
				(uint16_t) row.fields[5].serialized.size();
			task.split_discount_len =
				(uint16_t) row.fields[6].serialized.size();
			task.split_tax_len =
				(uint16_t) row.fields[7].serialized.size();
			task.split_comment_len =
				(uint16_t) row.fields[15].serialized.size();
		}

		DpaPreparedRow out;
		out.row_id = row.row_id;
		out.row_index = (uint32_t) row_index;
		out.expected_row_payload = row.row_payload;
		tasks->push_back(task);
		prepared.push_back(std::move(out));
	}

	if (input_working_set_bytes != nullptr)
		*input_working_set_bytes = input_bytes;
	return prepared;
}

static RunResult
run_once(FlexioHarness &harness, const std::vector<DpaPreparedRow> &rows,
		 uint8_t *output_base, size_t output_slot_size, uint32_t output_capacity,
		 int threads, int batch_size, size_t requested_rows_per_task,
		 bool rows_per_task_all, uint32_t iterations, size_t logical_rows,
		 size_t output_slot_rows, bool window_writeback, uint64_t ring_slots,
		 uint32_t poll_us, int timeout_seconds, uint64_t input_working_set_bytes,
		 uint32_t schedule_mode, uint32_t ring_partition_mode,
		 uint32_t input_map_mode)
{
	const size_t max_tasks_per_cmdq = 16384;
	RunResult result;
	result.threads = threads;
	result.rows = logical_rows;
	result.working_set_rows = output_slot_rows;
	result.input_working_set_rows = rows.size();
	result.rows_per_task = rows_per_task_all ? 0 : requested_rows_per_task;
	result.ring_slots = ring_slots;
	result.ring_partition_mode = ring_partition_mode;
	result.input_map_mode = input_map_mode;
	result.input_working_set_bytes = input_working_set_bytes;
	result.schedule_mode = schedule_mode;

	memset(output_base, 0, output_slot_size * output_slot_rows);

	size_t max_batch_size = std::max((size_t) 1, max_tasks_per_cmdq / (size_t) threads);
	int effective_batch_size;
	if (schedule_mode == PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
	{
		/*
		 * Static chunked mode submits exactly one CmdQ item per logical worker.
		 * A batch size of one keeps the CmdQ capacity tied to worker count and
		 * avoids hiding accidental per-worker over-submission.
		 */
		if ((size_t) threads > max_tasks_per_cmdq)
			fail("--schedule static-chunked exceeds current CmdQ task capacity");
		if (ring_partition_mode == PRINTTUP_DPA_RING_PARTITION_PER_WORKER &&
			ring_slots != 0 && ring_slots < (uint64_t) threads)
			fail("--ring-partition per-worker needs at least one ring slot per worker");
		effective_batch_size = 1;
	}
	else if (batch_size > 0)
	{
		/*
		 * The CmdQ QP depth limit is only a per-submission capacity limit. Total
		 * logical row count can exceed it because the submission loop below feeds
		 * the reused CmdQ in chunks. Clamp an oversized explicit batch instead of
		 * failing the run, so "--batch-size huge" still means "use the largest
		 * safe chunk size for this worker count".
		 */
		if ((size_t) batch_size > max_batch_size)
		{
			std::cerr << "warning: clamping --batch-size " << batch_size
					  << " to " << max_batch_size
					  << " for " << threads
					  << " workers so workers * batch_size stays <= "
					  << max_tasks_per_cmdq << "\n";
			effective_batch_size = (int) max_batch_size;
		}
		else
			effective_batch_size = batch_size;
	}
	else
	{
		effective_batch_size =
			(int) std::min(max_batch_size,
						   (output_slot_rows + (size_t) threads - 1) / (size_t) threads);
	}
	size_t max_cmdq_tasks_per_chunk = (size_t) threads * (size_t) effective_batch_size;
	if (max_cmdq_tasks_per_chunk == 0)
		fail("internal error: zero CmdQ task capacity");
	if (max_cmdq_tasks_per_chunk > max_tasks_per_cmdq)
		max_cmdq_tasks_per_chunk = max_tasks_per_cmdq;

	if (input_map_mode == PRINTTUP_DPA_INPUT_MAP_LOGICAL_MODULO ||
		schedule_mode != PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
	{
		uint64_t base_bytes = 0;
		std::vector<uint64_t> prefix_bytes(rows.size() + 1, 0);
		for (size_t i = 0; i < rows.size(); i++)
		{
			base_bytes += rows[i].expected_row_payload.size();
			prefix_bytes[i + 1] = base_bytes;
		}
		result.payload_bytes =
			(uint64_t) (logical_rows / rows.size()) * base_bytes +
			prefix_bytes[logical_rows % rows.size()];
	}
	else
	{
		/*
		 * Worker-offset mapping intentionally changes which prepared row a logical
		 * row serializes. Compute exact byte accounting outside the timed region so
		 * throughput remains comparable to what the DPA actually wrote.
		 */
		for (size_t logical_index = 0; logical_index < logical_rows; logical_index++)
		{
			size_t prepared_index =
				prepared_index_for_logical(logical_index, rows.size(), schedule_mode,
										   input_map_mode, threads,
										   requested_rows_per_task);
			result.payload_bytes += rows[prepared_index].expected_row_payload.size();
		}
	}
	result.serializer_work_bytes = result.payload_bytes * (uint64_t) iterations;

	flexio_cmdq_attr attr{};
	attr.workers = threads;
	attr.batch_size = effective_batch_size;
	attr.state = FLEXIO_CMDQ_STATE_PENDING;

	flexio_cmdq *cmdq = nullptr;
	if (flexio_cmdq_create(harness.process(), &attr, &cmdq) != FLEXIO_STATUS_SUCCESS)
		fail("flexio_cmdq_create failed");

	/*
	 * Build the queue once, then feed it chunk by chunk. Contiguous mode keeps
	 * the original many-small-range behavior. Static chunked mode instead queues
	 * one long-lived RPC per logical worker; each worker walks fixed row chunks
	 * in a deterministic strided pattern, so no DPA-side atomics are needed.
	 */
	bool started = false;
	uint64_t start_us = 0;
	if (schedule_mode == PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED)
	{
		uint64_t enqueue_start_us = now_us();

		if (logical_rows > UINT32_MAX)
			fail("logical row range exceeds current 32-bit DPA batch argument");
		for (int worker_index = 0; worker_index < threads; worker_index++)
		{
			uint64_t packed_worker =
				((uint64_t) (uint32_t) threads << 32) |
				(uint64_t) (uint32_t) worker_index;

			if (flexio_cmdq_task_add(cmdq, printtup_dpa_reserialize_static_worker,
									 packed_worker) != FLEXIO_STATUS_SUCCESS)
				fail("flexio_cmdq_task_add failed");
		}
		result.enqueue_elapsed_us += now_us() - enqueue_start_us;
		result.cmdq_chunks = 1;
		result.cmdq_tasks = (uint64_t) threads;

		start_us = now_us();
		started = true;
		uint64_t state_start_us = now_us();
		if (flexio_cmdq_state_running(cmdq) != FLEXIO_STATUS_SUCCESS)
			fail("flexio_cmdq_state_running failed");
		result.start_elapsed_us += now_us() - state_start_us;

		uint64_t drain_start_us = now_us();
		while (!flexio_cmdq_is_empty(cmdq))
		{
			if (poll_us > 0)
				std::this_thread::sleep_for(std::chrono::microseconds(poll_us));
			uint64_t elapsed_us = now_us() - start_us;
			if (elapsed_us > (uint64_t) timeout_seconds * 1000000ULL)
				fail("timed out waiting for DPA CmdQ");
		}
		result.drain_wait_us += now_us() - drain_start_us;
	}
	else
	{
		for (size_t chunk_start = 0; chunk_start < logical_rows;)
		{
			size_t remaining_rows = logical_rows - chunk_start;
			size_t fixed_rows_per_task = rows_per_task_all ? 1 : requested_rows_per_task;
			size_t max_rows_per_chunk;
			if (rows_per_task_all || fixed_rows_per_task >= output_slot_rows)
				max_rows_per_chunk = output_slot_rows;
			else
				max_rows_per_chunk =
					std::min(output_slot_rows, max_cmdq_tasks_per_chunk * fixed_rows_per_task);
			size_t chunk_rows = std::min(remaining_rows, max_rows_per_chunk);
			size_t effective_rows_per_task = rows_per_task_all ?
				(chunk_rows + (size_t) threads - 1) / (size_t) threads :
				fixed_rows_per_task;
			size_t cmdq_tasks = (chunk_rows + effective_rows_per_task - 1) /
				effective_rows_per_task;

			if (chunk_rows == 0 || effective_rows_per_task == 0 || cmdq_tasks == 0)
				fail("internal error: zero DPA row-batch scheduling unit");
			if (cmdq_tasks > max_cmdq_tasks_per_chunk)
				fail("internal error: row batching exceeded CmdQ task capacity");
			if (chunk_start > UINT32_MAX || chunk_rows > UINT32_MAX)
				fail("logical row range exceeds current 32-bit DPA batch argument");

			uint64_t enqueue_start_us = now_us();
			for (size_t task_index = 0; task_index < cmdq_tasks; task_index++)
			{
				size_t task_row_start = chunk_start + task_index * effective_rows_per_task;
				size_t task_row_count =
					std::min(effective_rows_per_task, logical_rows - task_row_start);
				uint64_t packed_range =
					((uint64_t) (uint32_t) task_row_count << 32) |
					(uint64_t) (uint32_t) task_row_start;

				if (flexio_cmdq_task_add(cmdq, printtup_dpa_reserialize_batch,
										 packed_range) !=
					FLEXIO_STATUS_SUCCESS)
					fail("flexio_cmdq_task_add failed");
			}
			result.enqueue_elapsed_us += now_us() - enqueue_start_us;

			if (!started)
			{
				start_us = now_us();
				started = true;
			}
			uint64_t state_start_us = now_us();
			if (flexio_cmdq_state_running(cmdq) != FLEXIO_STATUS_SUCCESS)
				fail("flexio_cmdq_state_running failed");
			result.start_elapsed_us += now_us() - state_start_us;

			uint64_t drain_start_us = now_us();
			while (!flexio_cmdq_is_empty(cmdq))
			{
				if (poll_us > 0)
					std::this_thread::sleep_for(std::chrono::microseconds(poll_us));
				uint64_t elapsed_us = now_us() - start_us;
				if (elapsed_us > (uint64_t) timeout_seconds * 1000000ULL)
					fail("timed out waiting for DPA CmdQ");
			}
			result.drain_wait_us += now_us() - drain_start_us;

			result.cmdq_chunks++;
			result.cmdq_tasks += cmdq_tasks;
			chunk_start += chunk_rows;
		}
	}
	if (!started)
		fail("internal error: DPA CmdQ was never started");
	result.host_elapsed_us = now_us() - start_us;

	size_t verify_rows = std::min(output_slot_rows, logical_rows);
	if (!window_writeback)
	{
		/*
		 * Without DPA window writeback, the ARM side intentionally does not have
		 * coherent result headers or output bytes. Use this only after a matching
		 * writeback-enabled correctness run has passed.
		 */
		result.verification_skipped = verify_rows;
	}
	else
	{
		for (size_t verify_index = 0; verify_index < verify_rows; verify_index++)
		{
			size_t final_logical_index =
				verify_index + ((logical_rows - 1 - verify_index) / output_slot_rows) *
				output_slot_rows;
			size_t expected_prepared_index =
				prepared_index_for_logical(final_logical_index, rows.size(), schedule_mode,
										   input_map_mode, threads,
										   requested_rows_per_task);
			const auto &row = rows[expected_prepared_index];
			uint8_t *slot = output_base + verify_index * output_slot_size;
			auto *header = reinterpret_cast<PrinttupDpaResultHeader *>(slot);
			uint8_t *body = slot + sizeof(PrinttupDpaResultHeader);

			if (header->magic != PRINTTUP_DPA_SLOT_MAGIC ||
				header->status != PRINTTUP_DPA_STATUS_OK ||
				header->error != PRINTTUP_DPA_ERR_NONE)
			{
				result.status_errors++;
				result.mismatches++;
				if (result.status_errors <= 4)
				{
					std::cerr << "row status error verify_index=" << verify_index
							  << " expected_row_id=" << row.row_id
							  << " magic=0x" << std::hex << header->magic << std::dec
							  << " status=" << header->status
							  << " error=" << header->error
							  << " output_len=" << header->output_len
							  << " header_row_id=" << header->row_id
							  << " header_row_index=" << header->row_index << "\n";
				}
				continue;
			}

			result.dpa_cycles_sum += header->cycles;
			result.dpa_cycles_max = std::max(result.dpa_cycles_max, header->cycles);

			if (header->output_len != row.expected_row_payload.size() ||
				header->output_len > output_capacity)
			{
				result.mismatches++;
				if (result.mismatches <= 4)
				{
					std::cerr << "row mismatch row_id=" << row.row_id
							  << " expected_len=" << row.expected_row_payload.size()
							  << " actual_len=" << header->output_len << "\n";
				}
			}
			else if (ring_slots == 0 &&
					 memcmp(body, row.expected_row_payload.data(),
							row.expected_row_payload.size()) != 0)
			{
				result.mismatches++;
				if (result.mismatches <= 4)
					std::cerr << "row body mismatch row_id=" << row.row_id << "\n";
			}
			else if (ring_slots != 0)
				result.verification_skipped++;
		}
	}

	if (result.host_elapsed_us > 0)
	{
		result.payload_throughput_mb_s =
			((double) result.payload_bytes / 1000000.0) /
			((double) result.host_elapsed_us / 1000000.0);
		result.throughput_mb_s =
			((double) result.serializer_work_bytes / 1000000.0) /
			((double) result.host_elapsed_us / 1000000.0);
	}
	if (result.dpa_cycles_sum > 0)
	{
		/*
		 * Each row task records cycles around its repeated serializer loop.
		 * Dividing aggregate task cycles by worker count is an approximation of
		 * DPA execution time when the CmdQ workers are reasonably balanced. The
		 * host-observed throughput remains the end-to-end number.
		 */
		double dpa_seconds =
			((double) result.dpa_cycles_sum / (double) threads) / 1800000000.0;
		result.dpa_throughput_mb_s =
			((double) result.serializer_work_bytes / 1000000.0) / dpa_seconds;
	}

	(void) flexio_cmdq_destroy(cmdq);
	return result;
}

} // namespace

int
main(int argc, char **argv)
{
	try
	{
		Options opts = parse_options(argc, argv);
		std::vector<uint8_t> data = read_file(opts.dump_path);
		DumpData dump = parse_dump(data, opts.max_rows);

		size_t max_payload = 0;
		uint64_t total_payload_bytes = 0;
		for (const auto &row : dump.rows)
		{
			max_payload = std::max(max_payload, row.row_payload.size());
			total_payload_bytes += row.row_payload.size();
		}
		uint32_t output_capacity = (uint32_t) align_up(max_payload + 64, 64);
		size_t output_slot_size =
			align_up(sizeof(PrinttupDpaResultHeader) + output_capacity, 64);
			size_t logical_rows = opts.logical_rows == 0 ? dump.rows.size() : opts.logical_rows;
			if (logical_rows == 0)
				fail("logical row count is zero");
			if (logical_rows > UINT32_MAX)
				fail("logical row count exceeds current 32-bit DPA batch argument");
			if (opts.schedule_mode == PRINTTUP_DPA_SCHEDULE_STATIC_CHUNKED &&
				opts.rows_per_task > UINT32_MAX)
				fail("--rows-per-task exceeds current 32-bit static chunk descriptor");
			size_t working_set_rows = opts.working_set_rows == 0 ?
				std::min(logical_rows, (size_t) 32768) :
				opts.working_set_rows;
		if (working_set_rows == 0)
			fail("working set row count is zero");
		if (working_set_rows > logical_rows)
			working_set_rows = logical_rows;
		size_t input_working_set_rows = opts.input_working_set_rows == 0 ?
			working_set_rows : opts.input_working_set_rows;
		if (input_working_set_rows == 0)
			fail("input working set row count is zero");
		if (input_working_set_rows > logical_rows)
			input_working_set_rows = logical_rows;
		size_t stats_bytes = output_slot_size * working_set_rows;
		size_t ring_entry_size = align_up(output_capacity, 64);
		uint64_t ring_slots = opts.ring_output_bytes == 0 ? 0 :
			opts.ring_output_bytes / ring_entry_size;
		if (opts.ring_output_bytes > 0 && ring_slots == 0)
			fail("--ring-output-bytes is too small for one output ring entry");
		size_t ring_bytes = (size_t) ring_slots * ring_entry_size;
		bool ring_on_dpa = ring_slots != 0 && opts.ring_output_location == "dpa";
		size_t host_ring_bytes = ring_on_dpa ? 0 : ring_bytes;
		bool input_on_host = opts.input_location == "host";
		uint64_t estimated_input_working_set_bytes =
			compute_input_working_set_bytes(dump, input_working_set_rows,
											opts.materialize_input_working_set);
		size_t host_input_offset = align_up(stats_bytes + host_ring_bytes, 64);
		/*
		 * Leave a guard tail after host-window input bytes. The DPA-side load path can
		 * fetch at cache-line/window granularity, so ending the registered MR exactly
		 * at the final varlena byte produced BAD_INPUT on the last prepared rows.
		 */
		size_t host_input_bytes = input_on_host ?
			align_up((size_t) estimated_input_working_set_bytes + 4096, 64) : 0;
		size_t output_bytes = host_input_offset + host_input_bytes;

		void *output_alloc = nullptr;
		if (posix_memalign(&output_alloc, 64, output_bytes) != 0)
			fail("posix_memalign for output buffer failed");
		auto *output_base = static_cast<uint8_t *>(output_alloc);
		memset(output_base, 0, output_bytes);
		uint8_t *host_input_base = input_on_host ?
			output_base + host_input_offset : nullptr;

			FlexioHarness harness(opts.device_name);
			harness.register_output(output_base, output_bytes);
			flexio_uintptr_t ring_daddr = 0;
			if (ring_on_dpa)
				ring_daddr = harness.allocate_dpa(ring_bytes);
		uint64_t input_working_set_bytes = 0;
		std::vector<PrinttupDpaTask> prepared_tasks;
		std::vector<PrinttupDpaField> prepared_fields;
		uint64_t control_setup_start_us = now_us();
		uint64_t control_setup_elapsed_us;
		/*
		 * For DPA-heap input, prepare_rows records normalized_daddr as offsets into
		 * this staging blob. After preparation we copy the blob once to DPA heap and
		 * patch every non-null field offset into an absolute DPA address. Host-window
		 * input keeps using offsets from host_input_base instead.
		 */
		std::vector<uint8_t> dpa_input_staging;
		if (!input_on_host)
			dpa_input_staging.reserve((size_t) estimated_input_working_set_bytes);
		std::vector<DpaPreparedRow> prepared =
			prepare_rows(harness, dump, output_base, output_slot_size, output_capacity,
						 opts.iterations, input_working_set_rows, working_set_rows,
						 opts.materialize_input_working_set, input_on_host,
						 host_input_base, host_input_bytes,
						 input_on_host ? nullptr : &dpa_input_staging,
						 &prepared_tasks,
						 &prepared_fields,
						 &input_working_set_bytes);
		if (input_working_set_bytes != estimated_input_working_set_bytes)
			fail("internal error: input byte precompute disagreed with preparation");
		if (!input_on_host)
		{
			if (dpa_input_staging.size() != input_working_set_bytes)
				fail("internal error: DPA input staging byte count disagreed with preparation");
			flexio_uintptr_t input_daddr =
				dpa_input_staging.empty() ? 0 :
				harness.copy_to_dpa(dpa_input_staging.data(), dpa_input_staging.size());
			for (PrinttupDpaField &field : prepared_fields)
			{
				if (!field.is_null)
					field.normalized_daddr += input_daddr;
			}
		}

		/*
		 * Copy compact descriptors as contiguous DPA arrays. The older version copied
		 * a full PrinttupDpaTask with 64 inline field slots per prepared row and then
		 * indexed through a pointer table. The current layout uses one compact task
		 * header per row plus one tightly packed field array containing only natts
		 * entries per row; fields_daddr is patched from a byte offset to a DPA address
		 * after the field array is copied.
		 */
		flexio_uintptr_t field_array_daddr = prepared_fields.empty() ? 0 :
			harness.copy_to_dpa(prepared_fields.data(),
								prepared_fields.size() * sizeof(prepared_fields[0]));
		for (PrinttupDpaTask &task : prepared_tasks)
			task.fields_daddr += field_array_daddr;
		flexio_uintptr_t row_task_array_daddr =
			harness.copy_to_dpa(prepared_tasks.data(),
								prepared_tasks.size() * sizeof(prepared_tasks[0]));
		PrinttupDpaBatchConfig batch_config{};
		batch_config.row_task_array_daddr = row_task_array_daddr;
		batch_config.output_base_haddr = (uint64_t) (uintptr_t) output_base;
		batch_config.output_slot_size = output_slot_size;
		batch_config.ring_base_offset = stats_bytes;
		batch_config.ring_base_daddr = ring_daddr;
		batch_config.ring_entry_size = ring_entry_size;
		batch_config.ring_slots = ring_slots;
		batch_config.input_base_offset = host_input_offset;
		batch_config.window_id = harness.window_id();
		batch_config.mkey = harness.output_lkey();
			batch_config.prepared_rows = (uint32_t) prepared.size();
			batch_config.window_writeback = opts.window_writeback ? 1U : 0U;
			batch_config.output_mode = ring_slots == 0 ?
				PRINTTUP_DPA_OUTPUT_SLOT : PRINTTUP_DPA_OUTPUT_RING;
			batch_config.ring_location = ring_on_dpa ?
				PRINTTUP_DPA_RING_DPA_HEAP : PRINTTUP_DPA_RING_HOST_WINDOW;
		batch_config.output_slot_rows = (uint32_t) working_set_rows;
		batch_config.input_location = input_on_host ?
			PRINTTUP_DPA_INPUT_HOST_WINDOW : PRINTTUP_DPA_INPUT_DPA_HEAP;
		batch_config.ring_partition_mode = opts.ring_partition_mode;
		batch_config.input_map_mode = opts.input_map_mode;
			/*
			 * Static chunked mode uses --rows-per-task as the per-worker chunk size:
			 * each worker handles that many adjacent rows, then jumps by
			 * workers * static_chunk_rows. Contiguous mode leaves these values
			 * harmlessly populated for easier CSV/debug correlation.
			 */
			batch_config.static_chunk_rows = opts.rows_per_task_all ?
				(uint32_t) 0 : (uint32_t) opts.rows_per_task;
			batch_config.static_logical_rows = (uint32_t) logical_rows;
			flexio_uintptr_t batch_config_daddr =
				harness.copy_to_dpa(&batch_config, sizeof(batch_config));
			harness.call_set_batch_config(batch_config_daddr);
			control_setup_elapsed_us = now_us() - control_setup_start_us;

			std::ofstream csv(opts.csv_path);
			if (!csv)
				fail("failed to open CSV output: " + opts.csv_path);
		csv << "threads,iterations,rows,working_set_rows,input_working_set_rows,"
			   "rows_per_dpa_task,schedule,window_writeback,input_materialization,"
			   "input_location,input_map,input_working_set_bytes,"
			   "ring_output_location,ring_partition,ring_output_bytes,ring_slots,"
			   "cmdq_chunks,cmdq_tasks,payload_bytes,"
				   "serializer_work_bytes,host_elapsed_us,"
			   "control_setup_elapsed_us,end_to_end_elapsed_us,"
			   "enqueue_elapsed_us,start_elapsed_us,drain_wait_us,"
			   "host_payload_throughput_mb_s,host_serializer_work_throughput_mb_s,"
			   "end_to_end_serializer_work_throughput_mb_s,"
			   "dpa_throughput_mb_s,"
			   "dpa_cycles_sum,dpa_cycles_max,avg_dpa_cycles_per_row,"
			   "mismatches,status_errors,verification_skipped\n";

			std::cout << "dump=" << opts.dump_path
				  << " parsed_rows=" << dump.rows.size()
				  << " working_set_rows=" << working_set_rows
				  << " input_working_set_rows=" << prepared.size()
				  << " logical_rows=" << logical_rows
			  << " input_materialization="
			  << (opts.materialize_input_working_set ? "materialized" : "shared")
				  << " input_location=" << opts.input_location
				  << " input_map=" << input_map_mode_name(opts.input_map_mode)
				  << " input_working_set_bytes=" << input_working_set_bytes
					  << " rows_per_task="
					  << (opts.rows_per_task_all ? std::string("all") :
						  std::to_string(opts.rows_per_task))
					  << " schedule=" << schedule_mode_name(opts.schedule_mode)
				  << " window_writeback=" << (opts.window_writeback ? 1 : 0)
				  << " ring_output_location=" << opts.ring_output_location
				  << " ring_partition=" << ring_partition_name(opts.ring_partition_mode)
				  << " ring_output_bytes=" << ring_bytes
				  << " ring_slots=" << ring_slots
				  << " poll_us=" << opts.poll_us
				  << " iterations=" << opts.iterations
				  << " total_payload_bytes=" << total_payload_bytes
				  << " output_slot_size=" << output_slot_size
				  << " csv=" << opts.csv_path << "\n";

			for (int threads : opts.thread_counts)
			{
				RunResult result = run_once(harness, prepared, output_base, output_slot_size,
										   output_capacity, threads, opts.batch_size,
									   opts.rows_per_task, opts.rows_per_task_all,
									   opts.iterations, logical_rows,
										   working_set_rows, opts.window_writeback, ring_slots,
										   opts.poll_us,
											   opts.timeout_seconds,
											   input_working_set_bytes,
											   opts.schedule_mode,
											   opts.ring_partition_mode,
											   opts.input_map_mode);
			double avg_cycles = result.rows == 0 ? 0.0 :
				(double) result.dpa_cycles_sum / (double) result.rows;
			uint64_t end_to_end_elapsed_us =
				control_setup_elapsed_us + result.host_elapsed_us;
			double end_to_end_throughput_mb_s = end_to_end_elapsed_us == 0 ? 0.0 :
				((double) result.serializer_work_bytes / 1000000.0) /
				((double) end_to_end_elapsed_us / 1000000.0);

				csv << result.threads << ','
					<< opts.iterations << ','
				<< result.rows << ','
					<< result.working_set_rows << ','
					<< result.input_working_set_rows << ','
					<< result.rows_per_task << ','
					<< schedule_mode_name(result.schedule_mode) << ','
					<< (opts.window_writeback ? 1 : 0) << ','
				<< (opts.materialize_input_working_set ? "materialized" : "shared") << ','
				<< opts.input_location << ','
				<< input_map_mode_name(result.input_map_mode) << ','
					<< result.input_working_set_bytes << ','
					<< opts.ring_output_location << ','
					<< ring_partition_name(result.ring_partition_mode) << ','
					<< ring_bytes << ','
				<< result.ring_slots << ','
				<< result.cmdq_chunks << ','
				<< result.cmdq_tasks << ','
				<< result.payload_bytes << ','
				<< result.serializer_work_bytes << ','
				<< result.host_elapsed_us << ','
				<< control_setup_elapsed_us << ','
				<< end_to_end_elapsed_us << ','
				<< result.enqueue_elapsed_us << ','
				<< result.start_elapsed_us << ','
				<< result.drain_wait_us << ','
				<< result.payload_throughput_mb_s << ','
				<< result.throughput_mb_s << ','
				<< end_to_end_throughput_mb_s << ','
				<< result.dpa_throughput_mb_s << ','
				<< result.dpa_cycles_sum << ','
				<< result.dpa_cycles_max << ','
				<< avg_cycles << ','
				<< result.mismatches << ','
				<< result.status_errors << ','
				<< result.verification_skipped << '\n';
			csv.flush();

				std::cout << "threads=" << result.threads
					  << " rows=" << result.rows
					  << " working_set_rows=" << result.working_set_rows
					  << " input_working_set_rows=" << result.input_working_set_rows
							  << " rows_per_dpa_task=" << result.rows_per_task
						  << " schedule=" << schedule_mode_name(result.schedule_mode)
					  << " window_writeback=" << (opts.window_writeback ? 1 : 0)
				  << " input_materialization="
				  << (opts.materialize_input_working_set ? "materialized" : "shared")
				  << " input_location=" << opts.input_location
				  << " input_map=" << input_map_mode_name(result.input_map_mode)
						  << " input_working_set_bytes=" << result.input_working_set_bytes
						  << " ring_output_location=" << opts.ring_output_location
						  << " ring_partition=" << ring_partition_name(result.ring_partition_mode)
					  << " ring_output_bytes=" << ring_bytes
					  << " ring_slots=" << result.ring_slots
					  << " cmdq_chunks=" << result.cmdq_chunks
					  << " cmdq_tasks=" << result.cmdq_tasks
					  << " mismatches=" << result.mismatches
					  << " verification_skipped=" << result.verification_skipped
					  << " host_payload_throughput_mb_s=" << result.payload_throughput_mb_s
					  << " host_serializer_work_throughput_mb_s=" << result.throughput_mb_s
					  << " end_to_end_serializer_work_throughput_mb_s="
					  << end_to_end_throughput_mb_s
					  << " dpa_throughput_mb_s=" << result.dpa_throughput_mb_s
					  << " host_elapsed_us=" << result.host_elapsed_us
					  << " control_setup_elapsed_us=" << control_setup_elapsed_us
					  << " enqueue_elapsed_us=" << result.enqueue_elapsed_us
					  << " start_elapsed_us=" << result.start_elapsed_us
					  << " drain_wait_us=" << result.drain_wait_us
					  << " avg_dpa_cycles_per_row=" << avg_cycles << "\n";
		}

		free(output_alloc);
		return 0;
	}
	catch (const std::exception &ex)
	{
		std::cerr << "error: " << ex.what() << "\n";
		return 1;
	}
}
