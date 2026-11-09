"""City name -> boundary polygon.

Order of attempts:
1. A local boundary under data/boundaries/<slug>/boundary.geojson (offline, curated).
2. Nominatim (OpenStreetMap) with polygon output.
Results from Nominatim are cached locally so later runs work offline.
"""
import json
import logging
import re

import requests
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import config

log = logging.getLogger(__name__)

# Common aliases so "Delhi, India", "New Delhi", "NCT of Delhi" all hit the same local folder.
ALIASES = {
    "delhi": "delhi", "new-delhi": "delhi", "nct-of-delhi": "delhi", "delhi-india": "delhi",
    "new-delhi-india": "delhi", "national-capital-territory-of-delhi": "delhi",
}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if s in ALIASES:
        return ALIASES[s]
    head = s.split("-india")[0] if s.endswith("-india") else s
    return ALIASES.get(head, s)


# OpenStreetMap feature types that describe an area (a coverage), as opposed to a point entity
# such as a building, school, shop or road. Only these are suggested or geocoded.
AREA_PLACE_TYPES = {"city", "town", "village", "municipality", "borough", "suburb", "quarter", "neighbourhood",
                    "city_district", "district", "county", "state_district", "state", "region", "province",
                    "hamlet", "locality", "island"}


def is_area(hit):
    cls, typ = hit.get("category") or hit.get("class"), hit.get("type")
    if cls == "boundary" and typ == "administrative":
        return True
    return cls == "place" and typ in AREA_PLACE_TYPES


_suggest_cache = {}
_last_call = [0.0]
_places = []


def _fold(text):
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _offline_places():
    """34k cities and towns worldwide (data/places/places.csv, built by scripts/build_places.py from GeoNames),
    so suggestions work even when Nominatim is unreachable. Every row is a populated place, never a building."""
    if not _places:
        import csv
        p = config.DATA_DIR / "places" / "places.csv"
        if p.exists():
            with p.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    parts = [r["name"]] + [x for x in (r["subcountry"], r["country"]) if x and x != r["name"]]
                    _places.append((_fold(r["name"]), ", ".join(parts), int(r["population"] or 0)))
    return _places


_nominatim_down_until = [0.0]


def suggest_places(q, limit=8):
    """Autocomplete for the city box: local boundary folders first, then Nominatim area results.
    Point entities (buildings, schools, roads, shops...) are never returned."""
    import time
    qn = re.sub(r"[^a-z0-9]", "", q.lower())
    out = []
    for d in sorted(config.BOUNDARY_DIR.iterdir()) if config.BOUNDARY_DIR.exists() else []:
        meta_path = d / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        label = meta.get("display_name", d.name.title())
        keys = [label, d.name] + [a for a, v in ALIASES.items() if v == d.name]
        if any(qn and qn in re.sub(r"[^a-z0-9]", "", k.lower()) for k in keys):
            out.append({"label": label, "value": label, "kind": "city", "source": "saved boundary"})
    seen = {x["label"] for x in out}
    local_slugs = {d.name for d in config.BOUNDARY_DIR.iterdir()} if config.BOUNDARY_DIR.exists() else set()
    head = _fold(q.split(",")[0])
    if len(head) >= 2:
        hits = [(0 if n == head else 1 if n.startswith(head) else 2, -pop, label)
                for n, label, pop in _offline_places() if head in n]
        for _, _, label in sorted(hits)[:limit * 2]:
            if label not in seen and not (local_slugs and slugify(label) in local_slugs):
                out.append({"label": label, "value": label, "kind": "city / town", "source": "GeoNames"})
                seen.add(label)
    if len(qn) < 3:
        return out[:limit]
    if q.lower() not in _suggest_cache and time.time() < _nominatim_down_until[0]:
        _suggest_cache[q.lower()] = []
    if q.lower() not in _suggest_cache:
        # Nominatim's usage policy allows about one request per second.
        wait = 1.0 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = requests.get(f"{config.NOMINATIM_URL}/search", params={
                "q": q, "format": "jsonv2", "limit": 15, "featureType": "settlement", "addressdetails": 1,
            }, headers={"User-Agent": config.USER_AGENT}, timeout=6)
            r.raise_for_status()
            hits = r.json()
        except Exception as e:  # noqa: BLE001
            log.info("Nominatim suggestions unavailable: %s", e)
            _nominatim_down_until[0] = time.time() + 300   # don't slow down typing; retry in 5 minutes
            hits = []
        res = []
        for h in hits:
            if not is_area(h) or h.get("osm_type") == "node" and h.get("type") not in AREA_PLACE_TYPES:
                continue
            a = h.get("address", {})
            parts = [h.get("name") or h["display_name"].split(",")[0]]
            for k in ("state", "country"):
                if a.get(k) and a[k] not in parts:
                    parts.append(a[k])
            label = ", ".join(parts)
            if label not in {x["label"] for x in res}:
                res.append({"label": label, "value": label, "kind": h.get("addresstype") or h.get("type"),
                            "source": "OpenStreetMap"})
        _suggest_cache[q.lower()] = res
    # OpenStreetMap results (which include districts and neighbourhoods) go ahead of the offline town list.
    local = [x for x in out if x["source"] == "saved boundary"]
    osm = [x for x in _suggest_cache[q.lower()] if x["label"] not in seen]
    return (local + osm + [x for x in out if x["source"] != "saved boundary"])[:limit]


def _read_geojson_geom(path):
    d = json.loads(path.read_text())
    feats = d["features"] if d.get("type") == "FeatureCollection" else [d]
    return unary_union([shape(f["geometry"]) for f in feats]).buffer(0)


def geocode_city(name: str):
    """Return dict(slug, name, geometry (shapely, EPSG:4326), source)."""
    slug = slugify(name)
    local = config.BOUNDARY_DIR / slug / "boundary.geojson"
    if local.exists():
        meta_path = local.parent / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        return {"slug": slug, "name": meta.get("display_name", name),
                "geometry": _read_geojson_geom(local), "source": meta.get("source", "local boundary file")}

    log.info("Geocoding %s via Nominatim", name)
    r = requests.get(f"{config.NOMINATIM_URL}/search", params={
        "q": name, "format": "jsonv2", "polygon_geojson": 1, "limit": 5, "polygon_threshold": 0.0005,
    }, headers={"User-Agent": config.USER_AGENT}, timeout=30)
    r.raise_for_status()
    hits = [h for h in r.json() if h.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon") and is_area(h)]
    if not hits:
        raise ValueError(f"No polygon boundary found for '{name}'. Add one at {local}.")
    # Prefer administrative boundaries, then the largest by importance.
    hits.sort(key=lambda h: (h.get("category") != "boundary", -float(h.get("importance") or 0)))
    h = hits[0]
    geom = shape(h["geojson"]).buffer(0)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": h["display_name"]}, "geometry": mapping(geom)}]}))
    (local.parent / "meta.json").write_text(json.dumps(
        {"display_name": h["display_name"], "source": "OpenStreetMap Nominatim", "osm_id": h.get("osm_id"),
         "osm_type": h.get("osm_type")}, indent=2))
    return {"slug": slug, "name": h["display_name"], "geometry": geom, "source": "OpenStreetMap Nominatim",
            "osm": (h.get("osm_type"), h.get("osm_id"))}
