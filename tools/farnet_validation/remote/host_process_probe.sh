#!/usr/bin/env bash
# Classify project processes without treating a kernel no-exe entry as unreadable user state.
set -euo pipefail
prefix_logical=${1:?installed prefix required}
postgres_expectation=${2:-stopped}
expected_prefix_canonical=${3:-}

# /proc/PID/exe reports the canonical physical path.  The configured prefix is
# deliberately a stable logical path and may traverse a parent symlink, so
# canonicalize it once before matching any process identity.
if [[ ! -d "$prefix_logical" ]]; then
    printf 'could not canonicalize installed prefix logical=%q\n' "$prefix_logical" >&2
    exit 3
fi
prefix_canonical=$(readlink -f -- "$prefix_logical" 2>/dev/null) || {
    printf 'could not canonicalize installed prefix logical=%q\n' "$prefix_logical" >&2
    exit 3
}
if [[ -z "$prefix_canonical" || ! -d "$prefix_canonical/bin" ]]; then
    printf 'invalid canonical installed prefix logical=%q canonical=%q reason=missing-bin\n' \
        "$prefix_logical" "$prefix_canonical" >&2
    exit 3
fi
if [[ -n "$expected_prefix_canonical" && "$prefix_canonical" != "$expected_prefix_canonical" ]]; then
    printf 'installed prefix identity changed logical=%q expected_canonical=%q observed_canonical=%q\n' \
        "$prefix_logical" "$expected_prefix_canonical" "$prefix_canonical" >&2
    exit 3
fi
printf 'PREFIX_IDENTITY logical=%q canonical=%q\n' "$prefix_logical" "$prefix_canonical"

unreadable=0
postgres_count=0
forbidden=0
identity_helper="$(dirname "$0")/proc_identity.py"

# Resolve the complete snapshot in one interpreter, with at most one
# privileged retry if an unprivileged user identity is unreadable. This
# retains pidfd-backed classification while avoiding one Python+sudo pair and
# one log line for every kernel task on DPU kernels.
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
            pid=$field1; start=$field2; exe=$field3; remote_exec_backend=$field4
            [[ "$pid" =~ ^[1-9][0-9]*$ && "$start" =~ ^[1-9][0-9]*$ &&
               -n "$exe" && "$remote_exec_backend" =~ ^-?[01]$ ]] || {
                scan_malformed=1
                continue
            }
            scan_exe_records=$((scan_exe_records + 1))
            case "$exe" in
                "$prefix_canonical"/bin/postgres*)
                    if (( remote_exec_backend < 0 )); then
                        printf 'UNREADABLE_USER pid=%s reason=project-postgres-cmdline\n' "$pid"
                        unreadable=$((unreadable + 1))
                    elif (( remote_exec_backend )); then
                        printf 'FORBIDDEN_PROCESS pid=%s start=%s exe=%s kind=remote-exec-backend\n' "$pid" "$start" "$exe"
                        forbidden=$((forbidden + 1))
                    else
                        printf 'POSTGRES_PROCESS pid=%s start=%s exe=%s\n' "$pid" "$start" "$exe"
                        postgres_count=$((postgres_count + 1))
                    fi
                    ;;
                "$prefix_canonical"/bin/pgbench*|"$prefix_canonical"/bin/pg_basebackup*|"$prefix_canonical"/bin/citus_tuple_sink_service*)
                    printf 'FORBIDDEN_PROCESS pid=%s start=%s exe=%s kind=client-or-host-service\n' "$pid" "$start" "$exe"
                    forbidden=$((forbidden + 1))
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
printf 'SUMMARY unreadable_user=%s postgres_processes=%s forbidden_processes=%s expectation=%s\n' \
    "$unreadable" "$postgres_count" "$forbidden" "$postgres_expectation"
(( scan_rc == 0 && scan_summary_seen == 1 && scan_malformed == 0 &&
   scan_exe_records == scan_resolved && scan_unreadable_records == scan_unreadable &&
   unreadable == scan_unreadable && unreadable == 0 && forbidden == 0 )) || exit 2
case "$postgres_expectation" in
    running) (( postgres_count > 0 )) ;;
    stopped) (( postgres_count == 0 )) ;;
    *) echo "unknown PostgreSQL expectation $postgres_expectation" >&2; exit 3 ;;
esac
