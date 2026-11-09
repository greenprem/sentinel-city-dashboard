"""Build data/places/places.csv, the offline list behind the city box's suggestions.

Inputs (both GeoNames-derived, downloaded from raw.githubusercontent.com):
  world-cities.csv  https://raw.githubusercontent.com/datasets/world-cities/main/data/world-cities.csv
                    (name, country, subcountry; ODC-PDDL)
  cities15000.csv   https://raw.githubusercontent.com/curran/data/gh-pages/geonames/cities15000.csv
                    (name, population; used only to rank suggestions)
Every row is a populated place (city or town), so no buildings, schools or roads can appear.
Usage: python scripts/build_places.py world-cities.csv cities15000.csv
"""
import csv
import sys
import unicodedata
from pathlib import Path


def fold(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t).lower() if c.isalnum() and ord(c) < 128)


def main(wc_path, pop_path):
    pop = {}
    with open(pop_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            k = fold(r["name"])
            pop[k] = max(pop.get(k, 0), int(r["population"] or 0))
    rows = []
    with open(wc_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((r["name"], r["subcountry"], r["country"], pop.get(fold(r["name"]), 0)))
    rows.sort(key=lambda r: -r[3])
    out = Path(__file__).resolve().parents[1] / "data" / "places" / "places.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "subcountry", "country", "population"])
        w.writerows(rows)
    print(f"{len(rows)} places -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
