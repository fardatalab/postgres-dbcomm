#!/usr/bin/env bash
# Expand one immutable archive into a run-owned tree and preserve make's real status.
set -euo pipefail
run_id=${1:?run id required}
archive=${2:?source archive required}
shift 2
(( $# > 0 )) || { echo "at least one target required" >&2; exit 3; }
root="/tmp/farnet-validation-$run_id/dpu-build"
rm -rf "$root"
mkdir -p -m 0700 "$root"
tar -xf "$archive" -C "$root"
cd "$root"
PG_CONFIG=/usr/bin/pg_config ./configure --without-libcurl > configure.log 2>&1
make -B -j8 "$@" CPPFLAGS=-D_GNU_SOURCE > build.log 2>&1
binary=build/homer/citus_tuple_sink_service
[[ -x "$binary" ]] || { echo "selected output absent" >&2; exit 1; }
outputs=("$binary")
if [[ " $* " == *" dpu-tcp-transport-smoke-bin "* ]]; then
    smoke=build/homer/homer_dpu_tcp_transport_smoke
    [[ -x "$smoke" ]] || { echo "DPU TCP smoke output absent" >&2; exit 1; }
    outputs+=("$smoke")
fi
sha256sum "${outputs[@]}" configure.log build.log
