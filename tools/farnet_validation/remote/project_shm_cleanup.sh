#!/usr/bin/env bash
# Remove only the runbook-listed Homer/PostgreSQL shared-memory families.
set -euo pipefail
removed=0
for path in /dev/shm/citus_remote_execution_control_* \
            /dev/shm/citus_remote_exec_cmd_* \
            /dev/shm/citus_remote_exec_cpl_* \
            /dev/shm/citus_remote_exec_client_cpl_* \
            /dev/shm/citus_remote_exec_res_v* \
            /dev/shm/citus_remote_exec_backend_spawn_v* \
            /dev/shm/citus_homer_frontend_arena_* \
            /dev/shm/citus_res_*; do
    [[ -e "$path" ]] || continue
    owner=$(stat -c '%U' "$path")
    [[ "$owner" == dbcomm ]] || {
        printf 'refusing foreign shared memory path=%s owner=%s\n' "$path" "$owner" >&2
        exit 2
    }
    sudo -n -u dbcomm rm -- "$path"
    removed=$((removed + 1))
done
printf 'PROJECT_SHM_CLEAN removed=%s\n' "$removed"
