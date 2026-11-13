"""Build the interactive comparison map (one self-contained HTML page).

    python figures/web_data.py out/la_nc_equal_pop.gpkg out/web_data.json
    python figures/build_web.py out out/web_data.json out/map.html
"""
import json
import sys
from pathlib import Path

outdir, data_json, dst = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
data = json.loads(data_json.read_text())
s = json.loads((outdir / "summary.json").read_text())
plan = s.get("plan") or {}
units = plan.get("units") or s.get("units", "blocks")
coverage = plan.get("coverage") or ",".join(s.get("added_areas") or [])
unit = {"blocks": "2020 census blocks", "tracts": "2020 census tracts"}[units]
areas = [a for a in coverage.split(",") if a and a != "councils"]
extra = (" plus every part of the city outside them" if "all" in areas else " plus " + " and ".join(areas)) if areas else ""
data["method"] = (
    f"Method: {unit} inside today's certified NC territory{extra} ({s['total_pop']:,} residents) were assigned to "
    f"{s['districts']} districts by an optimal-transport balancing step, then polished so every district is contiguous. "
    f"Largest deviation from the equal share: ±{s['max_dev_pct']:.1f}%. "
    "Names come from the current council or neighborhood most residents are drawn from.")
if areas:
    data["method"] += (" Land outside certified councils comes from the City of LA boundary, labelled by LA Times neighborhood (LA GeoHub)."
                       if "all" in areas else
                       f" {' and '.join(areas)} boundaries: LA Times neighborhoods (LA GeoHub), clipped to land outside certified councils.")
template = (Path(__file__).parent / "map_template.html").read_text()
html = template.replace("__DATA__", json.dumps(data, separators=(",", ":")))
dst.write_text(html)
print(dst, len(html) // 1024, "KB")
