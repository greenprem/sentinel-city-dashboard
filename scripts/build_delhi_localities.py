"""Build Delhi's locality layers (run once; outputs are committed under data/boundaries/delhi/).

Outputs
- zones/wards.geojson         290 municipal wards (DataMeet), names made unique, used as the
                              "Localities" zone set: the smallest polygons freely available here.
- zones/subdistricts.geojson  27 Census-2011 sub-districts (datta07/INDIAN-SHAPEFILES).
- localities.json             a search index of named places (Saket, Badarpur, Connaught Place...)
                              each mapped to the ward polygon that contains it, so a search for a
                              place that is not itself a ward name still finds its section.

Locality points come from the GeoNames postal-code dump for India (CC BY 4.0), keeping only rows
with accuracy 4 (matched to a GeoNames place), plus a few well-known places GeoNames lacks.

    python scripts/build_delhi_localities.py IN.txt subdistricts_raw.geojson
IN.txt: https://download.geonames.org/export/zip/IN.zip
subdistricts: https://raw.githubusercontent.com/datta07/INDIAN-SHAPEFILES/master/STATES/DELHI/DELHI_SUBDISTRICTS.geojson
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

from shapely.geometry import Point, mapping, shape

ROOT = Path(__file__).resolve().parent.parent / "data" / "boundaries" / "delhi"

# Approximate centres of major areas missing from the accuracy-4 GeoNames rows.
EXTRA = [
    ("Dwarka", 28.5921, 77.0460),
    ("Rohini", 28.7360, 77.1130),
    ("Pitampura", 28.7034, 77.1319),
    ("Janakpuri", 28.6219, 77.0878),
]


# Suggestions must be areas people live or work in, not single sites: postal rows named after
# a building, institution, landmark or road are dropped.
NOT_AN_AREA = re.compile(
    r"\b(airport|gate|house|bhawan|bhavan|court|complex|school|college|university|institute|hospital|"
    r"station|camp|office|road|marg|lane|chowk crossing|market|mall|place$|stadium|temple|masjid$|"
    r"secretariat|parliament|ministry|exchange|depot|power|plant|btps|iit|aiims|jnu)\b", re.I)


# Area names that happen to contain one of the words above.
KEEP = {"Connaught Place"}


def clean(name):
    name = re.sub(r"\s*\((South|North|East|West|Central|New|North West|North East|South West|South East)?\s*Delhi\)", "", name)
    return name.strip()


def main(geonames_txt, subdistricts_src):
    wards_path = ROOT / "zones" / "wards.geojson"
    wards = json.loads(wards_path.read_text())
    counts = Counter(f["properties"]["name"] for f in wards["features"])
    seen = Counter()
    for f in wards["features"]:
        n = f["properties"]["name"]
        if counts[n] > 1:
            seen[n] += 1
            f["properties"]["name"] = f"{n} ({seen[n]})"
    wards["label"] = "Localities (municipal wards)"
    wards["order"] = 0
    wards_path.write_text(json.dumps(wards))

    sd = json.loads(Path(subdistricts_src).read_text())
    out = [{"type": "Feature", "properties": {"name": f["properties"]["sdtname"]},
            "geometry": mapping(shape(f["geometry"]).buffer(0).simplify(0.0001))} for f in sd["features"]]
    (ROOT / "zones" / "subdistricts.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "label": "Sub-districts (Census 2011)", "order": 2, "features": out}))
    d = json.loads((ROOT / "zones" / "districts.geojson").read_text())
    d["order"] = 1
    (ROOT / "zones" / "districts.geojson").write_text(json.dumps(d))

    polys = [(f["properties"]["name"], shape(f["geometry"])) for f in wards["features"]]
    points = []
    for line in Path(geonames_txt).read_text(encoding="utf-8").splitlines():
        c = line.split("\t")
        if len(c) < 12 or c[3] != "Delhi" or c[11] != "4":
            continue
        name = clean(c[2])
        if NOT_AN_AREA.search(name) and name not in KEEP:
            continue
        points.append((name, float(c[9]), float(c[10]), c[1], "GeoNames postal codes"))
    points += [(n, la, lo, "", "manual (approximate centre)") for n, la, lo in EXTRA]
    index, used = [], set()
    for name, lat, lon, pin, src in points:
        if name.lower() in used:
            continue
        p = Point(lon, lat)
        ward = next((w for w, g in polys if g.contains(p)), None)
        if not ward:
            continue
        used.add(name.lower())
        index.append({"name": name, "lat": lat, "lon": lon, "ward": ward, "pincode": pin, "source": src})
    index.sort(key=lambda r: r["name"])
    (ROOT / "localities.json").write_text(json.dumps(index, indent=1))
    print(f"{len(index)} named localities mapped onto {len(polys)} ward polygons")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
