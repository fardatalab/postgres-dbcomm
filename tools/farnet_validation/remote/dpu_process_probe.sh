#!/usr/bin/env bash
# Prove DPU-native service identity and expose stale 9727/9728 listeners.
set -euo pipefail
run_id=${1:?run id required}
expectation=${2:-any}
expected_exe="/tmp/farnet-validation-$run_id/dpu-build/build/homer/citus_tuple_sink_service"
found=0
expected_found=0
unexpected=0
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
                    found=$((found + 1))
                    if [[ "$exe" == "$expected_exe" ]]; then expected_found=$((expected_found + 1)); expected_pid=$pid
                    else unexpected=$((unexpected + 1)); fi
                    set +e
                    digest_output=$(python3 "$identity_helper" digest "$pid" "$start" "$exe" 2>&1)
                    digest_rc=$?
                    if (( digest_rc != 0 )); then
                        digest_output=$(sudo -n python3 "$identity_helper" digest "$pid" "$start" "$exe" 2>&1)
                        digest_rc=$?
                    fi
                    set -e
                    IFS=$'\t' read -r digest_record digest_pid digest_start digest digest_exe <<< "$digest_output"
                    if (( digest_rc != 0 )) || [[ "$digest_record" != PIDFD_DIGEST ||
                         "$digest_pid" != "$pid" || "$digest_start" != "$start" ||
                         ! "$digest" =~ ^[0-9a-f]{64}$ || "$digest_exe" != "$exe" ]]; then
                        printf 'DPU_SERVICE_IDENTITY_RACED pid=%s start=%s exe=%s\n' "$pid" "$start" "$exe" >&2
                        scan_malformed=1
                        continue
                    fi
                    printf 'DPU_SERVICE pid=%s start=%s digest=%s exe=%s run=%s\n' "$pid" "$start" "$digest" "$exe" "$run_id"
                    ;;
            esac
            ;;
        SCAN_UNREADABLE)
            [[ "$field1" =~ ^[1-9][0-9]*$ && -n "$field2" ]] || { scan_malformed=1; continue; }
            scan_unreadable_records=$((scan_unreadable_records + 1))
            printf 'UNREADABLE_USER pid=%s reason=%s\n' "$field1" "$field2"
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
listener_output=$(ss -H -ltnp '( sport = :9727 or sport = :9728 )') || exit 2
listeners=$(printf '%s\n' "$listener_output" | sed '/^$/d' | wc -l)
nonzero_recvq=$(printf '%s\n' "$listener_output" | awk 'NF >= 2 && $2 != 0 { count++ } END { print count + 0 }')
owned_listeners=0
if [[ -n "${expected_pid:-}" ]]; then
    owned_listeners=$(printf '%s\n' "$listener_output" | grep -c "pid=$expected_pid," || true)
fi
[[ -z "$listener_output" ]] || printf '%s\n' "$listener_output"
printf 'SUMMARY services=%s expected_services=%s unexpected_services=%s expectation=%s listeners=%s owned_listeners=%s nonzero_recvq=%s unreadable_user=%s expected_exe=%s\n' \
    "$found" "$expected_found" "$unexpected" "$expectation" "$listeners" "$owned_listeners" \
    "$nonzero_recvq" "$unreadable" "$expected_exe"
(( scan_rc == 0 && scan_summary_seen == 1 && scan_malformed == 0 &&
   scan_exe_records == scan_resolved && scan_unreadable_records == scan_unreadable &&
   unreadable == scan_unreadable && unreadable == 0 )) || exit 2
case "$expectation" in
    one) (( found == 1 && expected_found == 1 && unexpected == 0 && listeners == 2 && owned_listeners == 2 && nonzero_recvq == 0 )) ;;
    zero) (( found == 0 && listeners == 0 )) ;;
    any) (( found <= 1 && unexpected == 0 )) ;;
    *) echo "unknown expectation $expectation" >&2; exit 3 ;;
esac
