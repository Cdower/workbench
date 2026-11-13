"""Static figures for the chrisdower.com post (today / redrawn maps and the spread chart).

    python figures/blog_figs.py out/la_nc_equal_pop.gpkg out/figures
"""
import sys, numpy as np, geopandas as gpd, networkx as nx, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from PIL import Image
gpkg, out = sys.argv[1], sys.argv[2]
d = gpd.read_file(gpkg, layer="districts_equal_pop").to_crs(3310)
n = gpd.read_file(gpkg, layer="nc_current").to_crs(3310)
cert = n[n.certified.astype(bool)]; nonc = n[~n.certified.astype(bool)]
T = int(d["target"].iloc[0]) if "target" in d else int(d.target_pop.iloc[0])
INK, MUTED, RULE, BG = "#1b2228", "#5d6a74", "#dcd9d0", "#ffffff"
UNDER, MID, OVER = "#2b7a8c", "#eceae4", "#c2410c"
TINTS = ["#f3d36b", "#f2a7a0", "#a9d4a1", "#9cc5e6", "#d6b9e2", "#f4bf87"]
plt.rcParams.update({"font.family": "DejaVu Sans", "text.color": INK, "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED})

def colors6(g):
    G = nx.Graph(); G.add_nodes_from(range(len(g)))
    for i, j in zip(*g.sindex.query(g.geometry.values, predicate="intersects")):
        if i < j and g.geometry.iloc[i].boundary.intersection(g.geometry.iloc[j].boundary).length > 50:
            G.add_edge(i, j)
    c = nx.coloring.greedy_color(G, strategy="DSATUR")
    return [TINTS[c[i] % 6] for i in range(len(g))]

def save(fig, name, q=82):
    p = f"{out}/{name}.png"; fig.savefig(p, dpi=100, facecolor=BG); plt.close(fig)
    im = Image.open(p).convert("RGB"); im.save(f"{out}/{name}.webp", quality=q, method=6)
    import os; os.remove(p); print(name, os.path.getsize(f"{out}/{name}.webp") // 1024, "KB")

def mapfig():
    fig = plt.figure(figsize=(14, 15.5)); ax = fig.add_axes([0, 0, 1, 1]); ax.set_axis_off()
    return fig, ax

# 1. today
fig, ax = mapfig()
cmap = LinearSegmentedColormap.from_list("dv", [UNDER, MID, OVER])
norm = TwoSlopeNorm(vmin=-1.3, vcenter=0, vmax=1.3)
cert.plot(ax=ax, color=[cmap(norm(np.clip(np.log(max(p, 1) / T), -1.3, 1.3))) for p in cert["pop"]], edgecolor=BG, linewidth=0.8)
nonc.plot(ax=ax, facecolor=BG, edgecolor=MUTED, hatch="////", linewidth=0.3)
for name, lab in [("WILSHIRE CENTER - KOREATOWN NC", "Koreatown\n96,588"), ("HERMON NC", "Hermon\n2,959")]:
    p = cert[cert.NAME == name].geometry.iloc[0].representative_point()
    ax.annotate(lab, (p.x, p.y), xytext=(p.x + 9000, p.y + (6000 if "Herm" in lab else -9000)), fontsize=26, color=INK,
                arrowprops=dict(arrowstyle="-", color=INK, lw=1), ha="left", va="center")
cax = fig.add_axes([0.06, 0.072, 0.34, 0.02])
grad = np.linspace(-1.3, 1.3, 256)[None, :]
cax.imshow(grad, aspect="auto", cmap=cmap, norm=norm, extent=[-1.3, 1.3, 0, 1])
cax.set_yticks([]); cax.set_xticks([np.log(.25), 0, np.log(4)]); cax.set_xticklabels(["¼×", f"{T:,}", "4×"], fontsize=22)
for s in cax.spines.values(): s.set_visible(False)
fig.text(0.06, 0.105, "Residents vs. an equal share", fontsize=24, color=MUTED)
hx = fig.add_axes([0.06, 0.022, 0.03, 0.02]); hx.add_patch(plt.Rectangle((0, 0), 1, 1, facecolor=BG, edgecolor=MUTED, hatch="////")); hx.set_axis_off()
fig.text(0.1, 0.024, "No neighborhood council", fontsize=22, color=MUTED)
save(fig, "today")

# 2. redrawn
fig, ax = mapfig()
d.plot(ax=ax, color=colors6(d), edgecolor=BG, linewidth=1.1)
n.boundary.plot(ax=ax, color=INK, linewidth=0.45, linestyle=(0, (3, 2)), alpha=0.55)
fig.text(0.06, 0.07, f"{len(d)} districts, each {T:,} ±{d.dev_pct.abs().max():.2f}%", fontsize=26, color=INK)
fig.text(0.06, 0.04, "Dashed lines: today's councils", fontsize=22, color=MUTED)
save(fig, "redrawn")

# 3. spread strip
fig, ax = plt.subplots(figsize=(14, 4.2)); fig.subplots_adjust(left=0.15, right=0.97, top=0.84, bottom=0.27)
xmax = 100000
for y, vals, cols, lab in [(1, cert["pop"].values, [cmap(norm(np.clip(np.log(max(p, 1) / T), -1.3, 1.3))) for p in cert["pop"]], "Today"),
                           (0, d["pop"].values, ["#5d6a74"] * len(d), "Redrawn")]:
    o = np.argsort(vals); jit = ((np.arange(len(vals)) % 5) - 2) * 0.06
    ax.scatter(vals[o], y + jit, s=90, c=np.array(cols, dtype=object)[o].tolist(), edgecolors=BG, linewidths=1.2, zorder=3)
    ax.text(-0.01, y, lab, transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=24, color=INK, fontweight="bold")
ax.axvline(T, color=INK, ls=(0, (4, 3)), lw=1.2, zorder=1)
ax.text(T + 800, 1.45, f"equal share {T:,}", fontsize=21, color=MUTED, va="center")
ax.set_xlim(0, xmax); ax.set_ylim(-0.45, 1.6); ax.set_yticks([])
ax.set_xticks(range(0, xmax + 1, 20000)); ax.set_xticklabels([f"{x // 1000}k" for x in range(0, xmax + 1, 20000)], fontsize=21)
for s in ["top", "right", "left"]: ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(RULE); ax.tick_params(axis="x", color=RULE)
ax.set_xlabel("Residents per council (2020)", fontsize=20)
save(fig, "spread", q=88)
