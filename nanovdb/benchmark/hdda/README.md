# Lean-HDDA register-pressure A/B benchmark

This directory contains a small ncu-driven harness that quantifies the win
from dropping `mT1`, `mDelta` and `mStep` from `nanovdb::math::HDDA` / `DDA`.
The pitch is straightforward: the previous HDDA carried roughly 16 fp32-equivalent
registers per thread; the lean version carries about 8, with the rest derived
on demand from the live `Ray`. On register-bound raytrace kernels the missing
7-or-so registers should translate directly into higher occupancy.

The harness drives the two existing CUDA examples that exercise the HDDA path:

- `ex_raytrace_level_set` (zero-crossing search via `nanovdb::math::ZeroCrossing`)
- `ex_raytrace_fog_volume` (the new `runNanoVDBHdda` kernel, which uses
  `TreeMarcher` to leap empty space)

## 1. Build two trees

Configure the baseline (pre-refactor) and lean trees side-by-side. Both must
have `NANOVDB_BUILD_EXAMPLES`, `NANOVDB_USE_CUDA`, `NANOVDB_CUDA_PTXAS_VERBOSE`
and `NANOVDB_USE_NVTX` on:

```bash
# from the OpenVDB repo root
cmake -S . -B build-baseline -DUSE_NANOVDB=ON -DNANOVDB_BUILD_EXAMPLES=ON \
      -DNANOVDB_USE_CUDA=ON -DNANOVDB_CUDA_PTXAS_VERBOSE=ON \
      -DNANOVDB_USE_NVTX=ON
cmake --build build-baseline -j

# check out the lean refactor, then:
cmake -S . -B build-lean -DUSE_NANOVDB=ON -DNANOVDB_BUILD_EXAMPLES=ON \
      -DNANOVDB_USE_CUDA=ON -DNANOVDB_CUDA_PTXAS_VERBOSE=ON \
      -DNANOVDB_USE_NVTX=ON
cmake --build build-lean -j
```

During the lean build, look for `ptxas info : Used N registers` lines in the
compile log - the delta vs. the baseline build is the headline number for the
refactor and you'll see it without ever running the binary.

## 2. Run the harness

```bash
cd nanovdb/benchmark/hdda
BASELINE_DIR=../../../build-baseline \
LEAN_DIR=../../../build-lean \
./profile_hdda.sh
```

Tuneable env vars (defaults shown):

- `BASELINE_DIR=build-baseline` - path to the baseline build tree
- `LEAN_DIR=build-lean` - path to the lean build tree
- `OUT_DIR=./reports` - where ncu CSVs and PFMs are written
- `WARMUP=5` / `ITERS=20` - per-example warmup and measured iterations
- `WIDTH=1024` / `HEIGHT=1024` - image resolution
- `NCU=ncu` - override if `ncu` isn't on `$PATH`

The script invokes `ncu --nvtx --nvtx-include "raytrace_kernel/"` so only the
kernel launch (the NVTX range pushed in `common.h`) is profiled. The metric
list is in `metrics.txt`; edit there if you want to widen or narrow it.

## 3. Read the summary

`profile_hdda.sh` calls `compare.py` at the end, which joins the baseline and
lean CSVs and prints a markdown table per benchmark:

```
## raytrace_level_set

| kernel | metric                              | baseline | lean    | delta   | delta % |
| ------ | ----------------------------------- | -------- | ------- | ------- | ------- |
| ...    | launch__registers_per_thread        | 48.000   | 40.000  | -8.000  | -16.7%  |
| ...    | sm__warps_active.avg.pct_of_peak... | 25.10    | 33.20   | +8.10   | +32.3%  |
| ...    | gpu__time_duration.sum              | 4.521    | 3.880   | -0.641  | -14.2%  |
```

The numbers above are illustrative. What we expect to see in practice:

- `launch__registers_per_thread` drops by ~6-8 on the level-set kernel.
- `sm__warps_active.avg.pct_of_peak_sustained_active` rises by one occupancy
  tier on sm_75 (e.g. 25% to 33%).
- Median kernel time on `ex_raytrace_level_set` improves 5-15% at 1024x1024
  on a typical RTX-class GPU.

If the register count *doesn't* move, the kernel is not register-bound and
occupancy is gated by something else (shared memory, block size). That is
also a useful answer, and the table will say so directly.

## Files

- `profile_hdda.sh` - driver script, invokes ncu and then compare.py.
- `metrics.txt` - the ncu metrics list (one per line, `#` for comments).
- `compare.py` - joins baseline and lean CSVs into a markdown delta table.
