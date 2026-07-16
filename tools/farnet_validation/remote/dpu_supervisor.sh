#!/usr/bin/env bash
# Run a DPU service under a detached, run-scoped supervisor with real wait status.
set -euo pipefail
action=${1:?start|supervise|stop|status|mark|interval|log required}
run_id=${2:?run id required}
state_root="/tmp/farnet-validation-$run_id/dpu-supervisor"
state="$state_root/state"
binary="/tmp/farnet-validation-$run_id/dpu-build/build/homer/citus_tuple_sink_service"

read_state() {
    [[ -r "$state" ]] || { echo "missing supervisor state" >&2; return 2; }
    read -r child_pid child_start child_digest supervisor_pid < "$state"
}

listeners_for_pid() {
    local pid=$1
    ss -H -ltnp '( sport = :9727 or sport = :9728 )' | grep -c "pid=$pid," || true
}

case "$action" in
supervise)
    bind_host=${3:?DPU peer bind host required}
    mkdir -p -m 0700 "$state_root"
    digest=$(sha256sum "$binary" | awk '{print $1}')
    log="$state_root/service.log"
    env HOMER_VALIDATION_RUN_ID="$run_id" HOMER_SERVICE_ENABLE_DPU_DMA=1 \
        HOMER_SERVICE_ENABLE_DOCA_DMA=1 HOMER_SERVICE_DPU_SETUP_PORT=9727 \
        HOMER_SERVICE_DOCA_DEV_PCI=0000:03:00.0 HOMER_SERVICE_PEER_BIND_HOST="$bind_host" \
        HOMER_SERVICE_PEER_PORT=9717 "$binary" >"$log" 2>&1 </dev/null &
    child=$!
    start=$(awk '{print $22}' "/proc/$child/stat")
    printf '%s %s %s %s\n' "$child" "$start" "$digest" "$BASHPID" > "$state.tmp"
    mv "$state.tmp" "$state"
    set +e
    wait "$child"
    rc=$?
    set -e
    printf '%s\n' "$rc" > "$state_root/wait_rc.tmp"
    mv "$state_root/wait_rc.tmp" "$state_root/wait_rc"
    exit 0
    ;;
start)
    bind_host=${3:?DPU peer bind host required}
    if [[ -e "$state_root" ]]; then
        echo "run-scoped DPU supervisor state already exists" >&2; exit 2
    fi
    if ss -H -ltnp '( sport = :9727 or sport = :9728 )' | grep -q .; then
        echo "stale listener refuses supervisor start" >&2; exit 2
    fi
    [[ -x "$binary" ]] || { echo "missing DPU service binary" >&2; exit 2; }
    mkdir -p -m 0700 "$state_root"
    nohup setsid "$0" supervise "$run_id" "$bind_host" >/dev/null 2>&1 </dev/null &
    launcher=$!
    for _ in $(seq 1 100); do
        [[ -s "$state" || -s "$state_root/wait_rc" ]] && break
        sleep 0.1
    done
    [[ -s "$state" ]] || {
        echo "supervisor did not publish state launcher=$launcher" >&2
        [[ -r "$state_root/service.log" ]] && tail -n 120 "$state_root/service.log"
        exit 2
    }
    read_state
    for _ in $(seq 1 100); do
        [[ $(listeners_for_pid "$child_pid") -eq 2 ]] && break
        [[ -s "$state_root/wait_rc" ]] && break
        sleep 0.1
    done
    listener_count=$(listeners_for_pid "$child_pid")
    [[ "$listener_count" -eq 2 ]] || {
        echo "DPU child did not own both listeners child=$child_pid listeners=$listener_count" >&2
        [[ -r "$state_root/service.log" ]] && tail -n 120 "$state_root/service.log"
        exit 2
    }
    printf 'STARTED child_pid=%s child_start=%s exe_digest=%s supervisor_pid=%s listeners=%s\n' \
        "$child_pid" "$child_start" "$child_digest" "$supervisor_pid" "$listener_count"
    ;;
stop)
    read_state
    if [[ -r "/proc/$child_pid/stat" ]]; then
        current_start=$(awk '{print $22}' "/proc/$child_pid/stat")
        current_digest=$(sha256sum "/proc/$child_pid/exe" | awk '{print $1}')
        [[ "$current_start" == "$child_start" && "$current_digest" == "$child_digest" ]] || {
            echo "DPU child identity changed" >&2; exit 2; }
        kill -TERM "$child_pid"
    fi
    for _ in $(seq 1 200); do
        [[ -s "$state_root/wait_rc" ]] && break
        sleep 0.1
    done
    [[ -s "$state_root/wait_rc" ]] || { echo "supervisor wait status absent" >&2; exit 2; }
    rc=$(cat "$state_root/wait_rc")
    [[ ! -e "/proc/$child_pid" ]] || { echo "DPU child remains after wait status" >&2; exit 2; }
    listener_count=$(ss -H -ltnp '( sport = :9727 or sport = :9728 )' | wc -l)
    [[ "$listener_count" -eq 0 ]] || { echo "DPU listeners remain after stop" >&2; exit 2; }
    printf 'STOPPED child_pid=%s wait_rc=%s listeners=%s\n' "$child_pid" "$rc" "$listener_count"
    [[ "$rc" -eq 0 ]]
    ;;
status)
    read_state
    alive=0
    identity_ok=0
    listeners=0
    if [[ -r "/proc/$child_pid/stat" ]]; then
        alive=1
        current_start=$(awk '{print $22}' "/proc/$child_pid/stat")
        current_digest=$(sha256sum "/proc/$child_pid/exe" | awk '{print $1}')
        [[ "$current_start" == "$child_start" && "$current_digest" == "$child_digest" ]] && identity_ok=1
        listeners=$(listeners_for_pid "$child_pid")
    fi
    wait_rc=NONE; [[ -r "$state_root/wait_rc" ]] && wait_rc=$(cat "$state_root/wait_rc")
    printf 'STATUS child_pid=%s child_start=%s exe_digest=%s supervisor_pid=%s alive=%s identity_ok=%s listeners=%s wait_rc=%s log=%s\n' \
        "$child_pid" "$child_start" "$child_digest" "$supervisor_pid" "$alive" "$identity_ok" \
        "$listeners" "$wait_rc" "$state_root/service.log"
    if [[ "$alive" -eq 1 ]]; then
        [[ "$identity_ok" -eq 1 && "$listeners" -eq 2 && "$wait_rc" == NONE ]]
    else
        [[ "$wait_rc" == 0 && "$listeners" -eq 0 ]]
    fi
    ;;
mark)
    label=${3:?candidate label required}
    [[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "unsafe candidate label" >&2; exit 3; }
    log="$state_root/service.log"; [[ -f "$log" ]] || { echo "service log absent" >&2; exit 2; }
    read -r dev inode size < <(stat -c '%d %i %s' "$log")
    printf '%s %s %s\n' "$dev" "$inode" "$size" > "$state_root/mark-$label.tmp"
    mv "$state_root/mark-$label.tmp" "$state_root/mark-$label"
    printf 'LOG_MARK label=%s device=%s inode=%s offset=%s\n' "$label" "$dev" "$inode" "$size"
    ;;
interval)
    label=${3:?candidate label required}
    [[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "unsafe candidate label" >&2; exit 3; }
    read -r start_dev start_inode start_offset < "$state_root/mark-$label"
    log="$state_root/service.log"; read -r end_dev end_inode end_offset < <(stat -c '%d %i %s' "$log")
    [[ "$start_dev" == "$end_dev" && "$start_inode" == "$end_inode" ]] || {
        echo "service log rotated during candidate" >&2; exit 2; }
    (( end_offset >= start_offset )) || { echo "service log truncated during candidate" >&2; exit 2; }
    count=$((end_offset - start_offset))
    printf 'LOG_INTERVAL label=%s device=%s inode=%s start=%s end=%s\n' \
        "$label" "$end_dev" "$end_inode" "$start_offset" "$end_offset"
    dd if="$log" iflag=skip_bytes,count_bytes skip="$start_offset" count="$count" status=none
    ;;
log)
    cat "$state_root/service.log"
    ;;
*) echo "unknown action: $action" >&2; exit 3 ;;
esac
