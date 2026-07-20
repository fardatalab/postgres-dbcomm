#!/usr/bin/env bash
# Expand one immutable archive into a run-owned tree and preserve make's real status.
#
# To build a DIAGNOSTIC DPU service, pass one or more --define=NAME[=VALUE] arguments after the
# archive, e.g.  --define=HOMER_COLLECTOR_STARVE_DIAG=1.  -D_GNU_SOURCE is ALWAYS supplied and is
# not the caller's business.
#
# ⚠ TWO INTERFACE HAZARDS, both learned by getting them wrong on a real run (2026-07-19):
#
# 1. NO SPACES IN AN ARGUMENT VALUE, EVER. This deliberately takes repeatable single-macro
#    arguments instead of one `--cppflags='-D_GNU_SOURCE -DFOO=1'` string. `ssh` does not forward
#    an argument VECTOR: it concatenates its arguments and hands one COMMAND STRING to the remote
#    shell, which re-splits on whitespace. A space inside a single value therefore splits into two
#    arguments across the hop -- and farnet0's DPU is reached through a SECOND hop
#    (dpu_dispatch.sh), so it needs a further layer of quoting that is easy to get wrong in one
#    place and right in the other. Keeping every value space-free makes the whole class impossible.
# 2. Pass these POSITIONALLY, never via the environment: dpu_dispatch.sh reaches farnet0's DPU with
#    `exec ssh dpu "$remote/$action" "$@"`, which forwards arguments but silently drops exported
#    variables -- an env-var interface would appear to work on farnet1 and quietly build a
#    NON-diagnostic binary on farnet0, i.e. exactly the asymmetric-evidence trap we must not create.
#
# ⚠ The "a diagnostic `make -B` build is sticky" hazard (AGENTS.md safety rule 6) does NOT apply
# here, and this is why: this script `rm -rf`s the build root and re-extracts the archive on EVERY
# invocation, so each DPU build is hermetic and cannot relink a previous run's instrumented
# objects. Stickiness remains a real hazard on the HOST trees, which are incremental.
set -euo pipefail
run_id=${1:?run id required}
archive=${2:?source archive required}
shift 2
# _GNU_SOURCE is unconditional: omitting it changes feature-test macros and the build fails far
# from here, so it is not exposed as a caller decision at all.
cppflags=-D_GNU_SOURCE
while [[ ${1-} == --define=* ]]; do
    define=${1#--define=}
    # A value containing whitespace cannot have survived the ssh hop intact; it means the caller's
    # quoting was lost and the macro they THINK they set is not the one being compiled. Refuse it
    # rather than build a subtly wrong binary and report success.
    [[ "$define" == *[[:space:]]* ]] && { echo "--define value must not contain whitespace: $define" >&2; exit 3; }
    [[ -n "$define" ]] || { echo "--define requires a macro name" >&2; exit 3; }
    cppflags="$cppflags -D$define"
    shift
done
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
#
# ⚠ DO NOT "SIMPLIFY" THE CHECK BELOW BACK TO `strings ... | grep -q`. That is what it said first,
# and under this file's `set -o pipefail` it FAILED UNCONDITIONALLY -- including, especially, when
# the strings were present. `grep -q` exits at the FIRST match, `strings` then dies of SIGPIPE, and
# pipefail reports that 141 as the PIPELINE's status. So the assertion fired on success (early
# match -> SIGPIPE) and on failure (no match -> grep 1) alike, and it aborted a validation run on
# two correctly-instrumented binaries. Plain `grep >/dev/null` consumes the whole stream, so
# `strings` exits 0 and the status means what it appears to mean.
if [[ "$cppflags" == *HOMER_COLLECTOR_STARVE_DIAG* ]]; then
    if ! strings "$binary" | grep 'starve-diag' > /dev/null; then
        echo "HOMER_COLLECTOR_STARVE_DIAG requested but no starve-diag strings in the binary" >&2
        exit 1
    fi
fi
outputs=("$binary")
if [[ " $* " == *" dpu-tcp-transport-smoke-bin "* ]]; then
    smoke=build/homer/homer_dpu_tcp_transport_smoke
    [[ -x "$smoke" ]] || { echo "DPU TCP smoke output absent" >&2; exit 1; }
    outputs+=("$smoke")
fi
sha256sum "${outputs[@]}" configure.log build.log
