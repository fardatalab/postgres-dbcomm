#!/usr/bin/env bash
# Checked-in farnet0 helper for file-based second-hop DPU operations.
set -euo pipefail
action=${1:?prepare, install, install-data, or helper name required}
run_id_from_path=$(basename "$(dirname "$(dirname "$0")")")
remote="/tmp/$run_id_from_path/helpers"
if [[ "$action" == prepare ]]; then
    exec ssh dpu install -d -m 0700 "$remote"
fi
if [[ "$action" == install ]]; then
    name=${2:?helper name required}
    scp "$(dirname "$0")/$name" "dpu:$remote/.$name.upload"
    ssh dpu install -m 0700 "$remote/.$name.upload" "$remote/$name"
    local_hash=$(sha256sum "$(dirname "$0")/$name" | awk '{print $1}')
    remote_hash=$(ssh dpu sha256sum "$remote/$name" | awk '{print $1}')
    [[ "$local_hash" == "$remote_hash" ]] || { echo "DPU helper digest mismatch" >&2; exit 2; }
    exit 0
fi
if [[ "$action" == install-data ]]; then
    source_path=${2:?staged data path required}
    destination="/tmp/$run_id_from_path/$(basename "$source_path")"
    scp "$source_path" "dpu:$destination.upload"
    ssh dpu mv "$destination.upload" "$destination"
    local_hash=$(sha256sum "$source_path" | awk '{print $1}')
    remote_hash=$(ssh dpu sha256sum "$destination" | awk '{print $1}')
    [[ "$local_hash" == "$remote_hash" ]] || { echo "DPU data digest mismatch" >&2; exit 2; }
    exit 0
fi
shift
exec ssh dpu "$remote/$action" "$@"
