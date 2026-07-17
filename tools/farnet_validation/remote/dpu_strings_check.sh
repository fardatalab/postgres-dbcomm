#!/usr/bin/env bash
# MEASUREMENT-ONLY helper: grep a run-scoped DPU-built binary for a literal string,
# without -q (hazard 3.3b: `grep -q` under a piped `strings` SIGPIPEs and can flip
# a real match into a reported pipeline failure).
set -euo pipefail
run_id=${1:?run id required}
literal=${2:?literal required}
binary="/tmp/farnet-validation-$run_id/dpu-build/build/homer/citus_tuple_sink_service"
[[ -x "$binary" ]] || { echo "binary absent: $binary" >&2; exit 2; }
if strings "$binary" | grep -F "$literal" >/dev/null; then
    echo "PRESENT"
else
    echo "ABSENT"
fi
