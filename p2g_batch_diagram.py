import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

RED, REDF = "#c62828", "#fdecea"
GRN, GRNF = "#2e7d32", "#e8f5e9"
BLU, BLUF = "#3f51b5", "#e8eaf6"
G0, G0F = "#1b7f3b", "#c8ead0"   # grid 0
G1, G1F = "#777777", "#eeeeee"   # grid 1 (empty)
G2, G2F = "#b03a2e", "#f6d5d0"   # grid 2
GREY, GREYF = "#9e9e9e", "#f2f2f2"
INK = "#222222"

fig = plt.figure(figsize=(16.4, 12.8), dpi=200)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 164); ax.set_ylim(0, 128); ax.axis("off")

def box(x, y, w, h, text="", fc="white", ec=INK, lw=1.0, fs=8, bold=False, color=INK, r=0.6, ha="center", va="center", style="round"):
    p = FancyBboxPatch((x, y), w, h, boxstyle=f"{style},pad=0,rounding_size={r}" if style=="round" else "square,pad=0",
                       fc=fc, ec=ec, lw=lw)
    ax.add_patch(p)
    if text:
        ax.text(x + (w/2 if ha=="center" else 0.8), y + h/2, text, ha=ha, va=va, fontsize=fs,
                fontweight="bold" if bold else "normal", color=color)
    return p

def txt(x, y, s, fs=8, bold=False, color=INK, ha="left", va="center", style="normal"):
    ax.text(x, y, s, fontsize=fs, fontweight="bold" if bold else "normal", color=color, ha=ha, va=va, fontstyle=style)

def arrow(x0, y0, x1, y1, color=INK, lw=1.0, style="-|>", ls="-"):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle=style, mutation_scale=9, color=color, lw=lw, linestyle=ls))

def panel(x, y, w, h, color):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=2", fc="white", ec=color, lw=2.0))

def timeline(x, y, w, host_items, stream_items):
    txt(x - 1, y + 4, "host", fs=8, ha="right", color="#555")
    txt(x - 1, y, "stream", fs=8, ha="right", color="#555")
    ax.plot([x, x + w], [y + 4, y + 4], color="#bbb", lw=1)
    ax.plot([x, x + w], [y, y], color="#bbb", lw=1)
    for (a, b, s, fc, ec, col) in host_items:
        box(x + a, y + 2.7, b - a, 2.6, s, fc=fc, ec=ec, fs=7, color=col, bold=True)
    for (a, b, s, fc, ec, col) in stream_items:
        box(x + a, y - 1.3, b - a, 2.6, s, fc=fc, ec=ec, fs=7, color=col)

# ---------------------------------------------------------------- title
txt(82, 125.6, "voxelsToGrid — one grid per build (#2391) vs. this PR's B grids per build", fs=15, bold=True, ha="center")
txt(82, 123.1, "Toy example: three grids from one voxel list. g0 has voxels a, b in root tile A and c, d in tile B, g1 is empty, "
    "and g2 has voxels e–g in tile A, the same region as g0's tile A. Voxels that share a tile here also share a leaf.", fs=8.5, ha="center", color="#444")

# ================================================================ #2391 panel
PX, PY, PW, PH = 2.5, 79, 159, 42
panel(PX, PY, PW, PH, RED)
txt(PX + 3, PY + PH - 3.2, "#2391 — one grid per build; the caller loops over grids and merges the handles", fs=13, bold=True, color=RED)
txt(PX + 3, PY + PH - 6.4, "PointsToGridData describes one grid. Batching means B voxelsToGrid calls (the voxel-set overload, or fVDB's from_ijk) "
    "plus nanovdb::cuda::mergeGridHandles.", fs=8.5, color="#444")

rows = [("g0  (a–d, 2 tiles)", G0, G0F, False), ("g1  (empty)", G1, G1F, True), ("g2  (e–g, 1 tile)", G2, G2F, False)]
y0 = PY + PH - 12.5
for i, (name, c, cf, empty) in enumerate(rows):
    y = y0 - i * 5.2
    box(PX + 3, y - 1.6, 13, 3.2, name, fc=cf, ec=c, fs=7.8, bold=True, color=c)
    if empty:
        steps = [("no voxels:\nskip the sorts", GREYF, GREY, INK, 40.8)]
    else:
        steps = [("sort keys\noffset, then tile", GREYF, GREY, INK, 13), ("rank tiles,\nreduce counts", GREYF, GREY, INK, 12),
                 ("count D2H\n+ sync", REDF, RED, RED, 8.4)]
    steps += [("getBuffer\nown allocation", GREYF, GREY, INK, 12), ("build kernels\nheader <<<1,1>>>", GREYF, GREY, INK, 13),
              ("sync", REDF, RED, RED, 5), ("GridHandle ctor\n(2 header readbacks)", REDF, RED, RED, 14)]
    x = PX + 18
    for k, (s, fc, ec, col, w) in enumerate(steps):
        box(x, y - 1.6, w, 3.2, s, fc=fc, ec=ec, fs=6.8, color=col)
        x += w + 1.2
        if k < len(steps) - 1:
            arrow(x - 1.2, y, x, y, color="#888", lw=0.8)
    txt(x + 0.5, y, "→ handle[%d]" % i, fs=7.5, color=c, bold=True)

my = y0 - 3 * 5.2 - 0.2
box(PX + 18, my - 1.6, 80, 3.2, "mergeGridHandles(handle[0..2]):  one allocation, per grid a memcpy, then rewriteGridCounts + cudaStreamSynchronize", fc=REDF, ec=RED, fs=6.9, color=RED)
arrow(PX + 98.2, my, PX + 100.5, my, color="#888", lw=0.8)
box(PX + 101, my - 1.6, 26, 3.2, "one buffer: grid0 | grid1 | grid2", fc=GREYF, ec=GREY, fs=7.2)

ty = PY + 4.2
timeline(PX + 18, ty, 107,
         host_items=[(9, 14, "wait", REDF, RED, RED), (24, 28.5, "sync", REDF, RED, RED), (28.5, 36, "ctor ×2", REDF, RED, RED),
                     (42, 46.5, "sync", REDF, RED, RED), (46.5, 54, "ctor ×2", REDF, RED, RED),
                     (63, 68, "wait", REDF, RED, RED), (78, 82.5, "sync", REDF, RED, RED), (82.5, 90, "ctor ×2", REDF, RED, RED),
                     (101, 107, "merge", REDF, RED, RED)],
         stream_items=[(0, 9, "g0 sort+count", BLUF, BLU, INK), (14, 24, "g0 build", BLUF, BLU, INK),
                       (36, 42, "g1 headers", BLUF, BLU, INK),
                       (54, 63, "g2 sort+count", BLUF, BLU, INK), (68, 78, "g2 build", BLUF, BLU, INK),
                       (90, 101, "merge copies", BLUF, BLU, INK)])
txt(PX + 18 + 53, ty - 3.4, "the stream idles at every host round trip, and the pattern repeats for every grid", fs=7.5, color=RED, ha="center")
txt(PX + 3, ty + 2, "every batch:", fs=8.5, bold=True)

cx, cy, cw, ch = PX + 128.5, PY + 8, 29, 25
box(cx, cy, cw, ch, "", fc=REDF, ec=RED, lw=1.4, r=1.2)
txt(cx + 1.5, cy + ch - 2.5, "Cost per batch of B grids", fs=10, bold=True, color=RED)
txt(cx + 1.5, cy + ch - 6.0, "• B × (count readback + final sync)", fs=7.8)
txt(cx + 1.5, cy + ch - 8.8, "• B × 2 header readbacks in the\n   GridHandle ctors", fs=7.8, va="top")
txt(cx + 1.5, cy + ch - 13.6, "• B + 1 allocations, every grid\n   copied once more by the merge", fs=7.8, va="top")
txt(cx + 1.5, cy + ch - 18.6, "B = 64 grids of 848 voxels:\n13.2 ms on an RTX PRO 6000", fs=7.6, color="#444", va="top")

# ================================================================ THIS PR panel
PX2, PY2, PW2, PH2 = 2.5, 2, 159, 75
panel(PX2, PY2, PW2, PH2, GRN)
txt(PX2 + 3, PY2 + PH2 - 3.2, "THIS PR — one VoxelsToGridBatch, B grids: grid as the primary sort key, per-grid views, one buffer", fs=13, bold=True, color=GRN)
txt(PX2 + 3, PY2 + PH2 - 6.4, "One voxel list and B+1 device offsets. Every scratch array spans the batch, sorted so each grid's voxels and nodes are contiguous. "
    "Each grid's PointsToGridData", fs=8.5, color="#444")
txt(PX2 + 3, PY2 + PH2 - 8.8, "views its own ranges, so the single-grid build functors run unchanged. With B = 1 there is no grid sort and no rebasing.", fs=8.5, color="#444")

LX = PX2 + 3            # row labels
tx = PX2 + 29           # content column
cw_ = 6.5               # cell pitch

def cells(y, items, label=None, sub=None):
    if label: txt(LX, y, label, fs=8.5, bold=True)
    for i, (s, c, cf) in enumerate(items):
        box(tx + i * cw_, y - 1.6, cw_ - 0.9, 3.2, s, fc=cf, ec=c, fs=7.8, bold=True, color=c)
    if sub:
        for i, s in enumerate(sub):
            txt(tx + i * cw_ + (cw_ - 0.9) / 2, y - 3.2, s, fs=7.0, ha="center", color="#555")

A0, B0, A2 = (G0, G0F), (G0, G0F), (G2, G2F)
# input row
iy = PY2 + PH2 - 14.5
cells(iy, [("a", *A0), ("b", *A0), ("c", *B0), ("d", *B0), ("e", *A2), ("f", *A2), ("g", *A2)], "input voxels + offsets:")
txt(tx - 2.2, iy - 3.2, "d_offsets:", fs=7.5, ha="right", color="#555")
for i, s in [(0, "0"), (4, "4  4"), (7, "7")]:
    txt(tx + i * cw_ - 0.45, iy - 3.2, s, fs=7.5, ha="center", color="#555")
txt(tx + 47, iy, "g1 is the empty range [4, 4); any order within a grid", fs=7.5, color=G1, style="italic")

# sorts row
sy = iy - 7.5
txt(LX, sy, "three stable radix sorts:", fs=8.5, bold=True)
box(tx, sy - 1.6, 27, 3.2, "1. in-tile offset (36 bits)", fc=GREYF, ec=GREY, fs=7.5)
arrow(tx + 27, sy, tx + 29.5, sy, color="#888", lw=0.8)
box(tx + 30, sy - 1.6, 27, 3.2, "2. tile key (63 bits)", fc=GREYF, ec=GREY, fs=7.5)
arrow(tx + 57, sy, tx + 59.5, sy, color="#888", lw=0.8)
box(tx + 60, sy - 1.6, 36, 3.2, "3. grid  (⌈log₂ B⌉ bits, one pass for B ≤ 256)", fc=GRNF, ec=GRN, fs=7.5, bold=True, color=GRN)
txt(tx + 98, sy, "→ order (grid, tile, voxel): the first two are #2391's sorts", fs=7.5, color="#555", style="italic")

# tile ranks row
ry = sy - 7.5
cells(ry, [("a", *A0), ("b", *A0), ("c", *B0), ("d", *B0), ("e", *A2), ("f", *A2), ("g", *A2)], "batch-wide tile ranks:",
      sub=["0", "0", "1", "1", "2", "2", "2"])
txt(tx - 0.8, ry - 3.2, "tile rank:", fs=7.5, ha="right", color="#555")
box(tx + 47, ry - 3.6, 52, 6.4, "", fc="white", ec=GRN, lw=1.0, r=0.8)
txt(tx + 48.2, ry + 1.4, "a grid's first voxel always starts a tile, so e gets rank 2", fs=7.3, color=GRN, bold=True)
txt(tx + 48.2, ry - 0.8, "although its tile A is g0's tile A. Voxel keys (rank << 36 | offset)", fs=7.2)
txt(tx + 48.2, ry - 2.6, "then differ across grids, and the RLE and unique passes never merge two.", fs=7.2)

# counts row
ky = ry - 8.5
txt(LX, ky, "count (batch-wide):", fs=8.5, bold=True)
box(tx, ky - 1.6, 40, 3.2, "DeviceSegmentedReduce(NodeStarts) over d_offsets", fc=GREYF, ec=GREY, fs=7.4)
arrow(tx + 40, ky, tx + 42.5, ky, color="#888", lw=0.8)
box(tx + 43, ky - 1.6, 32, 3.2, "ONE readback: counts[B] + offsets", fc=GRNF, ec=GRN, fs=7.4, bold=True, color=GRN)
arrow(tx + 75, ky, tx + 77.5, ky, color="#888", lw=0.8)
box(tx + 78, ky - 1.6, 37, 3.2, "host: level starts, layout, ONE allocation", fc=GRNF, ec=GRN, fs=7.4, color=GRN)
txt(tx + 59, ky - 3.4, "(#2391: one readback per grid)", fs=7.3, color="#555", style="italic", ha="center")

# per-grid views
dy = ky - 9.5
txt(LX, dy + 1.2, "PointsToGridData[g]:", fs=8.5, bold=True)
datas = [("g0", G0, G0F, "voxels [0, 4), tiles [0, 2)\nd_keys = keys + 0, d_tile_keys = tileKeys + 0\nnodeCount = {2, 2, 2}   mGridIndex 0 / 3"),
         ("g1", G1, G1F, "voxels [4, 4), tiles [2, 2)\nviews of length 0\nnodeCount = {0, 0, 0}   mGridIndex 1 / 3"),
         ("g2", G2, G2F, "voxels [4, 7), tiles [2, 3)\nd_keys = keys + 4, ranks rebased: 2 → 0\nnodeCount = {1, 1, 1}   mGridIndex 2 / 3")]
for i, (n, c, cf, s) in enumerate(datas):
    x = tx + i * 30
    box(x, dy - 3.6, 29, 7.2, "", fc=cf, ec=c, r=0.8)
    txt(x + 1, dy + 2.2, n, fs=8.5, bold=True, color=c)
    txt(x + 4.2, dy, s, fs=6.5, va="center")

rx, rY = tx + 91.5, dy + 0.5
box(rx, rY - 6.5, 36.5, 12.5, "", fc="white", ec=GRN, lw=1.2, r=1.0)
txt(rx + 1.5, rY + 4.2, "build: existing functors, one launch each", fs=8.5, bold=True, color=GRN)
txt(rx + 1.5, rY + 1.7, "PerGridFunctor<F>, for item t of a level:", fs=7.0)
txt(rx + 1.5, rY - 0.4, "   g = gridOf(starts.level, t)   (binary search)", fs=7.0)
txt(rx + 1.5, rY - 2.5, "   F()(t − starts.level[g], &data[g])", fs=7.0)
txt(rx + 1.5, rY - 4.6, "batched only: header (B threads), leaf prefix sum", fs=7.0)

# output buffer
oy = dy - 10
txt(LX, oy, "output (one allocation):", fs=8.5, bold=True)
segs = [("grid 0:  Grid Tree Root Upper[2] Lower[2] Leaf[2]", G0, G0F, 36), ("grid 1:  Grid Tree Root(0), valid empty grid", G1, G1F, 26),
        ("grid 2:  Grid Tree Root Upper Lower Leaf", G2, G2F, 30)]
x = tx
for s, c, cf, w in segs:
    box(x, oy - 1.6, w, 3.2, s, fc=cf, ec=c, fs=6.9, color=c, style="square", r=0)
    x += w
txt(tx, oy - 3.4, "laid out as mergeGridHandles would; GridHandle::adopt takes the layout from the host, so no header is read back", fs=7.2, color="#555")

# timeline
ty2 = PY2 + 5.2
timeline(tx, ty2, 92,
         host_items=[(30, 36, "wait", GRNF, GRN, GRN), (80, 86, "sync", GRNF, GRN, GRN), (86, 92, "adopt", GRNF, GRN, GRN)],
         stream_items=[(0, 30, "sort + count, all grids", BLUF, BLU, INK),
                       (36, 80, "build, all grids: headers(B) · upper · lower · leaf · voxels · bbox", BLUF, BLU, INK)])
txt(tx + 46, ty2 - 3.4, "two host waits per batch, however many grids; the number of launches does not depend on B", fs=7.5, color=GRN, ha="center")
txt(LX, ty2 + 2, "every batch:", fs=8.5, bold=True)

cx, cy, cw, ch = tx + 95, oy - 10, 32.5, 13.5
box(cx, cy, cw, ch, "", fc=GRNF, ec=GRN, lw=1.4, r=1.2)
txt(cx + 1.5, cy + ch - 2.5, "Cost per batch of B grids", fs=10, bold=True, color=GRN)
txt(cx + 1.5, cy + ch - 5.3, "• 1 count readback, 1 final sync", fs=7.4)
txt(cx + 1.5, cy + ch - 7.5, "• 1 allocation, no merge, no header readbacks", fs=7.4)
txt(cx + 1.5, cy + ch - 9.7, "• output byte-identical to build + merge", fs=7.4)
txt(cx + 1.5, cy + ch - 12.0, "B = 64 grids of 848 voxels: 0.48 ms", fs=7.4, color="#444")

import os
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voxels_to_grid_batch_diagram.png")
fig.savefig(out, dpi=200, facecolor="white")
print(out)
