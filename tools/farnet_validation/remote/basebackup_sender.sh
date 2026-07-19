#!/usr/bin/env bash
# Start/wait the four-role basebackup SENDER in the background, retaining its real exit
# status and a start timestamp in run-scoped files.
#
# Why this exists: the operator runbook runs the sender in the FOREGROUND, which makes it
# impossible to overlap with another workload without hand-orchestration. The MIXED
# validation workload (gate pgbench concurrent with basebackup, see AGENTS.md) needs the
# sender backgrounded so pgbench can run inside its streaming window. This mirrors
# basebackup_consumer.sh's proven start/supervise/wait pattern exactly -- same nohup+setsid
# supervision, same run-scoped state dir, same real-rc preservation -- so there is one
# lifecycle idiom to reason about, not two.
#
# The recorded START/END epoch timestamps are LOAD-BEARING for the mixed workload: they are
# what proves the two workloads' live windows actually OVERLAPPED. A "concurrent" run whose
# windows did not overlap is a sequential run in disguise and proves nothing about byte-ring
# pool contention.
set -euo pipefail
action=${1:?start|supervise|wait required}; run_id=${2:?run id required}
root="/tmp/farnet-validation-$run_id/basebackup-sender"

# The sender target option is parsed with pg_strtoint32; reject a bad tag synchronously,
# before creating state or a process that would otherwise fail late. Same rule as the
# consumer -- both ends parse the tag as signed int32.
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
    prefix=${3:?prefix required}; tag=${4:?tag required}
    slots=${5:?slots required}; bytes=${6:?bytes required}
    recv_host=${7:?receiver host required}; recv_port=${8:?receiver port required}
    validate_tag "$tag"
    mkdir -p -m 0700 "$root"
    date +%s > "$root/start_epoch"
    set +e
    sudo -n -u dbcomm "$prefix/bin/pg_basebackup" \
        -h /tmp -p 5433 -U dbcomm -X none -c fast \
        -t "homer:mode=rdma,host=$recv_host,port=$recv_port,node=2,slots=$slots,bytes=$bytes,tag=$tag" \
        -v >"$root/sender.log" 2>&1
    rc=$?
    set -e
    date +%s > "$root/end_epoch"
    printf '%s\n' "$rc" > "$root/wait_rc.tmp"; mv "$root/wait_rc.tmp" "$root/wait_rc"
    exit 0
    ;;
start)
    shift 2
    [[ $# -eq 6 ]] || {
        echo "start requires prefix tag slots bytes recv_host recv_port" >&2; exit 2; }
    validate_tag "$2"
    [[ ! -e "$root" ]] || { echo "sender state already exists" >&2; exit 2; }
    mkdir -p -m 0700 "$root"
    nohup setsid "$0" supervise "$run_id" "$@" >/dev/null 2>&1 </dev/null &
    supervisor=$!
    # Field 22 is starttime: pid+starttime is the only non-racy process identity.
    start=$(awk '{print $22}' "/proc/$supervisor/stat")
    printf '%s %s\n' "$supervisor" "$start" > "$root/state.tmp"; mv "$root/state.tmp" "$root/state"
    printf 'SENDER_STARTED supervisor_pid=%s start=%s\n' "$supervisor" "$start"
    ;;
# alive: cheap liveness gate -- the sender supervisor is still running and has begun writing.
#
# ⚠ This is deliberately NOT the authoritative "streaming has begun" signal, and must not be
# used as one. The trustworthy marker lives on the CONSUMER side: the line
#   "consuming basebackup stream node=... requested_slots=... bytes=... tag=..."
# which checks.basebackup_check already parses (checks.py:155), i.e. a checked-in anchor
# rather than a guessed sender string. The mixed-workload orchestrator gates pgbench launch
# on THAT. This action only answers "did the sender fail before it ever got going?".
alive)
    if [[ -s "$root/wait_rc" ]]; then
        printf 'SENDER_ALIVE=no rc=%s\n' "$(cat "$root/wait_rc")" >&2
        exit 2
    fi
    [[ -s "$root/state" ]] || { echo "SENDER_ALIVE=no reason=no_state" >&2; exit 2; }
    printf 'SENDER_ALIVE=yes at_epoch=%s\n' "$(date +%s)"
    ;;
wait)
    read -r supervisor start < "$root/state"
    for _ in $(seq 1 9000); do [[ -s "$root/wait_rc" ]] && break; sleep 0.1; done
    [[ -s "$root/wait_rc" ]] || { echo "sender wait status absent" >&2; exit 2; }
    rc=$(cat "$root/wait_rc")
    cat "$root/sender.log"
    printf 'SENDER_WAIT_RC=%s supervisor_pid=%s start=%s start_epoch=%s end_epoch=%s\n' \
        "$rc" "$supervisor" "$start" \
        "$(cat "$root/start_epoch" 2>/dev/null || echo -)" \
        "$(cat "$root/end_epoch" 2>/dev/null || echo -)"
    exit "$rc"
    ;;
*) echo "unknown action $action" >&2; exit 3 ;;
esac
