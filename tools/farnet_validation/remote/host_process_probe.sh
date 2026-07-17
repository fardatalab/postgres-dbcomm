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
for proc in /proc/[0-9]*; do
    pid=${proc#/proc/}
    exe=
    if exe=$(readlink -f "$proc/exe" 2>/dev/null) || exe=$(sudo -n readlink -f "$proc/exe" 2>/dev/null); then
        case "$exe" in
            "$prefix_canonical"/bin/postgres*)
                start=$(awk '{print $22}' "$proc/stat")
                args=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
                if [[ "$args" == *"remote exec backend"* ]]; then
                    printf 'FORBIDDEN_PROCESS pid=%s start=%s exe=%s kind=remote-exec-backend\n' "$pid" "$start" "$exe"
                    forbidden=$((forbidden + 1))
                else
                    printf 'POSTGRES_PROCESS pid=%s start=%s exe=%s\n' "$pid" "$start" "$exe"
                    postgres_count=$((postgres_count + 1))
                fi
                ;;
            "$prefix_canonical"/bin/pgbench*|"$prefix_canonical"/bin/pg_basebackup*|"$prefix_canonical"/bin/citus_tuple_sink_service*)
                start=$(awk '{print $22}' "$proc/stat")
                printf 'FORBIDDEN_PROCESS pid=%s start=%s exe=%s kind=client-or-host-service\n' "$pid" "$start" "$exe"
                forbidden=$((forbidden + 1))
                ;;
        esac
        continue
    fi

    # procfs advertises cmdline st_size=0 even for live user processes, so
    # shell `-s` is not an identity predicate. The helper performs a bounded
    # content read plus PF_KTHREAD/zombie classification and distinguishes a
    # proven raced exit from a still-live unreadable identity.
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
done
printf 'SUMMARY unreadable_user=%s postgres_processes=%s forbidden_processes=%s expectation=%s\n' \
    "$unreadable" "$postgres_count" "$forbidden" "$postgres_expectation"
(( unreadable == 0 && forbidden == 0 )) || exit 2
case "$postgres_expectation" in
    running) (( postgres_count > 0 )) ;;
    stopped) (( postgres_count == 0 )) ;;
    *) echo "unknown PostgreSQL expectation $postgres_expectation" >&2; exit 3 ;;
esac
