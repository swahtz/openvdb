import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

RED, REDF = "#c62828", "#fdecea"
GRN, GRNF = "#2e7d32", "#e8f5e9"
BLU, BLUF = "#3f51b5", "#e8eaf6"
ORG, ORGF = "#e65100", "#fff3e0"
PUR, PURF = "#6a1b9a", "#f3e5f5"
GREY, GREYF = "#9e9e9e", "#f2f2f2"
INK = "#222222"
# the three (tile, upper child) pairs the functors touch
PA, PAF = "#1565c0", "#d6e6f8"   # (t0, 5)
PB, PBF = "#ad1457", "#f8d7e5"   # (t0, 9)
PC, PCF = "#00838f", "#d2f0f2"   # (t1, 2)

fig = plt.figure(figsize=(16.4, 11.8), dpi=200)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 164); ax.set_ylim(0, 118); ax.axis("off")

def box(x, y, w, h, text="", fc="white", ec=INK, lw=1.0, fs=8, bold=False, color=INK, r=0.6, ha="center"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}", fc=fc, ec=ec, lw=lw))
    if text:
        ax.text(x + (w / 2 if ha == "center" else 0.8), y + h / 2, text, ha=ha, va="center", fontsize=fs,
                fontweight="bold" if bold else "normal", color=color)

def cell(x, y, w, h, text="", fc="white", ec="#999", fs=7, color=INK, bold=False):
    ax.add_patch(plt.Rectangle((x, y), w, h, fc=fc, ec=ec, lw=0.8))
    if text:
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs, color=color,
                fontweight="bold" if bold else "normal")

def txt(x, y, s, fs=8, bold=False, color=INK, ha="left", va="center", style="normal"):
    ax.text(x, y, s, fontsize=fs, fontweight="bold" if bold else "normal", color=color, ha=ha, va=va, fontstyle=style)

def arrow(x0, y0, x1, y1, color=INK, lw=1.0, ls="-", rad=0.0):
    ax.add_patch(FancyArrowPatch((x0, y0), (x1, y1), arrowstyle="-|>", mutation_scale=9, color=color, lw=lw,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}"))

def panel(x, y, w, h, color):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=2", fc="white", ec=color, lw=2.0))

# Slots drawn for one tile: 0..11, a gap, then 32767
SLOTS = list(range(12)) + ["…", 32767]
SW = 4.2

def strip(x, y, touched, labels=None, h=3.0, empty=""):
    """One tile's 32,768 upper-child slots, abbreviated. touched: {slot: (fc, ec, text)}"""
    xs = {}
    for i, s in enumerate(SLOTS):
        cx = x + i * SW
        if s == "…":
            txt(cx + SW / 2, y + h / 2, "…", fs=9, ha="center", color="#777")
            continue
        fc, ec, t, col = "white", "#aaa", empty, "#999"
        if s in touched:
            fc, ec, t = touched[s]; col = ec
        cell(cx, y, SW, h, t, fc=fc, ec=ec, fs=7, color=col, bold=s in touched)
        xs[s] = cx + SW / 2
        if labels:
            txt(cx + SW / 2, y - 1.1, str(s), fs=6, ha="center", color="#777")
    return xs

# ---------------------------------------------------------------- title
txt(82, 115.6, "TopologyBuilder lower masks: develop/nanovdb's dense rows vs. this PR's rows claimed on first touch", fs=15, bold=True, ha="center")
txt(82, 113.0, "Toy example: a build whose speculative root has two tiles, t0 and t1. The internal-node functors touch three (tile, upper child) pairs: "
    "(t0, 5), (t0, 9) and (t1, 2). A tile has 32,768 upper-node children; a Mask<4> is 512 B.", fs=8.5, ha="center", color="#444")

# ================================================================ DEVELOP panel
PX, PY, PW, PH = 2.5, 79, 159, 31.5
panel(PX, PY, PW, PH, RED)
txt(PX + 3, PY + PH - 3.2, "develop/nanovdb: one Mask<4> per upper-node child of every processed tile, touched or not", fs=13, bold=True, color=RED)
txt(PX + 3, PY + PH - 6.3, "allocateInternalMaskBuffers reserves and clears T × 32,768 Mask<4>; countNodes and processLowerNodes then walk all T × 32,768 slots.",
    fs=8.5, color="#444")

dense_touch0 = {5: (PAF, PA, "■"), 9: (PBF, PB, "■")}
dense_touch1 = {2: (PCF, PC, "■")}
txt(PX + 3, PY + 18.3, "lower masks, t0", fs=8.5, bold=True)
strip(PX + 22, PY + 16.8, dense_touch0, labels=True)
txt(PX + 3, PY + 12.3, "lower masks, t1", fs=8.5, bold=True)
strip(PX + 22, PY + 10.8, dense_touch1, labels=True)
txt(PX + 22 + 14 * SW + 1.5, PY + 18.3, "32,768 × 512 B = 16 MiB per tile,", fs=8, color=RED, bold=True)
txt(PX + 22 + 14 * SW + 1.5, PY + 15.9, "cleared with cudaMemsetAsync", fs=8, color=RED)
txt(PX + 22 + 14 * SW + 1.5, PY + 12.3, "3 of 65,536 slots hold anything", fs=8, color="#555", style="italic")

steps = [("countNodes: EnumerateNodes over\nT × 32,768 slots", GREYF, GREY),
         ("lowerCounts, leafCounts, lowerOffsets,\nleafOffsets: 4 × 128 KiB per tile", REDF, RED),
         ("two scans over T × 32,768", GREYF, GREY),
         ("processLowerNodes: grid (T, 256),\nevery warp walks 128 slots", GREYF, GREY)]
x = PX + 3
for i, (s, fc, ec) in enumerate(steps):
    w = [27, 31, 20, 30][i]
    box(x, PY + 2.2, w, 5.2, s, fc=fc, ec=ec, fs=7.3, color=ec if ec == RED else INK)
    if i < len(steps) - 1:
        arrow(x + w, PY + 4.8, x + w + 2.0, PY + 4.8, color="#888", lw=0.8)
    x += w + 2.0

box(PX + 121, PY + 2.2, 35, 20.5, "", fc=REDF, ec=RED, lw=1.5)
txt(PX + 123, PY + 20.3, "Cost per processed tile", fs=10.5, bold=True, color=RED)
txt(PX + 123, PY + 16.4, "• 16 MiB lower masks + 512 KiB of\n   counts and offsets, whether the tile\n   holds one lower node or 32,768", fs=8)
txt(PX + 123, PY + 10.4, "• speculative tiles multiply it: a grid\n   touching ijk = 0 dilates into 8 tiles", fs=8)
txt(PX + 123, PY + 5.0, "64 such grids, batched: 8.4 GiB, 8.1 ms\n(fVDB #835 chunks batches to cap it)", fs=8, color=RED, bold=True)

# ================================================================ THIS PR panel
GX, GY, GW, GH = 2.5, 2.5, 159, 74
panel(GX, GY, GW, GH, GRN)
txt(GX + 3, GY + GH - 3.2, "This PR: a Mask<4> only for the (tile, upper child) pairs the functors touch", fs=13, bold=True, color=GRN)
txt(GX + 3, GY + GH - 6.3, "buildInternalMasks(stream, maxRows, launch) hands the functors a SparseLowerMasks view. Writes go through "
    "lowerMasks.setOnAtomic(tile, uc, bit) / atomicOrWord / orWord instead of lowerMasks[tile][uc].", fs=8.5, color="#444")

# ---- step 1: fill
S1Y = GY + GH - 12.5
txt(GX + 3, S1Y + 2.2, "1  fill: one functor pass, rows claimed on first touch", fs=10, bold=True, color=GRN)
txt(GX + 3, S1Y - 2.3, "row index, t0", fs=8.5, bold=True)
xs0 = strip(GX + 22, S1Y - 3.8, {5: (PAF, PA, "2"), 9: (PBF, PB, "0")}, labels=True, empty="–")
txt(GX + 3, S1Y - 8.3, "row index, t1", fs=8.5, bold=True)
xs1 = strip(GX + 22, S1Y - 9.8, {2: (PCF, PC, "1")}, labels=True, empty="–")
txt(GX + 22, S1Y - 13.2, "uint32 per upper child, holding its pool row: 128 KiB per tile, memset to Unclaimed (–)", fs=7.5, color="#555")

# pool
PoolX, PoolY = GX + 86, S1Y - 10.3
txt(PoolX, S1Y - 0.6, "pool: maxRows × 512 B, rows in claim order", fs=8.5, bold=True)
pool = [("row 0", "(t0, 9)", PBF, PB), ("row 1", "(t1, 2)", PCF, PC), ("row 2", "(t0, 5)", PAF, PA)]
for i, (r, k, fc, ec) in enumerate(pool):
    box(PoolX + i * 11.5, PoolY + 3.2, 10.5, 4.6, f"{r}\nMask<4> {k}", fc=fc, ec=ec, fs=7, color=ec)
cell(PoolX + 3 * 11.5, PoolY + 3.2, 6, 4.6, "…", fc="white", ec="#bbb", fs=8, color="#999")
txt(PoolX, PoolY + 0.6, "maxRows is the consumer's bound: 8 × source lower nodes\nfor refine, 1 × for coarsen, 27 × for dilate", fs=7.2, color="#555")
txt(PoolX, PoolY - 3.0, "(t0, 9) was touched first, so it got row 0", fs=7.2, color="#555", style="italic")

# warp-aggregated claim inset
IX, IY, IW, IH = GX + 128, S1Y - 16.0, 28, 19.5
box(IX, IY, IW, IH, "", fc="white", ec=GRN, lw=1.2)
txt(IX + 1.5, IY + IH - 1.8, "claiming a row, per warp", fs=8.5, bold=True, color=GRN)
lanes = [PA, PA, PB, PA, PB, PB, PA, PC]
for i, c in enumerate(lanes):
    cell(IX + 1.5 + i * 3.1, IY + IH - 5.6, 2.8, 2.4, str(i), fc="white", ec=c, fs=6, color=c, bold=True)
txt(IX + 1.5, IY + IH - 7.7, "lanes, colored by the slot they write", fs=6.5, color="#555")
claim = ["__match_any_sync(slot) → peers",
         "leader: CAS(slot, – → claiming)",
         "   r = atomicAdd(claimed, 1); store r",
         "__shfl_sync(peers, r, leader)",
         "r ≥ maxRows → overflow flag;",
         "   getBuffer throws"]
for i, s in enumerate(claim):
    txt(IX + 1.5, IY + IH - 9.8 - i * 1.55, s, fs=6.6, color=RED if "overflow" in s or "throws" in s else INK)

# ---- step 2: rank
S2Y = GY + 33
txt(GX + 3, S2Y + 8.4, "2  rank, in countNodes: the order of the set upper-mask bits is the lower-node index", fs=10, bold=True, color=GRN)
txt(GX + 3, S2Y + 4.3, "upper mask, t0", fs=8.5, bold=True)
strip(GX + 22, S2Y + 2.8, {5: (PAF, PA, "1"), 9: (PBF, PB, "1")}, empty="0")
txt(GX + 3, S2Y - 1.0, "upper mask, t1", fs=8.5, bold=True)
strip(GX + 22, S2Y - 2.5, {2: (PCF, PC, "1")}, empty="0")
txt(GX + 22, S2Y - 5.0, "4 KiB per tile, as before; 512 words each", fs=7.5, color="#555")
box(GX + 86, S2Y + 1.5, 26, 4.6, "popcount per word,\nexclusive scan (2 KiB per tile)", fc=GREYF, ec=GREY, fs=7.2)
arrow(GX + 22 + 14 * SW + 0.5, S2Y + 3.8, GX + 86, S2Y + 3.8, color="#888", lw=0.8)
txt(GX + 86, S2Y - 1.3, "rank(t, uc) = prefix[t·512 + uc/64]\n             + popc(word below uc)", fs=7.2, color="#444")

RX = GX + 116
txt(RX, S2Y + 7.0, "rank", fs=7.5, bold=True, ha="center")
txt(RX + 9, S2Y + 7.0, "key", fs=7.5, bold=True, ha="center")
txt(RX + 21, S2Y + 7.0, "pool row", fs=7.5, bold=True, ha="center")
ranks = [("0", "(t0, 5)", "2", PAF, PA), ("1", "(t0, 9)", "0", PBF, PB), ("2", "(t1, 2)", "1", PCF, PC)]
for i, (r, k, row, fc, ec) in enumerate(ranks):
    yy = S2Y + 3.6 - i * 3.3
    cell(RX - 2.5, yy, 5, 2.8, r, fc=fc, ec=ec, fs=7.5, color=ec, bold=True)
    cell(RX + 4, yy, 10, 2.8, k, fc=fc, ec=ec, fs=7.5, color=ec)
    cell(RX + 17.5, yy, 7, 2.8, row, fc=fc, ec=ec, fs=7.5, color=ec)
arrow(GX + 112.3, S2Y + 3.8, RX - 2.7, S2Y + 3.8, color="#888", lw=0.8)
txt(RX - 2.5, S2Y - 7.4, "rowKeys / rowOf, one entry per lower node", fs=7.0, color="#555")
txt(GX + 86, S2Y - 7.4, "the same order the dense scan produced,\nso the output is byte-identical", fs=7.5, color=GRN, bold=True)

# ---- step 3: per lower node
S3Y = GY + 13
txt(GX + 3, S3Y + 6.5, "3  count and write, one warp per lower node instead of per slot", fs=10, bold=True, color=GRN)
steps = [("leafCounts[rank] =\npopcount of its pool row", GRNF, GRN),
         ("scan → leafOffsets\n(lower nodes + 1 entries)", GREYF, GREY),
         ("per-grid counts and bases\nat each grid's tile boundaries", GREYF, GREY),
         ("one D2H: counts + overflow flag\n(getBuffer waits, as before)", GRNF, GRN),
         ("processLowerRows: link upper,\nwrite lower node and leaf preambles", GRNF, GRN)]
x = GX + 3
for i, (s, fc, ec) in enumerate(steps):
    w = 23.5
    box(x, S3Y - 1.0, w, 5.4, s, fc=fc, ec=ec, fs=7.2, color=ec if ec == GRN else INK)
    if i < len(steps) - 1:
        arrow(x + w, S3Y + 1.7, x + w + 1.6, S3Y + 1.7, color="#888", lw=0.8)
    x += w + 1.6

box(GX + 128, GY + 2.2, 28, 21.5, "", fc=GRNF, ec=GRN, lw=1.5)
txt(GX + 130, GY + 21.3, "Cost", fs=10.5, bold=True, color=GRN)
txt(GX + 130, GY + 17.6, "• per tile: 4 KiB upper mask +\n   128 KiB index + 2 KiB prefix", fs=8)
txt(GX + 130, GY + 13.0, "• per touched pair: 512 B mask\n   + 16 B key and row", fs=8)
txt(GX + 130, GY + 8.6, "• host waits per build: one, as before", fs=8)
txt(GX + 130, GY + 4.7, "the same 64 grids:\n80 MiB, 2.1 ms", fs=8.5, color=GRN, bold=True)

txt(GX + 3, GY + 3.6, "Deprecated, still working: allocateInternalMaskBuffers, deviceLowerMasks, lowerOffsetRows, leafOffsetRows and the raw "
    "Mask<4>(*)[32768] functor overloads (they forward to a DenseLowerMasks view).", fs=7.5, color="#555", style="italic")

fig.savefig("sparse_lower_masks_diagram.png", dpi=200)
