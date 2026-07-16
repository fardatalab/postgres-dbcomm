#!/usr/bin/env bash
# Stop only the selected PostgreSQL data directory; already-stopped is clean.
set -euo pipefail
prefix=${1:?installed prefix required}
missing_data_capability=${2:-require-data}
pg_ctl="$prefix/bin/pg_ctl"
data="$prefix/data"

case "$missing_data_capability" in
require-data|--allow-missing-data) ;;
*)
    printf 'unknown missing-data capability %q\n' "$missing_data_capability" >&2
    exit 3
    ;;
esac

# An absent peer data directory is a declared topology fact, not evidence that
# every missing directory is safe.  Require both a usable installed pg_ctl and
# an explicit caller capability before accepting this role as already stopped.
if [[ ! -x "$pg_ctl" ]]; then
    printf 'could not establish PostgreSQL identity pg_ctl=%q reason=missing-or-not-executable\n' "$pg_ctl" >&2
    exit 2
fi
if [[ ! -d "$data" ]]; then
    if [[ "$missing_data_capability" == "--allow-missing-data" ]]; then
        printf 'POSTGRES_STOPPED data=%q already_stopped=1 missing_data=1 capability=allow-missing-data\n' "$data"
        exit 0
    fi
    printf 'could not establish PostgreSQL status data=%q reason=missing-data capability=require-data\n' "$data" >&2
    exit 2
fi

set +e
sudo -n -u dbcomm "$pg_ctl" -D "$data" status >/dev/null 2>&1
status_rc=$?
set -e
case "$status_rc" in
0) exec sudo -n -u dbcomm "$pg_ctl" -D "$data" stop -m fast ;;
3) printf 'POSTGRES_STOPPED data=%s already_stopped=1\n' "$data" ;;
*) printf 'could not establish PostgreSQL status data=%s rc=%s\n' "$data" "$status_rc" >&2; exit 2 ;;
esac
