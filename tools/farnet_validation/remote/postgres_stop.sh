#!/usr/bin/env bash
# Stop only the selected PostgreSQL data directory; already-stopped is clean.
set -euo pipefail
prefix=${1:?installed prefix required}
pg_ctl="$prefix/bin/pg_ctl"
data="$prefix/data"

set +e
sudo -n -u dbcomm "$pg_ctl" -D "$data" status >/dev/null 2>&1
status_rc=$?
set -e
case "$status_rc" in
0) exec sudo -n -u dbcomm "$pg_ctl" -D "$data" stop -m fast ;;
3) printf 'POSTGRES_STOPPED data=%s already_stopped=1\n' "$data" ;;
*) printf 'could not establish PostgreSQL status data=%s rc=%s\n' "$data" "$status_rc" >&2; exit 2 ;;
esac
