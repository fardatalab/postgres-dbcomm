#!/usr/bin/env bash
# Supervise the blocking DPU smoke server so the host client can start second.
set -euo pipefail
action=${1:?start|supervise|wait required}; run_id=${2:?run id required}
root="/tmp/farnet-validation-$run_id/dpu-tcp-smoke"
binary="/tmp/farnet-validation-$run_id/dpu-build/build/homer/homer_dpu_tcp_transport_smoke"
case "$action" in
supervise)
    mkdir -p -m 0700 "$root"
    set +e
    "$binary" --server --dev-pci 0000:03:00.0 --port 9727 --timeout-ms 45000 \
        --expect-backend-command-publish --expect-response-publish \
        --expect-backend-completion-pull --expect-byte-ring-pull \
        --expect-dpu-to-host-payload --expect-loop2-backpressure >"$root/server.log" 2>&1
    rc=$?
    set -e
    printf '%s\n' "$rc" > "$root/wait_rc.tmp"; mv "$root/wait_rc.tmp" "$root/wait_rc"
    exit 0
    ;;
start)
    [[ -x "$binary" ]] || { echo "candidate smoke server absent" >&2; exit 2; }
    [[ ! -e "$root" ]] || { echo "smoke state already exists" >&2; exit 2; }
    mkdir -p -m 0700 "$root"
    nohup setsid "$0" supervise "$run_id" >/dev/null 2>&1 </dev/null &
    supervisor=$!; start=$(awk '{print $22}' "/proc/$supervisor/stat")
    printf '%s %s %s\n' "$supervisor" "$start" "$(sha256sum "$binary" | awk '{print $1}')" \
        > "$root/state.tmp"; mv "$root/state.tmp" "$root/state"
    printf 'SMOKE_SERVER_STARTED supervisor_pid=%s start=%s digest=%s\n' \
        "$supervisor" "$start" "$(awk '{print $3}' "$root/state")"
    ;;
wait)
    read -r supervisor start digest < "$root/state"
    for _ in $(seq 1 500); do [[ -s "$root/wait_rc" ]] && break; sleep 0.1; done
    [[ -s "$root/wait_rc" ]] || { echo "smoke server wait status absent" >&2; exit 2; }
    rc=$(cat "$root/wait_rc"); cat "$root/server.log"
    printf 'SMOKE_SERVER_WAIT_RC=%s supervisor_pid=%s start=%s digest=%s\n' "$rc" "$supervisor" "$start" "$digest"
    exit "$rc"
    ;;
*) echo "unknown action $action" >&2; exit 3 ;;
esac
