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
    kill_pid=0
    case "$exe" in
        "$prefix_canonical"/bin/citus_tuple_sink_service*|"$prefix_canonical"/bin/pgbench*|"$prefix_canonical"/bin/pg_basebackup*) kill_pid=1 ;;
        "$prefix_canonical"/bin/postgres*)
            args=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
            [[ "$args" == *"remote exec backend"* ]] && kill_pid=1
            ;;
    esac
    if (( kill_pid )); then
        sudo -n python3 "$identity_helper" signal "$pid" "$start" "$exe"
    fi
done
(( unreadable == 0 )) || { printf 'UNREADABLE_USER count=%s\n' "$unreadable" >&2; exit 2; }
# Re-resolve the stable logical name, but require it still names the exact tree
# that cleanup scanned.  A parent-symlink change is identity drift, not a clean
# post-cleanup result.
exec bash "$(dirname "$0")/host_process_probe.sh" "$prefix_logical" stopped "$prefix_canonical"
