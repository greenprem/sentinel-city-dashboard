"""Area-wise zoning of a city.

Zone sets, in order of preference:
- curated local files: data/boundaries/<slug>/zones/*.geojson (each feature needs properties.name)
- OpenStreetMap administrative subdivisions inside the city, fetched from Overpass (cached)
- a regular grid (always available), GRID_CELL_M on a side, clipped to the city
"""
import json
import logging

import numpy as np
import requests
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as shp_transform

from . import config

log = logging.getLogger(__name__)


def _load(path):
    d = json.loads(path.read_text())
    feats = [{"name": f["properties"].get("name") or f"Zone {i + 1}", "geometry": shape(f["geometry"]).buffer(0)}
             for i, f in enumerate(d["features"])]
    return d.get("label", path.stem.replace("_", " ").title()), d.get("order", 50), feats


def _overpass_subdivisions(city):
    """Admin areas one or two levels below the city, from OSM. Returns list or []."""
    osm = city.get("osm")
    if not osm or osm[0] != "relation":
        return []
    area_id = 3600000000 + int(osm[1])
    q = f"""[out:json][timeout:60];area({area_id})->.c;
    (relation(area.c)["boundary"="administrative"]["admin_level"~"^(6|7|8|9|10)$"];);out geom;"""
    try:
        r = requests.post(config.OVERPASS_URL, data={"data": q}, headers={"User-Agent": config.USER_AGENT}, timeout=90)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("Overpass unavailable: %s", e)
        return []
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    by_level = {}
    for el in r.json().get("elements", []):
        lines = [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                 for m in el.get("members", []) if m.get("type") == "way" and m.get("geometry")]
        polys = list(polygonize(unary_union(lines))) if lines else []
        if not polys:
            continue
        lvl = el["tags"].get("admin_level")
        by_level.setdefault(lvl, []).append({"name": el["tags"].get("name", str(el["id"])),
                                             "geometry": unary_union(polys).buffer(0)})
    # Most useful level: the coarsest one with at least 4 areas.
    for lvl in sorted(by_level, key=int):
        if len(by_level[lvl]) >= 4:
            return by_level[lvl]
    return []


def grid_zones(city_geom, utm_crs, cell=config.GRID_CELL_M):
    to_utm = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True).transform
    to_ll = Transformer.from_crs(utm_crs, "EPSG:4326", always_xy=True).transform
    g = shp_transform(to_utm, city_geom)
    minx, miny, maxx, maxy = g.bounds
    minx, miny = np.floor(minx / cell) * cell, np.floor(miny / cell) * cell
    zones = []
    rows = int(np.ceil((maxy - miny) / cell))
    cols = int(np.ceil((maxx - minx) / cell))
    for r in range(rows):
        for c in range(cols):
            cb = box(minx + c * cell, maxy - (r + 1) * cell, minx + (c + 1) * cell, maxy - r * cell)
            inter = cb.intersection(g)
            if inter.is_empty or inter.area < 0.15 * cell * cell:
                continue
            zones.append({"name": f"R{r + 1:02d}-C{c + 1:02d}", "geometry": shp_transform(to_ll, inter)})
    return zones


def zone_sets(city, utm_crs):
    """Return {key: {"label": str, "zones": [ {name, geometry} ]}}."""
    sets = {}
    zdir = config.BOUNDARY_DIR / city["slug"] / "zones"
    if zdir.exists():
        loaded = sorted(((p.stem,) + _load(p) for p in zdir.glob("*.geojson")), key=lambda t: (t[2], t[0]))
        for key, label, _order, feats in loaded:
            sets[key] = {"label": label, "zones": feats}
    if not sets:
        subs = _overpass_subdivisions(city)
        if subs:
            zdir.mkdir(parents=True, exist_ok=True)
            fc = {"type": "FeatureCollection", "label": "OSM administrative areas",
                  "features": [{"type": "Feature", "properties": {"name": z["name"]}, "geometry": mapping(z["geometry"])}
                               for z in subs]}
            (zdir / "osm_admin.geojson").write_text(json.dumps(fc))
            sets["osm_admin"] = {"label": "OSM administrative areas", "zones": subs}
    sets["grid"] = {"label": f"{config.GRID_CELL_M / 1000:g} km grid",
                    "zones": grid_zones(city["geometry"], utm_crs)}
    # Clip everything to the city so stats never include pixels outside it.
    for s in sets.values():
        clipped = []
        for z in s["zones"]:
            gi = z["geometry"].intersection(city["geometry"])
            if not gi.is_empty:
                clipped.append({"name": z["name"], "geometry": gi})
        s["zones"] = clipped
    return sets
