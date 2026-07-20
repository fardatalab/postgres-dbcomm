#!/usr/bin/env bash
# Expand one immutable archive into a run-owned tree and preserve make's real status.
#
# Optional first argument after the archive: --cppflags=<flags>, to build a DIAGNOSTIC DPU
# service (e.g. --cppflags='-D_GNU_SOURCE -DHOMER_COLLECTOR_STARVE_DIAG=1'). Passing the flags
# positionally rather than via the environment is deliberate: dpu_dispatch.sh reaches farnet0's
# DPU with `exec ssh dpu "$remote/$action" "$@"`, which forwards ARGUMENTS but silently drops
# exported variables -- an env-var interface would appear to work on farnet1 and quietly build a
# NON-diagnostic binary on farnet0, i.e. exactly the asymmetric-evidence trap we must not create.
#
# ⚠ The "a diagnostic `make -B` build is sticky" hazard (AGENTS.md safety rule 6) does NOT apply
# here, and this is why: this script `rm -rf`s the build root and re-extracts the archive on EVERY
# invocation, so each DPU build is hermetic and cannot relink a previous run's instrumented
# objects. Stickiness remains a real hazard on the HOST trees, which are incremental.
set -euo pipefail
run_id=${1:?run id required}
archive=${2:?source archive required}
shift 2
cppflags=-D_GNU_SOURCE
if [[ ${1-} == --cppflags=* ]]; then
    cppflags=${1#--cppflags=}
    shift
    # _GNU_SOURCE is not optional: omitting it changes feature-test macros and the build fails
    # far from here, so refuse a caller-supplied set that forgot it rather than debug the fallout.
    [[ "$cppflags" == *-D_GNU_SOURCE* ]] || { echo "--cppflags must retain -D_GNU_SOURCE" >&2; exit 3; }
fi
(( $# > 0 )) || { echo "at least one target required" >&2; exit 3; }
root="/tmp/farnet-validation-$run_id/dpu-build"
rm -rf "$root"
mkdir -p -m 0700 "$root"
tar -xf "$archive" -C "$root"
cd "$root"
PG_CONFIG=/usr/bin/pg_config ./configure --without-libcurl > configure.log 2>&1
make -B -j8 "$@" CPPFLAGS="$cppflags" > build.log 2>&1
binary=build/homer/citus_tuple_sink_service
[[ -x "$binary" ]] || { echo "selected output absent" >&2; exit 1; }

# POSITIVE instrumentation proof -- the inverse of the runbook's negative scan, and load-bearing
# for exactly one failure mode: if a diagnostic macro is misspelled or not threaded through, the
# build still SUCCEEDS and produces a silent binary. Its silence is then indistinguishable from
# "the instrumented condition never occurred", so absence-of-output would be read as evidence
# AGAINST the hypothesis under test when it is really evidence of a missing probe. Fail loudly.
if [[ "$cppflags" == *HOMER_COLLECTOR_STARVE_DIAG* ]]; then
    strings "$binary" | grep -q 'starve-diag' || {
        echo "HOMER_COLLECTOR_STARVE_DIAG requested but no starve-diag strings in the binary" >&2
        exit 1
    }
fi
outputs=("$binary")
if [[ " $* " == *" dpu-tcp-transport-smoke-bin "* ]]; then
    smoke=build/homer/homer_dpu_tcp_transport_smoke
    [[ -x "$smoke" ]] || { echo "DPU TCP smoke output absent" >&2; exit 1; }
    outputs+=("$smoke")
fi
sha256sum "${outputs[@]}" configure.log build.log
