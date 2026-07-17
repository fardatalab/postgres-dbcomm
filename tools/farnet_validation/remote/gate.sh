#!/usr/bin/env bash
# Execute the selected-DPU gate with local DPU setup and remote DPU peer kept distinct.
set -euo pipefail
prefix=${1:?prefix required}; dboid=${2:?db oid required}; useroid=${3:?user oid required}
transactions=${4:?transaction count required}; debug=${5:?debug flag required}
# S6 Track A (2026-07-17): native --homer-dpu now IS the selected-DPU command path (the fold);
# --homer-dpu-command is a deprecated one-cycle alias. Exercise the native flag here.
args=("$prefix/bin/pgbench" -h /tmp -p 5433 -U dbcomm --homer --homer-dpu
      --homer-database-oid "$dboid" --homer-user-oid "$useroid"
      --homer-peer-host 10.10.1.201 --homer-peer-port 9717 --homer-peer-node 1
      --latency-percentiles --client-cpu=3 -n -M simple -c 1 -j 1 -t "$transactions" postgres)
[[ "$debug" == 1 ]] && args+=(--debug)
exec sudo -n -u dbcomm env HOMER_FRONTEND_DPU_SETUP_HOST=10.10.1.200 \
    HOMER_FRONTEND_DPU_SETUP_PORT=9727 HOMER_FRONTEND_DOCA_DEV_PCI=0000:21:00.0 \
    HOMER_FRONTEND_DPU_SETUP_TIMEOUT_MS=15000 "${args[@]}"
