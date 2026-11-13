# 99 Equal Councils — LA Neighborhood Councils at equal population

Los Angeles has 99 certified Neighborhood Councils. Counted from 2020 Census blocks, they range from
**2,959** residents (Hermon) to **96,588** (Wilshire Center–Koreatown), a 33× spread. Another **84,600** people
live in parts of the city no certified council covers: Brentwood, Pacific Palisades, and pockets such as
Vermont Knolls, Boyle Heights and Broadway-Manchester.
This project redraws **the entire City of Los Angeles** (3,899,133 residents in 2020) into
**99 contiguous districts of equal population** (target 39,385 each, within ±1%),
in the spirit of Neil Freeman's [Electoral College Reform](https://fakeisthenewreal.org/reform/) map.

The defaults follow California's fair-map rules for cities (the FAIR MAPS Act): equal population first, then
contiguity, then lines residents recognize, then compactness. Today's council lines aren't a constraint; the new
districts replace them. Everything is adjustable: the number of districts, the tolerance, which census count gets
equalized, what (if anything) to keep together and which lines to follow. Set it in a plan file, on the command
line, or in a form inside QGIS.

## Fairness rules

The redraw borrows the ranked criteria of the FAIR MAPS Act
([Cal. Elections Code §21130](https://law.justia.com/codes/california/code-elec/division-21/chapter-2/section-21130/)).
No law binds a map of neighborhood councils; it's simply the best-tested local rulebook. In order:

| Rank | Criterion | How the pipeline applies it |
|---|---|---|
| Floor | Substantially equal population | Every district within `tolerance` of an equal share (±1% by default) of 2020 Census residents. |
| 1 | Contiguous; corners and water don't count | Districts are grown on a shared-edge block graph. Any detached piece left at the end joins the district it shares the longest boundary with. |
| 2 | Keep neighborhoods and communities whole | Off by default. The rule exists so city council districts don't carve up neighborhoods; here the districts *are* the neighborhood units, and matching today's council lines would just freeze them. Set `keep_whole` to `councils` or `neighborhoods` (LA Times) to turn it on: groups smaller than a district then stay whole unless the tolerance needs a split, and the build logs each one. |
| 4 | Follow lines residents recognize | District lines are slid onto freeways, rail, the LA River and the Mulholland ridge wherever that stays within tolerance. |
| 5 | Compact | Balancing minimizes residents' distance to their district's center. Polsby–Popper, Reock, person-distance and cut edges are all reported. |
| Banned | Favoring or hurting an incumbent, candidate or party | No such data is ever used. See below. |

**Neutral inputs (a rule, not a default):** nothing about incumbents, board members' addresses, neighborhood
council election results, stakeholder rolls or partisan data enters this pipeline. The only inputs are census
counts, census geography, and the city's council, boundary and neighborhood layers.

**How much changes, measured by people.** `nc_splits.csv` shows how each of today's councils is spread across
the new districts. It's a report, not a constraint. `effective_splits` is 1/Σp² − 1, where p is each district's
share of the council's residents (an even 50/50 split is 1, a 90/10 split is 0.22), and `split_pairs` is the chance
two of its residents land in different districts. `min_pieces` is the fewest districts a council of its size must
span; `extra_pieces` counts the rest.

**Population base.** FAIR MAPS has cities count incarcerated people at their last home address. The Statewide
Database publishes adjusted block counts that do this
([redistricting database](https://statewidedatabase.org/redistricting2021/redistricting_database.html)). Download
the block-level CSV and point `population_file` at it, mapping column names with `population_columns` (see its
data dictionary). The default stays the unadjusted census file.

**Undercount.** The 2020 Census most likely undercounted Hispanic, high-poverty, renter-heavy and immigrant
neighborhoods in LA County, so equal on paper isn't equal on the ground there. Don't treat the tightest tolerance
as the fairest.

**The default map in numbers** (2020 residents, ±1%): every district is one piece apart from the harbor islands.
Mean Polsby–Popper is 0.34 and mean Reock 0.50, against 0.14 and 0.42 when the map tried to keep councils together,
and district lines cross 4,143 block edges instead of 7,434. Residents sit 1.2 km from their district's center on
average. 99 of the 110 councils and uncovered areas end up in more than one district; `nc_splits.csv` shows how.

## What's here

| Path | What it is |
|---|---|
| `redistrict.py` | The pipeline: `fetch` (download inputs), `build` (draw districts), `restat` (recompute after hand edits) |
| `plan.toml` | The default plan, every setting commented |
| `plans/` | Example plans: adults, households, 15 districts, natural borders, keep councils, blank slate, precise |
| `qgis/` | Two QGIS Processing tools: **Redraw council districts** and **Recalculate districts after editing** |
| `render_png.py` | Quick-look PNG of a build |
| `figures/` | The blog figures (`blog_figs.py`) and the interactive comparison map (`web_data.py`, `build_web.py`, `map_template.html`) |
| `requirements.txt` | Python packages |

A build writes to `out/` (or the plan's `out`):

| File | What it is |
|---|---|
| `la_nc_equal_pop.gpkg` | **Open this in QGIS.** Three layers with styles built in: `districts_equal_pop`, `nc_current`, `units` |
| `districts_equal_pop.geojson`, `nc_current.geojson` | The districts and today's councils, web-friendly |
| `nc_splits.csv` | How each of today's councils is spread across the new districts |
| `districts_equal_pop.csv` | Stats per district (counts, deviation, race/ethnicity, area, compactness, source councils) |
| `unit_assignment.csv` | `GEOID20 → District` lookup (the plan itself, as a plain table) |
| `summary.json` | The settings used and the headline numbers |

`data/` and `out/` are git-ignored. `python redistrict.py fetch` fills `data/` (about 600 MB, mostly census blocks).
The build published with the blog post (made before these defaults), and the post that explains it, are at
[chrisdower.com/blog/la-99-equal-councils](https://chrisdower.com/blog/la-99-equal-councils/).

## Setup (once)

```bash
cd la-equal-councils
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python redistrict.py fetch             # census blocks, counts, roads, water, council and city boundaries
```

## Change the plan

Every setting lives in `plan.toml`; flags on the command line override it.

| Setting | Flag | What it does |
|---|---|---|
| `districts` | `--districts 51` | How many districts. 99 starts one district from each current council and keeps names and `heir_of`; any other number starts from population-weighted k-means. |
| `balance_on` | `--balance-on vap` | What every district gets an equal share of: `pop` residents, `vap` adults 18+, `households` occupied homes, `housing_units` all homes. |
| `tolerance` | `--tolerance 0.02` | Largest allowed difference from an equal share, as a fraction (0.02 = ±2%). With blocks and 99 districts, about 0.005 is the floor. |
| `coverage` | `--coverage councils` | `all` = the whole city; `councils` = certified councils only; or LA Times neighborhood names. |
| `units` | `--units tracts` | `blocks` (precise) or `tracts` (about a minute, roughly ±5%). |
| `seeds` | `--seeds kmeans` | `auto`, `councils` or `kmeans`. |
| `keep_whole` | `--keep-whole neighborhoods` | Optional groups to avoid splitting: `none` (default), `councils` (today's) or `neighborhoods` (LA Times). |
| `keep_small_whole` | `--no-keep-small-whole` | With `keep_whole`: groups smaller than a district stay whole unless the tolerance needs a split. |
| `stick` | `--stick 3` | With `keep_whole`: how hard to keep bigger groups in few pieces. |
| `barriers` | `--barriers freeways,mulholland` | Lines district boundaries should follow: `freeways`, `rail`, `la_river`, `mulholland` (the Santa Monica Mountains ridge), or any line or polygon file (`--add-barriers` appends). `--barriers ''` for none. |
| `smooth` | `--no-smooth` | After balancing, slide lines onto barriers where the tolerance allows. |
| `barrier_routing` | `--barrier-routing` | Also measure distance along the block network, so a crossing counts `barrier_strength` times. Slower, stronger. |
| `barrier_strength` | `--barrier-strength 5` | With `barrier_routing`: crossing a barrier counts as this many times the distance. |
| `population_file` | `--population-file adj.csv` | Block counts to use instead of the census file (see Population base). |
| `random_seed` | `--random-seed 7` | Different k-means starting points. |
| `out` | `--out out/try-2` | Output folder. |

```bash
.venv/bin/python redistrict.py build                                   # plan.toml
.venv/bin/python redistrict.py build --config plans/households.toml    # an example plan
.venv/bin/python redistrict.py build --districts 15 --units tracts     # quick experiment
```

A block-level build takes 10–30 minutes; tracts take about a minute.

Barriers work in two ways. After balancing, `smooth` trades boundary blocks between neighbors whenever the line gets
cheaper: a block edge along a barrier costs less than an ordinary street, and the trade has to stay within tolerance
and keep both districts in one piece. With `barrier_routing`, balancing also measures distance along the block
network, where a step across a barrier counts `barrier_strength` times. They're a nudge, not a wall. Districts here
hold about 39,000 people, much smaller than the spaces between freeways, so many freeway miles still fall inside a
district. For a hard edge, edit those blocks by hand in QGIS.

Balancing on something other than residents means districts differ in residents: `dev_pct` measures the balanced
count, `pop_dev_pct` measures residents.

## In QGIS

**Install the tools (once):** Processing Toolbox → the Python icon → *Add Script to Toolbox…* → pick
`qgis/la_councils_build.py`, then again for `qgis/la_councils_restat.py`. They appear under
*Scripts → LA equal councils*.

**Redraw council districts** is a form for every setting above. Leave a field on *From plan file* (or blank) to use
`plan.toml`. Point *Project folder* at this folder; *Python interpreter* defaults to `.venv` in it. Draw your own
barriers in any line layer and pick it under *Your own barrier lines*. Each run lands in its own layer group named
after its settings, so runs sit side by side.

**Edit by hand:** in the *Census blocks* layer of a run, Toggle Editing, select blocks, use the Field Calculator to set
`district_id`, and Save. Then run **Recalculate districts after editing** on that layer: the districts layer and stats
are rebuilt and the map refreshes. `n_parts` above 1 means you've split a district.

A redistricting plugin such as [Redistricting for QGIS](https://plugins.qgis.org/plugins/Redistricting/) can also track
district totals live while you edit; point it at the blocks layer with `district_id`.

## Layers and key fields

- **`units`**: the building blocks (28,131 census blocks). `district_id` is the assignment. Also carries `pop`,
  `pop_hisp`, `pop_white`, `pop_black`, `pop_asian`, …, `vap`, `households`, `housing_units`, and `nc_current`
  (the council it's in today; for uncovered land, its LA Times neighborhood, or `OTHER CITY LAND` for slivers
  with under 100 residents).
- **`districts_equal_pop`**: one row per district. `balance_on`, `target`, `deviation` and `dev_pct` describe the
  balanced count; `pop_dev_pct` is residents. `polsby_popper` is compactness (0–1). `source_ncs` lists where its
  residents live today. `heir_of` is a one-to-one successor mapping to the 99 certified councils (only when there are
  99 districts).
- **`nc_current`**: today's boundaries with `pop`, the balanced count, `dev_pct` and `certified`.

Names: each district takes the name of the current council (or neighborhood) most of its residents come from; several
districts drawn mostly from one big council get a compass suffix.

## Method

1. **Territory**: every census block whose internal point falls inside a certified council, plus (with
   `coverage = "all"`) every block in the rest of the city boundary.
2. **Seeds**: one per council at its population-weighted center when there are 99 districts, otherwise k-means++.
3. **Balance**: an optimal-transport linear program assigns block groups to centers, minimizing squared distance with
   every district holding exactly 1/N of the balanced count; centers re-center (Lloyd iterations) and repeat, then one
   pass at block level. `stick` penalizes splitting the `keep_whole` groups. With `barrier_routing`, distance is measured along
   the block network.
4. **Repair**: any district split into pieces keeps its largest piece; stray pieces join the neighbor they share the
   most boundary with.
5. **Polish** (only if still outside tolerance): boundary blocks swap between neighboring districts (never breaking
   contiguity) to shrink the largest deviation. When swaps stall, the count is pushed along a chain of neighboring
   districts to the nearest one with room, passing through empty blocks and choke points like the Shoestring strip.
6. **Keep groups whole** (only with `keep_whole`): each split small group, cheapest first, moves wholly into the
   district holding most of it, then the rest rebalances around it. If that can't stay within tolerance and contiguous,
   the group is put back and left split.
7. **Smooth**: lines slide onto barriers within the tolerance (see above); any piece detached across water or at a
   corner joins the district it shares the longest boundary with.
8. **Name**: dominant source, plus the `heir_of` mapping (Hungarian assignment on shared residents).

## Data

- Neighborhood council boundaries: City of Los Angeles, certified council layer (`maps.lacity.org` Boundaries/MapServer/18).
- City boundary: City of Los Angeles (`maps.lacity.org` Boundaries/MapServer/7).
- Names for uncovered land and the `neighborhoods` option: LA Times Neighborhood Boundaries on LA GeoHub.
- Counts: 2020 Census P.L. 94-171 redistricting file by block, from census.gov: residents and race/ethnicity (P2),
  adults (P3), housing units and occupied units (H1). LA County totals 10,014,009 residents, 3,591,981 housing units,
  3,420,628 households.
- Geometry, roads and water: Census TIGER/Line 2020.

## Limits worth knowing

- **The tightest tolerance depends on the plan.** Blocks can't be split, so 99 districts bottom out around ±0.5%; the
  Harbor reaches the rest of the city only through the Shoestring strip, and one block holds 8,727 people (UCLA's
  residence halls). If a plan can't reach its tolerance, the build says so and keeps the best it found.
- Fewer, bigger districts balance more easily; many small ones less so.
- Districts whose `n_parts` is above 1 have a sliver that touches the rest only across water or at a corner
  (a few harbor islands have no land neighbor at all).
