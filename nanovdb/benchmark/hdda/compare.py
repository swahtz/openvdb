#!/usr/bin/env python3
# Copyright Contributors to the OpenVDB Project
# SPDX-License-Identifier: Apache-2.0
"""Parse ncu CSV output for the lean-HDDA A/B and print a delta table.

Designed to consume the reports written by profile_hdda.sh, which produces
one CSV file per (tree, bench) pair: e.g. baseline-raytrace_level_set.csv
and lean-raytrace_level_set.csv. Joins rows on (kernel-name, metric-name).
"""

import argparse
import csv
import os
import sys
from collections import defaultdict


# Metrics we want surfaced in the summary table. Order = column order.
HEADLINE_METRICS = [
    "launch__registers_per_thread",
    "sm__warps_active.avg.pct_of_peak_sustained_active",
    "smsp__warp_issue_stalled_no_instruction.avg.pct_of_peak_sustained_active",
    "smsp__inst_executed.avg.per_cycle_active",
    "dram__bytes_read.sum",
    "dram__bytes_write.sum",
    "gpu__time_duration.sum",
]


def load_report(path):
    """Read an ncu CSV into {(kernel_name, metric): float_value}.

    ncu CSV layout varies slightly between releases; we look for the columns by
    name rather than position. Rows that lack a numeric value are skipped.
    """
    rows = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            kernel = (row.get("Kernel Name")
                      or row.get("kernel_name")
                      or row.get("Function Name"))
            metric = (row.get("Metric Name")
                      or row.get("metric_name"))
            value = (row.get("Metric Value")
                     or row.get("metric_value"))
            if not kernel or not metric or value in (None, ""):
                continue
            try:
                rows[(kernel.strip(), metric.strip())] = float(value)
            except ValueError:
                # Some metrics report units like "Kbyte/s" alongside the value;
                # strip them and retry.
                try:
                    rows[(kernel.strip(), metric.strip())] = float(value.split()[0])
                except (ValueError, IndexError):
                    continue
    return rows


def find_pairs(reports_dir):
    """Return a list of (bench_name, baseline_path, lean_path) tuples."""
    by_bench = defaultdict(dict)
    for name in os.listdir(reports_dir):
        if not name.endswith(".csv"):
            continue
        stem = name[:-4]
        for tree in ("baseline", "lean"):
            prefix = tree + "-"
            if stem.startswith(prefix):
                bench = stem[len(prefix):]
                by_bench[bench][tree] = os.path.join(reports_dir, name)
                break
    pairs = []
    for bench, paths in sorted(by_bench.items()):
        if "baseline" in paths and "lean" in paths:
            pairs.append((bench, paths["baseline"], paths["lean"]))
        else:
            print(f"skipping {bench}: missing baseline or lean report",
                  file=sys.stderr)
    return pairs


def render_table(bench, baseline, lean):
    kernels = sorted({k for (k, _m) in baseline.keys()} & {k for (k, _m) in lean.keys()})
    if not kernels:
        print(f"## {bench}\n\nNo kernels in common; nothing to compare.\n")
        return
    print(f"## {bench}\n")
    headers = ["kernel", "metric", "baseline", "lean", "delta", "delta %"]
    rows = [headers]
    for kernel in kernels:
        for metric in HEADLINE_METRICS:
            b = baseline.get((kernel, metric))
            l = lean.get((kernel, metric))
            if b is None or l is None:
                continue
            delta = l - b
            pct = (delta / b * 100.0) if b else float("nan")
            rows.append([
                kernel[:48],
                metric,
                f"{b:.3f}",
                f"{l:.3f}",
                f"{delta:+.3f}",
                f"{pct:+.1f}%",
            ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    for ix, row in enumerate(rows):
        line = "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |"
        print(line)
        if ix == 0:
            print(sep)
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reports-dir", required=True,
                    help="Directory containing baseline-*.csv and lean-*.csv")
    args = ap.parse_args()

    pairs = find_pairs(args.reports_dir)
    if not pairs:
        print("No baseline/lean report pairs found.", file=sys.stderr)
        sys.exit(1)
    for bench, b_path, l_path in pairs:
        baseline = load_report(b_path)
        lean = load_report(l_path)
        render_table(bench, baseline, lean)


if __name__ == "__main__":
    main()
