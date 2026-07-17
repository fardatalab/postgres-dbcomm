#!/usr/bin/env bash
# Reap only exact installed Homer clients/services and socketless backends after PostgreSQL is stopped.
set -euo pipefail
prefix_logical=${1:?installed prefix required}

# Match the canonical identity returned by /proc/PID/exe even when the stable
# configured prefix traverses a host-specific parent symlink.
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
printf 'PREFIX_IDENTITY logical=%q canonical=%q\n' "$prefix_logical" "$prefix_canonical"

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
            pid=$field1; start=$field2; exe=$field3; remote_exec_backend=$field4
            [[ "$pid" =~ ^[1-9][0-9]*$ && "$start" =~ ^[1-9][0-9]*$ &&
               -n "$exe" && "$remote_exec_backend" =~ ^-?[01]$ ]] || {
                scan_malformed=1
                continue
            }
            scan_exe_records=$((scan_exe_records + 1))
            kill_pid=0
            case "$exe" in
                "$prefix_canonical"/bin/citus_tuple_sink_service*|"$prefix_canonical"/bin/pgbench*|"$prefix_canonical"/bin/pg_basebackup*) kill_pid=1 ;;
                "$prefix_canonical"/bin/postgres*)
                    if (( remote_exec_backend < 0 )); then
                        printf 'UNREADABLE_USER pid=%s reason=project-postgres-cmdline\n' "$pid" >&2
                        unreadable=$((unreadable + 1))
                    elif (( remote_exec_backend )); then
                        kill_pid=1
                    fi
                    ;;
            esac
            if (( kill_pid )); then
                sudo -n python3 "$identity_helper" signal "$pid" "$start" "$exe"
            fi
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
# Re-resolve the stable logical name, but require it still names the exact tree
# that cleanup scanned.  A parent-symlink change is identity drift, not a clean
# post-cleanup result.
exec bash "$(dirname "$0")/host_process_probe.sh" "$prefix_logical" stopped "$prefix_canonical"
