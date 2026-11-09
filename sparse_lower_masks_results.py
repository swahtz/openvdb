import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RED, GRN, INK = "#c62828", "#2e7d32", "#222222"

# case, op, dense ms, dense peak MiB, sparse ms, sparse peak MiB, output MiB
# RTX PRO 6000 Blackwell, CUDA 13.2. Medians of 10 runs (5 for the lattices, meshes and
# mcns10442, 30 for dragon and crawler). Peak is all device memory the build holds, output included.
ROWS = [
    ("shell64 ×64, batched",            "refine",  1.767,  1071.9, 1.221,   33.1,   31.39),
    ("corner64 ×64, batched",           "dilate",  8.099,  8450.0, 2.115,   80.4,   76.37),
    ("scattered ×4, batched",           "refine",  2.461,  3152.3, 0.352,   47.2,   45.09),
    ("shell512 ×4, batched",            "dilate",  0.345,    82.2, 0.323,   20.3,   16.87),
    ("block128 ×8, batched",            "dilate",  1.271,  1068.2, 0.590,   28.4,   27.76),
    ("lattice of 2,048 tiles",          "coarsen", 3.408,  4293.4, 0.387,  134.9,  132.34),
    ("lattice of 2,048 tiles",          "dilate",  404.5, 34346.9, 359.8, 1098.7, 1058.69),
    ("dragon (124k leaves)",            "coarsen", 2.280,   137.0, 2.102,    7.2,    6.86),
    ("crawler (760k leaves)",           "dilate",  4.032,   258.7, 3.661,  143.4,  125.21),
    ("T1-13632 (782k leaves)",          "refine",  13.40,  1086.5, 10.84,  721.2,  659.61),
    ("mcns10442 (19M leaves)",          "dilate",  117.5,  6157.6, 106.5, 3610.2, 3084.92),
    ("mesh, 11.9M triangles",           "MeshToGrid", 580.0, 3561.7, 579.7, 3561.7, 379.51),
]

labels = [f"{c}  ·  {op}" for c, op, *_ in ROWS]
y = np.arange(len(ROWS))[::-1]
h = 0.38

fig, (axm, axt) = plt.subplots(1, 2, figsize=(16.4, 8.6), dpi=200, sharey=True,
                               gridspec_kw={"width_ratios": [1.25, 1], "wspace": 0.05})
fig.subplots_adjust(left=0.2, right=0.985, top=0.85, bottom=0.12)

dm = np.array([r[3] for r in ROWS]); sm = np.array([r[5] for r in ROWS]); om = np.array([r[6] for r in ROWS])
axm.barh(y + h / 2, dm, h, color=RED, alpha=0.85, label="develop/nanovdb (dense rows)")
axm.barh(y - h / 2, sm, h, color=GRN, alpha=0.85, label="this PR (rows on first touch)")
axm.scatter(om, y, marker="|", s=420, color=INK, zorder=3, label="output grid size")
axm.set_xscale("log"); axm.set_xlim(2, 2e5)
axm.set_xlabel("peak device memory during the build, MiB (log scale)")
for yi, d, s in zip(y, dm, sm):
    axm.text(s * 1.15, yi - h / 2, f"{s:,.0f}" if s >= 10 else f"{s:.1f}", va="center", fontsize=7.5, color=GRN)
    axm.text(d * 1.15, yi + h / 2, f"{d:,.0f}  ({d / s:.0f}×)" if d / s >= 1.5 else f"{d:,.0f}", va="center", fontsize=7.5, color=RED)

dt = np.array([r[2] for r in ROWS]); st = np.array([r[4] for r in ROWS])
axt.barh(y + h / 2, dt, h, color=RED, alpha=0.85)
axt.barh(y - h / 2, st, h, color=GRN, alpha=0.85)
axt.set_xscale("log"); axt.set_xlim(0.1, 4e3)
axt.set_xlabel("build time, ms (log scale)")
for yi, d, s in zip(y, dt, st):
    r = d / s
    note = f"{r:.1f}× faster" if r >= 1.1 else ("same" if r > 0.9 else f"{1 / r:.1f}× slower")
    axt.text(max(d, s) * 1.25, yi, note, va="center", fontsize=7.5, color=GRN if r >= 1.1 else "#555")

axm.set_yticks(y); axm.set_yticklabels(labels, fontsize=8.5)
for a in (axm, axt):
    a.grid(axis="x", which="major", color="#ddd", lw=0.6); a.set_axisbelow(True)
    for s in ("top", "right"): a.spines[s].set_visible(False)
    a.tick_params(axis="x", labelsize=8)
axm.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=3, fontsize=8.5, frameon=False)

fig.text(0.5, 0.95, "TopologyBuilder lower masks: peak memory and build time, develop/nanovdb vs. this PR",
         ha="center", fontsize=14, fontweight="bold", color=INK)
fig.text(0.5, 0.915, "Outputs are byte-identical in every case. Small batches and many-tile grids fall to roughly their output size; "
         "large grids keep their speed with less scratch.", ha="center", fontsize=9, color="#444")
fig.text(0.2, 0.035, "RTX PRO 6000 Blackwell, CUDA 13.2. Medians of 5–30 runs. Peak is all device memory a build holds, output included. "
         "MeshToGrid's row bound (its box-triangle pair count) is too loose to help yet; its peak is its own pair buffers.",
         fontsize=7.5, color="#555")

fig.savefig("sparse_lower_masks_results.png", dpi=200)
