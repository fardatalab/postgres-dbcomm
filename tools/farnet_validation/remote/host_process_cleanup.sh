#!/usr/bin/env bash
# Reap only exact installed Homer clients/services and socketless backends after PostgreSQL is stopped.
set -euo pipefail
prefix=${1:?installed prefix required}
unreadable=0
for proc in /proc/[0-9]*; do
    pid=${proc#/proc/}
    exe=$(readlink -f "$proc/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$proc/exe" 2>/dev/null) || {
        if [[ ! -r "$proc/cmdline" || -s "$proc/cmdline" ]]; then unreadable=$((unreadable + 1)); fi
        continue
    }
    kill_pid=0
    case "$exe" in
        "$prefix"/bin/citus_tuple_sink_service*|"$prefix"/bin/pgbench*|"$prefix"/bin/pg_basebackup*) kill_pid=1 ;;
        "$prefix"/bin/postgres*)
            args=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
            [[ "$args" == *"remote exec backend"* ]] && kill_pid=1
            ;;
    esac
    if (( kill_pid )); then sudo -n kill -9 "$pid"; fi
done
(( unreadable == 0 )) || { printf 'UNREADABLE_USER count=%s\n' "$unreadable" >&2; exit 2; }
exec "$(dirname "$0")/host_process_probe.sh" "$prefix" stopped
