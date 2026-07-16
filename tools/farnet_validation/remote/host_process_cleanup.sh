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
for proc in /proc/[0-9]*; do
    pid=${proc#/proc/}
    exe=$(readlink -f "$proc/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$proc/exe" 2>/dev/null) || {
        if [[ ! -r "$proc/cmdline" || -s "$proc/cmdline" ]]; then unreadable=$((unreadable + 1)); fi
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
    if (( kill_pid )); then sudo -n kill -9 "$pid"; fi
done
(( unreadable == 0 )) || { printf 'UNREADABLE_USER count=%s\n' "$unreadable" >&2; exit 2; }
# Re-resolve the stable logical name, but require it still names the exact tree
# that cleanup scanned.  A parent-symlink change is identity drift, not a clean
# post-cleanup result.
exec bash "$(dirname "$0")/host_process_probe.sh" "$prefix_logical" stopped "$prefix_canonical"
