import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

RED, REDF = "#c62828", "#fdecea"
GRN, GRNF = "#2e7d32", "#e8f5e9"
BLU, BLUF = "#3f51b5", "#e8eaf6"
ORG = "#e65100"
G0, G0F = "#1b7f3b", "#c8ead0"   # grid 0
G1, G1F = "#777777", "#eeeeee"   # grid 1 (empty)
G2, G2F = "#b03a2e", "#f6d5d0"   # grid 2
GREY, GREYF = "#9e9e9e", "#f2f2f2"
INK = "#222222"

fig = plt.figure(figsize=(16.4, 11.4), dpi=200)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 164); ax.set_ylim(0, 114); ax.axis("off")

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

def arrow(x0, y0, x1, y1, color=INK, lw=1.0, style="-|>", ls="-", shrinkA=0, shrinkB=0):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle=style, mutation_scale=9, color=color, lw=lw,
                                 linestyle=ls, shrinkA=shrinkA, shrinkB=shrinkB))

def panel(x, y, w, h, color):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=2", fc="white", ec=color, lw=2.0))

def timeline(x, y, w, label_host, label_stream, host_items, stream_items):
    # rows: host at y+4, stream at y
    txt(x - 1, y + 4, "host", fs=8, ha="right", color="#555")
    txt(x - 1, y, "stream", fs=8, ha="right", color="#555")
    ax.plot([x, x + w], [y + 4, y + 4], color="#bbb", lw=1)
    ax.plot([x, x + w], [y, y], color="#bbb", lw=1)
    for (a, b, s, fc, ec, col) in host_items:
        box(x + a, y + 2.7, b - a, 2.6, s, fc=fc, ec=ec, fs=7, color=col, bold=True)
    for (a, b, s, fc, ec, col) in stream_items:
        box(x + a, y - 1.3, b - a, 2.6, s, fc=fc, ec=ec, fs=7, color=col)

# ---------------------------------------------------------------- title
txt(82, 111.6, "TopologyBuilder — master's one grid per build vs. this PR's B grids per build", fs=15, bold=True, ha="center")
txt(82, 109.1, "Toy example (the new unit test): refine three grids at once. g0 has 2 speculative root tiles, g1 is empty, g2 (voxels across the ±4096 boundaries) has 5. "
    "A root tile is a 4096³ region; each tile owns a Mask<5> (4 KiB) and a densified row of 32768 Mask<4> (16 MiB).", fs=8.5, ha="center", color="#444")

# ================================================================ MASTER panel
PX, PY, PW, PH = 2.5, 66, 159, 41
panel(PX, PY, PW, PH, RED)
txt(PX + 3, PY + PH - 3.2, "MASTER — one grid per builder; the caller loops over grids and merges the handles", fs=13, bold=True, color=RED)
txt(PX + 3, PY + PH - 6.4, "TopologyBuilderData holds one buffer pointer and one nodeCount[3]; BuildGridTreeRootFunctor hardcodes mGridIndex = 0, mGridCount = 1. "
    "Batching means B independent builds plus nanovdb::cuda::mergeGridHandles.", fs=8.5, color="#444")

# three per-grid pipelines, stacked
rows = [("g0  (2 tiles)", G0, G0F, "masks: 2 × (4 KiB + 16 MiB)", "Data: nodeCount, d_bufferPtr"),
        ("g1  (empty)",   G1, G1F, "masks: none",                 "Data: zeros"),
        ("g2  (5 tiles)", G2, G2F, "masks: 5 × (4 KiB + 16 MiB)", "Data: nodeCount, d_bufferPtr")]
y0 = PY + PH - 12.5
for i, (name, c, cf, m, d) in enumerate(rows):
    y = y0 - i * 5.2
    box(PX + 3, y - 1.6, 12, 3.2, name, fc=cf, ec=c, fs=8, bold=True, color=c)
    steps = [("host root\n(D2H root+uppers)", REDF, RED, RED), (m, GREYF, GREY, INK), ("countNodes\n3 D2H totals", GREYF, GREY, INK),
             ("sync", REDF, RED, RED), ("getBuffer\nown allocation", GREYF, GREY, INK), ("build kernels\n<<<1,1>>> header", GREYF, GREY, INK),
             ("sync", REDF, RED, RED), ("GridHandle ctor\n(2 syncs)", REDF, RED, RED)]
    x = PX + 17
    widths = [12, 14, 12, 5, 12, 13, 5, 12]
    for (s, fc, ec, col), w in zip(steps, widths):
        box(x, y - 1.6, w, 3.2, s, fc=fc, ec=ec, fs=6.8, color=col)
        x += w + 1.2
        if s != "GridHandle ctor\n(2 syncs)":
            arrow(x - 1.2, y, x, y, color="#888", lw=0.8)
    txt(x + 0.5, y, "→ handle[%d]" % i, fs=7.5, color=c, bold=True)

# merge
my = y0 - 3 * 5.2 - 0.2
box(PX + 17, my - 1.6, 80, 3.2, "mergeGridHandles(handle[0..2]):  per grid  memcpy + updateGridCount<<<1,1>>> + D2H dirty flag + cudaStreamSynchronize", fc=REDF, ec=RED, fs=6.9, color=RED)
arrow(PX + 97.2, my, PX + 99.5, my, color="#888", lw=0.8)
box(PX + 100, my - 1.6, 26, 3.2, "one buffer: grid0 | grid1 | grid2", fc=GREYF, ec=GREY, fs=7.2)

# timeline master
ty = PY + 4.2
timeline(PX + 17, ty, 105, "host", "stream",
         host_items=[(0, 8, "root D2H", REDF, RED, RED), (15.1, 19.6, "sync", REDF, RED, RED), (27.6, 32, "sync", REDF, RED, RED),
                     (36.5, 44.5, "root D2H", REDF, RED, RED), (51.6, 56.1, "sync", REDF, RED, RED), (64.1, 68.5, "sync", REDF, RED, RED),
                     (73, 81, "root D2H", REDF, RED, RED), (88.1, 92.5, "sync", REDF, RED, RED), (100.6, 105, "sync", REDF, RED, RED)],
         stream_items=[(8, 15.1, "g0 count", BLUF, BLU, INK), (19.6, 27.6, "g0 build", BLUF, BLU, INK),
                       (44.5, 51.6, "g1 count", BLUF, BLU, INK), (56.1, 64.1, "g1 build", BLUF, BLU, INK),
                       (81, 88.1, "g2 count", BLUF, BLU, INK), (92.5, 100.6, "g2 build", BLUF, BLU, INK)])
txt(PX + 17 + 52, ty - 3.4, "stream idles at every host round trip; the pattern repeats per grid, then once more per grid inside mergeGridHandles", fs=7.5, color=RED, ha="center")
txt(PX + 3, ty + 2, "every batch:", fs=8.5, bold=True)

# cost callout master
cx, cy, cw, ch = PX + 128, PY + 8, 29.5, 26
box(cx, cy, cw, ch, "", fc=REDF, ec=RED, lw=1.4, r=1.2)
txt(cx + 1.5, cy + ch - 2.5, "Cost per batch of B grids", fs=10, bold=True, color=RED)
txt(cx + 1.5, cy + ch - 6.5,  "• B × (root readback + count sync\n      + final sync)", fs=7.8, va="top")
txt(cx + 1.5, cy + ch - 11.5,  "• B × 2 syncs in GridHandle ctors", fs=7.8)
txt(cx + 1.5, cy + ch - 14.3, "• B × 1 sync inside mergeGridHandles", fs=7.8)
txt(cx + 1.5, cy + ch - 17.1, "• B allocations, then one copy", fs=7.8)
txt(cx + 1.5, cy + ch - 20.3, "fVDB measured ~1.5–2 ms fixed cost\nper member, linear in B\n(openvdb/fvdb-core#755).", fs=7.6, color="#444", va="top")

# ================================================================ THIS PR panel
PX2, PY2, PW2, PH2 = 2.5, 2, 159, 62
panel(PX2, PY2, PW2, PH2, GRN)
txt(PX2 + 3, PY2 + PH2 - 3.2, "THIS PR — one builder, B grids: batch-wide scratch, per-grid bases, one buffer", fs=13, bold=True, color=GRN)
txt(PX2 + 3, PY2 + PH2 - 6.4, "Every scratch array is indexed by processed tile (or node) across the batch. Each grid's TopologyBuilderData records where its tiles and nodes start", fs=8.5, color="#444")
txt(PX2 + 3, PY2 + PH2 - 8.8, "(tileBase, upperBase, lowerBase, leafBase); the build kernels subtract those bases. With B = 1 every base is 0 and the flow is exactly the old one.", fs=8.5, color="#444")

# processed tiles row
ry = PY2 + PH2 - 14.5
txt(PX2 + 3, ry, "processed tiles (T = 7):", fs=8.5, bold=True)
tiles = [("t0", G0, G0F, 0), ("t1", G0, G0F, 0), ("t2", G2, G2F, 2), ("t3", G2, G2F, 2), ("t4", G2, G2F, 2), ("t5", G2, G2F, 2), ("t6", G2, G2F, 2)]
tx = PX2 + 26
for i, (t, c, cf, g) in enumerate(tiles):
    box(tx + i * 6.5, ry - 1.6, 5.6, 3.2, t, fc=cf, ec=c, fs=8, bold=True, color=c)
    txt(tx + i * 6.5 + 2.8, ry - 3.2, str(g), fs=7.5, ha="center", color="#555")
txt(tx - 0.8, ry - 3.2, "tileToGrid:", fs=7.5, ha="right", color="#555")
txt(tx + 47, ry - 3.2, "g1 owns no tiles (tileCount = 0, tileBase = 2)", fs=7.5, color=G1, style="italic")

# masks
txt(tx + 72, ry + 0.9, "masks: ONE allocation, T Mask<5> + T × 32768 Mask<4>", fs=8, bold=True)
txt(tx + 72, ry - 1.4, "the consumer's mask-fill functor is unchanged; per grid it gets\nroot(g),  upperMasks + tileBase[g],  lowerMasks + tileBase[g]", fs=7.3, color="#444")

# scans row
sy = ry - 8
txt(PX2 + 3, sy, "countNodes (batch-wide):", fs=8.5, bold=True)
box(tx, sy - 1.6, 30, 3.2, "EnumerateNodes over T tiles → 3 inclusive scans", fc=GREYF, ec=GREY, fs=7.5)
arrow(tx + 30, sy, tx + 32.5, sy, color="#888", lw=0.8)
box(tx + 33, sy - 1.6, 44, 3.2, "GatherNodeCounts (B threads): counts and bases at each grid's tile boundaries", fc=GRNF, ec=GRN, fs=7.3, color=GRN)
arrow(tx + 77, sy, tx + 79.5, sy, color="#888", lw=0.8)
box(tx + 80, sy - 1.6, 19, 3.2, "ONE D2H of Data[B]", fc=GRNF, ec=GRN, fs=7.5, bold=True, color=GRN)
txt(tx + 100.5, sy, "(master: three scalar D2H copies)", fs=7.5, color="#555", style="italic")

# Data[B] boxes
dy = sy - 9.2
txt(PX2 + 3, dy + 1.2, "Data[B] after gather:", fs=8.5, bold=True)
datas = [("g0", G0, G0F, "tileBase 0, tileCount 2\nupperBase 0  lowerBase 0  leafBase 0\nnodeCount = {4, 3, 2}   mGridIndex 0 / 3"),
         ("g1", G1, G1F, "tileBase 2, tileCount 0\nbases = g2's (empty grids share the next base)\nnodeCount = {0, 0, 0}   mGridIndex 1 / 3"),
         ("g2", G2, G2F, "tileBase 2, tileCount 5\nupperBase = upperOff[2], lowerBase = lowerOff[2·32768]\nnodeCount = {…}   mGridIndex 2 / 3")]
for i, (n, c, cf, s) in enumerate(datas):
    x = tx + i * 31
    box(x, dy - 3.6, 30, 7.2, "", fc=cf, ec=c, r=0.8)
    txt(x + 1, dy + 2.2, n, fs=8.5, bold=True, color=c)
    txt(x + 4.2, dy, s, fs=6.5, va="center")

# output buffer + rebasing
oy = dy - 10.5
txt(PX2 + 3, oy, "output (one allocation):", fs=8.5, bold=True)
segs = [("grid 0:  Grid Tree Root Upper[2] Lower[3] Leaf[4]", G0, G0F, 38), ("grid 1:  Grid Tree Root(0), valid empty grid", G1, G1F, 27), ("grid 2:  Grid Tree Root Upper Lower Leaf", G2, G2F, 31)]
x = tx
for s, c, cf, w in segs:
    box(x, oy - 1.6, w, 3.2, s, fc=cf, ec=c, fs=6.9, color=c, style="square", r=0)
    x += w
txt(tx, oy - 3.4, "d_bufferPtr[g] = base + Σ size[<g];  mGridSize per grid;  the GridHandle ctor validates the chain once", fs=7.2, color="#555")

# rebasing example
rx, rY = tx + 95, dy + 0.5
box(rx, rY - 6.5, 36.5, 12.5, "", fc="white", ec=GRN, lw=1.2, r=1.0)
txt(rx + 1.5, rY + 4.2, "rebasing, in every build kernel thread", fs=8.5, bold=True, color=GRN)
txt(rx + 1.5, rY + 1.7, "tile-indexed (upper, lower):  g = tileToGrid[t]", fs=7.0)
txt(rx + 1.5, rY - 0.4, "node-indexed (leaf, bbox):  g = gridOfIndex(leafBase, t)", fs=7.0)
txt(rx + 1.5, rY - 2.5, "local = t − leafBase[g], into grid g's own node array", fs=7.0)
txt(rx + 1.5, rY - 4.6, "mOffset = vox[t] − vox[leafBase[g]] + 1  (restarts per grid)", fs=7.0)

# timeline PR
ty2 = PY2 + 5.2
timeline(tx, ty2, 96, "host", "stream",
         host_items=[(0, 13, "speculative roots", GRNF, GRN, GRN), (27, 32, "sync", GRNF, GRN, GRN), (80, 85, "sync", GRNF, GRN, GRN), (85, 96, "GridHandle ctor", GRNF, GRN, GRN)],
         stream_items=[(13, 27, "count, all grids", BLUF, BLU, INK), (32, 80, "build kernels, all grids: header(B) · upper(T) · lower(T) · leaf · bbox · post", BLUF, BLU, INK)])
txt(tx + 48, ty2 - 3.4, "the caller stages the roots, then one count sync and one final sync for the whole batch; launch count is independent of B", fs=7.5, color=GRN, ha="center")
txt(PX2 + 3, ty2 + 2, "every batch:", fs=8.5, bold=True)

# cost callout PR
cx, cy, cw, ch = tx + 99, oy - 6.8, 32.5, 11
box(cx, cy, cw, ch, "", fc=GRNF, ec=GRN, lw=1.4, r=1.2)
txt(cx + 1.5, cy + ch - 2.5, "Cost per batch of B grids", fs=10, bold=True, color=GRN)
txt(cx + 1.5, cy + ch - 5.4, "• roots staged by the caller, 1 count sync, 1 final sync", fs=7.4)
txt(cx + 1.5, cy + ch - 7.7, "• 1 allocation, 1 GridHandle, no merge", fs=7.4)
txt(cx + 1.5, cy + ch - 10.0, "• B = 1: byte-identical output, consumers unchanged", fs=7.4)

out = "/tmp/claude-1000/-home-jswartz-Development-fvdb-core/eb17c6bb-d3e3-4223-a060-08f6db45b115/scratchpad/topology_builder_batch_diagram.png"
fig.savefig(out, dpi=200, facecolor="white")
print(out)
