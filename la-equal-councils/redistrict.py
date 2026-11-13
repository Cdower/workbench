#!/usr/bin/env python3
"""
Equal-population Los Angeles neighborhood council districts.

Redraws Los Angeles into N contiguous districts that each hold an equal share
of a 2020 Census count (people by default). Inspired by Neil Freeman's
"Electoral College Reform" map (fakeisthenewreal.org/reform).

Every setting lives in a plan file (plan.toml); command-line flags override it.

  python redistrict.py fetch                         download inputs into data/
  python redistrict.py build                         build with plan.toml (or defaults)
  python redistrict.py build --config plans/adults.toml --tolerance 0.02
  python redistrict.py restat out/la_nc_equal_pop.gpkg   recompute after editing district_id in QGIS

Pipeline
  1. Units: 2020 census blocks (or tracts) inside the chosen territory.
  2. Seeds: one per current council (when districts = 99), otherwise
     population-weighted k-means++.
  3. Balance: optimal-transport LP (every district gets exactly 1/N of the
     balanced count, minimising squared distance to its centre) with Lloyd
     re-centring. Distance is network distance when barriers are on, so
     crossing a freeway or the Mulholland ridge costs more. `stick` adds a
     penalty for splitting the keep_whole groups.
  4. Repair: every district reduced to one connected piece.
  5. Polish + chain push: boundary-block trades between neighbours until the
     largest deviation is within tolerance (contiguity always kept).
  6. Name: each district is named for the current council (or neighborhood
     with no council) most of its residents come from.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LANC_DATA", HERE / "data"))

WORK_CRS = "EPSG:3310"     # California Albers (metres, equal-area)
EXPORT_CRS = "EPSG:4326"

NC_PATHS = [DATA / "nc_certified.geojson",
            DATA / "nc_certified" / "Neighborhood_Councils_(Certified).shp"]
BLOCKS_ZIP = DATA / "tl_2020_06_tabblock20.zip"          # Census TIGER/Line 2020 blocks
NEIGHBORHOODS = DATA / "la_times_neighborhoods.geojson"   # LA GeoHub
CITY_BOUNDARY = DATA / "city_boundary.geojson"            # maps.lacity.org Boundaries/MapServer/7
TRACTS_SHP = DATA / "tl_2020_06037_tract20" / "tl_2020_06037_tract20.shp"
PL_CSV = DATA / "la_county_2020_block_pl94171.csv"       # P.L. 94-171 by block, LA County
ROADS_ZIP = DATA / "tl_2020_06037_roads.zip"
LINEARWATER_ZIP = DATA / "tl_2020_06037_linearwater.zip"
AREAWATER_ZIP = DATA / "tl_2020_06037_areawater.zip"
RAILS_ZIP = DATA / "tl_2020_us_rails.zip"
LA_BBOX = (-118.95, 33.70, -117.65, 34.82)               # lon/lat, LA County

DEMO = ["pop", "pop_hisp", "pop_white", "pop_black", "pop_aian",
        "pop_asian", "pop_nhpi", "pop_other", "pop_two", "vap", "households", "housing_units"]
BALANCE_CHOICES = {
    "pop": "residents",
    "vap": "adults (18+)",
    "households": "households (occupied homes)",
    "housing_units": "housing units",
}
BARRIER_CHOICES = {
    "freeways": "freeways (TIGER primary roads)",
    "mulholland": "Mulholland Drive, the Santa Monica Mountains ridge",
    "la_river": "the Los Angeles River",
    "rail": "railroads and rail transit (TIGER rails)",
}
LA_COUNTY_POP_2020 = 10_014_009


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 0. the plan: defaults < plan.toml < command-line flags
# --------------------------------------------------------------------------
@dataclass
class Plan:
    districts: int = 99                 # how many districts to draw
    balance_on: str = "pop"             # pop | vap | households | housing_units
    tolerance: float = 0.01             # largest allowed deviation from an equal share (0.01 = ±1%)
    coverage: str = "all"               # all | councils | comma-separated LA Times neighborhoods
    units: str = "blocks"               # blocks | tracts
    seeds: str = "auto"                 # auto | councils | kmeans
    keep_whole: str = "none"            # none | councils | neighborhoods: groups to keep together (optional)
    stick: float = 1.0                  # with keep_whole: how hard to keep those groups whole (0 = not at all)
    keep_small_whole: bool = True       # with keep_whole: groups smaller than a district stay whole unless the tolerance needs a split
    barriers: list = field(default_factory=lambda: ["freeways", "rail", "la_river", "mulholland"])
    smooth: bool = True                 # after balancing, move lines onto barriers (and keep_whole lines)
    barrier_routing: bool = False       # also measure distance along the street network (crossings cost more)
    barrier_strength: float = 3.0       # with barrier_routing: distance multiplier for crossing a barrier
    population_file: str = ""           # optional block-level counts to use instead (e.g. Statewide Database adjusted)
    population_columns: dict = field(default_factory=dict)  # our column -> its column, e.g. {pop = "TOTPOP"}
    random_seed: int = 1                # for k-means seeds
    out: str = "out"                    # output folder (relative to the plan file's folder)

    def validate(self):
        if self.districts < 2:
            raise SystemExit("districts must be at least 2")
        if self.balance_on not in BALANCE_CHOICES:
            raise SystemExit(f"balance_on must be one of {', '.join(BALANCE_CHOICES)}")
        if self.population_file and not Path(self.population_file).exists():
            raise SystemExit(f"population_file {self.population_file} not found")
        if not 0 < self.tolerance < 0.5:
            raise SystemExit("tolerance is a fraction: 0.01 means ±1%")
        if self.units not in ("blocks", "tracts"):
            raise SystemExit("units must be blocks or tracts")
        if self.seeds not in ("auto", "councils", "kmeans"):
            raise SystemExit("seeds must be auto, councils or kmeans")
        if self.keep_whole not in ("councils", "neighborhoods", "none"):
            raise SystemExit("keep_whole must be councils, neighborhoods or none")
        for b in self.barriers:
            if b not in BARRIER_CHOICES and not Path(b).exists():
                raise SystemExit(f"barrier {b!r} is not one of {', '.join(BARRIER_CHOICES)} or an existing file")
        return self


def load_plan(path: str | None, overrides: dict) -> Plan:
    plan = Plan()
    base = Path.cwd()
    if path is None and (HERE / "plan.toml").exists():
        path = str(HERE / "plan.toml")
    if path:
        p = Path(path)
        raw = tomllib.loads(p.read_text())
        flat = {}
        for k, v in raw.items():                     # sections are only for readability
            if isinstance(v, dict):
                flat.update(v)
            else:
                flat[k] = v
        known = {f.name for f in fields(Plan)}
        unknown = set(flat) - known
        if unknown:
            raise SystemExit(f"{p.name}: unknown setting(s) {', '.join(sorted(unknown))}")
        for k, v in flat.items():
            setattr(plan, k, v)
        base = p.parent
        log(f"plan: {p}")
    for k, v in overrides.items():
        if v is not None:
            setattr(plan, k, v)
    if isinstance(plan.barriers, str):
        plan.barriers = [b.strip() for b in plan.barriers.split(",") if b.strip()]
    plan.barriers = [b if b in BARRIER_CHOICES else str((base / b).resolve()) for b in plan.barriers]
    if plan.population_file and not Path(plan.population_file).is_absolute():
        plan.population_file = str((base / plan.population_file).resolve())
    if not Path(plan.out).is_absolute():
        plan.out = str((base / plan.out).resolve())
    return plan.validate()


# --------------------------------------------------------------------------
# 1. data
# --------------------------------------------------------------------------
def load_ncs(coverage: str) -> gpd.GeoDataFrame:
    """Certified NCs (certified=True), then the land outside them that the
    plan covers (certified=False), named by LA Times neighborhood."""
    for p in NC_PATHS:
        if p.exists():
            nc = gpd.read_file(p)
            break
    else:
        raise SystemExit("No NC boundary file found in data/ (run: python redistrict.py fetch)")
    nc = nc[["NAME", "NC_ID", "SERVICE_RE", "geometry"]].to_crs(WORK_CRS)
    nc["geometry"] = nc.geometry.make_valid()
    nc = nc.sort_values("NAME").reset_index(drop=True)
    nc["certified"] = True
    add = [a.strip() for a in coverage.split(",") if a.strip() and a.strip() != "councils"]
    if add:
        hoods = gpd.read_file(NEIGHBORHOODS).to_crs(WORK_CRS)
        hoods["geometry"] = hoods.geometry.make_valid()
        taken = nc.union_all()
        if "all" in add:
            city = gpd.read_file(CITY_BOUNDARY).to_crs(WORK_CRS).geometry.make_valid().union_all()
            gap = city.difference(taken)
            pieces = gpd.overlay(gpd.GeoDataFrame(geometry=[gap], crs=WORK_CRS), hoods[["name", "geometry"]],
                                 how="identity", keep_geom_type=True)
            pieces["name"] = pieces["name"].fillna("Other city land")
            extra = pieces.dissolve("name").reset_index()
            extra = extra[extra.geometry.area > 1.0]
        else:
            missing = set(add) - set(hoods.name)
            if missing:
                raise SystemExit(f"Not in the LA Times neighborhood layer: {sorted(missing)}")
            extra = hoods[hoods.name.isin(add)].copy()
            extra["geometry"] = extra.geometry.difference(taken)
        extra = gpd.GeoDataFrame({"NAME": extra.name.str.upper(), "NC_ID": None, "SERVICE_RE": None,
                                  "certified": False}, geometry=extra.geometry.values, crs=WORK_CRS)
        nc = pd.concat([nc, extra.sort_values("NAME")], ignore_index=True)
        log(f"territory: {int(nc.certified.sum())} certified NCs + {len(extra)} areas outside them")
    return nc


def load_pl(population_file: str = "", population_columns: dict | None = None) -> pd.DataFrame:
    if not PL_CSV.exists():
        raise SystemExit("Census counts missing (run: python redistrict.py fetch)")
    pl = pd.read_csv(PL_CSV, dtype={"GEOID20": str})
    assert int(pl["pop"].sum()) == LA_COUNTY_POP_2020, "LA County 2020 total mismatch"
    if population_file:
        # e.g. the Statewide Database's adjusted P.L. 94-171, with incarcerated people
        # counted at their last address (FAIR MAPS Act). Columns not mapped keep census values.
        alt = pd.read_csv(population_file, dtype=str)
        key = "GEOID20" if "GEOID20" in alt else "BLOCK20" if "BLOCK20" in alt else None
        if key is None:
            raise SystemExit(f"{population_file} needs a GEOID20 or BLOCK20 column")
        alt = alt.set_index(alt[key].str.zfill(15))
        cols = population_columns or {"pop": "pop"}
        for ours, theirs in cols.items():
            if ours not in DEMO or theirs not in alt:
                raise SystemExit(f"population_columns: can't map {ours} -> {theirs}")
            new = pl.GEOID20.map(pd.to_numeric(alt[theirs], errors="coerce"))
            pl[ours] = new.fillna(pl[ours]).round().astype(int)
        log(f"population: {Path(population_file).name} for {', '.join(cols)} "
            f"(LA County residents {int(pl['pop'].sum()):,})")
    missing = [c for c in DEMO if c not in pl]
    if missing:
        raise SystemExit(f"{PL_CSV.name} lacks {', '.join(missing)} (run: python redistrict.py fetch --force)")
    return pl


def load_units(kind: str, nc: gpd.GeoDataFrame, plan=None) -> gpd.GeoDataFrame:
    pl = load_pl(plan.population_file if plan else "", plan.population_columns if plan else None)
    if kind == "blocks":
        if not BLOCKS_ZIP.exists():
            raise SystemExit("Census blocks missing (run: python redistrict.py fetch), or use units = \"tracts\"")
        g = gpd.read_file(BLOCKS_ZIP, where="COUNTYFP20 = '037'",
                          columns=["GEOID20", "COUNTYFP20", "INTPTLAT20", "INTPTLON20", "ALAND20", "AWATER20"])
        g = g.to_crs("EPSG:4269").merge(pl, on="GEOID20", how="left")
    else:
        g = gpd.read_file(TRACTS_SHP)[["GEOID20", "INTPTLAT20", "INTPTLON20", "ALAND20", "AWATER20", "geometry"]]
        agg = pl.assign(GEOID20=pl.GEOID20.str[:11]).groupby("GEOID20")[DEMO].sum().reset_index()
        g = g.merge(agg, on="GEOID20", how="left")
    g[DEMO] = g[DEMO].fillna(0).astype(int)
    g = g.rename(columns={"GEOID20": "unit_id"})
    ip = gpd.GeoSeries(gpd.points_from_xy(g.INTPTLON20.astype(float), g.INTPTLAT20.astype(float)),
                       crs="EPSG:4269").to_crs(WORK_CRS)
    ipg = gpd.GeoDataFrame({"i": np.arange(len(g))}, geometry=ip)
    j = gpd.sjoin(ipg, nc[["NAME", "geometry"]], predicate="within", how="inner")
    j = j[~j.index.duplicated()]
    g = g.to_crs(WORK_CRS)
    g["nc_current"] = None
    g.loc[j["i"].values, "nc_current"] = j["NAME"].values
    g["ipx"], g["ipy"] = ip.x.values, ip.y.values
    g = g[g.nc_current.notna()].reset_index(drop=True)
    g["geometry"] = g.geometry.make_valid()
    log(f"{kind}: {len(g):,} units, {g['pop'].sum():,} residents")
    return g


def consolidate_extras(nc, units, min_pop=100):
    """Fold uncovered slivers (fewer than `min_pop` residents) into one
    'OTHER CITY LAND' area so the map isn't littered with empty edge strips."""
    pop = units.groupby("nc_current")["pop"].sum()
    small = nc[~nc.certified & ((nc.NAME.map(pop).fillna(0) < min_pop) | (nc.NAME == "OTHER CITY LAND"))].NAME.tolist()
    if not small:
        return nc, units
    units = units.copy()
    units.loc[units.nc_current.isin(small), "nc_current"] = "OTHER CITY LAND"
    keep = nc[~nc.NAME.isin(small)]
    other = gpd.GeoDataFrame({"NAME": ["OTHER CITY LAND"], "NC_ID": [None], "SERVICE_RE": [None], "certified": [False]},
                             geometry=[nc[nc.NAME.isin(small)].union_all()], crs=nc.crs)
    nc = pd.concat([keep, other], ignore_index=True)
    log(f"folded {len(small)} slivers under {min_pop} residents into OTHER CITY LAND")
    return nc, units


def group_labels(units, keep_whole):
    """The groups `stick` tries to keep whole, as one label per unit."""
    if keep_whole == "councils":
        return units.nc_current.values
    if keep_whole == "neighborhoods":
        hoods = gpd.read_file(NEIGHBORHOODS).to_crs(WORK_CRS)[["name", "geometry"]]
        pts = gpd.GeoDataFrame({"i": np.arange(len(units))},
                               geometry=gpd.points_from_xy(units.ipx, units.ipy), crs=WORK_CRS)
        j = gpd.sjoin(pts, hoods, predicate="within", how="left")
        j = j[~j.index.duplicated()]
        lab = j["name"].values.astype(object)
        lab[pd.isna(lab)] = units.nc_current.values[pd.isna(lab)]
        return lab
    return None


def load_barriers(names) -> gpd.GeoSeries | None:
    """Barrier lines in WORK_CRS from built-in names or line files."""
    if not names:
        return None
    parts = []
    for b in names:
        if b in ("freeways", "mulholland"):
            if not ROADS_ZIP.exists():
                raise SystemExit("TIGER roads missing (run: python redistrict.py fetch)")
            r = gpd.read_file(ROADS_ZIP, columns=["FULLNAME", "MTFCC"]).to_crs(WORK_CRS)
            sel = r[r.MTFCC == "S1100"] if b == "freeways" else r[r.FULLNAME.fillna("").str.startswith("Mulholland")]
            parts.append(sel.geometry)
        elif b == "rail":
            if not RAILS_ZIP.exists():
                raise SystemExit("TIGER rails missing (run: python redistrict.py fetch)")
            r = gpd.read_file(RAILS_ZIP, bbox=LA_BBOX).to_crs(WORK_CRS)
            parts.append(r.geometry)
        elif b == "la_river":
            if not LINEARWATER_ZIP.exists() or not AREAWATER_ZIP.exists():
                raise SystemExit("TIGER water missing (run: python redistrict.py fetch)")
            lw = gpd.read_file(LINEARWATER_ZIP, columns=["FULLNAME"]).to_crs(WORK_CRS)
            aw = gpd.read_file(AREAWATER_ZIP, columns=["FULLNAME"]).to_crs(WORK_CRS)
            parts.append(lw[lw.FULLNAME.fillna("").str.contains("Los Angeles Riv")].geometry)
            parts.append(aw[aw.FULLNAME.fillna("").str.contains("Los Angeles Riv")].boundary)
        else:
            g = gpd.read_file(b).to_crs(WORK_CRS).geometry
            parts.append(gpd.GeoSeries([x.boundary if "Polygon" in x.geom_type else x for x in g if x is not None],
                                       crs=WORK_CRS))
        log(f"barrier: {b if b in BARRIER_CHOICES else Path(b).name}")
    return gpd.GeoSeries(pd.concat(parts, ignore_index=True), crs=WORK_CRS)


# --------------------------------------------------------------------------
# 2. adjacency graph (rook: shared edge, not just a corner)
# --------------------------------------------------------------------------
def build_graph(units: gpd.GeoDataFrame, barriers=None, min_shared_m: float = 1.0) -> nx.Graph:
    geoms = units.geometry.values
    G = nx.Graph()
    G.add_nodes_from(range(len(units)))
    left, right = units.sindex.query(geoms, predicate="intersects")
    m = left < right
    left, right = left[m], right[m]
    bl = gpd.GeoSeries(geoms[left]).boundary.values
    br = gpd.GeoSeries(geoms[right]).boundary.values
    shared = gpd.GeoSeries(bl, crs=WORK_CRS).intersection(gpd.GeoSeries(br, crs=WORK_CRS), align=False)
    length = shared.length.values
    cross = np.zeros(len(left), bool)
    if barriers is not None and len(barriers):
        # an edge "crosses" a barrier when its shared boundary runs along one:
        # at least 2 of 3 points sampled along it lie within 25 m of a barrier line
        import shapely
        lines = shapely.get_parts(barriers.values)
        tree = shapely.STRtree(lines)
        ok = np.flatnonzero(length >= min_shared_m)
        near = np.zeros(len(left), int)
        for f in (0.25, 0.5, 0.75):
            p = shapely.line_interpolate_point(shapely.line_merge(shared.values[ok]), f, normalized=True)
            hit = np.unique(tree.query(p, predicate="dwithin", distance=25)[0])
            near[ok[hit]] += 1
        cross = near >= 2
    for a, b, s, c in zip(left, right, length, cross):
        if s >= min_shared_m:
            G.add_edge(int(a), int(b), w=float(s), barrier=bool(c))
    comps = sorted(nx.connected_components(G), key=len, reverse=True)
    if len(comps) > 1:                                # stitch islands to their nearest unit
        pts = np.c_[units.ipx, units.ipy]
        main = np.fromiter(comps[0], int)
        kd = cKDTree(pts[main])
        for c in comps[1:]:
            c = np.fromiter(c, int)
            d, k = kd.query(pts[c])
            i = int(np.argmin(d))
            G.add_edge(int(c[i]), int(main[k[i]]), w=0.0, barrier=False)
        log(f"graph: stitched {len(comps) - 1} island component(s)")
    nb = sum(1 for *_, c in G.edges(data="barrier") if c)
    log(f"graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges"
        + (f", {nb:,} along barriers" if barriers is not None else ""))
    return G


# --------------------------------------------------------------------------
# 2b. whole-council units (FAIR MAPS rank 2: keep communities whole)
# --------------------------------------------------------------------------
def consolidate_groups(G, assign, w, pts, K, tol, labels, small):
    """Reunite each split small group (FAIR MAPS rank 2) by moving its stray pieces
    into the district holding most of it, then rebalancing with blocks that aren't
    part of a reunited group. A group that can't be reunited within the tolerance,
    or without splitting a district, is put back and left split."""
    T = w.sum() / K
    lab = pd.Series(labels)
    idx_of = {g: np.flatnonzero(lab.values == g) for g in small}
    locked = np.zeros(len(assign), bool)

    def minority(g):
        i = idx_of[g]
        sh = np.bincount(assign[i], weights=w[i] + 1e-9, minlength=K)
        return sh.sum() - sh.max()

    whole, kept_split = [], []
    for g in sorted(small, key=minority):
        i = idx_of[g]
        sh = np.bincount(assign[i], weights=w[i] + 1e-9, minlength=K)
        M = int(np.argmax(sh))
        stray = i[assign[i] != M]
        if len(stray) == 0:
            locked[i] = True
            whole.append(g)
            continue
        before = assign.copy()
        losers = set(assign[stray].tolist())
        assign[stray] = M
        ok = all(nx.is_connected(G.subgraph(np.flatnonzero(assign == d))) for d in losers | {M}
                 if (assign == d).any())
        if ok:
            locked[i] = True
            if np.abs(np.bincount(assign, weights=w, minlength=K) - T).max() / T > tol:
                assign = polish(G, assign, w, pts, K, tol, max_passes=40, locked=locked, quiet=True)
                assign = chain_push(G, assign, w, pts, K, tol, max_iter=400, locked=locked, quiet=True)
            ok = np.abs(np.bincount(assign, weights=w, minlength=K) - T).max() / T <= tol
        if ok:
            whole.append(g)
        else:
            assign[:] = before
            locked[i] = False
            kept_split.append(g)
    log(f"whole groups: {len(whole)} of {len(small)} small groups kept whole"
        + (f"; left split to stay within tolerance: {', '.join(kept_split)}" if kept_split else ""))
    return assign, locked, kept_split


def cut_cost(G, labels):
    """Cost of a district line along each edge: 1 for an ordinary street, less along
    a barrier (freeway, rail, river, ridge; FAIR MAPS rank 4) or, with keep_whole,
    a line between two groups."""
    def c(u, v):
        x = 1.0
        if G[u][v].get("barrier"):
            x -= 0.6
        if labels is not None and labels[u] != labels[v]:
            x -= 0.6
        return max(x, 0.1)
    return c


def smooth_boundaries(G, assign, w, K, tol, labels, passes=6, locked=None):
    """Move boundary units when that makes the lines cheaper (onto barriers, fewer
    cut edges), staying inside the tolerance and contiguous."""
    T = w.sum() / K
    lo, hi = T * (1 - tol), T * (1 + tol)
    mem = district_members(assign, K)
    pk = np.bincount(assign, weights=w, minlength=K)
    c = cut_cost(G, labels)
    total = 0
    for p in range(passes):
        moved = 0
        for u in range(len(assign)):
            if locked is not None and locked[u]:
                continue
            A = assign[u]
            nb = {}
            for v in G[u]:
                nb.setdefault(assign[v], []).append(v)
            if all(k == A for k in nb):
                continue
            stay = sum(c(u, v) for v in nb.get(A, []))
            for B, vs in nb.items():
                if B == A:
                    continue
                gain = stay - sum(c(u, v) for v in vs)
                if gain < -1e-9 and pk[A] - w[u] >= lo and pk[B] + w[u] <= hi and connected_without(G, mem[A], u):
                    assign[u] = B
                    mem[A].discard(u); mem[B].add(u)
                    pk[A] -= w[u]; pk[B] += w[u]
                    moved += 1
                    break
        total += moved
        if moved == 0:
            break
    log(f"smoothing: moved {total:,} units onto cheaper lines")
    return assign


def fix_multipart(blocks, assign, K):
    """Districts must be one piece on the ground (corners and water don't count).
    Any outlying piece joins the district it shares the longest boundary with."""
    dis = gpd.GeoDataFrame({"d": assign}, geometry=blocks.geometry.values, crs=blocks.crs).dissolve("d")
    moved, left = 0, []
    sidx = blocks.sindex
    for d, geom in dis.geometry.items():
        parts = sorted(getattr(geom, "geoms", [geom]), key=lambda g: g.area, reverse=True)
        for part in parts[1:]:
            cand = sidx.query(part, predicate="intersects")
            idx = [i for i in cand if assign[i] == d and blocks.geometry.values[i].representative_point().within(part.buffer(1))]
            best, blen = None, 1.0
            for e, g2 in dis.geometry.items():
                if e != d and g2.intersects(part.buffer(1)):
                    L = part.boundary.intersection(g2.buffer(1)).length
                    if L > blen:
                        best, blen = e, L
            if best is None or not idx:
                left.append(int(d))
                continue
            assign[idx] = best
            moved += len(idx)
    log(f"contiguity: moved {moved} block(s) from detached pieces" + (f"; {len(left)} piece(s) left (islands)" if left else ""))
    return assign


def split_table(units, bal, T, tol):
    """Per council (and uncovered area): how its residents are spread across the new
    districts. Today's councils aren't a constraint; this measures how much changes.
    effective_splits: 1/sum(p^2) - 1 (50/50 = 1, 90/10 = 0.22); split_pairs: chance two
    residents land in different districts; min_pieces: the fewest districts it must span."""
    t = units.groupby(["nc_current", "district_id"]).agg(pop=("pop", "sum"), n=("pop", "size"), b=(bal, "sum")).reset_index()
    rows = []
    for g, x in t.groupby("nc_current"):
        tot = max(x["pop"].sum(), 1)
        p = x["pop"] / tot
        pieces = int((x["pop"] > 0).sum()) or 1
        min_pieces = max(1, int(np.ceil(x["b"].sum() / (T * (1 + tol)))))
        rows.append({"NAME": g, "pop": int(x["pop"].sum()), "pieces": pieces, "min_pieces": min_pieces,
                     "extra_pieces": max(0, pieces - min_pieces),
                     "effective_splits": round(float(1 / (p ** 2).sum() - 1), 3),
                     "split_pairs": round(float(1 - (p ** 2).sum()), 3),
                     "districts": "; ".join(f"{d} ({v:.0%})" for d, v in
                                            zip(x.district_id, p) if v > 0)})
    return pd.DataFrame(rows).sort_values(["extra_pieces", "effective_splits"], ascending=False)


class NetworkDistance:
    """Shortest-path distance over the adjacency graph, where an edge that
    crosses a barrier is `strength` times longer than the straight line."""

    def __init__(self, G, pts, strength):
        from scipy.sparse import coo_matrix
        e = np.array([(a, b, 1.0 if not c else strength) for a, b, c in G.edges(data="barrier")])
        a, b, mult = e[:, 0].astype(int), e[:, 1].astype(int), e[:, 2]
        w = np.hypot(*(pts[a] - pts[b]).T) * mult + 1e-6
        n = len(pts)
        self.M = coo_matrix((np.r_[w, w], (np.r_[a, b], np.r_[b, a])), shape=(n, n)).tocsr()
        self.tree = cKDTree(pts)

    def from_centres(self, centres):
        from scipy.sparse.csgraph import dijkstra
        _, src = self.tree.query(centres)
        d0, _ = self.tree.query(centres)
        D = dijkstra(self.M, directed=False, indices=src)          # K x N
        return (D + d0[:, None]).T                                  # N x K


# --------------------------------------------------------------------------
# 3. balanced assignment: optimal transport LP + Lloyd re-centring
# --------------------------------------------------------------------------
def lp_assign(D2, w, home, stick, R2, cand=None, pairs=None):
    """Assign units to K districts, every district exactly sum(w)/K, minimising
    D2/R2 + stick * [district != home]. D2 is N x K squared distance.
    Candidates: all districts, the `cand` nearest per unit, or explicit `pairs`."""
    from scipy.optimize import linprog
    from scipy.sparse import coo_matrix, vstack
    N, K = D2.shape
    T = w.sum() / K
    if pairs is not None:
        rows, cols = pairs
    elif cand is None:
        rows, cols = np.repeat(np.arange(N), K), np.tile(np.arange(K), N)
    else:
        rows, cols = np.repeat(np.arange(N), cand.shape[1]), cand.ravel()
    cost = D2[rows, cols] / R2 + stick * ((home[rows] >= 0) & (cols != home[rows]))
    nv = len(rows)
    A = vstack([coo_matrix((np.ones(nv), (rows, np.arange(nv))), shape=(N, nv)),
                coo_matrix((w[rows], (cols, np.arange(nv))), shape=(K, nv))]).tocsr()
    r = linprog(cost, A_eq=A, b_eq=np.r_[np.ones(N), np.full(K, T)], bounds=(0, 1), method="highs-ds")
    if r.x is None:
        if pairs is not None:
            log("  restricted assignment infeasible; widening to the 12 nearest districts")
            return lp_assign(D2, w, home, stick, R2, np.argsort(D2, axis=1)[:, :min(K, 12)])
        if cand is not None and cand.shape[1] < K:
            k2 = min(K, cand.shape[1] * 2)
            return lp_assign(D2, w, home, stick, R2, np.argsort(D2, axis=1)[:, :k2])
        raise RuntimeError(r.message)
    order = np.lexsort((-r.x, rows))
    first = np.r_[True, rows[order][1:] != rows[order][:-1]]
    a = np.empty(N, int)
    a[rows[order][first]] = cols[order][first]
    return a


def kmeans_pp(pts, w, K, rng):
    p = w / w.sum()
    c = [pts[rng.choice(len(pts), p=p)]]
    for _ in range(1, K):
        d2 = ((pts[:, None, :] - np.array(c)[None]) ** 2).sum(-1).min(1) * w
        c.append(pts[rng.choice(len(pts), p=d2 / d2.sum())])
    return np.array(c)


def group_home(groups, a, w, K):
    """For each unit, the district holding most of its group's count."""
    df = pd.DataFrame({"g": groups, "d": a, "w": w + 1e-9})
    best = df.groupby(["g", "d"])["w"].sum().reset_index().sort_values("w").drop_duplicates("g", keep="last")
    return pd.Series(groups).map(best.set_index("g")["d"]).fillna(-1).astype(int).values


def balance(units, w, K, plan, groups, council_home, netdist, G):
    """Coarse-to-fine: Lloyd/LP on block groups (or tracts), then one LP on units."""
    pts = np.c_[units.ipx, units.ipy].astype(float) / 1000.0     # km
    N = len(pts)
    stick = plan.stick if groups is not None else 0.0
    # coarse level: block groups
    key = units["coarse"] if "coarse" in units else units.unit_id
    ck, cidx = np.unique(key.values, return_inverse=True)
    C = len(ck)
    cw = np.bincount(cidx, weights=w, minlength=C)
    ww = w + 1e-6
    cpts = np.c_[np.bincount(cidx, ww * pts[:, 0], C), np.bincount(cidx, ww * pts[:, 1], C)] / np.bincount(cidx, ww, C)[:, None]

    seeds = plan.seeds
    if seeds == "auto":
        seeds = "councils" if council_home is not None else "kmeans"
    if seeds == "councils":
        if council_home is None:
            raise SystemExit(f"seeds = \"councils\" needs districts = {units.attrs['n_certified']} (one per council)")
        centres = np.array([np.average(pts[council_home == k], axis=0, weights=ww[council_home == k]) for k in range(K)])
        home = council_home.copy() if plan.keep_whole == "councils" else np.full(N, -1)
    else:
        centres = kmeans_pp(cpts, cw + 1e-6, K, np.random.default_rng(plan.random_seed))
        home = np.full(N, -1)
        log(f"seeds: k-means++ for {K} districts (random_seed {plan.random_seed})")

    def dist2(centres_km, level):
        """Cost of each unit-district pair. Straight-line: squared distance (compact,
        convex districts). Network: plain shortest-path distance, because squared
        network distances can split a district in two; plain ones can't (every
        block's path to its centre stays inside its district)."""
        if netdist is None:
            P = cpts if level == "coarse" else pts
            return ((P[:, None, :] - centres_km[None]) ** 2).sum(-1)
        D = netdist.from_centres(centres_km * 1000.0) / 1000.0      # N x K, km
        if level == "fine":
            return D
        return np.vstack([np.bincount(cidx, ww * D[:, k], C) for k in range(K)]).T / np.bincount(cidx, ww, C)[:, None]

    T = w.sum() / K
    chome_of = lambda h: pd.DataFrame({"c": cidx, "h": h, "w": ww}).groupby(["c", "h"])["w"].sum() \
        .reset_index().sort_values("w").drop_duplicates("c", keep="last").set_index("c")["h"].reindex(range(C)).fillna(-1).astype(int).values
    R2 = None
    for it in range(12):
        D2 = dist2(centres, "coarse")
        if R2 is None:
            R2 = np.median(D2.min(1)) * 2 + 1e-9                   # scale of "nearby"
        ch = chome_of(home)
        cand = None if K <= 24 else np.argsort(D2, axis=1)[:, :24]  # nearest 24 districts per block group
        a = lp_assign(D2, cw, ch, stick, R2, cand)
        pk = np.bincount(a, weights=cw, minlength=K)
        log(f"  lloyd {it}: coarse max deviation {np.abs(pk - T).max() / T:.2%}")
        new = centres.copy()
        for k in range(K):
            m = a == k
            if m.any():
                new[k] = np.average(cpts[m], axis=0, weights=cw[m] + 1e-6)
        shift = np.sqrt(((new - centres) ** 2).sum(1)).max()
        centres = new
        if groups is not None and stick > 0 and not (seeds == "councils" and plan.keep_whole == "councils"):
            home = group_home(groups, a[cidx], w, K)
        if shift < 0.05 and it >= 2:
            break
    # make the block-group plan contiguous before refining it: `stick` (and ties)
    # can leave a neighborhood's far end attached to a district it doesn't touch
    eu, ev = np.array(list(G.edges())).T
    cu, cv = cidx[eu], cidx[ev]
    m = cu != cv
    CG = nx.Graph()
    CG.add_nodes_from(range(C))
    for x, y in zip(cu[m], cv[m]):
        if CG.has_edge(x, y):
            CG[x][y]["w"] += 1
        else:
            CG.add_edge(int(x), int(y), w=1.0)
    a = repair_contiguity(CG, a.copy(), cw, K)
    if len(np.unique(a)) < K:
        raise SystemExit("a district vanished while drawing; try another random_seed or a lower stick")
    # Block-level refinement by LP only keeps districts in one piece when the cost is
    # straight-line distance and each block's "home" is the district grown from its
    # own council (or there's no home at all). Otherwise start from the contiguous
    # block-group plan and let polish/chain_push balance it block by block.
    refine = netdist is None and (stick == 0 or (seeds == "councils" and plan.keep_whole == "councils"))
    if not refine:
        a = a[cidx]
        pk = np.bincount(a, weights=w, minlength=K)
        log(f"block-group plan: max deviation {np.abs(pk - T).max() / T:.2%} (balanced block by block next)")
        return a
    # fine level: each block may only go to a district its own block group or a
    # neighbouring block group landed in, so the result stays in one piece
    D2 = dist2(centres, "fine")
    nb = np.unique(np.r_[np.c_[cu[m], a[cv[m]]], np.c_[cv[m], a[cu[m]]], np.c_[np.arange(C), a]], axis=0)  # (block group, district)
    allowed = pd.DataFrame(nb, columns=["c", "d"])
    fine = pd.DataFrame({"u": np.arange(N), "c": cidx}).merge(allowed, on="c")
    a = lp_assign(D2, w.astype(float), home, stick, R2, pairs=(fine.u.values, fine.d.values))
    pk = np.bincount(a, weights=w, minlength=K)
    log(f"balanced assignment: max deviation {np.abs(pk - T).max() / T:.2%}")
    return a


# --------------------------------------------------------------------------
# 4. contiguity repair
# --------------------------------------------------------------------------
def district_members(assign, K):
    mem = [set() for _ in range(K)]
    for u, d in enumerate(assign):
        mem[d].add(u)
    return mem


def repair_contiguity(G, assign, w, K):
    changed, rounds = True, 0
    while changed and rounds < 50:
        changed, rounds = False, rounds + 1
        mem = district_members(assign, K)
        for d in range(K):
            if not mem[d]:
                continue
            comps = list(nx.connected_components(G.subgraph(mem[d])))
            if len(comps) == 1:
                continue
            comps.sort(key=lambda c: (w[list(c)].sum(), len(c)), reverse=True)
            for c in comps[1:]:
                votes = {}
                for u in c:
                    for v in G[u]:
                        if assign[v] != d:
                            votes[assign[v]] = votes.get(assign[v], 0) + G[u][v]["w"] + 1e-6
                if votes:
                    tgt = max(votes, key=votes.get)
                    for u in c:
                        assign[u] = tgt
                    changed = True
    return assign


# --------------------------------------------------------------------------
# 5. local search (keeps contiguity)
# --------------------------------------------------------------------------
def connected_without(G, members: set, u: int) -> bool:
    rest = members - {u}
    if not rest:
        return False
    nbrs = [v for v in G[u] if v in rest]
    if len(nbrs) <= 1:
        return True
    start, targets = nbrs[0], set(nbrs[1:])
    seen, q = {start}, deque([start])
    while q and targets:
        x = q.popleft()
        for y in G[x]:
            if y in rest and y not in seen:
                seen.add(y)
                targets.discard(y)
                q.append(y)
    return not targets


def polish(G, assign, w, pts, K, tol, max_passes=400, locked=None, quiet=False):
    T = w.sum() / K
    mem = district_members(assign, K)
    pk = np.array([w[list(m)].sum() for m in mem], float)

    def centre(d):
        idx = np.fromiter(mem[d], int)
        return np.average(pts[idx], axis=0, weights=w[idx] + 1e-3)

    cent = np.array([centre(d) for d in range(K)])
    R2 = np.median([((pts[np.fromiter(mem[d], int)] - cent[d]) ** 2).sum(1).mean() for d in range(K)])
    for p in range(max_passes):
        if np.abs(pk - T).max() / T <= tol:
            break
        moved = 0
        for A in np.argsort(-np.abs(pk - T)):
            for _rep in range(4):
                cands = {}
                for u in mem[A]:
                    if locked is not None and locked[u]:
                        continue
                    pu = w[u]
                    for v in G[u]:
                        B = assign[v]
                        if B == A or (u, B) in cands:
                            continue
                        gain = (pk[A] - T) ** 2 + (pk[B] - T) ** 2 - (pk[A] - pu - T) ** 2 - (pk[B] + pu - T) ** 2
                        if gain > 1e-9:
                            comp = ((pts[u] - cent[B]) ** 2).sum() - ((pts[u] - cent[A]) ** 2).sum()
                            # prefer handing a block across an ordinary edge, not across a barrier
                            across = all(G[u][x].get("barrier", False) for x in G[u] if assign[x] == B)
                            cands[(u, B)] = comp / R2 - gain / T ** 2 * 50 + (3.0 if across else 0.0)
                done = False
                for (u, B) in sorted(cands, key=cands.get):
                    if connected_without(G, mem[A], u):
                        assign[u] = B
                        mem[A].discard(u); mem[B].add(u)
                        pk[A] -= w[u]; pk[B] += w[u]
                        moved += 1
                        done = True
                        break
                if not done:
                    break
        if p % 25 == 0:
            cent = np.array([centre(d) for d in range(K)])
            if not quiet:
                log(f"  polish pass {p}: max deviation {np.abs(pk - T).max() / T:.3%}")
        if moved == 0:
            break
    if not quiet:
        log(f"polish: max deviation {np.abs(pk - T).max() / T:.3%}")
    return assign


def chain_push(G, assign, w, pts, K, tol, max_iter=3000, locked=None, quiet=False):
    """When greedy swaps stall, push weight along a path of neighbouring
    districts (A -> X1 -> ... -> Z) from the worst district to the nearest one
    on the other side of the target, one boundary bite per hop."""
    T = w.sum() / K
    mem = district_members(assign, K)
    pk = np.array([w[list(m)].sum() for m in mem], float)
    eu, ev = np.array(list(G.edges())).T

    def dadj():
        da, db = assign[eu], assign[ev]
        m = da != db
        adj = [set() for _ in range(K)]
        for x, y in zip(da[m], db[m]):
            adj[x].add(y); adj[y].add(x)
        return adj

    def move(u, P, Q):
        assign[u] = Q
        mem[P].discard(u); mem[Q].add(u)
        pk[P] -= w[u]; pk[Q] += w[u]

    def across(u, Q):
        """True when u touches Q only across a barrier (freeway, ridge, river)."""
        return all(G[u][x].get("barrier", False) for x in G[u] if assign[x] == Q)

    def hop(P, Q, want, depth=0):
        """Hand Q one connected bite of P close to `want`, never splitting P.
        At a choke point (e.g. the Shoestring strip) the block goes with the
        strip beyond it; if only empty land touches Q, that goes first."""
        if len(mem[P]) < 3:
            return None
        sub = G.subgraph(mem[P])
        cuts = set(nx.articulation_points(sub))
        border = [u for u in mem[P] if any(assign[v] == Q for v in G[u])
                  and (locked is None or not locked[u])]
        singles = [u for u in border if u not in cuts]
        full = [u for u in singles if w[u] > 0]
        if full:
            u = min(full, key=lambda x: (across(x, Q), abs(w[x] - want)))
            move(u, P, Q)
            return [u]
        bites = []
        for u in border:
            if u in cuts:
                comps = sorted(nx.connected_components(sub.subgraph(mem[P] - {u})),
                               key=lambda c: (w[list(c)].sum(), len(c)), reverse=True)
                lost = [u] + [x for c in comps[1:] for x in c]
                if len(lost) <= max(20, len(mem[P]) // 8) and (locked is None or not locked[lost].any()):
                    bites.append(lost)
        full = [b for b in bites if w[b].sum() > 0]
        if full:
            b = min(full, key=lambda b: (across(b[0], Q), abs(w[b].sum() - want)))
            for x in b:
                move(x, P, Q)
            return b
        empty = [[u] for u in singles] + bites
        if depth >= 300 or not empty:
            return None
        b = max(empty, key=len)
        for x in b:
            move(x, P, Q)
        more = hop(P, Q, want, depth + 1)
        if more is None:
            for x in reversed(b):
                move(x, Q, P)
            return None
        return b + more

    skip = set()
    best_ssq, since = np.inf, 0
    for it in range(max_iter):
        dev = np.abs(pk - T)
        if dev.max() / T <= tol:
            break
        ssq = (dev ** 2).sum()
        if ssq < best_ssq - 1e-6 * T * T:
            best_ssq, since = ssq, 0
        else:
            since += 1
            if since > 300:
                break
        order = [d for d in np.argsort(-dev) if d not in skip]
        if not order:
            break
        A = order[0]
        s = 1 if pk[A] > T else -1
        adj = dadj()
        prev, q, Zs = {A: None}, deque([A]), []
        while q and len(Zs) < 8:
            x = q.popleft()
            if x != A and (pk[x] - T) * s < 0:
                Zs.append(x)
            for y in adj[x]:
                if y not in prev:
                    prev[y] = x
                    q.append(y)
        success = False
        for Z in Zs:
            path = [Z]
            while prev[path[-1]] is not None:
                path.append(prev[path[-1]])
            path = path[::-1]
            if s < 0:
                path = path[::-1]
            want = min(dev[A], dev[Z])
            before = (dev ** 2).sum()
            snapshot, ok = [], True
            for P, Q in zip(path[:-1], path[1:]):
                us = hop(P, Q, want)
                if us is None:
                    ok = False
                    break
                snapshot += [(u, P, Q) for u in us]
                want = w[us[-1]]
            if ok and ((pk - T) ** 2).sum() < before:
                success = True
                break
            for u, P, Q in reversed(snapshot):
                move(u, Q, P)
        if success:
            skip.clear()
        else:
            skip.add(A)
        if it % 200 == 0 and not quiet:
            log(f"  chain {it}: max deviation {np.abs(pk - T).max() / T:.3%}")
    if not quiet:
        log(f"chain push: max deviation {np.abs(pk - T).max() / T:.3%}")
    return assign


# --------------------------------------------------------------------------
# 6. naming + stats + export
# --------------------------------------------------------------------------
COMPASS = r" \((North|South|East|West|Northeast|Northwest|Southeast|Southwest|Central)( \d+)?\)$"


def name_districts(units, assign, nc_names, n_cert, K):
    """Name = the current council (or uncovered area) most residents come from;
    districts sharing one get a compass suffix. `heir_of` is a one-to-one
    successor mapping, only when there is one district per council."""
    idx = {n: i for i, n in enumerate(nc_names)}
    M = np.zeros((K, len(nc_names)))
    np.add.at(M, (assign, units.nc_current.map(idx).values), units["pop"].values)
    dom = {d: nc_names[int(np.argmax(M[d]))] for d in range(K)}
    pts = np.c_[units.ipx, units.ipy]
    pw = units["pop"].values + 1e-6
    cen = {d: np.average(pts[assign == d], axis=0, weights=pw[assign == d]) for d in range(K)}
    names = {}
    for n in set(dom.values()):
        ds = [d for d in range(K) if dom[d] == n]
        if len(ds) == 1:
            names[ds[0]] = n
            continue
        mean = np.mean([cen[d] for d in ds], axis=0)
        spread = max(np.hypot(*(cen[d] - mean)) for d in ds)
        used = {}
        for d in ds:
            dx, dy = cen[d] - mean
            if np.hypot(dx, dy) < 0.25 * spread and len(ds) > 2:
                lab = "Central"
            elif len(ds) == 2:
                lab = ("East" if dx > 0 else "West") if abs(dx) > abs(dy) else ("North" if dy > 0 else "South")
            else:
                ang = (np.degrees(np.arctan2(dy, dx)) + 360) % 360
                lab = ["East", "Northeast", "North", "Northwest", "West", "Southwest", "South", "Southeast"][int(((ang + 22.5) % 360) // 45)]
            used[lab] = used.get(lab, 0) + 1
            names[d] = f"{n} ({lab}{'' if used[lab] == 1 else ' ' + str(used[lab])})"
    heirs = {d: "" for d in range(K)}
    if K == n_cert:
        r, c = linear_sum_assignment(-M[:, :n_cert])
        heirs = {int(d): (nc_names[k] if M[d, k] >= 0.01 * max(M[d].sum(), 1) else "") for d, k in zip(r, c)}
    return names, heirs


def district_layer(units: gpd.GeoDataFrame, bal: str, nc_pop: pd.Series) -> gpd.GeoDataFrame:
    K = units.district_id.nunique()
    T = units[bal].sum() / K
    Tp = units["pop"].sum() / K
    d = units[["district_id", "geometry"]].dissolve("district_id")
    d = d.join(units.groupby("district_id")[DEMO].sum())
    d["district_name"] = units.groupby("district_id")["district_name"].first()
    if "heir_of" in units:
        d["heir_of"] = units.groupby("district_id")["heir_of"].first()
    d["balance_on"] = bal
    d["target"] = round(T)
    d["deviation"] = (d[bal] - T).round().astype(int)
    d["dev_pct"] = ((d[bal] - T) / T * 100).round(3)
    d["target_pop"] = round(Tp)
    d["pop_dev_pct"] = ((d["pop"] - Tp) / Tp * 100).round(3)
    for c in ["hisp", "white", "black", "asian"]:
        d[f"pct_{c}"] = (d[f"pop_{c}"] / d["pop"].clip(lower=1) * 100).round(1)
    d["area_sqmi"] = (d.geometry.area / 2_589_988.11).round(2)
    d["density_sqmi"] = (d["pop"] / d["area_sqmi"].clip(lower=0.01)).round(0)
    d["polsby_popper"] = (4 * np.pi * d.geometry.area / d.geometry.length ** 2).round(3)
    import shapely
    d["reock"] = (d.geometry.area / (np.pi * shapely.minimum_bounding_radius(d.geometry.values) ** 2)).round(3)
    if "ipx" in units:
        # person-distance: how far residents live from their district's population centre
        pw = units["pop"] + 1e-6
        cx = (units.ipx * pw).groupby(units.district_id).sum() / pw.groupby(units.district_id).sum()
        cy = (units.ipy * pw).groupby(units.district_id).sum() / pw.groupby(units.district_id).sum()
        dist = np.hypot(units.ipx - units.district_id.map(cx), units.ipy - units.district_id.map(cy))
        d["mean_dist_km"] = ((dist * pw).groupby(units.district_id).sum() / pw.groupby(units.district_id).sum() / 1000).round(2)
    d["n_parts"] = d.geometry.apply(lambda g: len(getattr(g, "geoms", [g])))
    share = (units.groupby(["district_id", "nc_current"])["pop"].sum()
             .groupby(level=0, group_keys=False).apply(lambda x: (x / max(x.sum(), 1)).sort_values(ascending=False)))
    base = d.district_name.str.replace(COMPASS, "", regex=True)
    d["pct_from_namesake"] = [round(share.get((i, n), 0) * 100, 1) for i, n in zip(d.index, base)]
    d["source_ncs"] = [", ".join(f"{n.removesuffix(' NC')} {v:.0%}" for n, v in share.loc[i].head(4).items() if v >= 0.05)
                       if i in share.index.get_level_values(0) else "" for i in d.index]
    d["namesake_pop_2020"] = base.map(nc_pop).fillna(0).astype(int)
    return d.reset_index()


def _write_layer(gdf, gpkg, layer):
    gdf.to_crs(EXPORT_CRS).to_file(gpkg, layer=layer, driver="GPKG", engine="pyogrio",
                                   layer_options={"OVERWRITE": "YES"})


def export(units, nc, outdir: Path, meta: dict, bal: str, keep_units_layer=False, cut_edges=None):
    """Write the GeoPackage (+ GeoJSON/CSV). With keep_units_layer the units
    layer (which you may be editing in QGIS) is left untouched."""
    outdir.mkdir(parents=True, exist_ok=True)
    K = units.district_id.nunique()
    nc_pop = units.groupby("nc_current")["pop"].sum()
    nc_bal = units.groupby("nc_current")[bal].sum()
    dist = district_layer(units, bal, nc_pop)
    T = units[bal].sum() / K

    ncl = nc.copy()
    ncl["pop"] = ncl.NAME.map(nc_pop).fillna(0).astype(int)
    ncl[bal] = ncl.NAME.map(nc_bal).fillna(0).astype(int)
    if "certified" not in ncl:
        ncl["certified"] = True
    ncl["target"] = round(T)
    ncl["dev_pct"] = ((ncl[bal] - T) / T * 100).round(1)
    ncl["area_sqmi"] = (ncl.geometry.area / 2_589_988.11).round(2)

    gpkg = outdir / "la_nc_equal_pop.gpkg"
    if not keep_units_layer:
        gpkg.unlink(missing_ok=True)
    _write_layer(dist, gpkg, "districts_equal_pop")
    _write_layer(ncl, gpkg, "nc_current")
    if not keep_units_layer:
        ucols = (["unit_id", "district_id", "district_name"] + (["heir_of"] if "heir_of" in units else [])
                 + ["nc_current"] + DEMO + ["ALAND20", "AWATER20", "geometry"])
        _write_layer(units[ucols], gpkg, "units")
    write_qgis_styles(gpkg, K)

    dist.to_crs(EXPORT_CRS).to_file(outdir / "districts_equal_pop.geojson", driver="GeoJSON")
    ncl.to_crs(EXPORT_CRS).to_file(outdir / "nc_current.geojson", driver="GeoJSON")
    dist.drop(columns="geometry").to_csv(outdir / "districts_equal_pop.csv", index=False)
    units[["unit_id", "district_id"]].rename(columns={"unit_id": "GEOID20", "district_id": "District"}) \
        .to_csv(outdir / "unit_assignment.csv", index=False)
    tol = (meta.get("plan") or {}).get("tolerance", 0.01)
    splits = split_table(units, bal, T, tol)
    old = outdir / "nc_splits.csv"
    splits.to_csv(old, index=False)
    split_ = splits[splits.pieces > 1]
    pw = splits["pop"] / max(splits["pop"].sum(), 1)
    cert = ncl.certified.astype(bool)
    meta.update(dict(
        districts=int(K), balance_on=bal, total_pop=int(units["pop"].sum()), total_balanced=int(units[bal].sum()),
        target=round(T), target_pop=round(units["pop"].sum() / K),
        max_dev_pct=float(dist.dev_pct.abs().max()), max_pop_dev_pct=float(dist.pop_dev_pct.abs().max()),
        min_balanced=int(dist[bal].min()), max_balanced=int(dist[bal].max()),
        current_nc_min_pop=int(ncl.loc[cert, "pop"].min()), current_nc_max_pop=int(ncl.loc[cert, "pop"].max()),
        added_area_pop={r.NAME: int(r["pop"]) for _, r in ncl[~cert].iterrows()},
        multipart_districts=int((dist.n_parts > 1).sum()),
        councils_split=int(len(split_)), extra_pieces=int(splits.extra_pieces.sum()),
        effective_splits=round(float(splits.effective_splits.sum()), 2),
        split_pairs=round(float((splits.split_pairs * pw).sum()), 4),
        mean_polsby_popper=round(float(dist.polsby_popper.mean()), 3), mean_reock=round(float(dist.reock.mean()), 3),
        mean_dist_km=round(float(dist.mean_dist_km.mean()), 2) if "mean_dist_km" in dist else None,
        cut_edges=cut_edges))
    (outdir / "summary.json").write_text(json.dumps(meta, indent=2))
    log(json.dumps({k: v for k, v in meta.items() if k != "added_area_pop"}, indent=2))
    log(f"wrote {gpkg}")
    return dist, ncl


# --------------------------------------------------------------------------
# QGIS default styles stored in the GeoPackage (auto-applied on load)
# --------------------------------------------------------------------------
PALETTE = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948",
           "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac", "#86bcb6", "#d37295"]


def _cat_qml(field, K, opacity, label_field=None, outline="#ffffff", width="0.4"):
    cats, syms = [], []
    for i in range(1, K + 1):
        cats.append(f'<category value="{i}" label="{i}" symbol="{i-1}" render="true"/>')
        col = PALETTE[(i * 7) % len(PALETTE)]
        syms.append(f'''<symbol type="fill" name="{i-1}" alpha="{opacity}" clip_to_extent="1" force_rhr="0">
 <layer class="SimpleFill" enabled="1" pass="0" locked="0">
  <Option type="Map">
   <Option name="color" value="{col}" type="QString"/>
   <Option name="outline_color" value="{outline}" type="QString"/>
   <Option name="outline_width" value="{width}" type="QString"/>
   <Option name="outline_width_unit" value="MM" type="QString"/>
   <Option name="style" value="solid" type="QString"/>
   <Option name="outline_style" value="solid" type="QString"/>
  </Option>
 </layer>
</symbol>''')
    labels = ""
    if label_field:
        labels = f'''<labeling type="simple"><settings calloutType="simple">
 <text-style fieldName="{label_field}" fontSize="8" fontWeight="75" textColor="30,30,30,255" isExpression="0">
  <text-buffer bufferDraw="1" bufferSize="0.8" bufferColor="255,255,255,220" bufferSizeUnits="MM"/>
 </text-style>
 <placement placement="1" offsetType="0" overrunDistance="0"/>
 <rendering obstacle="1" scaleVisibility="0" fontLimitPixelSize="0"/>
</settings></labeling>'''
    return f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="Symbology|Labeling">
 <renderer-v2 type="categorizedSymbol" attr="{field}" symbollevels="0" enableorderby="0" forceraster="0">
  <categories>{"".join(cats)}</categories>
  <symbols>{"".join(syms)}</symbols>
 </renderer-v2>
 {labels}
 <labelsEnabled>{"1" if label_field else "0"}</labelsEnabled>
</qgis>'''


def _outline_qml(color="#222222", width="0.6", dash=True):
    return f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.34" styleCategories="Symbology">
 <renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0">
  <symbols>
   <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0">
    <layer class="SimpleFill" enabled="1" pass="0" locked="0">
     <Option type="Map">
      <Option name="color" value="0,0,0,0" type="QString"/>
      <Option name="outline_color" value="{color}" type="QString"/>
      <Option name="outline_width" value="{width}" type="QString"/>
      <Option name="outline_width_unit" value="MM" type="QString"/>
      <Option name="style" value="no" type="QString"/>
      <Option name="outline_style" value="{"dash" if dash else "solid"}" type="QString"/>
     </Option>
    </layer>
   </symbol>
  </symbols>
 </renderer-v2>
</qgis>'''


def write_qgis_styles(gpkg: Path, K: int):
    import sqlite3
    styles = [
        ("districts_equal_pop", "equal-pop districts", _cat_qml("district_id", K, "0.55", "district_name", "#ffffff", "0.6")),
        ("units", "units by district", _cat_qml("district_id", K, "0.65", None, "#ffffff", "0.05")),
        ("nc_current", "current NC outlines", _outline_qml()),
    ]
    con = sqlite3.connect(gpkg)
    con.execute("""CREATE TABLE IF NOT EXISTS layer_styles (
        id INTEGER PRIMARY KEY AUTOINCREMENT, f_table_catalog TEXT(256), f_table_schema TEXT(256),
        f_table_name TEXT(256), f_geometry_column TEXT(256), styleName TEXT(30), styleQML TEXT,
        styleSLD TEXT, useAsDefault BOOLEAN, description TEXT, owner TEXT(30), ui TEXT(30),
        update_time DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("DELETE FROM layer_styles")
    for tbl, name, qml in styles:
        con.execute("INSERT INTO layer_styles (f_table_catalog,f_table_schema,f_table_name,f_geometry_column,"
                    "styleName,styleQML,styleSLD,useAsDefault,description,owner,update_time) "
                    "VALUES ('','',?,'geom',?,?,'',1,?,'',strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                    (tbl, name, qml, name))
    try:
        con.execute("INSERT OR IGNORE INTO gpkg_contents (table_name,data_type,identifier) VALUES "
                    "('layer_styles','attributes','layer_styles')")
    except sqlite3.Error:
        pass
    con.commit()
    con.close()


# --------------------------------------------------------------------------
def cmd_build(args):
    plan = load_plan(args.config, {
        "districts": args.districts, "balance_on": args.balance_on, "tolerance": args.tolerance,
        "coverage": args.coverage, "units": args.units, "seeds": args.seeds, "keep_whole": args.keep_whole,
        "stick": args.stick, "keep_small_whole": args.keep_small_whole, "barriers": args.barriers,
        "smooth": args.smooth, "barrier_routing": args.barrier_routing, "barrier_strength": args.barrier_strength,
        "population_file": args.population_file, "random_seed": args.random_seed, "out": args.out})
    if args.add_barriers:
        extra = [b.strip() for b in args.add_barriers.split(",") if b.strip()]
        plan.barriers = list(plan.barriers) + [b if b in BARRIER_CHOICES else str(Path(b).resolve()) for b in extra]
        plan.validate()
    log("settings: " + ", ".join(f"{k}={v}" for k, v in asdict(plan).items()))
    K, tol = plan.districts, plan.tolerance
    nc = load_ncs(plan.coverage)
    n_cert = int(nc.certified.sum())
    blocks = load_units(plan.units, nc, plan)
    nc, blocks = consolidate_extras(nc, blocks)
    blocks.attrs["n_certified"] = n_cert
    names = nc.NAME.tolist()
    barriers = load_barriers(plan.barriers)
    GB = build_graph(blocks, barriers)
    wb = blocks[plan.balance_on].values.astype(float)
    if wb.sum() <= 0:
        raise SystemExit(f"no {plan.balance_on} in the chosen territory")
    T = wb.sum() / K
    labels = group_labels(blocks, plan.keep_whole)

    # with keep_whole: groups smaller than a district are kept whole where the tolerance allows
    small = set()
    if plan.keep_small_whole and labels is not None:
        gsum = pd.Series(wb).groupby(pd.Series(labels)).sum()
        small = set(gsum[gsum <= T * (1 + tol)].index)

    council_home = None
    if K == n_cert:
        h = blocks.nc_current.map({n: i for i, n in enumerate(names)}).values
        council_home = np.where(h < n_cert, h, -1)
    pts_b = np.c_[blocks.ipx, blocks.ipy].astype(float)
    netdist = NetworkDistance(GB, pts_b, plan.barrier_strength) if (barriers is not None and plan.barrier_routing) else None
    bassign = balance(blocks.assign(coarse=blocks.unit_id.str[:12] if plan.units == "blocks" else blocks.unit_id),
                      wb, K, plan, labels, council_home, netdist, GB)
    bassign = repair_contiguity(GB, bassign, wb, K)
    if len(np.unique(bassign)) != K:
        raise SystemExit("a district vanished during repair; try another random_seed or a lower stick")

    def deviation(a):
        return np.abs(np.bincount(a, weights=wb, minlength=K) - T).max() / T

    for _round in range(3):
        if deviation(bassign) <= tol:
            break
        bassign = polish(GB, bassign, wb, pts_b, K, tol)
        bassign = chain_push(GB, bassign, wb, pts_b, K, tol)
    locked, kept_split = None, []
    if small:
        bassign, locked, kept_split = consolidate_groups(GB, bassign, wb, pts_b, K, tol, labels, small)
    if plan.smooth:
        bassign = smooth_boundaries(GB, bassign, wb, K, tol, labels, locked=locked)
    bassign = fix_multipart(blocks, bassign, K)
    dev = deviation(bassign)
    if dev > tol:
        log(f"note: best reachable deviation is ±{dev:.2%}, above the ±{tol:.2%} asked for")

    dnames, heirs = name_districts(blocks, bassign, names, n_cert, K)
    order = sorted(range(K), key=lambda d: dnames[d])
    remap = {d: i + 1 for i, d in enumerate(order)}
    blocks["district_id"] = [remap[a] for a in bassign]
    blocks["district_name"] = [dnames[a] for a in bassign]
    blocks["heir_of"] = [heirs[a] for a in bassign]
    cut = int(sum(1 for x, y in GB.edges() if bassign[x] != bassign[y]))
    meta = {"plan": asdict(plan), "units": plan.units, "within_tolerance": bool(dev <= tol)}
    if small:
        meta["groups_left_split"] = kept_split
    export(blocks, nc, Path(plan.out), meta, plan.balance_on, cut_edges=cut)


def cmd_restat(args):
    gpkg = Path(args.gpkg)
    prev = gpkg.parent / "summary.json"
    old = json.loads(prev.read_text()) if prev.exists() else {}
    bal = args.balance_on or old.get("balance_on") or (old.get("plan") or {}).get("balance_on") or "pop"
    units = gpd.read_file(gpkg, layer="units").to_crs(WORK_CRS)
    nc = gpd.read_file(gpkg, layer="nc_current").to_crs(WORK_CRS)
    units["district_id"] = units.district_id.astype(int)
    for c in DEMO:
        if c not in units:
            if c == bal:
                raise SystemExit(f"the units layer has no {bal} column")
            units[c] = 0
    names = units.groupby("district_id")["district_name"].agg(lambda s: s.mode().iat[0])
    units["district_name"] = units.district_id.map(names)
    if "heir_of" in units:
        units["heir_of"] = units.district_id.map(units.groupby("district_id")["heir_of"].agg(lambda s: s.mode().iat[0]))
    keep = [c for c in ["NAME", "NC_ID", "SERVICE_RE", "certified", "geometry"] if c in nc]
    meta = {k: v for k, v in old.items() if k in ("plan", "units")}
    meta["restat_from"] = gpkg.name
    export(units, nc[keep], gpkg.parent, meta, bal, keep_units_layer=True)


URLS = {
    "blocks": "https://www2.census.gov/geo/tiger/TIGER2020/TABBLOCK20/tl_2020_06_tabblock20.zip",
    "pl": ("https://www2.census.gov/programs-surveys/decennial/2020/data/"
           "01-Redistricting_File--PL_94-171/California/ca2020.pl.zip"),
    "roads": "https://www2.census.gov/geo/tiger/TIGER2020/ROADS/tl_2020_06037_roads.zip",
    "linearwater": "https://www2.census.gov/geo/tiger/TIGER2020/LINEARWATER/tl_2020_06037_linearwater.zip",
    "areawater": "https://www2.census.gov/geo/tiger/TIGER2020/AREAWATER/tl_2020_06037_areawater.zip",
    "tracts": "https://www2.census.gov/geo/tiger/TIGER2020/TRACT/tl_2020_06_tract.zip",
    "rails": "https://www2.census.gov/geo/tiger/TIGER2020/RAILS/tl_2020_us_rails.zip",
    "nc": ("https://maps.lacity.org/lahub/rest/services/Boundaries/MapServer/18/query"
           "?where=1%3D1&outFields=*&outSR=4326&f=geojson"),
    "city": ("https://maps.lacity.org/lahub/rest/services/Boundaries/MapServer/7/query"
             "?where=1%3D1&outFields=*&outSR=4326&f=geojson"),
    "hoods": ("https://services5.arcgis.com/7nsPwEMP38bSkCjy/arcgis/rest/services/LA_Times_Neighborhoods/"
              "FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"),
}


def pl_to_csv(zip_path: Path, dst: Path):
    """Pull LA County block counts out of the 2020 P.L. 94-171 file:
    P1/P2 (residents by race/ethnicity), P3 (adults), H1 (housing units, occupied)."""
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        def read(name, cols):
            with z.open(name) as f:
                return pd.read_csv(f, sep="|", header=None, usecols=cols, dtype=str, encoding="latin-1")
        geo = read("cageo2020.pl", [2, 7, 8, 14])
        geo.columns = ["SUMLEV", "LOGRECNO", "GEOID", "COUNTY"]
        geo = geo[(geo.SUMLEV == "750") & (geo.COUNTY == "037")]
        s1 = read("ca000012020.pl", [4] + list(range(76, 87)))      # P2_001..P2_011
        s1.columns = ["LOGRECNO"] + [f"P2_{i:03d}" for i in range(1, 12)]
        s2 = read("ca000022020.pl", [4, 5, 149, 150])                # P3_001, H1_001, H1_002
        s2.columns = ["LOGRECNO", "P3_001", "H1_001", "H1_002"]
    m = geo.merge(s1, on="LOGRECNO").merge(s2, on="LOGRECNO")
    out = pd.DataFrame({"GEOID20": m.GEOID.str[-15:]})
    src = {"pop": "P2_001", "pop_hisp": "P2_002", "pop_white": "P2_005", "pop_black": "P2_006", "pop_aian": "P2_007",
           "pop_asian": "P2_008", "pop_nhpi": "P2_009", "pop_other": "P2_010", "pop_two": "P2_011",
           "vap": "P3_001", "housing_units": "H1_001", "households": "H1_002"}
    for k, v in src.items():
        out[k] = m[v].astype(int)
    assert int(out["pop"].sum()) == LA_COUNTY_POP_2020, "LA County 2020 total mismatch"
    out.to_csv(dst, index=False)
    log(f"census counts: {len(out):,} LA County blocks")


def cmd_fetch(args):
    """Download every input into data/ (about 600 MB, mostly census blocks)."""
    import urllib.request
    DATA.mkdir(parents=True, exist_ok=True)

    def get(key, dst, what, always=False):
        if dst.exists() and not (always or args.force):
            return
        log(f"downloading {what} ...")
        tmp = dst.with_suffix(dst.suffix + ".part")
        urllib.request.urlretrieve(URLS[key], tmp)
        tmp.replace(dst)

    get("blocks", BLOCKS_ZIP, "2020 census blocks for California (~400 MB)")
    if not PL_CSV.exists() or args.force or "households" not in PL_CSV.read_text().split("\n", 1)[0]:
        z = DATA / "ca2020.pl.zip"
        get("pl", z, "2020 redistricting counts for California (~80 MB)", always=True)
        pl_to_csv(z, PL_CSV)
        z.unlink()
    get("roads", ROADS_ZIP, "LA County roads (freeways, Mulholland)")
    get("linearwater", LINEARWATER_ZIP, "LA County rivers and streams")
    get("areawater", AREAWATER_ZIP, "LA County water areas")
    get("rails", RAILS_ZIP, "US railroads and rail transit (~30 MB)")
    if not TRACTS_SHP.exists():
        z = DATA / "tl_2020_06_tract.zip"
        get("tracts", z, "2020 census tracts for California", always=True)
        g = gpd.read_file(z, where="COUNTYFP = '037'").rename(columns=lambda c: c if c == "geometry" else c + "20")
        TRACTS_SHP.parent.mkdir(parents=True, exist_ok=True)
        g.to_file(TRACTS_SHP)
        z.unlink()
    for key, dst, what in [("nc", DATA / "nc_certified.geojson", "certified neighborhood councils"),
                           ("city", CITY_BOUNDARY, "City of LA boundary"),
                           ("hoods", NEIGHBORHOODS, "LA Times neighborhoods")]:
        try:
            get(key, dst, what, always=True)
        except Exception as e:
            log(f"could not refresh {what} ({e}); keeping the copy in data/" if dst.exists() else f"could not download {what}: {e}")
    log("done")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="draw districts from a plan file and/or flags")
    b.add_argument("--config", help="plan file (default: plan.toml next to this script, if present)")
    b.add_argument("--districts", type=int, help="how many districts to draw (99 = one per council)")
    b.add_argument("--balance-on", choices=list(BALANCE_CHOICES), help="census count every district gets an equal share of")
    b.add_argument("--tolerance", "--tol", type=float, help="largest allowed deviation, as a fraction (0.01 = ±1%%)")
    b.add_argument("--coverage", "--add-areas", help="all | councils | comma-separated LA Times neighborhoods")
    b.add_argument("--units", choices=["blocks", "tracts"])
    b.add_argument("--seeds", choices=["auto", "councils", "kmeans"])
    b.add_argument("--keep-whole", choices=["councils", "neighborhoods", "none"])
    b.add_argument("--stick", type=float, help="how hard to keep keep_whole groups intact (0 = not at all)")
    b.add_argument("--keep-small-whole", action=argparse.BooleanOptionalAction, default=None,
                   help="never split a keep_whole group smaller than a district unless the tolerance needs it")
    b.add_argument("--smooth", action=argparse.BooleanOptionalAction, default=None,
                   help="move lines onto barriers and council lines after balancing")
    b.add_argument("--barrier-routing", action=argparse.BooleanOptionalAction, default=None,
                   help="also measure distance along the street network, where crossing a barrier costs more")
    b.add_argument("--population-file", help="block-level counts to use instead of the census file (see README)")
    b.add_argument("--barriers", help="comma-separated: freeways, rail, la_river, mulholland, or paths to line files; '' for none")
    b.add_argument("--add-barriers", help="comma-separated barriers to add to the plan's list (e.g. your own line file)")
    b.add_argument("--barrier-strength", type=float, help="with --barrier-routing: distance multiplier for crossing (default 3)")
    b.add_argument("--random-seed", type=int)
    b.add_argument("--out", help="output folder")
    b.set_defaults(fn=cmd_build)
    f = sub.add_parser("fetch", help="download census and city data into data/")
    f.add_argument("--force", action="store_true", help="download everything again")
    f.set_defaults(fn=cmd_fetch)
    r = sub.add_parser("restat", help="recompute districts and stats after editing district_id in QGIS")
    r.add_argument("gpkg")
    r.add_argument("--balance-on", choices=list(BALANCE_CHOICES), help="default: what the build balanced")
    r.set_defaults(fn=cmd_restat)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
