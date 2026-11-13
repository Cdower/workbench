"""Build the compact JSON the interactive map embeds.

    python figures/web_data.py out/la_nc_equal_pop.gpkg out/web_data.json
"""
import json, sys
import geopandas as gpd, numpy as np, networkx as nx, shapely
gpkg, out = sys.argv[1], sys.argv[2]
d = gpd.read_file(gpkg, layer="districts_equal_pop").to_crs(3310)
n = gpd.read_file(gpkg, layer="nc_current").to_crs(3310)

def color(g, k=6):
    G = nx.Graph(); G.add_nodes_from(range(len(g)))
    s = g.sindex
    for i, j in zip(*s.query(g.geometry.values, predicate="intersects")):
        if i < j and g.geometry.iloc[i].boundary.intersection(g.geometry.iloc[j].boundary).length > 50:
            G.add_edge(i, j)
    c = nx.coloring.greedy_color(G, strategy="DSATUR")
    return [c[i] % k for i in range(len(g))]

d["c"] = color(d); n["c"] = color(n)
def pack(g, props, tol=25):
    g = g.copy(); g["geometry"] = g.geometry.simplify(tol, preserve_topology=True)
    g = g.to_crs(4326)
    # d3-geo is spherical: exterior rings must run clockwise, or a polygon is
    # read as "the whole globe except this shape" and the map collapses.
    def polys(x):
        # snap to the 1e-5° grid we export at, repair, keep real polygons only
        x = shapely.make_valid(shapely.set_precision(x, 1e-5))
        parts = [q for q in shapely.get_parts(x) if q.geom_type == "Polygon" and q.area > 5e-9]  # ~50 m²
        return shapely.union_all(parts) if parts else None
    geoms = [polys(x) for x in g.geometry.values]
    g = g[[x is not None for x in geoms]].copy()
    g["geometry"] = shapely.orient_polygons(np.array([x for x in geoms if x is not None]), exterior_cw=True)
    fc = json.loads(g[props + ["geometry"]].to_json())
    def rnd(x):
        return [rnd(v) for v in x] if isinstance(x[0], (list, tuple)) else [round(x[0], 5), round(x[1], 5)]
    for f in fc["features"]:
        f["geometry"]["coordinates"] = rnd(f["geometry"]["coordinates"]); f.pop("id", None)
    return fc
def title(s):
    s = s.replace(" NC", "").replace(" NDC", "")
    up = lambda x: x[:1] + x[1:].capitalize() if x.startswith("(") else x.capitalize()
    cap = lambda w: "/".join("-".join(up(x) for x in part.split("-")) for part in w.split("/"))
    return " ".join(w if w in ("(CANNDU)", "P.I.C.O.", "LA-32", "NOHO", "CANNDU") else cap(w) for w in s.split()).replace("Noho", "NoHo").replace("Pico", "PICO")
d["name"] = d.district_name.map(title); d["heir"] = d.heir_of.map(title)
d["src"] = d.source_ncs.fillna("").map(lambda s: ", ".join(title(p.rsplit(" ",1)[0]) + " " + p.rsplit(" ",1)[1] for p in s.split(", ") if p))
n["name"] = n.NAME.map(title)
n["certified"] = n["certified"].astype(bool) if "certified" in n else True
data = dict(
    districts=pack(d, ["district_id", "name", "heir", "pop", "dev_pct", "src", "pct_hisp", "pct_white", "pct_black", "pct_asian", "area_sqmi", "c"]),
    ncs=pack(n, ["name", "pop", "dev_pct", "area_sqmi", "c", "certified"]),
    target=int(d["target"].iloc[0]) if "target" in d else int(d.target_pop.iloc[0]), total=int(d["pop"].sum()),
)
json.dump(data, open(out, "w"), separators=(",", ":"))
print(len(json.dumps(data)) // 1024, "KB")
