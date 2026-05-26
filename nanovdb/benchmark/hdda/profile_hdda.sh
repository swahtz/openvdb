#!/usr/bin/env bash
# Copyright Contributors to the OpenVDB Project
# SPDX-License-Identifier: Apache-2.0
#
# Drive an A/B ncu comparison of the raytrace examples before/after the lean
# HDDA refactor. The script assumes you've built two trees side-by-side:
#
#   build-baseline/  - built from the pre-refactor commit
#   build-lean/      - built from the lean tree
#
# Both should be configured with:
#   cmake -DNANOVDB_BUILD_EXAMPLES=ON -DNANOVDB_USE_CUDA=ON \
#         -DNANOVDB_CUDA_PTXAS_VERBOSE=ON -DNANOVDB_USE_NVTX=ON ...
#
# The PTXAS_VERBOSE option makes the ptxas register count visible at compile
# time, and NVTX lets ncu --nvtx --nvtx-include "raytrace_kernel/" isolate the
# kernel from any incidental setup work.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
METRICS_FILE="${SCRIPT_DIR}/metrics.txt"

BASELINE_DIR="${BASELINE_DIR:-build-baseline}"
LEAN_DIR="${LEAN_DIR:-build-lean}"
OUT_DIR="${OUT_DIR:-${SCRIPT_DIR}/reports}"
NCU="${NCU:-ncu}"

# Slim defaults so a single run is reasonably quick - tune via env if needed.
WARMUP="${WARMUP:-5}"
ITERS="${ITERS:-20}"
WIDTH="${WIDTH:-1024}"
HEIGHT="${HEIGHT:-1024}"

mkdir -p "${OUT_DIR}"

# Read the metric list (one per line, # comments allowed) into a comma string.
if [[ ! -f "${METRICS_FILE}" ]]; then
    echo "metrics file not found: ${METRICS_FILE}" >&2
    exit 1
fi
METRICS=$(grep -v '^[[:space:]]*#' "${METRICS_FILE}" \
            | grep -v '^[[:space:]]*$' \
            | paste -sd, -)

run_one() {
    local tree="$1"   # baseline | lean
    local bench="$2"  # raytrace_level_set | raytrace_fog_volume
    local label="${tree}-${bench}"
    local build_dir
    if [[ "${tree}" == "baseline" ]]; then
        build_dir="${BASELINE_DIR}"
    else
        build_dir="${LEAN_DIR}"
    fi
    local exe="${build_dir}/nanovdb/nanovdb/examples/ex_${bench}/ex_${bench}"
    if [[ ! -x "${exe}" ]]; then
        echo "missing executable: ${exe}" >&2
        return 1
    fi
    local report="${OUT_DIR}/${label}.csv"
    echo ">>> profiling ${label}"
    # --nvtx-include matches the range pushed in common.h around the launch.
    "${NCU}" \
        --target-processes all \
        --metrics "${METRICS}" \
        --csv \
        --nvtx --nvtx-include "raytrace_kernel/" \
        --log-file "${report}" \
        "${exe}" --warmup "${WARMUP}" --iters "${ITERS}" \
                 --width "${WIDTH}" --height "${HEIGHT}" \
                 --out-prefix "${OUT_DIR}/${label}"
}

for bench in raytrace_level_set raytrace_fog_volume; do
    run_one baseline "${bench}"
    run_one lean     "${bench}"
done

echo
echo "=== summary ==="
python3 "${SCRIPT_DIR}/compare.py" --reports-dir "${OUT_DIR}"
