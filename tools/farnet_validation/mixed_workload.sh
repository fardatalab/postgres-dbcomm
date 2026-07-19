#!/usr/bin/env bash
# Orchestrate the MIXED validation workload: gate pgbench running CONCURRENTLY with a
# four-role selected-DPU basebackup (see AGENTS.md "Validation and workload policy").
#
# WHY A SCRIPT: every prior acceptance run was sequential (gate -> basebackup -> smoke), so
# byte-ring pool contention between tuple and bulk sessions has never been exercised. Doing
# that overlap by hand is both laborious and easy to get silently wrong -- if one workload
# finishes before the other starts, you have a SEQUENTIAL run wearing a concurrent costume,
# and it proves nothing. This script makes the overlap explicit, gated, and evidenced.
#
# ⚠ STATUS: NOT YET EXERCISED. Written alongside the first (manually orchestrated) mixed run.
# Until a mixed run has actually been driven THROUGH this script and passed, treat it as
# unproven tooling: prefer the manual runbook procedure for a verdict-bearing run, and use
# this to reproduce the shape.
#
# SAFETY: single-hop ssh only (never `ssh a ssh b`); invoked by path, no heredoc-in-bash -c.
# Our PostgreSQL is port 5433; farnet1 also runs a FOREIGN 5432 instance -- never touch it.
set -euo pipefail

run_id=${1:?run id required}
prefix=${2:-/data/dbcomm/pg-citus}
dboid=${3:?db oid required}
useroid=${4:?user oid required}
tag=${5:?tag required}
clients=${6:-4}            # 4 => 2*4 = 8 receiver byte-ring slots; see the budget note below
transactions=${7:-2000}    # sized so pgbench lives INSIDE the basebackup window
bb_slots=${8:-4}
bb_bytes=${9:-524288}
recv_host=${10:-10.10.1.200}   # farnet0 DPU (receiver for BOTH workloads)
recv_port=${11:-9717}

helpers="/tmp/farnet-validation-$run_id/helpers"
consumer_log="/tmp/farnet-validation-$run_id/basebackup-consumer/consumer.log"

# ---------------------------------------------------------------------------------------
# BYTE-RING SLOT BUDGET (the binding constraint -- derivation in the transport CONTRACTS.md
# entry for HOMER_DPU_BYTE_RING_SLOTS_PER_REGION):
#   pool  = HOMER_DPU_BYTE_RING_POOL_REGIONS(2) x HOMER_DPU_BYTE_RING_SLOTS_PER_REGION(8) = 16 PER DPU
#   need  = 2 * (concurrent pgbench sessions) + 1 * (concurrent basebackup sessions)   [receiver DPU]
#   here  = 2*clients + 1, and farnet0 is the receiver for BOTH workloads.
# Exhaustion is NOT graceful: a session that finds no free slot sets engine->fatalError and
# kills command-pull/byte-ring-pull/PE-drain for EVERY session on that DPU. Refuse to launch
# a shape that does not leave headroom.
# ---------------------------------------------------------------------------------------
POOL_TOTAL=16
need=$(( 2 * clients + 1 ))
if (( need > POOL_TOTAL - 2 )); then
    echo "REFUSING: byte-ring budget too tight: need=$need of $POOL_TOTAL (2*${clients}+1);" \
         "leave >=2 slots headroom (use -c4 => 9/16). Raise HOMER_DPU_BYTE_RING_SLOTS_PER_REGION" \
         "and rebuild BOTH DPUs if you truly need more." >&2
    exit 2
fi
printf 'BUDGET ok: clients=%s need=%s/%s on the receiver DPU (headroom=%s)\n' \
    "$clients" "$need" "$POOL_TOTAL" "$(( POOL_TOTAL - need ))"

# 1. Consumer first (farnet0) -- it must be listening before the sender opens the stream.
ssh farnet0 "$helpers/basebackup_consumer.sh" start "$run_id" \
    "$prefix" "$dboid" "$useroid" "$tag" "$bb_slots" "$bb_bytes"

# 2. Sender (farnet1), BACKGROUNDED so pgbench can run inside its window.
ssh farnet1 "$helpers/basebackup_sender.sh" start "$run_id" \
    "$prefix" "$tag" "$bb_slots" "$bb_bytes" "$recv_host" "$recv_port"

# 3. Gate on the CONSUMER's checked-in stream-open anchor (the same line
#    checks.basebackup_check parses) -- NOT a guessed sender string. Only once the stream is
#    genuinely open may pgbench launch; otherwise the "overlap" is fiction.
stream_open_epoch=""
for _ in $(seq 1 1200); do
    if ssh farnet0 "sudo -n grep -c 'consuming basebackup stream' '$consumer_log' 2>/dev/null || true" \
        | grep -qE '^[1-9]'; then
        stream_open_epoch=$(date +%s)
        break
    fi
    # Fail fast if the sender already died rather than spinning to the timeout.
    if ! ssh farnet1 "$helpers/basebackup_sender.sh" alive "$run_id" >/dev/null 2>&1; then
        echo "ABORT: sender terminated before the stream opened; mixed overlap impossible" >&2
        ssh farnet1 "$helpers/basebackup_sender.sh" wait "$run_id" || true
        exit 2
    fi
    sleep 0.1
done
[[ -n "$stream_open_epoch" ]] || { echo "ABORT: basebackup stream never opened" >&2; exit 2; }
printf 'STREAM_OPEN_EPOCH=%s\n' "$stream_open_epoch"

# 4. Gate pgbench (farnet0), in the foreground, INSIDE the streaming window.
gate_start_epoch=$(date +%s)
printf 'GATE_START_EPOCH=%s\n' "$gate_start_epoch"
set +e
ssh farnet0 "$helpers/gate.sh" "$prefix" "$dboid" "$useroid" "$transactions" 0 "$clients" "$clients"
gate_rc=$?
set -e
gate_end_epoch=$(date +%s)
printf 'GATE_END_EPOCH=%s GATE_RC=%s\n' "$gate_end_epoch" "$gate_rc"

# 5. Reap both, preserving real statuses.
set +e
ssh farnet1 "$helpers/basebackup_sender.sh" wait "$run_id"; sender_rc=$?
ssh farnet0 "$helpers/basebackup_consumer.sh" wait "$run_id"; consumer_rc=$?
set -e

# 6. OVERLAP EVIDENCE -- the acceptance-critical output. The gate must have both started AND
#    finished strictly inside the basebackup's live window, or the run did not test coexistence.
sender_end_epoch=$(ssh farnet1 "cat /tmp/farnet-validation-$run_id/basebackup-sender/end_epoch 2>/dev/null || echo 0")
printf 'MIXED_EVIDENCE stream_open=%s gate_start=%s gate_end=%s sender_end=%s gate_rc=%s sender_rc=%s consumer_rc=%s\n' \
    "$stream_open_epoch" "$gate_start_epoch" "$gate_end_epoch" "$sender_end_epoch" \
    "$gate_rc" "$sender_rc" "$consumer_rc"
if (( gate_start_epoch >= stream_open_epoch && sender_end_epoch > gate_start_epoch )); then
    printf 'MIXED_OVERLAP=yes overlap_seconds=%s\n' "$(( (sender_end_epoch < gate_end_epoch ? sender_end_epoch : gate_end_epoch) - gate_start_epoch ))"
else
    printf 'MIXED_OVERLAP=no -- SEQUENTIAL IN DISGUISE; this run does NOT validate the mixed property\n'
fi
(( gate_rc == 0 && sender_rc == 0 && consumer_rc == 0 )) || exit 1
