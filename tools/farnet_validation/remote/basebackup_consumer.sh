#!/usr/bin/env bash
# Start/wait the receiver first while retaining its real exit status in run-scoped files.
set -euo pipefail
action=${1:?start|supervise|wait required}; run_id=${2:?run id required}
root="/tmp/farnet-validation-$run_id/basebackup-consumer"

# Both the sender target option and --homer-tag are parsed as signed int32.
# Reject a bad tag synchronously, before creating consumer state or a process
# that would otherwise wait after the sender rejects its target string.
validate_tag() {
    local tag=${1:?tag required}
    [[ "$tag" =~ ^[0-9]+$ ]] || { echo "tag must be numeric" >&2; return 2; }
    [[ ${#tag} -le 10 ]] || { echo "tag exceeds int32" >&2; return 2; }
    local value=$((10#$tag))
    (( value >= 1 && value <= 2147483647 )) || {
        echo "tag must be in positive int32 range" >&2
        return 2
    }
}

case "$action" in
supervise)
    prefix=${3:?prefix required}; dboid=${4:?db oid required}; useroid=${5:?user oid required}
    tag=${6:?tag required}; slots=${7:?slots required}; bytes=${8:?bytes required}
    validate_tag "$tag"
    mkdir -p -m 0700 "$root"
    set +e
    sudo -n -u dbcomm env HOMER_VALIDATION_RUN_ID="$run_id" \
        HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200 HOMER_FRONTEND_DPU_SETUP_PORT=9727 \
        HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 \
        "$prefix/bin/pg_basebackup" --homer-receive --homer-node 2 \
        --homer-database-oid "$dboid" --homer-user-oid "$useroid" \
        --homer-slots "$slots" --homer-bytes "$bytes" --homer-tag "$tag" >"$root/consumer.log" 2>&1
    rc=$?
    set -e
    printf '%s\n' "$rc" > "$root/wait_rc.tmp"; mv "$root/wait_rc.tmp" "$root/wait_rc"
    exit 0
    ;;
start)
    shift 2
    [[ $# -eq 6 ]] || { echo "start requires prefix dboid useroid tag slots bytes" >&2; exit 2; }
    validate_tag "$4"
    [[ ! -e "$root" ]] || { echo "consumer state already exists" >&2; exit 2; }
    mkdir -p -m 0700 "$root"
    nohup setsid "$0" supervise "$run_id" "$@" >/dev/null 2>&1 </dev/null &
    supervisor=$!
    start=$(awk '{print $22}' "/proc/$supervisor/stat")
    printf '%s %s\n' "$supervisor" "$start" > "$root/state.tmp"; mv "$root/state.tmp" "$root/state"
    printf 'CONSUMER_STARTED supervisor_pid=%s start=%s\n' "$supervisor" "$start"
    ;;
wait)
    read -r supervisor start < "$root/state"
    for _ in $(seq 1 9000); do [[ -s "$root/wait_rc" ]] && break; sleep 0.1; done
    [[ -s "$root/wait_rc" ]] || { echo "consumer wait status absent" >&2; exit 2; }
    rc=$(cat "$root/wait_rc")
    cat "$root/consumer.log"
    printf 'CONSUMER_WAIT_RC=%s supervisor_pid=%s start=%s\n' "$rc" "$supervisor" "$start"
    exit "$rc"
    ;;
*) echo "unknown action $action" >&2; exit 3 ;;
esac
