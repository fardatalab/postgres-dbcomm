#!/usr/bin/env bash
#
# Run the current single-client pgbench comparison with code-level pgbench
# frontend pinning. The Homer service/backend pins are deliberately not hidden
# here because they must be set before starting citus_tuple_sink_service:
#
#   HOMER_SERVICE_CPU=2 HOMER_REMOTE_EXEC_BACKEND_CPU=4 citus_tuple_sink_service
#
# The script records process placement from /proc so benchmark artifacts include
# evidence of where the frontend, service, and backend processes actually ran.

set -euo pipefail

if [[ -x /data/dbcomm/postgres-citus-separate-comm-stack/build/src/bin/pgbench/pgbench ]]; then
	PGBENCH=${PGBENCH:-/data/dbcomm/postgres-citus-separate-comm-stack/build/src/bin/pgbench/pgbench}
else
	PGBENCH=${PGBENCH:-/data/dbcomm/pg-citus/bin/pgbench}
fi

DBNAME=${DBNAME:-postgres}
HOMER_DATABASE_OID=${HOMER_DATABASE_OID:-5}
HOMER_USER_OID=${HOMER_USER_OID:-10}
CLIENT_CPU=${CLIENT_CPU:-3}
TRANSACTIONS=${TRANSACTIONS:-10000}
RUNS=${RUNS:-3}
SEED_BASE=${SEED_BASE:-9000}
LATENCY_PERCENTILES=${LATENCY_PERCENTILES:-1}
RUN_AS=${RUN_AS:-}
OUT_DIR=${OUT_DIR:-/data/dbcomm/pg-citus/homer_pgbench_runs/$(date +%Y%m%d_%H%M%S)}

mkdir -p "${OUT_DIR}"

run_as_prefix=()
if [[ -n "${RUN_AS}" ]]; then
	run_as_prefix=(sudo -n -u "${RUN_AS}")
fi

record_process_snapshot()
{
	local label=$1
	local output="${OUT_DIR}/${label}_processes.txt"

	{
		printf 'date: '
		date -Is
		printf 'uname: '
		uname -a
		printf 'expected_service_cpu: %s\n' "${HOMER_SERVICE_CPU:-<not set in this shell>}"
		printf 'expected_backend_cpu: %s\n' "${HOMER_REMOTE_EXEC_BACKEND_CPU:-<not set in this shell>}"
		printf '\nprocesses:\n'
		ps -eo pid,ppid,psr,comm,args | \
			grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench' | \
			grep -v grep || true
		printf '\n/proc affinity:\n'
		for pid in $(pgrep -f 'citus_tuple_sink_service|postgres -D|remote exec|pgbench' || true); do
			if [[ -r "/proc/${pid}/status" ]]; then
				awk -v pid="${pid}" '
					/^Name:/ { name=$2 }
					/^Cpus_allowed_list:/ { cpus=$2 }
					END { if (name != "") printf "pid=%s name=%s cpus_allowed_list=%s\n", pid, name, cpus }
				' "/proc/${pid}/status"
			fi
		done
	} > "${output}"
}

run_pgbench()
{
	local mode=$1
	local run_index=$2
	local seed=$3
	shift 3

	local output="${OUT_DIR}/${mode}_run${run_index}.txt"
	local percentile_args=()

	if [[ "${LATENCY_PERCENTILES}" != "0" ]]; then
		percentile_args=(--latency-percentiles)
	fi

	printf '== %s run %s seed %s ==\n' "${mode}" "${run_index}" "${seed}" | tee "${output}"
	record_process_snapshot "${mode}_run${run_index}_before"

	{
		/usr/bin/time -f 'time_real=%e time_user=%U time_sys=%S maxrss_kb=%M' \
			"${run_as_prefix[@]}" \
			"${PGBENCH}" \
			--client-cpu="${CLIENT_CPU}" \
			--random-seed="${seed}" \
			"${percentile_args[@]}" \
			-n -c 1 -j 1 -t "${TRANSACTIONS}" \
			"$@" \
			"${DBNAME}"
	} 2>&1 | tee -a "${output}"

	record_process_snapshot "${mode}_run${run_index}_after"
}

{
	printf 'pghost: %s\n' "${PGHOST:-<unset>}"
	printf 'pgport: %s\n' "${PGPORT:-<unset>}"
	printf 'pgbench: %s\n' "${PGBENCH}"
	printf 'dbname: %s\n' "${DBNAME}"
	printf 'client_cpu: %s\n' "${CLIENT_CPU}"
	printf 'transactions: %s\n' "${TRANSACTIONS}"
	printf 'runs: %s\n' "${RUNS}"
	printf 'latency_percentiles: %s\n' "${LATENCY_PERCENTILES}"
	printf 'run_as: %s\n' "${RUN_AS:-<current user>}"
	printf 'homer_database_oid: %s\n' "${HOMER_DATABASE_OID}"
	printf 'homer_user_oid: %s\n' "${HOMER_USER_OID}"
	printf 'service_cpu_env_seen_by_script: %s\n' "${HOMER_SERVICE_CPU:-<not set in this shell>}"
	printf 'backend_cpu_env_seen_by_script: %s\n' "${HOMER_REMOTE_EXEC_BACKEND_CPU:-<not set in this shell>}"
} > "${OUT_DIR}/config.txt"

record_process_snapshot "initial"

for run_index in $(seq 1 "${RUNS}"); do
	seed=$((SEED_BASE + run_index))
	run_pgbench "homer" "${run_index}" "${seed}" \
		--homer \
		--homer-database-oid "${HOMER_DATABASE_OID}" \
		--homer-user-oid "${HOMER_USER_OID}"
	run_pgbench "libpq" "${run_index}" "${seed}"
done

record_process_snapshot "final"
printf 'artifacts: %s\n' "${OUT_DIR}"
