#!/usr/bin/env bash
# Reap DPU-native services by executable path, then hard-fail if either listener remains.
set -euo pipefail
run_id=${1:?run id required}
unreadable=0
identity_helper="$(dirname "$0")/proc_identity.py"
scan_args=(scan)
[[ ${HOMER_VALIDATION_VERBOSE_PROC:-0} == 1 ]] && scan_args+=(--verbose-benign)
set +e
scan_output=$(python3 "$identity_helper" "${scan_args[@]}" 2>&1)
scan_rc=$?
if (( scan_rc != 0 )); then
    scan_output=$(sudo -n python3 "$identity_helper" "${scan_args[@]}" 2>&1)
    scan_rc=$?
fi
set -e
scan_summary_seen=0
scan_malformed=0
scan_resolved=0
scan_kernel=0
scan_zombie=0
scan_raced=0
scan_unreadable=0
scan_exe_records=0
scan_unreadable_records=0
while IFS=$'\t' read -r record field1 field2 field3 field4 field5; do
    case "$record" in
        SCAN_EXE)
            pid=$field1; start=$field2; exe=$field3
            [[ "$pid" =~ ^[1-9][0-9]*$ && "$start" =~ ^[1-9][0-9]*$ &&
               -n "$exe" && "$field4" =~ ^-?[01]$ ]] || {
                scan_malformed=1
                continue
            }
            scan_exe_records=$((scan_exe_records + 1))
            case "$exe" in
                */citus-dbcomm/build/homer/citus_tuple_sink_service*|*/dpu-build/build/homer/citus_tuple_sink_service*)
                    sudo -n python3 "$identity_helper" signal "$pid" "$start" "$exe" ;;
            esac
            ;;
        SCAN_UNREADABLE)
            [[ "$field1" =~ ^[1-9][0-9]*$ && -n "$field2" ]] || { scan_malformed=1; continue; }
            scan_unreadable_records=$((scan_unreadable_records + 1))
            printf 'UNREADABLE_USER pid=%s reason=%s\n' "$field1" "$field2" >&2
            unreadable=$((unreadable + 1))
            ;;
        SCAN_BENIGN)
            printf 'PROC_SCAN_BENIGN kind=%s pid=%s stage=%s\n' "$field1" "$field2" "$field3"
            ;;
        SCAN_SUMMARY)
            [[ "$field1" =~ ^[0-9]+$ && "$field2" =~ ^[0-9]+$ &&
               "$field3" =~ ^[0-9]+$ && "$field4" =~ ^[0-9]+$ &&
               "$field5" =~ ^[0-9]+$ ]] || { scan_malformed=1; continue; }
            scan_summary_seen=$((scan_summary_seen + 1))
            scan_resolved=$field1; scan_kernel=$field2; scan_zombie=$field3
            scan_raced=$field4; scan_unreadable=$field5
            ;;
        SCAN_ERROR)
            printf 'PROC_SCAN_ERROR stage=%s detail=%s\n' "$field1" "$field2" >&2
            scan_malformed=1
            ;;
        '') ;;
        *) printf 'PROC_SCAN_MALFORMED record=%q\n' "$record" >&2; scan_malformed=1 ;;
    esac
done <<< "$scan_output"
printf 'PROC_SCAN_SUMMARY resolved_exe=%s kernel_no_exe=%s zombie_no_exe=%s raced_exit=%s unreadable_user=%s\n' \
    "$scan_resolved" "$scan_kernel" "$scan_zombie" "$scan_raced" "$scan_unreadable"
(( scan_rc == 0 && scan_summary_seen == 1 && scan_malformed == 0 &&
   scan_exe_records == scan_resolved && scan_unreadable_records == scan_unreadable &&
   unreadable == scan_unreadable && unreadable == 0 )) || {
    printf 'UNREADABLE_OR_INVALID_PROC_SCAN count=%s scan_rc=%s\n' "$unreadable" "$scan_rc" >&2
    exit 2
}
if ss -ltnp '( sport = :9727 or sport = :9728 )' | tail -n +2 | grep -q .; then
    echo "stale DPU listener remains after cleanup run=$run_id" >&2
    exit 2
fi
printf 'DPU_CLEAN run=%s listeners=0\n' "$run_id"
