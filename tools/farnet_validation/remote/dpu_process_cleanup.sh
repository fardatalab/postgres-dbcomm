#!/usr/bin/env bash
# Reap DPU-native services by executable path, then hard-fail if either listener remains.
set -euo pipefail
run_id=${1:?run id required}
unreadable=0
for proc in /proc/[0-9]*; do
    exe=$(readlink -f "$proc/exe" 2>/dev/null) || {
        if [[ ! -r "$proc/cmdline" || -s "$proc/cmdline" ]]; then unreadable=$((unreadable + 1)); fi
        continue
    }
    case "$exe" in
        */citus-dbcomm/build/homer/citus_tuple_sink_service*|*/dpu-build/build/homer/citus_tuple_sink_service*)
            kill -9 "${proc#/proc/}" ;;
    esac
done
(( unreadable == 0 )) || { printf 'UNREADABLE_USER count=%s\n' "$unreadable" >&2; exit 2; }
if ss -ltnp '( sport = :9727 or sport = :9728 )' | tail -n +2 | grep -q .; then
    echo "stale DPU listener remains after cleanup run=$run_id" >&2
    exit 2
fi
printf 'DPU_CLEAN run=%s listeners=0\n' "$run_id"
