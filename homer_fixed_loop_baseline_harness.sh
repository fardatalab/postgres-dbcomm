#!/usr/bin/env bash
#
# Run reproducible Homer fixed-loop baseline smokes/sweeps against the currently
# installed runtime. This script intentionally does not build, install, or switch
# HOMER_FIXED_PAYLOAD_MODE; choose and install the mode first, then pass a
# descriptive --mode label so the result directory records what was intended.

set -euo pipefail

PREFIX=${PREFIX:-/data/dbcomm/pg-citus}
PEER_HOST=${PEER_HOST:-farnet0}
LOCAL_PGHOST=${LOCAL_PGHOST:-/tmp}
LOCAL_PGPORT=${LOCAL_PGPORT:-5432}
LOCAL_PGUSER=${LOCAL_PGUSER:-dbcomm}
LOCAL_DB=${LOCAL_DB:-postgres}
LOCAL_SERVICE_HOST=${LOCAL_SERVICE_HOST:-10.10.1.102}
REMOTE_SERVICE_HOST=${REMOTE_SERVICE_HOST:-10.10.1.100}
HOMER_SERVICE_PORT=${HOMER_SERVICE_PORT:-9717}
REMOTE_CLIENT_CPU=${REMOTE_CLIENT_CPU:-3}
REMOTE_CLIENT_CPUS=${REMOTE_CLIENT_CPUS:-8,9,10,11}
PGBENCH_C1_TX=${PGBENCH_C1_TX:-1000}
PGBENCH_C4_TX=${PGBENCH_C4_TX:-2500}
MIXED_BASEBACKUP_START_DELAY=${MIXED_BASEBACKUP_START_DELAY:-0.5}
BASEBACKUP_TARGET=${BASEBACKUP_TARGET:-homer:mode=rdma,host=${REMOTE_SERVICE_HOST},port=${HOMER_SERVICE_PORT},node=2,slots=8,bytes=8388608}
RESULT_ROOT=${RESULT_ROOT:-/tmp}
MODE_LABEL=${MODE_LABEL:-unknown-installed-mode}
RUNS=1
RUN_LABEL=""
RUN_SUFFIX=""
WORKLOAD=""

usage()
{
	cat <<'USAGE'
Usage:
  homer_fixed_loop_baseline_harness.sh --mode MODE --workload WORKLOAD [--label LABEL] [--runs N]

Workloads:
  preflight
  pgbench-c1
  pgbench-c4
  basebackup-local-blackhole
  basebackup-rdma
  mixed-pgbench-c1-basebackup
  mixed-pgbench-c4-basebackup
  smoke-core

Environment overrides:
  PREFIX, PEER_HOST, LOCAL_SERVICE_HOST, REMOTE_SERVICE_HOST,
  HOMER_SERVICE_PORT, RESULT_ROOT, DBOID, USEROID, REMOTE_CLIENT_CPU,
  REMOTE_CLIENT_CPUS, PGBENCH_C1_TX, PGBENCH_C4_TX,
  MIXED_BASEBACKUP_START_DELAY, BASEBACKUP_TARGET.

This harness records commands, git/binary identity, process preflight, and raw
logs. It does not rebuild or verify that the installed binary actually matches
--mode; keep the install step explicit in the runbook.
USAGE
}

while [ "$#" -gt 0 ]; do
	case "$1" in
		--mode)
			MODE_LABEL="$2"
			shift 2
			;;
		--workload)
			WORKLOAD="$2"
			shift 2
			;;
		--label)
			RUN_LABEL="$2"
			shift 2
			;;
		--runs)
			RUNS="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

if [ -z "$WORKLOAD" ]; then
	echo "--workload is required" >&2
	usage >&2
	exit 2
fi
if ! [[ "$RUNS" =~ ^[0-9]+$ ]] || [ "$RUNS" -lt 1 ]; then
	echo "--runs must be a positive integer" >&2
	usage >&2
	exit 2
fi

timestamp=$(date +%Y%m%d_%H%M%S)
safe_mode=$(printf '%s' "$MODE_LABEL" | tr -c 'A-Za-z0-9_.-' '_')
safe_workload=$(printf '%s' "$WORKLOAD" | tr -c 'A-Za-z0-9_.-' '_')
if [ -n "$RUN_LABEL" ]; then
	safe_label="_$(printf '%s' "$RUN_LABEL" | tr -c 'A-Za-z0-9_.-' '_')"
else
	safe_label=""
fi
OUT="${RESULT_ROOT}/homer_fixed_loop_${safe_mode}_${safe_workload}${safe_label}_${timestamp}"
mkdir -p "$OUT"

run_logged()
{
	local name=$1
	shift

	{
		printf '### %s\n' "$name"
		printf '+'
		printf ' %q' "$@"
		printf '\n'
		"$@"
	} > "${OUT}/${name}${RUN_SUFFIX}.log" 2>&1
}

record_command()
{
	local name=$1
	shift

	{
		printf '+'
		printf ' %q' "$@"
		printf '\n'
		} > "${OUT}/${name}${RUN_SUFFIX}.cmd"
	}

query_oid()
{
	local sql=$1

	sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/psql" \
		-h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" \
		-AtX "$LOCAL_DB" -c "$sql"
}

ensure_oids()
{
	if [ -z "${DBOID:-}" ]; then
		DBOID=$(query_oid "select oid from pg_database where datname = '${LOCAL_DB}'")
	fi
	if [ -z "${USEROID:-}" ]; then
		USEROID=$(query_oid "select oid from pg_roles where rolname = '${LOCAL_PGUSER}'")
	fi
}

write_metadata()
{
	{
		printf 'timestamp=%s\n' "$timestamp"
		printf 'mode_label=%s\n' "$MODE_LABEL"
		printf 'workload=%s\n' "$WORKLOAD"
		printf 'prefix=%s\n' "$PREFIX"
		printf 'peer_host=%s\n' "$PEER_HOST"
		printf 'local_service_host=%s\n' "$LOCAL_SERVICE_HOST"
		printf 'remote_service_host=%s\n' "$REMOTE_SERVICE_HOST"
		printf 'homer_service_port=%s\n' "$HOMER_SERVICE_PORT"
		printf 'runs=%s\n' "$RUNS"
		printf 'pgbench_c1_tx=%s\n' "$PGBENCH_C1_TX"
		printf 'pgbench_c4_tx=%s\n' "$PGBENCH_C4_TX"
		printf 'mixed_basebackup_start_delay=%s\n' "$MIXED_BASEBACKUP_START_DELAY"
		printf 'db_oid=%s\n' "${DBOID:-<unset>}"
		printf 'user_oid=%s\n' "${USEROID:-<unset>}"
		printf 'basebackup_target=%s\n' "$BASEBACKUP_TARGET"
		printf 'postgres_git='
		git -C /data/dbcomm/postgres-citus-homer-unscheduled-baselines rev-parse --short HEAD 2>/dev/null || true
		printf 'citus_git='
		git -C /data/dbcomm/citus-dbcomm-homer-unscheduled-baselines rev-parse --short HEAD 2>/dev/null || true
		for binary in \
			"${PREFIX}/bin/postgres" \
			"${PREFIX}/bin/pgbench" \
			"${PREFIX}/bin/pg_basebackup" \
			"${PREFIX}/bin/citus_tuple_sink_service" \
			"${PREFIX}/lib/x86_64-linux-gnu/postgresql/citus.so"; do
			if [ -e "$binary" ]; then
				sha256sum "$binary"
			else
				printf 'missing %s\n' "$binary"
			fi
		done
	} > "${OUT}/metadata.txt"
}

preflight()
{
	ps -eo pid,ppid,psr,comm,args |
		grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' |
		grep -v grep || true
	ssh "$PEER_HOST" "ps -eo pid,ppid,psr,comm,args | grep -E 'citus_tuple_sink_service|postgres -D|remote exec|pgbench|pg_basebackup|walsender' | grep -v grep || true"
}

run_preflight()
{
	run_logged preflight_before bash -c "$(declare -f preflight); PEER_HOST='$PEER_HOST'; preflight"
}

run_pgbench_c1()
{
	ensure_oids
	record_command pgbench_c1 ssh "$PEER_HOST" "sudo -n -u ${LOCAL_PGUSER} ${PREFIX}/bin/pgbench -h ${LOCAL_PGHOST} -p ${LOCAL_PGPORT} -U ${LOCAL_PGUSER} --homer --homer-database-oid ${DBOID} --homer-user-oid ${USEROID} --homer-peer-host ${LOCAL_SERVICE_HOST} --homer-peer-port ${HOMER_SERVICE_PORT} --homer-peer-node 1 --latency-percentiles --client-cpu=${REMOTE_CLIENT_CPU} -n -M simple -c 1 -j 1 -t ${PGBENCH_C1_TX} ${LOCAL_DB}"
	ssh "$PEER_HOST" "sudo -n -u ${LOCAL_PGUSER} ${PREFIX}/bin/pgbench \
		-h ${LOCAL_PGHOST} -p ${LOCAL_PGPORT} -U ${LOCAL_PGUSER} \
		--homer \
		--homer-database-oid ${DBOID} \
		--homer-user-oid ${USEROID} \
		--homer-peer-host ${LOCAL_SERVICE_HOST} \
		--homer-peer-port ${HOMER_SERVICE_PORT} \
		--homer-peer-node 1 \
		--latency-percentiles \
		--client-cpu=${REMOTE_CLIENT_CPU} \
		-n -M simple -c 1 -j 1 -t ${PGBENCH_C1_TX} ${LOCAL_DB}" \
		> "${OUT}/pgbench_c1${RUN_SUFFIX}.log" 2>&1
}

run_pgbench_c4()
{
	ensure_oids
	record_command pgbench_c4 ssh "$PEER_HOST" "sudo -n -u ${LOCAL_PGUSER} ${PREFIX}/bin/pgbench -h ${LOCAL_PGHOST} -p ${LOCAL_PGPORT} -U ${LOCAL_PGUSER} --homer --homer-database-oid ${DBOID} --homer-user-oid ${USEROID} --homer-peer-host ${LOCAL_SERVICE_HOST} --homer-peer-port ${HOMER_SERVICE_PORT} --homer-peer-node 1 --latency-percentiles --client-cpus=${REMOTE_CLIENT_CPUS} -n -M simple -c 4 -j 4 -t ${PGBENCH_C4_TX} ${LOCAL_DB}"
	ssh "$PEER_HOST" "sudo -n -u ${LOCAL_PGUSER} ${PREFIX}/bin/pgbench \
		-h ${LOCAL_PGHOST} -p ${LOCAL_PGPORT} -U ${LOCAL_PGUSER} \
		--homer \
		--homer-database-oid ${DBOID} \
		--homer-user-oid ${USEROID} \
		--homer-peer-host ${LOCAL_SERVICE_HOST} \
		--homer-peer-port ${HOMER_SERVICE_PORT} \
		--homer-peer-node 1 \
		--latency-percentiles \
		--client-cpus=${REMOTE_CLIENT_CPUS} \
		-n -M simple -c 4 -j 4 -t ${PGBENCH_C4_TX} ${LOCAL_DB}" \
		> "${OUT}/pgbench_c4${RUN_SUFFIX}.log" 2>&1
}

run_basebackup_rdma()
{
	record_command basebackup_rdma /usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" -h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" -X none -c fast -t "$BASEBACKUP_TARGET" -v
	/usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" \
		-h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" \
		-X none -c fast \
		-t "$BASEBACKUP_TARGET" \
		-v > "${OUT}/basebackup_rdma${RUN_SUFFIX}.log" 2>&1
}

run_basebackup_local_blackhole()
{
	local target="homer:mode=blackhole,node=1"

	record_command basebackup_local_blackhole /usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" -h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" -X none -c fast -t "$target" -v
	/usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" \
		-h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" \
		-X none -c fast \
		-t "$target" \
		-v > "${OUT}/basebackup_local_blackhole${RUN_SUFFIX}.log" 2>&1
}

run_mixed()
{
	local clients=$1
	local basebackup_pid
	local pgbench_status
	local basebackup_status

	ensure_oids
	record_command mixed_basebackup /usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" -h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" -X none -c fast -t "$BASEBACKUP_TARGET" -v
	(
		/usr/bin/time -p sudo -n -u "$LOCAL_PGUSER" "${PREFIX}/bin/pg_basebackup" \
			-h "$LOCAL_PGHOST" -p "$LOCAL_PGPORT" -U "$LOCAL_PGUSER" \
			-X none -c fast \
			-t "$BASEBACKUP_TARGET" \
			-v
	) > "${OUT}/mixed_basebackup${RUN_SUFFIX}.log" 2>&1 &
	basebackup_pid=$!

	sleep "$MIXED_BASEBACKUP_START_DELAY"
	set +e
	if [ "$clients" = "1" ]; then
		run_pgbench_c1
	else
		run_pgbench_c4
	fi
	pgbench_status=$?
	wait "$basebackup_pid"
	basebackup_status=$?
	set -e
	if [ "$pgbench_status" -ne 0 ] || [ "$basebackup_status" -ne 0 ]; then
		echo "mixed workload failed: pgbench_status=${pgbench_status} basebackup_status=${basebackup_status}" >&2
		return 1
	fi
}

case "$WORKLOAD" in
	pgbench-c1|pgbench-c4|mixed-pgbench-c1-basebackup|mixed-pgbench-c4-basebackup|smoke-core)
		ensure_oids
		;;
esac

run_workload_once()
{
	case "$WORKLOAD" in
		preflight)
			;;
		pgbench-c1)
			run_pgbench_c1
			;;
		pgbench-c4)
			run_pgbench_c4
			;;
		basebackup-local-blackhole)
			run_basebackup_local_blackhole
			;;
		basebackup-rdma)
			run_basebackup_rdma
			;;
		mixed-pgbench-c1-basebackup)
			run_mixed 1
			;;
		mixed-pgbench-c4-basebackup)
			run_mixed 4
			;;
		smoke-core)
			run_pgbench_c1
			run_pgbench_c4
			run_basebackup_local_blackhole
			run_basebackup_rdma
			;;
		*)
			echo "unknown workload: $WORKLOAD" >&2
			usage >&2
			exit 2
			;;
	esac
}

write_metadata
for run in $(seq 1 "$RUNS"); do
	if [ "$RUNS" -gt 1 ]; then
		RUN_SUFFIX=$(printf '_run%02d' "$run")
	else
		RUN_SUFFIX=""
	fi
	run_preflight
	run_workload_once
	run_logged preflight_after bash -c "$(declare -f preflight); PEER_HOST='$PEER_HOST'; preflight"
done

printf 'artifacts=%s\n' "$OUT"
