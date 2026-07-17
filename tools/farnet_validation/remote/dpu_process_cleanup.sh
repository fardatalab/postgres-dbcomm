#!/usr/bin/env bash
# Reap DPU-native services by executable path, then hard-fail if either listener remains.
set -euo pipefail
run_id=${1:?run id required}
unreadable=0
identity_helper="$(dirname "$0")/proc_identity.py"
for proc in /proc/[0-9]*; do
    pid=${proc#/proc/}
    exe=$(readlink -f "$proc/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$proc/exe" 2>/dev/null) || {
        classification=
        privileged_classification=
        if classification=$(python3 "$identity_helper" classify "$pid" 2>&1) ||
            privileged_classification=$(sudo -n python3 "$identity_helper" classify "$pid" 2>&1); then
            [[ -n "$privileged_classification" ]] && classification=$privileged_classification
            printf '%s\n' "$classification"
        else
            [[ -n "$privileged_classification" ]] && classification=$privileged_classification
            unreadable=$((unreadable + 1))
            printf '%s\n' "$classification"
        fi
        continue
    }
    start=$(awk '{print $22}' "$proc/stat" 2>/dev/null) || {
        [[ ! -d "$proc" ]] && continue
        unreadable=$((unreadable + 1))
        continue
    }
    case "$exe" in
        */citus-dbcomm/build/homer/citus_tuple_sink_service*|*/dpu-build/build/homer/citus_tuple_sink_service*)
            sudo -n python3 "$identity_helper" signal "$pid" "$start" "$exe" ;;
    esac
done
(( unreadable == 0 )) || { printf 'UNREADABLE_USER count=%s\n' "$unreadable" >&2; exit 2; }
if ss -ltnp '( sport = :9727 or sport = :9728 )' | tail -n +2 | grep -q .; then
    echo "stale DPU listener remains after cleanup run=$run_id" >&2
    exit 2
fi
printf 'DPU_CLEAN run=%s listeners=0\n' "$run_id"
