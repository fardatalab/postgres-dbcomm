#!/usr/bin/env bash
# Prove DPU-native service identity and expose stale 9727/9728 listeners.
set -euo pipefail
run_id=${1:?run id required}
expectation=${2:-any}
expected_exe="/tmp/farnet-validation-$run_id/dpu-build/build/homer/citus_tuple_sink_service"
found=0
expected_found=0
unexpected=0
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
    case "$exe" in
        */citus-dbcomm/build/homer/citus_tuple_sink_service*|*/dpu-build/build/homer/citus_tuple_sink_service*)
            start=$(awk '{print $22}' "$proc/stat")
            digest=$(sha256sum "$proc/exe" | awk '{print $1}')
            printf 'DPU_SERVICE pid=%s start=%s digest=%s exe=%s run=%s\n' "$pid" "$start" "$digest" "$exe" "$run_id"
            found=$((found + 1))
            if [[ "$exe" == "$expected_exe" ]]; then expected_found=$((expected_found + 1)); expected_pid=$pid
            else unexpected=$((unexpected + 1)); fi
            ;;
    esac
done
listener_output=$(ss -H -ltnp '( sport = :9727 or sport = :9728 )') || exit 2
listeners=$(printf '%s\n' "$listener_output" | sed '/^$/d' | wc -l)
nonzero_recvq=$(printf '%s\n' "$listener_output" | awk '$2 != 0 { count++ } END { print count + 0 }')
owned_listeners=0
if [[ -n "${expected_pid:-}" ]]; then
    owned_listeners=$(printf '%s\n' "$listener_output" | grep -c "pid=$expected_pid," || true)
fi
printf '%s\n' "$listener_output"
printf 'SUMMARY services=%s expected_services=%s unexpected_services=%s expectation=%s listeners=%s owned_listeners=%s nonzero_recvq=%s unreadable_user=%s expected_exe=%s\n' \
    "$found" "$expected_found" "$unexpected" "$expectation" "$listeners" "$owned_listeners" \
    "$nonzero_recvq" "$unreadable" "$expected_exe"
(( unreadable == 0 )) || exit 2
case "$expectation" in
    one) (( found == 1 && expected_found == 1 && unexpected == 0 && listeners == 2 && owned_listeners == 2 && nonzero_recvq == 0 )) ;;
    zero) (( found == 0 && listeners == 0 )) ;;
    any) (( found <= 1 && unexpected == 0 )) ;;
    *) echo "unknown expectation $expectation" >&2; exit 3 ;;
esac
