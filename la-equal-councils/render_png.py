"""Quick-look PNG: today's councils next to the redrawn districts.

    python render_png.py out/la_nc_equal_pop.gpkg out/preview.png
"""
import sys
import geopandas as gpd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
gpkg, out = sys.argv[1], sys.argv[2]
d = gpd.read_file(gpkg, layer="districts_equal_pop").to_crs(3310)
n = gpd.read_file(gpkg, layer="nc_current").to_crs(3310)
fig, axes = plt.subplots(1, 2, figsize=(22, 13))
for ax, (lyr, col, title) in zip(axes, [(n, "pop", "Current NCs (2020 pop)"), (d, "district_id", "Equal-population redraw")]):
    if col == "pop":
        lyr.plot(ax=ax, column=col, cmap="viridis", edgecolor="white", linewidth=0.4, legend=True)
    else:
        lyr.plot(ax=ax, column=col, cmap="tab20", edgecolor="white", linewidth=0.6)
        n.boundary.plot(ax=ax, color="black", linewidth=0.3, alpha=0.5)
        for _, r in lyr.iterrows():
            p = r.geometry.representative_point()
            ax.annotate(r.district_name.removesuffix(" NC")[:14], (p.x, p.y), fontsize=4.5, ha="center")
    ax.set_title(title); ax.set_axis_off()
plt.tight_layout(); plt.savefig(out, dpi=130)
